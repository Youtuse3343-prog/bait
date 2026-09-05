from __future__ import annotations
import os
import threading
from waitress import serve
from bot.client import ProfessionalBot
from bot.config import settings
from web.app import create_app
from web.keepalive import KeepAliveService


def validate_config():
    missing = []
    for name, value in {
        "DISCORD_TOKEN": settings.token,
        "DISCORD_CLIENT_ID": settings.client_id,
        "DISCORD_CLIENT_SECRET": settings.client_secret,
        "OWNER_ID": settings.owner_id,
        "SECRET_KEY": settings.secret_key,
    }.items():
        if not value or value == "change-me" or value == "replace-with-a-long-random-secret" or str(value).startswith("YOUR_"):
            missing.append(name)
    if missing:
        raise SystemExit("Missing/unsafe configuration: " + ", ".join(missing) + ". Copy .env.example to .env and fill it in.")


def run_web(app):
    serve(app, host="0.0.0.0", port=settings.port, threads=8)


def main():
    validate_config()
    bot = ProfessionalBot()
    keep_alive = KeepAliveService()
    app = create_app(bot, keep_alive)
    threading.Thread(target=run_web, args=(app,), daemon=True, name="dashboard").start()
    keep_alive.start()
    bot.run(settings.token, log_handler=None)


if __name__ == "__main__":
    main()
