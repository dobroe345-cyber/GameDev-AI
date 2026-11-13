"""Тесты слоя БД и памяти (SQLite + имитация драйвера PostgreSQL)."""
import sqlite3
import threading

import pytest

from db import Database, DatabaseError, MIGRATIONS, _pg_sql
from memory import Memory


# ----------------------------------------------------------- SQLite / миграции
def test_migrate_is_idempotent(tmp_path):
    db = Database(tmp_path / "a.db")
    assert db.migrate() == [m[0] for m in MIGRATIONS]
    assert db.migrate() == []
    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"chats", "messages", "settings", "tasks", "task_events", "files", "schema_migrations"} <= tables


def test_data_survives_reopen(tmp_path):
    path = tmp_path / "persist.db"
    m = Memory(path)
    m.add_message("c1", "user", "привет")
    m.set_setting("theme", "dark")
    m.db.close()
    m2 = Memory(path)  # «перезапуск сервера»
    assert [x["content"] for x in m2.get_history("c1")] == ["привет"]
    assert m2.get_setting("theme") == "dark"


def test_tx_rolls_back_on_error(tmp_path):
    db = Database(tmp_path / "r.db")
    db.migrate()
    with pytest.raises(RuntimeError):
        with db.tx() as t:
            t.execute("INSERT INTO chats (id, title, created_at, updated_at) VALUES ('x','',1,1)")
            raise RuntimeError("boom")
    assert db.one("SELECT id FROM chats WHERE id='x'") is None


def test_concurrent_writes_are_serialized(tmp_path):
    m = Memory(tmp_path / "t.db")
    errors = []

    def work(n):
        try:
            for i in range(20):
                m.add_message(f"chat{n % 3}", "user", f"{n}-{i}")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    assert sum(len(m.get_history(f"chat{k}", 500)) for k in range(3)) == 120


# ------------------------------------------------------------------- память
def test_add_and_get_history(tmp_path):
    m = Memory(tmp_path / "t.db")
    m.add_message("c1", "user", "привет")
    m.add_message("c1", "assistant", "здравствуй")
    h = m.get_history("c1")
    assert [x["role"] for x in h] == ["user", "assistant"]
    assert h[0]["content"] == "привет"


def test_chats_are_isolated_and_clearable(tmp_path):
    m = Memory(tmp_path / "t.db")
    m.add_message("a", "user", "1")
    m.add_message("b", "user", "2")
    assert {c["chat_id"] for c in m.list_chats()} == {"a", "b"}
    assert m.clear("a") == 1
    assert m.get_history("a") == []
    assert len(m.get_history("b")) == 1


def test_history_limit_keeps_latest(tmp_path):
    m = Memory(tmp_path / "t.db")
    for i in range(5):
        m.add_message("c", "user", str(i))
    assert [x["content"] for x in m.get_history("c", limit=2)] == ["3", "4"]


def test_rename_delete_cascade(tmp_path):
    m = Memory(tmp_path / "t.db")
    m.add_message("c", "user", "x")
    assert m.rename_chat("c", "Новое имя")
    assert m.get_chat("c")["title"] == "Новое имя"
    assert m.delete_chat("c")
    assert m.get_chat("c") is None
    assert m.db.query("SELECT id FROM messages") == []


def test_sync_history_backfills_regenerates_and_avoids_duplicates(tmp_path):
    m = Memory(tmp_path / "t.db")
    h = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
    assert m.sync_history("c", h) == 3            # чата не было: всё восстановлено
    assert m.sync_history("c", h) == 0            # повтор: ничего не меняется
    # перегенерация: клиент убрал последний ответ и прислал ту же историю
    m.add_message("c", "assistant", "старый ответ")
    assert m.sync_history("c", h) == 1            # лишний ответ удалён
    assert [x["content"] for x in m.get_history("c")] == ["a", "b", "c"]
    # правка середины: хвост пересоздаётся
    h2 = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "ИЗМЕНЕНО"}]
    m.sync_history("c", h2)
    assert [x["content"] for x in m.get_history("c")] == ["a", "ИЗМЕНЕНО"]


def test_mark_interrupted_only_old_streaming(tmp_path):
    m = Memory(tmp_path / "t.db")
    old = m.add_message("c", "assistant", "часть", status="streaming")
    m.db.execute("UPDATE messages SET updated_at = updated_at - 500 WHERE id = ?", (old,))
    fresh = m.add_message("c", "assistant", "идёт", status="streaming")
    assert m.mark_interrupted(60) == 1
    status = {x["id"]: x["status"] for x in m.get_history("c")}
    assert status[old] == "interrupted" and status[fresh] == "streaming"


def test_settings_roundtrip(tmp_path):
    m = Memory(tmp_path / "t.db")
    m.set_setting("a", {"x": [1, 2]})
    m.set_setting("a", {"x": [3]})
    assert m.get_setting("a") == {"x": [3]}
    assert m.get_setting("нет", 7) == 7
    assert m.all_settings() == {"a": {"x": [3]}}
    assert m.delete_setting("a") and not m.delete_setting("a")


