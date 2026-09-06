from __future__ import annotations

import asyncio
import io
import random
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TypeVar

import discord

T = TypeVar("T")


def render(text: str, *, member: discord.Member | None = None, guild: discord.Guild | None = None) -> str:
    text = text or ""
    if member:
        text = text.replace("{mention}", member.mention).replace("{user}", member.display_name).replace("{user_id}", str(member.id))
    if guild:
        text = text.replace("{server}", guild.name).replace("{server_id}", str(guild.id)).replace("{member_count}", str(guild.member_count or 0))
    return text


def _hex_int(value: Any, fallback: int) -> int:
    try:
        if isinstance(value, int):
            return max(0, min(0xFFFFFF, value))
        raw = str(value or "").strip().lstrip("#")
        return int(raw, 16) if len(raw) == 6 else fallback
    except (ValueError, TypeError):
        return fallback


def embed(title: str, description: str = "", *, color: int = 0x7C3AED, guild_id: int | None = None, kind: str = "default") -> discord.Embed:
    footer_text = ""
    footer_icon = ""
    if guild_id:
        try:
            from bot.store import store
            appearance = store.get_guild(guild_id).get("appearance", {})
            key = {"moderation": "moderation_color", "ticket": "ticket_color", "welcome": "welcome_color"}.get(kind, "accent_color")
            color = _hex_int(appearance.get(key), color)
            footer_text = str(appearance.get("footer_text") or "")[:2048]
            footer_icon = str(appearance.get("footer_icon_url") or "")[:2048]
        except Exception:
            pass
    e = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    if footer_text:
        e.set_footer(text=footer_text, icon_url=footer_icon or None)
    return e


def rich_embed(data: dict[str, Any] | None, *, guild_id: int | None = None) -> discord.Embed | None:
    if not data:
        return None

    def clean(value: Any, limit: int) -> str:
        return str(value or "").strip()[:limit]

    title = clean(data.get("title"), 256)
    author_name = clean(data.get("author_name"), 256)
    footer_text = clean(data.get("footer_text"), 2048)
    description = clean(data.get("description"), 4096)
    base_without_description = len(title) + len(author_name) + len(footer_text)
    description = description[: max(0, min(4096, 6000 - base_without_description))]
    used_chars = base_without_description + len(description)

    fields: list[tuple[str, str, bool]] = []
    for field in (data.get("fields") or [])[:25]:
        if used_chars >= 6000:
            break
        name = clean(field.get("name"), min(256, 6000 - used_chars))
        if not name:
            continue
        used_chars += len(name)
        value = clean(field.get("value"), min(1024, max(0, 6000 - used_chars)))
        if not value:
            continue
        used_chars += len(value)
        fields.append((name, value, bool(field.get("inline"))))

    has_content = bool(title or description or fields or data.get("image_url") or data.get("thumbnail_url") or author_name or footer_text)
    if not has_content:
        return None

    fallback_color = 0x7C3AED
    if guild_id:
        try:
            from bot.store import store
            fallback_color = _hex_int(store.get_guild(guild_id).get("appearance", {}).get("accent_color"), fallback_color)
        except Exception:
            pass
    color = _hex_int(data.get("color"), fallback_color)
    timestamp = datetime.now(timezone.utc) if data.get("timestamp") else None
    e = discord.Embed(
        title=title or None,
        url=clean(data.get("title_url"), 2048) or None,
        description=description or None,
        color=color,
        timestamp=timestamp,
    )
    if author_name:
        e.set_author(name=author_name, url=clean(data.get("author_url"), 2048) or None, icon_url=clean(data.get("author_icon_url"), 2048) or None)
    thumbnail = clean(data.get("thumbnail_url"), 2048)
    image = clean(data.get("image_url"), 2048)
    if thumbnail: e.set_thumbnail(url=thumbnail)
    if image: e.set_image(url=image)
    if footer_text: e.set_footer(text=footer_text, icon_url=clean(data.get("footer_icon_url"), 2048) or None)
    for name, value, inline in fields: e.add_field(name=name, value=value, inline=inline)
    return e


