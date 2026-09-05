from __future__ import annotations
import time
import discord
from discord.ext import commands
from bot.config import settings
from bot.store import store
from bot.utils import embed
from bot.views.tickets import TicketCreateView, TicketControlView
from bot.views.verification import VerificationLinkView


class ProfessionalBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.started_at = time.time()
        self._synced_once = False

    async def setup_hook(self):
        for ext in [
            "bot.cogs.core",
            "bot.cogs.server_tools",
            "bot.cogs.moderation",
            "bot.cogs.events",
            "bot.cogs.automation",
        ]:
            await self.load_extension(ext)
        self.add_view(TicketCreateView(self))
        self.add_view(TicketControlView(self))
        if settings.sync_commands_on_start:
            if settings.dev_guild_id:
                guild = discord.Object(id=settings.dev_guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                print(f"[commands] synced {len(synced)} commands to DEV_GUILD_ID={settings.dev_guild_id}")
            else:
                synced = await self.tree.sync()
                print(f"[commands] synced {len(synced)} global commands; removed stale commands not in current tree")

    async def on_ready(self):
        print(f"[bot] logged in as {self.user} ({self.user.id if self.user else 'unknown'}) in {len(self.guilds)} guild(s)")

    async def apply_verification(self, guild_id: int, user_id: int) -> tuple[bool, str]:
        guild = self.get_guild(guild_id)
        if not guild:
            return False, "The bot is not connected to this server."
        cfg = store.get_guild(guild_id)
        if not cfg["features"]["verification"]:
            return False, "Verification is disabled for this server."
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                return False, "Your Discord account is not currently a member of this server."
            except discord.HTTPException:
                return False, "Discord did not return your server membership."
        add_role = guild.get_role(int(cfg["roles"]["verification_add"])) if cfg["roles"]["verification_add"] else None
        remove_role = guild.get_role(int(cfg["roles"]["verification_remove"])) if cfg["roles"]["verification_remove"] else None
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
        if isinstance(ch, discord.TextChannel) and cfg["features"]["logs"]:
            try:
                await ch.send(embed=embed("Member Verified", f"{member.mention} (`{member.id}`) completed Discord OAuth verification."))
            except discord.HTTPException:
                pass
        return True, cfg["verification"].get("success_message") or "Verification complete."

    async def post_dashboard_ticket_panel(self, guild_id: int, channel_id: int) -> bool:
        guild = self.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild else None
        if not guild or not isinstance(channel, discord.TextChannel):
            return False
        cfg = store.get_guild(guild_id)
        if not cfg["features"]["tickets"]:
            return False
        await channel.send(embed=embed(cfg["tickets"]["panel_title"], cfg["tickets"]["panel_description"]), view=TicketCreateView(self, cfg["tickets"]["button_label"]))
        return True

    async def post_dashboard_verification_panel(self, guild_id: int, channel_id: int) -> bool:
        guild = self.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild else None
        if not guild or not isinstance(channel, discord.TextChannel):
            return False
        cfg = store.get_guild(guild_id)
        if not cfg["features"]["verification"]:
            return False
        url = f"{settings.dashboard_base_url}/verify/{guild_id}"
        await channel.send(embed=embed(cfg["verification"]["panel_title"], cfg["verification"]["panel_description"]), view=VerificationLinkView(url, cfg["verification"]["button_label"]))
        return True

    async def send_dashboard_message(self, channel_id: int, content: str, announcement: bool = False) -> bool:
        channel = self.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return False
        if announcement:
            await channel.send(embed=embed("Announcement", content[:1900]))
        else:
            await channel.send(content[:1900])
        return True

    async def send_dashboard_dm(self, user_id: int, content: str) -> bool:
        try:
            user = self.get_user(user_id) or await self.fetch_user(user_id)
            await user.send(content[:1900])
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False
