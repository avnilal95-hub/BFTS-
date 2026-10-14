import asyncio
import discord
from discord.ext import commands

PANEL_CHANNEL_ID = 1498633989609492624
NORMAL_STOCK_ROLE_ID = 1498638676647874620
MIRAGE_STOCK_ROLE_ID = 1498638765630033950

PANEL_TITLE = "Stock Notifications"
PANEL_DESCRIPTION = (
    "Click any button to get notified!\n\n"
    "🔔 **Normal stock** — <@&1498638676647874620>\n"
    "🌫️ **Mirage stock** — <@&1498638765630033950>"
)


class StockNotificationView(discord.ui.View):
    def __init__(self):
        # Persistent view: button custom IDs are stable and timeout is disabled.
        super().__init__(timeout=None)

    async def toggle_role(
        self,
        interaction: discord.Interaction,
        role_id: int,
        role_label: str,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Use these buttons inside the server.", ephemeral=True
            )
            return

        guild = interaction.guild
        member = interaction.user
        if not isinstance(member, discord.Member):
            member = guild.get_member(interaction.user.id)

        role = guild.get_role(role_id)
        if member is None or role is None:
            await interaction.response.send_message(
                "I couldn't find your member profile or the configured role. "
                "Please tell a server admin.",
                ephemeral=True,
            )
            return

        bot_member = guild.me
        if (
            bot_member is None
            or not bot_member.guild_permissions.manage_roles
            or role >= bot_member.top_role
        ):
            await interaction.response.send_message(
                "I can't manage that role. An admin must enable **Manage Roles** "
                "for the bot and move its highest role above the notification roles.",
                ephemeral=True,
            )
            return

        try:
            if role in member.roles:
                await member.remove_roles(
                    role, reason=f"{role_label} notification opt-out"
                )
                await interaction.response.send_message(
                    "Your role is gone.", ephemeral=True
                )
            else:
                await member.add_roles(
                    role, reason=f"{role_label} notification opt-in"
                )
                await interaction.response.send_message(
                    f"You'll now get **{role_label}** notifications. "
                    "Click the same button again to remove the role.",
                    ephemeral=True,
                )
        except discord.Forbidden:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Discord denied the role update. Please contact an admin.",
                    ephemeral=True,
                )
        except discord.HTTPException:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Discord couldn't update your role. Please try again.",
                    ephemeral=True,
                )

    @discord.ui.button(
        label="Normal stock",
        style=discord.ButtonStyle.primary,
        emoji="🔔",
        custom_id="stock_notify:normal",
    )
    async def normal_stock(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.toggle_role(interaction, NORMAL_STOCK_ROLE_ID, "Normal stock")

    @discord.ui.button(
        label="Mirage stock",
        style=discord.ButtonStyle.secondary,
        emoji="🌫️",
        custom_id="stock_notify:mirage",
    )
    async def mirage_stock(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.toggle_role(interaction, MIRAGE_STOCK_ROLE_ID, "Mirage stock")


class StockNotifications(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.panel_view = StockNotificationView()
        self._panel_task = None

    async def cog_load(self) -> None:
        # Register persistent buttons after every startup.
        self.bot.add_view(self.panel_view)
        self._panel_task = asyncio.create_task(self._ensure_panel_posted())

    async def cog_unload(self) -> None:
        if self._panel_task is not None:
            self._panel_task.cancel()

    async def _ensure_panel_posted(self) -> None:
        await self.bot.wait_until_ready()

        channel = self.bot.get_channel(PANEL_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(PANEL_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                print(f"[notify] Cannot access channel {PANEL_CHANNEL_ID}: {exc}")
                return

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            print(f"[notify] Channel {PANEL_CHANNEL_ID} is not a text channel/thread.")
            return

        # Avoid reposting the same panel on every restart.
        try:
            async for message in channel.history(limit=50):
                if (
                    self.bot.user is not None
                    and message.author.id == self.bot.user.id
                    and message.embeds
                    and message.embeds[0].title == PANEL_TITLE
                ):
                    print(f"[notify] Existing panel found in channel {PANEL_CHANNEL_ID}.")
                    return
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"[notify] Could not check channel history: {exc}")

        embed = discord.Embed(
            title=PANEL_TITLE,
            description=PANEL_DESCRIPTION,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Blox Fruits • Notification Roles")

        try:
            await channel.send(embed=embed, view=self.panel_view)
            print(f"[notify] Notification panel posted in channel {PANEL_CHANNEL_ID}.")
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"[notify] Failed to post panel: {exc}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StockNotifications(bot))
