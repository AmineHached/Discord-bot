import os
import re
import json
import datetime
import pytz
import aiohttp

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Load .env when available (development only)
if load_dotenv:
    load_dotenv()

# Read token from environment. Railway and other hosts set env vars for you.
TOKEN = os.getenv("BOT_TOKEN") or os.getenv("DISCORD_TOKEN")

# =====================
# Server / Roles / Channels
# =====================

GUILD_NAME = "Kaikei"

RECRUIT_ROLE_NAME = "🌱 Recruit"
MEMBER_ROLE_NAME = "🎮 Member"
APPROVAL_CHANNEL_NAME = "apply-here"

# =====================
# Approval Permissions
# =====================

# Only users with these roles can approve applications
APPROVAL_ROLES = ["👑 Guild Master", "👑 Vice Master", "⚔️ Warlord", "🛡️ Officer"]

# ====================
# Scheduled Event Config
# ====================

EVENT_NAME = "Guild Party"
EVENT_DESCRIPTION = (
    "Join the Guild Party for an exciting gathering dedicated to bringing all guild members together in a fun atmosphere."
)

# Voice channel the event is attached to (must match exactly)
EVENT_VOICE_CHANNEL_NAME = "Guild Voice"

# Optional cover image (put the file next to this script)
# If file is missing, event will be created without an image.
EVENT_IMAGE_PATH = "20262228254.png"

# Schedule: Weekdays at 8:30 PM (local time)
TIMEZONE = "Africa/Tunis"
EVENT_HOUR = 19  
EVENT_MINUTE = 00
EVENT_DURATION_MINUTES = 120

# Reminders posted to this text channel
REMINDER_CHANNEL_NAME = "events-signups"
PING_ROLE_NAME = "🎮 Member"  # ping this role in reminders (create a dedicated role if you want)

# =====================
# Raid-Helper Sync Config
# =====================

# Bot name Raid-Helper uses (must match exactly)
RAID_HELPER_BOT_NAME = "Raid-Helper"

# Channel where Raid-Helper posts its event embeds
RAID_HELPER_CHANNEL_NAME = "events-signups"

# Default duration given to created scheduled events (minutes)
RAID_EVENT_DURATION_MINUTES = 60

# File to persist message_id → scheduled_event_id mapping across restarts
RAID_EVENT_MAP_FILE = "raid_event_map.json"

# =====================
# Bot Setup
# =====================

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

tz = pytz.timezone(TIMEZONE)
scheduler = AsyncIOScheduler(timezone=tz)

# =====================
# Helpers
# =====================

async def get_guild():
    return discord.utils.get(bot.guilds, name=GUILD_NAME)

async def get_text_channel(guild: discord.Guild, name: str):
    return discord.utils.get(guild.text_channels, name=name)

async def get_voice_channel(guild: discord.Guild, name: str):
    return discord.utils.get(guild.voice_channels, name=name)

def next_weekday_event_start_local(now_local: datetime.datetime) -> datetime.datetime:
    # If it's before today's event time and today is weekday -> today
    event_today = now_local.replace(hour=EVENT_HOUR, minute=EVENT_MINUTE, second=0, microsecond=0)

    if now_local.weekday() <= 4 and now_local < event_today:
        return event_today

    # Otherwise, find next weekday
    d = now_local
    for _ in range(7):
        d = d + datetime.timedelta(days=1)
        if d.weekday() <= 4:
            return d.replace(hour=EVENT_HOUR, minute=EVENT_MINUTE, second=0, microsecond=0)

    # Fallback (should never hit)
    return (now_local + datetime.timedelta(days=1)).replace(hour=EVENT_HOUR, minute=EVENT_MINUTE, second=0, microsecond=0)

async def send_reminder(text: str):
    guild = await get_guild()
    if guild is None:
        return

    channel = await get_text_channel(guild, REMINDER_CHANNEL_NAME)
    if channel is None:
        return

    await channel.send(
        f"@🎮 Member {text}",
        allowed_mentions=discord.AllowedMentions(everyone=True)
    )

