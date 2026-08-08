import asyncio
import base64
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
from pymongo import ReturnDocument

from core.blackjack import BlackjackView, CasinoStore, format_amount

BUILD_VERSION = "3.0.0-production-casino"

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
    # Casino / blackjack economy
    "casino_enabled": True,
    "casino_currency": "credits",
    "casino_starting_balance": 1000,
    "casino_daily_reward": 250,
    "blackjack_min_bet": 10,
    "blackjack_max_bet": 5000,
    "blackjack_dealer_hits_soft_17": False,
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
    await mdb.casino_wallets.create_index([("guild_id", 1), ("user_id", 1)], unique=True)
    await mdb.casino_wallets.create_index([("guild_id", 1), ("balance", -1)])
    await mdb.casino_sessions.create_index([("guild_id", 1), ("user_id", 1)], unique=True)
    await mdb.casino_sessions.create_index("expires_at", expireAfterSeconds=0)
    await mdb.casino_events.create_index([("guild_id", 1), ("created_at", -1)])


async def get_guild_config(guild_id: int) -> Dict[str, Any]:
    guild_id = int(guild_id)
    now = asyncio.get_running_loop().time()
    cached = CONFIG_CACHE.get(guild_id)
    if cached and now - cached[0] < CONFIG_CACHE_SECONDS:
        return dict(cached[1])
    existing = await mdb.guild_configs.find_one({"guild_id": guild_id}, {"_id": 0})
    if not existing:
        doc = {"guild_id": int(guild_id), **DEFAULT_GUILD_CONFIG, "created_at": now_iso(), "updated_at": now_iso()}
        await mdb.guild_configs.insert_one(doc)
        clean = {k: v for k, v in doc.items() if k != "_id"}
        CONFIG_CACHE[guild_id] = (now, clean)
        return dict(clean)

    update: Dict[str, Any] = {}
    for key, value in DEFAULT_GUILD_CONFIG.items():
        if key not in existing:
            update[key] = value
    for key, value in DEFAULT_GUILD_CONFIG["stats_channels"].items():
        if key not in existing.get("stats_channels", {}):
            update[f"stats_channels.{key}"] = value
    if update:
        update["updated_at"] = now_iso()
        await mdb.guild_configs.update_one({"guild_id": int(guild_id)}, {"$set": update})
        existing = await mdb.guild_configs.find_one({"guild_id": int(guild_id)}, {"_id": 0})
    CONFIG_CACHE[guild_id] = (now, existing)
    return dict(existing)


async def set_config(guild_id: int, updates: Dict[str, Any]) -> None:
    await get_guild_config(guild_id)
    updates["updated_at"] = now_iso()
    await mdb.guild_configs.update_one({"guild_id": int(guild_id)}, {"$set": updates}, upsert=True)
    CONFIG_CACHE.pop(int(guild_id), None)


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
    return f"This bot is not for public use. {OWNER_CONTACT}"


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await safe_interaction_send(interaction, "This command only works in a server.", ephemeral=True)
            return False
        config = await get_guild_config(interaction.guild.id)
        if not member_is_command_admin(interaction.user, config):
            await safe_interaction_send(interaction, owner_private_message(), ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)


def guild_enabled_or_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return True
        if is_owner_user(interaction.user.id):
            return True
        config = await get_guild_config(interaction.guild.id)
        if config.get("enabled"):
            return True
        await safe_interaction_send(interaction, owner_private_message(), ephemeral=True)
        return False
    return app_commands.check(predicate)

