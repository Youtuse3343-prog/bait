import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)).strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    token: str = os.getenv("DISCORD_TOKEN", "")
    client_id: str = os.getenv("DISCORD_CLIENT_ID", "")
    client_secret: str = os.getenv("DISCORD_CLIENT_SECRET", "")
    owner_id: int = _int("OWNER_ID")
    oauth_redirect_uri: str = os.getenv("OAUTH_REDIRECT_URI", "http://127.0.0.1:5000/oauth/callback")
    dashboard_base_url: str = os.getenv("DASHBOARD_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
    secret_key: str = os.getenv("SECRET_KEY", "change-me")
    mongodb_uri: str = os.getenv("MONGODB_URI", "").strip()
    mongodb_db: str = os.getenv("MONGODB_DB", "professional_discord_bot")
    port: int = _int("PORT", 5000)
    session_cookie_secure: bool = _bool("SESSION_COOKIE_SECURE", False)
    sync_commands_on_start: bool = _bool("SYNC_COMMANDS_ON_START", True)
    dev_guild_id: int = _int("DEV_GUILD_ID")
    process_mode: str = os.getenv("PROCESS_MODE", "combined").strip().lower()  # combined | bot | web
    internal_api_key: str = os.getenv("INTERNAL_API_KEY", "").strip()
    web_action_poll_seconds: int = max(2, _int("ACTION_POLL_SECONDS", 5))


settings = Settings()
