import os
import discord
from discord.ext import commands
from pathlib import Path

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN environment variable is missing. "
        "Set it in your hosting platform's Variables settings."
    )

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


async def load_cogs():
    cog_dir = Path(__file__).parent / "cog"

    for file in cog_dir.glob("*.py"):
        if file.name.startswith("_"):
            continue

        extension = f"cog.{file.stem}"

        try:
            await bot.load_extension(extension)
            print(f"Loaded cog: {extension}")
        except Exception as error:
            print(f"Failed to load {extension}: {error}")


async def main():
    async with bot:
        await load_cogs()
        await bot.start(TOKEN)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
