import asyncio
import hashlib
import hmac
import html
import io
import json
import logging
import os
import random
import sys
import traceback
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlparse

import discord
from aiohttp import ClientError, ClientSession, ClientTimeout, web
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient


BUILD_VERSION = "4.0.1-production-dashboard"

load_dotenv()

# =========================
# ENV CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017/moealturej_bot").strip()
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "moealturej_bot").strip()

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/")
DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "").strip()
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "1222903158125105194"))
OWNER_CONTACT = os.getenv("OWNER_CONTACT", "Contact moealturej, the owner, to talk about using this bot for your server.").strip()

DEFAULT_STORE_URL = os.getenv("DEFAULT_STORE_URL", "https://www.moealturej.com").strip()
ROTATING_STATUSES = [
    s.strip() for s in os.getenv("ROTATING_STATUSES", "Watching /help,moealturej support,Watching tickets").split(",") if s.strip()
]
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0").strip()
WEB_PORT = int(os.getenv("PORT", os.getenv("WEB_PORT", "8080")))
KEEP_ALIVE_URL = os.getenv("KEEP_ALIVE_URL", "").strip()
ENABLE_SELF_PING = os.getenv("ENABLE_SELF_PING", "false").strip().lower() in {"1", "true", "yes", "on"}
SYNC_COMMANDS = os.getenv("SYNC_COMMANDS", "false").strip().lower() in {"1", "true", "yes", "on"}
DASHBOARD_OWNER_ONLY = os.getenv("DASHBOARD_OWNER_ONLY", "true").strip().lower() in {"1", "true", "yes", "on"}
WEB_SESSION_DAYS = max(1, min(30, int(os.getenv("WEB_SESSION_DAYS", "7"))))

# Startup protection: if Render/Cloudflare temporarily blocks this server IP
# from discord.com, do NOT crash/restart-loop. Keep the web health server
# online and wait before trying login again. Restart loops make error 1015 last longer.
STARTUP_LOGIN_RETRY_SECONDS = int(os.getenv("STARTUP_LOGIN_RETRY_SECONDS", "1800"))
STARTUP_GENERIC_RETRY_SECONDS = int(os.getenv("STARTUP_GENERIC_RETRY_SECONDS", "300"))
STARTUP_MAX_LOGIN_ATTEMPTS = int(os.getenv("STARTUP_MAX_LOGIN_ATTEMPTS", "0"))  # 0 = forever

EMBED_COLOR = 0x7C3AED
ERROR_COLOR = 0xEF4444
SUCCESS_COLOR = 0x22C55E
INFO_COLOR = 0x38BDF8
WARNING_COLOR = 0xF59E0B
STARTED_AT = datetime.now(timezone.utc)

log = logging.getLogger("moealturej")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# =========================
# SMART DISCORD RATE LIMITING
# =========================
# These are intentionally conservative. Discord.py handles normal per-route
# buckets, but a bug, spam-clicks, join raids, or multiple dashboard sends can
# still push the bot into Discord's global 429 lockout. All high-volume Discord
# actions in this app now pass through this guard.
DISCORD_API_MIN_GAP = float(os.getenv("DISCORD_API_MIN_GAP", "1.25"))
DISCORD_ROLE_MIN_GAP = float(os.getenv("DISCORD_ROLE_MIN_GAP", "3.50"))
DISCORD_MESSAGE_MIN_GAP = float(os.getenv("DISCORD_MESSAGE_MIN_GAP", "1.75"))
DISCORD_INTERACTION_MIN_GAP = float(os.getenv("DISCORD_INTERACTION_MIN_GAP", "1.25"))
DISCORD_CHANNEL_MIN_GAP = float(os.getenv("DISCORD_CHANNEL_MIN_GAP", "7.50"))
DISCORD_MAX_RETRIES = int(os.getenv("DISCORD_MAX_RETRIES", "3"))
DISCORD_429_CIRCUIT_THRESHOLD = int(os.getenv("DISCORD_429_CIRCUIT_THRESHOLD", "3"))
DISCORD_429_CIRCUIT_SECONDS = int(os.getenv("DISCORD_429_CIRCUIT_SECONDS", "900"))
STATS_UPDATE_MINUTES = int(os.getenv("STATS_UPDATE_MINUTES", "60"))
VERIFY_CLICK_COOLDOWN_SECONDS = int(os.getenv("VERIFY_CLICK_COOLDOWN_SECONDS", "45"))
TICKET_CLICK_COOLDOWN_SECONDS = int(os.getenv("TICKET_CLICK_COOLDOWN_SECONDS", "120"))
DASHBOARD_SEND_COOLDOWN_SECONDS = int(os.getenv("DASHBOARD_SEND_COOLDOWN_SECONDS", "20"))
MEMBER_JOIN_WELCOME_COOLDOWN_SECONDS = int(os.getenv("MEMBER_JOIN_WELCOME_COOLDOWN_SECONDS", "20"))
CONFIG_CACHE_SECONDS = int(os.getenv("CONFIG_CACHE_SECONDS", "30"))
COMMAND_COOLDOWN_SECONDS = int(os.getenv("COMMAND_COOLDOWN_SECONDS", "4"))
MAX_PURGE_AMOUNT = int(os.getenv("MAX_PURGE_AMOUNT", "100"))

class DiscordRateLimiter:
    def __init__(self) -> None:
        self._global_lock = asyncio.Lock()
        self._route_locks: dict[str, asyncio.Lock] = {}
        self._last_global = 0.0
        self._last_route: dict[str, float] = {}
        self._cooldowns: dict[str, float] = {}
        self._blocked_until = 0.0
        self._route_429s: dict[str, list[float]] = {}

    def _lock_for(self, route: str) -> asyncio.Lock:
        lock = self._route_locks.get(route)
        if lock is None:
            lock = asyncio.Lock()
            self._route_locks[route] = lock
        return lock

    async def wait(self, route: str, min_gap: float = DISCORD_API_MIN_GAP) -> None:
        loop = asyncio.get_running_loop()
        async with self._global_lock:
            now = loop.time()
            wait_for = max(0.0, self._blocked_until - now, self._last_global + DISCORD_API_MIN_GAP - now)
            if wait_for:
                await asyncio.sleep(wait_for + random.uniform(0.05, 0.20))
            self._last_global = loop.time()

        lock = self._lock_for(route)
        async with lock:
            now = loop.time()
            wait_for = max(0.0, self._last_route.get(route, 0.0) + min_gap - now)
            if wait_for:
                await asyncio.sleep(wait_for + random.uniform(0.05, 0.20))
            self._last_route[route] = loop.time()

    def block_global(self, seconds: float) -> None:
        loop = asyncio.get_running_loop()
        self._blocked_until = max(self._blocked_until, loop.time() + max(1.0, seconds))

    def is_globally_blocked(self) -> bool:
        return self._blocked_until > asyncio.get_running_loop().time()

    def register_429(self, route: str, retry_after: float) -> None:
        """Circuit-break repeated 429s so the bot stops digging the hole deeper."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        recent = [t for t in self._route_429s.get(route, []) if now - t < 180]
        recent.append(now)
        self._route_429s[route] = recent
        if len(recent) >= DISCORD_429_CIRCUIT_THRESHOLD:
            pause = max(float(DISCORD_429_CIRCUIT_SECONDS), retry_after + 60.0)
            self._blocked_until = max(self._blocked_until, now + pause)
            log.error("Discord circuit breaker opened for %.0fs after repeated 429s on route %s", pause, route)

    def seconds_until_unblocked(self) -> float:
        return max(0.0, self._blocked_until - asyncio.get_running_loop().time())

    def on_cooldown(self, key: str, seconds: int) -> bool:
        loop = asyncio.get_running_loop()
        now = loop.time()
        until = self._cooldowns.get(key, 0.0)
        if until > now:
            return True
        self._cooldowns[key] = now + seconds
        # small cleanup so this never grows forever
        if len(self._cooldowns) > 10000:
            old = now - 300
            self._cooldowns = {k: v for k, v in self._cooldowns.items() if v > old}
        return False

rate_limiter = DiscordRateLimiter()


def _retry_after_from(exc: discord.HTTPException) -> float:
    """Get Discord's exact retry_after from every place discord.py/aiohttp may expose it."""
    candidates = [getattr(exc, "retry_after", None)]
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        candidates.extend([
            headers.get("Retry-After"),
            headers.get("X-RateLimit-Reset-After"),
        ])
    for attr in ("text", "message"):
        data = getattr(exc, attr, None)
        if isinstance(data, dict):
            candidates.append(data.get("retry_after"))
        elif isinstance(data, str) and data.strip().startswith("{"):
            try:
                parsed = json.loads(data)
                if isinstance(parsed, dict):
                    candidates.append(parsed.get("retry_after"))
            except Exception:
                pass
    for value in candidates:
        try:
            if value is not None:
                return max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return 0.0


def _is_global_429(exc: discord.HTTPException) -> bool:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers and str(headers.get("X-RateLimit-Global", "")).lower() == "true":
        return True
    text = str(getattr(exc, "text", "") or getattr(exc, "message", "") or exc).lower()
    return "global" in text or "blocked" in text


async def discord_guarded(label: str, route: str, func, *, min_gap: float = DISCORD_API_MIN_GAP, retries: int = DISCORD_MAX_RETRIES, default=None):
    """
    Hard guard for Discord API calls.
    - Spaces requests before they hit Discord.
    - Uses Discord's actual Retry-After on 429.
    - Opens a circuit breaker after repeated 429s so one feature cannot poison the whole bot.
    """
    max_attempts = max(1, retries + 1)
    for attempt in range(1, max_attempts + 1):
        if rate_limiter.is_globally_blocked() and route.startswith(("edit_channel", "create_channel", "presence")):
            log.warning("Skipping non-critical Discord action during global cooldown: %s (%.0fs left)", label, rate_limiter.seconds_until_unblocked())
            return default
        await rate_limiter.wait(route, min_gap)
        try:
            return await func()
        except discord.Forbidden:
            log.warning("Discord forbidden during %s", label)
            return default
        except discord.NotFound:
            log.warning("Discord target not found during %s", label)
            return default
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 429:
                retry_after = _retry_after_from(exc) or min(120.0, 2.0 ** attempt)
                if _is_global_429(exc):
                    rate_limiter.block_global(retry_after + 10)
                rate_limiter.register_429(route, retry_after)
                if attempt >= max_attempts:
                    break
                log.warning("Discord 429 during %s. Retry %s/%s after %.2fs", label, attempt, max_attempts - 1, retry_after)
                await asyncio.sleep(retry_after + random.uniform(1.0, 2.5))
                continue
            log.warning("Discord HTTP error during %s: %s", label, exc)
            return default
        except ClientError as exc:
            if attempt >= max_attempts:
                break
            wait_for = min(30.0, 2.0 * attempt)
            log.warning("Network error during %s: %s. Retry %s/%s after %.1fs", label, exc, attempt, max_attempts - 1, wait_for)
            await asyncio.sleep(wait_for)
    log.error("Discord action failed after retries: %s", label)
    return default


async def safe_interaction_defer(interaction: discord.Interaction, *, ephemeral: bool = True) -> bool:
    if interaction.response.is_done():
        return True
    async def op():
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        return True
    return bool(await discord_guarded("interaction defer", f"interaction:{interaction.user.id}", op, min_gap=DISCORD_INTERACTION_MIN_GAP, retries=1, default=False))


async def safe_interaction_send(interaction: discord.Interaction, *args, **kwargs) -> bool:
    async def op():
        if interaction.response.is_done():
            await interaction.followup.send(*args, **kwargs)
        else:
            await interaction.response.send_message(*args, **kwargs)
        return True
    return bool(await discord_guarded("interaction response", f"interaction:{interaction.user.id}", op, min_gap=DISCORD_INTERACTION_MIN_GAP, default=False))


async def safe_channel_send(channel: discord.abc.Messageable, *args, **kwargs):
    channel_id = getattr(channel, "id", "dm")
    return await discord_guarded("channel send", f"send:{channel_id}", lambda: channel.send(*args, **kwargs), min_gap=DISCORD_MESSAGE_MIN_GAP)


async def safe_user_send(user: discord.abc.User, *args, **kwargs) -> bool:
    return bool(await discord_guarded("user DM", f"dm:{user.id}", lambda: user.send(*args, **kwargs), min_gap=DISCORD_MESSAGE_MIN_GAP, default=False))


CHANNEL_NAME_CACHE: dict[int, str] = {}


async def safe_channel_edit(channel: discord.abc.GuildChannel, **kwargs) -> bool:
    # Channel edits are one of Discord's strictest buckets. Never call it for a no-op.
    new_name = kwargs.get("name")
    if new_name is not None:
        if CHANNEL_NAME_CACHE.get(channel.id) == new_name or getattr(channel, "name", None) == new_name:
            CHANNEL_NAME_CACHE[channel.id] = new_name
            return True
        CHANNEL_NAME_CACHE[channel.id] = new_name
    return bool(await discord_guarded("channel edit", f"edit_channel:{channel.id}", lambda: channel.edit(**kwargs), min_gap=DISCORD_CHANNEL_MIN_GAP, default=False))


async def safe_channel_delete(channel: discord.abc.GuildChannel, **kwargs) -> bool:
    return bool(await discord_guarded("channel delete", f"delete_channel:{channel.id}", lambda: channel.delete(**kwargs), min_gap=DISCORD_CHANNEL_MIN_GAP, default=False))

async def safe_create_text_channel(guild: discord.Guild, **kwargs) -> Optional[discord.TextChannel]:
    return await discord_guarded("create text channel", f"create_channel:{guild.id}", lambda: guild.create_text_channel(**kwargs), min_gap=DISCORD_CHANNEL_MIN_GAP, default=None)


async def safe_create_voice_channel(guild: discord.Guild, *args, **kwargs) -> Optional[discord.VoiceChannel]:
    return await discord_guarded("create voice channel", f"create_channel:{guild.id}", lambda: guild.create_voice_channel(*args, **kwargs), min_gap=DISCORD_CHANNEL_MIN_GAP, default=None)


async def safe_create_category(guild: discord.Guild, *args, **kwargs) -> Optional[discord.CategoryChannel]:
    return await discord_guarded("create category", f"create_channel:{guild.id}", lambda: guild.create_category(*args, **kwargs), min_gap=DISCORD_CHANNEL_MIN_GAP, default=None)


async def safe_change_presence(*args, **kwargs) -> bool:
    return bool(await discord_guarded("change presence", "presence", lambda: bot.change_presence(*args, **kwargs), min_gap=10.0, retries=2, default=False))


async def safe_fetch_member(guild: discord.Guild, user_id: int):
    return await discord_guarded("fetch member", f"fetch_member:{guild.id}", lambda: guild.fetch_member(user_id), min_gap=DISCORD_API_MIN_GAP, default=None)


async def safe_fetch_user(user_id: int):
    return await discord_guarded("fetch user", "fetch_user", lambda: bot.fetch_user(user_id), min_gap=DISCORD_API_MIN_GAP, default=None)


TICKET_TYPES = {
    "general": {
        "label": "General support",
        "description": "Get help with general questions.",
        "emoji": "💬",
        "support_role_key": "ticket_role_general",
    },
    "hwid": {
        "label": "Key HWID reset",
        "description": "Request a HWID reset for your key.",
        "emoji": "🔑",
        "support_role_key": "ticket_role_hwid",
    },
    "key_not_received": {
        "label": "Key not received",
        "description": "Get help if your key was not delivered.",
        "emoji": "📦",
        "support_role_key": "ticket_role_key_not_received",
    },
}

COMMAND_CATALOG: Dict[str, Dict[str, str]] = {
    "ping": {"group": "Essentials", "label": "Ping", "feature": "feature_utilities"},
    "store": {"group": "Essentials", "label": "Store", "feature": "feature_utilities"},
    "help": {"group": "Essentials", "label": "Help", "feature": "feature_utilities"},
    "serverinfo": {"group": "Essentials", "label": "Server info", "feature": "feature_utilities"},
    "userinfo": {"group": "Essentials", "label": "User info", "feature": "feature_utilities"},
    "avatar": {"group": "Essentials", "label": "Avatar", "feature": "feature_utilities"},
    "commands": {"group": "Administration", "label": "Admin command menu", "feature": "feature_admin_commands"},
    "setup_enable": {"group": "Administration", "label": "Enable / disable bot", "feature": "feature_admin_commands"},
    "set_admin_role": {"group": "Administration", "label": "Set admin role", "feature": "feature_admin_commands"},
    "set_verified_role": {"group": "Verification", "label": "Set verified role", "feature": "feature_verification"},
    "set_unverified_role": {"group": "Verification", "label": "Set unverified role", "feature": "feature_verification"},
    "set_auto_role": {"group": "Welcome", "label": "Set auto role", "feature": "feature_welcome"},
    "set_logs": {"group": "Administration", "label": "Set log channels", "feature": "feature_admin_commands"},
    "send_verification_panel": {"group": "Verification", "label": "Send verification panel", "feature": "feature_verification"},
    "set_ticket_category": {"group": "Tickets", "label": "Set ticket category", "feature": "feature_tickets"},
    "set_ticket_role": {"group": "Tickets", "label": "Set ticket support role", "feature": "feature_tickets"},
    "send_ticket_panel": {"group": "Tickets", "label": "Send ticket panel", "feature": "feature_tickets"},
    "set_store": {"group": "Administration", "label": "Set store URL", "feature": "feature_admin_commands"},
    "announce": {"group": "Content", "label": "Announcement", "feature": "feature_announcements"},
    "stats_setup": {"group": "Statistics", "label": "Stats setup", "feature": "feature_stats"},
    "config_show": {"group": "Administration", "label": "Show config", "feature": "feature_admin_commands"},
    "setup_audit": {"group": "Administration", "label": "Setup audit", "feature": "feature_admin_commands"},
    "purge": {"group": "Moderation", "label": "Purge messages", "feature": "feature_moderation"},
    "timeout": {"group": "Moderation", "label": "Timeout", "feature": "feature_moderation"},
    "untimeout": {"group": "Moderation", "label": "Remove timeout", "feature": "feature_moderation"},
    "warn": {"group": "Moderation", "label": "Warn", "feature": "feature_moderation"},
    "warnings": {"group": "Moderation", "label": "View warnings", "feature": "feature_moderation"},
    "slowmode": {"group": "Moderation", "label": "Slowmode", "feature": "feature_moderation"},
    "lock": {"group": "Moderation", "label": "Lock channel", "feature": "feature_moderation"},
    "unlock": {"group": "Moderation", "label": "Unlock channel", "feature": "feature_moderation"},
    "ticket_add": {"group": "Tickets", "label": "Add member to ticket", "feature": "feature_tickets"},
    "ticket_rename": {"group": "Tickets", "label": "Rename ticket", "feature": "feature_tickets"},
}
DEFAULT_COMMAND_ENABLED: Dict[str, bool] = {name: True for name in COMMAND_CATALOG}

DEFAULT_OWNER_SETTINGS: Dict[str, Any] = {
    "key": "global",
    "dashboard_title": "moealturej Bot Control",
    "dashboard_subtitle": "Private operations dashboard",
    "dashboard_notice": "",
    "dashboard_accent": "A855F7",
    "dashboard_logo_url": "",
    "default_store_url": DEFAULT_STORE_URL,
    "default_brand_name": "moealturej",
    "default_brand_color": "A855F7",
    "default_brand_footer": "moealturej • Professional server tools",
    "default_brand_icon_url": "",
    "presence_enabled": True,
    "presence_type": "watching",
    "presence_interval_seconds": 300,
    "presence_statuses": list(ROTATING_STATUSES) or ["Watching /help", "moealturej support", "Watching tickets"],
    "global_pause": False,
    "global_pause_message": "Bot features are temporarily paused for maintenance.",
    "owner_contact": OWNER_CONTACT,
    "dm_sender_enabled": True,
    "announcement_sender_enabled": True,
    "stats_interval_minutes": STATS_UPDATE_MINUTES,
    "max_purge_amount": MAX_PURGE_AMOUNT,
}

DEFAULT_GUILD_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "verified_role": None,
    "unverified_role": None,
    "auto_role": None,
    "bot_admin_role": None,
    "welcome_channel": None,
    "verification_channel": None,
    "verification_log_channel": None,
    "ticket_category": None,
    "ticket_panel_channel": None,
    "ticket_log_channel": None,
    "ticket_role_general": None,
    "ticket_role_hwid": None,
    "ticket_role_key_not_received": None,
    "store_url": DEFAULT_STORE_URL,
    "announce_image": None,
    "announce_footer": "moealturej",
    "stats_category": None,
    "stats_channels": {"members": None, "humans": None, "bots": None, "boosts": None},
    "stats_name_members": "👥 Members: {count}",
    "stats_name_humans": "🧑 Humans: {count}",
    "stats_name_bots": "🤖 Bots: {count}",
    "stats_name_boosts": "🚀 Boosts: {count}",
    "open_tickets": {},
    "oauth_verify_join_enabled": True,
    "welcome_message": "Welcome {mention} to **{server}**. Please verify if required and open a ticket if you need support.",
    "welcome_enabled": True,
    "moderation_log_channel": None,
    "command_log_channel": None,
    "default_ticket_name": "ticket-{username}-{short_id}",
    # Brand and presentation
    "brand_name": "moealturej",
    "brand_color": "7C3AED",
    "brand_footer": "moealturej • Professional server tools",
    "brand_icon_url": "",
    # Verification customization and safety
    "verification_title": "Verify Access",
    "verification_description": "Confirm your Discord account to unlock the server. The secure link expires after 10 minutes.",
    "verification_button_label": "Verify with Discord",
    "verification_success_message": "You are verified and now have access to the server.",
    "verification_min_account_days": 3,
    "verification_require_member": False,
    # Welcome customization
    "welcome_title": "Welcome to {server}",
    "welcome_ping_user": False,
    # Ticket customization
    "ticket_panel_title": "Support Center",
    "ticket_panel_description": "Choose the category that best matches your request. You will be asked for a short summary before the private ticket opens.",
    "ticket_open_message": "Thanks for contacting support. A team member will be with you shortly.",
    "ticket_label_general": "General support",
    "ticket_description_general": "Questions, account help, and general assistance.",
    "ticket_label_hwid": "Key HWID reset",
    "ticket_description_hwid": "Request a hardware ID reset for a purchased key.",
    "ticket_label_key_not_received": "Key not received",
    "ticket_description_key_not_received": "Get help with a missing or delayed key delivery.",
    # Feature gates and command customization
    "feature_utilities": True,
    "feature_admin_commands": True,
    "feature_verification": True,
    "feature_welcome": True,
    "feature_tickets": True,
    "feature_moderation": True,
    "feature_stats": True,
    "feature_announcements": True,
    "feature_dms": True,
    "command_enabled": dict(DEFAULT_COMMAND_ENABLED),
    # Public command copy
    "help_title": "Command Center",
    "help_description": "Everything you need, organized in one place.",
    "ping_title": "System Online",
    "ping_description": "Discord latency: `{latency_ms}ms`",
    "store_title": "moealturej Store",
    "store_description": "Browse products, downloads, and account tools securely.",
    "store_button_label": "Open Store",
    "moderation_dm_warn": True,
    "moderation_dm_timeout": True,
}

# =========================
# DISCORD / DB BOOT
# =========================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True  # Needed only to build ticket transcripts.

bot = commands.Bot(command_prefix="!", intents=intents)
web_runner: Optional[web.AppRunner] = None
mongo_client: Optional[AsyncIOMotorClient] = None
http_session: Optional[ClientSession] = None
mdb = None
views_added = False
commands_synced = False
startup_blocked_until: Optional[datetime] = None
last_startup_error: Optional[str] = None
CONFIG_CACHE: dict[int, tuple[float, Dict[str, Any]]] = {}
OWNER_SETTINGS_CACHE: Optional[tuple[float, Dict[str, Any]]] = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat()


async def init_mongo() -> None:
    global mongo_client, mdb
    if mdb is not None:
        return
    mongo_client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=8000)
    mdb = mongo_client[MONGO_DB_NAME]
    await mdb.command("ping")
    await mdb.guild_configs.create_index("guild_id", unique=True)
    await mdb.oauth_states.create_index("expires_at", expireAfterSeconds=0)
    await mdb.sessions.create_index("expires_at", expireAfterSeconds=0)
    await mdb.ticket_events.create_index([("guild_id", 1), ("created_at", -1)])
    await mdb.verification_events.create_index([("guild_id", 1), ("created_at", -1)])
    await mdb.dashboard_events.create_index([("guild_id", 1), ("created_at", -1)])
    await mdb.moderation_events.create_index([("guild_id", 1), ("created_at", -1)])
    await mdb.warnings.create_index([("guild_id", 1), ("user_id", 1), ("created_at", -1)])
    await mdb.error_events.create_index([("created_at", -1)])
    await mdb.error_events.create_index([("guild_id", 1), ("created_at", -1)])
    await mdb.verified_members.create_index([("guild_id", 1), ("user_id", 1)], unique=True)
    await mdb.owner_settings.create_index("key", unique=True)


