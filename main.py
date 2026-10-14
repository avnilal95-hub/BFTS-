import os
import asyncio
import discord
from discord.ext import commands, tasks
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

activity_index = 0


@tasks.loop(seconds=15)
async def rotate_activity():
    global activity_index

    total_users = sum(
        guild.member_count or 0
        for guild in bot.guilds
    )

    activities = [
        discord.Activity(
            type=discord.ActivityType.listening,
            name=f"{total_users} users"
        ),
        discord.Activity(
            type=discord.ActivityType.watching,
            name="Blox Fruits Server"
        )
    ]

    await bot.change_presence(activity=activities[activity_index])
    activity_index = (activity_index + 1) % len(activities)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")

    if not rotate_activity.is_running():
        rotate_activity.start()


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
    asyncio.run(main())
