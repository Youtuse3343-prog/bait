from __future__ import annotations
import io
from datetime import datetime, timezone
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
