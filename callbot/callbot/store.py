"""Remembers each chat: recent messages, whether the owner has taken over, and which
WhatsApp messages were already handled (Meta sometimes sends the same one twice)."""

from __future__ import annotations

import os
import sqlite3
import threading
import time


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (phone TEXT, role TEXT, content TEXT, ts REAL);
            CREATE INDEX IF NOT EXISTS messages_phone ON messages(phone, ts);
            CREATE TABLE IF NOT EXISTS chats (
                phone TEXT PRIMARY KEY, name TEXT DEFAULT '', last_seen REAL DEFAULT 0,
                paused_until REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS processed (msg_id TEXT PRIMARY KEY, ts REAL);
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            """
        )
        self._db.commit()

    def _exec(self, sql: str, args: tuple = ()) -> list:
        with self._lock:
            cur = self._db.execute(sql, args)
            rows = cur.fetchall()
            self._db.commit()
            return rows

    # -- dedupe ----------------------------------------------------------------
    def first_time_seen(self, msg_id: str) -> bool:
        """True the first time we see this WhatsApp message id."""
        with self._lock:
            try:
                self._db.execute("INSERT INTO processed VALUES (?,?)", (msg_id, time.time()))
                self._db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    # -- chats -----------------------------------------------------------------
    def _ensure(self, phone: str) -> None:
        self._exec("INSERT OR IGNORE INTO chats(phone) VALUES (?)", (phone,))

    def last_seen(self, phone: str) -> float:
        rows = self._exec("SELECT last_seen FROM chats WHERE phone=?", (phone,))
        return rows[0][0] if rows else 0.0

    def touch(self, phone: str, name: str = "") -> None:
        self._ensure(phone)
        if name:
            self._exec("UPDATE chats SET last_seen=?, name=? WHERE phone=?", (time.time(), name, phone))
        else:
            self._exec("UPDATE chats SET last_seen=? WHERE phone=?", (time.time(), phone))

    def profile_name(self, phone: str) -> str:
        rows = self._exec("SELECT name FROM chats WHERE phone=?", (phone,))
        return rows[0][0] if rows else ""

    def pause(self, phone: str, hours: float) -> None:
        self._ensure(phone)
        self._exec("UPDATE chats SET paused_until=? WHERE phone=?", (time.time() + hours * 3600, phone))

    def resume(self, phone: str) -> None:
        self._exec("UPDATE chats SET paused_until=0 WHERE phone=?", (phone,))

    def is_paused(self, phone: str) -> bool:
        rows = self._exec("SELECT paused_until FROM chats WHERE phone=?", (phone,))
        return bool(rows) and rows[0][0] > time.time()

    # -- messages --------------------------------------------------------------
    def add(self, phone: str, role: str, content: str) -> None:
        self._exec("INSERT INTO messages VALUES (?,?,?,?)", (phone, role, content, time.time()))

    def history(self, phone: str, limit: int = 20, since: float = 0) -> list[dict]:
        rows = self._exec(
            "SELECT role, content FROM messages WHERE phone=? AND ts>=? ORDER BY ts DESC LIMIT ?",
            (phone, since, limit),
        )
        return [{"role": r, "content": c} for r, c in reversed(rows)]

    def forget(self, phone: str) -> None:
        """Delete everything stored about a customer (privacy request)."""
        self._exec("DELETE FROM messages WHERE phone=?", (phone,))
        self._exec("DELETE FROM chats WHERE phone=?", (phone,))

    def cleanup(self, keep_days: int = 30) -> None:
        cutoff = time.time() - keep_days * 86400
        self._exec("DELETE FROM messages WHERE ts<?", (cutoff,))
        self._exec("DELETE FROM processed WHERE ts<?", (cutoff,))

    # -- small key/value (e.g. last reminder run) ------------------------------
    def get(self, k: str) -> str | None:
        rows = self._exec("SELECT v FROM kv WHERE k=?", (k,))
        return rows[0][0] if rows else None

    def set(self, k: str, v: str) -> None:
        self._exec("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, v))
