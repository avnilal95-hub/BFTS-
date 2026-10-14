"""
Advanced staff role management via direct message, without a command prefix.

Usage:
    DM the bot: staff @user
    DM the bot: staff USER_ID

Requires:
- Message Content Intent enabled in code and the Discord Developer Portal.
- The requester has Manage Roles in the target server.
- The bot has Manage Roles, and its top role is above the configured staff role.
"""

import logging
import re
from typing import Optional

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

# Configure these IDs for your server.
STAFF_ROLE_ID = 1498697265634738236
STAFF_LOG_CHANNEL_ID = 1559956651044511856


def make_embed(
    title: str,
    description: str,
    *,
    color: discord.Color = discord.Color.blurple(),
    guild: Optional[discord.Guild] = None,
) -> discord.Embed:
    """Consistent embed styling used for all replies and audit messages."""
    embed = discord.Embed(title=title, description=description, color=color)
    if guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text="Blox Fruits Server • Staff Management")
    return embed


class Staff(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def reply(
        self,
        message: discord.Message,
        title: str,
        description: str,
        *,
        color: discord.Color = discord.Color.blurple(),
        guild: Optional[discord.Guild] = None,
    ) -> None:
        await message.channel.send(
            embed=make_embed(title, description, color=color, guild=guild)
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is not None:
            return

        content = message.content.strip()
        # Match "staff" as a standalone first word; don't intercept "staffing".
        match = re.match(r"^staff(?:\s+|$)(.*)$", content, flags=re.IGNORECASE)
        if not match:
            return

        target_text = match.group(1).strip()
        if not target_text:
            await self.reply(
                message,
                "⚠️ Missing User",
                "Send `staff @user` or `staff USER_ID` in this DM.",
                color=discord.Color.orange(),
            )
            return

        # Supports regular mentions, nickname mentions, and raw numeric IDs.
        mention_match = re.search(r"<@!?(\d+)>", target_text)
        if mention_match:
            target_id = int(mention_match.group(1))
        elif target_text.isdigit():
            target_id = int(target_text)
        else:
            await self.reply(
                message,
                "⚠️ Couldn't Identify User",
                "Mention the target user or provide their numeric Discord ID.",
                color=discord.Color.orange(),
            )
            return

        # Resolve a server where both people are members and the configured role exists.
        candidates = []
        for guild in self.bot.guilds:
            requester = guild.get_member(message.author.id)
            target = guild.get_member(target_id)
            staff_role = guild.get_role(STAFF_ROLE_ID)
            if requester and target and staff_role:
                candidates.append((guild, requester, target, staff_role))

        # Prefer a server where the requester is permitted to manage this role.
        authorized = []
        for guild, requester, target, staff_role in candidates:
            requester_can_manage = requester.guild_permissions.manage_roles
            requester_outranks_role = (
                guild.owner_id == requester.id or requester.top_role > staff_role
            )
            if requester_can_manage and requester_outranks_role:
                authorized.append((guild, requester, target, staff_role))

        if not authorized:
            if candidates:
                await self.reply(
                    message,
                    "❌ Missing Permission",
                    "You're lacking permission for role management, or your highest "
                    "role must be above the Staff role.",
                    color=discord.Color.red(),
                )
            else:
                await self.reply(
                    message,
                    "🔎 Server or Role Not Found",
                    "I couldn't find a server where both you and the target are "
                    "members and the configured Staff role exists. Check the IDs "
                    "at the top of `cog/staff.py`.",
                    color=discord.Color.orange(),
                )
            return

        guild, requester, target, staff_role = authorized[0]
        bot_member = guild.me

        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            await self.reply(
                message,
                "❌ Bot Permission Missing",
                f"I need **Manage Roles** permission in **{guild.name}**.",
                color=discord.Color.red(),
                guild=guild,
            )
            return

        if bot_member.top_role <= staff_role:
            await self.reply(
                message,
                "❌ Role Hierarchy Error",
                "Move the bot's highest role above the Staff role in "
                "**Server Settings → Roles**.",
                color=discord.Color.red(),
                guild=guild,
            )
            return

        if staff_role in target.roles:
            await self.reply(
                message,
                "ℹ️ Already Staff",
                f"{target.mention} already has {staff_role.mention}.",
                color=discord.Color.orange(),
                guild=guild,
            )
            return

        try:
            await target.add_roles(
                staff_role,
                reason=f"Staff role granted by {requester} ({requester.id})",
            )
        except discord.Forbidden:
            await self.reply(
                message,
                "❌ Assignment Blocked",
                "Discord denied the role update. Check bot permissions and role order.",
                color=discord.Color.red(),
                guild=guild,
            )
            return
        except discord.HTTPException:
            log.exception("Discord API error assigning staff role in guild %s", guild.id)
            await self.reply(
                message,
                "❌ Discord API Error",
                "Discord couldn't complete the role update. Please try again later.",
                color=discord.Color.red(),
                guild=guild,
            )
            return

        # Confirmation to the requester.
        success = make_embed(
            "✅ Staff Role Assigned",
            f"{target.mention} has been given {staff_role.mention} in **{guild.name}**.",
            color=discord.Color.green(),
            guild=guild,
        )
        success.add_field(name="Member", value=f"{target.mention}\n`{target.id}`", inline=True)
        success.add_field(name="Granted by", value=f"{requester.mention}", inline=True)
        await message.channel.send(embed=success)

        # Direct message to the new staff member. Role assignment remains successful
        # even if the recipient has DMs disabled.
        dm_embed = make_embed(
            "🎉 You've Received the Staff Role!",
            f"You've been added to the Staff team in **{guild.name}**.",
            color=discord.Color.green(),
            guild=guild,
        )
        dm_embed.add_field(name="Role", value=staff_role.mention, inline=True)
        dm_embed.add_field(name="Granted by", value=str(requester), inline=True)
        try:
            await target.send(embed=dm_embed)
            dm_status = "Delivered"
        except (discord.Forbidden, discord.HTTPException):
            dm_status = "Could not DM the member (their privacy settings may block DMs)."

        # Audit log in the configured channel.
        log_channel = guild.get_channel(STAFF_LOG_CHANNEL_ID)
        if isinstance(log_channel, (discord.TextChannel, discord.Thread)):
            audit = make_embed(
                "🛡️ New Staff Member",
                f"{target.mention} has been added to the Staff team.",
                color=discord.Color.blurple(),
                guild=guild,
            )
            audit.add_field(name="Member", value=f"{target.mention}\n`{target.id}`", inline=True)
            audit.add_field(name="Granted by", value=f"{requester.mention}\n`{requester.id}`", inline=True)
            audit.add_field(name="Role", value=staff_role.mention, inline=True)
            audit.add_field(name="Member DM", value=dm_status, inline=False)
            try:
                await log_channel.send(
                    content=staff_role.mention,
                    embed=audit,
                    allowed_mentions=discord.AllowedMentions(roles=True, users=True),
                )
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Could not send staff audit log to channel %s", STAFF_LOG_CHANNEL_ID)
        else:
            log.warning(
                "Staff log channel %s was not found or is not a text channel in guild %s",
                STAFF_LOG_CHANNEL_ID,
                guild.id,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Staff(bot))