# =========================
# UI HELPERS
# =========================
def make_embed(title: str, description: str, color: int = EMBED_COLOR) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color, timestamp=utcnow())


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
    embed.set_footer(text=footer, icon_url=icon_url or None)
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
    lines = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<style>body{font-family:Arial;background:#0b0b10;color:#fff;padding:24px}.msg{border-bottom:1px solid #292938;padding:12px 0}.meta{color:#a8a8b8;font-size:13px}.content{white-space:pre-wrap;margin-top:6px}.att a{color:#c4b5fd}</style>",
        f"<title>Transcript #{html.escape(channel.name)}</title></head><body>",
        f"<h1>Transcript: #{html.escape(channel.name)}</h1>",
    ]
    async for msg in channel.history(limit=None, oldest_first=True):
        author = html.escape(str(msg.author))
        content = html.escape(msg.content or "")
        created = msg.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines.append("<div class='msg'>")
        lines.append(f"<div class='meta'><strong>{author}</strong> • {created}</div>")
        if content:
            lines.append(f"<div class='content'>{content}</div>")
        if msg.embeds:
            for emb in msg.embeds:
                title = html.escape(emb.title or "Embed")
                desc = html.escape(emb.description or "")
                lines.append(f"<div class='content'>[Embed] <strong>{title}</strong><br>{desc}</div>")
        if msg.attachments:
            links = " ".join(f"<a href='{html.escape(a.url)}'>{html.escape(a.filename)}</a>" for a in msg.attachments)
            lines.append(f"<div class='att'>Attachments: {links}</div>")
        lines.append("</div>")
    lines.append("</body></html>")
    filename = f"transcript-{channel.guild.id}-{channel.id}.html"
    return filename, "\n".join(lines).encode("utf-8")

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
    css = """
    <style>
    :root{color-scheme:dark;--bg:#030306;--bg2:#070711;--glass:rgba(12,12,22,.74);--glass2:rgba(255,255,255,.055);--panel:rgba(14,14,25,.82);--panel2:rgba(124,58,237,.14);--line:rgba(255,255,255,.12);--line2:rgba(192,132,252,.35);--text:#f8f7ff;--muted:rgba(248,247,255,.66);--soft:rgba(248,247,255,.84);--purple:#8b5cf6;--purple2:#c084fc;--pink:#ec4899;--blue:#38bdf8;--green:#22c55e;--danger:#fb7185;--shadow:0 30px 110px rgba(0,0,0,.42)}
    *{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;min-height:100vh;background:radial-gradient(circle at 18% -10%,rgba(139,92,246,.38),transparent 34rem),radial-gradient(circle at 92% 12%,rgba(236,72,153,.18),transparent 30rem),radial-gradient(circle at 55% 96%,rgba(56,189,248,.12),transparent 32rem),linear-gradient(180deg,#05050a,#020204 68%,#05050a);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;overflow-x:hidden}body:before{content:"";position:fixed;inset:0;pointer-events:none;background-image:linear-gradient(rgba(255,255,255,.045) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.045) 1px,transparent 1px);background-size:72px 72px;mask-image:linear-gradient(to bottom,rgba(0,0,0,.9),transparent 82%);opacity:.55}body:after{content:"";position:fixed;inset:0;pointer-events:none;background:radial-gradient(circle at 50% 0,rgba(255,255,255,.08),transparent 38rem);mix-blend-mode:screen}a{color:#e9d5ff;text-decoration:none}.wrap{width:min(1220px,calc(100% - 30px));margin:auto;padding:28px 0 58px}.nav{position:sticky;top:14px;z-index:10;display:flex;justify-content:space-between;align-items:center;margin-bottom:28px;padding:12px 14px;border:1px solid var(--line);border-radius:24px;background:linear-gradient(135deg,rgba(8,8,15,.82),rgba(20,15,34,.68));backdrop-filter:blur(22px);box-shadow:0 22px 90px rgba(0,0,0,.36)}.brand{display:flex;align-items:center;gap:11px;font-weight:950;letter-spacing:-.05em}.brand:before{content:"✦";display:grid;place-items:center;width:38px;height:38px;border-radius:14px;background:linear-gradient(135deg,var(--purple),var(--pink) 55%,var(--blue));box-shadow:0 14px 50px rgba(139,92,246,.46)}.navlinks{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.navlinks a{padding:9px 12px;border-radius:14px;color:rgba(255,255,255,.74);font-weight:800;font-size:14px}.navlinks a:hover{background:rgba(255,255,255,.08);color:#fff}.hero{position:relative;overflow:hidden;border:1px solid var(--line);border-radius:34px;padding:38px;background:linear-gradient(145deg,rgba(139,92,246,.24),rgba(236,72,153,.08) 38%,rgba(56,189,248,.07) 62%,rgba(255,255,255,.04));box-shadow:var(--shadow)}.hero:before{content:"";position:absolute;inset:1px;border-radius:33px;border:1px solid rgba(255,255,255,.06);pointer-events:none}.hero:after{content:"";position:absolute;right:-100px;top:-120px;width:340px;height:340px;background:radial-gradient(circle,rgba(192,132,252,.38),transparent 68%);filter:blur(2px)}h1{font-size:clamp(34px,5.3vw,68px);letter-spacing:-.07em;line-height:.92;margin:0 0 13px;max-width:930px}h2{letter-spacing:-.04em;margin:0 0 12px;font-size:clamp(22px,2.4vw,31px)}h3{letter-spacing:-.03em;margin:0 0 10px;font-size:20px}.card,.guild,.panel{position:relative;border:1px solid var(--line);background:linear-gradient(145deg,var(--panel),rgba(255,255,255,.04));border-radius:26px;padding:23px;box-shadow:0 24px 90px rgba(0,0,0,.29);backdrop-filter:blur(20px);overflow:hidden}.card:before,.guild:before{content:"";position:absolute;inset:0;background:linear-gradient(135deg,rgba(255,255,255,.07),transparent 38%);pointer-events:none;opacity:.55}.guild{transition:transform .18s ease,border-color .18s ease,background .18s ease}.guild:hover{transform:translateY(-4px);border-color:var(--line2);background:linear-gradient(145deg,rgba(124,58,237,.2),rgba(255,255,255,.055))}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:16px}.section-title{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;margin:30px 0 13px}.btn,button{display:inline-flex;align-items:center;justify-content:center;gap:9px;border:0;border-radius:16px;background:linear-gradient(135deg,#7c3aed,#a855f7 55%,#ec4899);color:white;padding:12px 17px;font-weight:950;cursor:pointer;box-shadow:0 17px 46px rgba(124,58,237,.29);transition:transform .16s ease,filter .16s ease,box-shadow .16s ease}.btn:hover,button:hover{transform:translateY(-1px);filter:brightness(1.08);box-shadow:0 22px 58px rgba(124,58,237,.34)}.btn.secondary{background:rgba(255,255,255,.075);box-shadow:none;border:1px solid var(--line)}.muted{color:var(--muted);line-height:1.66}.pill{display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border-radius:999px;background:rgba(139,92,246,.14);color:#ede9fe;border:1px solid rgba(192,132,252,.28);font-size:13px;font-weight:900;box-shadow:inset 0 1px rgba(255,255,255,.08)}code{display:inline-block;max-width:100%;overflow:auto;padding:11px 13px;border-radius:15px;border:1px solid var(--line);background:rgba(0,0,0,.34);color:#ddd6fe}label{display:block;color:rgba(255,255,255,.84);font-size:13px;font-weight:900;letter-spacing:.01em}input,select,textarea{width:100%;margin:8px 0 16px;padding:14px 15px;border-radius:16px;border:1px solid rgba(255,255,255,.14);background:#10101b;color:#f8fafc;outline:none;box-shadow:inset 0 0 0 9999px rgba(255,255,255,.018);font:inherit}input::placeholder,textarea::placeholder{color:rgba(255,255,255,.36)}input:focus,select:focus,textarea:focus{border-color:rgba(192,132,252,.75);box-shadow:0 0 0 4px rgba(124,58,237,.18)}textarea{min-height:145px;resize:vertical;line-height:1.55}select{appearance:none;background-color:#10101b;background-image:linear-gradient(45deg,transparent 50%,#c4b5fd 50%),linear-gradient(135deg,#c4b5fd 50%,transparent 50%);background-position:calc(100% - 19px) 52%,calc(100% - 12px) 52%;background-size:7px 7px,7px 7px;background-repeat:no-repeat;padding-right:42px}select option{background:#0d0d18;color:#f8fafc}select option:hover,select option:checked{background:#7c3aed;color:#fff}.row{display:grid;grid-template-columns:1fr 1fr;gap:16px}.form-section{margin-top:17px;padding-top:17px;border-top:1px solid var(--line)}.savebar{position:sticky;bottom:14px;display:flex;justify-content:flex-end;margin-top:10px;padding:12px;border:1px solid var(--line);border-radius:22px;background:rgba(7,7,13,.8);backdrop-filter:blur(20px)}.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px}.preview-shell{border:1px solid var(--line);border-radius:24px;background:linear-gradient(145deg,rgba(0,0,0,.28),rgba(255,255,255,.035));padding:16px}.preview-message{white-space:pre-wrap;color:#f8fafc;line-height:1.55;margin-bottom:12px;padding:13px 14px;border:1px solid rgba(255,255,255,.08);border-radius:16px;background:rgba(255,255,255,.045)}.preview-box{border:1px solid var(--line);border-left:4px solid var(--purple);border-radius:18px;background:rgba(0,0,0,.24);padding:18px;margin-top:8px}.preview-title{font-weight:950;font-size:20px;letter-spacing:-.025em}.preview-desc{white-space:pre-wrap;color:rgba(255,255,255,.78);line-height:1.55;margin-top:8px}.preview-footer{color:rgba(255,255,255,.48);font-size:12px;margin-top:14px}.preview-img{max-width:100%;border-radius:16px;margin-top:14px;border:1px solid var(--line)}.preview-thumb{float:right;width:88px;height:88px;object-fit:cover;border-radius:16px;margin-left:14px;margin-bottom:10px;border:1px solid var(--line)}.tiny{font-size:12px;color:rgba(255,255,255,.48)}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-top:16px}.stat{padding:18px;border:1px solid var(--line);border-radius:20px;background:rgba(255,255,255,.045)}.stat b{display:block;font-size:27px;letter-spacing:-.04em}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:20px}table{width:100%;border-collapse:collapse;min-width:720px}th,td{padding:13px 15px;text-align:left;border-bottom:1px solid var(--line);font-size:13px}th{color:#ddd6fe;background:rgba(124,58,237,.12)}td{color:var(--soft)}@media(max-width:760px){.row{grid-template-columns:1fr}.nav{position:relative;top:0;align-items:flex-start;gap:12px;flex-direction:column}.hero{padding:25px}.grid{grid-template-columns:1fr}h1{font-size:39px}}

    .compact-hero{padding:30px}.compact-hero h1{font-size:clamp(36px,5vw,58px)}.notice{margin:16px 0;padding:14px 16px;border-radius:18px;border:1px solid var(--line);background:rgba(255,255,255,.045);color:var(--soft)}.notice.success{border-color:rgba(34,197,94,.35);background:rgba(34,197,94,.10)}.notice.warning{border-color:rgba(245,158,11,.38);background:rgba(245,158,11,.10)}.dashboard-actions{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0 22px}.settings-form{display:grid;gap:13px}.settings-form details{border:1px solid var(--line);border-radius:24px;background:linear-gradient(145deg,var(--panel),rgba(255,255,255,.035));box-shadow:0 18px 60px rgba(0,0,0,.22);overflow:hidden}.settings-form summary{cursor:pointer;list-style:none;display:flex;justify-content:space-between;align-items:center;gap:18px;padding:20px 22px;font-size:18px;font-weight:900}.settings-form summary::-webkit-details-marker{display:none}.settings-form summary span{display:flex;align-items:center;gap:12px}.settings-form summary b{display:grid;place-items:center;width:34px;height:34px;border-radius:12px;background:rgba(139,92,246,.18);color:#ddd6fe;font-size:12px}.settings-form summary small{color:var(--muted);font-weight:700}.settings-form details[open] summary{border-bottom:1px solid var(--line)}.details-body{padding:22px}.ticket-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.subcard{padding:17px;border:1px solid var(--line);border-radius:20px;background:rgba(255,255,255,.035)}.checkline{display:flex;align-items:center;gap:10px;padding:12px 0}.checkline input{width:auto;margin:0}.settings-form .savebar{align-items:center;justify-content:space-between}.settings-form small{display:block;color:var(--muted);font-weight:600;line-height:1.5}@media(max-width:900px){.ticket-grid{grid-template-columns:1fr}.settings-form summary{align-items:flex-start;flex-direction:column}.settings-form .savebar{align-items:stretch;flex-direction:column}}
    </style>
    """
    html_doc = f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title>{css}</head><body><main class='wrap'><nav class='nav'><div class='brand'>moealturej bot</div><div class='navlinks'><a href='/'>Dashboard</a><a href='/health'>Health</a><a href='/logout'>Logout</a></div></nav>{body}</main></body></html>"
    return web.Response(text=html_doc, content_type="text/html")