async def get_guild_config(guild_id: int) -> Dict[str, Any]:
    guild_id = int(guild_id)
    now = asyncio.get_running_loop().time()
    cached = CONFIG_CACHE.get(guild_id)
    if cached and now - cached[0] < CONFIG_CACHE_SECONDS:
        return dict(cached[1])
    existing = await mdb.guild_configs.find_one({"guild_id": guild_id}, {"_id": 0})
    if not existing:
        defaults = dict(DEFAULT_GUILD_CONFIG)
        defaults["stats_channels"] = dict(DEFAULT_GUILD_CONFIG["stats_channels"])
        defaults["command_enabled"] = dict(DEFAULT_COMMAND_ENABLED)
        try:
            owner_defaults = await get_owner_settings()
            defaults.update({
                "store_url": owner_defaults.get("default_store_url") or DEFAULT_STORE_URL,
                "brand_name": owner_defaults.get("default_brand_name") or "moealturej",
                "brand_color": owner_defaults.get("default_brand_color") or "A855F7",
                "brand_footer": owner_defaults.get("default_brand_footer") or DEFAULT_GUILD_CONFIG["brand_footer"],
                "brand_icon_url": owner_defaults.get("default_brand_icon_url") or "",
            })
        except Exception:
            pass
        doc = {"guild_id": int(guild_id), **defaults, "created_at": now_iso(), "updated_at": now_iso()}
        await mdb.guild_configs.insert_one(doc)
        clean = {k: v for k, v in doc.items() if k != "_id"}
        CONFIG_CACHE[guild_id] = (now, clean)
        return dict(clean)

    update: Dict[str, Any] = {}
    unset: Dict[str, str] = {}
    for key, value in DEFAULT_GUILD_CONFIG.items():
        if key not in existing:
            update[key] = value
    if "stats_channels" in existing:
        for key, value in DEFAULT_GUILD_CONFIG["stats_channels"].items():
            if key not in existing.get("stats_channels", {}):
                update[f"stats_channels.{key}"] = value
    if "command_enabled" in existing:
        command_state = existing.get("command_enabled", {}) or {}
        for key, value in DEFAULT_COMMAND_ENABLED.items():
            if key not in command_state:
                update[f"command_enabled.{key}"] = value
        for key in command_state:
            if key not in DEFAULT_COMMAND_ENABLED:
                unset[f"command_enabled.{key}"] = ""

    allowed_root_keys = set(DEFAULT_GUILD_CONFIG) | {"guild_id", "created_at", "updated_at"}
    for key in existing:
        if key not in allowed_root_keys:
            unset[key] = ""

    if update or unset:
        update["updated_at"] = now_iso()
        operation: Dict[str, Any] = {"$set": update}
        if unset:
            operation["$unset"] = unset
        await mdb.guild_configs.update_one({"guild_id": int(guild_id)}, operation)
        existing = await mdb.guild_configs.find_one({"guild_id": int(guild_id)}, {"_id": 0})
    CONFIG_CACHE[guild_id] = (now, existing)
    return dict(existing)


async def set_config(guild_id: int, updates: Dict[str, Any]) -> None:
    await get_guild_config(guild_id)
    updates["updated_at"] = now_iso()
    await mdb.guild_configs.update_one({"guild_id": int(guild_id)}, {"$set": updates}, upsert=True)
    CONFIG_CACHE.pop(int(guild_id), None)


async def get_owner_settings() -> Dict[str, Any]:
    global OWNER_SETTINGS_CACHE
    now = asyncio.get_running_loop().time()
    if OWNER_SETTINGS_CACHE and now - OWNER_SETTINGS_CACHE[0] < CONFIG_CACHE_SECONDS:
        return dict(OWNER_SETTINGS_CACHE[1])
    existing = await mdb.owner_settings.find_one({"key": "global"}, {"_id": 0})
    if not existing:
        doc = dict(DEFAULT_OWNER_SETTINGS)
        doc["presence_statuses"] = list(DEFAULT_OWNER_SETTINGS["presence_statuses"])
        doc["updated_at"] = now_iso()
        await mdb.owner_settings.insert_one(doc)
        existing = {k: v for k, v in doc.items() if k != "_id"}
    else:
        missing = {k: v for k, v in DEFAULT_OWNER_SETTINGS.items() if k not in existing}
        allowed_owner_keys = set(DEFAULT_OWNER_SETTINGS) | {"key", "created_at", "updated_at"}
        stale = {k: "" for k in existing if k not in allowed_owner_keys}
        if missing or stale:
            missing["updated_at"] = now_iso()
            operation: Dict[str, Any] = {"$set": missing}
            if stale:
                operation["$unset"] = stale
            await mdb.owner_settings.update_one({"key": "global"}, operation)
            existing = await mdb.owner_settings.find_one({"key": "global"}, {"_id": 0})
    OWNER_SETTINGS_CACHE = (now, existing)
    return dict(existing)


async def set_owner_settings(updates: Dict[str, Any]) -> None:
    global OWNER_SETTINGS_CACHE
    updates = dict(updates)
    updates["updated_at"] = now_iso()
    await mdb.owner_settings.update_one({"key": "global"}, {"$set": updates, "$setOnInsert": {"key": "global"}}, upsert=True)
    OWNER_SETTINGS_CACHE = None


def command_feature_name(command_name: str) -> Optional[str]:
    meta = COMMAND_CATALOG.get(command_name or "")
    return meta.get("feature") if meta else None


async def command_is_available(interaction: discord.Interaction, config: Dict[str, Any]) -> bool:
    if is_owner_user(interaction.user.id):
        return True
    owner = await get_owner_settings()
    if owner.get("global_pause"):
        await safe_interaction_send(interaction, str(owner.get("global_pause_message") or "Bot features are temporarily paused."), ephemeral=True)
        return False
    command_name = getattr(getattr(interaction, "command", None), "name", "") or ""
    feature = command_feature_name(command_name)
    if feature and config.get(feature) is False:
        await safe_interaction_send(interaction, "That module is disabled in this server.", ephemeral=True)
        return False
    enabled_map = config.get("command_enabled") or {}
    if command_name and enabled_map.get(command_name, True) is False:
        await safe_interaction_send(interaction, "That command is disabled in this server.", ephemeral=True)
        return False
    return True


async def add_open_ticket(guild_id: int, user_id: int, channel_id: int, ticket_type: str) -> None:
    await set_config(guild_id, {f"open_tickets.{user_id}": {"channel_id": int(channel_id), "type": ticket_type, "opened_at": now_iso()}})


async def remove_open_ticket(guild_id: int, user_id: int) -> None:
    await mdb.guild_configs.update_one({"guild_id": int(guild_id)}, {"$unset": {f"open_tickets.{user_id}": ""}, "$set": {"updated_at": now_iso()}})


async def save_event(collection: str, payload: Dict[str, Any]) -> None:
    payload.setdefault("created_at", now_iso())
    await mdb[collection].insert_one(payload)


async def report_exception(context: str, exc: BaseException, *, guild_id: Optional[int] = None, user_id: Optional[int] = None, details: Optional[Dict[str, Any]] = None) -> str:
    """Log an internal failure with a user-safe incident ID."""
    incident_id = secrets.token_hex(4).upper()
    log.error("Incident %s in %s: %s", incident_id, context, exc, exc_info=(type(exc), exc, exc.__traceback__))
    payload = {
        "incident_id": incident_id,
        "context": context,
        "error_type": type(exc).__name__,
        "message": str(exc)[:1000],
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-12000:],
        "guild_id": int(guild_id) if guild_id else None,
        "user_id": int(user_id) if user_id else None,
        "details": details or {},
        "created_at": utcnow(),
    }
    try:
        if mdb is not None:
            await mdb.error_events.insert_one(payload)
    except Exception:
        log.exception("Could not persist incident %s", incident_id)
    return incident_id

# =========================
# AUTH / ACCESS HELPERS
# =========================
def sign_value(value: str) -> str:
    sig = hmac.new(DASHBOARD_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{sig}"


def unsign_value(signed: str) -> Optional[str]:
    try:
        value, sig = signed.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(DASHBOARD_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()
    return value if hmac.compare_digest(sig, expected) else None


def is_owner_user(user_id: int) -> bool:
    return int(user_id) == OWNER_USER_ID


async def get_dashboard_user(request: web.Request) -> Optional[Dict[str, Any]]:
    raw = request.cookies.get("moe_session")
    if not raw:
        return None
    session_id = unsign_value(raw)
    if not session_id:
        return None
    session = await mdb.sessions.find_one({"session_id": session_id, "expires_at": {"$gt": utcnow()}}, {"_id": 0})
    return session


async def _discord_rest_request(method: str, url: str, *, route: str, **kwargs) -> tuple[int, Any]:
    async def op():
        global http_session
        if http_session is None or http_session.closed:
            http_session = ClientSession(timeout=ClientTimeout(total=25))
        async with http_session.request(method, url, **kwargs) as resp:
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = await resp.text()
            if resp.status == 429:
                retry_after = 0.0
                if isinstance(body, dict):
                    retry_after = float(body.get("retry_after") or 0)
                if isinstance(body, dict) and body.get("global"):
                    rate_limiter.block_global(retry_after + 5)
                raise discord.HTTPException(resp, body)
            return resp.status, body
    result = await discord_guarded(f"REST {method} {route}", f"rest:{route}", op, min_gap=DISCORD_API_MIN_GAP, default=None)
    if result is None:
        raise web.HTTPTooManyRequests(text="Discord is rate limiting requests. Try again shortly.")
    return result


async def exchange_code(code: str, redirect_uri: str) -> Dict[str, Any]:
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    status, body = await _discord_rest_request("POST", "https://discord.com/api/oauth2/token", route="oauth_token", data=data, headers=headers)
    if status >= 400:
        raise web.HTTPBadRequest(text=f"Discord OAuth failed: {body}")
    return body


async def discord_get(path: str, token: str) -> Any:
    status, body = await _discord_rest_request("GET", f"https://discord.com/api{path}", route=f"get:{path}", headers={"Authorization": f"Bearer {token}"})
    if status >= 400:
        raise web.HTTPBadRequest(text=f"Discord API failed: {body}")
    return body


async def discord_put(path: str, token: str, payload: Dict[str, Any]) -> tuple[int, Any]:
    return await _discord_rest_request("PUT", f"https://discord.com/api{path}", route=f"put:{path}", headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"}, json=payload)


def guild_manageable(user_guild: Dict[str, Any]) -> bool:
    permissions = int(user_guild.get("permissions", "0"))
    manage_guild = bool(permissions & 0x20)
    administrator = bool(permissions & 0x8)
    return manage_guild or administrator or bool(user_guild.get("owner"))


async def dashboard_can_access(user: Dict[str, Any], guild_id: int) -> bool:
    if is_owner_user(int(user["user_id"])):
        return True
    if DASHBOARD_OWNER_ONLY:
        return False
    for user_guild in user.get("guilds", []):
        if int(user_guild.get("id", 0)) == int(guild_id) and guild_manageable(user_guild):
            return True
    return False


def member_is_command_admin(member: discord.Member, config: Dict[str, Any]) -> bool:
    if is_owner_user(member.id):
        return True
    if member.id == member.guild.owner_id:
        return True
    admin_role_id = config.get("bot_admin_role")
    if admin_role_id and any(role.id == int(admin_role_id) for role in member.roles):
        return True
    return admin_role_id is None and member.guild_permissions.manage_guild


def owner_private_message() -> str:
    cached_owner = OWNER_SETTINGS_CACHE[1] if OWNER_SETTINGS_CACHE else DEFAULT_OWNER_SETTINGS
    contact = str(cached_owner.get("owner_contact") or OWNER_CONTACT).strip()[:300]
    return f"This bot is not for public use. {contact}"


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await safe_interaction_send(interaction, "This command only works in a server.", ephemeral=True)
            return False
        config = await get_guild_config(interaction.guild.id)
        if not member_is_command_admin(interaction.user, config):
            await safe_interaction_send(interaction, owner_private_message(), ephemeral=True)
            return False
        return await command_is_available(interaction, config)
    return app_commands.check(predicate)


def guild_enabled_or_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return True
        if is_owner_user(interaction.user.id):
            return True
        config = await get_guild_config(interaction.guild.id)
        if config.get("enabled"):
            return await command_is_available(interaction, config)
        await safe_interaction_send(interaction, owner_private_message(), ephemeral=True)
        return False
    return app_commands.check(predicate)

# =========================
# UI HELPERS
# =========================
def make_embed(title: str, description: str, color: int = EMBED_COLOR) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color, timestamp=utcnow())


def is_http_url(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        parsed = urlparse(raw)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def parse_color_value(value: Any, fallback: int = EMBED_COLOR) -> int:
    raw = str(value or "").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    try:
        return int(raw, 16) & 0xFFFFFF if raw else fallback
    except ValueError:
        return fallback


def make_branded_embed(config: Dict[str, Any], title: str, description: str, color: Optional[int] = None) -> discord.Embed:
    embed = make_embed(title, description, color if color is not None else parse_color_value(config.get("brand_color")))
    footer = str(config.get("brand_footer") or config.get("brand_name") or "moealturej")[:2048]
    icon_url = str(config.get("brand_icon_url") or "").strip()
    embed.set_footer(text=footer, icon_url=icon_url if is_http_url(icon_url) else None)
    return embed


def render_template(template: str, *, guild: discord.Guild, member: Optional[discord.Member] = None, extra: Optional[Dict[str, str]] = None) -> str:
    values = {
        "server": guild.name,
        "server_id": str(guild.id),
        "member_count": str(guild.member_count or len(guild.members)),
        "mention": member.mention if member else "",
        "username": member.display_name if member else "",
        "user_id": str(member.id) if member else "",
    }
    values.update(extra or {})
    rendered = str(template)
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def render_stat_name(config: Dict[str, Any], key: str, count: int, guild: discord.Guild) -> str:
    fallback = str(DEFAULT_GUILD_CONFIG.get(f"stats_name_{key}") or f"{key.title()}: {{count}}")
    template = str(config.get(f"stats_name_{key}") or fallback)
    value = template.replace("{count}", str(count)).replace("{server}", guild.name)
    return value[:100] or fallback.replace("{count}", str(count))[:100]


def ticket_type_info(config: Dict[str, Any], key: str) -> Dict[str, Any]:
    base = dict(TICKET_TYPES[key])
    base["label"] = str(config.get(f"ticket_label_{key}") or base["label"])[:100]
    base["description"] = str(config.get(f"ticket_description_{key}") or base["description"])[:100]
    return base


def clean_channel_name(text: str) -> str:
    allowed = string.ascii_lowercase + string.digits + "-"
    text = text.lower().replace(" ", "-")
    return "".join(c for c in text if c in allowed)[:80] or "ticket"


def clean_ticket_template(text: str) -> str:
    value = str(text or DEFAULT_GUILD_CONFIG["default_ticket_name"]).lower().replace(" ", "-")
    allowed = set(string.ascii_lowercase + string.digits + "-_{}")
    value = "".join(ch for ch in value if ch in allowed)[:90]
    for placeholder in ("{username}", "{type}", "{short_id}"):
        value = value.replace(placeholder.replace("_", "-"), placeholder)
    return value or DEFAULT_GUILD_CONFIG["default_ticket_name"]


async def safe_add_role(member: discord.Member, role_id: Optional[int], reason: str) -> bool:
    if not role_id:
        return False
    role = member.guild.get_role(int(role_id))
    if not role:
        return False
    if role in member.roles:
        return True
    if not member.guild.me.guild_permissions.manage_roles or role >= member.guild.me.top_role:
        log.warning("Cannot add role %s in %s due to permissions/hierarchy", role.name, member.guild.name)
        return False

    async def op():
        await member.add_roles(role, reason=reason)
        return True

    return bool(await discord_guarded(f"add role {role.id} to {member.id}", f"role:{member.guild.id}", op, min_gap=DISCORD_ROLE_MIN_GAP, default=False))


async def safe_remove_role(member: discord.Member, role_id: Optional[int], reason: str) -> bool:
    if not role_id:
        return False
    role = member.guild.get_role(int(role_id))
    if not role or role not in member.roles:
        return False
    if not member.guild.me.guild_permissions.manage_roles or role >= member.guild.me.top_role:
        log.warning("Cannot remove role %s in %s due to permissions/hierarchy", role.name, member.guild.name)
        return False

    async def op():
        await member.remove_roles(role, reason=reason)
        return True

    return bool(await discord_guarded(f"remove role {role.id} from {member.id}", f"role:{member.guild.id}", op, min_gap=DISCORD_ROLE_MIN_GAP, default=False))


async def send_verified_dm(member: discord.Member, config: Dict[str, Any]) -> None:
    success_message = str(config.get("verification_success_message") or DEFAULT_GUILD_CONFIG["verification_success_message"])
    embed = make_branded_embed(config, "Verified successfully", f"{success_message}\n\n**Server:** {member.guild.name}", SUCCESS_COLOR)
    store_url = str(config.get("store_url") or DEFAULT_STORE_URL)
    if store_url:
        embed.add_field(name="Store", value=store_url, inline=False)
    embed.set_thumbnail(url=member.guild.icon.url if member.guild.icon else member.display_avatar.url)
    await safe_user_send(member, embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def log_verification(guild: discord.Guild, user: discord.abc.User, method: str, status: str, details: str = "") -> None:
    config = await get_guild_config(guild.id)
    await save_event("verification_events", {
        "guild_id": guild.id,
        "user_id": user.id,
        "username": str(user),
        "method": method,
        "status": status,
        "details": details,
    })
    channel = guild.get_channel(config.get("verification_log_channel") or 0)
    if isinstance(channel, discord.TextChannel):
        status_color = SUCCESS_COLOR if status == "success" else (INFO_COLOR if status == "already_verified" else ERROR_COLOR)
        embed = make_branded_embed(config, "Verification Log", f"**User:** {user.mention if hasattr(user, 'mention') else user}\n**Method:** {method}\n**Status:** {status}\n{details}", status_color)
        await safe_channel_send(channel, embed=embed)


async def build_ticket_transcript(channel: discord.TextChannel) -> tuple[str, bytes]:
    """Render a portable, escaped ticket transcript using the dashboard visual language."""
    config = await get_guild_config(channel.guild.id)
    brand_name = html.escape(str(config.get("brand_name") or "moealturej")[:60])
    accent = str(config.get("brand_color") or "A855F7").strip().lstrip("#")
    if len(accent) not in {3, 6} or any(ch not in string.hexdigits for ch in accent):
        accent = "A855F7"
    messages: list[str] = []
    message_count = 0
    attachment_count = 0
    async for msg in channel.history(limit=None, oldest_first=True):
        message_count += 1
        author = html.escape(str(msg.author))
        display_name = html.escape(getattr(msg.author, "display_name", str(msg.author)))
        content = html.escape(msg.content or "")
        created = msg.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        avatar = html.escape(str(getattr(getattr(msg.author, "display_avatar", None), "url", "")))
        parts = ["<article class='message'>", "<div class='message-head'>"]
        if avatar:
            parts.append(f"<img class='avatar' src='{avatar}' alt=''>")
        parts.append(f"<div><strong>{display_name}</strong><div class='meta'>{author} • {created}</div></div></div>")
        if content:
            parts.append(f"<div class='content'>{content}</div>")
        for emb in msg.embeds:
            title = html.escape(emb.title or "Embed")
            desc = html.escape(emb.description or "")
            parts.append(f"<div class='embed'><b>{title}</b>{('<div>'+desc+'</div>') if desc else ''}</div>")
        if msg.attachments:
            attachment_count += len(msg.attachments)
            links = "".join(
                f"<a class='attachment' href='{html.escape(a.url)}' rel='noreferrer noopener'>↗ {html.escape(a.filename)}</a>"
                for a in msg.attachments
            )
            parts.append(f"<div class='attachments'>{links}</div>")
        parts.append("</article>")
        messages.append("".join(parts))

    guild_name = html.escape(channel.guild.name)
    channel_name = html.escape(channel.name)
    generated = utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    document = f"""<!doctype html>
<html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Transcript #{channel_name}</title>
<style>
:root{{--bg:#050508;--panel:#0b0a10;--panel2:#111019;--line:#24212e;--soft:#aaa3b5;--text:#f7f4fb;--accent:#{accent};--accent2:#{accent}}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at 50% -20%,rgba(168,85,247,.14),transparent 32%),var(--bg);color:var(--text);font:14px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
a{{color:var(--accent2)}}.wrap{{width:min(1040px,calc(100% - 32px));margin:0 auto;padding:34px 0 64px}}
.top{{display:flex;justify-content:space-between;align-items:center;gap:16px;border-bottom:1px solid var(--line);padding-bottom:20px;margin-bottom:24px}}.brand{{font-weight:850;letter-spacing:-.02em}}.dot{{display:inline-block;width:8px;height:8px;background:var(--accent);border-radius:999px;box-shadow:0 0 20px var(--accent);margin-right:8px}}
.hero{{padding:28px;border:1px solid var(--line);border-radius:24px;background:linear-gradient(135deg,#0a0910,#100b17);margin-bottom:18px}}.eyebrow{{font-size:11px;font-weight:800;letter-spacing:.18em;text-transform:uppercase;color:#c8b6d7}}h1{{font-size:clamp(30px,5vw,52px);line-height:1;margin:10px 0 12px;letter-spacing:-.05em}}.muted,.meta{{color:var(--soft)}}
.stats{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:22px}}.stat{{padding:14px 16px;background:#0d0b13;border:1px solid var(--line);border-radius:14px}}.stat b{{display:block;font-size:18px;margin-top:3px}}
.feed{{border:1px solid var(--line);border-radius:22px;overflow:hidden;background:var(--panel)}}.message{{padding:18px 20px;border-bottom:1px solid var(--line)}}.message:last-child{{border-bottom:0}}.message-head{{display:flex;align-items:center;gap:11px}}.avatar{{width:38px;height:38px;border-radius:12px;object-fit:cover;background:#17131f}}.meta{{font-size:12px;margin-top:1px}}.content{{white-space:pre-wrap;overflow-wrap:anywhere;margin:12px 0 0 49px;color:#ebe7f0}}.embed{{margin:12px 0 0 49px;padding:12px 14px;border-left:3px solid var(--accent);background:var(--panel2);border-radius:0 12px 12px 0;white-space:pre-wrap}}.attachments{{margin:12px 0 0 49px;display:flex;flex-wrap:wrap;gap:8px}}.attachment{{text-decoration:none;padding:8px 10px;border:1px solid var(--line);background:#14111a;border-radius:10px}}
.footer{{margin-top:18px;text-align:center;color:var(--soft);font-size:12px}}@media(max-width:640px){{.stats{{grid-template-columns:1fr}}.content,.embed,.attachments{{margin-left:0}}.top{{align-items:flex-start;flex-direction:column}}}}
</style></head><body><main class='wrap'>
<div class='top'><div class='brand'><span class='dot'></span>{brand_name} Support</div><div class='muted'>Read-only ticket archive</div></div>
<section class='hero'><span class='eyebrow'>Support transcript</span><h1>#{channel_name}</h1><p class='muted'>A portable record from <strong>{guild_name}</strong>. Times are shown in UTC.</p><div class='stats'><div class='stat'><span class='muted'>Messages</span><b>{message_count}</b></div><div class='stat'><span class='muted'>Attachments</span><b>{attachment_count}</b></div><div class='stat'><span class='muted'>Generated</span><b style='font-size:13px'>{generated}</b></div></div></section>
<section class='feed'>{''.join(messages) if messages else "<div class='message muted'>No messages were recorded in this ticket.</div>"}</section>
<div class='footer'>Generated by {brand_name} Bot Control • Transcript content is escaped before rendering.</div>
</main></body></html>"""
    filename = f"transcript-{channel.guild.id}-{channel.id}.html"
    return filename, document.encode("utf-8")

# =========================
# VERIFICATION / TICKET VIEWS
# =========================
TICKET_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}


def _ticket_lock(guild_id: int, user_id: int) -> asyncio.Lock:
    key = (int(guild_id), int(user_id))
    lock = TICKET_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        TICKET_LOCKS[key] = lock
    return lock


def ticket_topic_value(topic: str, key: str) -> Optional[str]:
    for part in (topic or "").split():
        if part.startswith(f"{key}="):
            return part.split("=", 1)[1]
    return None


def member_can_manage_ticket(member: discord.Member, config: Dict[str, Any], ticket_type: str, owner_id: Optional[int] = None) -> bool:
    if member.guild_permissions.manage_channels or member_is_command_admin(member, config):
        return True
    if owner_id and member.id == owner_id:
        return True
    info = TICKET_TYPES.get(ticket_type)
    role_id = config.get(info["support_role_key"]) if info else None
    return bool(role_id and any(role.id == int(role_id) for role in member.roles))


class OAuthVerifyView(discord.ui.View):
    def __init__(self, guild_id: int, *, label: str = "Verify with Discord"):
        super().__init__(timeout=None)
        self.guild_id = int(guild_id)
        self.verify_button.label = label[:80] or "Verify with Discord"

    @discord.ui.button(label="Verify with Discord", style=discord.ButtonStyle.success, emoji="✅", custom_id="moe_oauth_verify")
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await safe_interaction_send(interaction, "This verification button only works inside the server.", ephemeral=True)
        if rate_limiter.on_cooldown(f"verify_click:{interaction.guild.id}:{interaction.user.id}", VERIFY_CLICK_COOLDOWN_SECONDS):
            return await safe_interaction_send(interaction, "Please wait a moment before requesting another verification link.", ephemeral=True)

        config = await get_guild_config(interaction.guild.id)
        if config.get("feature_verification") is False and not is_owner_user(interaction.user.id):
            return await safe_interaction_send(interaction, "Verification is currently disabled in this server.", ephemeral=True)
        verified_role_id = config.get("verified_role")
        verified_role = interaction.guild.get_role(int(verified_role_id or 0)) if verified_role_id else None
        if not verified_role:
            return await safe_interaction_send(interaction, "Verification is not fully configured yet. An administrator needs to select a verified role.", ephemeral=True)
        if not interaction.guild.me.guild_permissions.manage_roles or verified_role >= interaction.guild.me.top_role:
            return await safe_interaction_send(interaction, "I cannot assign the verified role. Move my bot role above it and give me **Manage Roles**.", ephemeral=True)
        if verified_role in interaction.user.roles:
            removed = await safe_remove_role(interaction.user, config.get("unverified_role"), "Already verified cleanup")
            extra = " I also removed your unverified role." if removed else ""
            await log_verification(interaction.guild, interaction.user, "panel-check", "already_verified", "User clicked verify but already had verified role." + extra)
            return await safe_interaction_send(interaction, f"✅ You are already verified in **{interaction.guild.name}**.{extra}", ephemeral=True)

        url = f"{PUBLIC_BASE_URL}/verify/start?guild_id={interaction.guild.id}&user_id={interaction.user.id}"
        view = discord.ui.View(timeout=180)
        view.add_item(discord.ui.Button(label="Open secure verification", style=discord.ButtonStyle.link, emoji="🔐", url=url))
        account_days = max(0, int(config.get("verification_min_account_days", 0)))
        requirement = f" Accounts must be at least **{account_days} day(s)** old." if account_days else ""
        await safe_interaction_send(interaction, f"Open the private OAuth2 link below. It expires in 10 minutes and only works for your Discord account.{requirement}", view=view, ephemeral=True)


async def create_ticket(interaction: discord.Interaction, ticket_key: str, subject: str, details: str) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await safe_interaction_send(interaction, "Tickets can only be opened inside a server.", ephemeral=True)
    if rate_limiter.on_cooldown(f"ticket_click:{interaction.guild.id}:{interaction.user.id}", TICKET_CLICK_COOLDOWN_SECONDS):
        return await safe_interaction_send(interaction, "Please wait before opening another ticket. This prevents duplicate channels.", ephemeral=True)

    await safe_interaction_defer(interaction, ephemeral=True)
    async with _ticket_lock(interaction.guild.id, interaction.user.id):
        config = await get_guild_config(interaction.guild.id)
        if config.get("feature_tickets") is False and not is_owner_user(interaction.user.id):
            return await safe_interaction_send(interaction, "Support tickets are currently disabled in this server.", ephemeral=True)
        existing = config.get("open_tickets", {}).get(str(interaction.user.id))
        if existing:
            channel_id = existing.get("channel_id") if isinstance(existing, dict) else existing
            channel = interaction.guild.get_channel(int(channel_id or 0))
            if channel:
                return await safe_interaction_send(interaction, f"You already have an open ticket: {channel.mention}", ephemeral=True)
            await remove_open_ticket(interaction.guild.id, interaction.user.id)

        category = interaction.guild.get_channel(config.get("ticket_category") or 0)
        if not isinstance(category, discord.CategoryChannel):
            return await safe_interaction_send(interaction, "The ticket category is not configured yet. Please contact an administrator.", ephemeral=True)

        ticket_info = ticket_type_info(config, ticket_key)
        support_role = interaction.guild.get_role(int(config.get(ticket_info["support_role_key"]) or 0))
        bot_member = interaction.guild.me
        if not bot_member or not bot_member.guild_permissions.manage_channels:
            return await safe_interaction_send(interaction, "I need **Manage Channels** before I can create private tickets.", ephemeral=True)

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True),
            bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True, read_message_history=True),
        }
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True)

        template = str(config.get("default_ticket_name") or DEFAULT_GUILD_CONFIG["default_ticket_name"])
        raw_name = render_template(template, guild=interaction.guild, member=interaction.user, extra={"type": ticket_key, "short_id": str(interaction.user.id)[-4:]})
        channel = await safe_create_text_channel(
            interaction.guild,
            name=clean_channel_name(raw_name),
            category=category,
            overwrites=overwrites,
            topic=f"owner_id={interaction.user.id} ticket_type={ticket_key} claimed_by=0",
            reason=f"{ticket_info['label']} ticket opened by {interaction.user}",
        )
        if not channel:
            return await safe_interaction_send(interaction, "Discord could not create the ticket. Check my channel permissions and try again.", ephemeral=True)

        await add_open_ticket(interaction.guild.id, interaction.user.id, channel.id, ticket_key)
        await save_event("ticket_events", {
            "guild_id": interaction.guild.id,
            "user_id": interaction.user.id,
            "channel_id": channel.id,
            "event": "opened",
            "ticket_type": ticket_key,
            "subject": subject[:100],
        })

        intro = str(config.get("ticket_open_message") or DEFAULT_GUILD_CONFIG["ticket_open_message"])
        embed = make_branded_embed(config, f"{ticket_info['emoji']} {ticket_info['label']}", intro)
        embed.add_field(name="Subject", value=subject[:256] or "No subject provided", inline=False)
        embed.add_field(name="Details", value=details[:1024] or "No additional details provided", inline=False)
        embed.add_field(name="Opened by", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
        mention_text = f"{interaction.user.mention} {support_role.mention if support_role else ''}".strip()
        allowed = discord.AllowedMentions(users=[interaction.user], roles=[support_role] if support_role else [], everyone=False)
        await safe_channel_send(channel, content=mention_text, embed=embed, view=CloseTicketView(), allowed_mentions=allowed)
        await safe_interaction_send(interaction, f"Your private ticket is ready: {channel.mention}", ephemeral=True)


class TicketDetailsModal(discord.ui.Modal, title="Open a support ticket"):
    subject = discord.ui.TextInput(label="Short subject", placeholder="What do you need help with?", max_length=100)
    details = discord.ui.TextInput(label="Details", placeholder="Include useful context, order IDs, errors, or steps already tried.", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, ticket_key: str):
        super().__init__(timeout=300)
        self.ticket_key = ticket_key

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await create_ticket(interaction, self.ticket_key, str(self.subject), str(self.details))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        incident = await report_exception("ticket_modal", error, guild_id=interaction.guild_id, user_id=interaction.user.id)
        await safe_interaction_send(interaction, f"The ticket could not be opened. Reference: `{incident}`", ephemeral=True)


class TicketSelect(discord.ui.Select):
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        config = config or DEFAULT_GUILD_CONFIG
        options = []
        for key in TICKET_TYPES:
            info = ticket_type_info(config, key)
            options.append(discord.SelectOption(label=info["label"], description=info["description"], emoji=info["emoji"], value=key))
        super().__init__(placeholder="Choose a support category…", min_values=1, max_values=1, options=options, custom_id="moe_ticket_select")

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await safe_interaction_send(interaction, "This only works inside a server.", ephemeral=True)
        await interaction.response.send_modal(TicketDetailsModal(self.values[0]))


class TicketPanelView(discord.ui.View):
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(timeout=None)
        self.add_item(TicketSelect(config))


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.secondary, emoji="🙋", custom_id="moe_claim_ticket")
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel) or not isinstance(interaction.user, discord.Member):
            return await safe_interaction_send(interaction, "This can only be used in a managed ticket.", ephemeral=True)
        topic = interaction.channel.topic or ""
        owner_raw = ticket_topic_value(topic, "owner_id")
        ticket_type = ticket_topic_value(topic, "ticket_type") or "unknown"
        owner_id = int(owner_raw) if owner_raw and owner_raw.isdigit() else None
        config = await get_guild_config(interaction.guild.id)
        if not member_can_manage_ticket(interaction.user, config, ticket_type) or interaction.user.id == owner_id:
            return await safe_interaction_send(interaction, "Only support staff can claim this ticket.", ephemeral=True)
        claimed_raw = ticket_topic_value(topic, "claimed_by")
        if claimed_raw and claimed_raw != "0":
            claimed = interaction.guild.get_member(int(claimed_raw)) if claimed_raw.isdigit() else None
            return await safe_interaction_send(interaction, f"This ticket is already claimed by {claimed.mention if claimed else '`'+claimed_raw+'`'}.", ephemeral=True)
        parts = [part for part in topic.split() if not part.startswith("claimed_by=")]
        parts.append(f"claimed_by={interaction.user.id}")
        await safe_channel_edit(interaction.channel, topic=" ".join(parts), reason=f"Ticket claimed by {interaction.user}")
        await save_event("ticket_events", {"guild_id": interaction.guild.id, "channel_id": interaction.channel.id, "event": "claimed", "claimed_by": interaction.user.id})
        await safe_channel_send(interaction.channel, embed=make_branded_embed(config, "Ticket claimed", f"{interaction.user.mention} is now handling this request.", INFO_COLOR))
        await safe_interaction_send(interaction, "Ticket claimed.", ephemeral=True)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="moe_close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel) or not isinstance(interaction.user, discord.Member):
            return await safe_interaction_send(interaction, "This can only be used inside a ticket channel.", ephemeral=True)
        topic = interaction.channel.topic or ""
        owner_raw = ticket_topic_value(topic, "owner_id")
        owner_id = int(owner_raw) if owner_raw and owner_raw.isdigit() else None
        ticket_type = ticket_topic_value(topic, "ticket_type") or "unknown"
        config = await get_guild_config(interaction.guild.id)
        if not member_can_manage_ticket(interaction.user, config, ticket_type, owner_id):
            return await safe_interaction_send(interaction, "You do not have permission to close this ticket.", ephemeral=True)

        await safe_interaction_defer(interaction, ephemeral=True)
        await safe_interaction_send(interaction, "Saving the transcript and closing this ticket…", ephemeral=True)
        try:
            filename, transcript = await build_ticket_transcript(interaction.channel)
        except Exception as exc:
            incident = await report_exception("ticket_transcript", exc, guild_id=interaction.guild.id, user_id=interaction.user.id, details={"channel_id": interaction.channel.id})
            return await safe_interaction_send(interaction, f"I did not close the ticket because its transcript could not be saved. Reference: `{incident}`", ephemeral=True)

        owner = interaction.guild.get_member(owner_id or 0)
        close_embed = make_branded_embed(config, "Ticket Closed", f"Ticket `{interaction.channel.name}` was closed by {interaction.user.mention}.", INFO_COLOR)
        close_embed.add_field(name="Type", value=ticket_type, inline=True)
        close_embed.add_field(name="Channel ID", value=str(interaction.channel.id), inline=True)
        if owner:
            await safe_user_send(owner, embed=close_embed, file=discord.File(io.BytesIO(transcript), filename=filename))

        log_channel = interaction.guild.get_channel(int(config.get("ticket_log_channel") or 0))
        if isinstance(log_channel, discord.TextChannel):
            await safe_channel_send(log_channel, embed=close_embed, file=discord.File(io.BytesIO(transcript), filename=filename), allowed_mentions=discord.AllowedMentions.none())

        await save_event("ticket_events", {"guild_id": interaction.guild.id, "user_id": owner_id, "channel_id": interaction.channel.id, "event": "closed", "ticket_type": ticket_type, "closed_by": interaction.user.id})
        if owner_id:
            await remove_open_ticket(interaction.guild.id, owner_id)
        await asyncio.sleep(1)
        await safe_channel_delete(interaction.channel, reason=f"Ticket closed by {interaction.user}")

