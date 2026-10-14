import discord
from discord.ext import commands

WELCOME_CHANNEL_ID = 1498634724032122970


class WelcomeMember(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(WELCOME_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                print(f"[welcome] Could not access welcome channel: {exc}")
                return

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            print(f"[welcome] Channel {WELCOME_CHANNEL_ID} is not a text channel/thread.")
            return

        # member_count includes the newly joined member.
        member_count = member.guild.member_count or len(member.guild.members)

        embed = discord.Embed(
            description=(
                f"👋 {member.mention}, thanks for joining **BFTS**!\n\n"
                f"🎉 We now have **{member_count:,}** members!"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        # Prefer the server's configured banner, then its icon, as the server image.
        guild = member.guild
        image_url = guild.banner.url if guild.banner else (
            guild.icon.url if guild.icon else None
        )
        if image_url:
            embed.set_image(url=image_url)

        embed.set_footer(text=f"Member #{member_count}")

        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"[welcome] Could not send welcome embed: {exc}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeMember(bot))