async def fetch_event_image_bytes():
    try:
        with open(EVENT_IMAGE_PATH, "rb") as f:
            return f.read()
    except Exception:
        return None

async def event_already_exists(guild: discord.Guild, start_time_utc: datetime.datetime) -> bool:
    try:
        events = await guild.fetch_scheduled_events()
    except Exception:
        return False

    # Consider it same event if name matches and start time within 2 minutes
    for ev in events:
        if ev.name != EVENT_NAME:
            continue
        if ev.start_time is None:
            continue
        delta = abs((ev.start_time - start_time_utc).total_seconds())
        if delta <= 120:
            return True
    return False

async def create_next_guild_party_event():
    guild = await get_guild()
    if guild is None:
        return

    voice_channel = await get_voice_channel(guild, EVENT_VOICE_CHANNEL_NAME)
    if voice_channel is None:
        return

    now_local = datetime.datetime.now(tz)
    start_local = next_weekday_event_start_local(now_local)
    end_local = start_local + datetime.timedelta(minutes=EVENT_DURATION_MINUTES)

    start_utc = start_local.astimezone(datetime.timezone.utc)
    end_utc = end_local.astimezone(datetime.timezone.utc)

    if await event_already_exists(guild, start_utc):
        return

    image_bytes = await fetch_event_image_bytes()

    await guild.create_scheduled_event(
        name=EVENT_NAME,
        description=EVENT_DESCRIPTION,
        start_time=start_utc,
        end_time=end_utc,
        channel=voice_channel,
        entity_type=discord.EntityType.voice,
        privacy_level=discord.PrivacyLevel.guild_only,
        image=image_bytes,
    )

# =====================
# Raid-Helper Sync Helpers
# =====================

raid_event_map: dict = {}

def load_raid_event_map():
    global raid_event_map
    try:
        with open(RAID_EVENT_MAP_FILE, "r") as f:
            raid_event_map = json.load(f)
    except Exception:
        raid_event_map = {}

def save_raid_event_map():
    try:
        with open(RAID_EVENT_MAP_FILE, "w") as f:
            json.dump(raid_event_map, f)
    except Exception as e:
        print(f"[WARN] Could not save raid_event_map: {e}")

def is_raid_helper_message(message: discord.Message) -> bool:
    return (
        message.guild is not None
        and message.author.bot
        and message.author.name == RAID_HELPER_BOT_NAME
        and message.channel.name == RAID_HELPER_CHANNEL_NAME
    )

def fix_spaced_title(text: str) -> str:
    """Convert 'B R E A K I N G   A R M Y' -> 'BREAKING ARMY'."""
    return re.sub(r'(?<=[A-Za-z]) (?=[A-Za-z])', '', text).strip()

def embed_all_text(embed: discord.Embed) -> str:
    parts = []
    if embed.title:
        parts.append(embed.title)
    if embed.description:
        parts.append(embed.description)
    if embed.author and embed.author.name:
        parts.append(embed.author.name)
    if embed.footer and embed.footer.text:
        parts.append(embed.footer.text)
    for field in embed.fields:
        if field.name:
            parts.append(field.name)
        if field.value:
            parts.append(field.value)
    return " ".join(parts)

