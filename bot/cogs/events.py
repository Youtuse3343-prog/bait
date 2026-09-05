from __future__ import annotations
import discord
from discord.ext import commands
from bot.store import store
from bot.utils import embed, render, safe_dm


class Events(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _log(self, guild: discord.Guild, title: str, description: str, *, mod: bool = False):
        cfg = store.get_guild(guild.id)
        if not cfg["features"]["logs"]:
            return
        key = "mod_logs" if mod else "logs"
        cid = cfg["channels"].get(key) or cfg["channels"].get("logs")
        ch = guild.get_channel(int(cid)) if cid else None
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send(embed=embed(title, description))
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = store.get_guild(member.guild.id)
        if cfg["features"]["autorole"] and cfg["roles"]["autorole"]:
            role = member.guild.get_role(int(cfg["roles"]["autorole"]))
            if role:
                try:
                    await member.add_roles(role, reason="Configured auto-role on join")
                except (discord.Forbidden, discord.HTTPException):
                    pass
        if cfg["features"]["welcome"]:
            cid = cfg["channels"]["welcome"]
            ch = member.guild.get_channel(int(cid)) if cid else None
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.send(render(cfg["welcome"]["content"], member=member, guild=member.guild))
                except discord.HTTPException:
                    pass
            if cfg["welcome"].get("dm_enabled"):
                await safe_dm(member, render(cfg["welcome"].get("dm_content", ""), member=member, guild=member.guild))
        await self._log(member.guild, "Member Joined", f"{member.mention} (`{member.id}`) joined.\nMember count: **{member.guild.member_count}**")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self._log(member.guild, "Member Left", f"**{member}** (`{member.id}`) left the server.\nMember count: **{member.guild.member_count}**")

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        body = message.clean_content[:1200] or "*(no text content)*"
        await self._log(message.guild, "Message Deleted", f"**Author:** {message.author.mention} (`{message.author.id}`)\n**Channel:** {message.channel.mention}\n**Content:**\n{body}")

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not before.guild or before.author.bot or before.content == after.content:
            return
        await self._log(before.guild, "Message Edited", f"**Author:** {before.author.mention}\n**Channel:** {before.channel.mention}\n**Before:** {before.clean_content[:700] or '(empty)'}\n**After:** {after.clean_content[:700] or '(empty)'}")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.roles != after.roles:
            old = {r.id for r in before.roles}
            new = {r.id for r in after.roles}
            added = [r.mention for r in after.roles if r.id in new - old]
            removed = [r.mention for r in before.roles if r.id in old - new]
            text = f"**Member:** {after.mention}\n"
            if added: text += f"**Added roles:** {' '.join(added)}\n"
            if removed: text += f"**Removed roles:** {' '.join(removed)}"
            await self._log(after.guild, "Member Roles Updated", text, mod=True)
        elif before.nick != after.nick:
            await self._log(after.guild, "Nickname Updated", f"**Member:** {after.mention}\n**Before:** {before.nick or before.name}\n**After:** {after.nick or after.name}", mod=True)


async def setup(bot):
    await bot.add_cog(Events(bot))
