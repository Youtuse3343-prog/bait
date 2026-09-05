from __future__ import annotations
from datetime import datetime, timezone
import discord
from discord.ext import commands, tasks
from bot.store import store


class Automation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.auto_messages.start()
        self.status_rotation.start()
        self._status_index = 0

    def cog_unload(self):
        self.auto_messages.cancel()
        self.status_rotation.cancel()

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


async def setup(bot):
    await bot.add_cog(Automation(bot))