def test_export_and_import_into_empty_db(tmp_path):
    a = Memory(tmp_path / "a.db")
    a.add_message("c1", "user", "привет")
    a.add_message("c1", "assistant", "мир")
    a.rename_chat("c1", "Тест")
    a.set_setting("s", 1)
    dump = a.export_all()
    b = Memory(tmp_path / "b.db")                  # «потеряна БД» — новая пустая
    assert b.import_all(dump) == {"chats": 1, "messages": 2}
    assert [x["content"] for x in b.get_history("c1")] == ["привет", "мир"]
    assert b.get_chat("c1")["title"] == "Тест" and b.get_setting("s") == 1
    assert b.import_all(dump) == {"chats": 0, "messages": 0}   # повторный импорт ничего не дублирует


# ------------------------------------------- имитация драйвера PostgreSQL
def test_pg_placeholder_conversion():
    assert _pg_sql("SELECT * FROM t WHERE a = ? AND b = ?") == "SELECT * FROM t WHERE a = %s AND b = %s"


class FakeOperationalError(Exception):
    pass


class FakeConn:
    """Подставной psycopg-коннект поверх SQLite: проверяет %s-плейсхолдеры, транзакции и переподключение."""

    def __init__(self, shared, log):
        self._c = shared
        self._log = log
        self.closed = False

    def _tr(self, sql):
        s = sql.replace("%s", "?").replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
        return None if "pg_advisory_xact_lock" in s else s

    def cursor(self):
        return FakeCursor(self)

    def execute(self, sql):
        if self.closed:
            raise FakeOperationalError("closed")
        self._log.append(("exec", sql))

    def commit(self):
        self._log.append(("commit",))

    def rollback(self):
        self._log.append(("rollback",))

    def close(self):
        self.closed = True


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._cur = None
        self.rowcount = -1

    def execute(self, sql, params=()):
        assert "?" not in sql, "в PostgreSQL-режиме не должно остаться ?"
        s = self._conn._tr(sql)
        if s is None:
            return
        self._cur = self._conn._c.execute(s, params)
        self.rowcount = self._cur.rowcount

    def fetchall(self):
        return [dict(r) for r in self._cur.fetchall()]


def make_fake_driver(fail_first=0):
    shared = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    shared.row_factory = sqlite3.Row
    shared.execute("PRAGMA foreign_keys=ON")
    state = {"connects": 0, "log": [], "conns": []}

    def connect(url, **kw):
        state["connects"] += 1
        assert kw["prepare_threshold"] is None
        if state["connects"] <= fail_first:
            raise FakeOperationalError("Neon просыпается")
        c = FakeConn(shared, state["log"])
        state["conns"].append(c)
        return c

    from types import SimpleNamespace
    return SimpleNamespace(connect=connect, OperationalError=FakeOperationalError, dict_row=object()), state


def test_pg_mode_full_memory_flow(monkeypatch):
    drv, state = make_fake_driver()
    db = Database("postgresql://u:p@host/db", driver=drv)
    assert db.dialect == "postgres" and db.persistent
    db.migrate()
    m = Memory(db)
    m.add_message("c", "user", "привет")
    m.sync_history("c", [{"role": "user", "content": "привет"}, {"role": "assistant", "content": "ок"}])
    m.set_setting("k", [1])
    assert [x["content"] for x in m.get_history("c")] == ["привет", "ок"]
    assert state["connects"] == 1                     # соединение переиспользуется
    assert ("commit",) in state["log"]


def test_pg_connect_retries_while_neon_wakes(monkeypatch):
    monkeypatch.setattr("db.time.sleep", lambda s: None)
    drv, state = make_fake_driver(fail_first=2)
    db = Database("postgresql://x", driver=drv)
    db.migrate()
    assert state["connects"] == 3


def test_pg_connect_gives_up_with_clear_error(monkeypatch):
    monkeypatch.setattr("db.time.sleep", lambda s: None)
    drv, _ = make_fake_driver(fail_first=99)
    db = Database("postgresql://x", driver=drv)
    with pytest.raises(DatabaseError, match="Не удалось подключиться"):
        db.migrate()


def test_pg_reconnects_after_idle_connection_died(monkeypatch):
    drv, state = make_fake_driver()
    db = Database("postgresql://x", driver=drv)
    db.migrate()
    Memory(db).add_message("c", "user", "до простоя")
    state["conns"][0].closed = True                   # Neon закрыл соединение при засыпании
    db._local.last -= 100                             # прошло много времени
    Memory(db).add_message("c", "user", "после простоя")
    assert state["connects"] == 2
    assert len(Memory(db).get_history("c")) == 2


def test_pg_rolls_back_on_error():
    drv, state = make_fake_driver()
    db = Database("postgresql://x", driver=drv)
    db.migrate()
    with pytest.raises(ValueError):
        with db.tx() as t:
            t.execute("INSERT INTO chats (id, title, created_at, updated_at) VALUES (%s,'',1,1)".replace("%s", "?"), ("z",))
            raise ValueError
    assert ("rollback",) in state["log"]