# =========================
# WEB DASHBOARD
# =========================
def page(title: str, body: str) -> web.Response:
    cached_owner = OWNER_SETTINGS_CACHE[1] if OWNER_SETTINGS_CACHE else DEFAULT_OWNER_SETTINGS
    accent = str(cached_owner.get("dashboard_accent") or "A855F7").strip().lstrip("#")
    if len(accent) not in {3, 6} or any(ch not in string.hexdigits for ch in accent):
        accent = "A855F7"
    logo_url = str(cached_owner.get("dashboard_logo_url") or "").strip()
    if not is_http_url(logo_url):
        logo_url = ""
    dashboard_brand = str(cached_owner.get("default_brand_name") or "moealturej")[:60]
    dashboard_title = str(cached_owner.get("dashboard_title") or f"{dashboard_brand} Bot Control")[:80]
    website_url = str(cached_owner.get("default_store_url") or DEFAULT_STORE_URL)
    if not is_http_url(website_url):
        website_url = DEFAULT_STORE_URL
    css = """
    <style>
    :root{color-scheme:dark;--bg:#050507;--bg-soft:#09090d;--panel:#0d0d12;--panel2:#111118;--raised:#15151d;--line:#24242d;--line-soft:#191920;--text:#f7f4fb;--soft:#d9d3df;--muted:#8f8799;--purple:#a855f7;--purple2:#c261ff;--purple-dim:rgba(168,85,247,.13);--green:#35d99a;--yellow:#f7c65c;--red:#fb7185;--blue:#60a5fa;--shadow:0 24px 70px rgba(0,0,0,.38)}
    *{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;min-height:100vh;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}body:before{content:"";position:fixed;z-index:100;top:0;right:0;width:3px;height:100vh;background:linear-gradient(180deg,var(--purple2),#7c3aed 50%,transparent);pointer-events:none}a{color:inherit;text-decoration:none}.shell{width:min(1260px,calc(100% - 36px));margin:0 auto}.topbar-wrap{position:sticky;top:0;z-index:50;background:rgba(5,5,7,.91);backdrop-filter:blur(18px);border-bottom:1px solid #17171d}.topbar{height:76px;display:flex;align-items:center;justify-content:space-between;gap:22px}.brand{display:flex;align-items:center;gap:11px;font-size:15px;font-weight:900;letter-spacing:-.03em;white-space:nowrap}.brandmark{width:38px;height:38px;display:grid;place-items:center;border-radius:11px;background:radial-gradient(circle at 35% 25%,#d486ff,#8b2de2 48%,#250443 100%);border:1px solid #5f228d;box-shadow:inset 0 0 18px rgba(255,255,255,.11),0 0 24px rgba(168,85,247,.13);font-size:16px}.brand small{color:var(--muted);font-size:10px;letter-spacing:.12em;text-transform:uppercase}.navlinks{display:flex;align-items:center;gap:4px;background:#0b0b10;border:1px solid #16161d;padding:5px;border-radius:13px}.navlinks a{padding:9px 13px;color:#98919f;font-size:13px;font-weight:800;border-radius:9px;transition:.16s ease}.navlinks a:hover{color:#fff;background:#14141b}.navlinks a[href='/owner']{color:#d8b4fe}.top-actions{display:flex;align-items:center;gap:8px}.iconbtn,.adminbtn{min-height:38px;display:inline-flex;align-items:center;justify-content:center;border:1px solid #22222a;background:#101015;border-radius:10px;padding:0 12px;color:#d9d3df;font-size:12px;font-weight:850}.adminbtn{background:#f5f3f7;color:#08080a;border-color:#fff}.content{padding:26px 0 64px}.hero{position:relative;border:1px solid var(--line);background:linear-gradient(135deg,#09090e,#0d0a12 62%,#130a1c);border-radius:22px;padding:36px;overflow:hidden;box-shadow:var(--shadow)}.hero:after{content:"";position:absolute;right:-100px;top:-140px;width:380px;height:380px;border-radius:50%;background:radial-gradient(circle,rgba(168,85,247,.20),transparent 65%);pointer-events:none}.compact-hero{padding:30px}.eyebrow,.pill{display:inline-flex;align-items:center;gap:8px;color:#c8a7df;font-size:10px;text-transform:uppercase;letter-spacing:.15em;font-weight:900}.eyebrow:before,.pill:before{content:"";width:7px;height:7px;border-radius:50%;background:var(--purple2);box-shadow:0 0 16px var(--purple)}h1{font-size:clamp(36px,5vw,62px);line-height:.98;letter-spacing:-.065em;margin:15px 0 14px;max-width:850px}h2{font-size:28px;letter-spacing:-.045em;margin:0}h3{font-size:18px;letter-spacing:-.03em;margin:0 0 8px}.muted{color:var(--muted);line-height:1.65}.soft{color:var(--soft)}.hero .muted{max-width:760px;font-size:15px}.hero-split{display:grid;grid-template-columns:1.55fr .9fr;padding:0}.hero-main{padding:56px 58px}.hero-side{padding:24px 28px;border-left:1px solid var(--line);background:rgba(255,255,255,.015);display:flex;flex-direction:column;justify-content:center}.hero-side h4{margin:0 0 8px;color:#8f8799;font-size:10px;letter-spacing:.14em;text-transform:uppercase}.feature-row{display:grid;grid-template-columns:34px 1fr;gap:13px;padding:16px 0;border-top:1px solid var(--line-soft)}.feature-row:first-of-type{border-top:0}.feature-icon{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;background:var(--purple-dim);color:#c06bff;font-weight:900}.feature-row b{font-size:13px}.feature-row span{display:block;margin-top:3px;color:#7e7687;font-size:11px;line-height:1.45}.toolbar,.dashboard-actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:18px}.btn,button{appearance:none;border:1px solid transparent;background:#f6f4f7;color:#070709;border-radius:11px;min-height:42px;padding:0 16px;font:inherit;font-size:12px;font-weight:900;display:inline-flex;align-items:center;justify-content:center;gap:8px;cursor:pointer;transition:.15s ease}.btn:hover,button:hover{transform:translateY(-1px);filter:brightness(1.04)}.btn.primary,button.primary{background:linear-gradient(135deg,#9d41ea,#b653ff);color:white;border-color:#bd72ff}.btn.secondary{background:#111117;color:#ddd7e3;border-color:#282830}.btn.ghost{background:transparent;color:#bdb4c4;border-color:#22222a}.btn.danger{background:#241115;color:#ff9aaa;border-color:#4d1f29}.section-title{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;margin:34px 0 14px}.section-title small{color:#817988;font-size:11px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(275px,1fr));gap:14px}.card,.guild,.panel,.subcard{border:1px solid var(--line);background:var(--panel);border-radius:16px;padding:20px;box-shadow:0 12px 38px rgba(0,0,0,.18)}.guild{transition:.15s ease}.guild:hover{border-color:#4f2d67;background:#100d14;transform:translateY(-2px)}.guild-head{display:flex;align-items:center;gap:12px;margin-bottom:16px}.guild-icon{width:43px;height:43px;border-radius:12px;object-fit:cover;border:1px solid #2c2c35;background:#16161e}.guild-icon.fallback{display:grid;place-items:center;color:#c56eff;font-weight:950}.guild-meta{min-width:0}.guild-meta h3{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.guild-meta span{font-size:11px;color:#77707f}.card-row{display:flex;align-items:center;justify-content:space-between;gap:10px}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:20px}.stat{border:1px solid var(--line);border-radius:13px;background:#0a0a0f;padding:15px}.stat span{display:block;color:#77707f;font-size:10px;text-transform:uppercase;letter-spacing:.09em;font-weight:850}.stat b{display:block;margin-top:5px;font-size:23px;letter-spacing:-.04em}.status-dot{display:inline-block;width:7px;height:7px;border-radius:99px;background:var(--green);box-shadow:0 0 12px rgba(53,217,154,.4);margin-right:6px}.notice{margin:14px 0;padding:13px 15px;border-radius:12px;border:1px solid var(--line);background:#0d0d12;color:#bbb2c2;font-size:12px}.notice.success{border-color:#1f503f;background:#0c1713;color:#9ee9cc}.notice.warning{border-color:#5c4925;background:#17130c;color:#efd08b}.notice.danger{border-color:#5e2933;background:#190d10;color:#ffb0bd}.settings-form{display:grid;gap:11px}.settings-form details{border:1px solid var(--line);border-radius:15px;background:var(--panel);overflow:hidden}.settings-form summary{cursor:pointer;list-style:none;display:flex;justify-content:space-between;align-items:center;gap:20px;padding:18px 20px;font-weight:900}.settings-form summary::-webkit-details-marker{display:none}.settings-form summary span{display:flex;align-items:center;gap:12px}.settings-form summary b{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;background:var(--purple-dim);color:#c16cff;font-size:10px}.settings-form summary small{color:#746d7b;font-size:11px;font-weight:700}.settings-form details[open] summary{border-bottom:1px solid var(--line)}.details-body{padding:20px}.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}.ticket-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:11px}.subcard{background:#0b0b10;padding:16px}.subcard h3{font-size:15px}label{display:block;color:#bcb4c2;font-size:11px;font-weight:850}input,select,textarea{width:100%;border:1px solid #292932;background:#0a0a0f;color:#f1edf5;border-radius:10px;padding:12px 13px;margin:7px 0 14px;outline:none;font:inherit;font-size:12px;transition:.15s ease}input:focus,select:focus,textarea:focus{border-color:#8542af;box-shadow:0 0 0 3px rgba(168,85,247,.10)}input::placeholder,textarea::placeholder{color:#5d5763}textarea{min-height:120px;resize:vertical;line-height:1.55}select{cursor:pointer}small{color:#756e7d}.savebar{position:sticky;bottom:14px;z-index:10;display:flex;align-items:center;justify-content:space-between;gap:12px;border:1px solid #30303a;background:rgba(10,10,14,.92);backdrop-filter:blur(14px);border-radius:14px;padding:11px 12px;box-shadow:0 18px 50px rgba(0,0,0,.35)}.savebar .muted{font-size:11px}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:13px}table{width:100%;border-collapse:collapse;min-width:720px}th,td{text-align:left;padding:12px 14px;border-bottom:1px solid var(--line-soft);font-size:11px}th{background:#0b0b10;color:#a89fb0;text-transform:uppercase;letter-spacing:.08em}td{color:#c9c1cf}.preview-shell{border:1px solid var(--line);border-radius:14px;background:#08080c;padding:15px}.preview-message{white-space:pre-wrap;padding:12px;border:1px solid var(--line);border-radius:10px;color:#d9d2df}.preview-box{margin-top:10px;border:1px solid var(--line);border-left:4px solid var(--purple);border-radius:10px;background:#101016;padding:15px}.preview-title{font-weight:900}.preview-desc{white-space:pre-wrap;color:#bbb3c2;line-height:1.55;margin-top:6px}.preview-footer{color:#6f6876;font-size:10px;margin-top:12px}.preview-img{max-width:100%;border-radius:9px;margin-top:12px}.preview-thumb{float:right;width:74px;height:74px;object-fit:cover;border-radius:9px;margin-left:12px}.tiny{font-size:10px;color:#726b79}.command-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:11px}.command-card{border:1px solid var(--line);border-radius:13px;background:#0a0a0f;padding:15px}.command-card h3{font-size:14px}.command-card .rowline{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-top:10px}.command-card select{width:116px;margin:0}.module-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:9px}.module{border:1px solid var(--line);background:#0a0a0f;border-radius:12px;padding:13px}.module b{font-size:12px}.module select{margin:9px 0 0}.footer{border-top:1px solid #15151b;padding:22px 0 36px;color:#625c68;font-size:10px;display:flex;justify-content:space-between;gap:12px}.kbd{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:#15151d;border:1px solid #2b2b34;border-radius:6px;padding:3px 6px;color:#c9c0d0}.empty{padding:34px;text-align:center;border:1px dashed #292932;border-radius:15px;color:#77707f}
    @media(max-width:900px){.navlinks{display:none}.hero-split{grid-template-columns:1fr}.hero-side{border-left:0;border-top:1px solid var(--line)}.hero-main{padding:38px 30px}.row,.ticket-grid{grid-template-columns:1fr}.top-actions .iconbtn{display:none}}
    @media(max-width:620px){.shell{width:min(100% - 22px,1260px)}.topbar{height:66px}.brand small{display:none}.content{padding-top:14px}.hero,.compact-hero{padding:23px}.hero-main{padding:30px 23px}.hero-side{padding:20px 23px}h1{font-size:40px}.section-title{align-items:flex-start;flex-direction:column}.savebar{align-items:stretch;flex-direction:column}.btn,button{width:100%}.toolbar .btn,.dashboard-actions .btn{width:auto}.footer{flex-direction:column}}
    </style>
    """
    css = css.replace("--purple:#a855f7", f"--purple:#{accent}")
    brand_mark = f"<img class='brandmark' src='{html.escape(logo_url)}' alt=''>" if logo_url else "<span class='brandmark'>M</span>"
    script = """
    <script>
    (function(){
      function cookie(name){return document.cookie.split('; ').find(v=>v.startsWith(name+'='))?.split('=').slice(1).join('=')||''}
      document.addEventListener('DOMContentLoaded',()=>{
        const token=decodeURIComponent(cookie('moe_csrf')||'');
        if(token){document.querySelectorAll("form[method='post'],form[method='POST']").forEach(form=>{if(!form.querySelector("input[name='_csrf']")){const i=document.createElement('input');i.type='hidden';i.name='_csrf';i.value=token;form.appendChild(i)}})}
      });
    })();
    </script>
    """
    html_doc = f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><meta name='theme-color' content='#050507'><title>{html.escape(title)} • {html.escape(dashboard_brand)}</title>{css}</head><body><div class='topbar-wrap'><div class='shell topbar'><a class='brand' href='/'>{brand_mark}<span>{html.escape(dashboard_brand)} <small>bot control</small></span></a><nav class='navlinks'><a href='/'>Dashboard</a><a href='/owner'>Owner</a><a href='/status'>Status</a><a href='{html.escape(website_url)}' target='_blank' rel='noopener'>Website</a></nav><div class='top-actions'><span class='iconbtn'><span class='status-dot'></span>{html.escape(BUILD_VERSION)}</span><a class='adminbtn' href='/logout'>Sign out</a></div></div></div><main class='shell content'>{body}</main><footer class='shell footer'><span>{html.escape(dashboard_title)} • production operations</span><span>Private dashboard • Discord OAuth protected</span></footer>{script}</body></html>"""
    return web.Response(text=html_doc, content_type="text/html")

async def home(request: web.Request) -> web.Response:
    user = await get_dashboard_user(request)
    owner_settings = await get_owner_settings()
    if not user:
        body = f"""
        <section class='hero hero-split'>
          <div class='hero-main'><span class='eyebrow'>Private Discord operations</span><h1>moealturej<br><span style='color:#ad57ed'>Bot Control.</span></h1><p class='muted'>Manage verification, support, moderation, server automation, announcements, embeds, logs, and command behavior from one production dashboard.</p><div class='toolbar'><a class='btn' href='/login'>Login with Discord →</a><a class='btn secondary' href='{html.escape(str(owner_settings.get('default_store_url') or DEFAULT_STORE_URL))}' target='_blank' rel='noopener'>Open website</a></div></div>
          <aside class='hero-side'><h4>Everything in one place</h4><div class='feature-row'><span class='feature-icon'>⚙</span><div><b>Full control</b><span>Every server module and command can be managed without editing code.</span></div></div><div class='feature-row'><span class='feature-icon'>◆</span><div><b>Production safety</b><span>OAuth sessions, rate-limit protection, audit logs, security headers, and incident references.</span></div></div><div class='feature-row'><span class='feature-icon'>↗</span><div><b>Direct operations</b><span>Send announcements, custom embeds, private DMs, and inspect recent activity.</span></div></div></aside>
        </section>"""
        return page("Bot Control", body)
    if DASHBOARD_OWNER_ONLY and not is_owner_user(int(user["user_id"])):
        return page("Private dashboard", f"<section class='hero compact-hero'><span class='eyebrow'>Restricted</span><h1>Owner access only.</h1><p class='muted'>{html.escape(str(owner_settings.get('owner_contact') or OWNER_CONTACT))}</p></section>")

    manageable = []
    for guild in bot.guilds:
        if await dashboard_can_access(user, guild.id):
            manageable.append(guild)
    total_members = sum(g.member_count or len(g.members) for g in manageable)
    configs = {}
    open_tickets = 0
    for guild in manageable:
        cfg = await get_guild_config(guild.id)
        configs[guild.id] = cfg
        open_tickets += len(cfg.get("open_tickets", {}))
    notice = str(owner_settings.get("dashboard_notice") or "").strip()
    notice_html = f"<div class='notice'>{html.escape(notice)}</div>" if notice else ""
    cards = []
    for guild in manageable:
        cfg = configs[guild.id]
        icon = f"<img class='guild-icon' src='{html.escape(str(guild.icon.url))}' alt=''>" if guild.icon else f"<span class='guild-icon fallback'>{html.escape(guild.name[:1].upper())}</span>"
        state = "Live" if cfg.get("enabled") else "Owner only"
        ticket_count = len(cfg.get("open_tickets", {}))
        cards.append(f"""<section class='guild'><div class='guild-head'>{icon}<div class='guild-meta'><h3>{html.escape(guild.name)}</h3><span>{guild.id}</span></div></div><div class='card-row'><span class='pill'>{html.escape(state)}</span><span class='muted' style='font-size:11px'>{ticket_count} open ticket{'s' if ticket_count != 1 else ''}</span></div><div class='toolbar'><a class='btn primary' href='/guild/{guild.id}'>Manage</a><a class='btn secondary' href='/guild/{guild.id}/commands'>Commands</a><a class='btn ghost' href='/guild/{guild.id}/activity'>Activity</a></div></section>""")
    body = f"""
    <section class='hero compact-hero'><span class='eyebrow'>Authenticated control center</span><h1>{html.escape(str(owner_settings.get('dashboard_title') or 'moealturej Bot Control'))}</h1><p class='muted'>{html.escape(str(owner_settings.get('dashboard_subtitle') or 'Private operations dashboard'))} • Signed in as <strong class='soft'>{html.escape(user.get('username','admin'))}</strong>.</p><div class='stats'><div class='stat'><span>Connected servers</span><b>{len(manageable)}</b></div><div class='stat'><span>Total members</span><b>{total_members:,}</b></div><div class='stat'><span>Open tickets</span><b>{open_tickets}</b></div><div class='stat'><span>Discord latency</span><b>{round(bot.latency*1000) if bot.latency else '—'}<small> ms</small></b></div></div></section>
    {notice_html}
    <div class='section-title'><div><span class='eyebrow'>Servers</span><h2 style='margin-top:8px'>Manage your bot.</h2></div><div class='toolbar' style='margin:0'><a class='btn secondary' href='/owner'>Owner settings</a></div></div>
    <div class='grid'>{''.join(cards) if cards else "<div class='empty'>No manageable bot servers found.</div>"}</div>"""
    return page("Dashboard", body)


async def owner_page(request: web.Request) -> web.Response:
    user = await get_dashboard_user(request)
    if not user or not is_owner_user(int(user["user_id"])):
        raise web.HTTPForbidden(text=owner_private_message())
    settings = await get_owner_settings()
    saved = "<div class='notice success'>Owner settings saved. Runtime caches were refreshed.</div>" if request.query.get("saved") else ""
    statuses = "\n".join(str(x) for x in settings.get("presence_statuses", []))
    def sel(value, expected):
        return "selected" if str(value) == str(expected) else ""
    guild_cards = []
    for guild in bot.guilds:
        cfg = await get_guild_config(guild.id)
        guild_cards.append(f"<section class='guild'><h3>{html.escape(guild.name)}</h3><p class='muted'>{guild.member_count or len(guild.members):,} members • {'enabled' if cfg.get('enabled') else 'owner only'}</p><div class='toolbar'><a class='btn secondary' href='/guild/{guild.id}'>Settings</a><a class='btn ghost' href='/guild/{guild.id}/commands'>Commands</a></div></section>")
    body = f"""
    <section class='hero compact-hero'><span class='eyebrow'>Owner account</span><h1>Global bot controls.</h1><p class='muted'>These settings control bot-wide branding, presence, maintenance state, and dashboard behavior. Server-specific settings remain isolated per guild.</p><div class='stats'><div class='stat'><span>Guilds</span><b>{len(bot.guilds)}</b></div><div class='stat'><span>Build</span><b style='font-size:17px'>{html.escape(BUILD_VERSION)}</b></div><div class='stat'><span>Global pause</span><b style='font-size:17px'>{'ON' if settings.get('global_pause') else 'OFF'}</b></div></div></section>{saved}
    <div class='section-title'><div><span class='eyebrow'>Global configuration</span><h2 style='margin-top:8px'>Owner settings</h2></div></div>
    <form class='settings-form' method='post'>
      <details open><summary><span><b>01</b> Dashboard identity</span><small>Theme copy and owner-facing presentation</small></summary><div class='details-body'><div class='row'><label>Dashboard title<input name='dashboard_title' maxlength='80' value='{html.escape(str(settings.get('dashboard_title') or ''))}'></label><label>Dashboard subtitle<input name='dashboard_subtitle' maxlength='140' value='{html.escape(str(settings.get('dashboard_subtitle') or ''))}'></label></div><label>Dashboard notice<input name='dashboard_notice' maxlength='300' value='{html.escape(str(settings.get('dashboard_notice') or ''))}' placeholder='Optional notice shown on the dashboard'></label><div class='row'><label>Accent color<input name='dashboard_accent' maxlength='7' value='#{html.escape(str(settings.get('dashboard_accent') or 'A855F7').lstrip('#'))}'></label><label>Dashboard logo URL<input name='dashboard_logo_url' value='{html.escape(str(settings.get('dashboard_logo_url') or ''))}' placeholder='Optional HTTPS image URL'></label></div></div></details>
      <details open><summary><span><b>02</b> Global links & defaults</span><small>Applied to global commands and new configuration</small></summary><div class='details-body'><label>Default store URL<input name='default_store_url' value='{html.escape(str(settings.get('default_store_url') or DEFAULT_STORE_URL))}'></label><div class='row'><label>Default brand name<input name='default_brand_name' maxlength='60' value='{html.escape(str(settings.get('default_brand_name') or 'moealturej'))}'></label><label>Default brand color<input name='default_brand_color' maxlength='7' value='#{html.escape(str(settings.get('default_brand_color') or 'A855F7').lstrip('#'))}'></label></div><label>Default embed footer<input name='default_brand_footer' maxlength='150' value='{html.escape(str(settings.get('default_brand_footer') or ''))}'></label><label>Default brand icon URL<input name='default_brand_icon_url' value='{html.escape(str(settings.get('default_brand_icon_url') or ''))}'></label></div></details>
      <details open><summary><span><b>03</b> Presence & runtime</span><small>Discord presence rotation and maintenance behavior</small></summary><div class='details-body'><div class='row'><label>Presence rotation<select name='presence_enabled'><option value='true' {sel(bool(settings.get('presence_enabled')), True)}>Enabled</option><option value='false' {sel(bool(settings.get('presence_enabled')), False)}>Disabled</option></select></label><label>Presence type<select name='presence_type'><option value='watching' {sel(settings.get('presence_type'),'watching')}>Watching</option><option value='playing' {sel(settings.get('presence_type'),'playing')}>Playing</option><option value='listening' {sel(settings.get('presence_type'),'listening')}>Listening</option><option value='competing' {sel(settings.get('presence_type'),'competing')}>Competing</option></select></label></div><div class='row'><label>Presence rotation interval (seconds)<input type='number' min='60' max='3600' name='presence_interval_seconds' value='{int(settings.get('presence_interval_seconds') or 300)}'></label><label>Live stats refresh (minutes)<input type='number' min='5' max='360' name='stats_interval_minutes' value='{int(settings.get('stats_interval_minutes') or STATS_UPDATE_MINUTES)}'></label></div><label>Presence statuses — one per line<textarea name='presence_statuses' maxlength='3000'>{html.escape(statuses)}</textarea></label><div class='row'><label>Global maintenance pause<select name='global_pause'><option value='false' {sel(bool(settings.get('global_pause')), False)}>Off</option><option value='true' {sel(bool(settings.get('global_pause')), True)}>On — owner bypass only</option></select></label><label>Owner contact text<input name='owner_contact' maxlength='300' value='{html.escape(str(settings.get('owner_contact') or OWNER_CONTACT))}'></label></div><label>Maintenance message<input name='global_pause_message' maxlength='300' value='{html.escape(str(settings.get('global_pause_message') or ''))}'></label></div></details>
      <details><summary><span><b>04</b> Dashboard tools</span><small>Globally allow or suspend high-impact senders</small></summary><div class='details-body'><div class='row'><label>Private DM sender<select name='dm_sender_enabled'><option value='true' {sel(bool(settings.get('dm_sender_enabled')), True)}>Enabled</option><option value='false' {sel(bool(settings.get('dm_sender_enabled')), False)}>Disabled</option></select></label><label>Announcement / embed sender<select name='announcement_sender_enabled'><option value='true' {sel(bool(settings.get('announcement_sender_enabled')), True)}>Enabled</option><option value='false' {sel(bool(settings.get('announcement_sender_enabled')), False)}>Disabled</option></select></label></div><label>Maximum messages per /purge<input type='number' min='1' max='100' name='max_purge_amount' value='{int(settings.get('max_purge_amount') or MAX_PURGE_AMOUNT)}'></label></div></details>
      <div class='savebar'><span class='muted'>Owner changes take effect immediately; presence interval updates on save.</span><button class='primary' type='submit'>Save owner settings</button></div>
    </form>
    <div class='section-title'><div><span class='eyebrow'>Connected servers</span><h2 style='margin-top:8px'>Quick access</h2></div></div><div class='grid'>{''.join(guild_cards) if guild_cards else "<div class='empty'>No connected servers.</div>"}</div>"""
    return page("Owner Settings", body)


async def owner_save(request: web.Request) -> web.Response:
    user = await get_dashboard_user(request)
    if not user or not is_owner_user(int(user["user_id"])):
        raise web.HTTPForbidden(text=owner_private_message())
    data = await request.post()
    def text(name: str, default: str = "", limit: int = 500) -> str:
        return str(data.get(name) or default).strip()[:limit]
    def as_bool(name: str) -> bool:
        return str(data.get(name, "false")).lower() == "true"
    def safe_url(name: str, fallback: str) -> str:
        value = text(name, fallback, 500)
        return value if is_http_url(value) else fallback
    def optional_url(name: str) -> str:
        value = text(name, "", 500)
        return value if is_http_url(value) else ""
    def color(name: str, fallback: str) -> str:
        value = text(name, fallback, 7).lstrip("#").upper()
        return value if len(value) in {3, 6} and all(ch in string.hexdigits for ch in value) else fallback
    try:
        interval = max(60, min(3600, int(data.get("presence_interval_seconds") or 300)))
    except (TypeError, ValueError):
        interval = 300
    try:
        stats_interval = max(5, min(360, int(data.get("stats_interval_minutes") or STATS_UPDATE_MINUTES)))
    except (TypeError, ValueError):
        stats_interval = STATS_UPDATE_MINUTES
    try:
        max_purge = max(1, min(100, int(data.get("max_purge_amount") or MAX_PURGE_AMOUNT)))
    except (TypeError, ValueError):
        max_purge = MAX_PURGE_AMOUNT
    statuses = [line.strip()[:120] for line in text("presence_statuses", "", 3000).splitlines() if line.strip()][:20]
    updates = {
        "dashboard_title": text("dashboard_title", "moealturej Bot Control", 80),
        "dashboard_subtitle": text("dashboard_subtitle", "Private operations dashboard", 140),
        "dashboard_notice": text("dashboard_notice", "", 300),
        "dashboard_accent": color("dashboard_accent", "A855F7"),
        "dashboard_logo_url": optional_url("dashboard_logo_url"),
        "default_store_url": safe_url("default_store_url", DEFAULT_STORE_URL),
        "default_brand_name": text("default_brand_name", "moealturej", 60),
        "default_brand_color": color("default_brand_color", "A855F7"),
        "default_brand_footer": text("default_brand_footer", "moealturej • Professional server tools", 150),
        "default_brand_icon_url": optional_url("default_brand_icon_url"),
        "presence_enabled": as_bool("presence_enabled"),
        "presence_type": text("presence_type", "watching", 20) if text("presence_type", "watching", 20) in {"watching", "playing", "listening", "competing"} else "watching",
        "presence_interval_seconds": interval,
        "presence_statuses": statuses or list(DEFAULT_OWNER_SETTINGS["presence_statuses"]),
        "global_pause": as_bool("global_pause"),
        "global_pause_message": text("global_pause_message", "Bot features are temporarily paused for maintenance.", 300),
        "owner_contact": text("owner_contact", OWNER_CONTACT, 300),
        "dm_sender_enabled": as_bool("dm_sender_enabled"),
        "announcement_sender_enabled": as_bool("announcement_sender_enabled"),
        "stats_interval_minutes": stats_interval,
        "max_purge_amount": max_purge,
    }
    await set_owner_settings(updates)
    if rotate_status.is_running():
        rotate_status.change_interval(seconds=interval)
    if update_stats.is_running():
        update_stats.change_interval(minutes=stats_interval)
    await save_event("dashboard_events", {"guild_id": None, "user_id": int(user["user_id"]), "event": "owner_settings_updated", "fields": sorted(updates)})
    raise web.HTTPFound("/owner?saved=1")


async def command_settings_page(request: web.Request) -> web.Response:
    user = await get_dashboard_user(request)
    guild_id = int(request.match_info["guild_id"])
    if not user or not await dashboard_can_access(user, guild_id):
        raise web.HTTPForbidden(text=owner_private_message())
    guild = bot.get_guild(guild_id)
    if not guild:
        return page("Missing server", "<section class='card'><h1>Bot is not in this server</h1></section>")
    config = await get_guild_config(guild_id)
    saved = "<div class='notice success'>Command and module settings saved.</div>" if request.query.get("saved") else ""
    enabled_map = config.get("command_enabled") or {}
    def opts(value: bool) -> str:
        return f"<option value='true' {'selected' if value else ''}>Enabled</option><option value='false' {'selected' if not value else ''}>Disabled</option>"
    groups: Dict[str, list[str]] = {}
    for name, meta in COMMAND_CATALOG.items():
        groups.setdefault(meta["group"], []).append(name)
    group_html = []
    for group, names in groups.items():
        cards = []
        for name in names:
            meta = COMMAND_CATALOG[name]
            cards.append(f"<div class='command-card'><h3>/{html.escape(name)}</h3><div class='muted' style='font-size:10px'>{html.escape(meta['label'])}</div><div class='rowline'><span class='muted' style='font-size:10px'>Availability</span><select name='cmd_{html.escape(name)}'>{opts(bool(enabled_map.get(name, True)))}</select></div></div>")
        group_html.append(f"<details {'open' if group in {'Essentials','Moderation'} else ''}><summary><span><b>•</b> {html.escape(group)}</span><small>{len(names)} commands</small></summary><div class='details-body'><div class='command-grid'>{''.join(cards)}</div></div></details>")
    feature_labels = [("feature_utilities","Member utilities"),("feature_admin_commands","Admin commands"),("feature_verification","Verification"),("feature_welcome","Welcome"),("feature_tickets","Tickets"),("feature_moderation","Moderation"),("feature_stats","Live stats"),("feature_announcements","Announcements"),("feature_dms","DM sender")]
    modules = "".join(f"<div class='module'><b>{html.escape(label)}</b><select name='{key}'>{opts(bool(config.get(key, True)))}</select></div>" for key,label in feature_labels)
    body = f"""
    <section class='hero compact-hero'><span class='eyebrow'>Command center</span><h1>{html.escape(guild.name)} commands.</h1><p class='muted'>Soft-disable individual slash commands or entire modules without removing registrations from Discord. The owner account always bypasses disabled states so you cannot lock yourself out.</p></section>{saved}
    <div class='dashboard-actions'><a class='btn secondary' href='/guild/{guild_id}'>← Server settings</a><a class='btn ghost' href='/guild/{guild_id}/activity'>Activity</a></div>
    <form class='settings-form' method='post'>
      <details open><summary><span><b>01</b> Module switches</span><small>Control complete feature groups</small></summary><div class='details-body'><div class='module-strip'>{modules}</div></div></details>
      <details open><summary><span><b>02</b> Member-facing copy</span><small>Customize the most visible public command responses</small></summary><div class='details-body'><div class='row'><label>Help title<input name='help_title' maxlength='100' value='{html.escape(str(config.get('help_title') or 'Command Center'))}'></label><label>Ping title<input name='ping_title' maxlength='100' value='{html.escape(str(config.get('ping_title') or 'System Online'))}'></label></div><label>Help description<textarea name='help_description' maxlength='800'>{html.escape(str(config.get('help_description') or ''))}</textarea></label><label>Ping description<input name='ping_description' maxlength='300' value='{html.escape(str(config.get('ping_description') or 'Discord latency: `{latency_ms}ms`'))}'><small>Available variable: &#123;latency_ms&#125;</small></label><div class='row'><label>Store title<input name='store_title' maxlength='100' value='{html.escape(str(config.get('store_title') or 'moealturej Store'))}'></label><label>Store button label<input name='store_button_label' maxlength='80' value='{html.escape(str(config.get('store_button_label') or 'Open Store'))}'></label></div><label>Store description<textarea name='store_description' maxlength='800'>{html.escape(str(config.get('store_description') or ''))}</textarea></label></div></details>
      {''.join(group_html)}
      <div class='savebar'><span class='muted'>Changes are applied on the next command interaction.</span><button class='primary' type='submit'>Save command center</button></div>
    </form>"""
    return page(f"{guild.name} Commands", body)


async def command_settings_save(request: web.Request) -> web.Response:
    user = await get_dashboard_user(request)
    guild_id = int(request.match_info["guild_id"])
    if not user or not await dashboard_can_access(user, guild_id):
        raise web.HTTPForbidden(text=owner_private_message())
    data = await request.post()
    def as_bool(name: str, default: bool = True) -> bool:
        raw = data.get(name)
        return default if raw is None else str(raw).lower() == "true"
    def text(name: str, default: str = "", limit: int = 800) -> str:
        return str(data.get(name) or default).strip()[:limit]
    command_map = {name: as_bool(f"cmd_{name}", True) for name in COMMAND_CATALOG}
    updates: Dict[str, Any] = {
        "command_enabled": command_map,
        "help_title": text("help_title", "Command Center", 100),
        "help_description": text("help_description", "Everything you need, organized in one place.", 800),
        "ping_title": text("ping_title", "System Online", 100),
        "ping_description": text("ping_description", "Discord latency: `{latency_ms}ms`", 300),
        "store_title": text("store_title", "moealturej Store", 100),
        "store_description": text("store_description", "Browse products, downloads, and account tools securely.", 800),
        "store_button_label": text("store_button_label", "Open Store", 80),
    }
    for feature in ("feature_utilities","feature_admin_commands","feature_verification","feature_welcome","feature_tickets","feature_moderation","feature_stats","feature_announcements","feature_dms"):
        updates[feature] = as_bool(feature, True)
    await set_config(guild_id, updates)
    await save_event("dashboard_events", {"guild_id": guild_id, "user_id": int(user["user_id"]), "event": "command_settings_updated"})
    raise web.HTTPFound(f"/guild/{guild_id}/commands?saved=1")

async def login(request: web.Request) -> web.Response:
    state = secrets.token_urlsafe(32)
    await mdb.oauth_states.insert_one({"state": state, "type": "dashboard", "expires_at": utcnow() + timedelta(minutes=10)})
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": f"{PUBLIC_BASE_URL}/oauth/callback",
        "response_type": "code",
        "scope": "identify guilds",
        "state": state,
    }
    raise web.HTTPFound(f"https://discord.com/oauth2/authorize?{urlencode(params)}")


async def oauth_callback(request: web.Request) -> web.Response:
    state = request.query.get("state", "")
    code = request.query.get("code", "")
    if request.query.get("error"):
        return page("Login cancelled", "<section class='card'><h1>Login cancelled</h1><p class='muted'>No dashboard session was created.</p></section>")
    found = await mdb.oauth_states.find_one_and_delete({"state": state, "type": "dashboard", "expires_at": {"$gt": utcnow()}})
    if not found or not code:
        raise web.HTTPBadRequest(text="Invalid or expired OAuth state.")
    token = await exchange_code(code, f"{PUBLIC_BASE_URL}/oauth/callback")
    user = await discord_get("/users/@me", token["access_token"])
    guilds = await discord_get("/users/@me/guilds", token["access_token"])
    session_id = secrets.token_urlsafe(36)
    await mdb.sessions.insert_one({"session_id": session_id, "user_id": int(user["id"]), "username": user.get("username", "user"), "guilds": guilds, "expires_at": utcnow() + timedelta(days=WEB_SESSION_DAYS)})
    resp = web.HTTPFound("/")
    resp.set_cookie("moe_session", sign_value(session_id), max_age=WEB_SESSION_DAYS * 86400, httponly=True, secure=PUBLIC_BASE_URL.startswith("https://"), samesite="Lax")
    raise resp


async def logout(request: web.Request) -> web.Response:
    raw = request.cookies.get("moe_session")
    sid = unsign_value(raw) if raw else None
    if sid:
        await mdb.sessions.delete_one({"session_id": sid})
    resp = web.HTTPFound("/")
    resp.del_cookie("moe_session")
    raise resp


async def guild_page(request: web.Request) -> web.Response:
    user = await get_dashboard_user(request)
    guild_id = int(request.match_info["guild_id"])
    if not user or not await dashboard_can_access(user, guild_id):
        return page("Not authorized", f"<section class='card'><h1>Access denied</h1><p class='muted'>{html.escape(owner_private_message())}</p></section>")
    guild = bot.get_guild(guild_id)
    if not guild:
        return page("Missing server", "<section class='card'><h1>Bot is not in this server</h1></section>")
    config = await get_guild_config(guild_id)

    def options(items, selected, *, prefix: str = ""):
        out = ["<option value=''>Not set</option>"]
        for obj in items:
            sel = "selected" if selected and int(selected) == obj.id else ""
            out.append(f"<option value='{obj.id}' {sel}>{prefix}{html.escape(obj.name)}</option>")
        return "".join(out)

    def selected(value: bool) -> str:
        return "selected" if value else ""

    roles = [r for r in guild.roles if not r.is_default() and not r.managed]
    text_channels = guild.text_channels
    voice_channels = guild.voice_channels
    categories = guild.categories
    me = guild.me
    permissions = me.guild_permissions if me else discord.Permissions.none()
    verified_role = guild.get_role(int(config.get("verified_role") or 0))
    problems: list[str] = []
    if config.get("feature_verification", True) and not permissions.manage_roles:
        problems.append("Verification needs Manage Roles")
    if (config.get("feature_tickets", True) or config.get("feature_stats", True)) and not permissions.manage_channels:
        problems.append("Tickets/live stats need Manage Channels")
    if not permissions.send_messages:
        problems.append("Bot is missing Send Messages")
    if config.get("feature_verification", True) and verified_role and me and verified_role >= me.top_role:
        problems.append("Verified role is above the bot role")
    if config.get("feature_verification", True) and not config.get("verified_role"):
        problems.append("Verified role is not selected")
    if config.get("feature_tickets", True) and not config.get("ticket_category"):
        problems.append("Ticket category is not selected")

    open_tickets = len(config.get("open_tickets", {}))
    verified_count = await mdb.verified_members.count_documents({"guild_id": guild_id})
    saved_banner = "<div class='notice success'>✓ Settings saved and cache refreshed.</div>" if request.query.get("saved") else ""
    diag_class = "success" if not problems else "warning"
    diag_text = "Everything required is configured." if not problems else " • ".join(problems)

    body = f"""
    <section class='hero compact-hero'>
      <span class='pill'>Control center</span><h1>{html.escape(guild.name)}</h1>
      <p class='muted'>Configure branding, verification, tickets, welcome messages, moderation logs, and server operations without editing code.</p>
      <div class='stats'><div class='stat'><span class='muted'>Members</span><b>{guild.member_count or len(guild.members)}</b></div><div class='stat'><span class='muted'>Open tickets</span><b>{open_tickets}</b></div><div class='stat'><span class='muted'>Verified</span><b>{verified_count}</b></div></div>
    </section>
    {saved_banner}
    <div class='notice {diag_class}'><strong>Setup audit:</strong> {html.escape(diag_text)}</div>
    <div class='dashboard-actions'><a class='btn primary' href='/guild/{guild_id}/commands'>Command center</a><a class='btn secondary' href='/guild/{guild_id}/announcements'>Announcement</a><a class='btn secondary' href='/guild/{guild_id}/embeds'>Embed builder</a><a class='btn secondary' href='/guild/{guild_id}/dms'>DM sender</a><a class='btn secondary' href='/guild/{guild_id}/activity'>Activity & errors</a></div>

    <form class='settings-form' method='post'>
      <details open><summary><span><b>01</b> Brand & core settings</span><small>Identity, access, and public links</small></summary><div class='details-body'>
        <div class='row'><label>Bot availability<select name='enabled'><option value='true' {selected(bool(config.get('enabled')))}>Enabled</option><option value='false' {selected(not bool(config.get('enabled')))}>Disabled / owner only</option></select></label><label>Store URL<input name='store_url' value='{html.escape(str(config.get('store_url') or DEFAULT_STORE_URL))}' placeholder='https://your-store.com'></label></div>
        <div class='row'><label>Brand name<input name='brand_name' maxlength='60' value='{html.escape(str(config.get('brand_name') or 'moealturej'))}'></label><label>Brand color<input name='brand_color' maxlength='7' value='#{html.escape(str(config.get('brand_color') or '7C3AED').lstrip('#'))}' placeholder='#7C3AED'></label></div>
        <label>Embed footer<input name='brand_footer' maxlength='150' value='{html.escape(str(config.get('brand_footer') or ''))}'></label>
        <div class='row'><label>Brand icon URL<input name='brand_icon_url' value='{html.escape(str(config.get('brand_icon_url') or ''))}' placeholder='Optional HTTPS image URL'></label><label>Bot admin role<select name='bot_admin_role'>{options(roles, config.get('bot_admin_role'))}</select></label></div>
        <div class='row'><label>Default announcement footer<input name='announce_footer' maxlength='150' value='{html.escape(str(config.get('announce_footer') or 'moealturej'))}'></label><label>Default announcement image URL<input name='announce_image' value='{html.escape(str(config.get('announce_image') or ''))}' placeholder='Optional banner image'></label></div>
      </div></details>

      <details open><summary><span><b>02</b> Verification system</span><small>OAuth security, account rules, and role assignment</small></summary><div class='details-body'>
        <div class='row'><label>Verified role<select name='verified_role'>{options(roles, config.get('verified_role'))}</select></label><label>Unverified role to remove<select name='unverified_role'>{options(roles, config.get('unverified_role'))}</select></label></div>
        <div class='row'><label>Auto role on join<select name='auto_role'>{options(roles, config.get('auto_role'))}</select></label><label>Verification logs<select name='verification_log_channel'>{options(text_channels, config.get('verification_log_channel'), prefix='#')}</select></label></div>
        <div class='row'><label>Minimum Discord account age (days)<input type='number' min='0' max='3650' name='verification_min_account_days' value='{int(config.get('verification_min_account_days', 3))}'></label><label>Verification panel channel<select name='verification_channel'>{options(text_channels, config.get('verification_channel'), prefix='#')}</select></label></div>
        <div class='row'><label>Existing member required<select name='verification_require_member'><option value='true' {selected(bool(config.get('verification_require_member')))}>Yes — deny users outside server</option><option value='false' {selected(not bool(config.get('verification_require_member')))}>No — allow approved OAuth join</option></select></label><label>OAuth server join<select name='oauth_verify_join_enabled'><option value='true' {selected(bool(config.get('oauth_verify_join_enabled', True)))}>Enabled</option><option value='false' {selected(not bool(config.get('oauth_verify_join_enabled', True)))}>Disabled</option></select></label></div>
        <label>Panel title<input name='verification_title' maxlength='100' value='{html.escape(str(config.get('verification_title') or 'Verify Access'))}'></label>
        <label>Panel description<textarea name='verification_description' maxlength='1500'>{html.escape(str(config.get('verification_description') or ''))}</textarea></label>
        <div class='row'><label>Button label<input name='verification_button_label' maxlength='80' value='{html.escape(str(config.get('verification_button_label') or 'Verify with Discord'))}'></label><label>Success message<input name='verification_success_message' maxlength='300' value='{html.escape(str(config.get('verification_success_message') or ''))}'></label></div>
      </div></details>

      <details><summary><span><b>03</b> Welcome experience</span><small>New-member message and destination</small></summary><div class='details-body'>
        <div class='row'><label>Welcome system<select name='welcome_enabled'><option value='true' {selected(bool(config.get('welcome_enabled', True)))}>Enabled</option><option value='false' {selected(not bool(config.get('welcome_enabled', True)))}>Disabled</option></select></label><label>Welcome channel<select name='welcome_channel'>{options(text_channels, config.get('welcome_channel'), prefix='#')}</select></label></div>
        <div class='row'><label>Welcome title<input name='welcome_title' maxlength='256' value='{html.escape(str(config.get('welcome_title') or 'Welcome to {server}'))}'></label><label>Ping the new member<select name='welcome_ping_user'><option value='true' {selected(bool(config.get('welcome_ping_user')))}>Yes</option><option value='false' {selected(not bool(config.get('welcome_ping_user')))}>No</option></select></label></div>
        <label>Welcome message<textarea name='welcome_message' maxlength='1500' placeholder='Use &#123;mention&#125;, &#123;server&#125;, &#123;username&#125;, &#123;member_count&#125;'>{html.escape(str(config.get('welcome_message') or DEFAULT_GUILD_CONFIG['welcome_message']))}</textarea></label>
      </div></details>

      <details open><summary><span><b>04</b> Support tickets</span><small>Panel copy, routing, staff roles, and naming</small></summary><div class='details-body'>
        <div class='row'><label>Ticket category<select name='ticket_category'>{options(categories, config.get('ticket_category'))}</select></label><label>Transcript log channel<select name='ticket_log_channel'>{options(text_channels, config.get('ticket_log_channel'), prefix='#')}</select></label></div>
        <div class='row'><label>Ticket panel channel<select name='ticket_panel_channel'>{options(text_channels, config.get('ticket_panel_channel'), prefix='#')}</select></label><label>Channel name template<input name='default_ticket_name' maxlength='90' value='{html.escape(str(config.get('default_ticket_name') or DEFAULT_GUILD_CONFIG['default_ticket_name']))}'><small>Use &#123;username&#125;, &#123;type&#125;, or &#123;short_id&#125;.</small></label></div>
        <label>Panel title<input name='ticket_panel_title' maxlength='100' value='{html.escape(str(config.get('ticket_panel_title') or 'Support Center'))}'></label>
        <label>Panel description<textarea name='ticket_panel_description' maxlength='1500'>{html.escape(str(config.get('ticket_panel_description') or ''))}</textarea></label>
        <label>Message inside new tickets<textarea name='ticket_open_message' maxlength='1000'>{html.escape(str(config.get('ticket_open_message') or ''))}</textarea></label>
        <div class='ticket-grid'>
          <section class='subcard'><h3>General</h3><label>Label<input name='ticket_label_general' value='{html.escape(str(config.get('ticket_label_general') or 'General support'))}'></label><label>Description<input name='ticket_description_general' maxlength='100' value='{html.escape(str(config.get('ticket_description_general') or ''))}'></label><label>Support role<select name='ticket_role_general'>{options(roles, config.get('ticket_role_general'))}</select></label></section>
          <section class='subcard'><h3>HWID reset</h3><label>Label<input name='ticket_label_hwid' value='{html.escape(str(config.get('ticket_label_hwid') or 'Key HWID reset'))}'></label><label>Description<input name='ticket_description_hwid' maxlength='100' value='{html.escape(str(config.get('ticket_description_hwid') or ''))}'></label><label>Support role<select name='ticket_role_hwid'>{options(roles, config.get('ticket_role_hwid'))}</select></label></section>
          <section class='subcard'><h3>Missing key</h3><label>Label<input name='ticket_label_key_not_received' value='{html.escape(str(config.get('ticket_label_key_not_received') or 'Key not received'))}'></label><label>Description<input name='ticket_description_key_not_received' maxlength='100' value='{html.escape(str(config.get('ticket_description_key_not_received') or ''))}'></label><label>Support role<select name='ticket_role_key_not_received'>{options(roles, config.get('ticket_role_key_not_received'))}</select></label></section>
        </div>
      </div></details>

      <details><summary><span><b>05</b> Logs & moderation</span><small>Audit destinations and member notification behavior</small></summary><div class='details-body'>
        <div class='row'><label>Moderation logs<select name='moderation_log_channel'>{options(text_channels, config.get('moderation_log_channel'), prefix='#')}</select></label><label>Command logs<select name='command_log_channel'>{options(text_channels, config.get('command_log_channel'), prefix='#')}</select></label></div>
        <div class='row'><label>DM members when warned<select name='moderation_dm_warn'><option value='true' {selected(bool(config.get('moderation_dm_warn', True)))}>Enabled</option><option value='false' {selected(not bool(config.get('moderation_dm_warn', True)))}>Disabled</option></select></label><label>DM members when timed out<select name='moderation_dm_timeout'><option value='true' {selected(bool(config.get('moderation_dm_timeout', True)))}>Enabled</option><option value='false' {selected(not bool(config.get('moderation_dm_timeout', True)))}>Disabled</option></select></label></div>
      </div></details>

      <details><summary><span><b>06</b> Live server stats</span><small>Voice-channel counters and display templates</small></summary><div class='details-body'>
        <div class='row'><label>Stats category<select name='stats_category'>{options(categories, config.get('stats_category'))}</select></label><label>Members voice channel<select name='stats_channel_members'>{options(voice_channels, (config.get('stats_channels') or {}).get('members'))}</select></label></div>
        <div class='row'><label>Humans voice channel<select name='stats_channel_humans'>{options(voice_channels, (config.get('stats_channels') or {}).get('humans'))}</select></label><label>Bots voice channel<select name='stats_channel_bots'>{options(voice_channels, (config.get('stats_channels') or {}).get('bots'))}</select></label></div>
        <label>Boosts voice channel<select name='stats_channel_boosts'>{options(voice_channels, (config.get('stats_channels') or {}).get('boosts'))}</select></label>
        <div class='row'><label>Members name template<input name='stats_name_members' maxlength='100' value='{html.escape(str(config.get('stats_name_members') or DEFAULT_GUILD_CONFIG['stats_name_members']))}'><small>Use &#123;count&#125; and &#123;server&#125;.</small></label><label>Humans name template<input name='stats_name_humans' maxlength='100' value='{html.escape(str(config.get('stats_name_humans') or DEFAULT_GUILD_CONFIG['stats_name_humans']))}'></label></div>
        <div class='row'><label>Bots name template<input name='stats_name_bots' maxlength='100' value='{html.escape(str(config.get('stats_name_bots') or DEFAULT_GUILD_CONFIG['stats_name_bots']))}'></label><label>Boosts name template<input name='stats_name_boosts' maxlength='100' value='{html.escape(str(config.get('stats_name_boosts') or DEFAULT_GUILD_CONFIG['stats_name_boosts']))}'></label></div>
      </div></details>

      <div class='savebar'><span class='muted'>Changes apply immediately after saving.</span><button type='submit'>Save all settings</button></div>
    </form>
    """
    return page(guild.name, body)


async def guild_save(request: web.Request) -> web.Response:
    user = await get_dashboard_user(request)
    guild_id = int(request.match_info["guild_id"])
    if not user or not await dashboard_can_access(user, guild_id):
        raise web.HTTPForbidden(text=owner_private_message())
    data = await request.post()

    def as_int(name: str, default: Optional[int] = None, low: int = 0, high: int = 10**20) -> Optional[int]:
        value = str(data.get(name, "")).strip()
        if not value and default is None:
            return None
        try:
            return max(low, min(high, int(value if value else default)))
        except (TypeError, ValueError):
            return default

    def as_bool(name: str) -> bool:
        return str(data.get(name, "false")).lower() == "true"

    def text(name: str, default: str = "", limit: int = 1500) -> str:
        return str(data.get(name) or default).strip()[:limit]

    def optional_url(name: str) -> str:
        value = text(name, "", 500)
        return value if is_http_url(value) else ""

    store_url = text("store_url", DEFAULT_STORE_URL, 500)
    if not is_http_url(store_url):
        store_url = DEFAULT_STORE_URL
    brand_color = text("brand_color", "7C3AED", 7).lstrip("#").upper()
    if len(brand_color) not in {3, 6} or any(ch not in string.hexdigits for ch in brand_color):
        brand_color = "7C3AED"
    updates = {
        "enabled": as_bool("enabled"),
        "store_url": store_url,
        "brand_name": text("brand_name", "moealturej", 60),
        "brand_color": brand_color,
        "brand_footer": text("brand_footer", "moealturej", 150),
        "brand_icon_url": optional_url("brand_icon_url"),
        "announce_footer": text("announce_footer", "moealturej", 150),
        "announce_image": optional_url("announce_image"),
        "verified_role": as_int("verified_role"),
        "unverified_role": as_int("unverified_role"),
        "auto_role": as_int("auto_role"),
        "bot_admin_role": as_int("bot_admin_role"),
        "verification_channel": as_int("verification_channel"),
        "verification_log_channel": as_int("verification_log_channel"),
        "verification_min_account_days": as_int("verification_min_account_days", 3, 0, 3650),
        "verification_require_member": as_bool("verification_require_member"),
        "oauth_verify_join_enabled": as_bool("oauth_verify_join_enabled"),
        "verification_title": text("verification_title", "Verify Access", 100),
        "verification_description": text("verification_description", DEFAULT_GUILD_CONFIG["verification_description"], 1500),
        "verification_button_label": text("verification_button_label", "Verify with Discord", 80),
        "verification_success_message": text("verification_success_message", DEFAULT_GUILD_CONFIG["verification_success_message"], 300),
        "welcome_channel": as_int("welcome_channel"),
        "welcome_enabled": as_bool("welcome_enabled"),
        "welcome_title": text("welcome_title", "Welcome to {server}", 256),
        "welcome_message": text("welcome_message", DEFAULT_GUILD_CONFIG["welcome_message"], 1500),
        "welcome_ping_user": as_bool("welcome_ping_user"),
        "moderation_log_channel": as_int("moderation_log_channel"),
        "command_log_channel": as_int("command_log_channel"),
        "moderation_dm_warn": as_bool("moderation_dm_warn"),
        "moderation_dm_timeout": as_bool("moderation_dm_timeout"),
        "stats_category": as_int("stats_category"),
        "stats_channels": {
            "members": as_int("stats_channel_members"),
            "humans": as_int("stats_channel_humans"),
            "bots": as_int("stats_channel_bots"),
            "boosts": as_int("stats_channel_boosts"),
        },
        "stats_name_members": text("stats_name_members", DEFAULT_GUILD_CONFIG["stats_name_members"], 100),
        "stats_name_humans": text("stats_name_humans", DEFAULT_GUILD_CONFIG["stats_name_humans"], 100),
        "stats_name_bots": text("stats_name_bots", DEFAULT_GUILD_CONFIG["stats_name_bots"], 100),
        "stats_name_boosts": text("stats_name_boosts", DEFAULT_GUILD_CONFIG["stats_name_boosts"], 100),
        "ticket_category": as_int("ticket_category"),
        "ticket_panel_channel": as_int("ticket_panel_channel"),
        "ticket_log_channel": as_int("ticket_log_channel"),
        "ticket_role_general": as_int("ticket_role_general"),
        "ticket_role_hwid": as_int("ticket_role_hwid"),
        "ticket_role_key_not_received": as_int("ticket_role_key_not_received"),
        "default_ticket_name": clean_ticket_template(text("default_ticket_name", DEFAULT_GUILD_CONFIG["default_ticket_name"], 90)),
        "ticket_panel_title": text("ticket_panel_title", "Support Center", 100),
        "ticket_panel_description": text("ticket_panel_description", DEFAULT_GUILD_CONFIG["ticket_panel_description"], 1500),
        "ticket_open_message": text("ticket_open_message", DEFAULT_GUILD_CONFIG["ticket_open_message"], 1000),
        "ticket_label_general": text("ticket_label_general", "General support", 100),
        "ticket_description_general": text("ticket_description_general", DEFAULT_GUILD_CONFIG["ticket_description_general"], 100),
        "ticket_label_hwid": text("ticket_label_hwid", "Key HWID reset", 100),
        "ticket_description_hwid": text("ticket_description_hwid", DEFAULT_GUILD_CONFIG["ticket_description_hwid"], 100),
        "ticket_label_key_not_received": text("ticket_label_key_not_received", "Key not received", 100),
        "ticket_description_key_not_received": text("ticket_description_key_not_received", DEFAULT_GUILD_CONFIG["ticket_description_key_not_received"], 100),
    }
    await set_config(guild_id, updates)
    await save_event("dashboard_events", {"guild_id": guild_id, "user_id": int(user["user_id"]), "event": "settings_updated", "fields": sorted(updates)})
    raise web.HTTPFound(f"/guild/{guild_id}?saved=1")


def parse_embed_color(value: str) -> int:
    return parse_color_value(value)


def channel_options(guild: discord.Guild, selected: Optional[int] = None) -> str:
    out = ["<option value=''>Select a channel</option>"]
    for channel in guild.text_channels:
        sel = "selected" if selected and int(selected) == channel.id else ""
        out.append(f"<option value='{channel.id}' {sel}>#{html.escape(channel.name)}</option>")
    return "".join(out)


def composer_page(guild: discord.Guild, mode: str, config: Dict[str, Any], sent: bool = False) -> web.Response:
    is_announcement = mode == "announcement"
    title = "Announcement Composer" if is_announcement else "Embed Composer"
    default_color = str(config.get("brand_color") or "A855F7").lstrip("#")
    defaults = {
        "title": "New Announcement" if is_announcement else "Embed Title",
        "message": "Write your announcement details here..." if is_announcement else "Write your embed description here...",
        "content": "",
        "footer": str(config.get("announce_footer") or config.get("brand_footer") or "moealturej"),
        "color": default_color,
        "image": str(config.get("announce_image") or "") if is_announcement else "",
    }
    mention_control = "<label style='display:flex;align-items:center;gap:10px;margin-bottom:14px'><input name='allow_mentions' type='checkbox' style='width:auto;margin:0'> Allow @user, @role, and @everyone mentions in the top message</label>" if is_announcement else ""
    body = f"""
    <section class='hero compact-hero'><span class='eyebrow'>{'Announcement tools' if is_announcement else 'Embed tools'}</span><h1>{title}.</h1><p class='muted'>Build a polished Discord message with a safe live preview. Mentions are suppressed by default to prevent accidental mass pings.</p></section>
    {'<div class="notice success">Message delivered to Discord successfully.</div>' if sent else ''}
    <div class='section-title'><div><span class='eyebrow'>Composer</span><h2 style='margin-top:8px'>Message content</h2></div><a class='btn secondary' href='/guild/{guild.id}'>Back to server</a></div>
    <form class='grid' method='post'>
      <section class='card'>
        <label>Destination channel<select name='channel_id' required>{channel_options(guild)}</select></label>
        <label>Top message / content<textarea id='contentInput' name='content' placeholder='Optional text shown above the embed'>{html.escape(defaults['content'])}</textarea></label>
        {mention_control}
        <div class='form-section'><h3>Embed</h3><label style='display:flex;align-items:center;gap:10px;margin:12px 0'><input id='embedEnabled' name='embed_enabled' type='checkbox' checked style='width:auto;margin:0'> Include an embed</label></div>
        <label>Embed title<input id='titleInput' name='title' maxlength='256' value='{html.escape(defaults['title'])}'></label>
        <label>Embed description<textarea id='messageInput' name='message'>{html.escape(defaults['message'])}</textarea></label>
        <div class='row'><label>Color hex<input id='colorInput' name='color' maxlength='7' value='{html.escape(defaults['color'])}' placeholder='A855F7'></label><label>Footer<input id='footerInput' name='footer' maxlength='2048' value='{html.escape(defaults['footer'])}'></label></div>
        <div class='row'><label>Thumbnail image URL<input id='thumbInput' name='thumbnail_url' placeholder='https://...'></label><label>Large image URL<input id='imageInput' name='image_url' value='{html.escape(defaults['image'])}' placeholder='https://...'></label></div>
        <div class='toolbar'><button class='primary' type='submit'>{'Send announcement' if is_announcement else 'Send embed'}</button><a class='btn secondary' href='/guild/{guild.id}'>Cancel</a></div>
      </section>
      <section class='card'>
        <h3>Discord preview</h3><p class='muted'>The preview mirrors the text, accent, images, and footer before you send.</p>
        <div class='preview-shell'><div class='preview-message' id='contentPreview'></div><div class='preview-box' id='previewBox'><img class='preview-thumb' id='previewThumb' style='display:none'><div class='preview-title' id='previewTitle'></div><div class='preview-desc' id='previewDesc'></div><img class='preview-img' id='previewImg' style='display:none'><div class='preview-footer' id='previewFooter'></div></div></div>
      </section>
    </form>
    <script>
    const contentInput=document.getElementById('contentInput'), embedEnabled=document.getElementById('embedEnabled'), titleInput=document.getElementById('titleInput'), messageInput=document.getElementById('messageInput'), colorInput=document.getElementById('colorInput'), footerInput=document.getElementById('footerInput'), imageInput=document.getElementById('imageInput'), thumbInput=document.getElementById('thumbInput');
    const contentPreview=document.getElementById('contentPreview'), box=document.getElementById('previewBox'), pTitle=document.getElementById('previewTitle'), pDesc=document.getElementById('previewDesc'), pFooter=document.getElementById('previewFooter'), pImg=document.getElementById('previewImg'), pThumb=document.getElementById('previewThumb');
    function cleanHex(v){{v=(v||'A855F7').replace('#','').trim();return /^[0-9a-fA-F]{{6}}$/.test(v)?v:'A855F7'}}
    function setImg(el,url){{url=(url||'').trim();if(url){{el.src=url;el.style.display='block'}}else{{el.removeAttribute('src');el.style.display='none'}}}}
    function updatePreview(){{const top=(contentInput.value||'').trim();contentPreview.textContent=top||'No top message';contentPreview.style.display=top?'block':'none';box.style.display=embedEnabled.checked?'block':'none';pTitle.textContent=titleInput.value||'Untitled';pDesc.textContent=messageInput.value||'';pFooter.textContent=footerInput.value||'';box.style.borderLeftColor='#'+cleanHex(colorInput.value);setImg(pImg,imageInput.value);setImg(pThumb,thumbInput.value)}}
    [contentInput,embedEnabled,titleInput,messageInput,colorInput,footerInput,imageInput,thumbInput].forEach(el=>el.addEventListener('input',updatePreview));embedEnabled.addEventListener('change',updatePreview);updatePreview();
    </script>"""
    return page(title, body)


async def announcement_page(request: web.Request) -> web.Response:
    user = await get_dashboard_user(request)
    guild_id = int(request.match_info["guild_id"])
    if not user or not await dashboard_can_access(user, guild_id):
        raise web.HTTPForbidden(text=owner_private_message())
    guild = bot.get_guild(guild_id)
    if not guild:
        return page("Missing server", "<section class='card'><h1>Bot is not in this server</h1></section>")
    config = await get_guild_config(guild_id)
    owner = await get_owner_settings()
    if (config.get("feature_announcements") is False or owner.get("announcement_sender_enabled") is False) and not is_owner_user(int(user["user_id"])):
        raise web.HTTPForbidden(text="Announcement tools are disabled.")
    return composer_page(guild, "announcement", config, request.query.get("sent") == "1")


async def embed_page(request: web.Request) -> web.Response:
    user = await get_dashboard_user(request)
    guild_id = int(request.match_info["guild_id"])
    if not user or not await dashboard_can_access(user, guild_id):
        raise web.HTTPForbidden(text=owner_private_message())
    guild = bot.get_guild(guild_id)
    if not guild:
        return page("Missing server", "<section class='card'><h1>Bot is not in this server</h1></section>")
    config = await get_guild_config(guild_id)
    owner = await get_owner_settings()
    if (config.get("feature_announcements") is False or owner.get("announcement_sender_enabled") is False) and not is_owner_user(int(user["user_id"])):
        raise web.HTTPForbidden(text="Embed tools are disabled.")
    return composer_page(guild, "embed", config, request.query.get("sent") == "1")


async def send_composer(request: web.Request, mode: str) -> web.Response:
    user = await get_dashboard_user(request)
    guild_id = int(request.match_info["guild_id"])
    if not user or not await dashboard_can_access(user, guild_id):
        raise web.HTTPForbidden(text=owner_private_message())
    guild = bot.get_guild(guild_id)
    if not guild:
        return page("Missing server", "<section class='card'><h1>Bot is not in this server</h1></section>")
    config = await get_guild_config(guild_id)
    owner = await get_owner_settings()
    if (config.get("feature_announcements") is False or owner.get("announcement_sender_enabled") is False) and not is_owner_user(int(user["user_id"])):
        raise web.HTTPForbidden(text="Message composer is disabled.")
    if rate_limiter.on_cooldown(f"dashboard_send:{guild_id}:{user['user_id']}:{mode}", DASHBOARD_SEND_COOLDOWN_SECONDS):
        return page("Slow down", "<section class='card'><h1>Slow down</h1><p class='muted'>Wait a few seconds before sending another dashboard message.</p></section>")
    data = await request.post()
    raw_channel = str(data.get("channel_id", "0")).strip()
    if not raw_channel.isdigit():
        return page("Invalid channel", "<section class='card'><h1>Invalid channel</h1><p class='muted'>Choose a valid text channel.</p></section>")
    channel = guild.get_channel(int(raw_channel))
    if not isinstance(channel, discord.TextChannel):
        return page("Invalid channel", "<section class='card'><h1>Invalid channel</h1><p class='muted'>Choose a text channel the bot can send messages in.</p></section>")

    content = str(data.get("content") or "").strip()[:1900]
    embed_enabled = data.get("embed_enabled") == "on"
    embed = None
    title = str(data.get("title") or ("Announcement" if mode == "announcement" else "Embed"))[:256]
    if embed_enabled:
        message = str(data.get("message") or "").strip()[:4000]
        footer = str(data.get("footer") or config.get("brand_footer") or "moealturej")[:2048]
        image_url = str(data.get("image_url") or "").strip()[:500]
        thumbnail_url = str(data.get("thumbnail_url") or "").strip()[:500]
        for label, value in (("image", image_url), ("thumbnail", thumbnail_url)):
            if value and not is_http_url(value):
                return page("Invalid image URL", f"<section class='card'><h1>Invalid {label} URL</h1><p class='muted'>Use a complete http:// or https:// image URL.</p></section>")
        color = parse_embed_color(str(data.get("color") or config.get("brand_color") or ""))
        embed = make_branded_embed(config, title, message or " ", color)
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        if image_url:
            embed.set_image(url=image_url)
        if footer:
            embed.set_footer(text=footer)

    if not content and not embed:
        return page("Nothing to send", "<section class='card'><h1>Nothing to send</h1><p class='muted'>Add a top message, enable the embed, or both.</p></section>")
    allow_mentions = mode == "announcement" and data.get("allow_mentions") == "on"
    mentions = discord.AllowedMentions(users=True, roles=True, everyone=True) if allow_mentions else discord.AllowedMentions.none()
    sent_message = await safe_channel_send(channel, content=content or None, embed=embed, allowed_mentions=mentions)
    if not sent_message:
        return page("Send failed", "<section class='card'><h1>Discord rejected the send</h1><p class='muted'>The bot hit a temporary Discord limit or lacks permission. Try again shortly.</p></section>")
    await save_event("dashboard_events", {"guild_id": guild.id, "user_id": int(user["user_id"]), "event": f"send_{mode}", "channel_id": channel.id, "title": title, "has_content": bool(content), "has_embed": bool(embed), "mentions_enabled": allow_mentions})
    raise web.HTTPFound(f"/guild/{guild.id}/{'announcements' if mode == 'announcement' else 'embeds'}?sent=1")


async def announcement_send(request: web.Request) -> web.Response:
    return await send_composer(request, "announcement")


async def embed_send(request: web.Request) -> web.Response:
    return await send_composer(request, "embed")

def member_select_options(guild: discord.Guild) -> str:
    """Build a manageable cached-member selector for the dashboard DM tool."""
    members = sorted(
        [m for m in guild.members if not m.bot],
        key=lambda m: (m.display_name or m.name).lower(),
    )[:500]
    out = ["<option value=''>Type/paste a Discord user ID or choose a cached member</option>"]
    for member in members:
        label = f"{member.display_name} (@{member.name}) — {member.id}"
        out.append(f"<option value='{member.id}'>{html.escape(label)}</option>")
    return "".join(out)


async def dm_page(request: web.Request) -> web.Response:
    user = await get_dashboard_user(request)
    guild_id = int(request.match_info["guild_id"])
    if not user or not await dashboard_can_access(user, guild_id):
        raise web.HTTPForbidden(text=owner_private_message())
    guild = bot.get_guild(guild_id)
    if not guild:
        return page("Missing server", "<section class='card'><h1>Bot is not in this server</h1></section>")
    config = await get_guild_config(guild_id)
    owner = await get_owner_settings()
    if (config.get("feature_dms") is False or owner.get("dm_sender_enabled") is False) and not is_owner_user(int(user["user_id"])):
        raise web.HTTPForbidden(text="DM sender is disabled.")
    sent = request.query.get("sent") == "1"
    failed = request.query.get("failed") == "1"
    reason = request.query.get("reason", "")[:180]
    default_color = str(config.get("brand_color") or "A855F7").lstrip("#")
    brand_name = str(config.get("brand_name") or "moealturej")[:60]
    default_footer = str(config.get("brand_footer") or brand_name)[:150]
    default_title = f"Message from {brand_name}"[:256]
    body = f"""
    <section class='hero compact-hero'><span class='eyebrow'>Private messaging</span><h1>DM Composer.</h1><p class='muted'>Send a controlled private message using this server's branding. User, role, and everyone mentions are suppressed for safer production use.</p></section>
    {'<div class="notice success">Private message delivered successfully.</div>' if sent else ''}
    {'<div class="notice" style="border-color:rgba(251,113,133,.28);background:rgba(251,113,133,.08)"><strong>DM failed.</strong> ' + html.escape(reason or 'The user may have DMs disabled, blocked the bot, or the ID may be invalid.') + '</div>' if failed else ''}
    <div class='section-title'><div><span class='eyebrow'>Direct message</span><h2 style='margin-top:8px'>Compose message</h2></div><a class='btn secondary' href='/guild/{guild.id}'>Back to server</a></div>
    <form class='grid' method='post'>
      <section class='card'>
        <div class='form-section'><h3>Recipient</h3><p class='muted'>Choose a cached member or paste an exact Discord user ID.</p></div>
        <label>Cached member<select id='memberSelect'>{member_select_options(guild)}</select></label>
        <label>Discord user ID<input id='userIdInput' name='user_id' inputmode='numeric' pattern='[0-9]{{15,25}}' autocomplete='off' placeholder='1222903158125105194' required></label>
        <div class='form-section'><h3>Message</h3><p class='muted'>Plain text is sent above the embed. You can send either part by itself.</p></div>
        <label>Top message<textarea id='plainInput' name='plain_message' maxlength='1900' placeholder='Optional text shown above the embed'></textarea></label>
        <label style='display:flex;align-items:center;gap:10px;margin:12px 0'><input id='embedEnabled' name='embed_enabled' type='checkbox' checked style='width:auto;margin:0'> Include branded embed</label>
        <label>Embed title<input id='titleInput' name='title' maxlength='256' value='{html.escape(default_title)}'></label>
        <label>Embed description<textarea id='messageInput' name='message' maxlength='4000'>Write your private message here...</textarea></label>
        <div class='row'><label>Color hex<input id='colorInput' name='color' maxlength='7' value='{html.escape(default_color)}' placeholder='A855F7'></label><label>Footer<input id='footerInput' name='footer' maxlength='2048' value='{html.escape(default_footer)}'></label></div>
        <div class='row'><label>Thumbnail image URL<input id='thumbInput' name='thumbnail_url' maxlength='500' placeholder='https://...'></label><label>Large image URL<input id='imageInput' name='image_url' maxlength='500' placeholder='https://...'></label></div>
        <p class='tiny'>Only complete http:// or https:// image URLs are accepted. Discord mentions are disabled in both the plain message and embed.</p>
        <div class='toolbar'><button class='primary' type='submit'>Send private DM</button><a class='btn secondary' href='/guild/{guild.id}'>Cancel</a></div>
      </section>
      <section class='card'>
        <h3>Discord preview</h3><p class='muted'>Preview the message before sending it to the selected user.</p>
        <div class='preview-shell'>
          <div class='preview-message' id='plainPreview'></div>
          <div class='preview-box' id='previewBox'>
            <img class='preview-thumb' id='previewThumb' style='display:none'>
            <div class='preview-title' id='previewTitle'></div>
            <div class='preview-desc' id='previewDesc'></div>
            <img class='preview-img' id='previewImg' style='display:none'>
            <div class='preview-footer' id='previewFooter'></div>
          </div>
        </div>
      </section>
    </form>
    <script>
    const memberSelect=document.getElementById('memberSelect'), userIdInput=document.getElementById('userIdInput'), plainInput=document.getElementById('plainInput'), embedEnabled=document.getElementById('embedEnabled');
    const titleInput=document.getElementById('titleInput'), messageInput=document.getElementById('messageInput'), colorInput=document.getElementById('colorInput'), footerInput=document.getElementById('footerInput'), imageInput=document.getElementById('imageInput'), thumbInput=document.getElementById('thumbInput');
    const box=document.getElementById('previewBox'), pTitle=document.getElementById('previewTitle'), pDesc=document.getElementById('previewDesc'), pFooter=document.getElementById('previewFooter'), pImg=document.getElementById('previewImg'), pThumb=document.getElementById('previewThumb'), plainPreview=document.getElementById('plainPreview');
    memberSelect.addEventListener('change',()=>{{if(memberSelect.value) userIdInput.value=memberSelect.value;}});
    function cleanHex(v){{v=(v||'{html.escape(default_color)}').replace('#','').trim(); return /^[0-9a-fA-F]{{3}}$|^[0-9a-fA-F]{{6}}$/.test(v)?v:'{html.escape(default_color)}'}}
    function setImg(el,url){{url=(url||'').trim(); const u=url.toLowerCase(); if(u.startsWith('http://')||u.startsWith('https://')){{el.src=url;el.style.display='block'}}else{{el.removeAttribute('src');el.style.display='none'}}}}
    function updatePreview(){{
      const plain=(plainInput.value||'').trim(); plainPreview.textContent=plain; plainPreview.style.display=plain?'block':'none';
      box.style.display=embedEnabled.checked?'block':'none'; pTitle.textContent=titleInput.value||'Untitled'; pDesc.textContent=messageInput.value||''; pFooter.textContent=footerInput.value||''; box.style.borderLeftColor='#'+cleanHex(colorInput.value); setImg(pImg,imageInput.value); setImg(pThumb,thumbInput.value);
    }}
    [plainInput,titleInput,messageInput,colorInput,footerInput,imageInput,thumbInput].forEach(el=>el.addEventListener('input',updatePreview)); embedEnabled.addEventListener('change',updatePreview); updatePreview();
    </script>
    """
    return page("DM Sender", body)


async def dm_send(request: web.Request) -> web.Response:
    user = await get_dashboard_user(request)
    guild_id = int(request.match_info["guild_id"])
    if not user or not await dashboard_can_access(user, guild_id):
        raise web.HTTPForbidden(text=owner_private_message())
    guild = bot.get_guild(guild_id)
    if not guild:
        return page("Missing server", "<section class='card'><h1>Bot is not in this server</h1></section>")
    config = await get_guild_config(guild_id)
    owner = await get_owner_settings()
    if (config.get("feature_dms") is False or owner.get("dm_sender_enabled") is False) and not is_owner_user(int(user["user_id"])):
        raise web.HTTPForbidden(text="DM sender is disabled.")
    if rate_limiter.on_cooldown(f"dashboard_dm:{guild_id}:{user['user_id']}", DASHBOARD_SEND_COOLDOWN_SECONDS):
        raise web.HTTPFound(f"/guild/{guild_id}/dms?failed=1&reason=Wait+a+few+seconds+before+sending+another+DM")
    data = await request.post()
    raw_user_id = str(data.get("user_id", "")).strip()
    if not raw_user_id.isdigit() or not (15 <= len(raw_user_id) <= 25):
        raise web.HTTPFound(f"/guild/{guild.id}/dms?failed=1&reason=Invalid+Discord+user+ID")
    target_id = int(raw_user_id)
    plain_message = str(data.get("plain_message") or "").strip()[:1900]
    embed_enabled = data.get("embed_enabled") == "on"
    embed = None
    if embed_enabled:
        brand_name = str(config.get("brand_name") or "moealturej")[:60]
        title = str(data.get("title") or f"Message from {brand_name}")[:256]
        message = str(data.get("message") or "").strip()[:4000]
        footer = str(data.get("footer") or config.get("brand_footer") or brand_name)[:2048]
        image_url = str(data.get("image_url") or "").strip()[:500]
        thumbnail_url = str(data.get("thumbnail_url") or "").strip()[:500]
        for label, value in (("image", image_url), ("thumbnail", thumbnail_url)):
            if value and not is_http_url(value):
                raise web.HTTPFound(f"/guild/{guild.id}/dms?failed=1&reason=Invalid+{label}+URL.+Use+http+or+https")
        color = parse_embed_color(str(data.get("color") or config.get("brand_color") or ""))
        embed = make_branded_embed(config, title, message or " ", color)
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        if image_url:
            embed.set_image(url=image_url)
        if footer:
            embed.set_footer(text=footer)
    if not plain_message and not embed:
        raise web.HTTPFound(f"/guild/{guild.id}/dms?failed=1&reason=Write+a+plain+message+or+enable+an+embed+first")

    try:
        target = guild.get_member(target_id)
        if target is None:
            try:
                target = await safe_fetch_member(guild, target_id)
            except discord.NotFound:
                target = None
            if target is None:
                target = await safe_fetch_user(target_id)
        sent_dm = await safe_user_send(target, content=plain_message or None, embed=embed, allowed_mentions=discord.AllowedMentions.none()) if target else False
        if not sent_dm:
            await save_event("dashboard_events", {"guild_id": guild.id, "user_id": int(user["user_id"]), "event": "send_dm_failed", "target_user_id": target_id, "reason": "blocked_missing_or_rate_limited"})
            raise web.HTTPFound(f"/guild/{guild.id}/dms?failed=1&reason=DM+blocked,+target+missing,+or+temporarily+rate+limited")
    except discord.Forbidden:
        await save_event("dashboard_events", {"guild_id": guild.id, "user_id": int(user["user_id"]), "event": "send_dm_failed", "target_user_id": target_id, "reason": "forbidden"})
        raise web.HTTPFound(f"/guild/{guild.id}/dms?failed=1&reason=That+user+has+DMs+disabled+or+blocked+bot+DMs")
    except discord.HTTPException as exc:
        await save_event("dashboard_events", {"guild_id": guild.id, "user_id": int(user["user_id"]), "event": "send_dm_failed", "target_user_id": target_id, "reason": str(exc)[:300]})
        raise web.HTTPFound(f"/guild/{guild.id}/dms?failed=1&reason=Discord+rejected+the+DM+request")

    await save_event("dashboard_events", {"guild_id": guild.id, "user_id": int(user["user_id"]), "event": "send_dm", "target_user_id": target_id, "has_plain": bool(plain_message), "has_embed": bool(embed)})
    raise web.HTTPFound(f"/guild/{guild.id}/dms?sent=1")

async def activity_page(request: web.Request) -> web.Response:
    user = await get_dashboard_user(request)
    guild_id = int(request.match_info["guild_id"])
    if not user or not await dashboard_can_access(user, guild_id):
        raise web.HTTPForbidden(text=owner_private_message())
    guild = bot.get_guild(guild_id)
    if not guild:
        return page("Missing server", "<section class='card'><h1>Bot is not in this server</h1></section>")
    events = []
    for collection in ("error_events", "dashboard_events", "moderation_events", "ticket_events", "verification_events"):
        async for item in mdb[collection].find({"guild_id": guild_id}, {"_id": 0}).sort("created_at", -1).limit(30):
            item["source"] = collection.replace("_events", "")
            events.append(item)
    events.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
    rows = []
    for item in events[:75]:
        actor = item.get("user_id") or item.get("moderator_id") or item.get("closed_by") or "—"
        detail = item.get("incident_id") or item.get("reason") or item.get("message") or item.get("title") or item.get("ticket_type") or item.get("status") or "—"
        rows.append(f"<tr><td>{html.escape(str(item.get('created_at',''))[:19].replace('T',' '))}</td><td>{html.escape(str(item.get('source','')))}</td><td>{html.escape(str(item.get('event','activity')))}</td><td>{html.escape(str(actor))}</td><td>{html.escape(str(detail))[:180]}</td></tr>")
    warning_count = await mdb.warnings.count_documents({"guild_id": guild_id})
    open_tickets = len((await get_guild_config(guild_id)).get("open_tickets", {}))
    body = f"""<section class='hero'><span class='pill'>📊 Operations center</span><h1>{html.escape(guild.name)} activity</h1><p class='muted'>One place to check bot health, recent actions, support load, and safety events.</p><div class='stats'><div class='stat'><span class='muted'>Latency</span><b>{round(bot.latency*1000) if bot.latency else '—'} ms</b></div><div class='stat'><span class='muted'>Members</span><b>{guild.member_count or len(guild.members)}</b></div><div class='stat'><span class='muted'>Open tickets</span><b>{open_tickets}</b></div><div class='stat'><span class='muted'>Warnings</span><b>{warning_count}</b></div></div></section><div class='section-title'><h2>Recent activity</h2><a class='btn secondary' href='/guild/{guild_id}'>Back to settings</a></div><section class='card'><div class='table-wrap'><table><thead><tr><th>Time (UTC)</th><th>Source</th><th>Action</th><th>Actor</th><th>Details</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan=5>No recorded activity yet.</td></tr>'}</tbody></table></div></section>"""
    return page(f"{guild.name} Activity", body)


