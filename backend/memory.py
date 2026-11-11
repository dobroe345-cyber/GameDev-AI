"""Память: история чатов в SQLite."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any


class Memory:
    def __init__(self, db_path: str | Path = "memory.db") -> None:
        self.db_path = str(db_path)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    ts REAL NOT NULL
                )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_chat ON messages(chat_id, id)")

    def add_message(self, chat_id: str, role: str, content: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO messages (chat_id, role, content, ts) VALUES (?, ?, ?, ?)",
                (chat_id, role, content, time.time()),
            )

    def get_history(self, chat_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT role, content, ts FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def list_chats(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT chat_id, COUNT(*) AS messages, MAX(ts) AS last_ts "
                "FROM messages GROUP BY chat_id ORDER BY last_ts DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def clear(self, chat_id: str) -> int:
        with self._conn() as c:
            return c.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,)).rowcount
