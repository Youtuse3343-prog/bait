from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import discord
from discord.ext import commands

from bot.store import store
from bot.utils import embed, safe_dm

URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
INVITE_RE = re.compile(r"(?:discord\.gg/|discord(?:app)?\.com/invite/)[A-Za-z0-9-]+", re.I)


class AutoMod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.message_times: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        self.message_texts: dict[tuple[int, int], deque[str]] = defaultdict(lambda: deque(maxlen=8))
        self.join_times: dict[int, deque[float]] = defaultdict(deque)

    def _ignored(self, message: discord.Message, cfg: dict) -> bool:
        if not isinstance(message.author, discord.Member) or message.author.bot:
            return True
        if message.author.guild_permissions.administrator or message.author.guild_permissions.manage_messages:
            return True
        acfg = cfg.get("automod", {})
        if message.channel.id in {int(x) for x in acfg.get("ignore_channels", []) if str(x).isdigit()}:
            return True
        ignored_roles = {int(x) for x in acfg.get("ignore_roles", []) if str(x).isdigit()}
        return any(r.id in ignored_roles for r in message.author.roles)

    def _domain(self, raw: str) -> str:
        try:
            host = (urlparse(raw).hostname or "").lower().strip(".")
            return host[4:] if host.startswith("www.") else host
        except Exception:
            return ""

    def _violations(self, message: discord.Message, cfg: dict) -> list[str]:
        content = message.content or ""
        acfg = cfg.get("automod", {})
        now = time.monotonic()
        reasons: list[str] = []
        key = (message.guild.id, message.author.id)

        if acfg.get("spam_enabled", True):
            dq = self.message_times[key]
            window = max(2, int(acfg.get("spam_window_seconds", 8)))
            while dq and now - dq[0] > window:
                dq.popleft()
            dq.append(now)
            if len(dq) >= max(3, int(acfg.get("spam_messages", 6))):
                reasons.append("message spam")
                dq.clear()

        norm = " ".join(content.lower().split())
        if norm:
            texts = self.message_texts[key]
            texts.append(norm)
            if acfg.get("duplicate_enabled", True):
                need = max(2, int(acfg.get("duplicate_count", 4)))
                if len(texts) >= need and list(texts)[-need:].count(norm) >= need:
                    reasons.append("duplicate-message spam")
                    texts.clear()

        if acfg.get("mention_enabled", True):
            mention_count = len(set(message.raw_mentions)) + len(set(message.raw_role_mentions))
            if message.mention_everyone:
                mention_count += 10
            if mention_count >= max(2, int(acfg.get("mention_limit", 6))):
                reasons.append("mention spam")

        if acfg.get("caps_enabled") and len(content) >= max(5, int(acfg.get("caps_min_length", 16))):
            letters = [c for c in content if c.isalpha()]
            if letters:
                pct = sum(1 for c in letters if c.isupper()) * 100 / len(letters)
                if pct >= max(10, int(acfg.get("caps_percent", 80))):
                    reasons.append("excessive caps")

        if acfg.get("invite_block") and INVITE_RE.search(content):
            reasons.append("Discord invite")

        urls = URL_RE.findall(content)
        whitelist = {str(d).lower().strip() for d in acfg.get("domain_whitelist", []) if str(d).strip()}
        blacklist = {str(d).lower().strip() for d in acfg.get("domain_blacklist", []) if str(d).strip()}
        for url in urls:
            domain = self._domain(url)
            if not domain:
                continue
            allowed = any(domain == d or domain.endswith("." + d) for d in whitelist)
            blocked = any(domain == d or domain.endswith("." + d) for d in blacklist)
            if blocked or (acfg.get("link_block") and not allowed):
                reasons.append(f"blocked link ({domain})")
                break

        lowered = content.casefold()
        for word in [str(w).strip().casefold() for w in acfg.get("bad_words", []) if str(w).strip()]:
            if word and word in lowered:
                reasons.append("filtered phrase")
                break
        return reasons

    async def _log(self, guild: discord.Guild, title: str, description: str):
        cfg = store.get_guild(guild.id)
        cid = cfg["channels"].get("automod_logs") or cfg["channels"].get("mod_logs") or cfg["channels"].get("logs")
        ch = guild.get_channel(int(cid)) if cid else None
        if cfg["features"].get("logs") and isinstance(ch, discord.TextChannel):
            try:
                await ch.send(embed=embed(title, description, guild_id=guild.id, kind="moderation"))
            except discord.HTTPException:
                pass

    async def _enforce(self, message: discord.Message, reason: str):
        if not message.guild or not isinstance(message.author, discord.Member):
            return
        cfg = store.get_guild(message.guild.id)
        acfg = cfg.get("automod", {})
        if acfg.get("delete_trigger", True):
            try:
                await message.delete()
            except discord.HTTPException:
                pass
        action = str(acfg.get("action") or "timeout")
        minutes = max(1, int(acfg.get("timeout_minutes", 10)))
        outcome = action
        try:
            if action == "warn":
                store.add_warning(message.guild.id, message.author.id, self.bot.user.id, f"AutoMod: {reason}")
            elif action == "timeout":
                await message.author.timeout(timedelta(minutes=minutes), reason=f"AutoMod: {reason}")
            elif action == "kick":
                await message.author.kick(reason=f"AutoMod: {reason}")
            elif action == "ban":
                await message.author.ban(reason=f"AutoMod: {reason}")
            else:
                outcome = "delete only"
        except discord.Forbidden:
            outcome = f"{action} failed (permissions/hierarchy)"
        except discord.HTTPException:
            outcome = f"{action} failed (Discord API)"

        cid = store.create_case(
            message.guild.id,
            message.author.id,
            self.bot.user.id,
            "automod",
            reason,
            automated=True,
            action=action,
            outcome=outcome,
            channel_id=message.channel.id,
            message_id=message.id,
        )
        await self._log(message.guild, f"AutoMod · Case #{cid}", f"**Member:** {message.author.mention}\n**Rule:** {reason}\n**Action:** {outcome}\n**Channel:** {message.channel.mention}")
        if outcome not in {"delete only"} and not outcome.endswith("failed (permissions/hierarchy)"):
            await safe_dm(message.author, f"AutoMod action in **{message.guild.name}**\nReason: **{reason}**\nAction: **{outcome}**")

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return
        cfg = store.get_guild(message.guild.id)
        if not cfg["features"].get("automod") or self._ignored(message, cfg):
            return
        reasons = self._violations(message, cfg)
        if reasons:
            await self._enforce(message, ", ".join(dict.fromkeys(reasons)))

    @commands.Cog.listener("on_member_join")
    async def on_member_join(self, member: discord.Member):
        cfg = store.get_guild(member.guild.id)
        if not cfg["features"].get("automod"):
            return
        acfg = cfg.get("automod", {})
        now = time.monotonic()
        dq = self.join_times[member.guild.id]
        window = max(5, int(acfg.get("join_rate_window_seconds", 30)))
        while dq and now - dq[0] > window:
            dq.popleft()
        dq.append(now)
        if acfg.get("join_rate_enabled", True) and len(dq) >= max(3, int(acfg.get("join_rate_count", 12))) and acfg.get("auto_raid_mode", True) and not acfg.get("raid_mode_active"):
            await self.set_raid_mode(member.guild.id, True, actor_id=self.bot.user.id, automatic=True)
            dq.clear()

        age_hours = (datetime.now(timezone.utc) - member.created_at).total_seconds() / 3600
        too_new = acfg.get("new_account_enabled") and age_hours < max(0, int(acfg.get("min_account_age_hours", 24)))
        if acfg.get("raid_mode_active") and acfg.get("raid_kick_new_accounts"):
            too_new = too_new or age_hours < max(0, int(acfg.get("min_account_age_hours", 24)))
        if too_new:
            reason = f"Account age {age_hours:.1f}h is below the configured minimum"
            try:
                await member.kick(reason=f"AutoMod anti-raid: {reason}")
                outcome = "kicked"
            except discord.HTTPException:
                outcome = "kick failed"
            cid = store.create_case(member.guild.id, member.id, self.bot.user.id, "anti_raid", reason, automated=True, outcome=outcome)
            await self._log(member.guild, f"Anti-Raid · Case #{cid}", f"**Member:** {member} (`{member.id}`)\n**Reason:** {reason}\n**Result:** {outcome}")

    async def set_raid_mode(self, guild_id: int, enabled: bool, *, actor_id: int | None = None, automatic: bool = False) -> tuple[bool, str]:
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            return False, "The bot is not connected to that server."
        cfg = store.get_guild(guild.id)
        acfg = cfg.setdefault("automod", {})
        acfg["raid_mode_active"] = bool(enabled)
        cfg["automod"] = acfg
        store.set_guild(guild.id, cfg)
        changed = 0
        for cid in acfg.get("raid_lock_channels", []):
            channel = guild.get_channel(int(cid)) if str(cid).isdigit() else None
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                overwrite = channel.overwrites_for(guild.default_role)
                overwrite.send_messages = False if enabled else None
                await channel.set_permissions(guild.default_role, overwrite=overwrite, reason="Raid mode enabled" if enabled else "Raid mode disabled")
                changed += 1
            except discord.HTTPException:
                pass
        store.audit(guild.id, actor_id or self.bot.user.id, "raid_mode.enabled" if enabled else "raid_mode.disabled", automatic=automatic, channels_changed=changed)
        await self._log(guild, "Raid Mode Enabled" if enabled else "Raid Mode Disabled", f"Locked channels changed: **{changed}**\nAutomatic: **{'Yes' if automatic else 'No'}**")
        return True, f"Raid mode {'enabled' if enabled else 'disabled'}. Updated {changed} configured channel(s)."


async def setup(bot):
    await bot.add_cog(AutoMod(bot))
