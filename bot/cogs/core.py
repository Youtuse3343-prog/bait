from __future__ import annotations

import platform
import time
import discord
from discord import app_commands
from discord.ext import commands

from bot.config import settings
from bot.store import store
from bot.utils import embed


class Core(commands.Cog):
    bot_group = app_commands.Group(name="bot", description="Bot information and command maintenance")

    def __init__(self, bot):
        self.bot = bot
        self.userinfo_menu = app_commands.ContextMenu(name="View User Info", callback=self.context_userinfo)
        try: self.bot.tree.add_command(self.userinfo_menu)
        except app_commands.CommandAlreadyRegistered: pass

    async def cog_unload(self):
        self.bot.tree.remove_command("View User Info", type=discord.AppCommandType.user)

    def _user_embed(self, member: discord.Member) -> discord.Embed:
        roles = [r.mention for r in member.roles[1:]][-12:]
        e = embed(
            f"User Info · {member}",
            f"**ID:** `{member.id}`\n**Display name:** {member.display_name}\n**Bot:** {'Yes' if member.bot else 'No'}\n**Created:** <t:{int(member.created_at.timestamp())}:F>\n**Joined:** {f'<t:{int(member.joined_at.timestamp())}:F>' if member.joined_at else 'Unknown'}\n**Roles:** {' '.join(roles) if roles else 'None'}",
            guild_id=member.guild.id,
        )
        e.set_thumbnail(url=member.display_avatar.url)
        return e

    @app_commands.command(name="ping", description="Show gateway latency")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=embed("Pong", f"Gateway latency: **{round(self.bot.latency * 1000)} ms**", guild_id=interaction.guild_id))

    @app_commands.command(name="userinfo", description="Show information about a server member")
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        if not isinstance(member, discord.Member): return await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        await interaction.response.send_message(embed=self._user_embed(member))

    async def context_userinfo(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.send_message(embed=self._user_embed(member), ephemeral=True)

    @app_commands.command(name="avatar", description="Show a member's avatar")
    async def avatar(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        e = embed(f"Avatar · {member}", guild_id=interaction.guild_id); e.set_image(url=member.display_avatar.url)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="serverinfo", description="Show information about this server")
    async def serverinfo(self, interaction: discord.Interaction):
        g = interaction.guild
        if not g: return await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        e = embed(g.name, f"**ID:** `{g.id}`\n**Owner:** {g.owner.mention if g.owner else 'Unknown'}\n**Members:** {g.member_count}\n**Channels:** {len(g.channels)}\n**Roles:** {len(g.roles)}\n**Boosts:** {g.premium_subscription_count or 0}\n**Created:** <t:{int(g.created_at.timestamp())}:F>", guild_id=g.id)
        if g.icon: e.set_thumbnail(url=g.icon.url)
        await interaction.response.send_message(embed=e)

    @bot_group.command(name="info", description="Show bot health and runtime information")
    async def botinfo(self, interaction: discord.Interaction):
        uptime = int(time.time() - self.bot.started_at)
        await interaction.response.send_message(embed=embed("Bot Info", f"**Servers:** {len(self.bot.guilds)}\n**Users cached:** {len(self.bot.users)}\n**Uptime:** {uptime // 3600}h {(uptime % 3600)//60}m\n**Latency:** {round(self.bot.latency*1000)} ms\n**Python:** {platform.python_version()}\n**discord.py:** {discord.__version__}", guild_id=interaction.guild_id))

    @bot_group.command(name="sync", description="Replace registered slash commands with the current command tree")
    @app_commands.describe(scope="Global production commands, current-server testing, or remove current-server overrides")
    @app_commands.choices(scope=[app_commands.Choice(name="global", value="global"), app_commands.Choice(name="current server", value="guild"), app_commands.Choice(name="remove current-server overrides", value="cleanup")])
    async def synccommands(self, interaction: discord.Interaction, scope: app_commands.Choice[str]):
        if interaction.user.id != settings.owner_id: return await interaction.response.send_message("Not authorized.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        if scope.value == "cleanup" and interaction.guild:
            self.bot.tree.clear_commands(guild=interaction.guild); await self.bot.tree.sync(guild=interaction.guild)
            return await interaction.followup.send("Removed current-server command overrides; this server now uses the global command set.", ephemeral=True)
        if scope.value == "guild" and interaction.guild:
            self.bot.tree.copy_global_to(guild=interaction.guild); synced = await self.bot.tree.sync(guild=interaction.guild)
        else:
            synced = await self.bot.tree.sync()
        await interaction.followup.send(f"Synced **{len(synced)}** command entries. Stale commands in that scope were replaced.", ephemeral=True)

    @app_commands.command(name="help", description="Show command groups")
    async def help_cmd(self, interaction: discord.Interaction):
        body = "**Utilities:** `/ping`, `/userinfo`, `/avatar`, `/serverinfo`\n**Moderation:** `/mod ...`\n**Tickets:** `/ticket ...`\n**Server management:** `/server ...`\n**Automatic messages:** `/automessage ...`\n**Bot:** `/bot info`, `/bot sync`\n\nRight-click users → **Apps** for User Info, Warn, Timeout, and Moderation History. Advanced configuration lives in the dashboard."
        await interaction.response.send_message(embed=embed("Commands", body, guild_id=interaction.guild_id), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Core(bot))