async def home(request: web.Request) -> web.Response:
    user = await get_dashboard_user(request)
    if not user:
        body = f"<section class='hero'><span class='pill'>🔒 Private control panel</span><h1>Private Discord bot dashboard</h1><p class='muted'>Login with Discord to manage approved servers, verification, tickets, transcripts, logs, and live stats.</p><a class='btn' href='/login'>Login with Discord</a><p class='muted'>{html.escape(OWNER_CONTACT)}</p></section>"
        return page("Dashboard", body)
    if DASHBOARD_OWNER_ONLY and not is_owner_user(int(user["user_id"])):
        return page("Not public", f"<section class='card'><h1>Not available publicly</h1><p class='muted'>{html.escape(owner_private_message())}</p></section>")

    guilds = user.get("guilds", [])
    cards = []
    bot_guild_ids = {g.id for g in bot.guilds}
    for g in guilds:
        if int(g["id"]) in bot_guild_ids and guild_manageable(g):
            icon = "🟢" if int(g["id"]) in bot_guild_ids else "⚪"
            cards.append(f"<div class='guild'><span class='pill'>{icon} Connected</span><h3>{html.escape(g['name'])}</h3><p class='muted'>Server ID: {g['id']}</p><a class='btn' href='/guild/{g['id']}'>Manage server</a></div>")
    body = f"<section class='hero compact-hero'><span class='pill'>Authenticated dashboard</span><h1>Welcome, {html.escape(user.get('username','admin'))}</h1><p class='muted'>Manage connected servers you are authorized to configure.</p></section><div class='section-title'><h2>Your servers</h2><span class='muted'>MongoDB synced</span></div><div class='grid'>{''.join(cards) or '<div class=card>No manageable bot servers found.</div>'}</div>"
    return page("Dashboard", body)


