"""Safe production preflight for the moealturej bot.

This script never prints secret values. It only validates that required settings exist
and that public URLs / numeric options look reasonable.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse


def load_simple_dotenv() -> None:
    path = Path(__file__).with_name(".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def valid_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def main() -> int:
    load_simple_dotenv()
    errors: list[str] = []
    warnings: list[str] = []

    required = ["BOT_TOKEN", "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "OWNER_USER_ID", "MONGO_URI", "DASHBOARD_SECRET", "PUBLIC_BASE_URL"]
    for key in required:
        if not os.getenv(key, "").strip():
            errors.append(f"{key} is missing")

    secret = os.getenv("DASHBOARD_SECRET", "")
    if secret and len(secret) < 32:
        warnings.append("DASHBOARD_SECRET is shorter than 32 characters")

    owner_id = os.getenv("OWNER_USER_ID", "")
    if owner_id and (not owner_id.isdigit() or not 15 <= len(owner_id) <= 25):
        errors.append("OWNER_USER_ID does not look like a Discord snowflake")

    public_url = os.getenv("PUBLIC_BASE_URL", "")
    if public_url and not valid_http_url(public_url):
        errors.append("PUBLIC_BASE_URL must be a complete http:// or https:// URL")
    if public_url.startswith("http://") and "localhost" not in public_url and "127.0.0.1" not in public_url:
        warnings.append("PUBLIC_BASE_URL is HTTP; production OAuth/cookies should use HTTPS")

    for key in ("DEFAULT_STORE_URL",):
        value = os.getenv(key, "")
        if value and not valid_http_url(value):
            errors.append(f"{key} must be a complete http:// or https:// URL")

    numeric_ranges = {
        "WEB_SESSION_DAYS": (1, 30),
        "MAX_PURGE_AMOUNT": (1, 100),
        "STATS_UPDATE_MINUTES": (1, 1440),
        "DISCORD_MAX_RETRIES": (0, 10),
    }
    for key, (low, high) in numeric_ranges.items():
        value = os.getenv(key, "")
        if not value:
            continue
        try:
            number = int(value)
        except ValueError:
            errors.append(f"{key} must be an integer")
            continue
        if not low <= number <= high:
            warnings.append(f"{key} is outside the recommended range {low}–{high}")

    print("moealturej Bot 4.0 preflight")
    print("- secrets: present/missing checks only; values are never printed")
    if warnings:
        print("\nWarnings:")
        for item in warnings:
            print(f"  ! {item}")
    if errors:
        print("\nErrors:")
        for item in errors:
            print(f"  x {item}")
        print(f"\nFAILED: {len(errors)} blocking issue(s), {len(warnings)} warning(s).")
        return 1
    print(f"\nPASS: no blocking issues, {len(warnings)} warning(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
