"""Постоянная память: переписки, сообщения и настройки (Neon PostgreSQL или SQLite).

Совместимость: прежние методы add_message / get_history / list_chats / clear сохранены.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from db import Database

_MAX_HISTORY = 2000


class Memory:
    def __init__(self, db: Database | str | os.PathLike) -> None:
        if not isinstance(db, Database):
            db = Database(str(db))
            db.migrate()
        self.db = db

    # ----------------------------------------------------------------- чаты
    @staticmethod
    def _ensure(tx, chat_id: str, title: str | None = None) -> None:
        now = time.time()
        tx.execute(
            "INSERT INTO chats (id, title, created_at, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (id) DO NOTHING",
            (chat_id, title or "", now, now),
        )
        if title:
            tx.execute("UPDATE chats SET title = ? WHERE id = ? AND title = ''", (title, chat_id))

    def ensure_chat(self, chat_id: str, title: str | None = None) -> dict:
        with self.db.tx() as tx:
            self._ensure(tx, chat_id, title)
            return tx.one("SELECT * FROM chats WHERE id = ?", (chat_id,)) or {}

    def get_chat(self, chat_id: str) -> dict | None:
        return self.db.one("SELECT * FROM chats WHERE id = ?", (chat_id,))

    def list_chats(self, limit: int = 200) -> list[dict]:
        rows = self.db.query(
            "SELECT c.id, c.title, c.created_at, c.updated_at, "
            "(SELECT COUNT(*) FROM messages m WHERE m.chat_id = c.id) AS messages "
            "FROM chats c ORDER BY c.updated_at DESC LIMIT ?",
            (limit,),
        )
        for r in rows:
            r["chat_id"] = r["id"]
            r["last_ts"] = r["updated_at"]
        return rows

    def rename_chat(self, chat_id: str, title: str) -> bool:
        return self.db.execute(
            "UPDATE chats SET title = ?, updated_at = ? WHERE id = ?", (title[:120], time.time(), chat_id)
        ) > 0

    def delete_chat(self, chat_id: str) -> bool:
        """Удаляет чат вместе с сообщениями (каскадно)."""
        with self.db.tx() as tx:
            tx.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            return tx.execute("DELETE FROM chats WHERE id = ?", (chat_id,)) > 0

    # ------------------------------------------------------------ сообщения
    def add_message(
        self, chat_id: str, role: str, content: str, status: str = "done", provider: str = "", model: str = ""
    ) -> int:
        now = time.time()
        with self.db.tx() as tx:
            self._ensure(tx, chat_id)
            row = tx.query(
                "INSERT INTO messages (chat_id, role, content, status, provider, model, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (chat_id, role, content, status, provider, model, now, now),
            )[0]
            tx.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id))
            return int(row["id"])

    def update_message(
        self, message_id: int, content: str | None = None, status: str | None = None,
        provider: str | None = None, model: str | None = None,
    ) -> None:
        sets, params = ["updated_at = ?"], [time.time()]
        for col, val in (("content", content), ("status", status), ("provider", provider), ("model", model)):
            if val is not None:
                sets.append(f"{col} = ?")
                params.append(val)
        params.append(message_id)
        self.db.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id = ?", params)

    def delete_message(self, message_id: int) -> bool:
        return self.db.execute("DELETE FROM messages WHERE id = ?", (message_id,)) > 0

    def get_history(self, chat_id: str, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(limit, _MAX_HISTORY))
        rows = self.db.query(
            "SELECT id, role, content, status, provider, model, created_at AS ts FROM messages "
            "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        return list(reversed(rows))

    def sync_history(self, chat_id: str, messages: list[dict]) -> int:
        """Приводит сохранённую переписку к виду `messages` (общий префикс не трогается).

        Нужна, потому что браузер присылает всю историю: так восстанавливается переписка,
        которой нет на сервере, а перегенерация ответа не плодит дубли.
        Возвращает число изменённых строк.
        """
        now = time.time()
        with self.db.tx() as tx:
            self._ensure(tx, chat_id)
            stored = tx.query("SELECT id, role, content FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,))
            p = 0
            while (
                p < len(stored) and p < len(messages)
                and stored[p]["role"] == messages[p]["role"]
                and stored[p]["content"] == messages[p]["content"]
            ):
                p += 1
            changed = 0
            if p < len(stored):
                changed += tx.execute("DELETE FROM messages WHERE chat_id = ? AND id >= ?", (chat_id, stored[p]["id"]))
            for m in messages[p:]:
                tx.execute(
                    "INSERT INTO messages (chat_id, role, content, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'done', ?, ?)",
                    (chat_id, m["role"], m["content"], now, now),
                )
                changed += 1
            if changed:
                tx.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id))
            return changed

    def mark_interrupted(self, older_than_s: float = 60.0) -> int:
        """Ответы, которые «идут» слишком давно, значит процесс умер на середине: помечаем прерванными."""
        cutoff = time.time() - older_than_s
        with self.db.tx() as tx:
            tx.execute("DELETE FROM messages WHERE status = 'streaming' AND content = '' AND updated_at < ?", (cutoff,))
            return tx.execute(
                "UPDATE messages SET status = 'interrupted', updated_at = ? WHERE status = 'streaming' AND updated_at < ?",
                (time.time(), cutoff),
            )

    def clear(self, chat_id: str) -> int:
        return self.db.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))

    # ------------------------------------------------------------ настройки
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.db.one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )

    def delete_setting(self, key: str) -> bool:
        return self.db.execute("DELETE FROM settings WHERE key = ?", (key,)) > 0

    def all_settings(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for r in self.db.query("SELECT key, value FROM settings ORDER BY key"):
            try:
                out[r["key"]] = json.loads(r["value"])
            except json.JSONDecodeError:
                continue
        return out

    # ----------------------------------------------------- резервная копия
    def export_all(self) -> dict:
        chats = self.db.query("SELECT * FROM chats ORDER BY created_at")
        messages = self.db.query("SELECT * FROM messages ORDER BY id")
        by_chat: dict[str, list] = {}
        for m in messages:
            by_chat.setdefault(m["chat_id"], []).append(m)
        return {
            "version": 1,
            "exported_at": time.time(),
            "chats": [{**c, "messages": by_chat.get(c["id"], [])} for c in chats],
            "settings": self.all_settings(),
        }

    def import_all(self, data: dict) -> dict:
        """Восстановление из резервной копии: существующие чаты не перезаписываются."""
        added_chats = added_msgs = 0
        for c in data.get("chats", []):
            cid = str(c.get("id", ""))[:64]
            if not cid or self.get_chat(cid):
                continue
            self.ensure_chat(cid, c.get("title") or None)
            for m in c.get("messages", []):
                if m.get("role") in ("user", "assistant"):
                    self.add_message(cid, m["role"], str(m.get("content", "")), status=m.get("status", "done"))
                    added_msgs += 1
            added_chats += 1
        for k, v in (data.get("settings") or {}).items():
            if self.get_setting(k) is None:
                self.set_setting(k, v)
        return {"chats": added_chats, "messages": added_msgs}