def parse_raid_embed(message: discord.Message):
    """Parse a Raid-Helper embed. Returns (name, start_utc, end_utc, description, image_url) or None."""
    if not message.embeds:
        return None

    embed = message.embeds[0]
    all_text = embed_all_text(embed)

    # Event name (title or author, fix spacing like 'B R E A K I N G   A R M Y')
    raw_name = embed.title or (embed.author.name if embed.author else None)
    if not raw_name:
        return None
    name = fix_spaced_title(raw_name)[:100]

    # Raid-Helper posts a placeholder embed first ("Loading…") and edits it later.
    # Detect by a field named "Loading..." or all_text that is only whitespace/invisible chars.
    if any(f.name and 'loading' in f.name.lower() for f in embed.fields):
        return None
    stripped = all_text.replace('\u200e', '').replace('\u200f', '').strip()
    if not stripped or stripped.lower() == name.lower():
        return None

    # Start time — prefer Discord timestamp <t:UNIX:?> (most reliable)
    ts_match = re.search(r'<t:(\d+)', all_text)
    if ts_match:
        start_utc = datetime.datetime.fromtimestamp(int(ts_match.group(1)), tz=datetime.timezone.utc)
    else:
        # Fallback: search all embed text parts for date/time patterns
        date_str = None
        time_str = None

        # Collect all text sources (fields, description, footer)
        search_texts = []
        for field in embed.fields:
            if field.value:
                search_texts.append(field.value)
        if embed.description:
            search_texts.append(embed.description)
        if embed.footer and embed.footer.text:
            search_texts.append(embed.footer.text)
        # Also try all_text as a last resort
        search_texts.append(all_text)

        for val in search_texts:
            if not date_str:
                # "Saturday, March 7, 2026" or "March 7, 2026" or "March 7 2026"
                dm = re.search(r'(?:[A-Z][a-z]+,\s+)?([A-Z][a-z]+ \d{1,2},? \d{4})', val)
                if dm:
                    date_str = dm.group(1).replace(',', '').strip()
            if not date_str:
                # MM/DD/YYYY or M/D/YYYY
                dm2 = re.search(r'\b(\d{1,2}/\d{1,2}/\d{4})\b', val)
                if dm2:
                    date_str = dm2.group(1)  # handled separately below
            if not date_str:
                # ISO  YYYY-MM-DD
                dm3 = re.search(r'\b(\d{4}-\d{2}-\d{2})\b', val)
                if dm3:
                    date_str = dm3.group(1)
            if not date_str:
                # "March 7" without year — use current year
                dm4 = re.search(r'\b([A-Z][a-z]+ \d{1,2})\b', val)
                if dm4:
                    date_str = f"{dm4.group(1)} {datetime.datetime.now(tz).year}"
            if not time_str:
                # "8:30 PM", "08:30PM", "8:30pm"
                tm = re.search(r'(\d{1,2}:\d{2}\s?[APap][Mm])', val)
                if tm:
                    time_str = tm.group(1).replace(' ', '')
                else:
                    # 24-hour "20:30" — only if we haven't found a 12-hour time yet
                    tm24 = re.search(r'\b(\d{1,2}:\d{2})\b', val)
                    if tm24:
                        time_str = tm24.group(1)

        if not date_str or not time_str:
            # Log exactly what text was found in the embed to aid debugging
            debug_parts = []
            for field in embed.fields:
                if field.value:
                    debug_parts.append(f"  field[{field.name!r}]: {field.value!r}")
            if embed.description:
                debug_parts.append(f"  description: {embed.description!r}")
            if embed.footer and embed.footer.text:
                debug_parts.append(f"  footer: {embed.footer.text!r}")
            print(f"[WARN] Could not parse date/time from Raid-Helper embed '{name}' "
                  f"(date_str={date_str!r}, time_str={time_str!r})")
            if debug_parts:
                print("[WARN]  Embed text dump:")
                for dp in debug_parts:
                    print(f"[WARN] {dp}")
            return None
        try:
            is_ampm = bool(re.search(r'[APap][Mm]$', time_str))
            time_fmt = "%I:%M%p" if is_ampm else "%H:%M"

            # Determine date format
            dt_local = None
            if re.match(r'\d{1,2}/\d{1,2}/\d{4}', date_str):
                # Try MM/DD/YYYY then DD/MM/YYYY
                for dfmt in ("%m/%d/%Y", "%d/%m/%Y"):
                    try:
                        dt_local = tz.localize(datetime.datetime.strptime(f"{date_str} {time_str}", f"{dfmt} {time_fmt}"))
                        break
                    except ValueError:
                        pass
            elif re.match(r'\d{4}-\d{2}-\d{2}', date_str):
                dt_local = tz.localize(datetime.datetime.strptime(f"{date_str} {time_str}", f"%Y-%m-%d {time_fmt}"))
            else:
                # "March 7 2026" style
                dt_local = tz.localize(datetime.datetime.strptime(f"{date_str} {time_str}", f"%B %d %Y {time_fmt}"))

            if dt_local is None:
                raise ValueError(f"Unrecognised date_str format: {date_str!r}")
            start_utc = dt_local.astimezone(datetime.timezone.utc)
        except Exception as e:
            print(f"[WARN] Date parse error for '{name}': {e} (date_str={date_str!r}, time_str={time_str!r})")
            return None

    end_utc = start_utc + datetime.timedelta(minutes=RAID_EVENT_DURATION_MINUTES)

    # Description
    description = (embed.description or "").strip()[:1000] or f"Raid event: {name}"

    # Image
    image_url = None
    if embed.image and embed.image.url:
        image_url = embed.image.url
    elif embed.thumbnail and embed.thumbnail.url:
        image_url = embed.thumbnail.url

    return name, start_utc, end_utc, description, image_url

