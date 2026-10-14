import discord
from discord.ext import commands

BOOST_LOG_CHANNEL_ID = 1534633887421169875
BOOSTER_ROLE_ID = 1534634395259109376


class ServerBoosts(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        # A new boost is indicated by premium_since changing from None to a timestamp.
        if before.premium_since is not None or after.premium_since is None:
            return

        guild = after.guild
        channel = guild.get_channel(BOOST_LOG_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(BOOST_LOG_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                print(f"[boost] Could not access boost log channel: {exc}")
                return

        # Discord's premium_subscription_count is the server's current boost count.
        boost_count = guild.premium_subscription_count
        if boost_count is None:
            boost_count = sum(
                1 for member in guild.members if member.premium_since is not None
            )

        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            embed = discord.Embed(
                description=(
                    f"🚀 {after.mention} just boosted the server!\n"
                    f"We now have **{boost_count}** boost"
                    f"{'s' if boost_count != 1 else ''}!"
                ),
                color=discord.Color.pink(),
            )
            embed.set_thumbnail(url=after.display_avatar.url)
            embed.set_footer(text=f"{guild.name} • Server Boost")

            try:
                await channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException) as exc:
                print(f"[boost] Could not send boost announcement: {exc}")

        # Give the booster role if it exists and the bot can manage it.
        role = guild.get_role(BOOSTER_ROLE_ID)
        bot_member = guild.me
        if role is None:
            print(f"[boost] Booster role {BOOSTER_ROLE_ID} was not found.")
            return
        if (
            bot_member is None
            or not bot_member.guild_permissions.manage_roles
            or role >= bot_member.top_role
        ):
            print(
                "[boost] Cannot assign booster role. Enable Manage Roles and "
                "move the bot's highest role above the booster role."
            )
            return

        try:
            if role not in after.roles:
                await after.add_roles(role, reason="Member boosted the server")
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"[boost] Could not assign booster role: {exc}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerBoosts(bot))
