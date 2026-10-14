import os
import asyncio
import logging
from pathlib import Path

import discord
from discord.ext import commands, tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN environment variable is missing. "
        "Set it in your hosting platform's Variables settings."
    )

BASE_DIR = Path(__file__).resolve().parent
COG_DIR = BASE_DIR / "cog"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Keep prefix commands (e.g. !ping) AND allow prefixless features
# (e.g. "staff @user") through on_message listeners inside cog/.
bot = commands.Bot(command_prefix="!", intents=intents)

_activity_index = 0


@tasks.loop(seconds=15)
async def rotate_activity():
    global _activity_index

    # Sum of member counts across all servers the bot is in.
    # People who share multiple servers may be counted more than once.
    total_users = sum(guild.member_count or 0 for guild in bot.guilds)
    activities = (
        discord.Activity(
            type=discord.ActivityType.listening,
            name=f"{total_users} users",
        ),
        discord.Activity(
            type=discord.ActivityType.watching,
            name="Blox Fruits Server",
        ),
    )
    await bot.change_presence(activity=activities[_activity_index])
    _activity_index = (_activity_index + 1) % len(activities)


@rotate_activity.before_loop
async def before_rotate_activity():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    logging.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    if not rotate_activity.is_running():
        rotate_activity.start()


async def load_cogs():
    if not COG_DIR.exists():
        logging.warning("Cog directory not found: %s", COG_DIR)
        return

    for file in sorted(COG_DIR.glob("*.py")):
        if file.name.startswith("_"):
            continue

        extension = f"cog.{file.stem}"
        try:
            await bot.load_extension(extension)
            logging.info("Loaded cog: %s", extension)
        except Exception:
            logging.exception("Failed to load cog: %s", extension)


async def main():
    async with bot:
        await load_cogs()
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
