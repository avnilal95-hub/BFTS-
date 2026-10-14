"""Staff role management via a direct message (no command prefix).

Send the bot a DM like:
    staff @username
or paste the target user's numeric Discord ID after "staff".
The bot must be able to identify the target member in a server it shares with you.
"""
import re
import discord
from discord.ext import commands

STAFF_ROLE_ID = 1498697265634738236
STAFF_LOG_CHANNEL_ID = 1559956651044511856


class Staff(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.guild is not None:
            return  # This command is accepted only in the bot's DMs.
        if not message.content.strip().lower().startswith("staff"):
            return

        # Only server members with Manage Roles may use this DM command.
        target_text = message.content.strip()[len("staff"):].strip()
        if not target_text:
            await message.channel.send(embed=discord.Embed(
                title="⚠️ Missing User",
                description="Send `staff @user` or `staff USER_ID` in this DM.",
                color=discord.Color.orange()))
            return

        # Resolve the target from a mention, ID, or username text.
        match = re.search(r"<@!?(\d+)>", target_text)
        id_match = re.search(r"\b(\d{15,22})\b", target_text)
        target_id = int(match.group(1)) if match else (int(id_match.group(1)) if id_match else None)

        if target_id is None:
            await message.channel.send(embed=discord.Embed(
                title="⚠️ Could Not Identify User",
                description="Please mention the user or provide their numeric Discord ID. Plain usernames may not be unique.",
                color=discord.Color.orange()))
            return

        # Find a shared guild where the requester has Manage Roles and the target is present.
        candidates = []
        for guild in self.bot.guilds:
            requester = guild.get_member(message.author.id)
            target = guild.get_member(target_id)
            if requester and target:
                candidates.append((guild, requester, target))

        authorized = [
            item for item in candidates
            if item[1].guild_permissions.manage_roles
            and (item[0].owner_id == message.author.id or
                 item[0].get_role(STAFF_ROLE_ID) is not None)
        ]
        if not authorized:
            await message.channel.send(embed=discord.Embed(
                title="❌ Missing Permission",
                description="You're lacking permission with role management in a shared server, or the target user isn't in one.",
                color=discord.Color.red()))
            return

        guild, requester, target = authorized[0]
        role = guild.get_role(STAFF_ROLE_ID)
        if role is None:
            await message.channel.send(embed=discord.Embed(
                title="❌ Staff Role Not Found",
                description=f"I couldn't find role `{STAFF_ROLE_ID}` in **{guild.name}**.",
                color=discord.Color.red()))
            return

        bot_member = guild.me
        if not bot_member or not bot_member.guild_permissions.manage_roles:
            await message.channel.send(embed=discord.Embed(
                title="❌ Bot Permission Missing",
                description=f"I need Manage Roles in **{guild.name}**.",
                color=discord.Color.red()))
            return
        if role >= bot_member.top_role:
            await message.channel.send(embed=discord.Embed(
                title="❌ Role Hierarchy Error",
                description="Move the bot's highest role above the staff role, then try again.",
                color=discord.Color.red()))
            return
        if role >= requester.top_role and requester.id != guild.owner_id:
            await message.channel.send(embed=discord.Embed(
                title="❌ Role Hierarchy Error",
                description="You can only assign roles below your highest role.",
                color=discord.Color.red()))
            return
        if role in target.roles:
            await message.channel.send(embed=discord.Embed(
                title="ℹ️ Already Staff",
                description=f"{target.mention} already has {role.mention} in **{guild.name}**.",
                color=discord.Color.orange()))
            return

        try:
            await target.add_roles(role, reason=f"Staff role granted by {requester} ({requester.id}) via bot DM")
        except (discord.Forbidden, discord.HTTPException):
            await message.channel.send(embed=discord.Embed(
                title="❌ Assignment Failed",
                description="Discord couldn't assign the role. Check bot permissions and role hierarchy.",
                color=discord.Color.red()))
            return

        dm_embed = discord.Embed(
            title="🎉 You've received the Staff Role!",
            description=f"{target.mention}, you've received the staff role in **{guild.name}**.",
            color=discord.Color.green())
        dm_embed.add_field(name="Role", value=role.mention, inline=True)
        dm_embed.add_field(name="Granted by", value=requester.mention, inline=True)
        if guild.icon:
            dm_embed.set_thumbnail(url=guild.icon.url)
        try:
            await target.send(embed=dm_embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

        log_channel = guild.get_channel(STAFF_LOG_CHANNEL_ID) or self.bot.get_channel(STAFF_LOG_CHANNEL_ID)
        if isinstance(log_channel, discord.TextChannel):
            log_embed = discord.Embed(
                title="🛡️ New Staff Member",
                description=f"{role.mention} {target.mention} is added to staff team.",
                color=discord.Color.blurple())
            log_embed.add_field(name="Added by", value=requester.mention, inline=True)
            log_embed.add_field(name="Member", value=f"{target.mention} (`{target.id}`)", inline=True)
            try:
                await log_channel.send(
                    content=role.mention,
                    embed=log_embed,
                    allowed_mentions=discord.AllowedMentions(roles=True, users=True))
            except discord.HTTPException as exc:
                print(f"[staff] Could not send log: {exc}")

        await message.channel.send(embed=discord.Embed(
            title="✅ Staff Role Assigned",
            description=f"{target.mention} has been given {role.mention} in **{guild.name}**.",
            color=discord.Color.green()))


async def setup(bot: commands.Bot):
    await bot.add_cog(Staff(bot))
