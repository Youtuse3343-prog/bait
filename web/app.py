from __future__ import annotations
import asyncio
import json
import secrets
import time
from collections import defaultdict, deque
from functools import wraps
from urllib.parse import urlencode

import requests
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from bot.config import settings
from bot.store import store

DISCORD_API = "https://discord.com/api/v10"


def create_app(bot, keep_alive=None):
    app = Flask(__name__)
    app.secret_key = settings.secret_key
    rate_buckets = defaultdict(deque)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=settings.session_cookie_secure,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        resp.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' https://cdn.discordapp.com https://media.discordapp.net https://www.moealturej.com https://moealturej.com data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://discord.com"
        if request.is_secure:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return resp

    def owner_required(fn):
        @wraps(fn)
        def inner(*args, **kwargs):
            if int(session.get("owner_id", 0) or 0) != settings.owner_id:
                return redirect(url_for("login"))
            return fn(*args, **kwargs)
        return inner

    def csrf_token():
        token = session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf"] = token
        return token

    def check_csrf():
        expected = session.get("csrf", "")
        actual = request.form.get("csrf", "")
        if not expected or not secrets.compare_digest(expected, actual):
            abort(400, "Invalid CSRF token")

    app.jinja_env.globals["csrf_token"] = csrf_token

    def rate_limit(bucket: str, limit: int = 20, window: int = 60):
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
        key = (bucket, ip)
        now = time.time()
        q = rate_buckets[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            abort(429, "Too many requests. Try again shortly.")
        q.append(now)

    def begin_oauth(purpose: str, guild_id: int | None = None):
        state = secrets.token_urlsafe(32)
        flows = session.get("oauth_flows", {})
        flows[state] = {"purpose": purpose, "guild_id": guild_id, "created": int(time.time())}
        session["oauth_flows"] = {k: v for k, v in list(flows.items())[-6:]}
        params = {
            "client_id": settings.client_id,
            "redirect_uri": settings.oauth_redirect_uri,
            "response_type": "code",
            "scope": "identify",
            "state": state,
            "prompt": "consent",
        }
        return redirect("https://discord.com/oauth2/authorize?" + urlencode(params))

    def exchange_user(code: str):
        token_resp = requests.post(
            DISCORD_API + "/oauth2/token",
            data={
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.oauth_redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        token_resp.raise_for_status()
        access = token_resp.json()["access_token"]
        user_resp = requests.get(DISCORD_API + "/users/@me", headers={"Authorization": f"Bearer {access}"}, timeout=10)
        user_resp.raise_for_status()
        return user_resp.json()

    def guild_or_404(guild_id: int):
        guild = bot.get_guild(int(guild_id))
        if not guild:
            abort(404)
        return guild

    @app.get("/health")
    def health():
        return {
            "ok": True,
            "bot_ready": bot.is_ready(),
            "guilds": len(bot.guilds),
            "keep_alive_worker": bool(keep_alive and keep_alive.snapshot().get("running")),
        }

    @app.get("/")
    def index():
        if session.get("owner_id") == settings.owner_id:
            return redirect(url_for("dashboard"))
        return render_template("login.html")

    @app.get("/login")
    def login():
        rate_limit("owner-login", 12, 60)
        if not settings.client_id or not settings.client_secret or not settings.owner_id:
            return render_template("error.html", message="OAuth is not configured. Fill DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, OWNER_ID and OAUTH_REDIRECT_URI in .env."), 503
        return begin_oauth("owner")

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.get("/oauth/callback")
    @app.get("/verify/callback")
    def oauth_callback():
        rate_limit("oauth-callback", 30, 60)
        code = request.args.get("code", "")
        state = request.args.get("state", "")
        flows = session.get("oauth_flows", {})
        flow = flows.pop(state, None) if state else None
        session["oauth_flows"] = flows
        if not flow or int(time.time()) - int(flow.get("created", 0)) > 600:
            return render_template("error.html", message="OAuth session expired or invalid. Start again."), 400
        if not code:
            return render_template("error.html", message="Discord OAuth was cancelled or did not return a code."), 400
        try:
            user = exchange_user(code)
        except requests.RequestException:
            return render_template("error.html", message="Discord OAuth could not be completed."), 502

        purpose = flow.get("purpose")
        if purpose == "owner":
            if int(user["id"]) != settings.owner_id:
                session.clear()
                return render_template("error.html", message="This dashboard is owner-only. Your Discord account is not authorized."), 403
            session.clear()
            session.permanent = True
            session["owner_id"] = int(user["id"])
            session["owner_name"] = user.get("global_name") or user.get("username")
            csrf_token()
            return redirect(url_for("dashboard"))

        if purpose == "verify":
            gid = int(flow.get("guild_id") or 0)
            if not gid:
                return render_template("error.html", message="Verification target is missing."), 400
            try:
                future = asyncio.run_coroutine_threadsafe(bot.apply_verification(gid, int(user["id"])), bot.loop)
                ok, message = future.result(timeout=12)
            except Exception:
                ok, message = False, "The bot could not complete the Discord role update."
            return render_template("verify_result.html", ok=ok, message=message)

        return render_template("error.html", message="Unknown OAuth flow."), 400

    @app.get("/verify/<int:guild_id>")
    def verify(guild_id: int):
        guild = guild_or_404(guild_id)
        cfg = store.get_guild(guild_id)
        if not cfg["features"]["verification"]:
            return render_template("error.html", message="Verification is disabled for this server."), 403
        return render_template("verify.html", guild=guild, cfg=cfg)

    @app.post("/verify/<int:guild_id>/start")
    def verify_start(guild_id: int):
        rate_limit("verify-start", 20, 60)
        guild_or_404(guild_id)
        return begin_oauth("verify", guild_id)

    @app.get("/dashboard")
    @owner_required
    def dashboard():
        guilds = sorted(bot.guilds, key=lambda g: g.name.lower())
        return render_template("dashboard.html", guilds=guilds)

    @app.route("/dashboard/guild/<int:guild_id>", methods=["GET", "POST"])
    @owner_required
    def guild_settings(guild_id: int):
        guild = guild_or_404(guild_id)
        cfg = store.get_guild(guild_id)
        if request.method == "POST":
            check_csrf()
            section = request.form.get("section", "settings")
            if section == "settings":
                for key in cfg["features"]:
                    cfg["features"][key] = request.form.get(f"feature_{key}") == "on"
                for key in cfg["channels"]:
                    raw = request.form.get(f"channel_{key}", "").strip()
                    cfg["channels"][key] = int(raw) if raw.isdigit() else None
                for key in cfg["roles"]:
                    raw = request.form.get(f"role_{key}", "").strip()
                    cfg["roles"][key] = int(raw) if raw.isdigit() else None
                cfg["welcome"]["content"] = request.form.get("welcome_content", cfg["welcome"]["content"])[:1900]
                cfg["welcome"]["dm_enabled"] = request.form.get("welcome_dm_enabled") == "on"
                cfg["welcome"]["dm_content"] = request.form.get("welcome_dm_content", cfg["welcome"]["dm_content"])[:1900]
                for key in ["panel_title", "panel_description", "button_label", "opening_message"]:
                    cfg["tickets"][key] = request.form.get(f"ticket_{key}", cfg["tickets"][key])[:1900]
                for key in ["panel_title", "panel_description", "button_label", "success_message"]:
                    cfg["verification"][key] = request.form.get(f"verification_{key}", cfg["verification"][key])[:1900]
                cfg["moderation"]["dm_on_action"] = request.form.get("moderation_dm_on_action") == "on"
                store.set_guild(guild_id, cfg)
                flash("Server configuration saved.", "success")
            elif section == "automessage_add":
                channel_id = request.form.get("am_channel", "")
                interval = request.form.get("am_interval", "")
                content = request.form.get("am_content", "").strip()
                if channel_id.isdigit() and interval.isdigit() and content:
                    store.add_automessage(guild_id, int(channel_id), content[:1900], max(5, min(10080, int(interval))))
                    flash("Automatic message added.", "success")
            elif section == "automessage_update":
                mid = request.form.get("message_id", "")
                channel_id = request.form.get("am_channel", "")
                interval = request.form.get("am_interval", "")
                content = request.form.get("am_content", "").strip()
                if mid.isdigit() and channel_id.isdigit() and interval.isdigit() and content:
                    store.update_automessage(guild_id, int(mid), channel_id=int(channel_id), content=content[:1900], interval_minutes=max(5, min(10080, int(interval))), enabled=request.form.get("am_enabled") == "on")
                    flash("Automatic message updated.", "success")
            elif section == "automessage_delete":
                mid = request.form.get("message_id", "")
                if mid.isdigit():
                    store.delete_automessage(guild_id, int(mid))
                    flash("Automatic message removed.", "success")
            elif section in {"post_ticket_panel", "post_verification_panel"}:
                channel_id = request.form.get("panel_channel", "")
                if not channel_id.isdigit():
                    flash("Choose a text channel.", "error")
                    return redirect(url_for("guild_settings", guild_id=guild_id))
                coro = bot.post_dashboard_ticket_panel(guild_id, int(channel_id)) if section == "post_ticket_panel" else bot.post_dashboard_verification_panel(guild_id, int(channel_id))
                fut = asyncio.run_coroutine_threadsafe(coro, bot.loop)
                try: ok = fut.result(timeout=10)
                except Exception: ok = False
                flash("Panel posted." if ok else "Could not post that panel. Check the feature switch and bot permissions.", "success" if ok else "error")
            elif section == "send_announcement":
                if not cfg["features"]["announcements"]:
                    flash("Announcements are disabled for this server.", "error")
                    return redirect(url_for("guild_settings", guild_id=guild_id))
                channel_id = request.form.get("action_channel", "")
                content = request.form.get("action_content", "").strip()
                ch = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
                if ch and content:
                    fut = asyncio.run_coroutine_threadsafe(bot.send_dashboard_message(ch.id, content, True), bot.loop)
                    try: fut.result(timeout=10)
                    except Exception: flash("Discord rejected the message.", "error")
                    else: flash("Announcement sent.", "success")
            elif section == "send_dm":
                if not cfg["features"]["bot_dms"]:
                    flash("Bot DMs are disabled for this server.", "error")
                    return redirect(url_for("guild_settings", guild_id=guild_id))
                user_id = request.form.get("dm_user_id", "").strip()
                content = request.form.get("dm_content", "").strip()
                if user_id.isdigit() and content:
                    fut = asyncio.run_coroutine_threadsafe(bot.send_dashboard_dm(int(user_id), content), bot.loop)
                    try: ok = fut.result(timeout=10)
                    except Exception: ok = False
                    flash("DM sent." if ok else "Could not DM that user.", "success" if ok else "error")
            return redirect(url_for("guild_settings", guild_id=guild_id))

        text_channels = [c for c in guild.channels if getattr(c, "type", None).name in {"text", "news"}] if guild.channels else []
        categories = [c for c in guild.categories]
        channel_choices = sorted(text_channels + categories, key=lambda c: (c.position, c.name.lower()))
        roles = [r for r in guild.roles if not r.is_default() and not r.managed]
        automessages = store.list_automessages(guild_id)
        return render_template("guild.html", guild=guild, cfg=cfg, channels=channel_choices, roles=roles, automessages=automessages)

    @app.route("/dashboard/global", methods=["GET", "POST"])
    @owner_required
    def global_settings():
        cfg = store.get_global()
        if request.method == "POST":
            check_csrf()
            section = request.form.get("section", "settings")

            if section == "ping_now":
                if not keep_alive:
                    flash("Keep-alive worker is unavailable.", "error")
                else:
                    ok, detail = keep_alive.ping_once()
                    flash("Self-ping succeeded (" + detail + ")." if ok else "Self-ping failed (" + detail + ").", "success" if ok else "error")
                return redirect(url_for("global_settings"))

            interval = request.form.get("status_interval", "45")
            cfg["status_interval_seconds"] = max(15, min(3600, int(interval) if interval.isdigit() else 45))
            statuses = []
            for i in range(1, 9):
                text = request.form.get(f"status_text_{i}", "").strip()
                kind = request.form.get(f"status_type_{i}", "watching")
                if text:
                    statuses.append({"type": kind if kind in {"playing","watching","listening","competing","streaming"} else "watching", "text": text[:128]})
            cfg["statuses"] = statuses

            keep_interval = request.form.get("keep_alive_interval", "300")
            cfg["keep_alive"] = {
                "enabled": request.form.get("keep_alive_enabled") == "on",
                "interval_seconds": max(60, min(3600, int(keep_interval) if keep_interval.isdigit() else 300)),
            }
            store.set_global(cfg)
            if keep_alive:
                keep_alive.wake()
            flash("Global bot settings saved.", "success")
            return redirect(url_for("global_settings"))
        return render_template("global.html", cfg=cfg, keep_alive_state=keep_alive.snapshot() if keep_alive else None)

    return app
