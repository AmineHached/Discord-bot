import os
import datetime
import pytz

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
# Scheduled Event Config
# =====================

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
EVENT_HOUR = 20
EVENT_MINUTE = 30
EVENT_DURATION_MINUTES = 120

# Reminders posted to this text channel
REMINDER_CHANNEL_NAME = "recruit-status"
PING_ROLE_NAME = "🎮 Member"  # ping this role in reminders (create a dedicated role if you want)

# =====================
# Bot Setup
# =====================

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

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

    role = discord.utils.get(guild.roles, name=PING_ROLE_NAME)
    mention = role.mention if role else "@here"
    await channel.send(f"{mention} {text}")

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
# Event: Bot Ready
# =====================

@bot.event
async def on_ready():
    print(f"Bot is online as {bot.user}")

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

    # Reminder 5 minutes before (Mon–Fri)
    scheduler.add_job(
        lambda: bot.loop.create_task(
            send_reminder(f"⏰ **{EVENT_NAME}** starts in **5 minutes**! Join voice 🎤")
        ),
        CronTrigger(day_of_week="mon-fri", hour=EVENT_HOUR, minute=EVENT_MINUTE - 5),
        id="guild_party_reminder",
        replace_existing=True,
    )

    # Start message at event time (Mon–Fri)
    scheduler.add_job(
        lambda: bot.loop.create_task(
            send_reminder(f"✅ **{EVENT_NAME}** is starting now! Jump in voice 🎮")
        ),
        CronTrigger(day_of_week="mon-fri", hour=EVENT_HOUR, minute=EVENT_MINUTE),
        id="guild_party_start",
        replace_existing=True,
    )

    scheduler.start()

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