async def login(request: web.Request) -> web.Response:
    state = secrets.token_urlsafe(32)
    await mdb.oauth_states.insert_one({"state": state, "type": "dashboard", "expires_at": utcnow() + timedelta(minutes=10)})
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": f"{PUBLIC_BASE_URL}/oauth/callback",
        "response_type": "code",
        "scope": "identify guilds",
        "state": state,
        "prompt": "none",
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

    def checked(value: bool) -> str:
        return "checked" if value else ""

    roles = [r for r in guild.roles if not r.is_default() and not r.managed]
    text_channels = guild.text_channels
    categories = guild.categories
    me = guild.me
    permissions = me.guild_permissions if me else discord.Permissions.none()
    verified_role = guild.get_role(int(config.get("verified_role") or 0))
    problems: list[str] = []
    if not permissions.manage_roles:
        problems.append("Bot is missing Manage Roles")
    if not permissions.manage_channels:
        problems.append("Bot is missing Manage Channels")
    if not permissions.send_messages:
        problems.append("Bot is missing Send Messages")
    if verified_role and me and verified_role >= me.top_role:
        problems.append("Verified role is above the bot role")
    if not config.get("verified_role"):
        problems.append("Verified role is not selected")
    if not config.get("ticket_category"):
        problems.append("Ticket category is not selected")

    open_tickets = len(config.get("open_tickets", {}))
    verified_count = await mdb.verified_members.count_documents({"guild_id": guild_id})
    wallet_count = await mdb.casino_wallets.count_documents({"guild_id": guild_id})
    saved_banner = "<div class='notice success'>✓ Settings saved and cache refreshed.</div>" if request.query.get("saved") else ""
    diag_class = "success" if not problems else "warning"
    diag_text = "Everything required is configured." if not problems else " • ".join(problems)

    body = f"""
    <section class='hero compact-hero'>
      <span class='pill'>Control center</span><h1>{html.escape(guild.name)}</h1>
      <p class='muted'>Configure branding, verification, tickets, welcome messages, moderation logs, and the blackjack economy without editing code.</p>
      <div class='stats'><div class='stat'><span class='muted'>Members</span><b>{guild.member_count or len(guild.members)}</b></div><div class='stat'><span class='muted'>Open tickets</span><b>{open_tickets}</b></div><div class='stat'><span class='muted'>Verified</span><b>{verified_count}</b></div><div class='stat'><span class='muted'>Casino players</span><b>{wallet_count}</b></div></div>
    </section>
    {saved_banner}
    <div class='notice {diag_class}'><strong>Setup audit:</strong> {html.escape(diag_text)}</div>
    <div class='dashboard-actions'><a class='btn secondary' href='/guild/{guild_id}/announcements'>Announcement</a><a class='btn secondary' href='/guild/{guild_id}/embeds'>Embed builder</a><a class='btn secondary' href='/guild/{guild_id}/dms'>DM sender</a><a class='btn secondary' href='/guild/{guild_id}/activity'>Activity & errors</a></div>

    <form class='settings-form' method='post'>
      <details open><summary><span><b>01</b> Brand & core settings</span><small>Identity, access, and public links</small></summary><div class='details-body'>
        <div class='row'><label>Bot availability<select name='enabled'><option value='true' {selected(bool(config.get('enabled')))}>Enabled</option><option value='false' {selected(not bool(config.get('enabled')))}>Disabled / owner only</option></select></label><label>Store URL<input name='store_url' value='{html.escape(str(config.get('store_url') or DEFAULT_STORE_URL))}' placeholder='https://your-store.com'></label></div>
        <div class='row'><label>Brand name<input name='brand_name' maxlength='60' value='{html.escape(str(config.get('brand_name') or 'moealturej'))}'></label><label>Brand color<input name='brand_color' maxlength='7' value='#{html.escape(str(config.get('brand_color') or '7C3AED').lstrip('#'))}' placeholder='#7C3AED'></label></div>
        <label>Embed footer<input name='brand_footer' maxlength='150' value='{html.escape(str(config.get('brand_footer') or ''))}'></label>
        <div class='row'><label>Brand icon URL<input name='brand_icon_url' value='{html.escape(str(config.get('brand_icon_url') or ''))}' placeholder='Optional HTTPS image URL'></label><label>Bot admin role<select name='bot_admin_role'>{options(roles, config.get('bot_admin_role'))}</select></label></div>
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

      <details open><summary><span><b>05</b> Blackjack & economy</span><small>Server-specific virtual credits; no real-money features</small></summary><div class='details-body'>
        <div class='row'><label>Casino<select name='casino_enabled'><option value='true' {selected(bool(config.get('casino_enabled', True)))}>Enabled</option><option value='false' {selected(not bool(config.get('casino_enabled', True)))}>Disabled</option></select></label><label>Currency name<input name='casino_currency' maxlength='24' value='{html.escape(str(config.get('casino_currency') or 'credits'))}'></label></div>
        <div class='row'><label>Starting balance<input type='number' min='0' max='100000000' name='casino_starting_balance' value='{int(config.get('casino_starting_balance', 1000))}'></label><label>Daily reward<input type='number' min='0' max='100000000' name='casino_daily_reward' value='{int(config.get('casino_daily_reward', 250))}'></label></div>
        <div class='row'><label>Minimum blackjack bet<input type='number' min='1' max='100000000' name='blackjack_min_bet' value='{int(config.get('blackjack_min_bet', 10))}'></label><label>Maximum blackjack bet<input type='number' min='1' max='100000000' name='blackjack_max_bet' value='{int(config.get('blackjack_max_bet', 5000))}'></label></div>
        <label class='checkline'><input type='checkbox' name='blackjack_dealer_hits_soft_17' {checked(bool(config.get('blackjack_dealer_hits_soft_17')))}> Dealer hits soft 17</label>
      </div></details>

      <details><summary><span><b>06</b> Logs & moderation</span><small>Where operational events are recorded</small></summary><div class='details-body'>
        <div class='row'><label>Moderation logs<select name='moderation_log_channel'>{options(text_channels, config.get('moderation_log_channel'), prefix='#')}</select></label><label>Command logs<select name='command_log_channel'>{options(text_channels, config.get('command_log_channel'), prefix='#')}</select></label></div>
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

    def as_int(name: str, default: Optional[int] = None, low: int = 0, high: int = 100_000_000) -> Optional[int]:
        value = str(data.get(name, "")).strip()
        if not value and default is None:
            return None
        try:
            return max(low, min(high, int(value if value else default)))
        except (TypeError, ValueError):
            return default

    def as_bool(name: str, *, checkbox: bool = False) -> bool:
        if checkbox:
            return name in data
        return str(data.get(name, "false")).lower() == "true"

    def text(name: str, default: str = "", limit: int = 1500) -> str:
        return str(data.get(name) or default).strip()[:limit]

    store_url = text("store_url", DEFAULT_STORE_URL, 500)
    if urlparse(store_url).scheme not in {"http", "https"}:
        store_url = DEFAULT_STORE_URL
    brand_color = text("brand_color", "7C3AED", 7).lstrip("#").upper()
    if len(brand_color) not in {3, 6} or any(ch not in string.hexdigits for ch in brand_color):
        brand_color = "7C3AED"
    min_bet = as_int("blackjack_min_bet", 10, 1) or 10
    max_bet = as_int("blackjack_max_bet", 5000, 1) or 5000
    if max_bet < min_bet:
        max_bet = min_bet

    updates = {
        "enabled": as_bool("enabled"),
        "store_url": store_url,
        "brand_name": text("brand_name", "moealturej", 60),
        "brand_color": brand_color,
        "brand_footer": text("brand_footer", "moealturej", 150),
        "brand_icon_url": text("brand_icon_url", "", 500),
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
        "casino_enabled": as_bool("casino_enabled"),
        "casino_currency": text("casino_currency", "credits", 24),
        "casino_starting_balance": as_int("casino_starting_balance", 1000, 0),
        "casino_daily_reward": as_int("casino_daily_reward", 250, 0),
        "blackjack_min_bet": min_bet,
        "blackjack_max_bet": max_bet,
        "blackjack_dealer_hits_soft_17": as_bool("blackjack_dealer_hits_soft_17", checkbox=True),
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


def composer_page(guild: discord.Guild, mode: str, sent: bool = False) -> web.Response:
    is_announcement = mode == "announcement"
    title = "Announcement Sender" if is_announcement else "Embed Sender"
    defaults = {
        "title": "New Announcement" if is_announcement else "Embed Title",
        "message": "Write your embed description here..." if not is_announcement else "Write your announcement details here...",
        "content": "@everyone" if is_announcement else "",
        "footer": "moealturej",
        "color": "7C3AED",
    }
    body = f"""
    <section class='hero'><span class='pill'>{'📣 Premium announcement' if is_announcement else '✨ Premium embed'} composer</span><h1>{title}</h1><p class='muted'>Create a fully custom Discord message: optional text above the embed, optional embed, thumbnail image, large image, footer, color, and a live premium preview.</p></section>
    {'<section class="card" style="margin-top:16px"><span class="pill">✅ Sent successfully</span><p class="muted">Your message was sent to Discord.</p></section>' if sent else ''}
    <div class='section-title'><h2>Compose</h2><a class='btn secondary' href='/guild/{guild.id}'>Back to settings</a></div>
    <form class='grid' method='post'>
      <section class='card'>
        <label>Send to channel<select name='channel_id' required>{channel_options(guild)}</select></label>
        <div class='form-section'><h2>Message outside embed</h2><p class='muted'>This appears above the embed. Use it for pings, short notes, links, or send a plain message only.</p></div>
        <label>Top message / content<textarea id='contentInput' name='content' placeholder='Optional text shown above the embed'>{html.escape(defaults['content'])}</textarea></label>
        <div class='form-section'><h2>Embed builder</h2><label style='display:flex;align-items:center;gap:10px'><input id='embedEnabled' name='embed_enabled' type='checkbox' checked style='width:auto;margin:0'> Include embed</label></div>
        <label>Embed title<input id='titleInput' name='title' maxlength='256' value='{html.escape(defaults['title'])}'></label>
        <label>Embed description<textarea id='messageInput' name='message'>{html.escape(defaults['message'])}</textarea></label>
        <div class='row'><label>Color hex<input id='colorInput' name='color' value='{defaults['color']}' placeholder='7C3AED'></label><label>Footer<input id='footerInput' name='footer' value='{html.escape(defaults['footer'])}'></label></div>
        <div class='row'><label>Thumbnail image URL<input id='thumbInput' name='thumbnail_url' placeholder='Small top-right embed image URL'></label><label>Large image URL<input id='imageInput' name='image_url' placeholder='Large image under embed text URL'></label></div>
        <p class='tiny'>Discord supports one embed thumbnail and one large embed image. The top message is separate from the embed.</p>
        <div class='toolbar'><button type='submit'>{'Send announcement' if is_announcement else 'Send embed'}</button><a class='btn secondary' href='/guild/{guild.id}'>Cancel</a></div>
      </section>
      <section class='card'>
        <h2>Live Preview</h2>
        <p class='muted'>Preview includes the outside message, embed thumbnail, and large image.</p>
        <div class='preview-shell'>
          <div class='preview-message' id='contentPreview'></div>
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
    const contentInput=document.getElementById('contentInput'), embedEnabled=document.getElementById('embedEnabled'), titleInput=document.getElementById('titleInput'), messageInput=document.getElementById('messageInput'), colorInput=document.getElementById('colorInput'), footerInput=document.getElementById('footerInput'), imageInput=document.getElementById('imageInput'), thumbInput=document.getElementById('thumbInput');
    const contentPreview=document.getElementById('contentPreview'), box=document.getElementById('previewBox'), pTitle=document.getElementById('previewTitle'), pDesc=document.getElementById('previewDesc'), pFooter=document.getElementById('previewFooter'), pImg=document.getElementById('previewImg'), pThumb=document.getElementById('previewThumb');
    function cleanHex(v){{v=(v||'7C3AED').replace('#','').trim(); return /^[0-9a-fA-F]{{6}}$/.test(v)?v:'7C3AED'}}
    function setImg(el,url){{url=(url||'').trim(); if(url){{el.src=url; el.style.display='block'}}else{{el.style.display='none'}}}}
    function updatePreview(){{
      const top=(contentInput.value||'').trim(); contentPreview.textContent=top||'No outside message. Only the embed will be sent.'; contentPreview.style.display=top||!embedEnabled.checked?'block':'none';
      box.style.display=embedEnabled.checked?'block':'none'; pTitle.textContent=titleInput.value||'Untitled'; pDesc.textContent=messageInput.value||''; pFooter.textContent=footerInput.value||''; box.style.borderLeftColor='#'+cleanHex(colorInput.value); setImg(pImg,imageInput.value); setImg(pThumb,thumbInput.value);
    }}
    [contentInput,embedEnabled,titleInput,messageInput,colorInput,footerInput,imageInput,thumbInput].forEach(el=>el.addEventListener('input',updatePreview)); embedEnabled.addEventListener('change',updatePreview); updatePreview();
    </script>
    """
    return page(title, body)

async def announcement_page(request: web.Request) -> web.Response:
    user = await get_dashboard_user(request)
    guild_id = int(request.match_info["guild_id"])
    if not user or not await dashboard_can_access(user, guild_id):
        raise web.HTTPForbidden(text=owner_private_message())
    guild = bot.get_guild(guild_id)
    if not guild:
        return page("Missing server", "<section class='card'><h1>Bot is not in this server</h1></section>")
    return composer_page(guild, "announcement", request.query.get("sent") == "1")


async def embed_page(request: web.Request) -> web.Response:
    user = await get_dashboard_user(request)
    guild_id = int(request.match_info["guild_id"])
    if not user or not await dashboard_can_access(user, guild_id):
        raise web.HTTPForbidden(text=owner_private_message())
    guild = bot.get_guild(guild_id)
    if not guild:
        return page("Missing server", "<section class='card'><h1>Bot is not in this server</h1></section>")
    return composer_page(guild, "embed", request.query.get("sent") == "1")


async def send_composer(request: web.Request, mode: str) -> web.Response:
    user = await get_dashboard_user(request)
    guild_id = int(request.match_info["guild_id"])
    if not user or not await dashboard_can_access(user, guild_id):
        raise web.HTTPForbidden(text=owner_private_message())
    guild = bot.get_guild(guild_id)
    if not guild:
        return page("Missing server", "<section class='card'><h1>Bot is not in this server</h1></section>")
    if rate_limiter.on_cooldown(f"dashboard_send:{guild_id}:{user['user_id']}:{mode}", DASHBOARD_SEND_COOLDOWN_SECONDS):
        return page("Slow down", "<section class='card'><h1>Slow down</h1><p class='muted'>Wait a few seconds before sending another dashboard message.</p></section>")
    data = await request.post()
    channel_id = int(str(data.get("channel_id", "0")) or 0)
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return page("Invalid channel", "<section class='card'><h1>Invalid channel</h1><p class='muted'>Choose a text channel the bot can send messages in.</p></section>")

    content = str(data.get("content") or "").strip()[:1900]
    embed_enabled = data.get("embed_enabled") == "on"
    embed = None
    title = str(data.get("title") or ("Announcement" if mode == "announcement" else "Embed"))[:256]

    if embed_enabled:
        message = str(data.get("message") or "").strip()[:4000]
        footer = str(data.get("footer") or "moealturej")[:2048]
        image_url = str(data.get("image_url") or "").strip()
        thumbnail_url = str(data.get("thumbnail_url") or "").strip()
        color = parse_embed_color(str(data.get("color") or ""))
        embed = make_embed(title, message or " ", color)
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        if image_url:
            embed.set_image(url=image_url)
        if footer:
            embed.set_footer(text=footer)

    if not content and not embed:
        return page("Nothing to send", "<section class='card'><h1>Nothing to send</h1><p class='muted'>Add a top message, enable the embed, or both.</p></section>")

    sent_message = await safe_channel_send(channel, content=content or None, embed=embed, allowed_mentions=discord.AllowedMentions.all() if mode == "announcement" else discord.AllowedMentions.none())
    if not sent_message:
        return page("Send failed", "<section class='card'><h1>Discord rejected the send</h1><p class='muted'>The bot hit a temporary Discord limit or lacks permission. Try again shortly.</p></section>")
    await save_event("dashboard_events", {"guild_id": guild.id, "user_id": int(user["user_id"]), "event": f"send_{mode}", "channel_id": channel.id, "title": title, "has_content": bool(content), "has_embed": bool(embed)})
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
    sent = request.query.get("sent") == "1"
    failed = request.query.get("failed") == "1"
    reason = request.query.get("reason", "")[:180]
    body = f"""
    <section class='hero'><span class='pill'>💌 Premium DM composer</span><h1>User DM Sender</h1><p class='muted'>Send fully custom private DMs with text above the embed, optional embed, thumbnail image, large image, footer, color, and a live Discord-style preview.</p></section>
    {'<section class="card" style="margin-top:16px"><span class="pill">✅ DM sent</span><p class="muted">The private message was delivered successfully.</p></section>' if sent else ''}
    {'<section class="card" style="margin-top:16px"><span class="pill" style="background:rgba(251,113,133,.12);border-color:rgba(251,113,133,.28);color:#fecdd3">⚠️ DM failed</span><p class="muted">' + html.escape(reason or 'The bot could not DM that user. They may have DMs disabled or the ID was invalid.') + '</p></section>' if failed else ''}
    <div class='section-title'><h2>Compose private message</h2><a class='btn secondary' href='/guild/{guild.id}'>Back to settings</a></div>
    <form class='grid' method='post'>
      <section class='card'>
        <label>Choose cached member<select id='memberSelect'>{member_select_options(guild)}</select></label>
        <label>Discord user ID<input id='userIdInput' name='user_id' inputmode='numeric' pattern='[0-9]{{15,25}}' placeholder='1222903158125105194' required></label>
        <div class='form-section'><h2>Message outside embed</h2><p class='muted'>This appears as normal DM text above the embed. You can send only this, only an embed, or both.</p></div>
        <label>Top DM message<textarea id='plainInput' name='plain_message' placeholder='Custom text shown above the embed'></textarea></label>
        <div class='form-section'><h2>Optional embed</h2><label style='display:flex;align-items:center;gap:10px'><input id='embedEnabled' name='embed_enabled' type='checkbox' checked style='width:auto;margin:0'> Include embed</label></div>
        <label>Embed title<input id='titleInput' name='title' maxlength='256' value='Message from moealturej'></label>
        <label>Embed description<textarea id='messageInput' name='message'>Write your custom DM embed here...</textarea></label>
        <div class='row'><label>Color hex<input id='colorInput' name='color' value='7C3AED' placeholder='7C3AED'></label><label>Footer<input id='footerInput' name='footer' value='moealturej'></label></div>
        <div class='row'><label>Thumbnail image URL<input id='thumbInput' name='thumbnail_url' placeholder='Small top-right embed image URL'></label><label>Large image URL<input id='imageInput' name='image_url' placeholder='Large image under embed text URL'></label></div>
        <p class='tiny'>Use thumbnail for a small logo/profile image and large image for banners or previews.</p>
        <div class='toolbar'><button type='submit'>Send private DM</button><a class='btn secondary' href='/guild/{guild.id}'>Cancel</a></div>
      </section>
      <section class='card'>
        <h2>Live Preview</h2>
        <p class='muted'>This is a close preview of the DM the user will receive.</p>
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
    function cleanHex(v){{v=(v||'7C3AED').replace('#','').trim(); return /^[0-9a-fA-F]{{6}}$/.test(v)?v:'7C3AED'}}
    function setImg(el,url){{url=(url||'').trim(); if(url){{el.src=url; el.style.display='block'}}else{{el.style.display='none'}}}}
    function updatePreview(){{
      const plain=(plainInput.value||'').trim(); plainPreview.textContent=plain||'No outside DM message. Only the embed will be sent.'; plainPreview.style.display=plain||!embedEnabled.checked?'block':'none';
      box.style.display=embedEnabled.checked?'block':'none'; pTitle.textContent=titleInput.value||'Untitled'; pDesc.textContent=messageInput.value||''; pFooter.textContent=footerInput.value||''; box.style.borderLeftColor='#'+cleanHex(colorInput.value); setImg(pImg,imageInput.value); setImg(pThumb,thumbInput.value);
    }}
    [plainInput,embedEnabled,titleInput,messageInput,colorInput,footerInput,imageInput,thumbInput].forEach(el=>el.addEventListener('input',updatePreview)); embedEnabled.addEventListener('change',updatePreview); updatePreview();
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
    if rate_limiter.on_cooldown(f"dashboard_dm:{guild_id}:{user['user_id']}", DASHBOARD_SEND_COOLDOWN_SECONDS):
        raise web.HTTPFound(f"/guild/{guild_id}/dms?failed=1&reason=Wait+a+few+seconds+before+sending+another+DM")
    data = await request.post()
    raw_user_id = str(data.get("user_id", "")).strip()
    if not raw_user_id.isdigit():
        raise web.HTTPFound(f"/guild/{guild.id}/dms?failed=1&reason=Invalid+Discord+user+ID")
    target_id = int(raw_user_id)
    plain_message = str(data.get("plain_message") or "").strip()[:1900]
    embed_enabled = data.get("embed_enabled") == "on"
    embed = None
    if embed_enabled:
        title = str(data.get("title") or "Message from moealturej")[:256]
        message = str(data.get("message") or "").strip()[:4000]
        footer = str(data.get("footer") or "moealturej")[:2048]
        image_url = str(data.get("image_url") or "").strip()
        thumbnail_url = str(data.get("thumbnail_url") or "").strip()
        color = parse_embed_color(str(data.get("color") or ""))
        embed = make_embed(title, message or " ", color)
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
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' https: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://discord.com"
    response.headers["X-Request-ID"] = request_id
    return response


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
    app.router.add_get("/login", login)
    app.router.add_get("/oauth/callback", oauth_callback)
    app.router.add_get("/logout", logout)
    app.router.add_get("/guild/{guild_id}", guild_page)
    app.router.add_post("/guild/{guild_id}", guild_save)
    app.router.add_get("/guild/{guild_id}/announcements", announcement_page)
    app.router.add_post("/guild/{guild_id}/announcements", announcement_send)
    app.router.add_get("/guild/{guild_id}/embeds", embed_page)
    app.router.add_post("/guild/{guild_id}/embeds", embed_send)
    app.router.add_get("/guild/{guild_id}/dms", dm_page)
    app.router.add_get("/guild/{guild_id}/activity", activity_page)
    app.router.add_post("/guild/{guild_id}/dms", dm_send)
    app.router.add_get("/verify/start", verify_start)
    app.router.add_get("/verify/callback", verify_callback)
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
        if config.get("unverified_role") and not (verified_role and verified_role in member.roles):
            await safe_add_role(member, config.get("unverified_role"), "Unverified role on join")
        if config.get("auto_role"):
            await safe_add_role(member, config.get("auto_role"), "Auto role on join")
        channel = member.guild.get_channel(int(config.get("welcome_channel") or 0))
        if config.get("welcome_enabled", True) and isinstance(channel, discord.TextChannel) and not rate_limiter.on_cooldown(f"welcome:{member.guild.id}", MEMBER_JOIN_WELCOME_COOLDOWN_SECONDS):
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
    if not ROTATING_STATUSES: return
    status = ROTATING_STATUSES[rotate_status.current_loop % len(ROTATING_STATUSES)]
    activity = discord.Activity(type=discord.ActivityType.watching, name=status[9:]) if status.lower().startswith("watching ") else discord.Game(name=status)
    await safe_change_presence(status=discord.Status.online, activity=activity)


