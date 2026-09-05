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
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Show the bot latency.")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=embed("Pong", f"Gateway latency: **{round(self.bot.latency * 1000)} ms**"))

    @app_commands.command(name="userinfo", description="Show information about a server member.")
    @app_commands.describe(member="Member to inspect")
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        if not isinstance(member, discord.Member):
            return await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        roles = [r.mention for r in member.roles[1:]][-12:]
        e = embed(f"User Info · {member}", f"**ID:** `{member.id}`\n**Display name:** {member.display_name}\n**Bot:** {'Yes' if member.bot else 'No'}\n**Created:** <t:{int(member.created_at.timestamp())}:F>\n**Joined:** {f'<t:{int(member.joined_at.timestamp())}:F>' if member.joined_at else 'Unknown'}\n**Roles:** {' '.join(roles) if roles else 'None'}")
        e.set_thumbnail(url=member.display_avatar.url)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="avatar", description="Show a member's avatar.")
    async def avatar(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        e = embed(f"Avatar · {member}")
        e.set_image(url=member.display_avatar.url)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="serverinfo", description="Show information about this server.")
    async def serverinfo(self, interaction: discord.Interaction):
        g = interaction.guild
        if not g:
            return await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        e = embed(g.name, f"**ID:** `{g.id}`\n**Owner:** {g.owner.mention if g.owner else 'Unknown'}\n**Members:** {g.member_count}\n**Channels:** {len(g.channels)}\n**Roles:** {len(g.roles)}\n**Created:** <t:{int(g.created_at.timestamp())}:F>")
        if g.icon: e.set_thumbnail(url=g.icon.url)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="botinfo", description="Show information about the bot.")
    async def botinfo(self, interaction: discord.Interaction):
        uptime = int(time.time() - self.bot.started_at)
        await interaction.response.send_message(embed=embed("Bot Info", f"**Servers:** {len(self.bot.guilds)}\n**Users cached:** {len(self.bot.users)}\n**Uptime:** {uptime // 3600}h {(uptime % 3600)//60}m\n**Python:** {platform.python_version()}\n**discord.py:** {discord.__version__}"))

    @app_commands.command(name="help", description="Show the bot's command categories.")
    async def help_cmd(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=embed("Commands", "**General:** `/ping`, `/userinfo`, `/avatar`, `/serverinfo`, `/botinfo`\n**Moderation:** `/warn`, `/warnings`, `/clearwarnings`, `/purge`, `/timeout`, `/untimeout`, `/kick`, `/ban`, `/unban`, `/slowmode`, `/lock`, `/unlock`\n**Server tools:** `/announce`, `/dm`, `/ticketpanel`, `/verificationpanel`, `/automessage add|list|remove`\n**Owner:** `/synccommands`\n\nMost configuration is intended to be managed from the secure dashboard."), ephemeral=True)

    @app_commands.command(name="synccommands", description="Owner only: replace Discord's registered commands with this bot's current command tree.")
    @app_commands.describe(scope="Use global for production or guild for instant testing")
    @app_commands.choices(scope=[app_commands.Choice(name="global", value="global"), app_commands.Choice(name="guild", value="guild"), app_commands.Choice(name="cleanup guild overrides", value="cleanup")])
    async def synccommands(self, interaction: discord.Interaction, scope: app_commands.Choice[str]):
        if interaction.user.id != settings.owner_id:
            return await interaction.response.send_message("Owner only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        if scope.value == "cleanup" and interaction.guild:
            self.bot.tree.clear_commands(guild=interaction.guild)
            synced = await self.bot.tree.sync(guild=interaction.guild)
            await interaction.followup.send("Cleared guild-specific command overrides. The server will now use the global command set only.", ephemeral=True)
            return
        if scope.value == "guild" and interaction.guild:
            self.bot.tree.copy_global_to(guild=interaction.guild)
            synced = await self.bot.tree.sync(guild=interaction.guild)
        else:
            synced = await self.bot.tree.sync()
        await interaction.followup.send(f"Synced **{len(synced)}** commands. Old registered commands not present in the current tree were removed for that scope.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Core(bot))
