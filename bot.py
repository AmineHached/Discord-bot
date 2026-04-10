import os
import re
import json
import datetime
import pytz
import aiohttp
import asyncio

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
# Guild Party Reminder Config
# ====================

EVENT_NAME = "Guild Party"

# Schedule: Weekdays at 8:30 PM (local time)
TIMEZONE = "Africa/Tunis"
EVENT_HOUR = int(os.getenv("EVENT_HOUR", "19"))
EVENT_MINUTE = int(os.getenv("EVENT_MINUTE", "0"))
EVENT_DAYS = os.getenv("EVENT_DAYS", "mon-sun")
GUILD_ID = os.getenv("GUILD_ID")

# Reminders posted to this text channel
REMINDER_CHANNEL_NAME = "events-signups"
REMINDER_CHANNEL_ID = os.getenv("REMINDER_CHANNEL_ID", "1492097338548813935")
PING_ROLE_NAME = "🎮 Member"  # ping this role in reminders (create a dedicated role if you want)

# =====================
# Raid-Helper Sync Config
# =====================

# Bot name Raid-Helper uses (must match exactly, unless ID is set)
RAID_HELPER_BOT_NAME = "Raid-Helper"
RAID_HELPER_BOT_ID = os.getenv("RAID_HELPER_BOT_ID")

# Channel where Raid-Helper posts its event embeds
RAID_HELPER_CHANNEL_NAME = "events-signups"
RAID_HELPER_CHANNEL_ID = os.getenv("RAID_HELPER_CHANNEL_ID")

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
    if GUILD_ID:
        try:
            guild = bot.get_guild(int(GUILD_ID))
            if guild is not None:
                return guild
            print(f"[WARN] GUILD_ID={GUILD_ID} not found in connected guilds.")
        except ValueError:
            print(f"[WARN] GUILD_ID is not a valid integer: {GUILD_ID!r}")

    guild = discord.utils.get(bot.guilds, name=GUILD_NAME)
    if guild is None:
        visible = ", ".join(g.name for g in bot.guilds) or "<none>"
        print(f"[WARN] Guild named '{GUILD_NAME}' was not found. Visible guilds: {visible}")
    return guild

async def get_text_channel(guild: discord.Guild, name: str, channel_id: str = None):
    if channel_id:
        try:
            channel = guild.get_channel(int(channel_id))
            if channel is not None:
                return channel
        except ValueError:
            print(f"[WARN] Channel ID is not a valid integer: {channel_id!r}")
    return discord.utils.get(guild.text_channels, name=name)

async def send_reminder(text: str):
    guild = await get_guild()
    if guild is None:
        print("[WARN] Reminder skipped: target guild not found.")
        return

    channel = await get_text_channel(guild, REMINDER_CHANNEL_NAME, REMINDER_CHANNEL_ID)
    if channel is None:
        print(
            f"[WARN] Reminder skipped: channel '{REMINDER_CHANNEL_NAME}' not found in guild '{guild.name}'."
        )
        return

    ping_role = discord.utils.get(guild.roles, name=PING_ROLE_NAME)
    if ping_role is None:
        print(f"[WARN] Ping role '{PING_ROLE_NAME}' not found. Sending reminder without ping.")
        try:
            await channel.send(text)
            print(f"[INFO] Reminder sent to #{channel.name} without role ping.")
        except Exception as e:
            print(f"[ERROR] Failed to send reminder without ping: {e}")
        return

    try:
        await channel.send(
            f"{ping_role.mention} {text}",
            allowed_mentions=discord.AllowedMentions(roles=[ping_role])
        )
        print(f"[INFO] Reminder sent to #{channel.name} with role ping {ping_role.name!r}.")
    except Exception as e:
        print(f"[ERROR] Failed to send reminder with ping: {e}")

# =====================
# Raid-Helper Sync Helpers
# =====================