async def verify_start(request: web.Request) -> web.Response:
    try:
        guild_id = int(request.query.get("guild_id", "0"))
        requested_user_id = int(request.query.get("user_id", "0"))
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid verification link.")
    guild = bot.get_guild(guild_id)
    if not guild:
        return page("Verification", "<section class='card'><h1>Server unavailable</h1><p class='muted'>The bot is not connected to this server.</p></section>")
    if not requested_user_id:
        return page("Verification", f"<section class='hero compact-hero'><span class='pill'>Secure verification</span><h1>Start inside Discord</h1><p class='muted'>Return to <b>{html.escape(guild.name)}</b> and click its verification button. Direct or copied links are intentionally blocked.</p></section>")

    config = await get_guild_config(guild_id)
    if config.get("feature_verification") is False:
        return page("Verification paused", "<section class='hero compact-hero'><span class='eyebrow'>Unavailable</span><h1>Verification is paused.</h1><p class='muted'>An administrator has temporarily disabled this module.</p></section>")
    member = guild.get_member(requested_user_id) or await safe_fetch_member(guild, requested_user_id)
    if member:
        verified_role = guild.get_role(int(config.get("verified_role") or 0)) if config.get("verified_role") else None
        if verified_role and verified_role in member.roles:
            removed = await safe_remove_role(member, config.get("unverified_role"), "Already verified cleanup from web guard")
            await log_verification(guild, member, "web-precheck", "already_verified", "OAuth start blocked because member already had the verified role." + (" Unverified role removed." if removed else ""))
            return page("Already verified", f"<section class='hero compact-hero'><span class='pill'>Already verified</span><h1>No action needed</h1><p class='muted'>You already have access to {html.escape(guild.name)}.</p></section>")
    elif config.get("verification_require_member") or not config.get("oauth_verify_join_enabled", True):
        return page("Membership required", f"<section class='card'><h1>Join the server first</h1><p class='muted'>This server only verifies current members. Join {html.escape(guild.name)}, then press Verify again.</p></section>")

    state = secrets.token_urlsafe(32)
    await mdb.oauth_states.insert_one({
        "state": state,
        "type": "verify",
        "guild_id": guild_id,
        "requested_user_id": requested_user_id,
        "created_at": utcnow(),
        "expires_at": utcnow() + timedelta(minutes=10),
    })
    scopes = ["identify"]
    if config.get("oauth_verify_join_enabled", True):
        scopes.append("guilds.join")
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": f"{PUBLIC_BASE_URL}/verify/callback",
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
    }
    raise web.HTTPFound(f"https://discord.com/oauth2/authorize?{urlencode(params)}")