@tasks.loop(minutes=STATS_UPDATE_MINUTES)
async def update_stats():
    if rate_limiter.is_globally_blocked():
        log.warning("Skipping stats update while Discord global cooldown/circuit breaker is active (%.0fs left)", rate_limiter.seconds_until_unblocked())
        return
    for guild in bot.guilds:
        config = await get_guild_config(guild.id)
        channels = config.get("stats_channels", {})
        humans = len([m for m in guild.members if not m.bot])
        bots = len([m for m in guild.members if m.bot])
        members = guild.member_count or len(guild.members)
        boosts = guild.premium_subscription_count or 0
        stats = {"members": f"👥 Members: {members}", "humans": f"🧑 Humans: {humans}", "bots": f"🤖 Bots: {bots}", "boosts": f"🚀 Boosts: {boosts}"}
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
    await safe_interaction_send(interaction, embed=make_branded_embed(config, "Pong", f"Discord latency: `{round(bot.latency * 1000)}ms`", SUCCESS_COLOR), ephemeral=True)


@bot.tree.command(name="store", description="Get the store link.")
@guild_enabled_or_owner()
async def store(interaction: discord.Interaction):
    config = await get_guild_config(interaction.guild.id) if interaction.guild else {"store_url": DEFAULT_STORE_URL}
    await safe_interaction_send(interaction, embed=make_branded_embed(config, "Store", f"Visit the store here:\n{config.get('store_url', DEFAULT_STORE_URL)}"), ephemeral=True)


