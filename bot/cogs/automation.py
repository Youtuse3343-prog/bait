from __future__ import annotations
from datetime import datetime, timezone
import time

import discord
from discord.ext import commands, tasks

from bot.store import store


STAT_TYPES = {"members", "humans", "bots", "boosts", "role"}


class Automation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._status_index = 0
        self._stats_last: dict[int, float] = {}
        self.auto_messages.start()
        self.status_rotation.start()
        self.server_stats_loop.start()

    def cog_unload(self):
        self.auto_messages.cancel()
        self.status_rotation.cancel()
        self.server_stats_loop.cancel()

    @tasks.loop(minutes=1)
    async def auto_messages(self):
        now = datetime.now(timezone.utc)
        for row in store.all_automessages():
            if not bool(row.get("enabled")):
                continue
            guild = self.bot.get_guild(int(row["guild_id"]))
            if not guild:
                continue
            cfg = store.get_guild(guild.id)
            if not cfg["features"]["auto_messages"]:
                continue
            last = row.get("last_sent")
            if last:
                try:
                    last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                    if (now - last_dt).total_seconds() < int(row["interval_minutes"]) * 60:
                        continue
                except ValueError:
                    pass
            channel = guild.get_channel(int(row["channel_id"]))
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(str(row["content"]))
                    store.mark_automessage_sent(int(row["id"]))
                except discord.HTTPException:
                    pass

    @auto_messages.before_loop
    async def before_auto(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=15)
    async def status_rotation(self):
        cfg = store.get_global()
        interval = max(15, int(cfg.get("status_interval_seconds", 45)))
        last = getattr(self, "_last_status_change", None)
        now = datetime.now(timezone.utc).timestamp()
        if last and now - last < interval:
            return
        statuses = cfg.get("statuses") or []
        if not statuses:
            return
        item = statuses[self._status_index % len(statuses)]
        self._status_index += 1
        kind = str(item.get("type", "watching")).lower()
        text = str(item.get("text", "the server"))[:128]
        activity_cls = {
            "playing": discord.Game,
            "streaming": None,
            "watching": lambda name: discord.Activity(type=discord.ActivityType.watching, name=name),
            "listening": lambda name: discord.Activity(type=discord.ActivityType.listening, name=name),
            "competing": lambda name: discord.Activity(type=discord.ActivityType.competing, name=name),
        }.get(kind)
        if kind == "streaming":
            activity = discord.Streaming(name=text, url="https://twitch.tv/discord")
        elif activity_cls:
            activity = activity_cls(text)
        else:
            activity = discord.Activity(type=discord.ActivityType.watching, name=text)
        await self.bot.change_presence(activity=activity, status=discord.Status.online)
        self._last_status_change = now

    @status_rotation.before_loop
    async def before_status(self):
        await self.bot.wait_until_ready()

    def _stat_value(self, guild: discord.Guild, item: dict) -> int:
        kind = str(item.get("type", "members"))
        if kind == "members":
            return int(guild.member_count or len(guild.members))
        if kind == "humans":
            return sum(1 for member in guild.members if not member.bot)
        if kind == "bots":
            return sum(1 for member in guild.members if member.bot)
        if kind == "boosts":
            return int(guild.premium_subscription_count or 0)
        if kind == "role":
            raw = item.get("role_id")
            role = guild.get_role(int(raw)) if str(raw or "").isdigit() else None
            return len(role.members) if role else 0
        return 0

    def _stat_channel_name(self, guild: discord.Guild, item: dict) -> str:
        value = self._stat_value(guild, item)
        emoji = str(item.get("emoji") or "").strip()[:24]
        label = str(item.get("label") or "Stat").strip()[:50]
        template = str(item.get("template") or "{emoji} {label}: {value}").strip()
        try:
            name = template.format(emoji=emoji, label=label, value=value)
        except (KeyError, ValueError):
            name = f"{emoji} {label}: {value}"
        name = " ".join(name.split())
        return (name or f"{label}: {value}")[:100]

    async def sync_server_stats(self, guild_id: int, *, force: bool = False) -> tuple[bool, str]:
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return False, "The bot is not connected to that server."
        cfg = store.get_guild(guild.id)
        if not cfg["features"].get("server_stats", False):
            return False, "Server Stats is disabled for this server."

        stats = cfg.get("server_stats") or {}
        items = list(stats.get("items") or [])[:8]
        if not any(bool(item.get("enabled")) for item in items):
            return False, "Enable at least one stat row first."

        me = guild.me
        if me is None or not me.guild_permissions.manage_channels:
            return False, "The bot needs Manage Channels to create or rename server-stat channels."

        changed = False
        category = None
        raw_category = stats.get("category_id")
        if str(raw_category or "").isdigit():
            candidate = guild.get_channel(int(raw_category))
            if isinstance(candidate, discord.CategoryChannel):
                category = candidate

        category_name = str(stats.get("category_name") or "│ SERVER STATS │").strip()[:100] or "│ SERVER STATS │"
        try:
            if category is None:
                category = await guild.create_category(category_name, reason="Server stats configured from dashboard")
                stats["category_id"] = category.id
                changed = True
            elif category.name != category_name:
                await category.edit(name=category_name, reason="Server stats dashboard sync")

            overwrites = {guild.default_role: discord.PermissionOverwrite(connect=False)}
            for item in items:
                channel = None
                raw_channel = item.get("channel_id")
                if str(raw_channel or "").isdigit():
                    candidate = guild.get_channel(int(raw_channel))
                    if isinstance(candidate, discord.VoiceChannel):
                        channel = candidate

                if not bool(item.get("enabled")):
                    if channel is not None:
                        await channel.delete(reason="Server stat row disabled from dashboard")
                        item["channel_id"] = None
                        changed = True
                    continue

                kind = str(item.get("type") or "members")
                if kind not in STAT_TYPES:
                    item["type"] = "members"
                target_name = self._stat_channel_name(guild, item)
                if channel is None:
                    channel = await guild.create_voice_channel(
                        target_name,
                        category=category,
                        overwrites=overwrites,
                        reason="Server stats configured from dashboard",
                    )
                    item["channel_id"] = channel.id
                    changed = True
                else:
                    edits = {}
                    if channel.name != target_name:
                        edits["name"] = target_name
                    if channel.category_id != category.id:
                        edits["category"] = category
                    if edits:
                        await channel.edit(reason="Server stats dashboard sync", **edits)

            stats["items"] = items
            cfg["server_stats"] = stats
            if changed:
                store.set_guild(guild.id, cfg)
            self._stats_last[guild.id] = time.monotonic()
            active_count = sum(1 for item in items if item.get("enabled"))
            return True, f"Synced {active_count} stat channel{'s' if active_count != 1 else ''}."
        except discord.Forbidden:
            return False, "Discord denied the channel update. Check Manage Channels and category permissions."
        except discord.HTTPException as exc:
            return False, f"Discord rejected the server-stat update ({getattr(exc, 'status', 'HTTP error')})."

    async def remove_server_stats(self, guild_id: int) -> tuple[bool, str]:
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return False, "The bot is not connected to that server."
        cfg = store.get_guild(guild.id)
        stats = cfg.get("server_stats") or {}
        removed = 0
        try:
            for item in list(stats.get("items") or [])[:8]:
                raw = item.get("channel_id")
                channel = guild.get_channel(int(raw)) if str(raw or "").isdigit() else None
                if isinstance(channel, discord.VoiceChannel):
                    await channel.delete(reason="Server stats removed from dashboard")
                    removed += 1
                item["channel_id"] = None

            raw_category = stats.get("category_id")
            category = guild.get_channel(int(raw_category)) if str(raw_category or "").isdigit() else None
            if isinstance(category, discord.CategoryChannel) and not category.channels:
                await category.delete(reason="Empty server stats category removed from dashboard")
            stats["category_id"] = None
            cfg["server_stats"] = stats
            store.set_guild(guild.id, cfg)
            self._stats_last.pop(guild.id, None)
            return True, f"Removed {removed} managed stat channel{'s' if removed != 1 else ''}."
        except discord.Forbidden:
            return False, "Discord denied the removal. Check Manage Channels permission."
        except discord.HTTPException:
            return False, "Discord rejected one of the channel removals."

    @tasks.loop(seconds=30)
    async def server_stats_loop(self):
        now = time.monotonic()
        for guild in list(self.bot.guilds):
            cfg = store.get_guild(guild.id)
            if not cfg["features"].get("server_stats", False):
                continue
            stats = cfg.get("server_stats") or {}
            interval = max(60, min(3600, int(stats.get("update_interval_seconds") or 300)))
            last = self._stats_last.get(guild.id, 0.0)
            if now - last < interval:
                continue
            await self.sync_server_stats(guild.id)

    @server_stats_loop.before_loop
    async def before_server_stats(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Automation(bot))