async def verify_callback(request: web.Request) -> web.Response:
    state = request.query.get("state", "")
    code = request.query.get("code", "")
    oauth_error = request.query.get("error", "")
    found = await mdb.oauth_states.find_one_and_delete({"state": state, "type": "verify", "expires_at": {"$gt": utcnow()}})
    if not found:
        raise web.HTTPBadRequest(text="This verification link is invalid, expired, or already used.")
    if oauth_error or not code:
        return page("Verification cancelled", "<section class='card'><h1>Verification cancelled</h1><p class='muted'>No roles were changed. Return to Discord when you are ready to try again.</p></section>")

    guild_id = int(found["guild_id"])
    requested_user_id = int(found["requested_user_id"])
    guild = bot.get_guild(guild_id)
    if not guild:
        return page("Verification", "<section class='card'><h1>Server unavailable</h1></section>")
    config = await get_guild_config(guild_id)
    if config.get("feature_verification") is False:
        return page("Verification paused", "<section class='hero compact-hero'><span class='eyebrow'>Unavailable</span><h1>Verification is paused.</h1><p class='muted'>No roles were changed.</p></section>")

    token = await exchange_code(code, f"{PUBLIC_BASE_URL}/verify/callback")
    user = await discord_get("/users/@me", token["access_token"])
    user_id = int(user["id"])
    discord_user = await safe_fetch_user(user_id)

    # Critical anti-link-sharing check: the OAuth account must be the member
    # who clicked the private verification button.
    if user_id != requested_user_id:
        if discord_user:
            await log_verification(guild, discord_user, "oauth2", "identity_mismatch", f"Expected user {requested_user_id}, received {user_id}. No roles changed.")
        return page("Wrong Discord account", f"<section class='card'><h1>Account mismatch</h1><p class='muted'>This link belongs to a different Discord account. Sign into the account that clicked Verify and request a fresh link.</p><code>Expected: {requested_user_id}</code></section>")

    minimum_days = max(0, int(config.get("verification_min_account_days", 0)))
    account_created = discord.utils.snowflake_time(user_id)
    account_age = utcnow() - account_created
    if account_age < timedelta(days=minimum_days):
        if discord_user:
            await log_verification(guild, discord_user, "oauth2", "account_too_new", f"Account age {account_age.days}d; minimum {minimum_days}d.")
        return page("Account too new", f"<section class='card'><h1>Account age requirement</h1><p class='muted'>This server requires Discord accounts to be at least <b>{minimum_days} day(s)</b> old. Your account is approximately <b>{account_age.days} day(s)</b> old.</p></section>")

    member = guild.get_member(user_id) or await safe_fetch_member(guild, user_id)
    if member is None and config.get("verification_require_member"):
        return page("Membership required", f"<section class='card'><h1>Join the server first</h1><p class='muted'>Join {html.escape(guild.name)}, then request a new verification link.</p></section>")

    if member is None and config.get("oauth_verify_join_enabled", True):
        status, _ = await discord_put(f"/guilds/{guild_id}/members/{user_id}", BOT_TOKEN, {"access_token": token["access_token"]})
        if status not in {201, 204}:
            return page("Join failed", "<section class='card'><h1>Could not join the server</h1><p class='muted'>Discord did not accept the server join. Join manually and try again.</p></section>")
        for attempt in range(4):
            await asyncio.sleep(0.75 + attempt * 0.35)
            member = guild.get_member(user_id) or await safe_fetch_member(guild, user_id)
            if member:
                break
    if member is None:
        return page("Verification delayed", "<section class='card'><h1>Discord is still processing</h1><p class='muted'>Return to Discord and request a new verification link in a minute.</p></section>")

    verified_role_id = config.get("verified_role")
    verified_role = guild.get_role(int(verified_role_id or 0)) if verified_role_id else None
    if not verified_role:
        await log_verification(guild, member, "oauth2", "configuration_error", "Verified role is missing.")
        return page("Setup incomplete", "<section class='card'><h1>Verification is not configured</h1><p class='muted'>An administrator must select a verified role.</p></section>")
    if not guild.me.guild_permissions.manage_roles or verified_role >= guild.me.top_role:
        await log_verification(guild, member, "oauth2", "role_hierarchy_error", "Bot cannot manage verified role.")
        return page("Role error", "<section class='card'><h1>Role hierarchy needs attention</h1><p class='muted'>The bot role must be above the verified role and have Manage Roles.</p></section>")

    already_verified = verified_role in member.roles
    role_ok = already_verified or await safe_add_role(member, verified_role_id, "User completed secure OAuth2 verification")
    removed_unverified = await safe_remove_role(member, config.get("unverified_role"), "User completed secure OAuth2 verification")
    details = "OAuth identity matched the requesting member. "
    details += "User already had the verified role." if already_verified else ("Verified role assigned." if role_ok else "Verified role assignment failed.")
    if removed_unverified:
        details += " Unverified role removed."

    if role_ok:
        await mdb.verified_members.update_one(
            {"guild_id": guild_id, "user_id": user_id},
            {"$set": {"username": str(member), "verified_at": utcnow(), "method": "oauth2", "account_created_at": account_created}},
            upsert=True,
        )
        await send_verified_dm(member, config)
    await log_verification(guild, member, "oauth2", "success" if role_ok else "failed", details)
    success_message = html.escape(str(config.get("verification_success_message") or DEFAULT_GUILD_CONFIG["verification_success_message"]))
    return page("Verified", f"<section class='hero compact-hero'><span class='pill'>Verification complete</span><h1>{'Already verified' if already_verified else 'Access unlocked'}</h1><p class='muted'>{success_message}</p><p class='muted'>You may close this tab and return to {html.escape(guild.name)}.</p></section>")


