"""Очередь задач в БД и воркер с восстановлением после перезапуска.

Задача живёт в таблице tasks: queued -> running -> done | failed | cancelled.
Воркер берёт задачу атомарно и ставит «аренду» (lease), продлевая её, пока работает.
Если процесс умер, аренда истекает, и любой новый процесс возвращает задачу в очередь;
агент продолжает с последнего сохранённого шага (state), а не с нуля.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any, Callable

from ai_core import ProvidersUnavailable
from db import Database

log = logging.getLogger("gdai.tasks")

ACTIVE = ("queued", "running", "waiting")


class TaskCancelled(Exception):
    """Задача отменена (или потеряла аренду): обработчик должен тихо остановиться."""


class PermanentError(Exception):
    """Ошибка, которую бессмысленно повторять (неверные входные данные): задача сразу получает статус failed."""


class WaitForUser(Exception):
    """Агенту нужен ответ человека: задача встаёт в статус waiting (состояние уже сохранено) и ждёт POST /tasks/{id}/answer."""


class TaskInterrupted(Exception):
    """Сервис останавливается: задача вернётся в очередь с сохранённым состоянием."""


def _j(text: str | None, default: Any = None) -> Any:
    try:
        return json.loads(text) if text else default
    except json.JSONDecodeError:
        return default


class TaskStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _row(r: dict | None) -> dict | None:
        if r is None:
            return None
        r = dict(r)
        r["payload"] = _j(r.get("payload"), {})
        r["state"] = _j(r.get("state"), {})
        r["result"] = _j(r.get("result"))
        return r

    # ----------------------------------------------------------- создание
    def create(self, kind: str, payload: dict | None = None, title: str = "", chat_id: str | None = None) -> dict:
        tid, now = uuid.uuid4().hex, time.time()
        self.db.execute(
            "INSERT INTO tasks (id, chat_id, kind, title, status, payload, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'queued', ?, '{}', ?, ?)",
            (tid, chat_id, kind, title[:200], json.dumps(payload or {}, ensure_ascii=False), now, now),
        )
        self.add_event(tid, "info", "Задача создана")
        return self.get(tid)  # type: ignore[return-value]

    def get(self, tid: str) -> dict | None:
        return self._row(self.db.one("SELECT * FROM tasks WHERE id = ?", (tid,)))

    def list(self, status: str | None = None, chat_id: str | None = None, limit: int = 50) -> list[dict]:
        sql, params = "SELECT * FROM tasks WHERE 1 = 1", []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if chat_id:
            sql += " AND chat_id = ?"
            params.append(chat_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 200)))
        return [self._row(r) for r in self.db.query(sql, params)]  # type: ignore[misc]

    # --------------------------------------------------------- выполнение
    def claim_next(self, lease_s: float, now: float | None = None) -> dict | None:
        """Атомарно забирает самую старую готовую задачу. Каждый захват считается попыткой."""
        now = time.time() if now is None else now
        rows = self.db.query(
            "UPDATE tasks SET status = 'running', lease_until = ?, started_at = COALESCE(started_at, ?), "
            "attempts = attempts + 1, updated_at = ? "
            "WHERE status = 'queued' AND id = (SELECT id FROM tasks WHERE status = 'queued' AND run_after <= ? "
            "ORDER BY created_at LIMIT 1) RETURNING id",
            (now + lease_s, now, now, now),
        )
        return self.get(rows[0]["id"]) if rows else None

    def heartbeat(self, tid: str, lease_s: float, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self.db.execute(
            "UPDATE tasks SET lease_until = ?, updated_at = ? WHERE id = ? AND status = 'running'",
            (now + lease_s, now, tid),
        ) > 0

    def save_state(self, tid: str, state: dict, progress: float | None = None) -> bool:
        """False, если задача уже не выполняется (отменена): обработчик должен остановиться."""
        sets, params = ["state = ?", "updated_at = ?"], [json.dumps(state, ensure_ascii=False), time.time()]
        if progress is not None:
            sets.append("progress = ?")
            params.append(max(0.0, min(1.0, progress)))
        params.append(tid)
        return self.db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ? AND status = 'running'", params) > 0

    def complete(self, tid: str, result: Any) -> bool:
        now = time.time()
        return self.db.execute(
            "UPDATE tasks SET status = 'done', result = ?, progress = 1, error = NULL, lease_until = 0, "
            "finished_at = ?, updated_at = ? WHERE id = ? AND status = 'running'",
            (json.dumps(result, ensure_ascii=False), now, now, tid),
        ) > 0

    def fail(self, tid: str, error: str) -> bool:
        now = time.time()
        return self.db.execute(
            "UPDATE tasks SET status = 'failed', error = ?, lease_until = 0, finished_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'running'",
            (error[:1000], now, now, tid),
        ) > 0

    def requeue(self, tid: str, delay_s: float = 0, refund_attempt: bool = False, error: str | None = None) -> bool:
        """Возвращает выполняющуюся задачу в очередь (состояние сохраняется)."""
        now = time.time()
        attempts = "CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END" if refund_attempt else "attempts"
        sets, params = f"status = 'queued', lease_until = 0, run_after = ?, updated_at = ?, attempts = {attempts}", [now + delay_s, now]
        if error is not None:
            sets += ", error = ?"
            params.append(error[:1000])
        params.append(tid)
        return self.db.execute(f"UPDATE tasks SET {sets} WHERE id = ? AND status = 'running'", params) > 0

    def wait_for_user(self, tid: str) -> bool:
        """running -> waiting. Попытка не расходуется: ожидание ответа не ошибка."""
        return self.db.execute(
            "UPDATE tasks SET status = 'waiting', lease_until = 0, updated_at = ?, "
            "attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END WHERE id = ? AND status = 'running'",
            (time.time(), tid),
        ) > 0

    def answer(self, tid: str, answer: dict, qid: int | None = None) -> str:
        """Кладёт ответ человека в состояние и возвращает задачу в очередь. Возвращает 'ok' | 'not_waiting' | 'stale'."""
        now = time.time()
        with self.db.tx() as tx:
            row = tx.one("SELECT state, status FROM tasks WHERE id = ?", (tid,))
            if not row or row["status"] != "waiting":
                return "not_waiting"
            state = _j(row["state"], {})
            q = state.get("question")
            if not q or (qid is not None and q.get("id") != qid):
                return "stale"
            state["answer"] = answer
            tx.execute("UPDATE tasks SET status = 'queued', state = ?, run_after = 0, lease_until = 0, updated_at = ? "
                       "WHERE id = ? AND status = 'waiting'", (json.dumps(state, ensure_ascii=False), now, tid))
            tx.execute("INSERT INTO task_events (task_id, ts, level, message) VALUES (?, ?, 'info', ?)",
                       (tid, now, "Ответ получен: " + ("«решай сам»" if answer.get("auto") else answer.get("text", "")[:120])))
        return "ok"

    def cancel(self, tid: str) -> bool:
        now = time.time()
        ok = self.db.execute(
            "UPDATE tasks SET status = 'cancelled', lease_until = 0, finished_at = ?, updated_at = ? "
            "WHERE id = ? AND status IN ('queued', 'running', 'waiting')",
            (now, now, tid),
        ) > 0
        if ok:
            self.add_event(tid, "warn", "Задача отменена")
        return ok

    def retry(self, tid: str) -> bool:
        """Повторный запуск неудавшейся или отменённой задачи: продолжит с сохранённого состояния."""
        ok = self.db.execute(
            "UPDATE tasks SET status = 'queued', attempts = 0, run_after = 0, lease_until = 0, error = NULL, "
            "finished_at = NULL, updated_at = ? WHERE id = ? AND status IN ('failed', 'cancelled')",
            (time.time(), tid),
        ) > 0
        if ok:
            self.add_event(tid, "info", "Запущена повторно")
        return ok

    def requeue_expired(self, max_attempts: int, now: float | None = None) -> dict:
        """Возвращает в очередь задачи умершего процесса (аренда истекла)."""
        now = time.time() if now is None else now
        requeued, failed = [], []
        with self.db.tx() as tx:
            for r in tx.query("SELECT id, attempts FROM tasks WHERE status = 'running' AND lease_until < ?", (now,)):
                if r["attempts"] >= max_attempts:
                    n = tx.execute(
                        "UPDATE tasks SET status = 'failed', error = ?, finished_at = ?, updated_at = ? "
                        "WHERE id = ? AND status = 'running' AND lease_until < ?",
                        ("Процесс прерывался слишком много раз во время этой задачи", now, now, r["id"], now),
                    )
                    (failed if n else []).append(r["id"])
                else:
                    n = tx.execute(
                        "UPDATE tasks SET status = 'queued', lease_until = 0, run_after = 0, updated_at = ? "
                        "WHERE id = ? AND status = 'running' AND lease_until < ?",
                        (now, r["id"], now),
                    )
                    (requeued if n else []).append(r["id"])
            for tid in requeued:
                tx.execute("INSERT INTO task_events (task_id, ts, level, message) VALUES (?, ?, 'warn', ?)",
                           (tid, now, "Процесс был прерван: задача возвращена в очередь и продолжится с сохранённого шага"))
            for tid in failed:
                tx.execute("INSERT INTO task_events (task_id, ts, level, message) VALUES (?, ?, 'error', ?)",
                           (tid, now, "Превышено число попыток"))
        return {"requeued": requeued, "failed": failed}

    # ------------------------------------------------------------- события
    def add_event(self, tid: str, level: str, message: str) -> None:
        self.db.execute(
            "INSERT INTO task_events (task_id, ts, level, message) VALUES (?, ?, ?, ?)",
            (tid, time.time(), level, message[:500]),
        )

    def events(self, tid: str, limit: int = 100) -> list[dict]:
        rows = self.db.query(
            "SELECT id, ts, level, message FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (tid, max(1, min(limit, 500))),
        )
        return list(reversed(rows))

    def next_due(self) -> float | None:
        """Когда воркеру нужно проснуться: срок ближайшей отложенной задачи или истечения аренды. None, если дел нет."""
        row = self.db.one(
            "SELECT MIN(CASE WHEN status = 'queued' THEN run_after ELSE lease_until END) AS t "
            "FROM tasks WHERE status IN ('queued', 'running')"
        )
        return row["t"] if row and row["t"] is not None else None

    def counts(self) -> dict[str, int]:
        return {r["status"]: int(r["n"]) for r in self.db.query("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")}


class TaskContext:
    """Что получает обработчик задачи."""

    def __init__(self, task: dict, store: TaskStore, deps: dict, worker: "TaskWorker") -> None:
        self.task, self.store, self.deps, self._worker = task, store, deps, worker
        self.state: dict = task.get("state") or {}
        self.payload: dict = task.get("payload") or {}

    def __getattr__(self, name: str) -> Any:   # ctx.router, ctx.memory, ctx.files
        try:
            return self.__dict__["deps"][name]
        except KeyError:
            raise AttributeError(name) from None

    def log(self, message: str, level: str = "info") -> None:
        self.store.add_event(self.task["id"], level, message)

    def check(self) -> None:
        self._worker.raise_if_stopping(self.task["id"])

    def checkpoint(self, progress: float | None = None) -> None:
        """Сохраняет прогресс в БД. Вызывай после каждого завершённого шага."""
        self.check()
        self.state["_defers"] = 0
        if not self.store.save_state(self.task["id"], self.state, progress):
            raise TaskCancelled()


Handler = Callable[[TaskContext], Any]
HANDLERS: dict[str, Handler] = {}


def register_task(kind: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn
    return deco


class TaskWorker:
    def __init__(
        self, store: TaskStore, deps: dict, *, lease_s: float = 60, max_attempts: int = 3,
        poll_s: float = 600.0, handlers: dict[str, Handler] | None = None,
    ) -> None:
        self.store, self.deps = store, deps
        self.lease_s, self.max_attempts, self.poll_s = lease_s, max_attempts, poll_s
        self.handlers = handlers if handlers is not None else HANDLERS
        self._stop = threading.Event()
        self._wake = threading.Event()     # «появилась работа»: будит воркер без опроса БД
        self._lost = threading.Event()     # аренда потеряна или задача отменена
        self._current: str | None = None
        self._threads: list[threading.Thread] = []

    # ---------------------------------------------------------------- старт
    def start(self) -> None:
        self.recover()
        for fn, name in ((self._loop, "gdai-worker"), (self._heartbeat_loop, "gdai-heartbeat")):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def recover(self) -> dict:
        res = self.store.requeue_expired(self.max_attempts)
        if res["requeued"] or res["failed"]:
            log.warning("Восстановление задач: %s", res)
        return res

    def notify(self) -> None:
        """Вызывать после создания задачи: воркер проснётся сразу."""
        self._wake.set()

    def stop(self, timeout: float = 8.0) -> None:
        """Мягкая остановка: текущая задача возвращается в очередь и продолжится в новом процессе."""
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout=timeout)
        tid = self._current
        if tid:  # обработчик не успел остановиться: отдаём задачу сразу, а не ждём истечения аренды
            self.store.requeue(tid, refund_attempt=True)
            self.store.add_event(tid, "info", "Сервис остановлен: задача вернётся в очередь")

    def raise_if_stopping(self, tid: str) -> None:
        if self._lost.is_set():
            raise TaskCancelled()
        if self._stop.is_set():
            raise TaskInterrupted()

    # ----------------------------------------------------------------- цикл
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.run_once():
                    self._sleep()
            except Exception:  # воркер не должен умирать из-за сбоя БД и т.п.
                log.exception("Сбой цикла воркера")
                self._stop.wait(min(self.poll_s, 30.0))

    def _sleep(self) -> None:
        """Спит до ближайшего дела. Бесконечный опрос не даёт «заснуть» бесплатной БД Neon и съедает её часы."""
        due = self.store.next_due()
        delay = self.poll_s if due is None else max(0.05, min(due - time.time() + 0.05, self.poll_s))
        self._wake.wait(delay)
        self._wake.clear()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(max(1.0, self.lease_s / 3)):
            self.beat_once()

    def beat_once(self) -> None:
        tid = self._current
        if tid:
            try:
                if not self.store.heartbeat(tid, self.lease_s):
                    self._lost.set()
            except Exception:
                log.exception("Не удалось продлить аренду")

    def maintenance(self) -> None:
        self.store.requeue_expired(self.max_attempts)

    def run_once(self) -> bool:
        """Берёт и выполняет одну задачу. True, если задача была."""
        self.maintenance()
        task = self.store.claim_next(self.lease_s)
        if not task:
            return False
        tid = task["id"]
        self._lost.clear()
        self._current = tid
        ctx = TaskContext(task, self.store, self.deps, self)
        handler = self.handlers.get(task["kind"])
        try:
            if handler is None:
                raise RuntimeError(f"Неизвестный тип задачи «{task['kind']}»")
            ctx.log(f"Старт (попытка {task['attempts']} из {self.max_attempts})")
            result = handler(ctx)
            if self.store.complete(tid, result):
                ctx.log("Готово")
        except TaskCancelled:
            ctx.log("Остановлена", "warn")
        except WaitForUser:
            self.store.wait_for_user(tid)
            ctx.log("Жду ответа пользователя")
        except TaskInterrupted:
            self.store.requeue(tid, refund_attempt=True)
            ctx.log("Сервис останавливается: задача вернётся в очередь", "warn")
        except PermanentError as e:
            self.store.fail(tid, str(e))
            ctx.log(f"Ошибка: {e}", "error")
        except ProvidersUnavailable as e:
            # Модели недоступны (например, выключен GPU): ждём, не тратя попытки
            k = int(ctx.state.get("_defers", 0)) + 1
            ctx.state["_defers"] = k
            self.store.save_state(tid, ctx.state)
            delay = min(30 * 2 ** (k - 1), 300)
            self.store.requeue(tid, delay_s=delay, refund_attempt=True)
            ctx.log(f"Модели недоступны, повтор через {int(delay)} с: {e}", "warn")
        except Exception as e:
            log.exception("Задача %s упала", tid)
            msg = f"{type(e).__name__}: {e}"
            if task["attempts"] >= self.max_attempts:
                self.store.fail(tid, msg)
                ctx.log(f"Ошибка, попытки исчерпаны: {msg}", "error")
            else:
                delay = 5 * task["attempts"]
                self.store.requeue(tid, delay_s=delay, error=msg)
                ctx.log(f"Ошибка, повтор через {delay} с: {msg}", "error")
        finally:
            self._current = None
        return True
