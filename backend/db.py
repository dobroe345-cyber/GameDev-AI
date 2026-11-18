"""Слой базы данных: Neon PostgreSQL (боевой режим) и SQLite (разработка и тесты).

SQL пишется один раз с плейсхолдерами `?`; для PostgreSQL они заменяются на `%s`.
Сервер на Render не хранит данные локально, поэтому в боевом режиме нужен DATABASE_URL.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Sequence


class DatabaseError(RuntimeError):
    """База недоступна или вернула ошибку."""


# ---------------------------------------------------------------- миграции
# {AUTO} подставляется под диалект. Новые изменения схемы добавляй НОВОЙ записью в конец.
MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        1,
        "initial",
        [
            """CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS messages (
                id {AUTO},
                chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'done',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id)",
            """CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                chat_id TEXT,
                kind TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                state TEXT NOT NULL DEFAULT '{}',
                result TEXT,
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                progress DOUBLE PRECISION NOT NULL DEFAULT 0,
                run_after DOUBLE PRECISION NOT NULL DEFAULT 0,
                lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                started_at DOUBLE PRECISION,
                finished_at DOUBLE PRECISION
            )""",
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, run_after)",
            """CREATE TABLE IF NOT EXISTS task_events (
                id {AUTO},
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                ts DOUBLE PRECISION NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, id)",
            """CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                storage_key TEXT NOT NULL,
                size BIGINT NOT NULL,
                sha256 TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'file',
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )""",
        ],
    ),
    (
        2,
        "auth_usage_projects",
        [
            """CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                created_at DOUBLE PRECISION NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS auth_codes (
                id {AUTO},
                email TEXT NOT NULL,
                purpose TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                used INTEGER NOT NULL DEFAULT 0,
                created_at DOUBLE PRECISION NOT NULL,
                expires_at DOUBLE PRECISION NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_codes_email ON auth_codes(email, id)",
            """CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                last_used DOUBLE PRECISION NOT NULL,
                expires_at DOUBLE PRECISION NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_sessions_email ON sessions(email)",
            """CREATE TABLE IF NOT EXISTS usage (
                day TEXT NOT NULL,
                provider TEXT NOT NULL,
                requests INTEGER NOT NULL DEFAULT 0,
                in_chars BIGINT NOT NULL DEFAULT 0,
                out_chars BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (day, provider)
            )""",
            """CREATE TABLE IF NOT EXISTS project_files (
                project TEXT NOT NULL,
                path TEXT NOT NULL,
                content TEXT NOT NULL,
                size INTEGER NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (project, path)
            )""",
        ],
    ),
]

_AUTO = {"sqlite": "INTEGER PRIMARY KEY AUTOINCREMENT", "postgres": "BIGSERIAL PRIMARY KEY"}
_MIGRATION_LOCK_ID = 727274  # advisory lock: два процесса не мигрируют одновременно


def _pg_sql(sql: str) -> str:
    return sql.replace("?", "%s")


class Tx:
    """Одна транзакция. Все методы возвращают обычные dict."""

    def __init__(self, cur: Any, dialect: str) -> None:
        self._cur = cur
        self.dialect = dialect

    def _run(self, sql: str, params: Sequence[Any]) -> None:
        self._cur.execute(_pg_sql(sql) if self.dialect == "postgres" else sql, tuple(params))

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        self._run(sql, params)
        return max(self._cur.rowcount, 0)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        self._run(sql, params)
        rows = self._cur.fetchall()
        return [dict(r) for r in rows]

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None


def _load_psycopg() -> SimpleNamespace:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as e:  # pragma: no cover - зависит от окружения
        raise DatabaseError("Для PostgreSQL нужен пакет psycopg: pip install 'psycopg[binary]'") from e
    return SimpleNamespace(connect=psycopg.connect, OperationalError=psycopg.OperationalError, dict_row=dict_row)


class Database:
    PING_AFTER = 20.0  # секунд простоя, после которых соединение проверяется

    def __init__(self, url: str | Path, *, driver: SimpleNamespace | None = None) -> None:
        url = str(url)
        self.url = url
        if url.startswith(("postgres://", "postgresql://")):
            self.dialect = "postgres"
            self._driver = driver or _load_psycopg()
            self._local = threading.local()
        else:
            self.dialect = "sqlite"
            path = url[len("sqlite:///"):] if url.startswith("sqlite:///") else url
            self._lock = threading.RLock()
            if path not in (":memory:", ""):
                Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path or ":memory:", check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=10000")
            if path not in (":memory:", ""):
                self._conn.execute("PRAGMA journal_mode=WAL")

    @property
    def persistent(self) -> bool:
        """Данные переживают перезапуск Render только в PostgreSQL."""
        return self.dialect == "postgres"

    # ------------------------------------------------------------ PostgreSQL
    def _pg_connect(self) -> Any:
        last: Exception | None = None
        for delay in (0, 0.5, 1.5, 3.0):  # Neon просыпается за доли секунды, но даём запас
            if delay:
                time.sleep(delay)
            try:
                return self._driver.connect(
                    self.url,
                    row_factory=self._driver.dict_row,
                    connect_timeout=15,
                    prepare_threshold=None,  # совместимость с PgBouncer (pooled-строка Neon)
                )
            except self._driver.OperationalError as e:
                last = e
        raise DatabaseError(f"Не удалось подключиться к PostgreSQL: {last}")

    def _pg_get(self) -> Any:
        loc = self._local
        conn = getattr(loc, "conn", None)
        if conn is not None and getattr(conn, "closed", False):
            conn = None
        if conn is not None and time.monotonic() - getattr(loc, "last", 0.0) > self.PING_AFTER:
            try:  # соединение могло быть закрыто при засыпании Neon
                conn.execute("SELECT 1")
                conn.rollback()
            except Exception:
                self._pg_drop()
                conn = None
        if conn is None:
            conn = self._pg_connect()
            loc.conn = conn
        return conn

    def _pg_drop(self) -> None:
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------ транзакции
    @contextmanager
    def tx(self) -> Iterator[Tx]:
        if self.dialect == "sqlite":
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    yield Tx(self._conn.cursor(), "sqlite")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
                else:
                    self._conn.execute("COMMIT")
            return
        conn = self._pg_get()
        try:
            yield Tx(conn.cursor(), "postgres")
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                self._pg_drop()
            raise
        finally:
            self._local.last = time.monotonic()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.tx() as t:
            return t.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        with self.tx() as t:
            return t.query(sql, params)

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        with self.tx() as t:
            return t.one(sql, params)

    # -------------------------------------------------------------- миграции
    def migrate(self) -> list[int]:
        """Применяет недостающие миграции. Безопасно вызывать при каждом старте."""
        applied_now: list[int] = []
        with self.tx() as t:
            if self.dialect == "postgres":
                t.execute("SELECT pg_advisory_xact_lock(?)", (_MIGRATION_LOCK_ID,))
            t.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at DOUBLE PRECISION NOT NULL)"
            )
            done = {r["version"] for r in t.query("SELECT version FROM schema_migrations")}
            for version, name, statements in MIGRATIONS:
                if version in done:
                    continue
                for stmt in statements:
                    t.execute(stmt.replace("{AUTO}", _AUTO[self.dialect]))
                t.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, time.time()),
                )
                applied_now.append(version)
        return applied_now

    def ping(self) -> bool:
        try:
            self.one("SELECT 1 AS ok")
            return True
        except Exception:
            return False

    def close(self) -> None:
        if self.dialect == "sqlite":
            with self._lock:
                self._conn.close()
        else:
            self._pg_drop()
