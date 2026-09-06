from __future__ import annotations

from datetime import timedelta
import discord
from discord import app_commands
from discord.ext import commands

from bot.config import settings
from bot.store import store
from bot.utils import embed, safe_dm


def moderation_enabled(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild_id and store.get_guild(interaction.guild_id)["features"].get("moderation"))


def can_moderate(interaction: discord.Interaction) -> bool:
    return bool(
        moderation_enabled(interaction)
        and (
            interaction.user.id == settings.owner_id
            or (isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.moderate_members)
        )
    )


def target_check(actor: discord.Member, target: discord.Member, bot_member: discord.Member | None) -> tuple[bool, str]:
    if actor.id == target.id:
        return False, "You cannot use that action on yourself."
    if target.id == target.guild.owner_id:
        return False, "The server owner cannot be moderated by the bot."
    if actor.id != target.guild.owner_id and target.top_role >= actor.top_role:
        return False, "Your highest role must be above the target member's highest role."
    if bot_member and target.top_role >= bot_member.top_role:
        return False, "Move the bot role above the target member's highest role."
    return True, "OK"


async def mod_log(guild: discord.Guild, title: str, description: str):
    cfg = store.get_guild(guild.id)
    if not cfg["features"].get("logs"):
        return
    cid = cfg["channels"].get("mod_logs") or cfg["channels"].get("logs")
    ch = guild.get_channel(int(cid)) if cid else None
    if isinstance(ch, discord.TextChannel):
        try:
            await ch.send(embed=embed(title, description, guild_id=guild.id, kind="moderation"))
        except discord.HTTPException:
            pass


