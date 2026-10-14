"""
main.py
-------
The bot's main entry point.

All command/features code should be placed in the `cog/` directory.
This file handles the bot token, presence/activity, and loading cogs.
"""

import asyncio
import os
from pathlib import Path

import discord
from discord.ext import commands

BASE_DIR = Path(__file__).resolve().parent
COG_DIR = BASE_DIR / "cog"

# Recommended: put the token in bot_token.txt beside this file.
# Keep bot_token.txt private and NEVER upload it to GitHub.
TOKEN_FILE = BASE_DIR / "bot_token.txt"

# Alternatively, set the DISCORD_BOT_TOKEN environment variable.
BOT_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

# Configure the bot's activity here.
ACTIVITY_TEXT = "Blox Fruits Stock"
ACTIVITY_TYPE = discord.ActivityType.watching
STATUS = discord.Status.online


class StockBot(commands.Bot):
    async def setup_hook(self) -> None:
        """Load every Python cog in the cog/ directory."""
        if not COG_DIR.exists():
            COG_DIR.mkdir(parents=True, exist_ok=True)
            print(f"Created cog directory: {COG_DIR}")

        for file in sorted(COG_DIR.glob("*.py")):
            if file.name.startswith("_"):
                continue

            extension = f"cog.{file.stem}"
            try:
                await self.load_extension(extension)
                print(f"Loaded cog: {extension}")
            except Exception as exc:
                print(f"Could not load {extension}: {exc!r}")


intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True  # Enable this intent in the Developer Portal if needed.

bot = StockBot(command_prefix="/", intents=intents)


@bot.event
async def on_ready() -> None:
    activity = discord.Activity(type=ACTIVITY_TYPE, name=ACTIVITY_TEXT)
    await bot.change_presence(status=STATUS, activity=activity)
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Cogs are managed from the cog/ directory.")


async def main() -> None:
    token = DISCORD_TOKEN.strip()
    if not token or token == "PASTE_YOUR_DISCORD_BOT_TOKEN_HERE":
        raise RuntimeError(
            "Add your Discord bot token to the BOT_TOKEN variable near the top of main.py."
        )

    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
