from __future__ import annotations
import discord
from discord import app_commands
from discord.ext import commands
from bot.config import settings
from bot.store import store
from bot.utils import embed, safe_dm
from bot.views.tickets import TicketCreateView
from bot.views.verification import VerificationLinkView


def manage_guild(interaction: discord.Interaction) -> bool:
    return bool(interaction.user.id == settings.owner_id or (isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild))


class AutoMessageGroup(app_commands.Group):
    def __init__(self, bot):
        super().__init__(name="automessage", description="Manage recurring automatic messages")
        self.bot = bot

    @app_commands.command(name="add", description="Add a recurring automatic message.")
    async def add(self, interaction: discord.Interaction, channel: discord.TextChannel, interval_minutes: app_commands.Range[int, 5, 10080], content: app_commands.Range[str, 1, 1800]):
        if not manage_guild(interaction):
            return await interaction.response.send_message("Manage Server permission required.", ephemeral=True)
        if not interaction.guild:
            return
        msg_id = store.add_automessage(interaction.guild.id, channel.id, content, int(interval_minutes))
        await interaction.response.send_message(f"Automatic message `#{msg_id}` will send in {channel.mention} every **{interval_minutes} minutes**.", ephemeral=True)

    @app_commands.command(name="list", description="List this server's recurring messages.")
    async def list_messages(self, interaction: discord.Interaction):
        if not manage_guild(interaction):
            return await interaction.response.send_message("Manage Server permission required.", ephemeral=True)
        rows = store.list_automessages(interaction.guild_id)
        if not rows:
            return await interaction.response.send_message("No automatic messages configured.", ephemeral=True)
        lines = [f"`#{r['id']}` <#{r['channel_id']}> · every {r['interval_minutes']}m · {'on' if r['enabled'] else 'off'} · {str(r['content'])[:90]}" for r in rows]
        await interaction.response.send_message(embed=embed("Automatic Messages", "\n".join(lines[:20])), ephemeral=True)

    @app_commands.command(name="remove", description="Remove a recurring automatic message.")
    async def remove(self, interaction: discord.Interaction, message_id: int):
        if not manage_guild(interaction):
            return await interaction.response.send_message("Manage Server permission required.", ephemeral=True)
        store.delete_automessage(interaction.guild_id, message_id)
        await interaction.response.send_message(f"Removed automatic message `#{message_id}`.", ephemeral=True)


class ServerTools(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.tree.add_command(AutoMessageGroup(bot))

    async def cog_unload(self):
        self.bot.tree.remove_command("automessage", type=discord.AppCommandType.chat_input)

    @app_commands.command(name="announce", description="Send a server announcement through the bot.")
    async def announce(self, interaction: discord.Interaction, message: app_commands.Range[str, 1, 1900], channel: discord.TextChannel | None = None):
        if not manage_guild(interaction):
            return await interaction.response.send_message("Manage Server permission required.", ephemeral=True)
        cfg = store.get_guild(interaction.guild_id)
        if not cfg["features"]["announcements"]:
            return await interaction.response.send_message("Announcements are disabled for this server.", ephemeral=True)
        target = channel
        if target is None and cfg["channels"]["announcements"]:
            target = interaction.guild.get_channel(int(cfg["channels"]["announcements"]))
        target = target or interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await interaction.response.send_message("Choose a text channel.", ephemeral=True)
        await target.send(embed=embed("Announcement", message))
        await interaction.response.send_message(f"Announcement sent to {target.mention}.", ephemeral=True)

    @app_commands.command(name="dm", description="Send a DM to a member through the bot.")
    async def dm(self, interaction: discord.Interaction, member: discord.Member, message: app_commands.Range[str, 1, 1900]):
        if not manage_guild(interaction):
            return await interaction.response.send_message("Manage Server permission required.", ephemeral=True)
        cfg = store.get_guild(interaction.guild_id)
        if not cfg["features"]["bot_dms"]:
            return await interaction.response.send_message("Bot DMs are disabled for this server.", ephemeral=True)
        ok = await safe_dm(member, message)
        await interaction.response.send_message("DM sent." if ok else "I could not DM that member (their DMs may be closed).", ephemeral=True)

    @app_commands.command(name="ticketpanel", description="Post the configured ticket panel.")
    async def ticketpanel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if not manage_guild(interaction):
            return await interaction.response.send_message("Manage Server permission required.", ephemeral=True)
        cfg = store.get_guild(interaction.guild_id)
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await interaction.response.send_message("Choose a text channel.", ephemeral=True)
        await target.send(embed=embed(cfg["tickets"]["panel_title"], cfg["tickets"]["panel_description"]), view=TicketCreateView(self.bot, cfg["tickets"]["button_label"]))
        await interaction.response.send_message(f"Ticket panel posted in {target.mention}.", ephemeral=True)

    @app_commands.command(name="verificationpanel", description="Post the configured Discord OAuth verification panel.")
    async def verificationpanel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if not manage_guild(interaction):
            return await interaction.response.send_message("Manage Server permission required.", ephemeral=True)
        cfg = store.get_guild(interaction.guild_id)
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await interaction.response.send_message("Choose a text channel.", ephemeral=True)
        url = f"{settings.dashboard_base_url}/verify/{interaction.guild_id}"
        await target.send(embed=embed(cfg["verification"]["panel_title"], cfg["verification"]["panel_description"]), view=VerificationLinkView(url, cfg["verification"]["button_label"]))
        await interaction.response.send_message(f"Verification panel posted in {target.mention}.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(ServerTools(bot))
