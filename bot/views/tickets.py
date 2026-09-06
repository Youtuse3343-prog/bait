from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

import discord

from bot.store import store
from bot.utils import embed, is_staff, render, transcript_channel


def _slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")
    return value[:30] or "ticket"


def _ticket_type(cfg: dict, key: str) -> dict | None:
    return next((t for t in cfg.get("tickets", {}).get("types", []) if t.get("enabled", True) and str(t.get("key")) == str(key)), None)


async def create_ticket_from_type(interaction: discord.Interaction, bot, type_cfg: dict, answers: list[dict] | None = None):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return
    guild = interaction.guild
    cfg = store.get_guild(guild.id)
    if not cfg["features"].get("tickets"):
        return await interaction.followup.send("Tickets are disabled for this server.", ephemeral=True)

    max_open = max(1, min(10, int(cfg.get("tickets", {}).get("max_open_per_user", 1))))
    open_rows = store.list_tickets(guild.id, status="open", owner_id=interaction.user.id, limit=20)
    if len(open_rows) >= max_open:
        return await interaction.followup.send(f"You already have the maximum of **{max_open}** open ticket(s).", ephemeral=True)

    category_id = type_cfg.get("category_id") or cfg["channels"].get("ticket_category")
    category = guild.get_channel(int(category_id)) if str(category_id or "").isdigit() else None
    if not isinstance(category, discord.CategoryChannel):
        category = None
    support_role_id = type_cfg.get("support_role_id") or cfg["roles"].get("ticket_support")
    support_role = guild.get_role(int(support_role_id)) if str(support_role_id or "").isdigit() else None
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
    }
    if support_role:
        overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    try:
        channel = await guild.create_text_channel(
            f"{_slug(type_cfg.get('name','ticket'))}-{_slug(interaction.user.display_name)}"[:100],
            category=category,
            overwrites=overwrites,
            topic=f"Ticket owner {interaction.user.id} · {type_cfg.get('name','General Support')}",
            reason=f"Ticket opened by {interaction.user}",
        )
    except discord.Forbidden:
        return await interaction.followup.send("I cannot create the ticket channel. Check Manage Channels and category permissions.", ephemeral=True)
    except discord.HTTPException:
        return await interaction.followup.send("Discord could not create the ticket channel.", ephemeral=True)

    answers = answers or []
    store.create_ticket(
        guild.id,
        channel.id,
        interaction.user.id,
        type_key=type_cfg.get("key", "general"),
        type_name=type_cfg.get("name", "General Support"),
        answers=answers,
        priority=type_cfg.get("priority", "normal"),
    )
    store.update_ticket(channel.id, activity_at=datetime.now(timezone.utc).isoformat())
    opening = render(cfg["tickets"].get("opening_message", "Thanks for opening a ticket, {mention}."), member=interaction.user, guild=guild)
    body = opening
    if answers:
        body += "\n\n" + "\n".join(f"**{a['question']}**\n{a['answer']}" for a in answers if a.get("answer"))
    ping = support_role.mention if support_role else None
    try:
        await channel.send(content=ping, embed=embed(type_cfg.get("name", "Support Ticket"), body[:4096], guild_id=guild.id, kind="ticket"), view=TicketControlView(bot))
    except discord.HTTPException:
        pass
    store.audit(guild.id, interaction.user.id, "ticket.opened", channel_id=channel.id, type=type_cfg.get("key"))
    await interaction.followup.send(f"Your ticket is ready: {channel.mention}", ephemeral=True)


