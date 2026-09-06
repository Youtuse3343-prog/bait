from __future__ import annotations

import time
from datetime import timedelta
import discord
from discord.ext import commands

from bot.config import settings
from bot.store import store
from bot.utils import embed, message_view, rich_embed, role_is_assignable, safe_dm, transcript_channel
from bot.views.tickets import TicketCreateView, TicketControlView
from bot.views.verification import VerificationLinkView
from bot.views.message_components import RoleActionButton


class ProfessionalBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.presences = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.started_at = time.time()
        self.ready_at: float | None = None

    async def setup_hook(self):
        for ext in [
            "bot.cogs.core",
            "bot.cogs.server_tools",
            "bot.cogs.moderation",
            "bot.cogs.events",
            "bot.cogs.automod",
            "bot.cogs.automation",
        ]:
            await self.load_extension(ext)
        self.add_view(TicketCreateView(self))
        self.add_view(TicketControlView(self))
        try:
            self.add_dynamic_items(RoleActionButton)
        except (AttributeError, TypeError):
            pass
        if settings.sync_commands_on_start:
            if settings.dev_guild_id:
                guild = discord.Object(id=settings.dev_guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                print(f"[commands] synced {len(synced)} commands to DEV_GUILD_ID={settings.dev_guild_id}")
            else:
                synced = await self.tree.sync()
                print(f"[commands] synced {len(synced)} global commands; stale commands in the global scope were replaced")

    async def on_ready(self):
        self.ready_at = time.time()
        print(f"[bot] logged in as {self.user} ({self.user.id if self.user else 'unknown'}) in {len(self.guilds)} guild(s)")

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):  # type: ignore[name-defined]
        try:
            text = "That command could not be completed."
            if isinstance(error, discord.app_commands.MissingPermissions):
                text = "You do not have the permissions required for that command."
            elif isinstance(error, discord.app_commands.CommandOnCooldown):
                text = f"Try again in {error.retry_after:.1f}s."
            elif isinstance(error, discord.app_commands.CheckFailure):
                text = "That command is not available here."
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    async def apply_verification(self, guild_id: int, user_id: int) -> tuple[bool, str]:
        guild = self.get_guild(guild_id)
        if not guild:
            return False, "The bot is not connected to this server."
        cfg = store.get_guild(guild_id)
        if not cfg["features"].get("verification"):
            return False, "Verification is disabled for this server."
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                return False, "Your Discord account is not currently a member of this server."
            except discord.HTTPException:
                return False, "Discord did not return your server membership."
        add_role = guild.get_role(int(cfg["roles"]["verification_add"])) if cfg["roles"].get("verification_add") else None
        remove_role = guild.get_role(int(cfg["roles"]["verification_remove"])) if cfg["roles"].get("verification_remove") else None
        if add_role:
            ok, why = role_is_assignable(guild, add_role)
            if not ok:
                return False, f"Verified role configuration problem: {why}"
        if remove_role:
            ok, why = role_is_assignable(guild, remove_role)
            if not ok:
                return False, f"Unverified role configuration problem: {why}"
        try:
            if remove_role and remove_role in member.roles:
                await member.remove_roles(remove_role, reason="Discord OAuth verification completed")
            if add_role and add_role not in member.roles:
                await member.add_roles(add_role, reason="Discord OAuth verification completed")
        except discord.Forbidden:
            return False, "I cannot update your roles. Move the bot role above the verification roles and grant Manage Roles."
        except discord.HTTPException:
            return False, "Discord rejected the role update."

        cid = cfg["channels"].get("logs")
        ch = guild.get_channel(int(cid)) if cid else None
        if isinstance(ch, discord.TextChannel) and cfg["features"].get("logs"):
            try:
                await ch.send(embed=embed("Member Verified", f"{member.mention} (`{member.id}`) completed Discord OAuth verification.", guild_id=guild.id))
            except discord.HTTPException:
                pass
        store.audit(guild.id, member.id, "verification.completed", user_id=member.id)
        return True, cfg["verification"].get("success_message") or "Verification complete."

    async def post_dashboard_ticket_panel(self, guild_id: int, channel_id: int) -> bool:
        guild = self.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild else None
        if not guild or not isinstance(channel, discord.TextChannel):
            return False
        cfg = store.get_guild(guild_id)
        if not cfg["features"].get("tickets"):
            return False
        await channel.send(embed=embed(cfg["tickets"]["panel_title"], cfg["tickets"]["panel_description"], guild_id=guild.id, kind="ticket"), view=TicketCreateView(self, cfg["tickets"]["button_label"]))
        return True

    async def post_dashboard_verification_panel(self, guild_id: int, channel_id: int) -> bool:
        guild = self.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild else None
        if not guild or not isinstance(channel, discord.TextChannel):
            return False
        cfg = store.get_guild(guild_id)
        if not cfg["features"].get("verification"):
            return False
        url = f"{settings.dashboard_base_url}/verify/{guild_id}"
        await channel.send(embed=embed(cfg["verification"]["panel_title"], cfg["verification"]["panel_description"], guild_id=guild.id), view=VerificationLinkView(url, cfg["verification"]["button_label"]))
        return True

    async def send_dashboard_message(
        self,
        channel_id: int,
        content: str = "",
        *,
        embed_data: dict | None = None,
        allow_mentions: bool = False,
        publish: bool = False,
        buttons: list[dict] | None = None,
    ) -> bool:
        channel = self.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return False
        content = (content or "").strip()[:2000]
        built_embed = rich_embed(embed_data, guild_id=channel.guild.id)
        if not content and built_embed is None:
            return False
        allowed_mentions = discord.AllowedMentions(everyone=True, users=True, roles=True, replied_user=False) if allow_mentions else discord.AllowedMentions.none()
        try:
            message = await channel.send(content=content or None, embed=built_embed, allowed_mentions=allowed_mentions, view=message_view(buttons, guild_id=channel.guild.id))
            if publish and channel.is_news():
                try: await message.publish()
                except discord.HTTPException: pass
            return True
        except discord.HTTPException:
            return False

    async def sync_server_stats(self, guild_id: int) -> tuple[bool, str]:
        cog = self.get_cog("Automation")
        if cog is None or not hasattr(cog, "sync_server_stats"):
            return False, "Server stats worker is unavailable."
        return await cog.sync_server_stats(guild_id, force=True)

    async def remove_server_stats(self, guild_id: int) -> tuple[bool, str]:
        cog = self.get_cog("Automation")
        if cog is None or not hasattr(cog, "remove_server_stats"):
            return False, "Server stats worker is unavailable."
        return await cog.remove_server_stats(guild_id)

    async def set_raid_mode(self, guild_id: int, enabled: bool, actor_id: int | None = None) -> tuple[bool, str]:
        cog = self.get_cog("AutoMod")
        if cog is None or not hasattr(cog, "set_raid_mode"):
            return False, "AutoMod worker is unavailable."
        return await cog.set_raid_mode(guild_id, enabled, actor_id=actor_id)

    async def send_dashboard_dm(self, user_id: int, content: str) -> bool:
        try:
            user = self.get_user(user_id) or await self.fetch_user(user_id)
            await user.send(content[:1900])
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def dashboard_moderate(self, guild_id: int, user_id: int, action: str, reason: str, duration_minutes: int = 10) -> tuple[bool, str]:
        """Apply a trusted dashboard moderation action with bot hierarchy safety."""
        guild = self.get_guild(int(guild_id))
        if not guild: return False, "The bot is not connected to that server."
        cfg = store.get_guild(guild.id)
        if not cfg["features"].get("moderation"): return False, "Moderation is disabled for this server."
        action = str(action or "").lower(); reason = (reason or "No reason provided")[:1000]
        if cfg.get("moderation", {}).get("reason_required") and reason == "No reason provided": return False, "A moderation reason is required by this server's configuration."
        member = guild.get_member(int(user_id))
        if member is None and action not in {"ban", "unban"}:
            try: member = await guild.fetch_member(int(user_id))
            except (discord.NotFound, discord.HTTPException): return False, "That member is not in the server."
        if member is not None:
            if member.id == guild.owner_id: return False, "The server owner cannot be moderated."
            if guild.me and member.top_role >= guild.me.top_role: return False, "Move the bot role above the target member's highest role."
        cog = self.get_cog("Moderation")
        try:
            if action == "warn":
                if member is None: return False, "That member is not in the server."
                if cog and hasattr(cog, "_warn"):
                    wid, cid, escalation = await cog._warn(guild, member, discord.Object(id=settings.owner_id), reason)
                else:
                    wid = store.add_warning(guild.id, member.id, settings.owner_id, reason); cid = store.create_case(guild.id, member.id, settings.owner_id, "warning", reason); escalation = None
                return True, f"Warning #{wid} · Case #{cid}" + (f" · {escalation}" if escalation else "")
            if action == "clear_warnings":
                count = store.clear_warnings(guild.id, int(user_id)); cid = store.create_case(guild.id, int(user_id), settings.owner_id, "warnings_cleared", f"Cleared {count} warning(s)", status="closed")
                return True, f"Cleared {count} warning(s) · Case #{cid}"
            if action == "timeout":
                if member is None: return False, "That member is not in the server."
                minutes = max(1, min(40320, int(duration_minutes))); await member.timeout(timedelta(minutes=minutes), reason=reason)
                cid = store.create_case(guild.id, member.id, settings.owner_id, "timeout", reason, duration_minutes=minutes)
                if cfg.get("moderation",{}).get("dm_on_action",True): await safe_dm(member, f"You were timed out in **{guild.name}** for **{minutes} minute(s)**.\nReason: {reason}")
                return True, f"Timed out for {minutes} minute(s) · Case #{cid}"
            if action == "untimeout":
                if member is None: return False, "That member is not in the server."
                await member.timeout(None, reason=reason); cid = store.create_case(guild.id, member.id, settings.owner_id, "untimeout", reason, status="closed"); return True, f"Timeout removed · Case #{cid}"
            if action == "kick":
                if member is None: return False, "That member is not in the server."
                if cfg.get("moderation",{}).get("dm_on_action",True): await safe_dm(member, f"You were kicked from **{guild.name}**.\nReason: {reason}")
                await member.kick(reason=reason); cid = store.create_case(guild.id, member.id, settings.owner_id, "kick", reason, status="closed"); return True, f"Member kicked · Case #{cid}"
            if action == "ban":
                target = member or discord.Object(id=int(user_id))
                if member is not None and cfg.get("moderation",{}).get("dm_on_action",True): await safe_dm(member, f"You were banned from **{guild.name}**.\nReason: {reason}")
                await guild.ban(target, reason=reason); cid = store.create_case(guild.id, int(user_id), settings.owner_id, "ban", reason); return True, f"User banned · Case #{cid}"
            if action == "unban":
                await guild.unban(discord.Object(id=int(user_id)), reason=reason); cid = store.create_case(guild.id, int(user_id), settings.owner_id, "unban", reason, status="closed"); return True, f"User unbanned · Case #{cid}"
            return False, "Unknown moderation action."
        except discord.Forbidden:
            return False, "Discord denied the action. Check the bot's permissions and role hierarchy."
        except discord.NotFound:
            return False, "That Discord user or moderation state was not found."
        except discord.HTTPException as exc:
            return False, f"Discord rejected the action ({getattr(exc, 'status', 'HTTP error')})."

    async def dashboard_channel_action(self, guild_id: int, channel_id: int, action: str, value: int = 0) -> tuple[bool, str]:
        guild = self.get_guild(int(guild_id)); channel = guild.get_channel(int(channel_id)) if guild else None
        if not guild or not isinstance(channel, discord.TextChannel): return False, "Choose a text channel the bot can access."
        try:
            if action == "purge":
                amount = max(1, min(500, int(value))); deleted = await channel.purge(limit=amount); store.create_case(guild.id, settings.owner_id, settings.owner_id, "purge", f"Purged {len(deleted)} messages in #{channel.name}", status="closed", channel_id=channel.id); return True, f"Deleted {len(deleted)} message(s)."
            if action in {"lock", "unlock"}:
                overwrite = channel.overwrites_for(guild.default_role); overwrite.send_messages = False if action == "lock" else None; await channel.set_permissions(guild.default_role, overwrite=overwrite, reason="Dashboard moderation control"); return True, f"Channel {action}ed."
            if action == "slowmode":
                seconds = max(0, min(21600, int(value))); await channel.edit(slowmode_delay=seconds, reason="Dashboard moderation control"); return True, f"Slowmode set to {seconds}s."
            return False, "Unknown channel action."
        except discord.Forbidden: return False, "Discord denied the channel action. Check Manage Messages/Manage Channels."
        except discord.HTTPException: return False, "Discord rejected the channel action."

    async def dashboard_close_ticket(self, guild_id: int, channel_id: int) -> tuple[bool, str]:
        guild = self.get_guild(int(guild_id)); channel = guild.get_channel(int(channel_id)) if guild else None; ticket = store.get_ticket(int(channel_id))
        if not guild or not ticket or str(ticket.get("guild_id")) != str(guild_id): return False, "Ticket not found."
        if not isinstance(channel, discord.TextChannel):
            store.update_ticket(int(channel_id), status="closed", closed_at=discord.utils.utcnow().isoformat(), closed_by=str(settings.owner_id)); return True, "Ticket record closed; channel was already missing."
        cfg=store.get_guild(guild.id); transcript=await transcript_channel(channel,int(cfg.get("tickets",{}).get("transcript_limit",1500))); log_id=cfg["channels"].get("ticket_logs") or cfg["channels"].get("logs"); log_ch=guild.get_channel(int(log_id)) if log_id else None
        if isinstance(log_ch,discord.TextChannel):
            try: await log_ch.send(embed=embed("Ticket Closed from Dashboard",f"`#{channel.name}` was closed from the management dashboard.",guild_id=guild.id,kind="ticket"),file=transcript)
            except discord.HTTPException: pass
        store.update_ticket(channel.id,status="closed",closed_at=discord.utils.utcnow().isoformat(),closed_by=str(settings.owner_id)); await channel.delete(reason="Ticket closed from dashboard"); return True,"Ticket closed and transcript processed."

    async def dashboard_sync_commands(self, guild_id: int | None = None, clear_guild_overrides: bool = False) -> tuple[bool, str]:
        try:
            if clear_guild_overrides and guild_id:
                target=discord.Object(id=int(guild_id)); self.tree.clear_commands(guild=target); synced=await self.tree.sync(guild=target); return True,f"Removed guild-specific command overrides ({len(synced)} remain in guild scope)."
            if guild_id:
                target=discord.Object(id=int(guild_id)); self.tree.copy_global_to(guild=target); synced=await self.tree.sync(guild=target); return True,f"Synced {len(synced)} commands to the server."
            synced=await self.tree.sync(); return True,f"Replaced global command registration with {len(synced)} current commands."
        except discord.HTTPException as exc:
            return False,f"Discord command sync failed ({getattr(exc,'status','HTTP error')})."


# Imported late to avoid shadowing discord.app_commands in type annotations above.
from discord import app_commands