@bot.tree.command(name="help", description="Show available commands.")
async def help_command(interaction: discord.Interaction):
    config = await get_guild_config(interaction.guild.id) if interaction.guild else DEFAULT_GUILD_CONFIG
    embed = make_branded_embed(config, "Command Center", "Useful commands are grouped below so members can find what they need quickly.")
    embed.add_field(name="Essentials", value="`/ping` latency • `/store` store link • `/serverinfo` server details • `/userinfo` member details • `/avatar` avatar", inline=False)
    if config.get("casino_enabled", True):
        embed.add_field(name="Blackjack", value="`/blackjack` play • `/balance` wallet • `/daily` daily credits • `/casino_leaderboard` rankings", inline=False)
    embed.add_field(name="Support", value="Use the server's Support Center panel to open a private ticket.", inline=False)
    await safe_interaction_send(interaction, embed=embed, ephemeral=True)


@bot.tree.command(name="commands", description="Show private owner/admin commands.")
@admin_only()
async def commands_menu(interaction: discord.Interaction):
    config = await get_guild_config(interaction.guild.id)
    embed = make_branded_embed(config, "Admin Commands", "Private setup and operations commands.")
    embed.add_field(name="Setup", value="`/setup_audit` `/setup_enable` `/set_admin_role` `/set_verified_role` `/set_unverified_role` `/set_auto_role` `/set_logs` `/set_ticket_category` `/set_ticket_role` `/stats_setup`", inline=False)
    embed.add_field(name="Panels", value="`/send_verification_panel` `/send_ticket_panel`", inline=False)
    embed.add_field(name="Content", value="`/set_store` `/announce` `/config_show`", inline=False)
    embed.add_field(name="Moderation", value="`/purge` `/timeout` `/untimeout` `/warn` `/warnings` `/slowmode` `/lock` `/unlock`", inline=False)
    embed.add_field(name="Dashboard", value=f"{PUBLIC_BASE_URL}/", inline=False)
    await safe_interaction_send(interaction, embed=embed, ephemeral=True)


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
    await set_config(interaction.guild.id, {"store_url": url})
    await safe_interaction_send(interaction, f"Store URL set to: {url}", ephemeral=True)