async def download_image(url) -> bytes:
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception:
        pass
    return None

async def sync_raid_event(message: discord.Message):
    """Create or update a Discord scheduled event from a Raid-Helper message."""
    guild = message.guild
    parsed = parse_raid_embed(message)
    if not parsed:
        return

    name, start_utc, end_utc, description, image_url = parsed
    image_bytes = await download_image(image_url)
    msg_id = str(message.id)

    # Check if we already have a scheduled event for this message
    existing = None
    mapped_id = raid_event_map.get(msg_id)
    if mapped_id:
        try:
            existing = await guild.fetch_scheduled_event(int(mapped_id))
        except Exception:
            existing = None

    if existing:
        try:
            kwargs = dict(
                name=name,
                description=description,
                start_time=start_utc,
                end_time=end_utc,
            )
            if image_bytes:
                kwargs["image"] = image_bytes
            await existing.edit(**kwargs)
            print(f"[INFO] Updated scheduled event '{name}'")
        except Exception as e:
            print(f"[ERROR] Failed to update event '{name}': {e}")
    else:
        try:
            kwargs = dict(
                name=name,
                description=description,
                start_time=start_utc,
                end_time=end_utc,
                entity_type=discord.EntityType.external,
                privacy_level=discord.PrivacyLevel.guild_only,
                location="Kaikei",
            )
            if image_bytes:
                kwargs["image"] = image_bytes
            event = await guild.create_scheduled_event(**kwargs)
            raid_event_map[msg_id] = str(event.id)
            save_raid_event_map()
            print(f"[INFO] Created scheduled event '{name}'")
        except Exception as e:
            print(f"[ERROR] Failed to create event '{name}': {e}")

# =====================
# Event: Bot Ready
# =====================

@bot.event
async def on_ready():
    print(f"Bot is online as {bot.user}")
    load_raid_event_map()

    if scheduler.running:
        return

    # Create the next scheduled event immediately on startup
    bot.loop.create_task(create_next_guild_party_event())

    # Every weekday shortly after midnight: ensure next event exists
    scheduler.add_job(
        lambda: bot.loop.create_task(create_next_guild_party_event()),
        CronTrigger(day_of_week="mon-fri", hour=0, minute=10),
        id="create_next_event",
        replace_existing=True,
    )

    # Reminder 15 minutes before (Mon–Fri)
    _reminder_total_minutes = EVENT_HOUR * 60 + EVENT_MINUTE - 15
    _reminder_hour = _reminder_total_minutes // 60
    _reminder_minute = _reminder_total_minutes % 60
    scheduler.add_job(
        lambda: bot.loop.create_task(
            send_reminder(f"⏰ **{EVENT_NAME}** starts in **15 minutes**! See yaa in the Guild Hall 🌿")
        ),
        CronTrigger(day_of_week="mon-fri", hour=_reminder_hour, minute=_reminder_minute),
        id="guild_party_reminder",
        replace_existing=True,
    )

    # Start message at event time (Mon–Fri)
    scheduler.add_job(
        lambda: bot.loop.create_task(
            send_reminder(f"✅ **{EVENT_NAME}** is starting now! Jump in the Guild Hall 🎮")
        ),
        CronTrigger(day_of_week="mon-fri", hour=EVENT_HOUR, minute=EVENT_MINUTE),
        id="guild_party_start",
        replace_existing=True,
    )

    scheduler.start()

