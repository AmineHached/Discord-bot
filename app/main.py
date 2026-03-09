import sys
import os

# Add the repo root to the path so bot.py can be found
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import bot, TOKEN

if not TOKEN:
    raise RuntimeError("Set your BOT_TOKEN environment variable before running the bot.")
bot.run(TOKEN)
