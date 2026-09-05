from __future__ import annotations
import asyncio
import discord
from bot.store import store
from bot.utils import embed, is_staff, render, transcript_channel


class TicketCreateView(discord.ui.View):
    def __init__(self, bot, label: str = "Create Ticket"):
        super().__init__(timeout=None)
        self.bot = bot
        self.create.label = (label or "Create Ticket")[:80]

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.primary, custom_id="tickets:create")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Tickets can only be opened inside a server.", ephemeral=True)
        cfg = store.get_guild(interaction.guild.id)
        if not cfg["features"]["tickets"]:
            return await interaction.response.send_message("Tickets are disabled on this server.", ephemeral=True)

        for ch in interaction.guild.text_channels:
            ticket = store.get_ticket(ch.id)
            if ticket and ticket.get("owner_id") == str(interaction.user.id) and ticket.get("status") == "open":
                return await interaction.response.send_message(f"You already have an open ticket: {ch.mention}", ephemeral=True)

        category = interaction.guild.get_channel(int(cfg["channels"]["ticket_category"])) if cfg["channels"]["ticket_category"] else None
        support = interaction.guild.get_role(int(cfg["roles"]["ticket_support"])) if cfg["roles"]["ticket_support"] else None
        bot_member = interaction.guild.me
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
        }
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True)
        if support:
            overwrites[support] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        safe_name = "".join(c for c in interaction.user.display_name.lower().replace(" ", "-") if c.isalnum() or c == "-")[:40] or str(interaction.user.id)
        channel = await interaction.guild.create_text_channel(f"ticket-{safe_name}", category=category if isinstance(category, discord.CategoryChannel) else None, overwrites=overwrites, reason=f"Ticket opened by {interaction.user}")
        store.create_ticket(interaction.guild.id, channel.id, interaction.user.id)
        opening = render(cfg["tickets"]["opening_message"], member=interaction.user, guild=interaction.guild)
        await channel.send(content=support.mention if support else None, embed=embed("Ticket Opened", opening), view=TicketControlView(self.bot))
        await interaction.followup.send(f"Created {channel.mention}", ephemeral=True)


class TicketControlView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.secondary, custom_id="tickets:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel) or not isinstance(interaction.user, discord.Member):
            return
        cfg = store.get_guild(interaction.guild.id)
        if not is_staff(interaction.user, cfg["roles"]["ticket_support"]):
            return await interaction.response.send_message("Only ticket staff can claim tickets.", ephemeral=True)
        ticket = store.get_ticket(interaction.channel.id)
        if not ticket or ticket.get("status") != "open":
            return await interaction.response.send_message("This is not an open ticket.", ephemeral=True)
        store.update_ticket(interaction.channel.id, claimed_by=str(interaction.user.id))
        await interaction.response.send_message(f"Ticket claimed by {interaction.user.mention}.")

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="tickets:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel) or not isinstance(interaction.user, discord.Member):
            return
        cfg = store.get_guild(interaction.guild.id)
        ticket = store.get_ticket(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message("This channel is not a tracked ticket.", ephemeral=True)
        owner_ok = ticket.get("owner_id") == str(interaction.user.id)
        if not owner_ok and not is_staff(interaction.user, cfg["roles"]["ticket_support"]):
            return await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)
        await interaction.response.send_message("Closing ticket and saving transcript…")
        transcript = await transcript_channel(interaction.channel)
        log_ch = interaction.guild.get_channel(int(cfg["channels"]["ticket_logs"])) if cfg["channels"]["ticket_logs"] else None
        if cfg["features"]["logs"] and isinstance(log_ch, discord.TextChannel):
            await log_ch.send(embed=embed("Ticket Closed", f"Channel: `{interaction.channel.name}`\nClosed by: {interaction.user.mention}\nOwner ID: `{ticket.get('owner_id')}`"), file=transcript)
        store.update_ticket(interaction.channel.id, status="closed")
        await asyncio.sleep(2)
        await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