@web.middleware
async def security_error_middleware(request: web.Request, handler):
    request_id = secrets.token_hex(4).upper()
    try:
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).netloc != urlparse(PUBLIC_BASE_URL).netloc:
                raise web.HTTPForbidden(text="Cross-site request blocked.")
            csrf_cookie = request.cookies.get("moe_csrf", "")
            csrf_value = unsign_value(csrf_cookie) if csrf_cookie else None
            supplied = request.headers.get("X-CSRF-Token", "")
            if request.content_type in {"application/x-www-form-urlencoded", "multipart/form-data"}:
                form = await request.post()
                supplied = str(form.get("_csrf") or supplied)
            if not csrf_value or not csrf_value.startswith("csrf_") or not supplied or not hmac.compare_digest(csrf_cookie, supplied):
                raise web.HTTPForbidden(text="Security token expired. Refresh the page and try again.")
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
    except Exception as exc:
        incident = await report_exception(
            "web_request",
            exc,
            user_id=None,
            details={"request_id": request_id, "method": request.method, "path": request.path},
        )
        response = page(
            "Something went wrong",
            f"<section class='card'><span class='pill'>Request failed</span><h1>That action could not be completed</h1><p class='muted'>The error was logged safely. Try again, then use this reference if it continues.</p><code>{incident}</code></section>",
        )
        response.set_status(500)
    if request.method == "GET" and not request.cookies.get("moe_csrf"):
        csrf = sign_value("csrf_" + secrets.token_urlsafe(24))
        response.set_cookie("moe_csrf", csrf, max_age=WEB_SESSION_DAYS * 86400, secure=PUBLIC_BASE_URL.startswith("https://"), httponly=False, samesite="Strict")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' https: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://discord.com"
    response.headers["X-Request-ID"] = request_id
    if response.content_type == "text/html":
        response.headers["Cache-Control"] = "no-store, private"
    return response

