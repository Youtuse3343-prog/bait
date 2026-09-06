from __future__ import annotations
import json
import os
import sqlite3
import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from bot.config import settings

DEFAULT_GUILD = {
    "features": {
        "tickets": True,
        "announcements": True,
        "welcome": True,
        "autorole": True,
        "verification": True,
        "moderation": True,
        "logs": True,
        "auto_messages": True,
        "bot_dms": True,
        "server_stats": False,
    },
    "channels": {
        "welcome": None,
        "logs": None,
        "mod_logs": None,
        "announcements": None,
        "ticket_category": None,
        "ticket_logs": None,
    },
    "roles": {
        "autorole": None,
        "verification_add": None,
        "verification_remove": None,
        "ticket_support": None,
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
}

DEFAULT_GLOBAL = {
    "statuses": [
        {"type": "watching", "text": "the server"},
        {"type": "playing", "text": "/help"},
    ],
    "status_interval_seconds": 45,
    "keep_alive": {
        "enabled": False,
        "interval_seconds": 300,
    },
}


def _merge(default: dict, current: dict) -> dict:
    out = deepcopy(default)
    for k, v in (current or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Store:
    def __init__(self) -> None:
        self.mongo = None
        self.lock = threading.RLock()
        if settings.mongodb_uri:
            from pymongo import MongoClient
            self.mongo = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5000)[settings.mongodb_db]
            self.mongo.command("ping")
            self.mongo.guilds.create_index("guild_id", unique=True)
            self.mongo.tickets.create_index("channel_id", unique=True)
            self.mongo.automessages.create_index([("guild_id", 1), ("id", 1)], unique=True)
            self.mongo.warnings.create_index([("guild_id", 1), ("user_id", 1)])
        else:
            os.makedirs("data", exist_ok=True)
            self.db = sqlite3.connect("data/bot.db", check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS guilds (guild_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS globals (key TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tickets (
                    channel_id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    status TEXT NOT NULL, claimed_by TEXT, created_at TEXT NOT NULL
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
            """)
            self.db.commit()

    def get_guild(self, guild_id: int) -> dict:
        gid = str(guild_id)
        if self.mongo is not None:
            row = self.mongo.guilds.find_one({"guild_id": gid}) or {}
            row.pop("_id", None)
            row.pop("guild_id", None)
            return _merge(DEFAULT_GUILD, row)
        with self.lock:
            row = self.db.execute("SELECT data FROM guilds WHERE guild_id=?", (gid,)).fetchone()
        return _merge(DEFAULT_GUILD, json.loads(row["data"]) if row else {})

    def set_guild(self, guild_id: int, data: dict) -> dict:
        gid = str(guild_id)
        merged = _merge(DEFAULT_GUILD, data)
        if self.mongo is not None:
            self.mongo.guilds.update_one({"guild_id": gid}, {"$set": {"guild_id": gid, **merged}}, upsert=True)
        else:
            with self.lock:
                self.db.execute("INSERT INTO guilds(guild_id,data) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET data=excluded.data", (gid, json.dumps(merged)))
                self.db.commit()
        return merged

    def get_global(self) -> dict:
        if self.mongo is not None:
            row = self.mongo.globals.find_one({"key": "global"}) or {}
            return _merge(DEFAULT_GLOBAL, row.get("data", {}))
        with self.lock:
            row = self.db.execute("SELECT data FROM globals WHERE key='global'").fetchone()
        return _merge(DEFAULT_GLOBAL, json.loads(row["data"]) if row else {})

    def set_global(self, data: dict) -> dict:
        merged = _merge(DEFAULT_GLOBAL, data)
        if self.mongo is not None:
            self.mongo.globals.update_one({"key": "global"}, {"$set": {"key": "global", "data": merged}}, upsert=True)
        else:
            with self.lock:
                self.db.execute("INSERT INTO globals(key,data) VALUES('global',?) ON CONFLICT(key) DO UPDATE SET data=excluded.data", (json.dumps(merged),))
                self.db.commit()
        return merged

    def create_ticket(self, guild_id: int, channel_id: int, owner_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        doc = {"guild_id": str(guild_id), "channel_id": str(channel_id), "owner_id": str(owner_id), "status": "open", "claimed_by": None, "created_at": now}
        if self.mongo is not None:
            self.mongo.tickets.update_one({"channel_id": str(channel_id)}, {"$set": doc}, upsert=True)
        else:
            with self.lock:
                self.db.execute("INSERT OR REPLACE INTO tickets(channel_id,guild_id,owner_id,status,claimed_by,created_at) VALUES(?,?,?,?,?,?)", tuple(doc[k] for k in ["channel_id","guild_id","owner_id","status","claimed_by","created_at"]))
                self.db.commit()

    def get_ticket(self, channel_id: int) -> dict | None:
        cid = str(channel_id)
        if self.mongo is not None:
            row = self.mongo.tickets.find_one({"channel_id": cid})
            if row: row.pop("_id", None)
            return row
        with self.lock:
            row = self.db.execute("SELECT * FROM tickets WHERE channel_id=?", (cid,)).fetchone()
        return dict(row) if row else None

    def update_ticket(self, channel_id: int, **fields: Any) -> None:
        if self.mongo is not None:
            self.mongo.tickets.update_one({"channel_id": str(channel_id)}, {"$set": fields})
        else:
            allowed = {"status", "claimed_by"}
            fields = {k: v for k, v in fields.items() if k in allowed}
            if not fields: return
            sql = ",".join(f"{k}=?" for k in fields)
            with self.lock:
                self.db.execute(f"UPDATE tickets SET {sql} WHERE channel_id=?", [*fields.values(), str(channel_id)])
                self.db.commit()

    def list_automessages(self, guild_id: int) -> list[dict]:
        gid = str(guild_id)
        if self.mongo is not None:
            rows = list(self.mongo.automessages.find({"guild_id": gid}, {"_id": 0}))
            return sorted(rows, key=lambda x: int(x["id"]))
        with self.lock:
            rows = self.db.execute("SELECT * FROM automessages WHERE guild_id=? ORDER BY id", (gid,)).fetchall()
        return [dict(r) for r in rows]

    def all_automessages(self) -> list[dict]:
        if self.mongo is not None:
            return list(self.mongo.automessages.find({}, {"_id": 0}))
        with self.lock:
            rows = self.db.execute("SELECT * FROM automessages").fetchall()
        return [dict(r) for r in rows]

    def add_automessage(self, guild_id: int, channel_id: int, content: str, interval_minutes: int, enabled: bool = True) -> int:
        gid = str(guild_id)
        if self.mongo is not None:
            from pymongo import ReturnDocument
            last = self.mongo.counters.find_one_and_update({"key":"automessage"}, {"$inc":{"value":1}}, upsert=True, return_document=ReturnDocument.AFTER)
            msg_id = int(last["value"])
            self.mongo.automessages.insert_one({"id": msg_id, "guild_id": gid, "channel_id": str(channel_id), "content": content, "interval_minutes": interval_minutes, "enabled": bool(enabled), "last_sent": None})
            return msg_id
        with self.lock:
            cur = self.db.execute("INSERT INTO automessages(guild_id,channel_id,content,interval_minutes,enabled,last_sent) VALUES(?,?,?,?,?,NULL)", (gid,str(channel_id),content,interval_minutes,int(enabled)))
            self.db.commit()
            return int(cur.lastrowid)

    def delete_automessage(self, guild_id: int, msg_id: int) -> None:
        if self.mongo is not None:
            self.mongo.automessages.delete_one({"guild_id": str(guild_id), "id": int(msg_id)})
        else:
            with self.lock:
                self.db.execute("DELETE FROM automessages WHERE guild_id=? AND id=?", (str(guild_id), int(msg_id)))
                self.db.commit()

    def update_automessage(self, guild_id: int, msg_id: int, *, channel_id: int, content: str, interval_minutes: int, enabled: bool) -> None:
        fields = {"channel_id": str(channel_id), "content": content, "interval_minutes": int(interval_minutes), "enabled": bool(enabled)}
        if self.mongo is not None:
            self.mongo.automessages.update_one({"guild_id": str(guild_id), "id": int(msg_id)}, {"$set": fields})
        else:
            with self.lock:
                self.db.execute("UPDATE automessages SET channel_id=?, content=?, interval_minutes=?, enabled=? WHERE guild_id=? AND id=?", (str(channel_id), content, int(interval_minutes), int(enabled), str(guild_id), int(msg_id)))
                self.db.commit()

    def mark_automessage_sent(self, msg_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if self.mongo is not None:
            self.mongo.automessages.update_one({"id": int(msg_id)}, {"$set": {"last_sent": now}})
        else:
            with self.lock:
                self.db.execute("UPDATE automessages SET last_sent=? WHERE id=?", (now, int(msg_id)))
                self.db.commit()

    def add_warning(self, guild_id: int, user_id: int, moderator_id: int, reason: str) -> int:
        now = datetime.now(timezone.utc).isoformat()
        if self.mongo is not None:
            from pymongo import ReturnDocument
            last = self.mongo.counters.find_one_and_update({"key":"warning"}, {"$inc":{"value":1}}, upsert=True, return_document=ReturnDocument.AFTER)
            wid = int(last["value"])
            self.mongo.warnings.insert_one({"id": wid, "guild_id": str(guild_id), "user_id": str(user_id), "moderator_id": str(moderator_id), "reason": reason, "created_at": now})
            return wid
        with self.lock:
            cur = self.db.execute("INSERT INTO warnings(guild_id,user_id,moderator_id,reason,created_at) VALUES(?,?,?,?,?)", (str(guild_id),str(user_id),str(moderator_id),reason,now))
            self.db.commit()
            return int(cur.lastrowid)

    def list_warnings(self, guild_id: int, user_id: int) -> list[dict]:
        if self.mongo is not None:
            return list(self.mongo.warnings.find({"guild_id": str(guild_id), "user_id": str(user_id)}, {"_id": 0}).sort("id", 1))
        with self.lock:
            rows = self.db.execute("SELECT * FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id", (str(guild_id),str(user_id))).fetchall()
        return [dict(r) for r in rows]

    def clear_warnings(self, guild_id: int, user_id: int) -> int:
        if self.mongo is not None:
            return int(self.mongo.warnings.delete_many({"guild_id": str(guild_id), "user_id": str(user_id)}).deleted_count)
        with self.lock:
            cur = self.db.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?", (str(guild_id),str(user_id)))
            self.db.commit()
            return int(cur.rowcount)


store = Store()