raid_event_map: dict = {}
raid_event_in_flight: set = set()

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
    if message.guild is None:
        return False

    if RAID_HELPER_CHANNEL_ID:
        try:
            if str(message.channel.id) != str(RAID_HELPER_CHANNEL_ID):
                return False
        except Exception:
            return False
    else:
        if message.channel.name != RAID_HELPER_CHANNEL_NAME:
            return False

    author = message.author

    if RAID_HELPER_BOT_ID:
        return str(author.id) == str(RAID_HELPER_BOT_ID)

    # Allow either bot user or webhook with matching name/display name
    if not (author.bot or message.webhook_id):
        return False

    target = RAID_HELPER_BOT_NAME.casefold()
    name = (author.name or "").casefold()
    display = (getattr(author, "display_name", "") or "").casefold()
    return name == target or display == target

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

    for embed in message.embeds:
        all_text = embed_all_text(embed)

        # Event name (title or author, fix spacing like 'B R E A K I N G   A R M Y')
        raw_name = embed.title or (embed.author.name if embed.author else None)
        if not raw_name:
            continue
        name = fix_spaced_title(raw_name)[:100]

        # Raid-Helper posts a placeholder embed first ("Loading...") and edits it later.
        if any(f.name and "loading" in f.name.lower() for f in embed.fields):
            continue
        stripped = all_text.replace("\u200e", "").replace("\u200f", "").strip()
        if not stripped or stripped.lower() == name.lower():
            continue

        # Start time — prefer Discord timestamp <t:UNIX:?> (most reliable)
        ts_match = re.search(r"<t:(\d+)", all_text)
        if ts_match:
            start_utc = datetime.datetime.fromtimestamp(int(ts_match.group(1)), tz=datetime.timezone.utc)
        else:
            # Fallback: search all embed text parts for date/time patterns
            date_str = None
            time_str = None

            search_texts = []
            for field in embed.fields:
                if field.value:
                    search_texts.append(field.value)
            if embed.description:
                search_texts.append(embed.description)
            if embed.footer and embed.footer.text:
                search_texts.append(embed.footer.text)
            search_texts.append(all_text)

            for val in search_texts:
                if not date_str:
                    dm = re.search(r"(?:[A-Z][a-z]+,\s+)?([A-Z][a-z]+ \d{1,2},? \d{4})", val)
                    if dm:
                        date_str = dm.group(1).replace(",", "").strip()
                if not date_str:
                    dm2 = re.search(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", val)
                    if dm2:
                        date_str = dm2.group(1)
                if not date_str:
                    dm3 = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", val)
                    if dm3:
                        date_str = dm3.group(1)
                if not date_str:
                    dm4 = re.search(r"\b([A-Z][a-z]+ \d{1,2})\b", val)
                    if dm4:
                        date_str = f"{dm4.group(1)} {datetime.datetime.now(tz).year}"
                if not time_str:
                    tm = re.search(r"(\d{1,2}:\d{2}\s?[APap][Mm])", val)
                    if tm:
                        time_str = tm.group(1).replace(" ", "")
                    else:
                        tm24 = re.search(r"\b(\d{1,2}:\d{2})\b", val)
                        if tm24:
                            time_str = tm24.group(1)

            if not date_str or not time_str:
                debug_parts = []
                for field in embed.fields:
                    if field.value:
                        debug_parts.append(f"  field[{field.name!r}]: {field.value!r}")
                if embed.description:
                    debug_parts.append(f"  description: {embed.description!r}")
                if embed.footer and embed.footer.text:
                    debug_parts.append(f"  footer: {embed.footer.text!r}")
                print(
                    f"[WARN] Could not parse date/time from Raid-Helper embed '{name}' "
                    f"(date_str={date_str!r}, time_str={time_str!r})"
                )
                if debug_parts:
                    print("[WARN]  Embed text dump:")
                    for dp in debug_parts:
                        print(f"[WARN] {dp}")
                continue
            try:
                is_ampm = bool(re.search(r"[APap][Mm]$", time_str))
                time_fmt = "%I:%M%p" if is_ampm else "%H:%M"

                dt_local = None
                if re.match(r"\d{1,2}/\d{1,2}/\d{4}", date_str):
                    for dfmt in ("%m/%d/%Y", "%d/%m/%Y"):
                        try:
                            dt_local = tz.localize(
                                datetime.datetime.strptime(f"{date_str} {time_str}", f"{dfmt} {time_fmt}")
                            )
                            break
                        except ValueError:
                            pass
                elif re.match(r"\d{4}-\d{2}-\d{2}", date_str):
                    dt_local = tz.localize(
                        datetime.datetime.strptime(f"{date_str} {time_str}", f"%Y-%m-%d {time_fmt}")
                    )
                else:
                    dt_local = tz.localize(
                        datetime.datetime.strptime(f"{date_str} {time_str}", f"%B %d %Y {time_fmt}")
                    )

                if dt_local is None:
                    raise ValueError(f"Unrecognised date_str format: {date_str!r}")
                start_utc = dt_local.astimezone(datetime.timezone.utc)
            except Exception as e:
                print(
                    f"[WARN] Date parse error for '{name}': {e} "
                    f"(date_str={date_str!r}, time_str={time_str!r})"
                )
                continue

        end_utc = start_utc + datetime.timedelta(minutes=RAID_EVENT_DURATION_MINUTES)

        description = (embed.description or "").strip()[:1000] or f"Raid event: {name}"

        image_url = None
        if embed.image and embed.image.url:
            image_url = embed.image.url
        elif embed.thumbnail and embed.thumbnail.url:
            image_url = embed.thumbnail.url

        return name, start_utc, end_utc, description, image_url

    return None

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
        print(f"[WARN] Raid-Helper message parsed as None (msg_id={message.id})")
        return

    name, start_utc, end_utc, description, image_url = parsed
    image_bytes = await download_image(image_url)
    msg_id = str(message.id)

    if msg_id in raid_event_in_flight:
        return

    raid_event_in_flight.add(msg_id)
    try:

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
                except discord.NotFound:
                    raid_event_map.pop(msg_id, None)
                    save_raid_event_map()
                    existing = None
                except Exception as e:
                    print(f"[ERROR] Failed to update event '{name}': {e}")

            if not existing:
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
    finally:
        raid_event_in_flight.discard(msg_id)

# =====================
# Event: Bot Ready
# =====================

@bot.event
async def on_ready():
    print(f"Bot is online as {bot.user}")
    print(
        f"[INFO] Reminder schedule: days='{EVENT_DAYS}', time={EVENT_HOUR:02d}:{EVENT_MINUTE:02d}, tz='{TIMEZONE}'"
    )
    print(f"[INFO] Current local scheduler time: {datetime.datetime.now(tz).isoformat()}")
    load_raid_event_map()

    if scheduler.running:
        return

    # Reminder 15 minutes before (Mon–Fri)
    _reminder_total_minutes = EVENT_HOUR * 60 + EVENT_MINUTE - 15
    _reminder_hour = _reminder_total_minutes // 60
    _reminder_minute = _reminder_total_minutes % 60
    scheduler.add_job(
        lambda: bot.loop.create_task(
            send_reminder(f"⏰ **{EVENT_NAME}** starts in **15 minutes**! See yaa in the Guild Hall 🌿")
        ),
        CronTrigger(day_of_week=EVENT_DAYS, hour=_reminder_hour, minute=_reminder_minute),
        id="guild_party_reminder",
        replace_existing=True,
    )

    # Start message at event time (Mon–Fri)
    scheduler.add_job(
        lambda: bot.loop.create_task(
            send_reminder(f"✅ **{EVENT_NAME}** is starting now! Jump in the Guild Hall 🎮")
        ),
        CronTrigger(day_of_week=EVENT_DAYS, hour=EVENT_HOUR, minute=EVENT_MINUTE),
        id="guild_party_start",
        replace_existing=True,
    )

    scheduler.start()

    reminder_job = scheduler.get_job("guild_party_reminder")
    start_job = scheduler.get_job("guild_party_start")
    if reminder_job is not None:
        print(f"[INFO] Next reminder run: {reminder_job.next_run_time}")
    if start_job is not None:
        print(f"[INFO] Next start run: {start_job.next_run_time}")

# =====================
# Raid-Helper Message Listeners
# =====================

@bot.event
async def on_message(message: discord.Message):
    if message.guild and message.channel and message.channel.name == RAID_HELPER_CHANNEL_NAME:
        author_name = getattr(message.author, "name", None)
        author_display = getattr(message.author, "display_name", None)
        print(
            "[INFO] Message in raid-helper channel "
            f"(msg_id={message.id}, author={author_name!r}, display={author_display!r}, "
            f"bot={message.author.bot}, webhook_id={message.webhook_id})"
        )
    if is_raid_helper_message(message):
        try:
            print(f"[INFO] Raid-Helper message detected (msg_id={message.id})")
            await sync_raid_event(message)
        except Exception as e:
            print(f"[ERROR] Raid-Helper on_message sync failed: {e}")
    await bot.process_commands(message)

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if after.guild and after.channel and after.channel.name == RAID_HELPER_CHANNEL_NAME:
        author_name = getattr(after.author, "name", None)
        author_display = getattr(after.author, "display_name", None)
        embed_count = len(after.embeds) if after.embeds else 0
        print(
            "[INFO] Edited message in raid-helper channel "
            f"(msg_id={after.id}, author={author_name!r}, display={author_display!r}, "
            f"bot={after.author.bot}, webhook_id={after.webhook_id}, embeds={embed_count})"
        )
    if is_raid_helper_message(after):
        try:
            print(f"[INFO] Raid-Helper message edited (msg_id={after.id})")
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