@bot.tree.command(name="announce", description="Send a clean announcement embed.")
@admin_only()
async def announce(interaction: discord.Interaction, channel: discord.TextChannel, title: str, message: str, image_url: Optional[str] = None):
    config = await get_guild_config(interaction.guild.id)
    embed = make_branded_embed(config, title, message)
    if image_url or config.get("announce_image"):
        embed.set_image(url=image_url or config.get("announce_image"))
    embed.set_footer(text=config.get("announce_footer") or "moealturej")
    await safe_channel_send(channel, embed=embed)
    await safe_interaction_send(interaction, f"Announcement sent in {channel.mention}.", ephemeral=True)


@bot.tree.command(name="stats_setup", description="Create/connect emoji live server stats voice channels.")
@admin_only()
async def stats_setup(interaction: discord.Interaction, category: Optional[discord.CategoryChannel] = None):
    await safe_interaction_defer(interaction, ephemeral=True)
    guild = interaction.guild
    if category is None:
        category = await safe_create_category(guild, "📊 Server Stats", reason="Live server stats setup")
    if category is None:
        return await safe_interaction_send(interaction, "Discord is busy right now. Please try stats setup again in a minute.", ephemeral=True)
    overwrites = {guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=True), guild.me: discord.PermissionOverwrite(connect=True, manage_channels=True, view_channel=True)}
    defaults = {"members": "👥 Members: 0", "humans": "🧑 Humans: 0", "bots": "🤖 Bots: 0", "boosts": "🚀 Boosts: 0"}
    created = {}
    config = await get_guild_config(guild.id)
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

# =========================
# BLACKJACK / VIRTUAL ECONOMY
# =========================
@bot.tree.command(name="blackjack", description="Play an interactive game of blackjack with virtual server credits.")
@app_commands.describe(bet="Credits to wager", private="Only show the table to you")
@app_commands.checks.cooldown(1, 5.0, key=lambda i: (i.guild_id, i.user.id))
@guild_enabled_or_owner()
async def blackjack(interaction: discord.Interaction, bet: app_commands.Range[int, 1, 100_000_000], private: bool = False):
    if not interaction.guild:
        return await safe_interaction_send(interaction, "Blackjack is only available inside a server.", ephemeral=True)
    config = await get_guild_config(interaction.guild.id)
    if not config.get("casino_enabled", True):
        return await safe_interaction_send(interaction, "The blackjack economy is disabled in this server.", ephemeral=True)
    minimum = max(1, int(config.get("blackjack_min_bet", 10)))
    maximum = max(minimum, int(config.get("blackjack_max_bet", 5000)))
    if int(bet) < minimum or int(bet) > maximum:
        currency = str(config.get("casino_currency") or "credits")
        return await safe_interaction_send(interaction, f"Your bet must be between **{format_amount(minimum, currency)}** and **{format_amount(maximum, currency)}**.", ephemeral=True)

    await interaction.response.defer(ephemeral=private, thinking=True)
    store = CasinoStore(mdb)
    session_id = ""
    try:
        ok, message, wallet = await store.reserve_game(
            interaction.guild.id,
            interaction.user.id,
            int(bet),
            max(0, int(config.get("casino_starting_balance", 1000))),
        )
        if not ok or not wallet:
            return await interaction.edit_original_response(content=message)
        session_id = str(wallet["session_id"])
        currency = str(config.get("casino_currency") or "credits")[:24]
        view = BlackjackView(
            store=store,
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            display_name=getattr(interaction.user, "display_name", interaction.user.name),
            session_id=session_id,
            bet=int(bet),
            currency=currency,
            balance_after_bet=int(wallet.get("balance", 0)),
            embed_factory=lambda title, description, color: make_branded_embed(config, title, description, color),
            report_error=report_exception,
            dealer_hits_soft_17=bool(config.get("blackjack_dealer_hits_soft_17")),
        )
        await view.resolve_initial()
        await interaction.edit_original_response(content=None, embed=view.build_embed(), view=view)
        view.message = await interaction.original_response()
        await save_event("casino_events", {"guild_id": interaction.guild.id, "user_id": interaction.user.id, "event": "blackjack_started", "bet": int(bet)})
    except Exception as exc:
        if session_id:
            await store.abandon(session_id, interaction.guild.id, interaction.user.id, int(bet), refund=True)
        incident = await report_exception("blackjack_command", exc, guild_id=interaction.guild.id, user_id=interaction.user.id)
        await interaction.edit_original_response(content=f"The table could not be opened. Your bet was refunded. Reference: `{incident}`", embed=None, view=None)