async def status_page(request: web.Request) -> web.Response:
    uptime = utcnow() - STARTED_AT
    startup_wait = max(0, int((startup_blocked_until - utcnow()).total_seconds())) if startup_blocked_until else 0
    mongo_ok = False
    try:
        if mdb is not None:
            await asyncio.wait_for(mdb.command("ping"), timeout=2.0)
            mongo_ok = True
    except Exception:
        mongo_ok = False
    bot_ready = bot.is_ready()
    overall = bot_ready and mongo_ok
    uptime_seconds = int(uptime.total_seconds())
    days, rem = divmod(uptime_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    uptime_label = f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"
    discord_text = "Operational" if bot_ready else ("Waiting / retrying" if startup_wait else "Starting")
    database_text = "Operational" if mongo_ok else "Unavailable"
    cooldown = round(rate_limiter.seconds_until_unblocked())
    notice = "All core systems are operational." if overall else "One or more services need attention. The bot will continue retrying safe recoverable connections."
    body = f"""
    <section class='hero compact-hero'><span class='eyebrow'>System status</span><h1>{'All systems operational.' if overall else 'Service degraded.'}</h1><p class='muted'>{html.escape(notice)}</p><div class='stats'><div class='stat'><span>Discord</span><b style='font-size:17px'>{html.escape(discord_text)}</b></div><div class='stat'><span>Database</span><b style='font-size:17px'>{html.escape(database_text)}</b></div><div class='stat'><span>Uptime</span><b style='font-size:17px'>{html.escape(uptime_label)}</b></div><div class='stat'><span>Latency</span><b>{round(bot.latency*1000) if bot.latency else '—'}<small> ms</small></b></div></div></section>
    <div class='section-title'><div><span class='eyebrow'>Runtime</span><h2 style='margin-top:8px'>Production health</h2></div><a class='btn secondary' href='/health'>JSON health</a></div>
    <div class='grid'><section class='card'><div class='card-row'><div><h3>Discord gateway</h3><p class='muted'>Connection and command runtime.</p></div><span class='pill'>{html.escape(discord_text)}</span></div><p class='tiny'>Connected guilds: {len(bot.guilds)} • Global API cooldown: {cooldown}s</p></section><section class='card'><div class='card-row'><div><h3>MongoDB</h3><p class='muted'>Persistent settings, sessions, tickets, and activity.</p></div><span class='pill'>{html.escape(database_text)}</span></div><p class='tiny'>Database probe uses a short timeout and does not expose credentials.</p></section><section class='card'><div class='card-row'><div><h3>Build</h3><p class='muted'>Currently deployed application version.</p></div><code>{html.escape(BUILD_VERSION)}</code></div><p class='tiny'>Startup retry: {startup_wait}s remaining{' • ' + html.escape(last_startup_error or '') if last_startup_error else ''}</p></section></div>
    """
    return page("System Status", body)


async def health(request: web.Request) -> web.Response:
    uptime = utcnow() - STARTED_AT
    startup_wait = 0
    if startup_blocked_until:
        startup_wait = max(0, int((startup_blocked_until - utcnow()).total_seconds()))
    mongo_ok = False
    try:
        if mdb is not None:
            await asyncio.wait_for(mdb.command("ping"), timeout=2.0)
            mongo_ok = True
    except Exception:
        mongo_ok = False
    bot_ready = bot.is_ready()
    status = "ready" if bot_ready and mongo_ok else "degraded"
    return web.json_response({
        "status": status,
        "version": BUILD_VERSION,
        "bot": str(bot.user) if bot.user else ("waiting_for_discord" if startup_wait else "starting"),
        "discord_ready": bot_ready,
        "database_ready": mongo_ok,
        "guilds": len(bot.guilds),
        "latency_ms": round(bot.latency * 1000) if bot.latency else None,
        "uptime_seconds": int(uptime.total_seconds()),
        "discord_global_cooldown_seconds": round(rate_limiter.seconds_until_unblocked()),
        "startup_retry_seconds": startup_wait,
        "last_startup_error": last_startup_error,
    })


async def start_web() -> None:
    global web_runner
    if web_runner:
        return
    app = web.Application(client_max_size=8 * 1024 ** 2, middlewares=[security_error_middleware])
    app.router.add_get("/", home)
    app.router.add_get("/owner", owner_page)
    app.router.add_post("/owner", owner_save)
    app.router.add_get("/login", login)
    app.router.add_get("/oauth/callback", oauth_callback)
    app.router.add_get("/logout", logout)
    app.router.add_get("/guild/{guild_id}", guild_page)
    app.router.add_post("/guild/{guild_id}", guild_save)
    app.router.add_get("/guild/{guild_id}/commands", command_settings_page)
    app.router.add_post("/guild/{guild_id}/commands", command_settings_save)
    app.router.add_get("/guild/{guild_id}/announcements", announcement_page)
    app.router.add_post("/guild/{guild_id}/announcements", announcement_send)
    app.router.add_get("/guild/{guild_id}/embeds", embed_page)
    app.router.add_post("/guild/{guild_id}/embeds", embed_send)
    app.router.add_get("/guild/{guild_id}/dms", dm_page)
    app.router.add_get("/guild/{guild_id}/activity", activity_page)
    app.router.add_post("/guild/{guild_id}/dms", dm_send)
    app.router.add_get("/verify/start", verify_start)
    app.router.add_get("/verify/callback", verify_callback)
    app.router.add_get("/status", status_page)
    app.router.add_get("/health", health)
    web_runner = web.AppRunner(app, access_log=log)
    await web_runner.setup()
    await web.TCPSite(web_runner, WEB_HOST, WEB_PORT).start()
    log.info("Dashboard running on http://%s:%s", WEB_HOST, WEB_PORT)

# =========================
# EVENTS / TASKS
# =========================
@bot.event
async def setup_hook():
    await init_mongo()
    await start_web()


@bot.event
async def on_ready():
    global views_added, commands_synced

    # on_ready can run again after Discord reconnects. Do not re-add persistent
    # views or re-sync slash commands on every reconnect. Repeated sync/restart
    # loops can push the bot into Discord's global 429 rate limit.
    if not views_added:
        bot.add_view(TicketPanelView())
        bot.add_view(CloseTicketView())
        bot.add_view(OAuthVerifyView(0))
        views_added = True

    # Keep this OFF on Render unless you intentionally changed slash commands.
    # To sync once after command edits, set SYNC_COMMANDS=true for one deploy,
    # then set it back to false.
    if SYNC_COMMANDS and not commands_synced:
        try:
            synced = await bot.tree.sync()
            commands_synced = True
            print(f"Synced {len(synced)} slash commands.")
        except Exception as e:
            print(f"Slash command sync failed: {e}")

    if not rotate_status.is_running(): rotate_status.start()
    if not update_stats.is_running(): update_stats.start()

    # Self-pinging can keep free web services in a restart/login loop. Leave it
    # disabled by default. Use an external uptime monitor only after the bot is stable.
    if ENABLE_SELF_PING and KEEP_ALIVE_URL and not self_ping.is_running():
        self_ping.start()

    print(f"Logged in as {bot.user}")


@bot.event
async def on_member_join(member: discord.Member):
    try:
        config = await get_guild_config(member.guild.id)
        verified_role = member.guild.get_role(int(config.get("verified_role") or 0)) if config.get("verified_role") else None
        if config.get("feature_verification", True) and config.get("unverified_role") and not (verified_role and verified_role in member.roles):
            await safe_add_role(member, config.get("unverified_role"), "Unverified role on join")
        if config.get("feature_welcome", True) and config.get("auto_role"):
            await safe_add_role(member, config.get("auto_role"), "Auto role on join")
        channel = member.guild.get_channel(int(config.get("welcome_channel") or 0))
        if config.get("feature_welcome", True) and config.get("welcome_enabled", True) and isinstance(channel, discord.TextChannel) and not rate_limiter.on_cooldown(f"welcome:{member.guild.id}", MEMBER_JOIN_WELCOME_COOLDOWN_SECONDS):
            title = render_template(str(config.get("welcome_title") or DEFAULT_GUILD_CONFIG["welcome_title"]), guild=member.guild, member=member)[:256]
            message = render_template(str(config.get("welcome_message") or DEFAULT_GUILD_CONFIG["welcome_message"]), guild=member.guild, member=member)[:4000]
            embed = make_branded_embed(config, title, message)
            embed.set_thumbnail(url=member.display_avatar.url)
            content = member.mention if config.get("welcome_ping_user") else None
            allowed = discord.AllowedMentions(users=[member] if content else [], roles=False, everyone=False)
            await safe_channel_send(channel, content=content, embed=embed, allowed_mentions=allowed)
    except Exception as exc:
        await report_exception("member_join", exc, guild_id=member.guild.id, user_id=member.id)


@tasks.loop(minutes=5)
async def rotate_status():
    settings = await get_owner_settings()
    interval = max(60, min(3600, int(settings.get("presence_interval_seconds") or 300)))
    if abs(rotate_status.seconds - interval) > 1:
        rotate_status.change_interval(seconds=interval)
    if not settings.get("presence_enabled", True):
        return
    statuses = [str(x).strip() for x in settings.get("presence_statuses", []) if str(x).strip()]
    if not statuses:
        return
    status = statuses[rotate_status.current_loop % len(statuses)]
    kind = str(settings.get("presence_type") or "watching").lower()
    activity_type = {
        "watching": discord.ActivityType.watching,
        "listening": discord.ActivityType.listening,
        "competing": discord.ActivityType.competing,
    }.get(kind)
    activity = discord.Game(name=status) if kind == "playing" else discord.Activity(type=activity_type or discord.ActivityType.watching, name=status)
    await safe_change_presence(status=discord.Status.online, activity=activity)


@tasks.loop(minutes=STATS_UPDATE_MINUTES)
async def update_stats():
    owner = await get_owner_settings()
    stats_interval = max(5, min(360, int(owner.get("stats_interval_minutes") or STATS_UPDATE_MINUTES)))
    if abs(update_stats.minutes - stats_interval) > 0.1:
        update_stats.change_interval(minutes=stats_interval)
    if rate_limiter.is_globally_blocked():
        log.warning("Skipping stats update while Discord global cooldown/circuit breaker is active (%.0fs left)", rate_limiter.seconds_until_unblocked())
        return
    for guild in bot.guilds:
        config = await get_guild_config(guild.id)
        if config.get("feature_stats") is False:
            continue
        channels = config.get("stats_channels", {})
        humans = len([m for m in guild.members if not m.bot])
        bots = len([m for m in guild.members if m.bot])
        members = guild.member_count or len(guild.members)
        boosts = guild.premium_subscription_count or 0
        stats = {
            "members": render_stat_name(config, "members", members, guild),
            "humans": render_stat_name(config, "humans", humans, guild),
            "bots": render_stat_name(config, "bots", bots, guild),
            "boosts": render_stat_name(config, "boosts", boosts, guild),
        }
        for key, name in stats.items():
            channel = guild.get_channel(channels.get(key) or 0)
            if isinstance(channel, discord.VoiceChannel) and channel.name != name:
                try: await safe_channel_edit(channel, name=name, reason="Live server stats update")
                except discord.HTTPException: pass


@tasks.loop(minutes=5)
async def self_ping():
    url = KEEP_ALIVE_URL
    if not url:
        return
    try:
        global http_session
        if http_session is None or http_session.closed:
            http_session = ClientSession(timeout=ClientTimeout(total=20))
        async with http_session.get(url) as response:
            await response.text()
    except (ClientError, asyncio.TimeoutError) as e:
        log.warning("Self-ping failed for %s: %s", url, e)

# =========================
# COMMANDS
# =========================
@bot.tree.command(name="ping", description="Check bot latency.")
@guild_enabled_or_owner()
async def ping(interaction: discord.Interaction):
    config = await get_guild_config(interaction.guild.id) if interaction.guild else DEFAULT_GUILD_CONFIG
    latency_ms = round(bot.latency * 1000) if bot.latency else 0
    template = str(config.get("ping_description") or "Discord latency: `{latency_ms}ms`")
    description = template.replace("{latency_ms}", str(latency_ms))
    uptime = utcnow() - STARTED_AT
    embed = make_branded_embed(config, str(config.get("ping_title") or "System Online"), description, SUCCESS_COLOR)
    embed.add_field(name="Status", value="🟢 Operational", inline=True)
    embed.add_field(name="Uptime", value=f"{int(uptime.total_seconds() // 3600)}h {int((uptime.total_seconds() % 3600) // 60)}m", inline=True)
    embed.add_field(name="Build", value=f"`{BUILD_VERSION}`", inline=True)
    await safe_interaction_send(interaction, embed=embed, ephemeral=True)


@bot.tree.command(name="store", description="Get the store link.")
@guild_enabled_or_owner()
async def store(interaction: discord.Interaction):
    config = await get_guild_config(interaction.guild.id) if interaction.guild else DEFAULT_GUILD_CONFIG
    url = str(config.get("store_url") or DEFAULT_STORE_URL)
    embed = make_branded_embed(config, str(config.get("store_title") or "moealturej Store"), str(config.get("store_description") or "Browse products, downloads, and account tools securely."))
    embed.add_field(name="Store", value=f"[Open website]({url})", inline=False)
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label=str(config.get("store_button_label") or "Open Store")[:80], url=url, style=discord.ButtonStyle.link, emoji="🛍️"))
    await safe_interaction_send(interaction, embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="help", description="Show available commands.")
@guild_enabled_or_owner()
async def help_command(interaction: discord.Interaction):
    config = await get_guild_config(interaction.guild.id) if interaction.guild else DEFAULT_GUILD_CONFIG
    enabled = config.get("command_enabled") or {}
    embed = make_branded_embed(config, str(config.get("help_title") or "Command Center"), str(config.get("help_description") or "Everything you need, organized in one place."))
    public_groups = {
        "Essentials": [("ping", "latency & status"), ("store", "store link"), ("serverinfo", "server overview"), ("userinfo", "member details"), ("avatar", "full-size avatar")],
    }
    for group, items in public_groups.items():
        visible = [f"`/{name}` — {label}" for name, label in items if enabled.get(name, True) and config.get(command_feature_name(name) or "feature_utilities", True)]
        if visible:
            embed.add_field(name=group, value="\n".join(visible), inline=False)
    if config.get("feature_tickets", True):
        embed.add_field(name="Support", value="Use the server's **Support Center** panel to open a private ticket.", inline=False)
    if isinstance(interaction.user, discord.Member) and member_is_command_admin(interaction.user, config) and config.get("feature_admin_commands", True):
        embed.add_field(name="Staff", value="`/commands` opens the private administration command index. The web dashboard contains full configuration.", inline=False)
    await safe_interaction_send(interaction, embed=embed, ephemeral=True)

@bot.tree.command(name="commands", description="Show private owner/admin commands.")
@admin_only()
async def commands_menu(interaction: discord.Interaction):
    config = await get_guild_config(interaction.guild.id)
    enabled = config.get("command_enabled") or {}
    embed = make_branded_embed(config, "Administration", "Private command index. Feature and command availability can be changed from the dashboard.")
    groups: Dict[str, list[str]] = {}
    for name, meta in COMMAND_CATALOG.items():
        if name in {"ping", "store", "help", "serverinfo", "userinfo", "avatar"}:
            continue
        if not enabled.get(name, True) and not is_owner_user(interaction.user.id):
            continue
        groups.setdefault(meta["group"], []).append(f"`/{name}`")
    for group, names in groups.items():
        embed.add_field(name=group, value=" ".join(names)[:1024], inline=False)
    embed.add_field(name="Web control", value=f"[Open dashboard]({PUBLIC_BASE_URL}/guild/{interaction.guild.id})", inline=False)
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open Dashboard", url=f"{PUBLIC_BASE_URL}/guild/{interaction.guild.id}", style=discord.ButtonStyle.link, emoji="⚙️"))
    await safe_interaction_send(interaction, embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="setup_enable", description="Owner: enable or disable this bot in this server.")
@admin_only()
async def setup_enable(interaction: discord.Interaction, enabled: bool):
    if not is_owner_user(interaction.user.id):
        return await safe_interaction_send(interaction, owner_private_message(), ephemeral=True)
    await set_config(interaction.guild.id, {"enabled": enabled})
    await safe_interaction_send(interaction, f"Server access is now {'enabled' if enabled else 'disabled/private'}.", ephemeral=True)


@bot.tree.command(name="set_admin_role", description="Set the role allowed to use admin bot commands.")
@admin_only()
async def set_admin_role(interaction: discord.Interaction, role: discord.Role):
    await set_config(interaction.guild.id, {"bot_admin_role": role.id})
    await safe_interaction_send(interaction, f"Bot admin role set to {role.mention}.", ephemeral=True)


@bot.tree.command(name="set_verified_role", description="Set the role given after OAuth2 verification.")
@admin_only()
async def set_verified_role(interaction: discord.Interaction, role: discord.Role):
    await set_config(interaction.guild.id, {"verified_role": role.id})
    await safe_interaction_send(interaction, f"Verified role set to {role.mention}.", ephemeral=True)


@bot.tree.command(name="set_unverified_role", description="Set the role removed after successful verification.")
@admin_only()
async def set_unverified_role(interaction: discord.Interaction, role: discord.Role):
    await set_config(interaction.guild.id, {"unverified_role": role.id})
    await safe_interaction_send(interaction, f"Unverified role set to {role.mention}. It will be removed after verification.", ephemeral=True)


@bot.tree.command(name="set_auto_role", description="Set the role automatically given when a member joins.")
@admin_only()
async def set_auto_role(interaction: discord.Interaction, role: discord.Role):
    await set_config(interaction.guild.id, {"auto_role": role.id})
    await safe_interaction_send(interaction, f"Auto role set to {role.mention}.", ephemeral=True)


@bot.tree.command(name="set_logs", description="Set verification and ticket transcript log channels.")
@admin_only()
async def set_logs(interaction: discord.Interaction, verification_logs: Optional[discord.TextChannel] = None, ticket_transcripts: Optional[discord.TextChannel] = None):
    updates = {}
    if verification_logs: updates["verification_log_channel"] = verification_logs.id
    if ticket_transcripts: updates["ticket_log_channel"] = ticket_transcripts.id
    await set_config(interaction.guild.id, updates)
    await safe_interaction_send(interaction, "Log channels updated.", ephemeral=True)


@bot.tree.command(name="send_verification_panel", description="Send the OAuth2 verification panel.")
@admin_only()
async def send_verification_panel(interaction: discord.Interaction, channel: discord.TextChannel):
    await safe_interaction_defer(interaction, ephemeral=True)
    await set_config(interaction.guild.id, {"verification_channel": channel.id})
    config = await get_guild_config(interaction.guild.id)
    embed = make_branded_embed(config, str(config.get("verification_title") or "Verify Access"), str(config.get("verification_description") or DEFAULT_GUILD_CONFIG["verification_description"]))
    embed.add_field(name="Secure by design", value="The private link expires in 10 minutes and only works for the account that clicked the button.", inline=False)
    await safe_channel_send(channel, embed=embed, view=OAuthVerifyView(interaction.guild.id, label=str(config.get("verification_button_label") or "Verify with Discord")))
    await safe_interaction_send(interaction, f"OAuth2 verification panel sent in {channel.mention}.", ephemeral=True)


@bot.tree.command(name="set_ticket_category", description="Set the category where tickets will be created.")
@admin_only()
async def set_ticket_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    await set_config(interaction.guild.id, {"ticket_category": category.id})
    await safe_interaction_send(interaction, f"Ticket category set to **{category.name}**.", ephemeral=True)


@bot.tree.command(name="set_ticket_role", description="Set the support role for a ticket type.")
@app_commands.choices(ticket_type=[app_commands.Choice(name="General support", value="general"), app_commands.Choice(name="Key HWID reset", value="hwid"), app_commands.Choice(name="Key not received", value="key_not_received")])
@admin_only()
async def set_ticket_role(interaction: discord.Interaction, ticket_type: app_commands.Choice[str], role: discord.Role):
    await set_config(interaction.guild.id, {TICKET_TYPES[ticket_type.value]["support_role_key"]: role.id})
    await safe_interaction_send(interaction, f"{ticket_type.name} support role set to {role.mention}.", ephemeral=True)


