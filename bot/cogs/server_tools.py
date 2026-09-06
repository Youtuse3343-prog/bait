from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.config import settings
from bot.store import store
from bot.utils import embed
from bot.views.tickets import TicketCreateView
from bot.views.verification import VerificationLinkView


def _manager(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild and isinstance(interaction.user, discord.Member) and (interaction.user.id == settings.owner_id or interaction.user.guild_permissions.manage_guild))


class ServerTools(commands.Cog):
    server = app_commands.Group(name="server", description="Server management tools")
    ticket = app_commands.Group(name="ticket", description="Ticket system tools")
    verification = app_commands.Group(name="verification", description="Verification tools")
    automessage = app_commands.Group(name="automessage", description="Recurring plain-text messages")

    def __init__(self, bot): self.bot = bot

    @server.command(name="announce", description="Send a simple announcement; use the dashboard Message Studio for rich messages")
    @app_commands.default_permissions(manage_guild=True)
    async def announce(self, interaction: discord.Interaction, channel: discord.TextChannel, message: str):
        if not _manager(interaction): return await interaction.response.send_message("Manage Server permission required.", ephemeral=True)
        cfg = store.get_guild(interaction.guild.id)
        if not cfg["features"].get("announcements"): return await interaction.response.send_message("Announcements are disabled for this server.", ephemeral=True)
        try: await channel.send(message[:2000], allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden: return await interaction.response.send_message("I cannot send messages in that channel.", ephemeral=True)
        store.audit(interaction.guild.id, interaction.user.id, "announcement.sent", channel_id=channel.id, source="slash")
        await interaction.response.send_message(f"Sent to {channel.mention}.", ephemeral=True)

    @server.command(name="stats-sync", description="Create/update configured server-stat channels")
    @app_commands.default_permissions(manage_guild=True)
    async def stats_sync(self, interaction: discord.Interaction):
        if not _manager(interaction): return await interaction.response.send_message("Manage Server permission required.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, detail = await self.bot.sync_server_stats(interaction.guild.id)
        await interaction.followup.send(detail, ephemeral=True)

    @server.command(name="stats-remove", description="Remove the channels managed by Server Stats")
    @app_commands.default_permissions(manage_guild=True)
    async def stats_remove(self, interaction: discord.Interaction):
        if not _manager(interaction): return await interaction.response.send_message("Manage Server permission required.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, detail = await self.bot.remove_server_stats(interaction.guild.id)
        await interaction.followup.send(detail, ephemeral=True)

    @server.command(name="raid-mode", description="Enable or disable the configured anti-raid lockdown")
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(state=[app_commands.Choice(name="enable", value="on"), app_commands.Choice(name="disable", value="off")])
    async def raid_mode(self, interaction: discord.Interaction, state: app_commands.Choice[str]):
        if not _manager(interaction): return await interaction.response.send_message("Manage Server permission required.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, detail = await self.bot.set_raid_mode(interaction.guild.id, state.value == "on", actor_id=interaction.user.id)
        await interaction.followup.send(detail, ephemeral=True)

    @ticket.command(name="panel", description="Post the configured ticket panel")
    @app_commands.default_permissions(manage_guild=True)
    async def ticket_panel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not _manager(interaction): return await interaction.response.send_message("Manage Server permission required.", ephemeral=True)
        cfg = store.get_guild(interaction.guild.id)
        if not cfg["features"].get("tickets"): return await interaction.response.send_message("Tickets are disabled.", ephemeral=True)
        try:
            await channel.send(embed=embed(cfg["tickets"]["panel_title"], cfg["tickets"]["panel_description"], guild_id=interaction.guild.id, kind="ticket"), view=TicketCreateView(self.bot, cfg["tickets"]["button_label"]))
            store.audit(interaction.guild.id, interaction.user.id, "ticket.panel_posted", channel_id=channel.id)
            await interaction.response.send_message(f"Ticket panel posted in {channel.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("I cannot send messages in that channel.", ephemeral=True)

    @ticket.command(name="close", description="Close the current tracked ticket")
    async def ticket_close(self, interaction: discord.Interaction):
        ticket = store.get_ticket(interaction.channel_id)
        if not ticket: return await interaction.response.send_message("This is not a tracked ticket.", ephemeral=True)
        # The persistent Close button performs transcript generation and permissions checks.
        await interaction.response.send_message("Use the **Close** button in the ticket control message so the transcript is generated safely.", ephemeral=True)

    @verification.command(name="panel", description="Post the configured Discord OAuth verification panel")
    @app_commands.default_permissions(manage_guild=True)
    async def verification_panel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not _manager(interaction): return await interaction.response.send_message("Manage Server permission required.", ephemeral=True)
        cfg = store.get_guild(interaction.guild.id)
        if not cfg["features"].get("verification"): return await interaction.response.send_message("Verification is disabled.", ephemeral=True)
        url = f"{settings.dashboard_base_url}/verify/{interaction.guild.id}"
        try:
            await channel.send(embed=embed(cfg["verification"]["panel_title"], cfg["verification"]["panel_description"], guild_id=interaction.guild.id), view=VerificationLinkView(url, cfg["verification"]["button_label"]))
            store.audit(interaction.guild.id, interaction.user.id, "verification.panel_posted", channel_id=channel.id)
            await interaction.response.send_message(f"Verification panel posted in {channel.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("I cannot send messages in that channel.", ephemeral=True)

    @automessage.command(name="add", description="Add a recurring plain-text message")
    @app_commands.default_permissions(manage_guild=True)
    async def automessage_add(self, interaction: discord.Interaction, channel: discord.TextChannel, interval_minutes: app_commands.Range[int, 5, 10080], content: str):
        if not _manager(interaction): return await interaction.response.send_message("Manage Server permission required.", ephemeral=True)
        cfg = store.get_guild(interaction.guild.id)
        if not cfg["features"].get("auto_messages"): return await interaction.response.send_message("Automatic messages are disabled.", ephemeral=True)
        mid = store.add_automessage(interaction.guild.id, channel.id, content[:1900], int(interval_minutes))
        store.audit(interaction.guild.id, interaction.user.id, "automessage.created", message_id=mid, channel_id=channel.id)
        await interaction.response.send_message(f"Created automatic message `#{mid}` every **{interval_minutes} min** in {channel.mention}.", ephemeral=True)

    @automessage.command(name="list", description="List recurring plain-text messages")
    @app_commands.default_permissions(manage_guild=True)
    async def automessage_list(self, interaction: discord.Interaction):
        if not _manager(interaction): return await interaction.response.send_message("Manage Server permission required.", ephemeral=True)
        rows = store.list_automessages(interaction.guild.id)
        body = "\n".join(f"`#{r['id']}` <#{r['channel_id']}> · every {r['interval_minutes']} min · {'on' if r['enabled'] else 'off'}" for r in rows[:25]) or "No automatic messages configured."
        await interaction.response.send_message(embed=embed("Automatic Messages", body, guild_id=interaction.guild.id), ephemeral=True)

    @automessage.command(name="remove", description="Remove a recurring plain-text message")
    @app_commands.default_permissions(manage_guild=True)
    async def automessage_remove(self, interaction: discord.Interaction, message_id: int):
        if not _manager(interaction): return await interaction.response.send_message("Manage Server permission required.", ephemeral=True)
        store.delete_automessage(interaction.guild.id, message_id)
        store.audit(interaction.guild.id, interaction.user.id, "automessage.deleted", message_id=message_id)
        await interaction.response.send_message(f"Removed automatic message `#{message_id}` if it existed.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(ServerTools(bot))