# =====================
# Raid-Helper Message Listeners
# =====================

@bot.event
async def on_message(message: discord.Message):
    if is_raid_helper_message(message):
        try:
            await sync_raid_event(message)
        except Exception as e:
            print(f"[ERROR] Raid-Helper on_message sync failed: {e}")
    await bot.process_commands(message)

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if is_raid_helper_message(after):
        try:
            await sync_raid_event(after)
        except Exception as e:
            print(f"[ERROR] Raid-Helper on_message_edit sync failed: {e}")

@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    msg_id = str(payload.message_id)
    mapped_id = raid_event_map.get(msg_id)
    if not mapped_id:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    try:
        event = await guild.fetch_scheduled_event(int(mapped_id))
        await event.delete()
        print(f"[INFO] Deleted scheduled event for removed Raid-Helper message")
    except Exception as e:
        print(f"[ERROR] Failed to delete event: {e}")

    raid_event_map.pop(msg_id, None)
    save_raid_event_map()

# =====================
# Handle Approval Reactions
# =====================

@bot.event
async def on_raw_reaction_add(payload):
    # Only handle checkmark reactions
    if payload.emoji.name != "✅":
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    # Get the member who reacted
    try:
        reacting_member = await guild.fetch_member(payload.user_id)
    except:
        return

    # Check if reacting member has approval permission
    has_approval_role = any(
        discord.utils.get(guild.roles, name=role_name) in reacting_member.roles
        for role_name in APPROVAL_ROLES
    )

    if not has_approval_role:
        return  # User doesn't have permission to approve

    # Make sure this is the correct channel
    channel = guild.get_channel(payload.channel_id)
    if channel.name != APPROVAL_CHANNEL_NAME:
        return

    # Get the message that was reacted to
    try:
        message = await channel.fetch_message(payload.message_id)
    except:
        return

    # Make sure this is not a bot or system message
    if message.author.bot:
        return

    # Get the author (applicant)
    applicant = message.author

    # Get the roles by name
    member_role = discord.utils.get(guild.roles, name=MEMBER_ROLE_NAME)
    recruit_role = discord.utils.get(guild.roles, name=RECRUIT_ROLE_NAME)

    # If roles don’t exist, do nothing
    if member_role is None or recruit_role is None:
        return

    # Add Member role
    try:
        await applicant.add_roles(member_role)
    except Exception as e:
        print("Failed to add role:", e)

    # Remove Recruit role
    try:
        await applicant.remove_roles(recruit_role)
    except Exception as e:
        print("Failed to remove role:", e)

    # Find recruit-status channel
    status_channel = discord.utils.get(
        guild.text_channels,
        name="recruit-status"
    )

    # Find reaction roles channel
    reaction_roles_channel = discord.utils.get(
        guild.text_channels,
        name="reaction-roles"
    )

    if status_channel:

        rr_mention = (
            reaction_roles_channel.mention
            if reaction_roles_channel else "#reaction-roles"
        )

        welcome_message = f"""
    # 🌿 Welcome to **Kaikei**, {applicant.mention}!

    Your application has been **approved** ✅  
    You are now officially a **🎮 Member**.

    📌 Next Steps

    🎭 **Choose your roles**
    ➡ Please go to {rr_mention} and select your roles.

    🎮 **Get invited in-game**
    ➡ Ping **at least an Officer or higher**  
    to receive your guild invite.

    >>> ⚔️ Welcome to Kaikei.
     Fight together. Grow stronger.
    """

        await status_channel.send(welcome_message)

# =====================
# Run Bot
# =====================

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Set your BOT_TOKEN environment variable before running the bot.")
    bot.run(TOKEN)