@bot.tree.command(name="send_ticket_panel", description="Send the ticket panel.")
@admin_only()
async def send_ticket_panel(interaction: discord.Interaction, channel: discord.TextChannel):
    await safe_interaction_defer(interaction, ephemeral=True)
    await set_config(interaction.guild.id, {"ticket_panel_channel": channel.id})
    config = await get_guild_config(interaction.guild.id)
    embed = make_branded_embed(config, str(config.get("ticket_panel_title") or "Support Center"), str(config.get("ticket_panel_description") or DEFAULT_GUILD_CONFIG["ticket_panel_description"]))
    options_text = "\n".join(f"{ticket_type_info(config, key)['emoji']} **{ticket_type_info(config, key)['label']}** — {ticket_type_info(config, key)['description']}" for key in TICKET_TYPES)
    embed.add_field(name="Available categories", value=options_text[:1024], inline=False)
    await safe_channel_send(channel, embed=embed, view=TicketPanelView(config))
    await safe_interaction_send(interaction, f"Ticket panel sent in {channel.mention}.", ephemeral=True)


@bot.tree.command(name="set_store", description="Set the store URL used by /store.")
@admin_only()
async def set_store(interaction: discord.Interaction, url: str):
    url = url.strip()
    if not is_http_url(url):
        return await safe_interaction_send(interaction, "Use a complete `https://` or `http://` store URL.", ephemeral=True)
    await set_config(interaction.guild.id, {"store_url": url[:500]})
    config = await get_guild_config(interaction.guild.id)
    await safe_interaction_send(interaction, embed=make_branded_embed(config, "Store updated", f"The `/store` destination is now:\n{url[:500]}", SUCCESS_COLOR), ephemeral=True)


@bot.tree.command(name="announce", description="Send a clean announcement embed.")
@admin_only()
async def announce(interaction: discord.Interaction, channel: discord.TextChannel, title: str, message: str, image_url: Optional[str] = None):
    config = await get_guild_config(interaction.guild.id)
    resolved_image = str(image_url or config.get("announce_image") or "").strip()[:500]
    if resolved_image and not is_http_url(resolved_image):
        return await safe_interaction_send(interaction, "The announcement image must be a complete `http://` or `https://` URL.", ephemeral=True)
    embed = make_branded_embed(config, title[:256], message[:4000])
    if resolved_image:
        embed.set_image(url=resolved_image)
    embed.set_footer(text=str(config.get("announce_footer") or config.get("brand_footer") or "moealturej")[:2048])
    sent = await safe_channel_send(channel, embed=embed, allowed_mentions=discord.AllowedMentions.none())
    if not sent:
        return await safe_interaction_send(interaction, "Discord could not send that announcement right now. Check the channel permissions and try again.", ephemeral=True)
    await log_command_event(interaction, "announce", channel=channel.id, title=title[:120])
    await safe_interaction_send(interaction, f"Announcement sent in {channel.mention}.", ephemeral=True)


@bot.tree.command(name="stats_setup", description="Create/connect emoji live server stats voice channels.")
@admin_only()
async def stats_setup(interaction: discord.Interaction, category: Optional[discord.CategoryChannel] = None):
    await safe_interaction_defer(interaction, ephemeral=True)
    guild = interaction.guild
    config = await get_guild_config(guild.id)
    if category is None and config.get("stats_category"):
        existing_category = guild.get_channel(int(config.get("stats_category") or 0))
        category = existing_category if isinstance(existing_category, discord.CategoryChannel) else None
    if category is None:
        category = await safe_create_category(guild, "📊 Server Stats", reason="Live server stats setup")
    if category is None:
        return await safe_interaction_send(interaction, "Discord is busy right now. Please try stats setup again in a minute.", ephemeral=True)
    overwrites = {guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=True), guild.me: discord.PermissionOverwrite(connect=True, manage_channels=True, view_channel=True)}
    defaults = {key: render_stat_name(config, key, 0, guild) for key in ("members", "humans", "bots", "boosts")}
    created = {}
    for key, name in defaults.items():
        channel = guild.get_channel((config.get("stats_channels") or {}).get(key) or 0)
        if not isinstance(channel, discord.VoiceChannel):
            channel = await safe_create_voice_channel(guild, name, category=category, overwrites=overwrites, reason="Live stats channel created")
        if channel is None:
            return await safe_interaction_send(interaction, "Discord is busy right now. Some stats channels could not be created. Try again in a minute.", ephemeral=True)
        created[key] = channel.id
    await set_config(guild.id, {"stats_category": category.id, "stats_channels": created})
    await safe_interaction_send(interaction, f"Emoji live stats channels are set in **{category.name}**. Stats will refresh on the next safe scheduled cycle.", ephemeral=True)


@bot.tree.command(name="config_show", description="Show this server's saved config.")
@admin_only()
async def config_show(interaction: discord.Interaction):
    config = await get_guild_config(interaction.guild.id)
    embed = make_branded_embed(config, "Server Config", "Current MongoDB settings.")
    for key in ["enabled", "verified_role", "unverified_role", "auto_role", "bot_admin_role", "verification_log_channel", "ticket_log_channel", "ticket_category", "store_url"]:
        embed.add_field(name=key, value=str(config.get(key)), inline=True)
    await safe_interaction_send(interaction, embed=embed, ephemeral=True)

@bot.tree.command(name="setup_audit", description="Check permissions, role hierarchy, channels, and production configuration.")
@admin_only()
async def setup_audit(interaction: discord.Interaction):
    guild = interaction.guild
    config = await get_guild_config(guild.id)
    me = guild.me
    issues: list[str] = []
    passed: list[str] = []
    if me is None:
        return await safe_interaction_send(interaction, "I could not resolve my server member record. Try again after Discord finishes caching this server.", ephemeral=True)

    checks: list[tuple[str, bool, bool]] = [
        ("Send Messages", me.guild_permissions.send_messages, True),
        ("Embed Links", me.guild_permissions.embed_links, True),
        ("Manage Roles", me.guild_permissions.manage_roles, bool(config.get("feature_verification", True) or config.get("feature_welcome", True))),
        ("Manage Channels", me.guild_permissions.manage_channels, bool(config.get("feature_tickets", True) or config.get("feature_stats", True))),
        ("Attach Files", me.guild_permissions.attach_files, bool(config.get("feature_tickets", True))),
        ("Read Message History", me.guild_permissions.read_message_history, bool(config.get("feature_tickets", True))),
        ("Manage Messages", me.guild_permissions.manage_messages, bool(config.get("feature_moderation", True))),
        ("Moderate Members", me.guild_permissions.moderate_members, bool(config.get("feature_moderation", True))),
    ]
    for label, ok, needed in checks:
        if not needed:
            passed.append(f"➖ {label} not required by enabled modules")
        elif ok:
            passed.append(f"✅ {label}")
        else:
            issues.append(f"❌ {label}")

    if config.get("feature_verification", True):
        verified_role = guild.get_role(int(config.get("verified_role") or 0))
        if not verified_role:
            issues.append("❌ Verified role is not configured")
        elif verified_role >= me.top_role:
            issues.append("❌ Verified role must be below the bot's highest role")
        else:
            passed.append("✅ Verified role hierarchy")
        if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET:
            issues.append("❌ Discord OAuth environment values are missing")
        else:
            passed.append("✅ OAuth environment values")
    else:
        passed.append("➖ Verification module disabled")

    if config.get("feature_tickets", True):
        if not isinstance(guild.get_channel(int(config.get("ticket_category") or 0)), discord.CategoryChannel):
            issues.append("❌ Ticket category is not configured")
        else:
            passed.append("✅ Ticket category")
    else:
        passed.append("➖ Ticket module disabled")

    owner = await get_owner_settings()
    if owner.get("global_pause"):
        issues.append("⚠️ Global maintenance mode is currently enabled")
    if not config.get("enabled") and not is_owner_user(interaction.user.id):
        issues.append("⚠️ This server is currently owner-only")

    embed = make_branded_embed(config, "Production Setup Audit", "Checks are scoped to the modules currently enabled for this server.", SUCCESS_COLOR if not issues else WARNING_COLOR)
    embed.add_field(name="Ready / not required", value="\n".join(passed)[:1024] or "None yet", inline=False)
    embed.add_field(name="Needs attention", value="\n".join(issues)[:1024] or "✅ No blocking issues found", inline=False)
    embed.add_field(name="Dashboard", value=f"{PUBLIC_BASE_URL}/guild/{guild.id}", inline=False)
    await log_command_event(interaction, "setup_audit", issue_count=len(issues))
    await safe_interaction_send(interaction, embed=embed, ephemeral=True)


# =========================
# PRODUCTION COMMANDS / ERROR REPORTING
# =========================
async def log_command_event(interaction: discord.Interaction, event: str, **extra: Any) -> None:
    if not interaction.guild:
        return
    payload = {"guild_id": interaction.guild.id, "user_id": interaction.user.id, "event": event, **extra}
    await save_event("dashboard_events", payload)
    config = await get_guild_config(interaction.guild.id)
    channel = interaction.guild.get_channel(int(config.get("command_log_channel") or 0))
    if isinstance(channel, discord.TextChannel):
        embed = make_branded_embed(config, "Command activity", f"**{event}** by {interaction.user.mention}", INFO_COLOR)
        if extra:
            embed.add_field(name="Details", value="\n".join(f"**{k}:** {v}" for k, v in extra.items())[:1000], inline=False)
        await safe_channel_send(channel, embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def log_moderation(guild: discord.Guild, moderator: discord.Member, action: str, target: discord.abc.User, reason: str) -> None:
    await save_event("moderation_events", {"guild_id": guild.id, "moderator_id": moderator.id, "target_user_id": target.id, "event": action, "reason": reason[:500]})
    config = await get_guild_config(guild.id)
    channel = guild.get_channel(int(config.get("moderation_log_channel") or 0))
    if isinstance(channel, discord.TextChannel):
        embed = make_branded_embed(config, f"Moderation: {action}", f"**Target:** {target.mention} (`{target.id}`)\n**Moderator:** {moderator.mention}\n**Reason:** {reason}", ERROR_COLOR if action in {"ban", "kick", "timeout"} else INFO_COLOR)
        await safe_channel_send(channel, embed=embed, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        return await safe_interaction_send(interaction, f"Slow down — try again in **{error.retry_after:.1f}s**.", ephemeral=True)
    if isinstance(error, (app_commands.MissingPermissions, app_commands.BotMissingPermissions)):
        missing = getattr(error, "missing_permissions", [])
        detail = ", ".join(str(item).replace("_", " ").title() for item in missing)
        return await safe_interaction_send(interaction, f"Missing permission{'' if len(missing) == 1 else 's'}: **{detail or 'unknown'}**.", ephemeral=True)
    if isinstance(error, app_commands.TransformerError):
        return await safe_interaction_send(interaction, "One of the command values is invalid. Check the selected member, channel, role, or number and try again.", ephemeral=True)
    if isinstance(error, app_commands.CommandSignatureMismatch):
        return await safe_interaction_send(interaction, "Discord has an older copy of this command. An administrator should sync commands once, then try again.", ephemeral=True)
    if isinstance(error, app_commands.CheckFailure):
        if not interaction.response.is_done():
            await safe_interaction_send(interaction, "That command is not available to you here.", ephemeral=True)
        return

    original = getattr(error, "original", error)
    incident = await report_exception(
        "app_command",
        original,
        guild_id=interaction.guild_id,
        user_id=interaction.user.id,
        details={"command": interaction.command.qualified_name if interaction.command else "unknown", "channel_id": interaction.channel_id},
    )
    if interaction.guild:
        try:
            await save_event("dashboard_events", {"guild_id": interaction.guild.id, "user_id": interaction.user.id, "event": "command_error", "incident_id": incident})
        except Exception:
            pass
    await safe_interaction_send(interaction, f"That command hit an unexpected error. It has been logged as `{incident}`.", ephemeral=True)


@bot.event
async def on_error(event_method: str, *args, **kwargs):
    exc = sys.exc_info()[1] or RuntimeError(f"Unknown event failure in {event_method}")
    await report_exception(f"discord_event:{event_method}", exc)


@bot.tree.command(name="serverinfo", description="Show useful information about this server.")
@guild_enabled_or_owner()
@app_commands.checks.cooldown(1, COMMAND_COOLDOWN_SECONDS, key=lambda i: (i.guild_id, i.user.id))
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        return await safe_interaction_send(interaction, "This command only works in a server.", ephemeral=True)
    humans = sum(1 for m in guild.members if not m.bot)
    config = await get_guild_config(guild.id)
    embed = make_branded_embed(config, guild.name, "Live server overview.")
    if guild.icon: embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Members", value=f"{guild.member_count or len(guild.members)} total\n{humans} humans", inline=True)
    embed.add_field(name="Channels", value=f"{len(guild.text_channels)} text\n{len(guild.voice_channels)} voice", inline=True)
    embed.add_field(name="Boosts", value=str(guild.premium_subscription_count or 0), inline=True)
    embed.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, style="R"), inline=True)
    embed.set_footer(text=f"Server ID: {guild.id}")
    await safe_interaction_send(interaction, embed=embed)


@bot.tree.command(name="userinfo", description="Show account and server information for a member.")
@guild_enabled_or_owner()
async def userinfo(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    config = await get_guild_config(interaction.guild.id)
    embed = make_branded_embed(config, str(member), f"Information for {member.mention}.")
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, style="R") if member.joined_at else "Unknown", inline=True)
    embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, style="R"), inline=True)
    embed.add_field(name="Top role", value=member.top_role.mention, inline=True)
    embed.set_footer(text=f"User ID: {member.id}")
    await safe_interaction_send(interaction, embed=embed, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="avatar", description="Show a member's full-size avatar.")
@guild_enabled_or_owner()
async def avatar(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    config = await get_guild_config(interaction.guild.id) if interaction.guild else DEFAULT_GUILD_CONFIG
    embed = make_branded_embed(config, f"{member.display_name}'s avatar", f"[Open original]({member.display_avatar.url})")
    embed.set_image(url=member.display_avatar.url)
    await safe_interaction_send(interaction, embed=embed)


@bot.tree.command(name="purge", description="Delete a batch of recent messages safely.")
@admin_only()
@app_commands.describe(amount="Number of messages to delete")
async def purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await safe_interaction_send(interaction, "Use this in a text channel.", ephemeral=True)
    await safe_interaction_defer(interaction, ephemeral=True)
    owner = await get_owner_settings()
    amount = min(int(amount), max(1, min(100, int(owner.get("max_purge_amount") or MAX_PURGE_AMOUNT))))
    deleted = await discord_guarded("purge messages", f"purge:{interaction.channel.id}", lambda: interaction.channel.purge(limit=amount, reason=f"Purged by {interaction.user}"), min_gap=3.0, default=[])
    await log_command_event(interaction, "purge", channel=interaction.channel.id, amount=len(deleted or []))
    await safe_interaction_send(interaction, f"Deleted **{len(deleted or [])}** messages.", ephemeral=True)


@bot.tree.command(name="timeout", description="Temporarily timeout a member.")
@admin_only()
async def timeout_member(interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason provided"):
    if member.id in {interaction.user.id, interaction.guild.owner_id} or member.top_role >= interaction.user.top_role and not is_owner_user(interaction.user.id):
        return await safe_interaction_send(interaction, "You cannot timeout that member.", ephemeral=True)
    until = utcnow() + timedelta(minutes=int(minutes))
    async def apply_timeout():
        await member.timeout(until, reason=reason)
        return True
    ok = await discord_guarded("timeout member", f"moderation:{interaction.guild.id}", apply_timeout, min_gap=2.0, default=False)
    if not ok:
        return await safe_interaction_send(interaction, "The timeout failed. Check role order and permissions.", ephemeral=True)
    await log_moderation(interaction.guild, interaction.user, "timeout", member, reason)
    config = await get_guild_config(interaction.guild.id)
    if config.get("moderation_dm_timeout", True):
        await safe_user_send(member, embed=make_branded_embed(config, f"Timeout in {interaction.guild.name}", f"You were timed out for **{minutes} minute(s)**.\n\n**Reason:** {reason}", ERROR_COLOR), allowed_mentions=discord.AllowedMentions.none())
    await safe_interaction_send(interaction, embed=make_branded_embed(config, "Timeout applied", f"{member.mention} was timed out for **{minutes} minute(s)**.\n\n**Reason:** {reason}", SUCCESS_COLOR), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="untimeout", description="Remove a member's timeout.")
@admin_only()
async def untimeout_member(interaction: discord.Interaction, member: discord.Member, reason: str = "Timeout removed"):
    async def remove_member_timeout():
        await member.timeout(None, reason=reason)
        return True
    ok = await discord_guarded("remove timeout", f"moderation:{interaction.guild.id}", remove_member_timeout, min_gap=2.0, default=False)
    if not ok:
        return await safe_interaction_send(interaction, "The timeout could not be removed. Check role order and permissions.", ephemeral=True)
    await log_moderation(interaction.guild, interaction.user, "untimeout", member, reason)
    await safe_interaction_send(interaction, f"Removed {member.mention}'s timeout.", ephemeral=True)


@bot.tree.command(name="warn", description="Record a warning for a member.")
@admin_only()
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    await mdb.warnings.insert_one({"guild_id": interaction.guild.id, "user_id": member.id, "moderator_id": interaction.user.id, "reason": reason[:1000], "created_at": now_iso()})
    count = await mdb.warnings.count_documents({"guild_id": interaction.guild.id, "user_id": member.id})
    await log_moderation(interaction.guild, interaction.user, "warn", member, reason)
    config = await get_guild_config(interaction.guild.id)
    if config.get("moderation_dm_warn", True):
        await safe_user_send(member, embed=make_branded_embed(config, f"Warning in {interaction.guild.name}", reason, ERROR_COLOR), allowed_mentions=discord.AllowedMentions.none())
    await safe_interaction_send(interaction, embed=make_branded_embed(config, "Warning recorded", f"{member.mention} now has **{count}** warning(s).\n\n**Reason:** {reason}", SUCCESS_COLOR), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="warnings", description="View recorded warnings for a member.")
@admin_only()
async def warnings(interaction: discord.Interaction, member: discord.Member):
    items = await mdb.warnings.find({"guild_id": interaction.guild.id, "user_id": member.id}, {"_id": 0}).sort("created_at", -1).limit(10).to_list(length=10)
    config = await get_guild_config(interaction.guild.id)
    embed = make_branded_embed(config, f"Warnings for {member}", f"Showing {len(items)} most recent warning(s).")
    for idx, item in enumerate(items, 1):
        embed.add_field(name=f"#{idx} • {str(item.get('created_at',''))[:10]}", value=f"{item.get('reason','No reason')[:700]}\nModerator: `{item.get('moderator_id','unknown')}`", inline=False)
    await safe_interaction_send(interaction, embed=embed, ephemeral=True)


@bot.tree.command(name="slowmode", description="Set this channel's slowmode delay.")
@admin_only()
async def slowmode(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await safe_interaction_send(interaction, "Use this in a text channel.", ephemeral=True)
    await safe_channel_edit(interaction.channel, slowmode_delay=int(seconds), reason=f"Changed by {interaction.user}")
    await log_command_event(interaction, "slowmode", channel=interaction.channel.id, seconds=seconds)
    await safe_interaction_send(interaction, f"Slowmode set to **{seconds} seconds**.", ephemeral=True)


@bot.tree.command(name="lock", description="Lock the current text channel for regular members.")
@admin_only()
async def lock_channel(interaction: discord.Interaction, reason: str = "Channel locked"):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await safe_interaction_send(interaction, "Use this in a text channel.", ephemeral=True)
    overwrite = channel.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = False
    await discord_guarded("lock channel", f"permission:{channel.id}", lambda: channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=reason), min_gap=3.0, default=None)
    await log_command_event(interaction, "lock", channel=channel.id, reason=reason)
    await safe_interaction_send(interaction, "🔒 Channel locked.")


@bot.tree.command(name="unlock", description="Unlock the current text channel.")
@admin_only()
async def unlock_channel(interaction: discord.Interaction, reason: str = "Channel unlocked"):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await safe_interaction_send(interaction, "Use this in a text channel.", ephemeral=True)
    overwrite = channel.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = None
    await discord_guarded("unlock channel", f"permission:{channel.id}", lambda: channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=reason), min_gap=3.0, default=None)
    await log_command_event(interaction, "unlock", channel=channel.id, reason=reason)
    await safe_interaction_send(interaction, "🔓 Channel unlocked.")


@bot.tree.command(name="ticket_add", description="Add a member to the current support ticket.")
@admin_only()
async def ticket_add(interaction: discord.Interaction, member: discord.Member):
    if not isinstance(interaction.channel, discord.TextChannel) or not interaction.channel.topic or "owner_id=" not in interaction.channel.topic:
        return await safe_interaction_send(interaction, "This is not a managed ticket channel.", ephemeral=True)
    await discord_guarded("ticket add member", f"permission:{interaction.channel.id}", lambda: interaction.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True), min_gap=3.0, default=None)
    await log_command_event(interaction, "ticket_add", channel=interaction.channel.id, member=member.id)
    await safe_interaction_send(interaction, f"Added {member.mention} to this ticket.")


@bot.tree.command(name="ticket_rename", description="Rename the current support ticket.")
@admin_only()
async def ticket_rename(interaction: discord.Interaction, name: str):
    if not isinstance(interaction.channel, discord.TextChannel) or not interaction.channel.topic or "owner_id=" not in interaction.channel.topic:
        return await safe_interaction_send(interaction, "This is not a managed ticket channel.", ephemeral=True)
    clean = clean_channel_name(name)[:90]
    await safe_channel_edit(interaction.channel, name=clean, reason=f"Ticket renamed by {interaction.user}")
    await log_command_event(interaction, "ticket_rename", channel=interaction.channel.id, name=clean)
    await safe_interaction_send(interaction, f"Ticket renamed to **{clean}**.", ephemeral=True)

# =========================
# START
# =========================
def is_cloudflare_startup_limit(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "429 too many requests" in text
        or "error 1015" in text
        or "you are being rate limited" in text
        or "cloudflare" in text and "rate limited" in text
    )


async def run_forever_without_restart_loop() -> None:
    global startup_blocked_until, last_startup_error

    missing = [name for name, value in {
        "BOT_TOKEN": BOT_TOKEN,
        "DISCORD_CLIENT_ID": DISCORD_CLIENT_ID,
        "DISCORD_CLIENT_SECRET": DISCORD_CLIENT_SECRET,
        "PUBLIC_BASE_URL": PUBLIC_BASE_URL,
        "MONGO_URI": MONGO_URI,
        "DASHBOARD_SECRET": DASHBOARD_SECRET,
    }.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required .env values: {', '.join(missing)}")

    # Start the dashboard/health server before Discord login. This prevents
    # Render from killing and restarting the service while Discord/Cloudflare is
    # temporarily blocking this Render IP. The dashboard will show
    # bot=waiting_for_discord until login succeeds.
    try:
        await init_mongo()
    except Exception as e:
        log.warning("Mongo init failed before Discord login; setup_hook will retry after login: %s", e)
    await start_web()

    attempt = 0
    while True:
        attempt += 1
        try:
            startup_blocked_until = None
            last_startup_error = None
            log.info("Starting Discord login attempt %s%s", attempt, "" if STARTUP_MAX_LOGIN_ATTEMPTS == 0 else f"/{STARTUP_MAX_LOGIN_ATTEMPTS}")
            await bot.start(BOT_TOKEN, reconnect=True)
            return
        except discord.HTTPException as e:
            if is_cloudflare_startup_limit(e):
                wait_seconds = STARTUP_LOGIN_RETRY_SECONDS
                startup_blocked_until = utcnow() + timedelta(seconds=wait_seconds)
                last_startup_error = "Discord/Cloudflare startup 429 or 1015. Waiting instead of restart-looping."
                log.error("Discord login is temporarily rate-limited by Discord/Cloudflare. Waiting %ss before retrying. Do NOT manually restart repeatedly.", wait_seconds)
                await asyncio.sleep(wait_seconds)
            else:
                last_startup_error = f"Discord HTTP startup error: {e}"[:500]
                log.exception("Discord HTTP startup error. Waiting %ss before retrying.", STARTUP_GENERIC_RETRY_SECONDS)
                await asyncio.sleep(STARTUP_GENERIC_RETRY_SECONDS)
        except Exception as e:
            last_startup_error = f"Startup error: {e}"[:500]
            log.exception("Startup crashed. Waiting %ss before retrying instead of letting Render restart-loop.", STARTUP_GENERIC_RETRY_SECONDS)
            await asyncio.sleep(STARTUP_GENERIC_RETRY_SECONDS)

        if STARTUP_MAX_LOGIN_ATTEMPTS and attempt >= STARTUP_MAX_LOGIN_ATTEMPTS:
            log.error("Reached STARTUP_MAX_LOGIN_ATTEMPTS=%s. Keeping health server online without more Discord login attempts.", STARTUP_MAX_LOGIN_ATTEMPTS)
            while True:
                await asyncio.sleep(3600)


async def main() -> None:
    global http_session
    try:
        await run_forever_without_restart_loop()
    finally:
        if not bot.is_closed():
            await bot.close()
        if http_session and not http_session.closed:
            await http_session.close()
        if web_runner:
            await web_runner.cleanup()
        if mongo_client:
            mongo_client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutdown requested")
