from __future__ import annotations
import io
from datetime import datetime, timezone
from typing import Any
import discord


def render(text: str, *, member: discord.Member | None = None, guild: discord.Guild | None = None) -> str:
    text = text or ""
    if member:
        text = text.replace("{mention}", member.mention).replace("{user}", member.display_name).replace("{user_id}", str(member.id))
    if guild:
        text = text.replace("{server}", guild.name).replace("{server_id}", str(guild.id)).replace("{member_count}", str(guild.member_count or 0))
    return text


def embed(title: str, description: str = "", *, color: int = 0x7C3AED) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    return e


def rich_embed(data: dict[str, Any] | None) -> discord.Embed | None:
    """Build a Discord embed from a dashboard-safe payload."""
    if not data:
        return None

    def clean(value: Any, limit: int) -> str:
        return str(value or "").strip()[:limit]

    title = clean(data.get("title"), 256)
    author_name = clean(data.get("author_name"), 256)
    footer_text = clean(data.get("footer_text"), 2048)
    description = clean(data.get("description"), 4096)

    # Discord caps the combined textual content of one embed at 6000 chars.
    base_without_description = len(title) + len(author_name) + len(footer_text)
    description = description[: max(0, min(4096, 6000 - base_without_description))]
    used_chars = base_without_description + len(description)

    raw_fields = data.get("fields") or []
    fields: list[tuple[str, str, bool]] = []
    for field in raw_fields[:10]:
        if used_chars >= 6000:
            break
        name = clean(field.get("name"), min(256, 6000 - used_chars))
        if not name:
            continue
        used_chars += len(name)
        if used_chars >= 6000:
            break
        value = clean(field.get("value"), min(1024, 6000 - used_chars))
        if not value:
            continue
        used_chars += len(value)
        fields.append((name, value, bool(field.get("inline"))))

    has_content = bool(
        title
        or description
        or fields
        or data.get("image_url")
        or data.get("thumbnail_url")
        or author_name
        or footer_text
    )
    if not has_content:
        return None

    try:
        color = int(data.get("color", 0x7C3AED))
    except (TypeError, ValueError):
        color = 0x7C3AED
    color = max(0, min(0xFFFFFF, color))

    timestamp = datetime.now(timezone.utc) if data.get("timestamp") else None
    e = discord.Embed(
        title=title or None,
        url=clean(data.get("title_url"), 2048) or None,
        description=description or None,
        color=color,
        timestamp=timestamp,
    )

    if author_name:
        e.set_author(
            name=author_name,
            url=clean(data.get("author_url"), 2048) or None,
            icon_url=clean(data.get("author_icon_url"), 2048) or None,
        )

    thumbnail = clean(data.get("thumbnail_url"), 2048)
    image = clean(data.get("image_url"), 2048)
    if thumbnail:
        e.set_thumbnail(url=thumbnail)
    if image:
        e.set_image(url=image)

    if footer_text:
        e.set_footer(text=footer_text, icon_url=clean(data.get("footer_icon_url"), 2048) or None)

    for name, value, inline in fields:
        e.add_field(name=name, value=value, inline=inline)
    return e


def is_staff(member: discord.Member, support_role_id: int | str | None = None) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    if support_role_id:
        return any(r.id == int(support_role_id) for r in member.roles)
    return False


async def safe_dm(user: discord.abc.Messageable, content: str) -> bool:
    try:
        await user.send(content)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def transcript_channel(channel: discord.TextChannel, limit: int = 1000) -> discord.File:
    rows = []
    async for msg in channel.history(limit=limit, oldest_first=True):
        stamp = msg.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        body = msg.clean_content
        if msg.attachments:
            body += " " + " ".join(a.url for a in msg.attachments)
        rows.append(f"[{stamp}] {msg.author} ({msg.author.id}): {body}")
    data = "\n".join(rows).encode("utf-8", "replace")
    return discord.File(io.BytesIO(data), filename=f"ticket-{channel.id}.txt")