def message_view(buttons: list[dict[str, Any]] | None, *, guild_id: int | None = None) -> discord.ui.View | None:
    items = [b for b in (buttons or [])[:5] if str(b.get("label") or "").strip()]
    if not items:
        return None
    view = discord.ui.View(timeout=None)
    from bot.views.message_components import RoleActionButton
    styles = {"primary": discord.ButtonStyle.primary, "secondary": discord.ButtonStyle.secondary, "success": discord.ButtonStyle.success, "danger": discord.ButtonStyle.danger}
    for item in items:
        kind = str(item.get("kind") or "link")
        label = str(item.get("label"))[:80]
        emoji = str(item.get("emoji") or "")[:50] or None
        if kind == "link" and str(item.get("url") or "").startswith(("https://", "http://")):
            view.add_item(discord.ui.Button(label=label, url=str(item.get("url"))[:512], emoji=emoji, style=discord.ButtonStyle.link))
        elif kind == "role" and guild_id and str(item.get("role_id") or "").isdigit():
            view.add_item(RoleActionButton(guild_id, int(item["role_id"]), str(item.get("mode") or "toggle"), label=label, emoji=emoji, style=styles.get(str(item.get("style") or "secondary"), discord.ButtonStyle.secondary)))
    return view if view.children else None


def is_staff(member: discord.Member, support_role_id: int | str | None = None) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    if support_role_id:
        try:
            return any(r.id == int(support_role_id) for r in member.roles)
        except (ValueError, TypeError):
            return False
    return False


def role_is_assignable(guild: discord.Guild, role: discord.Role | None) -> tuple[bool, str]:
    if role is None:
        return False, "The configured role no longer exists."
    me = guild.me
    if me is None:
        return False, "The bot member is not available in this server."
    if role.managed:
        return False, "Discord-managed roles cannot be assigned manually."
    if role >= me.top_role:
        return False, f"Move the bot role above **{role.name}** in Server Settings → Roles."
    if not me.guild_permissions.manage_roles:
        return False, "Grant the bot **Manage Roles**."
    return True, "OK"


async def retry_discord(call: Callable[[], Awaitable[T]], *, attempts: int = 4, base_delay: float = 0.7) -> T:
    """Retry transient Discord/network failures with bounded exponential backoff.

    discord.py already honors 429 rate limits internally. This adds resilience for transient
    5xx/gateway/network errors without retrying permission/validation failures.
    """
    last: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            return await call()
        except (discord.Forbidden, discord.NotFound):
            raise
        except discord.HTTPException as exc:
            last = exc
            if getattr(exc, "status", 0) and int(exc.status) < 500:
                raise
        except (OSError, asyncio.TimeoutError) as exc:
            last = exc
        if attempt < attempts - 1:
            await asyncio.sleep(min(8.0, base_delay * (2 ** attempt) + random.random() * 0.25))
    assert last is not None
    raise last


async def safe_dm(user: discord.abc.Messageable, content: str) -> bool:
    try:
        await retry_discord(lambda: user.send(content[:2000]))
        return True
    except (discord.Forbidden, discord.HTTPException, OSError):
        return False


async def transcript_channel(channel: discord.TextChannel, limit: int = 1500) -> discord.File:
    rows = []
    async for msg in channel.history(limit=max(1, min(5000, limit)), oldest_first=True):
        stamp = msg.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        body = msg.clean_content
        if msg.embeds:
            body += " " + " ".join(f"[embed: {e.title or e.description or 'content'}]" for e in msg.embeds)
        if msg.attachments:
            body += " " + " ".join(a.url for a in msg.attachments)
        rows.append(f"[{stamp}] {msg.author} ({msg.author.id}): {body}")
    data = "\n".join(rows).encode("utf-8", "replace")
    return discord.File(io.BytesIO(data), filename=f"ticket-{channel.id}.txt")
