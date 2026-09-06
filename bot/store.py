from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from bot.config import settings

SCHEMA_VERSION = 2


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat()


DEFAULT_GUILD = {
    "schema_version": SCHEMA_VERSION,
    "features": {
        "tickets": True,
        "announcements": True,
        "welcome": True,
        "autorole": True,
        "verification": True,
        "moderation": True,
        "automod": False,
        "logs": True,
        "auto_messages": True,
        "bot_dms": True,
        "server_stats": False,
        "live_dashboard": True,
    },
    "channels": {
        "welcome": None,
        "logs": None,
        "mod_logs": None,
        "automod_logs": None,
        "announcements": None,
        "ticket_category": None,
        "ticket_logs": None,
    },
    "roles": {
        "autorole": None,
        "verification_add": None,
        "verification_remove": None,
        "ticket_support": None,
        "staff": None,
    },
    "welcome": {
        "content": "Welcome {mention} to **{server}**! You are member #{member_count}.",
        "dm_enabled": False,
        "dm_content": "Welcome to {server}, {user}!",
    },
    "tickets": {
        "panel_title": "Support Tickets",
        "panel_description": "Need help? Press the button below to create a private ticket.",
        "button_label": "Create Ticket",
        "opening_message": "Thanks for opening a ticket, {mention}. A staff member will be with you shortly.",
        "max_open_per_user": 1,
        "auto_close_hours": 0,
        "transcript_limit": 1500,
        "types": [
            {
                "key": "general",
                "enabled": True,
                "name": "General Support",
                "emoji": "🎫",
                "description": "General help and support",
                "category_id": None,
                "support_role_id": None,
                "priority": "normal",
                "questions": [
                    {"label": "What do you need help with?", "placeholder": "Describe the issue", "required": True, "style": "paragraph"}
                ],
            }
        ],
    },
    "verification": {
        "panel_title": "Verification",
        "panel_description": "Verify with Discord OAuth to unlock the server.",
        "button_label": "Verify with Discord",
        "success_message": "Verification complete. You can return to Discord.",
    },
    "moderation": {
        "dm_on_action": True,
        "reason_required": False,
        "warning_actions": [
            {"count": 3, "action": "timeout", "duration_minutes": 10},
            {"count": 5, "action": "timeout", "duration_minutes": 60},
            {"count": 7, "action": "kick", "duration_minutes": 0},
            {"count": 10, "action": "ban", "duration_minutes": 0},
        ],
    },
    "automod": {
        "spam_enabled": True,
        "spam_messages": 6,
        "spam_window_seconds": 8,
        "duplicate_enabled": True,
        "duplicate_count": 4,
        "mention_enabled": True,
        "mention_limit": 6,
        "caps_enabled": False,
        "caps_percent": 80,
        "caps_min_length": 16,
        "invite_block": False,
        "link_block": False,
        "domain_blacklist": [],
        "domain_whitelist": [],
        "bad_words": [],
        "ignore_roles": [],
        "ignore_channels": [],
        "action": "timeout",
        "timeout_minutes": 10,
        "delete_trigger": True,
        "new_account_enabled": False,
        "min_account_age_hours": 24,
        "join_rate_enabled": True,
        "join_rate_count": 12,
        "join_rate_window_seconds": 30,
        "auto_raid_mode": True,
        "raid_mode_active": False,
        "raid_kick_new_accounts": True,
        "raid_lock_channels": [],
    },
    "server_stats": {
        "category_id": None,
        "category_name": "│ SERVER STATS │",
        "update_interval_seconds": 300,
        "items": [
            {"enabled": True, "type": "members", "label": "Members", "emoji": "👥", "template": "{emoji} {label}: {value}", "role_id": None, "channel_id": None},
            {"enabled": True, "type": "humans", "label": "Humans", "emoji": "🧑", "template": "{emoji} {label}: {value}", "role_id": None, "channel_id": None},
            {"enabled": True, "type": "bots", "label": "Bots", "emoji": "🤖", "template": "{emoji} {label}: {value}", "role_id": None, "channel_id": None},
            {"enabled": True, "type": "boosts", "label": "Boosts", "emoji": "🚀", "template": "{emoji} {label}: {value}", "role_id": None, "channel_id": None},
        ],
    },
    "appearance": {
        "accent_color": "#7c3aed",
        "success_color": "#22c55e",
        "warning_color": "#f59e0b",
        "error_color": "#ef4444",
        "footer_text": "",
        "footer_icon_url": "",
        "ticket_color": "#7c3aed",
        "moderation_color": "#ef4444",
        "welcome_color": "#7c3aed",
    },
}

DEFAULT_GLOBAL = {
    "schema_version": SCHEMA_VERSION,
    "statuses": [
        {"type": "watching", "text": "the server"},
        {"type": "playing", "text": "/help"},
    ],
    "status_interval_seconds": 45,
    "keep_alive": {"enabled": False, "interval_seconds": 300},
    "backup": {"enabled": True, "interval_hours": 24, "keep": 14},
}


