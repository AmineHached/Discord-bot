import os
try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

import discord
from discord.ext import commands

# Load .env when available (development only)
if load_dotenv:
    load_dotenv()

# Read token from environment. Railway and other hosts set env vars for you.
TOKEN = os.getenv("BOT_TOKEN") or os.getenv("DISCORD_TOKEN")
GUILD_NAME = "Kaikei"


# Names of roles (must match exactly)
RECRUIT_ROLE_NAME = "🌱 Recruit"
MEMBER_ROLE_NAME = "🎮 Member"

# Name of the channel where approvals work
APPROVAL_CHANNEL_NAME = "apply-here"

# =====================
# Bot Setup
# =====================

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =====================
# Event: Bot Ready
# =====================

@bot.event
async def on_ready():
    print(f"Bot is online as {bot.user}")

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

    # 📌 Next Steps

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