class TicketQuestionModal(discord.ui.Modal):
    def __init__(self, bot, guild_id: int, type_cfg: dict):
        super().__init__(title=str(type_cfg.get("name") or "Open Ticket")[:45], timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        self.type_key = str(type_cfg.get("key") or "general")
        self.question_labels: list[str] = []
        questions = list(type_cfg.get("questions") or [])[:5]
        for q in questions:
            label = str(q.get("label") or "Question")[:45]
            self.question_labels.append(label)
            style = discord.TextStyle.paragraph if q.get("style") == "paragraph" else discord.TextStyle.short
            self.add_item(discord.ui.TextInput(
                label=label,
                placeholder=str(q.get("placeholder") or "")[:100] or None,
                required=bool(q.get("required", True)),
                max_length=1000 if style is discord.TextStyle.paragraph else 300,
                style=style,
            ))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cfg = store.get_guild(self.guild_id)
        type_cfg = _ticket_type(cfg, self.type_key)
        if not type_cfg:
            return await interaction.followup.send("That ticket type is no longer available.", ephemeral=True)
        answers = [{"question": label, "answer": str(item.value)} for label, item in zip(self.question_labels, self.children)]
        await create_ticket_from_type(interaction, self.bot, type_cfg, answers)


class TicketTypeSelect(discord.ui.Select):
    def __init__(self, bot, guild_id: int, types: list[dict]):
        self.bot = bot
        self.guild_id = guild_id
        options = []
        for t in types[:25]:
            emoji = str(t.get("emoji") or "").strip() or None
            options.append(discord.SelectOption(label=str(t.get("name") or "Support")[:100], value=str(t.get("key") or "general")[:100], description=str(t.get("description") or "")[:100] or None, emoji=emoji))
        super().__init__(placeholder="Choose a ticket type…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        cfg = store.get_guild(self.guild_id)
        type_cfg = _ticket_type(cfg, self.values[0])
        if not type_cfg:
            return await interaction.response.send_message("That ticket type is no longer available.", ephemeral=True)
        questions = list(type_cfg.get("questions") or [])[:5]
        if questions:
            return await interaction.response.send_modal(TicketQuestionModal(self.bot, self.guild_id, type_cfg))
        await interaction.response.defer(ephemeral=True, thinking=True)
        await create_ticket_from_type(interaction, self.bot, type_cfg, [])


class TicketTypeView(discord.ui.View):
    def __init__(self, bot, guild_id: int, types: list[dict]):
        super().__init__(timeout=180)
        self.add_item(TicketTypeSelect(bot, guild_id, types))


class TicketCreateView(discord.ui.View):
    def __init__(self, bot, label: str = "Create Ticket"):
        super().__init__(timeout=None)
        self.bot = bot
        button = discord.ui.Button(label=label[:80] or "Create Ticket", style=discord.ButtonStyle.primary, custom_id="tickets:create")
        button.callback = self.create_ticket
        self.add_item(button)

    async def create_ticket(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        cfg = store.get_guild(interaction.guild.id)
        if not cfg["features"].get("tickets"):
            return await interaction.response.send_message("Tickets are disabled for this server.", ephemeral=True)
        types = [t for t in cfg.get("tickets", {}).get("types", []) if t.get("enabled", True)]
        if not types:
            types = [{"key":"general","name":"General Support","description":"General help","questions":[]}]
        if len(types) > 1:
            return await interaction.response.send_message("Choose the type of ticket you want to open.", view=TicketTypeView(self.bot, interaction.guild.id, types), ephemeral=True)
        type_cfg = types[0]
        if list(type_cfg.get("questions") or [])[:5]:
            return await interaction.response.send_modal(TicketQuestionModal(self.bot, interaction.guild.id, type_cfg))
        await interaction.response.defer(ephemeral=True, thinking=True)
        await create_ticket_from_type(interaction, self.bot, type_cfg, [])


class TicketControlView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.secondary, custom_id="tickets:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel) or not isinstance(interaction.user, discord.Member):
            return
        cfg = store.get_guild(interaction.guild.id)
        ticket = store.get_ticket(interaction.channel.id)
        if not ticket or ticket.get("status") != "open":
            return await interaction.response.send_message("This is not an open ticket.", ephemeral=True)
        type_cfg = _ticket_type(cfg, str(ticket.get("type_key") or "general")) or {}
        support_role = type_cfg.get("support_role_id") or cfg["roles"].get("ticket_support")
        if not is_staff(interaction.user, support_role):
            return await interaction.response.send_message("Only ticket staff can claim tickets.", ephemeral=True)
        now = datetime.now(timezone.utc).isoformat()
        fields = {"claimed_by": str(interaction.user.id)}
        if not ticket.get("first_response_at"):
            fields["first_response_at"] = now
        store.update_ticket(interaction.channel.id, **fields)
        store.audit(interaction.guild.id, interaction.user.id, "ticket.claimed", channel_id=interaction.channel.id)
        await interaction.response.send_message(f"Ticket claimed by {interaction.user.mention}.")

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="tickets:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel) or not isinstance(interaction.user, discord.Member):
            return
        cfg = store.get_guild(interaction.guild.id)
        ticket = store.get_ticket(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message("This channel is not a tracked ticket.", ephemeral=True)
        type_cfg = _ticket_type(cfg, str(ticket.get("type_key") or "general")) or {}
        support_role = type_cfg.get("support_role_id") or cfg["roles"].get("ticket_support")
        owner_ok = ticket.get("owner_id") == str(interaction.user.id)
        if not owner_ok and not is_staff(interaction.user, support_role):
            return await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)
        await interaction.response.send_message("Closing ticket and generating the transcript…")
        transcript = await transcript_channel(interaction.channel, int(cfg.get("tickets", {}).get("transcript_limit", 1500)))
        log_id = cfg["channels"].get("ticket_logs") or cfg["channels"].get("logs")
        log_ch = interaction.guild.get_channel(int(log_id)) if log_id else None
        if cfg["features"].get("logs") and isinstance(log_ch, discord.TextChannel):
            try:
                await log_ch.send(
                    embed=embed("Ticket Closed", f"Channel: `{interaction.channel.name}`\nType: **{ticket.get('type_name','Support')}**\nClosed by: {interaction.user.mention}\nOwner: <@{ticket.get('owner_id')}>", guild_id=interaction.guild.id, kind="ticket"),
                    file=transcript,
                )
            except discord.HTTPException:
                pass
        store.update_ticket(interaction.channel.id, status="closed", closed_at=datetime.now(timezone.utc).isoformat(), closed_by=str(interaction.user.id), transcript_name=f"ticket-{interaction.channel.id}.txt")
        store.audit(interaction.guild.id, interaction.user.id, "ticket.closed", channel_id=interaction.channel.id)
        await asyncio.sleep(2)
        try:
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            pass
