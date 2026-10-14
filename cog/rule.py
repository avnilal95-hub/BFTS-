"""
cog/rule.py — Welcome embed with the server rules supplied by the owner.
"""

import discord
from discord.ext import commands

WELCOME_CHANNEL_ID = 1498630797916246189
RULES_CHANNEL_ID = 1491880004227829939

RULES = [
    (
        "1. Treat everyone with respect.",
        "Treating someone with disrespect (harassment, sexism, racism, hate speech) will result in you getting punished. Even if you hate someone, that doesn't mean you should start insulting them.",
    ),
    (
        "2. No spamming or self-promotion.",
        "Promoting your stuff or spamming is not allowed. This includes server links, video/stream links, and any type of spam (soundboard spam, message spam, keyboard spam).",
    ),
    (
        "3. No NSFW/NSFL.",
        "Any type of NSFW or NSFL will result in a ban. This cannot be appealed.",
    ),
    (
        "4. No evading punishment.",
        "Attempting to evade punishment by using alternate accounts (or any other way) will lead to further action.",
    ),
    (
        "5. Use common sense.",
        "If you're about to do something, and you have to think if you should do it or not, then don't. It's most likely against the rules if you have to think about sending a message or doing something.",
    ),
    (
        "6. No suspicious links.",
        "Any links that seem suspicious (masked links included) will be deleted, and if it's confirmed to be a malicious link, you will be punished.",
    ),
    (
        "7. No loopholes.",
        "Any kind of loophole isn't allowed. Don't think that because you broke a rule in a way that wasn't stated here that you shouldn't get punished. Breaking a rule differently is still breaking a rule.",
    ),
    (
        "8. No cross-trading.",
        "Cross-trading is against the Roblox Terms of Service, which means that it definitely isn't allowed here. Any attempts to cross-trade will result in punishment.",
    ),
    (
        "9. No NSFW/Weird trading.",
        "This goes against server rule 3. You will be banned, and all appeals will be denied.",
    ),
    (
        "10. No scamming.",
        "Attempting to scam (or successfully scamming) will result with you being banned.",
    ),
    (
        "11. No mention of pedophiles.",
        "Mentioning pedophiles like Diddy or Epstein will result in punishment.",
    ),
    (
        "12. No violating TOS.",
        "Violating either Discord TOS or Roblox TOS is prohibited.",
    ),
]


class Rule(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def make_welcome_embed(self, member: discord.Member) -> discord.Embed:
        guild = member.guild
        embed = discord.Embed(
            title="📕 Server Rule Book",
            description=(
                f"Welcome {member.mention} to **{guild.name}**!\\n"
                "Please read the complete rule book below. By staying in the server, "
                "you agree to follow these rules."
            ),
            color=discord.Color.blurple(),
        )

        # Discord embeds have a 25-field limit and 6000-character total limit.
        # Twelve rules fit as individual fields in one embed.
        for title, explanation in RULES:
            embed.add_field(name=title, value=explanation, inline=False)

        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        embed.set_author(
            name=f"{guild.name} • Official Rules",
            icon_url=member.display_avatar.url,
        )
        embed.set_footer(
            text="Please follow all rules • Staff may take action for violations",
        )
        return embed

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
        if channel is None:
            channel = self.bot.get_channel(WELCOME_CHANNEL_ID)

        if member.bot:
            if channel is not None:
                try:
                    await channel.send(f"{member.mention} has joined the server.")
                except discord.HTTPException as exc:
                    print(f"[rule] Could not post bot-join notice: {exc}")
            return

        embed = self.make_welcome_embed(member)

        try:
            await member.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"[rule] Could not DM {member} ({member.id}): {exc}")

        if channel is None:
            print(
                f"[rule] Welcome channel {WELCOME_CHANNEL_ID} was not found "
                f"in guild {member.guild.id}."
            )
            return

        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            print(f"[rule] Could not post welcome embed: {exc}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Rule(bot))
