from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from collections import defaultdict, deque
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlencode

import requests
from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, send_file, session, stream_with_context, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from bot.config import settings
from bot.store import store

DISCORD_API = "https://discord.com/api/v10"
STAT_TYPES = {"members","humans","bots","boosts","role","online","staff","verified","unverified","voice_users","open_tickets","total_tickets","channel_count","server_age_days","banned"}


def create_app(bot=None, keep_alive=None):
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
        resp.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://discord.com"
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
            token = secrets.token_urlsafe(32); session["csrf"] = token
        return token

    def check_csrf():
        expected = session.get("csrf", ""); actual = request.form.get("csrf", "") or request.headers.get("X-CSRF-Token", "")
        if not expected or not secrets.compare_digest(expected, actual):
            abort(400, "Invalid CSRF token")

    app.jinja_env.globals["csrf_token"] = csrf_token

    def rate_limit(bucket: str, limit: int = 20, window: int = 60):
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
        key = (bucket, ip); now = time.time(); q = rate_buckets[key]
        while q and now - q[0] > window: q.popleft()
        if len(q) >= limit: abort(429, "Too many requests. Try again shortly.")
        q.append(now)

    def begin_oauth(purpose: str, guild_id: int | None = None):
        state = secrets.token_urlsafe(32); flows = session.get("oauth_flows", {})
        flows[state] = {"purpose": purpose, "guild_id": guild_id, "created": int(time.time())}
        session["oauth_flows"] = {k: v for k, v in list(flows.items())[-6:]}
        params = {"client_id": settings.client_id, "redirect_uri": settings.oauth_redirect_uri, "response_type": "code", "scope": "identify", "state": state, "prompt": "consent"}
        return redirect("https://discord.com/oauth2/authorize?" + urlencode(params))

    def exchange_user(code: str):
        token_resp = requests.post(DISCORD_API + "/oauth2/token", data={"client_id": settings.client_id,"client_secret": settings.client_secret,"grant_type":"authorization_code","code":code,"redirect_uri":settings.oauth_redirect_uri}, headers={"Content-Type":"application/x-www-form-urlencoded"}, timeout=10)
        token_resp.raise_for_status(); access = token_resp.json()["access_token"]
        user_resp = requests.get(DISCORD_API + "/users/@me", headers={"Authorization": f"Bearer {access}"}, timeout=10); user_resp.raise_for_status(); return user_resp.json()

    def bot_live() -> bool:
        return bool(bot is not None and getattr(bot, "is_ready", lambda: False)())

    def live_guild(guild_id: int):
        return bot.get_guild(int(guild_id)) if bot_live() else None

    def snapshot(guild_id: int) -> dict | None:
        return store.get_snapshot(guild_id)

    def guild_exists(guild_id: int) -> bool:
        return bool(live_guild(guild_id) or snapshot(guild_id))

    def guild_data(guild_id: int) -> dict:
        guild = live_guild(guild_id)
        if guild:
            me = guild.me
            channels = [{"id":c.id,"name":c.name,"type":str(c.type),"position":getattr(c,"position",0),"mention":getattr(c,"mention",f"#{c.name}")} for c in guild.channels]
            roles = [{"id":r.id,"name":r.name,"position":r.position,"managed":r.managed,"member_count":len(r.members),"default":r.is_default()} for r in guild.roles]
            permissions = {name: bool(value) for name, value in (me.guild_permissions if me else [])}
            return {
                "id": guild.id, "name": guild.name, "member_count": int(guild.member_count or 0),
                "humans": sum(1 for m in guild.members if not m.bot), "bots": sum(1 for m in guild.members if m.bot),
                "boosts": int(guild.premium_subscription_count or 0), "online": sum(1 for m in guild.members if str(m.status) != "offline"),
                "voice_users": sum(1 for m in guild.members if m.voice and m.voice.channel),
                "icon_url": str(guild.icon.url) if guild.icon else "", "owner_id": guild.owner_id,
                "created_at": guild.created_at.isoformat(), "channels": channels, "roles": roles, "permissions": permissions,
                "bot_top_role_position": me.top_role.position if me else 0, "live": True,
            }
        snap = snapshot(guild_id)
        if not snap: abort(404)
        return {**snap, "id": int(snap.get("id") or guild_id), "live": False, "bot_top_role_position": int(snap.get("bot_top_role_position") or 0)}

    def all_guild_data() -> list[dict]:
        if bot_live():
            return [guild_data(g.id) for g in sorted(bot.guilds, key=lambda g: g.name.lower())]
        rows = []
        for snap in store.list_snapshots():
            try: rows.append(guild_data(int(snap.get("id") or snap.get("guild_id"))))
            except Exception: pass
        return sorted(rows, key=lambda g: g.get("name", "").lower())

    def safe_url(value: str, limit: int = 2048) -> str:
        value = (value or "").strip()[:limit]
        return value if value.startswith(("https://", "http://")) else ""

    def embed_from_form() -> dict | None:
        if request.form.get("action_use_embed") != "on": return None
        raw_color = request.form.get("embed_color", "#7c3aed").strip().lstrip("#")
        try:
            color = int(raw_color, 16) if len(raw_color) in {3,6} else 0x7C3AED
            if len(raw_color) == 3: color = int("".join(ch*2 for ch in raw_color),16)
        except ValueError: color = 0x7C3AED
        fields = []
        for i in range(10):
            name = request.form.get(f"embed_field_name_{i}", "").strip()[:256]; value = request.form.get(f"embed_field_value_{i}", "").strip()[:1024]
            if name and value: fields.append({"name":name,"value":value,"inline":request.form.get(f"embed_field_inline_{i}")=="on"})
        payload = {
            "title":request.form.get("embed_title","").strip()[:256], "title_url":safe_url(request.form.get("embed_title_url","")),
            "description":request.form.get("embed_description","").strip()[:4096], "color":color,
            "author_name":request.form.get("embed_author_name","").strip()[:256], "author_url":safe_url(request.form.get("embed_author_url","")),
            "author_icon_url":safe_url(request.form.get("embed_author_icon_url","")), "thumbnail_url":safe_url(request.form.get("embed_thumbnail_url","")),
            "image_url":safe_url(request.form.get("embed_image_url","")), "footer_text":request.form.get("embed_footer_text","").strip()[:2048],
            "footer_icon_url":safe_url(request.form.get("embed_footer_icon_url","")), "timestamp":request.form.get("embed_timestamp")=="on", "fields":fields,
        }
        meaningful = any(payload.get(k) for k in ("title","description","author_name","thumbnail_url","image_url","footer_text")) or bool(fields)
        return payload if meaningful else None

    def buttons_from_form() -> list[dict]:
        out = []
        for i in range(5):
            if request.form.get(f"button_enabled_{i}") != "on": continue
            kind = request.form.get(f"button_kind_{i}", "link")
            label = request.form.get(f"button_label_{i}", "").strip()[:80]
            if not label: continue
            item = {"kind":kind if kind in {"link","role"} else "link","label":label,"emoji":request.form.get(f"button_emoji_{i}","").strip()[:50],"style":request.form.get(f"button_style_{i}","secondary")}
            if item["kind"] == "link":
                item["url"] = safe_url(request.form.get(f"button_url_{i}", ""), 512)
                if not item["url"]: continue
            else:
                rid = request.form.get(f"button_role_id_{i}", "").strip()
                if not rid.isdigit(): continue
                item["role_id"] = int(rid); item["mode"] = request.form.get(f"button_mode_{i}","toggle") if request.form.get(f"button_mode_{i}") in {"add","remove","toggle"} else "toggle"
            out.append(item)
        return out

    def message_payload_from_form() -> dict:
        return {
            "content":request.form.get("action_content","").strip()[:2000], "embed":embed_from_form(),
            "buttons":buttons_from_form(), "allow_mentions":request.form.get("action_allow_mentions")=="on",
            "publish":request.form.get("action_publish")=="on",
        }

    def apply_server_stats_form(cfg: dict) -> None:
        stats = cfg.setdefault("server_stats", {}); old_items = list(stats.get("items") or [])
        stats["category_name"] = (request.form.get("stats_category_name","│ SERVER STATS │").strip() or "│ SERVER STATS │")[:100]
        raw = request.form.get("stats_interval","300"); stats["update_interval_seconds"] = max(60,min(3600,int(raw) if raw.isdigit() else 300))
        items=[]
        for i in range(12):
            old = old_items[i] if i < len(old_items) else {}
            kind = request.form.get(f"stat_type_{i}", old.get("type","members")); kind = kind if kind in STAT_TYPES else "members"
            role = request.form.get(f"stat_role_{i}","").strip()
            items.append({"enabled":request.form.get(f"stat_enabled_{i}")=="on","type":kind,"label":(request.form.get(f"stat_label_{i}",old.get("label",f"Stat {i+1}")).strip() or f"Stat {i+1}")[:50],"emoji":request.form.get(f"stat_emoji_{i}",old.get("emoji","")).strip()[:24],"template":(request.form.get(f"stat_template_{i}",old.get("template","{emoji} {label}: {value}")).strip() or "{emoji} {label}: {value}")[:100],"role_id":int(role) if role.isdigit() else None,"channel_id":old.get("channel_id")})
        stats["items"] = items; cfg["server_stats"] = stats

    def apply_ticket_types_form(cfg: dict) -> None:
        current = {str(t.get("key")):t for t in cfg.get("tickets",{}).get("types",[])}; types=[]
        for i in range(5):
            key = (request.form.get(f"ticket_type_key_{i}","").strip().lower().replace(" ","-") or f"type-{i+1}")[:32]
            name = request.form.get(f"ticket_type_name_{i}","").strip()[:80]
            if not name: continue
            old=current.get(key,{})
            cat=request.form.get(f"ticket_type_category_{i}","").strip(); role=request.form.get(f"ticket_type_role_{i}","").strip(); questions=[]
            for q in range(5):
                label=request.form.get(f"ticket_q_label_{i}_{q}","").strip()[:45]
                if not label: continue
                questions.append({"label":label,"placeholder":request.form.get(f"ticket_q_placeholder_{i}_{q}","").strip()[:100],"required":request.form.get(f"ticket_q_required_{i}_{q}")=="on","style":"paragraph" if request.form.get(f"ticket_q_style_{i}_{q}")=="paragraph" else "short"})
            types.append({"key":key,"enabled":request.form.get(f"ticket_type_enabled_{i}")=="on","name":name,"emoji":request.form.get(f"ticket_type_emoji_{i}","").strip()[:24],"description":request.form.get(f"ticket_type_description_{i}","").strip()[:100],"category_id":int(cat) if cat.isdigit() else None,"support_role_id":int(role) if role.isdigit() else None,"priority":request.form.get(f"ticket_type_priority_{i}","normal") if request.form.get(f"ticket_type_priority_{i}") in {"low","normal","high","urgent"} else "normal","questions":questions,"_old":old.get("_old")})
        cfg["tickets"]["types"] = types or cfg["tickets"].get("types",[])

    def apply_automod_form(cfg: dict) -> None:
        acfg=cfg.setdefault("automod",{})
        bools=["spam_enabled","duplicate_enabled","mention_enabled","caps_enabled","invite_block","link_block","delete_trigger","new_account_enabled","join_rate_enabled","auto_raid_mode","raid_kick_new_accounts"]
        for key in bools: acfg[key]=request.form.get(f"automod_{key}")=="on"
        ints={"spam_messages":(3,20,6),"spam_window_seconds":(2,60,8),"duplicate_count":(2,10,4),"mention_limit":(2,30,6),"caps_percent":(10,100,80),"caps_min_length":(5,100,16),"timeout_minutes":(1,40320,10),"min_account_age_hours":(0,8760,24),"join_rate_count":(3,100,12),"join_rate_window_seconds":(5,300,30)}
        for key,(lo,hi,default) in ints.items():
            raw=request.form.get(f"automod_{key}",str(default)); acfg[key]=max(lo,min(hi,int(raw) if raw.lstrip("-").isdigit() else default))
        acfg["action"]=request.form.get("automod_action","timeout") if request.form.get("automod_action") in {"warn","timeout","kick","ban","delete"} else "timeout"
        for key in ("domain_blacklist","domain_whitelist","bad_words"):
            acfg[key]=[x.strip()[:120] for x in request.form.get(f"automod_{key}","").replace(",","\n").splitlines() if x.strip()][:200]
        acfg["ignore_roles"]=[int(x) for x in request.form.getlist("automod_ignore_roles") if x.isdigit()]
        acfg["ignore_channels"]=[int(x) for x in request.form.getlist("automod_ignore_channels") if x.isdigit()]
        acfg["raid_lock_channels"]=[int(x) for x in request.form.getlist("automod_raid_lock_channels") if x.isdigit()]
        cfg["automod"]=acfg

    def deep_changes(old: dict, new: dict, prefix: str = "") -> list[dict]:
        out=[]
        keys=set(old)|set(new)
        for k in sorted(keys):
            path=f"{prefix}.{k}" if prefix else str(k); a=old.get(k); b=new.get(k)
            if isinstance(a,dict) and isinstance(b,dict): out.extend(deep_changes(a,b,path))
            elif a != b: out.append({"field":path,"old":a,"new":b})
            if len(out)>=40: break
        return out[:40]

    def config_health(g: dict, cfg: dict) -> list[dict]:
        channels={int(c["id"]):c for c in g.get("channels",[])}; roles={int(r["id"]):r for r in g.get("roles",[])}; perms=g.get("permissions",{}); checks=[]
        def add(name,ok,detail,severity="ok"): checks.append({"name":name,"ok":bool(ok),"detail":detail,"severity":"ok" if ok else severity})
        add("Bot connection", bool(g.get("live") or g.get("updated_at")), "Live gateway connected." if g.get("live") else "Using the latest bot snapshot.", "warning")
        if cfg["features"].get("tickets"):
            add("Ticket channel permission", bool(perms.get("manage_channels")), "Manage Channels is available." if perms.get("manage_channels") else "Grant Manage Channels for ticket/category creation.", "error")
        if cfg["features"].get("server_stats"):
            add("Server Stats permission", bool(perms.get("manage_channels")), "Manage Channels is available." if perms.get("manage_channels") else "Grant Manage Channels for Server Stats.", "error")
        role_features = cfg["features"].get("autorole") or cfg["features"].get("verification")
        if role_features: add("Role management permission", bool(perms.get("manage_roles")), "Manage Roles is available." if perms.get("manage_roles") else "Grant Manage Roles.", "error")
        for key,label in [("welcome","Welcome channel"),("logs","Log channel"),("mod_logs","Mod log channel"),("ticket_logs","Ticket log channel")]:
            raw=cfg["channels"].get(key)
            if raw: add(label, int(raw) in channels, "Configured channel exists." if int(raw) in channels else "Configured channel was deleted.", "warning")
        for key,label in [("autorole","Auto-role"),("verification_add","Verified role"),("verification_remove","Unverified role"),("ticket_support","Ticket support role")]:
            raw=cfg["roles"].get(key)
            if raw:
                exists=int(raw) in roles; detail="Configured role exists." if exists else "Configured role was deleted."
                if exists and g.get("live") and int(roles[int(raw)].get("position",0)) >= int(g.get("bot_top_role_position",0)): exists=False; detail="Move the bot role above this role."
                add(label,exists,detail,"error" if key in {"verification_add","autorole"} else "warning")
        add("OAuth callback", settings.oauth_redirect_uri.startswith("https://") or settings.oauth_redirect_uri.startswith("http://127.0.0.1"), "OAuth redirect URI is configured." if settings.oauth_redirect_uri else "Configure OAUTH_REDIRECT_URI.", "error")
        return checks

    def permission_matrix(g: dict) -> list[dict]:
        perms=g.get("permissions",{})
        required=[("manage_roles","Manage Roles","Auto-role, verification, role buttons"),("manage_channels","Manage Channels","Tickets, Server Stats, raid locks"),("manage_messages","Manage Messages","Purge, AutoMod deletion"),("moderate_members","Moderate Members","Timeouts and AutoMod timeouts"),("kick_members","Kick Members","Moderation and anti-raid"),("ban_members","Ban Members","Moderation and AutoMod bans"),("view_audit_log","View Audit Log","Advanced moderation context"),("manage_webhooks","Manage Webhooks","Announcement-channel workflows")]
        return [{"key":k,"name":name,"ok":bool(perms.get(k)),"used_by":used} for k,name,used in required]

    def stat_previews(g: dict, cfg: dict) -> list[dict]:
        analytics=store.ticket_analytics(int(g["id"])); roles={int(r["id"]):r for r in g.get("roles",[])}; out=[]
        values={"members":g.get("member_count",0),"humans":g.get("humans",0),"bots":g.get("bots",0),"boosts":g.get("boosts",0),"online":g.get("online",0),"voice_users":g.get("voice_users",0),"open_tickets":analytics["open"],"total_tickets":analytics["total"],"channel_count":len(g.get("channels",[]))}
        try: values["server_age_days"]=(datetime.now(timezone.utc)-datetime.fromisoformat(str(g.get("created_at")).replace("Z","+00:00"))).days
        except Exception: values["server_age_days"]=0
        for item in list(cfg.get("server_stats",{}).get("items") or [])[:12]:
            kind=item.get("type","members"); value=values.get(kind,0)
            if kind in {"role","staff","verified","unverified"}:
                rid=item.get("role_id")
                if not rid:
                    rid=cfg["roles"].get({"staff":"staff","verified":"verification_add","unverified":"verification_remove"}.get(kind,""))
                value=roles.get(int(rid),{}).get("member_count",0) if str(rid or "").isdigit() else 0
            template=str(item.get("template") or "{emoji} {label}: {value}")
            try:name=template.format(emoji=item.get("emoji",""),label=item.get("label","Stat"),value=value)
            except Exception:name=f"{item.get('emoji','')} {item.get('label','Stat')}: {value}"
            out.append({"name":" ".join(name.split())[:100],"value":value})
        return out

    def direct_or_queue(guild_id: int, action: str, payload: dict, direct_coro=None, timeout: int = 20) -> tuple[bool,str]:
        if bot_live() and direct_coro is not None:
            try:
                fut=asyncio.run_coroutine_threadsafe(direct_coro,bot.loop); result=fut.result(timeout=timeout)
                if isinstance(result,tuple): return bool(result[0]),str(result[1])
                return bool(result), "Completed" if result else "Discord rejected the action."
            except Exception as exc: return False, f"Action failed: {type(exc).__name__}"
        aid=store.enqueue_action(guild_id,action,payload); return True,f"Queued as {aid[:8]} for the bot worker."

    logo_cache={"body":None,"content_type":"image/png","fetched_at":0.0}

    @app.get("/brand/logo.png")
    def brand_logo():
        local_logo=os.path.join(app.static_folder or "","logo.png")
        if os.path.isfile(local_logo): return send_file(local_logo,mimetype="image/png",max_age=86400)
        now=time.time()
        if logo_cache["body"] is None or now-logo_cache["fetched_at"]>86400:
            try:
                upstream=requests.get("https://www.moealturej.com/static/logo.png",headers={"User-Agent":"moealturej-bot-dashboard/2.0"},timeout=8);upstream.raise_for_status()
                ctype=upstream.headers.get("Content-Type","image/png").split(";",1)[0].strip()
                if not ctype.startswith("image/"): raise requests.RequestException("not an image")
                logo_cache.update(body=upstream.content,content_type=ctype,fetched_at=now)
            except requests.RequestException:
                if logo_cache["body"] is None: abort(502,"Brand logo is temporarily unavailable.")
        response=Response(logo_cache["body"],mimetype=logo_cache["content_type"]);response.headers["Cache-Control"]="public,max-age=3600,stale-while-revalidate=86400";return response

    @app.get("/health")
    def health():
        db_ok, backend = store.health()
        return {"ok":bool(db_ok),"database":backend,"bot_ready":bot_live(),"guilds":len(all_guild_data()) if db_ok else 0,"process_mode":settings.process_mode,"keep_alive_worker":bool(keep_alive and keep_alive.snapshot().get("running"))}, (200 if db_ok else 503)

    @app.get("/ready")
    def ready():
        db_ok, backend = store.health()
        if not db_ok: return {"ok":False,"reason":"Persistence backend unavailable","database":backend},503
        if settings.process_mode == "combined" and not bot_live(): return {"ok":False,"reason":"Discord gateway not ready","database":backend},503
        return {"ok":True,"database":backend,"bot_ready":bot_live()}

    @app.get("/")
    def index():
        return redirect(url_for("dashboard")) if session.get("owner_id") == settings.owner_id else render_template("login.html")

    @app.get("/login")
    def login():
        rate_limit("login",12,60)
        if not settings.client_id or not settings.client_secret or not settings.owner_id: return render_template("error.html",message="Authentication is not configured correctly."),503
        return begin_oauth("owner")

    @app.get("/logout")
    def logout(): session.clear(); return redirect(url_for("index"))

    @app.get("/oauth/callback")
    @app.get("/verify/callback")
    def oauth_callback():
        rate_limit("oauth-callback",30,60); code=request.args.get("code",""); state=request.args.get("state",""); flows=session.get("oauth_flows",{}); flow=flows.pop(state,None) if state else None; session["oauth_flows"]=flows
        if not flow or int(time.time())-int(flow.get("created",0))>600: return render_template("error.html",message="OAuth session expired or invalid. Start again."),400
        if not code: return render_template("error.html",message="Discord OAuth was cancelled or did not return a code."),400
        try:user=exchange_user(code)
        except requests.RequestException:return render_template("error.html",message="Discord OAuth could not be completed."),502
        purpose=flow.get("purpose")
        if purpose=="owner":
            if int(user["id"])!=settings.owner_id: session.clear(); return render_template("error.html",message="This Discord account is not authorized."),403
            session.clear();session.permanent=True;session["owner_id"]=int(user["id"]);session["owner_name"]=user.get("global_name") or user.get("username");csrf_token();return redirect(url_for("dashboard"))
        if purpose=="verify":
            gid=int(flow.get("guild_id") or 0)
            if not gid:return render_template("error.html",message="Verification target is missing."),400
            if bot_live():
                try:fut=asyncio.run_coroutine_threadsafe(bot.apply_verification(gid,int(user["id"])),bot.loop);ok,message=fut.result(timeout=12)
                except Exception:ok,message=False,"The role update could not be completed."
            else:
                store.enqueue_action(gid,"apply_verification",{"user_id":int(user["id"])});ok,message=True,"Verification was accepted and the bot is applying your server roles."
            return render_template("verify_result.html",ok=ok,message=message)
        return render_template("error.html",message="Unknown OAuth flow."),400

    @app.get("/verify/<int:guild_id>")
    def verify(guild_id:int):
        if not guild_exists(guild_id): abort(404)
        cfg=store.get_guild(guild_id)
        if not cfg["features"].get("verification"): return render_template("error.html",message="Verification is disabled for this server."),403
        g=guild_data(guild_id);return render_template("verify.html",guild=g,cfg=cfg)

    @app.post("/verify/<int:guild_id>/start")
    def verify_start(guild_id:int): rate_limit("verify-start",20,60); (abort(404) if not guild_exists(guild_id) else None); return begin_oauth("verify",guild_id)

    @app.get("/dashboard")
    @owner_required
    def dashboard():
        guilds=all_guild_data();total_members=sum(int(g.get("member_count",0)) for g in guilds);return render_template("dashboard.html",guilds=guilds,total_members=total_members,bot_online=bot_live())

    @app.route("/dashboard/guild/<int:guild_id>",methods=["GET","POST"])
    @owner_required
    def guild_settings(guild_id:int):
        if not guild_exists(guild_id): abort(404)
        g=guild_data(guild_id); cfg=store.get_guild(guild_id)
        if request.method=="POST":
            check_csrf();section=request.form.get("section","settings"); actor=session.get("owner_id")
            if section=="settings":
                old=deepcopy(cfg)
                for key in cfg["features"]: cfg["features"][key]=request.form.get(f"feature_{key}")=="on"
                for key in cfg["channels"]:
                    raw=request.form.get(f"channel_{key}","").strip();cfg["channels"][key]=int(raw) if raw.isdigit() else None
                for key in cfg["roles"]:
                    raw=request.form.get(f"role_{key}","").strip();cfg["roles"][key]=int(raw) if raw.isdigit() else None
                cfg["welcome"]["content"]=request.form.get("welcome_content",cfg["welcome"]["content"])[:1900];cfg["welcome"]["dm_enabled"]=request.form.get("welcome_dm_enabled")=="on";cfg["welcome"]["dm_content"]=request.form.get("welcome_dm_content",cfg["welcome"]["dm_content"])[:1900]
                for key in ["panel_title","panel_description","button_label","opening_message"]:cfg["tickets"][key]=request.form.get(f"ticket_{key}",cfg["tickets"][key])[:1900]
                cfg["tickets"]["max_open_per_user"]=max(1,min(10,int(request.form.get("ticket_max_open","1")) if request.form.get("ticket_max_open","").isdigit() else 1));cfg["tickets"]["auto_close_hours"]=max(0,min(720,int(request.form.get("ticket_auto_close","0")) if request.form.get("ticket_auto_close","").isdigit() else 0))
                apply_ticket_types_form(cfg)
                for key in ["panel_title","panel_description","button_label","success_message"]:cfg["verification"][key]=request.form.get(f"verification_{key}",cfg["verification"][key])[:1900]
                cfg["moderation"]["dm_on_action"]=request.form.get("moderation_dm_on_action")=="on";cfg["moderation"]["reason_required"]=request.form.get("moderation_reason_required")=="on"
                rules=[]
                for i in range(4):
                    count=request.form.get(f"warning_count_{i}","");action=request.form.get(f"warning_action_{i}","none");duration=request.form.get(f"warning_duration_{i}","10")
                    if count.isdigit() and int(count)>0 and action in {"timeout","kick","ban"}:rules.append({"count":int(count),"action":action,"duration_minutes":int(duration) if duration.isdigit() else 10})
                cfg["moderation"]["warning_actions"]=rules
                apply_automod_form(cfg);apply_server_stats_form(cfg)
                ap=cfg.setdefault("appearance",{})
                for key,default in [("accent_color","#7c3aed"),("success_color","#22c55e"),("warning_color","#f59e0b"),("error_color","#ef4444"),("ticket_color","#7c3aed"),("moderation_color","#ef4444"),("welcome_color","#7c3aed")]:
                    raw=request.form.get(f"appearance_{key}",default).strip();ap[key]=raw if len(raw)==7 and raw.startswith("#") else default
                ap["footer_text"]=request.form.get("appearance_footer_text","").strip()[:2048];ap["footer_icon_url"]=safe_url(request.form.get("appearance_footer_icon_url",""));cfg["appearance"]=ap
                saved=store.set_guild(guild_id,cfg);changes=deep_changes(old,saved);store.audit(guild_id,actor,"config.updated",changes=changes,ip=request.remote_addr)
                flash(f"Configuration saved · {len(changes)} change(s).","success")
            elif section in {"server_stats_sync","server_stats_remove"}:
                if section=="server_stats_sync":ok,detail=direct_or_queue(guild_id,"stats_sync",{},bot.sync_server_stats(guild_id) if bot_live() else None)
                else:ok,detail=direct_or_queue(guild_id,"stats_remove",{},bot.remove_server_stats(guild_id) if bot_live() else None)
                store.audit(guild_id,actor,section,result=detail);flash(detail,"success" if ok else "error")
            elif section=="raid_mode":
                enabled=request.form.get("raid_enabled")=="true";payload={"enabled":enabled,"actor_id":actor};ok,detail=direct_or_queue(guild_id,"raid_mode",payload,bot.set_raid_mode(guild_id,enabled,actor_id=actor) if bot_live() else None);store.audit(guild_id,actor,"raid_mode.requested",enabled=enabled,result=detail);flash(detail,"success" if ok else "error")
            elif section in {"post_ticket_panel","post_verification_panel"}:
                cid=request.form.get("panel_channel","")
                if not cid.isdigit():flash("Choose a text channel.","error")
                else:
                    action="ticket_panel" if section=="post_ticket_panel" else "verification_panel";coro=bot.post_dashboard_ticket_panel(guild_id,int(cid)) if (bot_live() and action=="ticket_panel") else (bot.post_dashboard_verification_panel(guild_id,int(cid)) if bot_live() else None)
                    ok,detail=direct_or_queue(guild_id,action,{"channel_id":int(cid)},coro);store.audit(guild_id,actor,action+".requested",channel_id=int(cid),result=detail);flash("Panel posted." if ok and bot_live() else detail,"success" if ok else "error")
            elif section=="send_message":
                payload=message_payload_from_form();cid=request.form.get("action_channel","")
                if not cid.isdigit():flash("Choose a valid text channel.","error")
                elif not payload["content"] and not payload["embed"]:flash("Add message content or an embed.","error")
                else:
                    coro=bot.send_dashboard_message(int(cid),payload["content"],embed_data=payload["embed"],allow_mentions=payload["allow_mentions"],publish=payload["publish"],buttons=payload["buttons"]) if bot_live() else None
                    ok,detail=direct_or_queue(guild_id,"send_message",{"channel_id":int(cid),**payload},coro);store.audit(guild_id,actor,"message.sent",channel_id=int(cid),has_embed=bool(payload["embed"]),buttons=len(payload["buttons"]),result=detail);flash("Message sent." if ok and bot_live() else detail,"success" if ok else "error")
            elif section=="schedule_message":
                payload=message_payload_from_form();cid=request.form.get("action_channel","");raw_at=request.form.get("schedule_at_iso","").strip();repeat=request.form.get("schedule_repeat_minutes","0")
                try:run_at=datetime.fromisoformat(raw_at.replace("Z","+00:00"));run_at=run_at if run_at.tzinfo else run_at.replace(tzinfo=timezone.utc)
                except ValueError:run_at=None
                if not cid.isdigit() or not run_at:flash("Choose a channel and a valid future schedule time.","error")
                elif not payload["content"] and not payload["embed"]:flash("Add message content or an embed.","error")
                else:
                    sid=store.add_scheduled_message(guild_id,int(cid),run_at.astimezone(timezone.utc).isoformat(),payload,max(0,min(10080,int(repeat) if repeat.isdigit() else 0)));store.audit(guild_id,actor,"message.scheduled",schedule_id=sid,channel_id=int(cid),next_run=run_at.isoformat());flash(f"Scheduled message #{sid}.","success")
            elif section=="save_template":
                payload=message_payload_from_form();name=request.form.get("template_name","").strip()[:80]
                if not name:flash("Give the template a name.","error")
                else:tid=store.save_template(guild_id,name,payload);store.audit(guild_id,actor,"template.saved",template_id=tid,name=name);flash(f"Saved template #{tid}.","success")
            elif section=="delete_template":
                tid=request.form.get("template_id","");
                if tid.isdigit():store.delete_template(guild_id,int(tid));store.audit(guild_id,actor,"template.deleted",template_id=int(tid));flash("Template deleted.","success")
            elif section=="delete_schedule":
                sid=request.form.get("schedule_id","");
                if sid.isdigit():store.delete_scheduled_message(guild_id,int(sid));store.audit(guild_id,actor,"message.schedule_deleted",schedule_id=int(sid));flash("Scheduled message deleted.","success")
            elif section=="send_dm":
                uid=request.form.get("dm_user_id","").strip();content=request.form.get("dm_content","").strip()[:1900]
                if uid.isdigit() and content:
                    coro=bot.send_dashboard_dm(int(uid),content) if bot_live() else None;ok,detail=direct_or_queue(guild_id,"send_dm",{"user_id":int(uid),"content":content},coro);store.audit(guild_id,actor,"dm.sent",user_id=int(uid),result=detail);flash("DM sent." if ok and bot_live() else detail,"success" if ok else "error")
                else:flash("Provide a user ID and message.","error")
            elif section=="automessage_add":
                cid=request.form.get("am_channel","");interval=request.form.get("am_interval","");content=request.form.get("am_content","").strip()
                if cid.isdigit() and interval.isdigit() and content:
                    mid=store.add_automessage(guild_id,int(cid),content[:1900],max(5,min(10080,int(interval))));store.audit(guild_id,actor,"automessage.created",message_id=mid);flash("Automatic message added.","success")
            elif section=="automessage_update":
                mid=request.form.get("message_id","");cid=request.form.get("am_channel","");interval=request.form.get("am_interval","");content=request.form.get("am_content","").strip()
                if mid.isdigit() and cid.isdigit() and interval.isdigit() and content:store.update_automessage(guild_id,int(mid),channel_id=int(cid),content=content[:1900],interval_minutes=max(5,min(10080,int(interval))),enabled=request.form.get("am_enabled")=="on");store.audit(guild_id,actor,"automessage.updated",message_id=int(mid));flash("Automatic message updated.","success")
            elif section=="automessage_delete":
                mid=request.form.get("message_id","");
                if mid.isdigit():store.delete_automessage(guild_id,int(mid));store.audit(guild_id,actor,"automessage.deleted",message_id=int(mid));flash("Automatic message removed.","success")
            elif section=="case_reason":
                cid=request.form.get("case_id","");reason=request.form.get("case_reason","").strip()[:1000]
                if cid.isdigit() and reason and store.update_case(guild_id,int(cid),reason=reason):store.audit(guild_id,actor,"case.reason_updated",case_id=int(cid));flash("Case reason updated.","success")
                else:flash("Case could not be updated.","error")
            elif section=="case_delete":
                cid=request.form.get("case_id","");ok=cid.isdigit() and store.delete_case(guild_id,int(cid));
                if ok:store.audit(guild_id,actor,"case.deleted",case_id=int(cid))
                flash("Case deleted." if ok else "Case not found.","success" if ok else "error")
            elif section=="moderate_user":
                uid=request.form.get("moderation_user_id","").strip();action=request.form.get("moderation_action","warn");reason=request.form.get("moderation_reason","").strip()[:1000] or "No reason provided";raw_duration=request.form.get("moderation_duration","10");duration=max(1,min(40320,int(raw_duration) if raw_duration.isdigit() else 10))
                if not uid.isdigit() or action not in {"warn","clear_warnings","timeout","untimeout","kick","ban","unban"}:flash("Choose a valid moderation action and numeric Discord user ID.","error")
                else:
                    payload={"user_id":int(uid),"moderation_action":action,"reason":reason,"duration_minutes":duration};coro=bot.dashboard_moderate(guild_id,int(uid),action,reason,duration) if bot_live() else None;ok,detail=direct_or_queue(guild_id,"moderate_user",payload,coro);store.audit(guild_id,actor,"moderation.dashboard_action",user_id=int(uid),moderation_action=action,result=detail);flash(detail,"success" if ok else "error")
            elif section=="moderate_channel":
                cid=request.form.get("moderation_channel_id","").strip();action=request.form.get("channel_action","slowmode");raw=request.form.get("channel_action_value","0");value=int(raw) if raw.lstrip("-").isdigit() else 0
                if not cid.isdigit() or action not in {"purge","lock","unlock","slowmode"}:flash("Choose a valid channel moderation action.","error")
                else:
                    payload={"channel_id":int(cid),"channel_action":action,"value":value};coro=bot.dashboard_channel_action(guild_id,int(cid),action,value) if bot_live() else None;ok,detail=direct_or_queue(guild_id,"moderate_channel",payload,coro);store.audit(guild_id,actor,"moderation.dashboard_channel",channel_id=int(cid),channel_action=action,result=detail);flash(detail,"success" if ok else "error")
            elif section=="ticket_close":
                cid=request.form.get("ticket_channel_id","").strip()
                if not cid.isdigit():flash("Ticket channel ID is invalid.","error")
                else:
                    coro=bot.dashboard_close_ticket(guild_id,int(cid)) if bot_live() else None;ok,detail=direct_or_queue(guild_id,"ticket_close",{"channel_id":int(cid)},coro);store.audit(guild_id,actor,"ticket.dashboard_close",channel_id=int(cid),result=detail);flash(detail,"success" if ok else "error")
            elif section in {"commands_sync_guild","commands_clear_guild"}:
                clear=section=="commands_clear_guild";payload={"clear_guild_overrides":clear};coro=bot.dashboard_sync_commands(guild_id,clear) if bot_live() else None;ok,detail=direct_or_queue(guild_id,"commands_sync",payload,coro);store.audit(guild_id,actor,"commands.guild_sync",clear_overrides=clear,result=detail);flash(detail,"success" if ok else "error")
            elif section=="repair_config":
                old=deepcopy(cfg);channel_ids={int(c["id"]) for c in g.get("channels",[])};role_ids={int(r["id"]) for r in g.get("roles",[])}
                for key,value in list(cfg.get("channels",{}).items()):
                    if value and int(value) not in channel_ids:cfg["channels"][key]=None
                for key,value in list(cfg.get("roles",{}).items()):
                    if value and int(value) not in role_ids:cfg["roles"][key]=None
                stats=cfg.get("server_stats",{});cat=stats.get("category_id")
                if cat and int(cat) not in channel_ids:stats["category_id"]=None
                for item in stats.get("items",[]):
                    if item.get("channel_id") and int(item["channel_id"]) not in channel_ids:item["channel_id"]=None
                    if item.get("role_id") and int(item["role_id"]) not in role_ids:item["role_id"]=None
                cfg["server_stats"]=stats;store.set_guild(guild_id,cfg);changes=deep_changes(old,cfg);store.audit(guild_id,actor,"config.safe_repair",changes=changes);flash(f"Safe repair completed · {len(changes)} stale reference(s) corrected.","success")
            elif section=="backup_now":
                bid=store.backup_guild(guild_id);store.prune_backups(guild_id,int(store.get_global().get("backup",{}).get("keep",14)));store.audit(guild_id,actor,"backup.created",backup_id=bid);flash(f"Configuration backup #{bid} created.","success")
            elif section=="backup_restore":
                bid=request.form.get("backup_id","");ok=bid.isdigit() and store.restore_backup(guild_id,int(bid));
                if ok:store.audit(guild_id,actor,"backup.restored",backup_id=int(bid))
                flash("Backup restored. Review and sync affected live systems." if ok else "Backup not found.","success" if ok else "error")
            return redirect(url_for("guild_settings",guild_id=guild_id,tab=request.form.get("return_tab","overview")))

        channels=sorted(g.get("channels",[]),key=lambda c:(int(c.get("position",0)),c.get("name","").lower()));text_channels=[c for c in channels if c.get("type") in {"text","news"}];categories=[c for c in channels if c.get("type")=="category"]
        roles=sorted([r for r in g.get("roles",[]) if not r.get("default") and not r.get("managed")],key=lambda r:int(r.get("position",0)),reverse=True)
        cases=store.list_cases(guild_id,limit=50);ticket_analytics=store.ticket_analytics(guild_id);recent_tickets=store.list_tickets(guild_id,limit=30);automessages=store.list_automessages(guild_id);templates=store.list_templates(guild_id);schedules=store.list_scheduled_messages(guild_id);audit_rows=store.list_audit(guild_id,80);health_checks=config_health(g,cfg);permission_rows=permission_matrix(g);backups=store.list_backups(guild_id,20)
        ok_count=sum(1 for x in health_checks if x["ok"]);health_score=round(ok_count/max(1,len(health_checks))*100)
        return render_template("guild.html",guild=g,cfg=cfg,channels=channels,text_channels=text_channels,categories=categories,roles=roles,cases=cases,ticket_analytics=ticket_analytics,recent_tickets=recent_tickets,automessages=automessages,templates=templates,schedules=schedules,audit_rows=audit_rows,health_checks=health_checks,health_score=health_score,permission_rows=permission_rows,stat_previews=stat_previews(g,cfg),backups=backups,active_tab=request.args.get("tab","overview"))

    @app.get("/dashboard/guild/<int:guild_id>/export")
    @owner_required
    def export_guild_config(guild_id:int):
        if not guild_exists(guild_id):abort(404)
        payload={"schema_version":2,"exported_at":datetime.now(timezone.utc).isoformat(),"guild":guild_data(guild_id),"config":store.get_guild(guild_id),"templates":store.list_templates(guild_id),"scheduled_messages":store.list_scheduled_messages(guild_id),"automatic_messages":store.list_automessages(guild_id)}
        body=json.dumps(payload,ensure_ascii=False,indent=2,default=str)
        store.audit(guild_id,session.get("owner_id"),"config.exported")
        return Response(body,mimetype="application/json",headers={"Content-Disposition":f'attachment; filename="guild-{guild_id}-config.json"'})

    @app.get("/api/guild/<int:guild_id>/template/<int:template_id>")
    @owner_required
    def template_api(guild_id:int,template_id:int):
        if not guild_exists(guild_id):abort(404)
        row=store.get_template(guild_id,template_id)
        if not row:abort(404)
        return jsonify(row)

    @app.get("/api/guild/<int:guild_id>/live")
    @owner_required
    def live_api(guild_id:int):
        if not guild_exists(guild_id):abort(404)
        @stream_with_context
        def generate():
            for _ in range(18):
                try:
                    g=guild_data(guild_id);analytics=store.ticket_analytics(guild_id);cases=store.list_cases(guild_id,limit=200);today=datetime.now(timezone.utc).date().isoformat();cases_today=sum(1 for c in cases if str(c.get("created_at","")).startswith(today));audit=store.list_audit(guild_id,1);payload={"bot_online":bot_live(),"latency_ms":round(bot.latency*1000) if bot_live() else None,"member_count":g.get("member_count",0),"online":g.get("online",0),"open_tickets":analytics["open"],"cases_today":cases_today,"last_event":audit[0].get("action") if audit else "No recent dashboard activity","ts":datetime.now(timezone.utc).isoformat()};yield "data: "+json.dumps(payload,separators=(",",":"))+"\n\n"
                except Exception:yield "event: error\ndata: {}\n\n"
                time.sleep(5)
        return Response(generate(),mimetype="text/event-stream",headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

    @app.route("/dashboard/global",methods=["GET","POST"])
    @owner_required
    def global_settings():
        cfg=store.get_global()
        if request.method=="POST":
            check_csrf();section=request.form.get("section","settings")
            if section=="ping_now":
                if not keep_alive:flash("Keep-alive worker is unavailable.","error")
                else:
                    ok,detail=keep_alive.ping_once();flash("Self-ping succeeded ("+detail+")." if ok else "Self-ping failed ("+detail+").","success" if ok else "error")
                return redirect(url_for("global_settings"))
            if section=="sync_commands_global":
                coro=bot.dashboard_sync_commands(None,False) if bot_live() else None;ok,detail=direct_or_queue(0,"commands_sync",{"clear_guild_overrides":False},coro);flash(detail,"success" if ok else "error");return redirect(url_for("global_settings"))
            interval=request.form.get("status_interval","45");cfg["status_interval_seconds"]=max(15,min(3600,int(interval) if interval.isdigit() else 45));statuses=[]
            for i in range(1,9):
                text=request.form.get(f"status_text_{i}","").strip();kind=request.form.get(f"status_type_{i}","watching")
                if text:statuses.append({"type":kind if kind in {"playing","watching","listening","competing","streaming"} else "watching","text":text[:128]})
            cfg["statuses"]=statuses;keep_interval=request.form.get("keep_alive_interval","300");cfg["keep_alive"]={"enabled":request.form.get("keep_alive_enabled")=="on","interval_seconds":max(60,min(3600,int(keep_interval) if keep_interval.isdigit() else 300))}
            backup_interval=request.form.get("backup_interval_hours","24");backup_keep=request.form.get("backup_keep","14");cfg["backup"]={"enabled":request.form.get("backup_enabled")=="on","interval_hours":max(1,min(168,int(backup_interval) if backup_interval.isdigit() else 24)),"keep":max(1,min(100,int(backup_keep) if backup_keep.isdigit() else 14))}
            store.set_global(cfg);keep_alive.wake() if keep_alive else None;flash("Global bot settings saved.","success");return redirect(url_for("global_settings"))
        return render_template("global.html",cfg=cfg,keep_alive_state=keep_alive.snapshot() if keep_alive else None,bot_online=bot_live(),process_mode=settings.process_mode)

    @app.errorhandler(404)
    def not_found(_): return render_template("error.html",message="That page or server could not be found."),404

    @app.errorhandler(500)
    def internal(_): return render_template("error.html",message="The dashboard hit an unexpected error. Check the service logs for details."),500

    return app
