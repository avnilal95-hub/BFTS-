"""Prefixless staff role assignment in server channels and bot DMs."""
import logging, re
from typing import Optional
import discord
from discord.ext import commands

log = logging.getLogger(__name__)
STAFF_ROLE_ID = 1498697265634738236
STAFF_LOG_CHANNEL_ID = 1559956651044511856

def embed(title, description, color=discord.Color.blurple(), guild=None):
    e = discord.Embed(title=title, description=description, color=color)
    if guild and guild.icon:
        e.set_thumbnail(url=guild.icon.url)
    e.set_footer(text="Blox Fruits Server • Staff Management")
    return e

class Staff(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def reply(self, message, title, description, color=discord.Color.blurple(), guild=None):
        await message.channel.send(embed=embed(title, description, color, guild))

    @commands.Cog.listener()
    async def on_message(self, message):
        log.info("ST DEBUG | author=%s | guild=%s | content=%r",
                 message.author, message.guild.name if message.guild else "DM", message.content)
        if message.author.bot:
            return
        m = re.match(r"^staff(?:\s+|$)(.*)$", message.content.strip(), re.I)
        if not m:
            return
        arg = m.group(1).strip()
        if not arg:
            await self.reply(message, "⚠️ Missing User", "Use `staff @user` or `staff USER_ID`.",
                             discord.Color.orange(), message.guild)
            return
        found = re.search(r"<@!?(\d+)>", arg)
        if found:
            target_id = int(found.group(1))
        elif arg.isdigit():
            target_id = int(arg)
        else:
            await self.reply(message, "⚠️ Couldn't Identify User",
                             "Mention the target or provide their numeric Discord ID.",
                             discord.Color.orange(), message.guild)
            return

        candidates = []
        guilds = [message.guild] if message.guild else list(self.bot.guilds)
        for g in guilds:
            if g is None:
                continue
            requester, target, role = g.get_member(message.author.id), g.get_member(target_id), g.get_role(STAFF_ROLE_ID)
            if requester and target and role:
                candidates.append((g, requester, target, role))

        authorized = []
        for g, requester, target, role in candidates:
            permitted = requester.guild_permissions.manage_roles or g.owner_id == requester.id
            outranks = g.owner_id == requester.id or requester.top_role > role
            if permitted and outranks:
                authorized.append((g, requester, target, role))
        if not authorized:
            if candidates:
                desc = "You need **Manage Roles**, and your highest role must be above the Staff role."
                await self.reply(message, "❌ Missing Permission", desc, discord.Color.red(), candidates[0][0])
            else:
                desc = ("I couldn't find both members and the configured Staff role in this server. "
                        "Check STAFF_ROLE_ID in `cog/st.py`." if message.guild else
                        "No server contains both members and the configured Staff role.")
                await self.reply(message, "❌ Member or Role Not Found", desc, discord.Color.red(), message.guild)
            return

        g, requester, target, role = authorized[0]
        me = g.me
        if not me or not me.guild_permissions.manage_roles:
            await self.reply(message, "❌ Bot Permission Missing", f"I need **Manage Roles** in **{g.name}**.",
                             discord.Color.red(), g)
            return
        if me.top_role <= role:
            await self.reply(message, "❌ Role Hierarchy Error",
                             "Move the bot's highest role above the Staff role in Server Settings → Roles.",
                             discord.Color.red(), g)
            return
        if role in target.roles:
            await self.reply(message, "ℹ️ Already Staff", f"{target.mention} already has {role.mention}.",
                             discord.Color.orange(), g)
            return
        try:
            await target.add_roles(role, reason=f"Staff role granted by {requester} ({requester.id})")
        except discord.Forbidden:
            await self.reply(message, "❌ Assignment Blocked",
                             "Discord denied the role update. Check permissions and role hierarchy.",
                             discord.Color.red(), g)
            return
        except discord.HTTPException:
            log.exception("Discord API error assigning staff role")
            await self.reply(message, "❌ Discord API Error", "Please try again later.",
                             discord.Color.red(), g)
            return

        ok = embed("✅ Staff Role Assigned",
                   f"{target.mention} has been given {role.mention} in **{g.name}**.",
                   discord.Color.green(), g)
        ok.add_field(name="Member", value=f"{target.mention}\n`{target.id}`", inline=True)
        ok.add_field(name="Granted by", value=requester.mention, inline=True)
        await message.channel.send(embed=ok)

        dm = embed("🎉 You've Received the Staff Role!",
                   f"You've received the Staff role in **{g.name}**.", discord.Color.green(), g)
        dm.add_field(name="Role", value=role.mention, inline=True)
        dm.add_field(name="Granted by", value=str(requester), inline=True)
        try:
            await target.send(embed=dm)
            dm_status = "Delivered"
        except (discord.Forbidden, discord.HTTPException):
            dm_status = "Could not DM member; privacy settings may block DMs."

        channel = g.get_channel(STAFF_LOG_CHANNEL_ID)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            audit = embed("🛡️ New Staff Member", f"{target.mention} was added to the Staff team.",
                          discord.Color.blurple(), g)
            audit.add_field(name="Member", value=f"{target.mention}\n`{target.id}`", inline=True)
            audit.add_field(name="Granted by", value=f"{requester.mention}\n`{requester.id}`", inline=True)
            audit.add_field(name="Role", value=role.mention, inline=True)
            audit.add_field(name="Member DM", value=dm_status, inline=False)
            try:
                await channel.send(content=role.mention, embed=audit,
                    allowed_mentions=discord.AllowedMentions(roles=True, users=True))
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Could not send staff log")
        else:
            log.warning("Staff log channel %s not found in guild %s", STAFF_LOG_CHANNEL_ID, g.id)

async def setup(bot):
    await bot.add_cog(Staff(bot))
