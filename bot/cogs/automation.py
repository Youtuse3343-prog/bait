from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

import discord
from discord.ext import commands, tasks

from bot.store import store
from bot.utils import embed, transcript_channel

STAT_TYPES = {
    "members", "humans", "bots", "boosts", "role", "online", "staff", "verified",
    "unverified", "voice_users", "open_tickets", "total_tickets", "channel_count",
    "server_age_days", "banned",
}


class Automation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._status_index = 0
        self._stats_last: dict[int, float] = {}
        self._ban_cache: dict[int, tuple[float, int]] = {}
        self.auto_messages.start()
        self.scheduled_messages.start()
        self.status_rotation.start()
        self.server_stats_loop.start()
        self.ticket_maintenance.start()
        self.snapshot_loop.start()
        self.action_queue_loop.start()
        self.backup_loop.start()

    def cog_unload(self):
        for loop in (self.auto_messages, self.scheduled_messages, self.status_rotation, self.server_stats_loop, self.ticket_maintenance, self.snapshot_loop, self.action_queue_loop, self.backup_loop):
            loop.cancel()

    @tasks.loop(minutes=1)
    async def auto_messages(self):
        now = datetime.now(timezone.utc)
        for row in store.all_automessages():
            if not bool(row.get("enabled")):
                continue
            guild = self.bot.get_guild(int(row["guild_id"]))
            if not guild or not store.get_guild(guild.id)["features"].get("auto_messages"):
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
                    await channel.send(str(row["content"])[:2000], allowed_mentions=discord.AllowedMentions.none())
                    store.mark_automessage_sent(int(row["id"]))
                except discord.HTTPException:
                    pass

    @auto_messages.before_loop
    async def before_auto(self): await self.bot.wait_until_ready()

    @tasks.loop(seconds=20)
    async def scheduled_messages(self):
        now = datetime.now(timezone.utc)
        for row in store.list_scheduled_messages(due_before=now.isoformat(), limit=50):
            guild = self.bot.get_guild(int(row["guild_id"]))
            if not guild or not store.get_guild(guild.id)["features"].get("auto_messages"):
                continue
            payload = row.get("payload") or {}
            try:
                ok = await self.bot.send_dashboard_message(
                    int(row["channel_id"]),
                    str(payload.get("content") or ""),
                    embed_data=payload.get("embed"),
                    allow_mentions=bool(payload.get("allow_mentions")),
                    publish=bool(payload.get("publish")),
                    buttons=payload.get("buttons") or [],
                )
            except Exception:
                ok = False
            if ok:
                recurrence = int(row.get("recurrence_minutes") or 0)
                if recurrence > 0:
                    next_run = (now + timedelta(minutes=recurrence)).isoformat()
                    store.mark_schedule_run(int(row["id"]), next_run=next_run)
                else:
                    store.mark_schedule_run(int(row["id"]), disable=True)
                store.audit(guild.id, self.bot.user.id, "message.schedule_ran", schedule_id=int(row["id"]), channel_id=int(row["channel_id"]))

    @scheduled_messages.before_loop
    async def before_scheduled(self): await self.bot.wait_until_ready()

    @tasks.loop(seconds=15)
    async def status_rotation(self):
        cfg = store.get_global(); interval = max(15, int(cfg.get("status_interval_seconds", 45)))
        last = getattr(self, "_last_status_change", None); now = time.monotonic()
        if last and now - last < interval: return
        statuses = cfg.get("statuses") or []
        if not statuses: return
        item = statuses[self._status_index % len(statuses)]; self._status_index += 1
        kind = str(item.get("type", "watching")).lower(); text = str(item.get("text", "the server"))[:128]
        if kind == "playing": activity = discord.Game(text)
        elif kind == "streaming": activity = discord.Streaming(name=text, url="https://twitch.tv/discord")
        else:
            atype = {"watching": discord.ActivityType.watching, "listening": discord.ActivityType.listening, "competing": discord.ActivityType.competing}.get(kind, discord.ActivityType.watching)
            activity = discord.Activity(type=atype, name=text)
        try: await self.bot.change_presence(activity=activity, status=discord.Status.online)
        except discord.HTTPException: return
        self._last_status_change = now

    @status_rotation.before_loop
    async def before_status(self): await self.bot.wait_until_ready()

    async def _stat_value(self, guild: discord.Guild, item: dict) -> int:
        kind = str(item.get("type", "members"))
        cfg = store.get_guild(guild.id)
        if kind == "members": return int(guild.member_count or len(guild.members))
        if kind == "humans": return sum(1 for m in guild.members if not m.bot)
        if kind == "bots": return sum(1 for m in guild.members if m.bot)
        if kind == "boosts": return int(guild.premium_subscription_count or 0)
        if kind == "online": return sum(1 for m in guild.members if m.status is not discord.Status.offline)
        if kind == "voice_users": return sum(1 for m in guild.members if m.voice and m.voice.channel)
        if kind == "channel_count": return len(guild.channels)
        if kind == "server_age_days": return max(0, (datetime.now(timezone.utc) - guild.created_at).days)
        if kind == "open_tickets": return int(store.ticket_analytics(guild.id)["open"])
        if kind == "total_tickets": return int(store.ticket_analytics(guild.id)["total"])
        if kind in {"role", "staff", "verified", "unverified"}:
            raw = item.get("role_id")
            if not raw:
                if kind == "staff": raw = cfg["roles"].get("staff") or cfg["roles"].get("ticket_support")
                elif kind == "verified": raw = cfg["roles"].get("verification_add")
                elif kind == "unverified": raw = cfg["roles"].get("verification_remove")
            role = guild.get_role(int(raw)) if str(raw or "").isdigit() else None
            return len(role.members) if role else 0
        if kind == "banned":
            cached = self._ban_cache.get(guild.id)
            if cached and time.monotonic() - cached[0] < 3600: return cached[1]
            try:
                bans = [entry async for entry in guild.bans(limit=None)]
                value = len(bans); self._ban_cache[guild.id] = (time.monotonic(), value); return value
            except discord.HTTPException: return cached[1] if cached else 0
        return 0

    async def _stat_channel_name(self, guild: discord.Guild, item: dict) -> str:
        value = await self._stat_value(guild, item)
        emoji = str(item.get("emoji") or "").strip()[:24]; label = str(item.get("label") or "Stat").strip()[:50]
        template = str(item.get("template") or "{emoji} {label}: {value}").strip()
        try: name = template.format(emoji=emoji, label=label, value=value)
        except (KeyError, ValueError): name = f"{emoji} {label}: {value}"
        name = " ".join(name.split())
        return (name or f"{label}: {value}")[:100]

    async def sync_server_stats(self, guild_id: int, *, force: bool = False) -> tuple[bool, str]:
        guild = self.bot.get_guild(int(guild_id))
        if not guild: return False, "The bot is not connected to that server."
        cfg = store.get_guild(guild.id)
        if not cfg["features"].get("server_stats"): return False, "Server Stats is disabled for this server."
        stats = cfg.get("server_stats") or {}; items = list(stats.get("items") or [])[:12]
        if not any(bool(i.get("enabled")) for i in items): return False, "Enable at least one stat row first."
        me = guild.me
        if not me or not me.guild_permissions.manage_channels: return False, "The bot needs Manage Channels to create or rename stat channels."
        category = None; raw_category = stats.get("category_id")
        if str(raw_category or "").isdigit():
            c = guild.get_channel(int(raw_category)); category = c if isinstance(c, discord.CategoryChannel) else None
        category_name = str(stats.get("category_name") or "│ SERVER STATS │")[:100]
        changed = False
        try:
            if category is None:
                category = await guild.create_category(category_name, reason="Dashboard server stats"); stats["category_id"] = category.id; changed = True
            elif category.name != category_name:
                await category.edit(name=category_name, reason="Dashboard server stats")
            overwrites = {guild.default_role: discord.PermissionOverwrite(connect=False)}
            for item in items:
                raw = item.get("channel_id"); channel = guild.get_channel(int(raw)) if str(raw or "").isdigit() else None
                if not isinstance(channel, discord.VoiceChannel): channel = None
                if not item.get("enabled"):
                    if channel:
                        await channel.delete(reason="Server stat disabled"); item["channel_id"] = None; changed = True
                    continue
                if str(item.get("type")) not in STAT_TYPES: item["type"] = "members"
                target = await self._stat_channel_name(guild, item)
                if channel is None:
                    channel = await guild.create_voice_channel(target, category=category, overwrites=overwrites, reason="Dashboard server stats"); item["channel_id"] = channel.id; changed = True
                else:
                    edits = {}
                    if channel.name != target: edits["name"] = target
                    if channel.category_id != category.id: edits["category"] = category
                    if edits: await channel.edit(reason="Dashboard server stats", **edits)
            stats["items"] = items; cfg["server_stats"] = stats
            if changed: store.set_guild(guild.id, cfg)
            self._stats_last[guild.id] = time.monotonic()
            return True, f"Synced {sum(1 for i in items if i.get('enabled'))} stat channel(s)."
        except discord.Forbidden: return False, "Discord denied the channel update. Check Manage Channels and category permissions."
        except discord.HTTPException as exc: return False, f"Discord rejected the server-stat update ({getattr(exc, 'status', 'HTTP error')})."

    async def remove_server_stats(self, guild_id: int) -> tuple[bool, str]:
        guild = self.bot.get_guild(int(guild_id))
        if not guild: return False, "The bot is not connected to that server."
        cfg = store.get_guild(guild.id); stats = cfg.get("server_stats") or {}; removed = 0
        try:
            for item in list(stats.get("items") or [])[:12]:
                raw = item.get("channel_id"); channel = guild.get_channel(int(raw)) if str(raw or "").isdigit() else None
                if isinstance(channel, discord.VoiceChannel): await channel.delete(reason="Server stats removed"); removed += 1
                item["channel_id"] = None
            raw = stats.get("category_id"); category = guild.get_channel(int(raw)) if str(raw or "").isdigit() else None
            if isinstance(category, discord.CategoryChannel) and not category.channels: await category.delete(reason="Server stats removed")
            stats["category_id"] = None; cfg["server_stats"] = stats; store.set_guild(guild.id, cfg)
            return True, f"Removed {removed} managed server-stat channel(s)."
        except discord.HTTPException: return False, "One or more stat channels could not be removed."

    @tasks.loop(seconds=30)
    async def server_stats_loop(self):
        for guild in list(self.bot.guilds):
            cfg = store.get_guild(guild.id)
            if not cfg["features"].get("server_stats"): continue
            interval = max(60, min(3600, int(cfg.get("server_stats", {}).get("update_interval_seconds", 300))))
            last = self._stats_last.get(guild.id, 0.0)
            if time.monotonic() - last >= interval: await self.sync_server_stats(guild.id)

    @server_stats_loop.before_loop
    async def before_stats(self): await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def ticket_maintenance(self):
        now = datetime.now(timezone.utc)
        for guild in list(self.bot.guilds):
            cfg = store.get_guild(guild.id); hours = int(cfg.get("tickets", {}).get("auto_close_hours", 0) or 0)
            if not cfg["features"].get("tickets") or hours <= 0: continue
            for ticket in store.list_tickets(guild.id, status="open", limit=500):
                marker = ticket.get("activity_at") or ticket.get("created_at")
                try: last = datetime.fromisoformat(str(marker).replace("Z", "+00:00"))
                except ValueError: continue
                if now - last < timedelta(hours=hours): continue
                ch = guild.get_channel(int(ticket["channel_id"]))
                if not isinstance(ch, discord.TextChannel):
                    store.update_ticket(int(ticket["channel_id"]), status="closed", closed_at=now.isoformat(), closed_by=str(self.bot.user.id)); continue
                transcript = await transcript_channel(ch, int(cfg.get("tickets", {}).get("transcript_limit", 1500)))
                log_id = cfg["channels"].get("ticket_logs") or cfg["channels"].get("logs"); log_ch = guild.get_channel(int(log_id)) if log_id else None
                if isinstance(log_ch, discord.TextChannel):
                    try: await log_ch.send(embed=embed("Ticket Auto-Closed", f"`#{ch.name}` was inactive for **{hours}h**.", guild_id=guild.id, kind="ticket"), file=transcript)
                    except discord.HTTPException: pass
                store.update_ticket(ch.id, status="closed", closed_at=now.isoformat(), closed_by=str(self.bot.user.id))
                try: await ch.delete(reason=f"Ticket auto-close after {hours}h inactivity")
                except discord.HTTPException: pass

    @ticket_maintenance.before_loop
    async def before_ticket_maintenance(self): await self.bot.wait_until_ready()

    @tasks.loop(minutes=1)
    async def snapshot_loop(self):
        for guild in list(self.bot.guilds):
            me = guild.me
            payload = {
                "id": guild.id, "name": guild.name, "member_count": int(guild.member_count or 0),
                "humans": sum(1 for m in guild.members if not m.bot), "bots": sum(1 for m in guild.members if m.bot),
                "boosts": int(guild.premium_subscription_count or 0), "online": sum(1 for m in guild.members if str(m.status) != "offline"),
                "voice_users": sum(1 for m in guild.members if m.voice and m.voice.channel),
                "created_at": guild.created_at.isoformat(), "icon_url": str(guild.icon.url) if guild.icon else "", "owner_id": guild.owner_id,
                "channels": [{"id":c.id,"name":c.name,"type":str(c.type),"position":getattr(c,"position",0)} for c in guild.channels],
                "roles": [{"id":r.id,"name":r.name,"position":r.position,"managed":r.managed,"member_count":len(r.members)} for r in guild.roles],
                "permissions": {name: bool(value) for name, value in (me.guild_permissions if me else discord.Permissions.none())},
                "bot_top_role_position": me.top_role.position if me else 0,
                "bot_ready": True,
            }
            store.set_snapshot(guild.id, payload)

    @snapshot_loop.before_loop
    async def before_snapshot(self): await self.bot.wait_until_ready()

    @tasks.loop(seconds=5)
    async def action_queue_loop(self):
        for action in store.claim_actions(limit=20):
            ok, result = await self._process_action(action)
            store.finish_action(action["id"], ok, result)

    @action_queue_loop.before_loop
    async def before_actions(self): await self.bot.wait_until_ready()

    async def _process_action(self, row: dict) -> tuple[bool, str]:
        action = row.get("action"); payload = row.get("payload") or {}; gid = int(row.get("guild_id") or 0)
        try:
            if action == "send_message":
                ok = await self.bot.send_dashboard_message(int(payload["channel_id"]), payload.get("content",""), embed_data=payload.get("embed"), allow_mentions=payload.get("allow_mentions",False), publish=payload.get("publish",False), buttons=payload.get("buttons") or [])
                return ok, "Message sent" if ok else "Message failed"
            if action == "send_dm":
                ok = await self.bot.send_dashboard_dm(int(payload["user_id"]), payload.get("content", "")); return ok, "DM sent" if ok else "DM failed"
            if action == "stats_sync": return await self.sync_server_stats(gid, force=True)
            if action == "stats_remove": return await self.remove_server_stats(gid)
            if action == "raid_mode":
                cog = self.bot.get_cog("AutoMod"); return await cog.set_raid_mode(gid, bool(payload.get("enabled")), actor_id=int(payload.get("actor_id") or self.bot.user.id)) if cog else (False, "AutoMod unavailable")
            if action == "ticket_panel": return (await self.bot.post_dashboard_ticket_panel(gid, int(payload["channel_id"])), "Ticket panel processed")
            if action == "verification_panel": return (await self.bot.post_dashboard_verification_panel(gid, int(payload["channel_id"])), "Verification panel processed")
            if action == "apply_verification": return await self.bot.apply_verification(gid, int(payload["user_id"]))
            if action == "moderate_user": return await self.bot.dashboard_moderate(gid,int(payload["user_id"]),payload.get("moderation_action","warn"),payload.get("reason","No reason provided"),int(payload.get("duration_minutes",10)))
            if action == "moderate_channel": return await self.bot.dashboard_channel_action(gid,int(payload["channel_id"]),payload.get("channel_action","slowmode"),int(payload.get("value",0)))
            if action == "ticket_close": return await self.bot.dashboard_close_ticket(gid,int(payload["channel_id"]))
            if action == "commands_sync": return await self.bot.dashboard_sync_commands(gid or None,bool(payload.get("clear_guild_overrides",False)))
            return False, f"Unknown action: {action}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"[:1000]

    @tasks.loop(hours=1)
    async def backup_loop(self):
        cfg = store.get_global(); bcfg = cfg.get("backup", {})
        if not bcfg.get("enabled", True): return
        interval = max(1, min(168, int(bcfg.get("interval_hours", 24))))
        for guild in list(self.bot.guilds):
            latest = store.list_backups(guild.id, limit=1)
            due = True
            if latest:
                try: due = datetime.now(timezone.utc) - datetime.fromisoformat(str(latest[0]["created_at"]).replace("Z", "+00:00")) >= timedelta(hours=interval)
                except ValueError: pass
            if due: store.backup_guild(guild.id)
            store.prune_backups(guild.id, int(bcfg.get("keep", 14)))

    @backup_loop.before_loop
    async def before_backup(self): await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Automation(bot))