class Moderation(commands.Cog):
    mod = app_commands.Group(name="mod", description="Moderation cases and member actions")

    def __init__(self, bot):
        self.bot = bot
        self.ctx_warn = app_commands.ContextMenu(name="Warn User", callback=self.context_warn)
        self.ctx_history = app_commands.ContextMenu(name="View Moderation History", callback=self.context_history)
        self.ctx_timeout = app_commands.ContextMenu(name="Timeout User (10m)", callback=self.context_timeout)
        for item in (self.ctx_warn, self.ctx_history, self.ctx_timeout):
            try:
                self.bot.tree.add_command(item)
            except app_commands.CommandAlreadyRegistered:
                pass

    async def cog_unload(self):
        for name in ("Warn User", "View Moderation History", "Timeout User (10m)"):
            self.bot.tree.remove_command(name, type=discord.AppCommandType.user)

    async def _guard(self, interaction: discord.Interaction, member: discord.Member | None = None) -> bool:
        if not interaction.guild or not moderation_enabled(interaction):
            await interaction.response.send_message("Moderation is disabled for this server.", ephemeral=True)
            return False
        if not can_moderate(interaction):
            await interaction.response.send_message("Moderate Members permission required.", ephemeral=True)
            return False
        if member and isinstance(interaction.user, discord.Member):
            ok, why = target_check(interaction.user, member, interaction.guild.me)
            if not ok:
                await interaction.response.send_message(why, ephemeral=True)
                return False
        return True

    async def _dm_action(self, guild: discord.Guild, member: discord.Member, action: str, reason: str):
        if store.get_guild(guild.id)["moderation"].get("dm_on_action", True):
            await safe_dm(member, f"You received a moderation action in **{guild.name}**.\nAction: **{action}**\nReason: {reason}")

    async def _record_case(self, guild: discord.Guild, member_id: int, moderator_id: int, action: str, reason: str, **payload) -> int:
        cid = store.create_case(guild.id, member_id, moderator_id, action, reason, **payload)
        await mod_log(guild, f"Case #{cid} · {action.title()}", f"**User:** <@{member_id}> (`{member_id}`)\n**Moderator:** <@{moderator_id}>\n**Reason:** {reason}")
        return cid

    async def _warn(self, guild: discord.Guild, member: discord.Member, moderator: discord.abc.User, reason: str) -> tuple[int, int, str | None]:
        wid = store.add_warning(guild.id, member.id, moderator.id, reason)
        cid = await self._record_case(guild, member.id, moderator.id, "warning", reason, warning_id=wid)
        await self._dm_action(guild, member, f"Warning #{wid}", reason)
        count = len(store.list_warnings(guild.id, member.id))
        escalation = await self._warning_escalation(guild, member, moderator, count)
        return wid, cid, escalation

    async def _warning_escalation(self, guild: discord.Guild, member: discord.Member, moderator: discord.abc.User, count: int) -> str | None:
        rules = sorted(store.get_guild(guild.id)["moderation"].get("warning_actions") or [], key=lambda r: int(r.get("count", 0)))
        rule = next((r for r in reversed(rules) if int(r.get("count", 0)) == count), None)
        if not rule:
            return None
        action = str(rule.get("action") or "none")
        minutes = max(1, int(rule.get("duration_minutes") or 10))
        reason = f"Automatic warning threshold reached ({count} warnings)"
        try:
            if action == "timeout":
                await member.timeout(timedelta(minutes=minutes), reason=reason)
                await self._record_case(guild, member.id, moderator.id, "timeout", reason, duration_minutes=minutes, automated=True)
                return f"Automatic action: {minutes} minute timeout."
            if action == "kick":
                await member.kick(reason=reason)
                await self._record_case(guild, member.id, moderator.id, "kick", reason, automated=True)
                return "Automatic action: kicked."
            if action == "ban":
                await member.ban(reason=reason)
                await self._record_case(guild, member.id, moderator.id, "ban", reason, automated=True)
                return "Automatic action: banned."
        except discord.HTTPException:
            return "The configured automatic warning action could not be applied."
        return None

    @mod.command(name="warn", description="Warn a member and create a moderation case")
    @app_commands.default_permissions(moderate_members=True)
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not await self._guard(interaction, member): return
        wid, cid, escalation = await self._warn(interaction.guild, member, interaction.user, reason[:1000])
        msg = f"Warned {member.mention}. Warning `#{wid}` · Case `#{cid}`."
        if escalation: msg += f"\n{escalation}"
        await interaction.response.send_message(msg, ephemeral=True)

    @mod.command(name="warnings", description="Show saved warnings for a member")
    @app_commands.default_permissions(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        if not await self._guard(interaction): return
        rows = store.list_warnings(interaction.guild.id, member.id)
        if not rows:
            return await interaction.response.send_message(f"{member.mention} has no warnings.", ephemeral=True)
        body = "\n".join(f"`#{r['id']}` <@{r['moderator_id']}> · {str(r['reason'])[:180]}" for r in rows[-15:])
        await interaction.response.send_message(embed=embed(f"Warnings · {member}", body, guild_id=interaction.guild.id, kind="moderation"), ephemeral=True)

    @mod.command(name="clear-warnings", description="Clear saved warnings for a member")
    @app_commands.default_permissions(moderate_members=True)
    async def clear_warnings(self, interaction: discord.Interaction, member: discord.Member):
        if not await self._guard(interaction): return
        count = store.clear_warnings(interaction.guild.id, member.id)
        cid = await self._record_case(interaction.guild, member.id, interaction.user.id, "warnings_cleared", f"Cleared {count} warning(s)", status="closed")
        await interaction.response.send_message(f"Cleared **{count}** warning(s). Case `#{cid}`.", ephemeral=True)

    @mod.command(name="timeout", description="Timeout a member")
    @app_commands.default_permissions(moderate_members=True)
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason provided"):
        if not await self._guard(interaction, member): return
        try:
            await member.timeout(timedelta(minutes=minutes), reason=reason)
        except discord.Forbidden:
            return await interaction.response.send_message("I cannot timeout that member. Check role hierarchy and Moderate Members.", ephemeral=True)
        cid = await self._record_case(interaction.guild, member.id, interaction.user.id, "timeout", reason, duration_minutes=minutes)
        await self._dm_action(interaction.guild, member, f"Timeout ({minutes} minutes)", reason)
        await interaction.response.send_message(f"Timed out {member.mention} for **{minutes} minutes**. Case `#{cid}`.", ephemeral=True)

    @mod.command(name="untimeout", description="Remove a member timeout")
    @app_commands.default_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member, reason: str = "Timeout removed"):
        if not await self._guard(interaction, member): return
        try: await member.timeout(None, reason=reason)
        except discord.Forbidden: return await interaction.response.send_message("I cannot edit that member's timeout.", ephemeral=True)
        cid = await self._record_case(interaction.guild, member.id, interaction.user.id, "untimeout", reason, status="closed")
        await interaction.response.send_message(f"Removed timeout from {member.mention}. Case `#{cid}`.", ephemeral=True)

    @mod.command(name="kick", description="Kick a member")
    @app_commands.default_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not await self._guard(interaction, member): return
        await self._dm_action(interaction.guild, member, "Kick", reason)
        try: await member.kick(reason=reason)
        except discord.Forbidden: return await interaction.response.send_message("I cannot kick that member. Check role hierarchy and permissions.", ephemeral=True)
        cid = await self._record_case(interaction.guild, member.id, interaction.user.id, "kick", reason, status="closed")
        await interaction.response.send_message(f"Kicked **{member}**. Case `#{cid}`.", ephemeral=True)

    @mod.command(name="ban", description="Ban a member")
    @app_commands.default_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, member: discord.Member, delete_days: app_commands.Range[int,0,7] = 0, reason: str = "No reason provided"):
        if not await self._guard(interaction, member): return
        await self._dm_action(interaction.guild, member, "Ban", reason)
        try: await member.ban(reason=reason, delete_message_seconds=int(delete_days) * 86400)
        except discord.Forbidden: return await interaction.response.send_message("I cannot ban that member. Check role hierarchy and Ban Members.", ephemeral=True)
        cid = await self._record_case(interaction.guild, member.id, interaction.user.id, "ban", reason)
        await interaction.response.send_message(f"Banned **{member}**. Case `#{cid}`.", ephemeral=True)

    @mod.command(name="unban", description="Unban a user by Discord ID")
    @app_commands.default_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = "No reason provided"):
        if not await self._guard(interaction): return
        if not user_id.isdigit(): return await interaction.response.send_message("Provide a numeric Discord user ID.", ephemeral=True)
        try: await interaction.guild.unban(discord.Object(id=int(user_id)), reason=reason)
        except discord.NotFound: return await interaction.response.send_message("That user is not currently banned.", ephemeral=True)
        except discord.Forbidden: return await interaction.response.send_message("I do not have permission to unban users.", ephemeral=True)
        cid = await self._record_case(interaction.guild, int(user_id), interaction.user.id, "unban", reason, status="closed")
        await interaction.response.send_message(f"Unbanned `{user_id}`. Case `#{cid}`.", ephemeral=True)

    @mod.command(name="purge", description="Delete recent messages")
    @app_commands.default_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 500]):
        if not await self._guard(interaction): return
        if not isinstance(interaction.channel, discord.TextChannel): return await interaction.response.send_message("Use this in a text channel.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try: deleted = await interaction.channel.purge(limit=amount)
        except discord.Forbidden: return await interaction.followup.send("I need Manage Messages in this channel.", ephemeral=True)
        cid = await self._record_case(interaction.guild, interaction.user.id, interaction.user.id, "purge", f"Purged {len(deleted)} messages in #{interaction.channel.name}", status="closed")
        await interaction.followup.send(f"Deleted **{len(deleted)}** messages. Case `#{cid}`.", ephemeral=True)

    @mod.command(name="slowmode", description="Set slowmode for this text channel")
    @app_commands.default_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int,0,21600]):
        if not await self._guard(interaction): return
        if not isinstance(interaction.channel, discord.TextChannel): return await interaction.response.send_message("Use this in a text channel.", ephemeral=True)
        try: await interaction.channel.edit(slowmode_delay=seconds, reason=f"Changed by {interaction.user}")
        except discord.Forbidden: return await interaction.response.send_message("I need Manage Channels.", ephemeral=True)
        await interaction.response.send_message(f"Slowmode set to **{seconds}s**.", ephemeral=True)

    @mod.command(name="lock", description="Lock this channel for @everyone")
    @app_commands.default_permissions(manage_channels=True)
    async def lock(self, interaction: discord.Interaction):
        if not await self._guard(interaction): return
        if not isinstance(interaction.channel, discord.TextChannel): return await interaction.response.send_message("Use this in a text channel.", ephemeral=True)
        overwrite = interaction.channel.overwrites_for(interaction.guild.default_role); overwrite.send_messages = False
        try: await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Locked by {interaction.user}")
        except discord.Forbidden: return await interaction.response.send_message("I need Manage Channels.", ephemeral=True)
        await interaction.response.send_message("Channel locked.", ephemeral=True)

    @mod.command(name="unlock", description="Unlock this channel for @everyone")
    @app_commands.default_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction):
        if not await self._guard(interaction): return
        if not isinstance(interaction.channel, discord.TextChannel): return await interaction.response.send_message("Use this in a text channel.", ephemeral=True)
        overwrite = interaction.channel.overwrites_for(interaction.guild.default_role); overwrite.send_messages = None
        try: await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Unlocked by {interaction.user}")
        except discord.Forbidden: return await interaction.response.send_message("I need Manage Channels.", ephemeral=True)
        await interaction.response.send_message("Channel unlocked.", ephemeral=True)

    @mod.command(name="case", description="Show a moderation case")
    @app_commands.default_permissions(moderate_members=True)
    async def case(self, interaction: discord.Interaction, case_id: int):
        if not await self._guard(interaction): return
        row = store.get_case(interaction.guild.id, case_id)
        if not row: return await interaction.response.send_message("Case not found.", ephemeral=True)
        body = f"**Type:** {str(row.get('type','')).replace('_',' ').title()}\n**User:** <@{row.get('user_id')}> (`{row.get('user_id')}`)\n**Moderator:** <@{row.get('moderator_id')}>\n**Status:** {row.get('status','active')}\n**Reason:** {row.get('reason','No reason')}\n**Created:** {row.get('created_at','Unknown')}"
        await interaction.response.send_message(embed=embed(f"Case #{case_id}", body, guild_id=interaction.guild.id, kind="moderation"), ephemeral=True)

    @mod.command(name="cases", description="Show recent moderation cases for a member")
    @app_commands.default_permissions(moderate_members=True)
    async def cases(self, interaction: discord.Interaction, member: discord.Member):
        if not await self._guard(interaction): return
        rows = store.list_cases(interaction.guild.id, user_id=member.id, limit=15)
        if not rows: return await interaction.response.send_message("No cases found for that member.", ephemeral=True)
        body = "\n".join(f"`#{r['id']}` **{str(r['type']).replace('_',' ').title()}** · {str(r['reason'])[:140]}" for r in rows)
        await interaction.response.send_message(embed=embed(f"Cases · {member}", body, guild_id=interaction.guild.id, kind="moderation"), ephemeral=True)

    @mod.command(name="reason", description="Update a case reason")
    @app_commands.default_permissions(moderate_members=True)
    async def reason(self, interaction: discord.Interaction, case_id: int, reason: str):
        if not await self._guard(interaction): return
        if not store.update_case(interaction.guild.id, case_id, reason=reason[:1000]): return await interaction.response.send_message("Case not found or unchanged.", ephemeral=True)
        store.audit(interaction.guild.id, interaction.user.id, "case.reason_updated", case_id=case_id)
        await interaction.response.send_message(f"Updated case `#{case_id}`.", ephemeral=True)

    @mod.command(name="case-delete", description="Delete a moderation case")
    @app_commands.default_permissions(administrator=True)
    async def case_delete(self, interaction: discord.Interaction, case_id: int):
        if not await self._guard(interaction): return
        if not (interaction.user.id == settings.owner_id or interaction.user.guild_permissions.administrator): return await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        ok = store.delete_case(interaction.guild.id, case_id)
        if ok: store.audit(interaction.guild.id, interaction.user.id, "case.deleted", case_id=case_id)
        await interaction.response.send_message("Case deleted." if ok else "Case not found.", ephemeral=True)

    async def context_warn(self, interaction: discord.Interaction, member: discord.Member):
        if not await self._guard(interaction, member): return
        wid, cid, escalation = await self._warn(interaction.guild, member, interaction.user, "Warned from user context menu")
        text=f"Warned {member.mention}. Warning `#{wid}` · Case `#{cid}`." + (f"\n{escalation}" if escalation else "")
        await interaction.response.send_message(text, ephemeral=True)

    async def context_history(self, interaction: discord.Interaction, member: discord.Member):
        if not await self._guard(interaction): return
        rows=store.list_cases(interaction.guild.id,user_id=member.id,limit=10)
        body="\n".join(f"`#{r['id']}` **{str(r['type']).replace('_',' ').title()}** · {str(r['reason'])[:120]}" for r in rows) or "No moderation cases."
        await interaction.response.send_message(embed=embed(f"Moderation History · {member}",body,guild_id=interaction.guild.id,kind="moderation"),ephemeral=True)

    async def context_timeout(self, interaction: discord.Interaction, member: discord.Member):
        if not await self._guard(interaction, member): return
        try: await member.timeout(timedelta(minutes=10),reason=f"Context-menu timeout by {interaction.user}")
        except discord.Forbidden: return await interaction.response.send_message("I cannot timeout that member.",ephemeral=True)
        cid=await self._record_case(interaction.guild,member.id,interaction.user.id,"timeout","10 minute context-menu timeout",duration_minutes=10)
        await interaction.response.send_message(f"Timed out {member.mention} for 10 minutes. Case `#{cid}`.",ephemeral=True)


async def setup(bot):
    await bot.add_cog(Moderation(bot))