def _merge(default: dict, current: dict | None) -> dict:
    out = deepcopy(default)
    for k, v in (current or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _migrate_config(default: dict, current: dict | None) -> dict:
    """Apply additive configuration migrations before merging defaults.

    v2 introduced AutoMod, richer tickets, appearance, server stats and the
    management-platform scheduler. Older JSON documents are intentionally kept
    compatible; unknown keys are preserved and missing v2 keys are filled by
    the default merge. Future migrations can be added here by schema version.
    """
    raw = deepcopy(current or {})
    version = int(raw.get("schema_version") or 1)
    if version < 2:
        # Earlier builds called the simple recurring-message feature
        # `announcements` in a few development snapshots. Preserve it while
        # normalizing the current feature switches additively.
        raw["schema_version"] = 2
    merged = _merge(default, raw)
    merged["schema_version"] = SCHEMA_VERSION
    return merged


class Store:
    """MongoDB-first persistence with a zero-setup SQLite fallback.

    Newer feature data uses JSON payload tables/collections so schema migrations remain
    additive and old deployments can be upgraded in-place without losing settings.
    """

    def __init__(self) -> None:
        self.mongo = None
        self.lock = threading.RLock()
        if settings.mongodb_uri:
            from pymongo import MongoClient
            self.mongo = MongoClient(
                settings.mongodb_uri,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                socketTimeoutMS=10000,
                retryWrites=True,
                retryReads=True,
            )[settings.mongodb_db]
            self.mongo.command("ping")
            self._mongo_indexes()
        else:
            os.makedirs("data", exist_ok=True)
            self.db = sqlite3.connect("data/bot.db", check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self._sqlite_schema()

    def _mongo_indexes(self) -> None:
        self.mongo.guilds.create_index("guild_id", unique=True)
        self.mongo.tickets.create_index("channel_id", unique=True)
        self.mongo.tickets.create_index([("guild_id", 1), ("owner_id", 1), ("status", 1)])
        self.mongo.automessages.create_index([("guild_id", 1), ("id", 1)], unique=True)
        self.mongo.warnings.create_index([("guild_id", 1), ("user_id", 1)])
        self.mongo.cases.create_index([("guild_id", 1), ("id", 1)], unique=True)
        self.mongo.cases.create_index([("guild_id", 1), ("user_id", 1), ("created_at", -1)])
        self.mongo.audit_logs.create_index([("guild_id", 1), ("created_at", -1)])
        self.mongo.message_templates.create_index([("guild_id", 1), ("id", 1)], unique=True)
        self.mongo.scheduled_messages.create_index([("guild_id", 1), ("id", 1)], unique=True)
        self.mongo.scheduled_messages.create_index([("enabled", 1), ("next_run", 1)])
        self.mongo.guild_snapshots.create_index("guild_id", unique=True)
        self.mongo.action_queue.create_index([("status", 1), ("created_at", 1)])
        self.mongo.backups.create_index([("guild_id", 1), ("created_at", -1)])

    def _sqlite_schema(self) -> None:
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS guilds (guild_id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS globals (key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tickets (
                channel_id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                status TEXT NOT NULL, claimed_by TEXT, created_at TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS automessages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                content TEXT NOT NULL, interval_minutes INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                last_sent TEXT
            );
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, user_id TEXT NOT NULL,
                moderator_id TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, user_id TEXT NOT NULL,
                moderator_id TEXT NOT NULL, type TEXT NOT NULL, reason TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, actor_id TEXT,
                action TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS message_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, name TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, next_run TEXT NOT NULL, recurrence_minutes INTEGER NOT NULL DEFAULT 0,
                last_run TEXT, created_at TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS guild_snapshots (
                guild_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS action_queue (
                id TEXT PRIMARY KEY, guild_id TEXT, action TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL,
                result TEXT
            );
            CREATE TABLE IF NOT EXISTS backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
        """)
        # Old SQLite copies from earlier builds lack the JSON payload ticket column.
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(tickets)").fetchall()}
        if "payload" not in cols:
            self.db.execute("ALTER TABLE tickets ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'")
        self.db.commit()

    def health(self) -> tuple[bool, str]:
        """Cheap readiness probe for the active persistence backend."""
        try:
            if self.mongo is not None:
                self.mongo.command("ping")
                return True, "mongodb"
            with self.lock:
                self.db.execute("SELECT 1").fetchone()
            return True, "sqlite"
        except Exception as exc:
            return False, f"{type(exc).__name__}"

    # ---------- configuration ----------
    def get_guild(self, guild_id: int) -> dict:
        gid = str(guild_id)
        if self.mongo is not None:
            row = self.mongo.guilds.find_one({"guild_id": gid}) or {}
            row.pop("_id", None); row.pop("guild_id", None)
            return _migrate_config(DEFAULT_GUILD, row)
        with self.lock:
            row = self.db.execute("SELECT data FROM guilds WHERE guild_id=?", (gid,)).fetchone()
        return _migrate_config(DEFAULT_GUILD, json.loads(row["data"]) if row else {})

    def set_guild(self, guild_id: int, data: dict) -> dict:
        gid = str(guild_id)
        merged = _merge(DEFAULT_GUILD, data)
        merged["schema_version"] = SCHEMA_VERSION
        if self.mongo is not None:
            self.mongo.guilds.update_one({"guild_id": gid}, {"$set": {"guild_id": gid, **merged}}, upsert=True)
        else:
            with self.lock:
                self.db.execute("INSERT INTO guilds(guild_id,data) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET data=excluded.data", (gid, _json(merged)))
                self.db.commit()
        return merged

    def get_global(self) -> dict:
        if self.mongo is not None:
            row = self.mongo.globals.find_one({"key": "global"}) or {}
            return _migrate_config(DEFAULT_GLOBAL, row.get("data", {}))
        with self.lock:
            row = self.db.execute("SELECT data FROM globals WHERE key='global'").fetchone()
        return _migrate_config(DEFAULT_GLOBAL, json.loads(row["data"]) if row else {})

    def set_global(self, data: dict) -> dict:
        merged = _merge(DEFAULT_GLOBAL, data); merged["schema_version"] = SCHEMA_VERSION
        if self.mongo is not None:
            self.mongo.globals.update_one({"key": "global"}, {"$set": {"key": "global", "data": merged}}, upsert=True)
        else:
            with self.lock:
                self.db.execute("INSERT INTO globals(key,data) VALUES('global',?) ON CONFLICT(key) DO UPDATE SET data=excluded.data", (_json(merged),))
                self.db.commit()
        return merged

    # ---------- tickets ----------
    def create_ticket(self, guild_id: int, channel_id: int, owner_id: int, **extra: Any) -> None:
        now = iso_now()
        payload = {
            "type_key": extra.get("type_key", "general"),
            "type_name": extra.get("type_name", "General Support"),
            "answers": extra.get("answers", []),
            "priority": extra.get("priority", "normal"),
            "first_response_at": None,
            "closed_at": None,
            "closed_by": None,
            "transcript_name": None,
        }
        doc = {"guild_id": str(guild_id), "channel_id": str(channel_id), "owner_id": str(owner_id), "status": "open", "claimed_by": None, "created_at": now, **payload}
        if self.mongo is not None:
            self.mongo.tickets.update_one({"channel_id": str(channel_id)}, {"$set": doc}, upsert=True)
        else:
            with self.lock:
                self.db.execute(
                    "INSERT OR REPLACE INTO tickets(channel_id,guild_id,owner_id,status,claimed_by,created_at,payload) VALUES(?,?,?,?,?,?,?)",
                    (str(channel_id), str(guild_id), str(owner_id), "open", None, now, _json(payload)),
                )
                self.db.commit()

    def _ticket_row(self, row: Any) -> dict | None:
        if not row: return None
        d = dict(row)
        if "payload" in d:
            try: d.update(json.loads(d.pop("payload") or "{}"))
            except json.JSONDecodeError: d.pop("payload", None)
        return d

    def get_ticket(self, channel_id: int) -> dict | None:
        cid = str(channel_id)
        if self.mongo is not None:
            row = self.mongo.tickets.find_one({"channel_id": cid}, {"_id": 0})
            return row
        with self.lock:
            return self._ticket_row(self.db.execute("SELECT * FROM tickets WHERE channel_id=?", (cid,)).fetchone())

    def update_ticket(self, channel_id: int, **fields: Any) -> None:
        cid = str(channel_id)
        core = {k: fields.pop(k) for k in list(fields) if k in {"status", "claimed_by"}}
        if self.mongo is not None:
            update = {**core, **fields}
            if update: self.mongo.tickets.update_one({"channel_id": cid}, {"$set": update})
            return
        with self.lock:
            row = self.db.execute("SELECT payload FROM tickets WHERE channel_id=?", (cid,)).fetchone()
            payload = json.loads(row["payload"] or "{}") if row else {}
            payload.update(fields)
            sets, values = ["payload=?"], [_json(payload)]
            for k, v in core.items(): sets.append(f"{k}=?"); values.append(v)
            values.append(cid)
            self.db.execute(f"UPDATE tickets SET {','.join(sets)} WHERE channel_id=?", values)
            self.db.commit()

    def list_tickets(self, guild_id: int, *, status: str | None = None, owner_id: int | None = None, limit: int = 200) -> list[dict]:
        gid = str(guild_id)
        if self.mongo is not None:
            query: dict[str, Any] = {"guild_id": gid}
            if status: query["status"] = status
            if owner_id is not None: query["owner_id"] = str(owner_id)
            return list(self.mongo.tickets.find(query, {"_id": 0}).sort("created_at", -1).limit(limit))
        sql, args = "SELECT * FROM tickets WHERE guild_id=?", [gid]
        if status: sql += " AND status=?"; args.append(status)
        if owner_id is not None: sql += " AND owner_id=?"; args.append(str(owner_id))
        sql += " ORDER BY created_at DESC LIMIT ?"; args.append(limit)
        with self.lock: rows = self.db.execute(sql, args).fetchall()
        return [self._ticket_row(r) for r in rows if r]

    def ticket_analytics(self, guild_id: int) -> dict:
        rows = self.list_tickets(guild_id, limit=5000)
        now = utcnow()
        opened = [r for r in rows if r.get("status") == "open"]
        closed = [r for r in rows if r.get("status") == "closed"]
        today = now.date()
        closed_today = sum(1 for r in closed if str(r.get("closed_at") or "")[:10] == today.isoformat())
        response_seconds, resolve_seconds = [], []
        by_type: dict[str, int] = {}
        by_staff: dict[str, int] = {}
        for r in rows:
            by_type[r.get("type_name") or r.get("type_key") or "Unknown"] = by_type.get(r.get("type_name") or r.get("type_key") or "Unknown", 0) + 1
            if r.get("claimed_by"): by_staff[str(r["claimed_by"])] = by_staff.get(str(r["claimed_by"]), 0) + 1
            try:
                created = datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00"))
                if r.get("first_response_at"):
                    first = datetime.fromisoformat(str(r["first_response_at"]).replace("Z", "+00:00")); response_seconds.append(max(0, (first-created).total_seconds()))
                if r.get("closed_at"):
                    end = datetime.fromisoformat(str(r["closed_at"]).replace("Z", "+00:00")); resolve_seconds.append(max(0, (end-created).total_seconds()))
            except (ValueError, KeyError): pass
        return {
            "open": len(opened), "closed_today": closed_today, "total": len(rows),
            "avg_response_minutes": round(sum(response_seconds)/len(response_seconds)/60, 1) if response_seconds else None,
            "avg_resolution_minutes": round(sum(resolve_seconds)/len(resolve_seconds)/60, 1) if resolve_seconds else None,
            "by_type": sorted(by_type.items(), key=lambda x: x[1], reverse=True)[:8],
            "by_staff": sorted(by_staff.items(), key=lambda x: x[1], reverse=True)[:8],
        }

    # ---------- legacy interval automessages ----------
    def list_automessages(self, guild_id: int) -> list[dict]:
        gid = str(guild_id)
        if self.mongo is not None:
            return sorted(list(self.mongo.automessages.find({"guild_id": gid}, {"_id": 0})), key=lambda x: int(x["id"]))
        with self.lock: rows = self.db.execute("SELECT * FROM automessages WHERE guild_id=? ORDER BY id", (gid,)).fetchall()
        return [dict(r) for r in rows]

    def all_automessages(self) -> list[dict]:
        if self.mongo is not None: return list(self.mongo.automessages.find({}, {"_id": 0}))
        with self.lock: rows = self.db.execute("SELECT * FROM automessages").fetchall()
        return [dict(r) for r in rows]

    def _next_counter(self, key: str) -> int:
        if self.mongo is not None:
            from pymongo import ReturnDocument
            row = self.mongo.counters.find_one_and_update({"key": key}, {"$inc": {"value": 1}}, upsert=True, return_document=ReturnDocument.AFTER)
            return int(row["value"])
        raise RuntimeError("SQLite counters use rowids")

    def add_automessage(self, guild_id: int, channel_id: int, content: str, interval_minutes: int, enabled: bool = True) -> int:
        gid = str(guild_id)
        if self.mongo is not None:
            mid = self._next_counter("automessage")
            self.mongo.automessages.insert_one({"id": mid, "guild_id": gid, "channel_id": str(channel_id), "content": content, "interval_minutes": int(interval_minutes), "enabled": bool(enabled), "last_sent": None})
            return mid
        with self.lock:
            cur = self.db.execute("INSERT INTO automessages(guild_id,channel_id,content,interval_minutes,enabled,last_sent) VALUES(?,?,?,?,?,NULL)", (gid, str(channel_id), content, int(interval_minutes), int(enabled))); self.db.commit(); return int(cur.lastrowid)

    def delete_automessage(self, guild_id: int, msg_id: int) -> None:
        if self.mongo is not None: self.mongo.automessages.delete_one({"guild_id": str(guild_id), "id": int(msg_id)}); return
        with self.lock: self.db.execute("DELETE FROM automessages WHERE guild_id=? AND id=?", (str(guild_id), int(msg_id))); self.db.commit()

    def update_automessage(self, guild_id: int, msg_id: int, *, channel_id: int, content: str, interval_minutes: int, enabled: bool) -> None:
        fields = {"channel_id": str(channel_id), "content": content, "interval_minutes": int(interval_minutes), "enabled": bool(enabled)}
        if self.mongo is not None: self.mongo.automessages.update_one({"guild_id": str(guild_id), "id": int(msg_id)}, {"$set": fields}); return
        with self.lock: self.db.execute("UPDATE automessages SET channel_id=?,content=?,interval_minutes=?,enabled=? WHERE guild_id=? AND id=?", (str(channel_id), content, int(interval_minutes), int(enabled), str(guild_id), int(msg_id))); self.db.commit()

    def mark_automessage_sent(self, msg_id: int) -> None:
        now = iso_now()
        if self.mongo is not None: self.mongo.automessages.update_one({"id": int(msg_id)}, {"$set": {"last_sent": now}}); return
        with self.lock: self.db.execute("UPDATE automessages SET last_sent=? WHERE id=?", (now, int(msg_id))); self.db.commit()

    # ---------- moderation warnings/cases ----------
    def add_warning(self, guild_id: int, user_id: int, moderator_id: int, reason: str) -> int:
        now = iso_now()
        if self.mongo is not None:
            wid = self._next_counter("warning"); self.mongo.warnings.insert_one({"id": wid, "guild_id": str(guild_id), "user_id": str(user_id), "moderator_id": str(moderator_id), "reason": reason, "created_at": now}); return wid
        with self.lock:
            cur = self.db.execute("INSERT INTO warnings(guild_id,user_id,moderator_id,reason,created_at) VALUES(?,?,?,?,?)", (str(guild_id),str(user_id),str(moderator_id),reason,now)); self.db.commit(); return int(cur.lastrowid)

    def list_warnings(self, guild_id: int, user_id: int) -> list[dict]:
        if self.mongo is not None: return list(self.mongo.warnings.find({"guild_id": str(guild_id), "user_id": str(user_id)}, {"_id": 0}).sort("id", 1))
        with self.lock: rows = self.db.execute("SELECT * FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id", (str(guild_id),str(user_id))).fetchall()
        return [dict(r) for r in rows]

    def clear_warnings(self, guild_id: int, user_id: int) -> int:
        if self.mongo is not None: return int(self.mongo.warnings.delete_many({"guild_id": str(guild_id), "user_id": str(user_id)}).deleted_count)
        with self.lock: cur = self.db.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?", (str(guild_id),str(user_id))); self.db.commit(); return int(cur.rowcount)

    def create_case(self, guild_id: int, user_id: int, moderator_id: int, case_type: str, reason: str, *, status: str = "active", **payload: Any) -> int:
        now = iso_now()
        if self.mongo is not None:
            cid = self._next_counter("case"); self.mongo.cases.insert_one({"id": cid,"guild_id":str(guild_id),"user_id":str(user_id),"moderator_id":str(moderator_id),"type":case_type,"reason":reason,"status":status,"created_at":now,**payload}); return cid
        with self.lock:
            cur = self.db.execute("INSERT INTO cases(guild_id,user_id,moderator_id,type,reason,status,created_at,payload) VALUES(?,?,?,?,?,?,?,?)", (str(guild_id),str(user_id),str(moderator_id),case_type,reason,status,now,_json(payload))); self.db.commit(); return int(cur.lastrowid)

    def _case_row(self, row: Any) -> dict | None:
        if not row: return None
        d = dict(row); payload = d.pop("payload", "{}")
        try: d.update(json.loads(payload or "{}"))
        except json.JSONDecodeError: pass
        return d

    def get_case(self, guild_id: int, case_id: int) -> dict | None:
        if self.mongo is not None: return self.mongo.cases.find_one({"guild_id": str(guild_id), "id": int(case_id)}, {"_id": 0})
        with self.lock: return self._case_row(self.db.execute("SELECT * FROM cases WHERE guild_id=? AND id=?", (str(guild_id), int(case_id))).fetchone())

    def list_cases(self, guild_id: int, *, user_id: int | None = None, case_type: str | None = None, limit: int = 100) -> list[dict]:
        if self.mongo is not None:
            q: dict[str, Any] = {"guild_id": str(guild_id)}
            if user_id is not None: q["user_id"] = str(user_id)
            if case_type: q["type"] = case_type
            return list(self.mongo.cases.find(q, {"_id": 0}).sort("id", -1).limit(limit))
        sql, args = "SELECT * FROM cases WHERE guild_id=?", [str(guild_id)]
        if user_id is not None: sql += " AND user_id=?"; args.append(str(user_id))
        if case_type: sql += " AND type=?"; args.append(case_type)
        sql += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        with self.lock: rows = self.db.execute(sql, args).fetchall()
        return [self._case_row(r) for r in rows if r]

    def update_case(self, guild_id: int, case_id: int, **fields: Any) -> bool:
        allowed = {k:v for k,v in fields.items() if k in {"reason","status","duration_minutes","expires_at","note"}}
        if not allowed: return False
        if self.mongo is not None:
            return bool(self.mongo.cases.update_one({"guild_id":str(guild_id),"id":int(case_id)}, {"$set":allowed}).modified_count)
        with self.lock:
            row = self.db.execute("SELECT payload,reason,status FROM cases WHERE guild_id=? AND id=?", (str(guild_id),int(case_id))).fetchone()
            if not row: return False
            payload = json.loads(row["payload"] or "{}"); core = {}
            for k,v in allowed.items():
                if k in {"reason","status"}: core[k]=v
                else: payload[k]=v
            sets=["payload=?"]; vals=[_json(payload)]
            for k,v in core.items(): sets.append(f"{k}=?"); vals.append(v)
            vals.extend([str(guild_id),int(case_id)])
            self.db.execute(f"UPDATE cases SET {','.join(sets)} WHERE guild_id=? AND id=?", vals); self.db.commit(); return True

    def delete_case(self, guild_id: int, case_id: int) -> bool:
        if self.mongo is not None: return bool(self.mongo.cases.delete_one({"guild_id":str(guild_id),"id":int(case_id)}).deleted_count)
        with self.lock: cur=self.db.execute("DELETE FROM cases WHERE guild_id=? AND id=?", (str(guild_id),int(case_id))); self.db.commit(); return bool(cur.rowcount)

    # ---------- dashboard audit ----------
    def audit(self, guild_id: int, actor_id: int | str | None, action: str, **payload: Any) -> int | str:
        now = iso_now()
        if self.mongo is not None:
            aid = self._next_counter("audit"); self.mongo.audit_logs.insert_one({"id":aid,"guild_id":str(guild_id),"actor_id":str(actor_id) if actor_id else None,"action":action,"created_at":now,**payload}); return aid
        with self.lock:
            cur=self.db.execute("INSERT INTO audit_logs(guild_id,actor_id,action,created_at,payload) VALUES(?,?,?,?,?)", (str(guild_id),str(actor_id) if actor_id else None,action,now,_json(payload))); self.db.commit(); return int(cur.lastrowid)

    def list_audit(self, guild_id: int, limit: int = 100) -> list[dict]:
        if self.mongo is not None: return list(self.mongo.audit_logs.find({"guild_id":str(guild_id)}, {"_id":0}).sort("created_at", -1).limit(limit))
        with self.lock: rows=self.db.execute("SELECT * FROM audit_logs WHERE guild_id=? ORDER BY id DESC LIMIT ?", (str(guild_id),int(limit))).fetchall()
        out=[]
        for r in rows:
            d=dict(r); payload=d.pop("payload","{}")
            try:d.update(json.loads(payload or "{}"))
            except json.JSONDecodeError:pass
            out.append(d)
        return out

    # ---------- message templates + scheduler ----------
    def save_template(self, guild_id: int, name: str, payload: dict, template_id: int | None = None) -> int:
        now=iso_now(); gid=str(guild_id)
        if self.mongo is not None:
            if template_id:
                self.mongo.message_templates.update_one({"guild_id":gid,"id":int(template_id)}, {"$set":{"name":name,"payload":payload,"updated_at":now}}); return int(template_id)
            tid=self._next_counter("message_template"); self.mongo.message_templates.insert_one({"id":tid,"guild_id":gid,"name":name,"created_at":now,"updated_at":now,"payload":payload}); return tid
        with self.lock:
            if template_id:
                self.db.execute("UPDATE message_templates SET name=?,updated_at=?,payload=? WHERE guild_id=? AND id=?", (name,now,_json(payload),gid,int(template_id))); self.db.commit(); return int(template_id)
            cur=self.db.execute("INSERT INTO message_templates(guild_id,name,created_at,updated_at,payload) VALUES(?,?,?,?,?)", (gid,name,now,now,_json(payload))); self.db.commit(); return int(cur.lastrowid)

    def list_templates(self, guild_id:int)->list[dict]:
        gid=str(guild_id)
        if self.mongo is not None: rows=list(self.mongo.message_templates.find({"guild_id":gid},{"_id":0}).sort("name",1))
        else:
            with self.lock: raw=self.db.execute("SELECT * FROM message_templates WHERE guild_id=? ORDER BY name",(gid,)).fetchall()
            rows=[]
            for r in raw:
                d=dict(r); d["payload"]=json.loads(d.get("payload") or "{}"); rows.append(d)
        return rows

    def get_template(self,guild_id:int,template_id:int)->dict|None:
        if self.mongo is not None:return self.mongo.message_templates.find_one({"guild_id":str(guild_id),"id":int(template_id)},{"_id":0})
        with self.lock:r=self.db.execute("SELECT * FROM message_templates WHERE guild_id=? AND id=?",(str(guild_id),int(template_id))).fetchone()
        if not r:return None
        d=dict(r);d["payload"]=json.loads(d.get("payload") or "{}");return d

    def delete_template(self,guild_id:int,template_id:int)->None:
        if self.mongo is not None:self.mongo.message_templates.delete_one({"guild_id":str(guild_id),"id":int(template_id)});return
        with self.lock:self.db.execute("DELETE FROM message_templates WHERE guild_id=? AND id=?",(str(guild_id),int(template_id)));self.db.commit()

    def add_scheduled_message(self,guild_id:int,channel_id:int,next_run:str,payload:dict,recurrence_minutes:int=0,enabled:bool=True)->int:
        gid=str(guild_id);now=iso_now()
        if self.mongo is not None:
            sid=self._next_counter("scheduled_message");self.mongo.scheduled_messages.insert_one({"id":sid,"guild_id":gid,"channel_id":str(channel_id),"enabled":bool(enabled),"next_run":next_run,"recurrence_minutes":int(recurrence_minutes),"last_run":None,"created_at":now,"payload":payload});return sid
        with self.lock:
            cur=self.db.execute("INSERT INTO scheduled_messages(guild_id,channel_id,enabled,next_run,recurrence_minutes,last_run,created_at,payload) VALUES(?,?,?,?,?,?,?,?)",(gid,str(channel_id),int(enabled),next_run,int(recurrence_minutes),None,now,_json(payload)));self.db.commit();return int(cur.lastrowid)

    def list_scheduled_messages(self,guild_id:int|None=None,*,due_before:str|None=None,limit:int=200)->list[dict]:
        if self.mongo is not None:
            q={}
            if guild_id is not None:q["guild_id"]=str(guild_id)
            if due_before:q.update({"enabled":True,"next_run":{"$lte":due_before}})
            return list(self.mongo.scheduled_messages.find(q,{"_id":0}).sort("next_run",1).limit(limit))
        sql="SELECT * FROM scheduled_messages WHERE 1=1";args=[]
        if guild_id is not None:sql+=" AND guild_id=?";args.append(str(guild_id))
        if due_before:sql+=" AND enabled=1 AND next_run<=?";args.append(due_before)
        sql+=" ORDER BY next_run LIMIT ?";args.append(int(limit))
        with self.lock:rows=self.db.execute(sql,args).fetchall()
        out=[]
        for r in rows:
            d=dict(r);d["payload"]=json.loads(d.get("payload") or "{}");out.append(d)
        return out

    def mark_schedule_run(self,schedule_id:int,*,next_run:str|None=None,disable:bool=False)->None:
        now=iso_now()
        if self.mongo is not None:
            fields={"last_run":now}
            if next_run:fields["next_run"]=next_run
            if disable:fields["enabled"]=False
            self.mongo.scheduled_messages.update_one({"id":int(schedule_id)},{"$set":fields});return
        with self.lock:
            if disable:self.db.execute("UPDATE scheduled_messages SET last_run=?,enabled=0 WHERE id=?",(now,int(schedule_id)))
            elif next_run:self.db.execute("UPDATE scheduled_messages SET last_run=?,next_run=? WHERE id=?",(now,next_run,int(schedule_id)))
            else:self.db.execute("UPDATE scheduled_messages SET last_run=? WHERE id=?",(now,int(schedule_id)))
            self.db.commit()

    def delete_scheduled_message(self,guild_id:int,schedule_id:int)->None:
        if self.mongo is not None:self.mongo.scheduled_messages.delete_one({"guild_id":str(guild_id),"id":int(schedule_id)});return
        with self.lock:self.db.execute("DELETE FROM scheduled_messages WHERE guild_id=? AND id=?",(str(guild_id),int(schedule_id)));self.db.commit()

    # ---------- snapshots + split-process queue ----------
    def set_snapshot(self,guild_id:int,payload:dict)->None:
        now=iso_now();gid=str(guild_id)
        if self.mongo is not None:self.mongo.guild_snapshots.update_one({"guild_id":gid},{"$set":{"guild_id":gid,"updated_at":now,"payload":payload}},upsert=True);return
        with self.lock:self.db.execute("INSERT INTO guild_snapshots(guild_id,updated_at,payload) VALUES(?,?,?) ON CONFLICT(guild_id) DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload",(gid,now,_json(payload)));self.db.commit()

    def get_snapshot(self,guild_id:int)->dict|None:
        if self.mongo is not None:
            r=self.mongo.guild_snapshots.find_one({"guild_id":str(guild_id)},{"_id":0});return r.get("payload")|{"updated_at":r.get("updated_at")} if r else None
        with self.lock:r=self.db.execute("SELECT * FROM guild_snapshots WHERE guild_id=?",(str(guild_id),)).fetchone()
        if not r:return None
        d=json.loads(r["payload"] or "{}");d["updated_at"]=r["updated_at"];return d

    def list_snapshots(self)->list[dict]:
        if self.mongo is not None:return [{**r.get("payload",{}),"guild_id":r["guild_id"],"updated_at":r.get("updated_at")} for r in self.mongo.guild_snapshots.find({}, {"_id":0})]
        with self.lock:rows=self.db.execute("SELECT * FROM guild_snapshots").fetchall()
        return [{**json.loads(r["payload"] or "{}"),"guild_id":r["guild_id"],"updated_at":r["updated_at"]} for r in rows]

    def enqueue_action(self,guild_id:int|None,action:str,payload:dict)->str:
        aid=uuid.uuid4().hex;now=iso_now();doc={"id":aid,"guild_id":str(guild_id) if guild_id else None,"action":action,"status":"queued","created_at":now,"updated_at":now,"payload":payload,"result":None}
        if self.mongo is not None:self.mongo.action_queue.insert_one(doc);return aid
        with self.lock:self.db.execute("INSERT INTO action_queue(id,guild_id,action,status,created_at,updated_at,payload,result) VALUES(?,?,?,?,?,?,?,?)",(aid,doc["guild_id"],action,"queued",now,now,_json(payload),None));self.db.commit();return aid

    def claim_actions(self,limit:int=20)->list[dict]:
        if self.mongo is not None:
            rows=list(self.mongo.action_queue.find({"status":"queued"},{"_id":0}).sort("created_at",1).limit(limit))
            for r in rows:self.mongo.action_queue.update_one({"id":r["id"],"status":"queued"},{"$set":{"status":"running","updated_at":iso_now()}})
            return rows
        with self.lock:
            rows=self.db.execute("SELECT * FROM action_queue WHERE status='queued' ORDER BY created_at LIMIT ?",(limit,)).fetchall();out=[]
            for r in rows:
                self.db.execute("UPDATE action_queue SET status='running',updated_at=? WHERE id=? AND status='queued'",(iso_now(),r["id"]));d=dict(r);d["payload"]=json.loads(d["payload"] or "{}");out.append(d)
            self.db.commit();return out

    def finish_action(self,action_id:str,ok:bool,result:str="")->None:
        status="done" if ok else "failed";now=iso_now()
        if self.mongo is not None:self.mongo.action_queue.update_one({"id":action_id},{"$set":{"status":status,"updated_at":now,"result":result[:2000]}});return
        with self.lock:self.db.execute("UPDATE action_queue SET status=?,updated_at=?,result=? WHERE id=?",(status,now,result[:2000],action_id));self.db.commit()

    # ---------- backups ----------
    def backup_guild(self,guild_id:int)->int:
        payload={"config":self.get_guild(guild_id),"templates":self.list_templates(guild_id),"automessages":self.list_automessages(guild_id),"schedules":self.list_scheduled_messages(guild_id),"created_at":iso_now()}
        if self.mongo is not None:
            bid=self._next_counter("backup");self.mongo.backups.insert_one({"id":bid,"guild_id":str(guild_id),"created_at":iso_now(),"payload":payload});return bid
        with self.lock:cur=self.db.execute("INSERT INTO backups(guild_id,created_at,payload) VALUES(?,?,?)",(str(guild_id),iso_now(),_json(payload)));self.db.commit();return int(cur.lastrowid)

    def list_backups(self,guild_id:int,limit:int=20)->list[dict]:
        if self.mongo is not None:return list(self.mongo.backups.find({"guild_id":str(guild_id)},{"_id":0,"payload":0}).sort("created_at",-1).limit(limit))
        with self.lock:rows=self.db.execute("SELECT id,guild_id,created_at FROM backups WHERE guild_id=? ORDER BY id DESC LIMIT ?",(str(guild_id),int(limit))).fetchall()
        return [dict(r) for r in rows]

    def prune_backups(self, guild_id: int, keep: int = 14) -> int:
        """Keep only the newest N configuration backups for a guild."""
        gid = str(guild_id); keep = max(1, min(100, int(keep)))
        if self.mongo is not None:
            old = list(self.mongo.backups.find({"guild_id": gid}, {"_id": 1}).sort("created_at", -1).skip(keep))
            if not old: return 0
            result = self.mongo.backups.delete_many({"_id": {"$in": [r["_id"] for r in old]}})
            return int(result.deleted_count)
        with self.lock:
            rows = self.db.execute("SELECT id FROM backups WHERE guild_id=? ORDER BY id DESC LIMIT -1 OFFSET ?", (gid, keep)).fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                self.db.executemany("DELETE FROM backups WHERE id=?", [(i,) for i in ids]); self.db.commit()
            return len(ids)

    def restore_backup(self,guild_id:int,backup_id:int)->bool:
        if self.mongo is not None:r=self.mongo.backups.find_one({"guild_id":str(guild_id),"id":int(backup_id)})
        else:
            with self.lock:r=self.db.execute("SELECT payload FROM backups WHERE guild_id=? AND id=?",(str(guild_id),int(backup_id))).fetchone()
            if r:r={"payload":json.loads(r["payload"])}
        if not r:return False
        payload=r.get("payload",{}); self.set_guild(guild_id,payload.get("config",{}))
        # Restore message templates as part of the configuration snapshot. Existing
        # templates are replaced so the restored state matches the selected backup.
        templates = list(payload.get("templates") or []); automessages = list(payload.get("automessages") or []); schedules = list(payload.get("schedules") or [])
        gid = str(guild_id)
        if self.mongo is not None:
            self.mongo.message_templates.delete_many({"guild_id": gid}); self.mongo.automessages.delete_many({"guild_id": gid}); self.mongo.scheduled_messages.delete_many({"guild_id": gid})
        else:
            with self.lock:
                self.db.execute("DELETE FROM message_templates WHERE guild_id=?", (gid,)); self.db.execute("DELETE FROM automessages WHERE guild_id=?", (gid,)); self.db.execute("DELETE FROM scheduled_messages WHERE guild_id=?", (gid,)); self.db.commit()
        for row in templates:
            self.save_template(guild_id, str(row.get("name") or "Restored template"), row.get("payload") or {})
        for row in automessages:
            self.add_automessage(guild_id, int(row.get("channel_id") or 0), str(row.get("content") or ""), int(row.get("interval_minutes") or 60), bool(row.get("enabled", True)))
        for row in schedules:
            if str(row.get("channel_id") or "").isdigit() and row.get("next_run"):
                self.add_scheduled_message(guild_id, int(row["channel_id"]), str(row["next_run"]), row.get("payload") or {}, int(row.get("recurrence_minutes") or 0), bool(row.get("enabled", True)))
        return True


store = Store()