@bot.tree.command(name="balance", description="Check your virtual casino balance and blackjack record.")
@guild_enabled_or_owner()
async def balance(interaction: discord.Interaction):
    if not interaction.guild:
        return await safe_interaction_send(interaction, "Balances are server-specific.", ephemeral=True)
    config = await get_guild_config(interaction.guild.id)
    store = CasinoStore(mdb)
    wallet = await store.ensure_wallet(interaction.guild.id, interaction.user.id, max(0, int(config.get("casino_starting_balance", 1000))))
    currency = str(config.get("casino_currency") or "credits")
    embed = make_branded_embed(config, f"{interaction.user.display_name}'s Wallet", "Virtual credits are for server entertainment only and have no cash value.")
    embed.add_field(name="Balance", value=format_amount(int(wallet.get("balance", 0)), currency), inline=False)
    embed.add_field(name="Wins", value=str(wallet.get("wins", 0)), inline=True)
    embed.add_field(name="Losses", value=str(wallet.get("losses", 0)), inline=True)
    embed.add_field(name="Pushes", value=str(wallet.get("pushes", 0)), inline=True)
    embed.add_field(name="Blackjacks", value=str(wallet.get("blackjacks", 0)), inline=True)
    embed.add_field(name="Total wagered", value=format_amount(int(wallet.get("wagered", 0)), currency), inline=True)
    embed.add_field(name="Net result", value=format_amount(int(wallet.get("profit", 0)), currency), inline=True)
    await safe_interaction_send(interaction, embed=embed, ephemeral=True)


@bot.tree.command(name="daily", description="Claim the server's daily virtual-credit reward.")
@app_commands.checks.cooldown(1, 5.0, key=lambda i: (i.guild_id, i.user.id))
@guild_enabled_or_owner()
async def daily(interaction: discord.Interaction):
    if not interaction.guild:
        return await safe_interaction_send(interaction, "Daily rewards are server-specific.", ephemeral=True)
    config = await get_guild_config(interaction.guild.id)
    if not config.get("casino_enabled", True):
        return await safe_interaction_send(interaction, "The casino economy is disabled here.", ephemeral=True)
    store = CasinoStore(mdb)
    await store.ensure_wallet(interaction.guild.id, interaction.user.id, max(0, int(config.get("casino_starting_balance", 1000))))
    today = utcnow().date().isoformat()
    reward = max(0, int(config.get("casino_daily_reward", 250)))
    wallet = await mdb.casino_wallets.find_one_and_update(
        {"guild_id": interaction.guild.id, "user_id": interaction.user.id, "last_daily_claim": {"$ne": today}},
        {"$inc": {"balance": reward}, "$set": {"last_daily_claim": today, "updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )
    currency = str(config.get("casino_currency") or "credits")
    if not wallet:
        return await safe_interaction_send(interaction, "You already claimed today's reward. The next claim unlocks after **00:00 UTC**.", ephemeral=True)
    embed = make_branded_embed(config, "Daily reward claimed", f"You received **{format_amount(reward, currency)}**.", SUCCESS_COLOR)
    embed.add_field(name="New balance", value=format_amount(int(wallet.get("balance", 0)), currency), inline=False)
    await safe_interaction_send(interaction, embed=embed, ephemeral=True)


@bot.tree.command(name="casino_leaderboard", description="Show the richest virtual-credit wallets in this server.")
@guild_enabled_or_owner()
async def casino_leaderboard(interaction: discord.Interaction):
    if not interaction.guild:
        return await safe_interaction_send(interaction, "Leaderboards are server-specific.", ephemeral=True)
    config = await get_guild_config(interaction.guild.id)
    currency = str(config.get("casino_currency") or "credits")
    wallets = await mdb.casino_wallets.find({"guild_id": interaction.guild.id}, {"_id": 0}).sort("balance", -1).limit(10).to_list(length=10)
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for index, wallet in enumerate(wallets, 1):
        member = interaction.guild.get_member(int(wallet.get("user_id", 0)))
        name = member.display_name if member else f"User {wallet.get('user_id', 'unknown')}"
        prefix = medals[index - 1] if index <= 3 else f"`#{index}`"
        lines.append(f"{prefix} **{discord.utils.escape_markdown(name)}** — {format_amount(int(wallet.get('balance', 0)), currency)}")
    embed = make_branded_embed(config, "Casino Leaderboard", "\n".join(lines) if lines else "No one has opened a casino wallet yet.")
    embed.set_footer(text=f"Virtual {currency} have no real-world value")
    await safe_interaction_send(interaction, embed=embed)


@bot.tree.command(name="setup_audit", description="Check permissions, role hierarchy, channels, and production configuration.")
@admin_only()
async def setup_audit(interaction: discord.Interaction):
    guild = interaction.guild
    config = await get_guild_config(guild.id)
    me = guild.me
    issues: list[str] = []
    passed: list[str] = []
    required_permissions = {
        "Manage Roles": me.guild_permissions.manage_roles,
        "Manage Channels": me.guild_permissions.manage_channels,
        "Send Messages": me.guild_permissions.send_messages,
        "Embed Links": me.guild_permissions.embed_links,
        "Attach Files": me.guild_permissions.attach_files,
        "Read Message History": me.guild_permissions.read_message_history,
    }
    for label, ok in required_permissions.items():
        (passed if ok else issues).append(f"{'✅' if ok else '❌'} {label}")
    verified_role = guild.get_role(int(config.get("verified_role") or 0))
    if not verified_role:
        issues.append("❌ Verified role is not configured")
    elif verified_role >= me.top_role:
        issues.append("❌ Verified role must be below the bot's highest role")
    else:
        passed.append("✅ Verified role hierarchy")
    if not isinstance(guild.get_channel(int(config.get("ticket_category") or 0)), discord.CategoryChannel):
        issues.append("❌ Ticket category is not configured")
    else:
        passed.append("✅ Ticket category")
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET:
        issues.append("❌ Discord OAuth environment values are missing")
    else:
        passed.append("✅ OAuth environment values")
    embed = make_branded_embed(config, "Production Setup Audit", "Fix the red items before sending public panels.", SUCCESS_COLOR if not issues else WARNING_COLOR)
    embed.add_field(name="Ready", value="\n".join(passed) or "None yet", inline=False)
    embed.add_field(name="Needs attention", value="\n".join(issues) or "✅ No blocking issues found", inline=False)
    embed.add_field(name="Dashboard", value=PUBLIC_BASE_URL, inline=False)
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
    amount = min(int(amount), MAX_PURGE_AMOUNT)
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
    await safe_interaction_send(interaction, f"Timed out {member.mention} for **{minutes} minutes**.", ephemeral=True)


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
    await safe_user_send(member, embed=make_branded_embed(config, f"Warning in {interaction.guild.name}", reason, ERROR_COLOR))
    await safe_interaction_send(interaction, f"Warned {member.mention}. They now have **{count}** warning(s).", ephemeral=True)


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
