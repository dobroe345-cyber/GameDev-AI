"""Тесты задач и агентов: восстановление после перезапуска, недоступный GPU, отмена, повторы."""
import threading
import time

import pytest

import agents  # noqa: F401  (регистрирует обработчики)
import tasks as tasks_mod
from agents import FINAL_SYSTEM, PLAN_SYSTEM, STEP_SYSTEM, parse_plan
from ai_core import ProvidersUnavailable
from db import Database
from file_manager import FileService
from memory import Memory
from storage import LocalStorage
from tasks import PermanentError, TaskCancelled, TaskStore, TaskWorker, register_task


class Crash(BaseException):
    """Имитация внезапной смерти процесса (kill -9): не перехватывается воркером."""


class FakeRouter:
    def __init__(self):
        self.calls = []
        self.down = False
        self.crash_on_call = None
        self.plan = '["Шаг А", "Шаг Б"]'

    def complete(self, messages, **kw):
        sysmsg = kw.get("system")
        self.calls.append(sysmsg)
        if self.crash_on_call == len(self.calls):
            raise Crash()
        if self.down:
            raise ProvidersUnavailable({"gpu": "выключен"})
        if sysmsg == PLAN_SYSTEM:
            return self.plan
        if sysmsg == STEP_SYSTEM:
            return "результат: " + messages[-1]["content"].splitlines()[-1]
        if sysmsg == FINAL_SYSTEM:
            return "ИТОГ"
        return "ответ в чат"


class Clock:
    """Управляемое время для всего процесса (задачи хранят время в БД)."""

    def __init__(self, monkeypatch):
        self.t = time.time()
        monkeypatch.setattr(time, "time", lambda: self.t)

    def advance(self, s):
        self.t += s


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


def boot(tmp_path, router=None, max_attempts=3, lease=60, db_name="t.db"):
    """Поднимает «процесс»: БД, память, файлы, воркер. Повторный вызов с тем же путём = перезапуск."""
    db = Database(tmp_path / db_name)
    db.migrate()
    mem, store = Memory(db), TaskStore(db)
    files = FileService(db, LocalStorage(tmp_path / "st"))
    router = router or FakeRouter()
    w = TaskWorker(store, {"router": router, "memory": mem, "files": files}, lease_s=lease, max_attempts=max_attempts)
    return w, store, mem, files, router


# ----------------------------------------------------------- основа очереди
def test_lifecycle_and_events(tmp_path):
    w, store, mem, files, r = boot(tmp_path)
    t = store.create("agent", {"goal": "сделай уровень"}, title="уровень", chat_id="c1")
    assert t["status"] == "queued"
    assert w.run_once() is True and w.run_once() is False
    t = store.get(t["id"])
    assert t["status"] == "done" and t["progress"] == 1 and t["result"]["steps"] == 2
    assert [e["message"] for e in store.events(t["id"])][0] == "Задача создана"
    assert [m["content"] for m in mem.get_history("c1")] == ["ИТОГ"]       # итог попал в чат


def test_claim_is_atomic_between_two_workers(tmp_path):
    db = Database(tmp_path / "x.db"); db.migrate()
    store = TaskStore(db)
    store.create("agent", {"goal": "g"})
    got, barrier = [], threading.Barrier(8)

    def grab():
        barrier.wait()
        got.append(store.claim_next(60))

    ts = [threading.Thread(target=grab) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sum(1 for g in got if g) == 1                                    # задачу получил ровно один


def test_run_after_and_fifo_order(tmp_path, clock):
    db = Database(tmp_path / "x.db"); db.migrate()
    store = TaskStore(db)
    a = store.create("k", {}); clock.advance(1)
    b = store.create("k", {})
    db.execute("UPDATE tasks SET run_after = ? WHERE id = ?", (clock.t + 100, a["id"]))
    assert store.claim_next(60)["id"] == b["id"]                            # a ещё «спит»
    assert store.claim_next(60) is None
    clock.advance(101)
    assert store.claim_next(60)["id"] == a["id"]


# ------------------------------------------------ перезапуск и восстановление
def test_agent_resumes_after_crash_without_repeating_finished_steps(tmp_path, clock):
    r1 = FakeRouter()
    r1.crash_on_call = 3          # план (1) и шаг 1 (2) сделаны, на шаге 2 (3) процесс умирает
    w1, store, mem, files, _ = boot(tmp_path, r1)
    t = store.create("agent", {"goal": "ГЕЙМ", "chat_id": "c"})
    with pytest.raises(Crash):
        w1.run_once()
    row = store.get(t["id"])
    assert row["status"] == "running" and len(row["state"]["results"]) == 1   # прогресс сохранён в БД

    # «Render перезапущен»: новый процесс, те же БД и хранилище
    r2 = FakeRouter()
    w2, store2, mem2, _, _ = boot(tmp_path, r2)
    assert w2.run_once() is False                       # аренда ещё действует: чужую задачу не трогаем
    clock.advance(61)                                   # аренда истекла: процесс точно мёртв
    assert w2.recover()["requeued"] == [t["id"]]
    assert w2.run_once() is True
    done = store2.get(t["id"])
    assert done["status"] == "done" and done["attempts"] == 2
    assert r2.calls == [STEP_SYSTEM, FINAL_SYSTEM]       # план и шаг 1 НЕ запрашивались повторно
    assert [m["content"] for m in mem2.get_history("c")] == ["ИТОГ"]
    assert any("возвращена в очередь" in e["message"] for e in store2.events(t["id"]))


def test_crash_loop_task_is_failed_not_retried_forever(tmp_path, clock):
    w, store, *_ = boot(tmp_path, max_attempts=2)
    t = store.create("agent", {"goal": "g"})
    for _ in range(2):
        store.claim_next(60)                            # захват = попытка; процесс «умирает»
        clock.advance(61)
        res = store.requeue_expired(2)
    assert res["failed"] == [t["id"]]
    row = store.get(t["id"])
    assert row["status"] == "failed" and "прерывался" in row["error"]


def test_graceful_stop_requeues_with_state_and_refunds_attempt(tmp_path):
    started = threading.Event()

    @register_task("slow_test")
    def slow(ctx):
        ctx.state["saved"] = 1
        ctx.checkpoint()
        started.set()
        while True:
            ctx.check()
            time.sleep(0.01)

    w, store, *_ = boot(tmp_path)
    w.poll_s = 0.02
    t = store.create("slow_test", {})
    w.start()
    assert started.wait(3)
    w.stop()
    row = store.get(t["id"])
    assert row["status"] == "queued" and row["attempts"] == 0 and row["state"]["saved"] == 1


# --------------------------------------------------- GPU выключен
def test_tasks_wait_while_gpu_is_down_and_resume_without_losing_attempts(tmp_path, clock):
    r = FakeRouter(); r.down = True
    w, store, mem, files, _ = boot(tmp_path, r)
    t = store.create("agent", {"goal": "g", "chat_id": "c"})
    assert w.run_once() is True
    row = store.get(t["id"])
    assert row["status"] == "queued" and row["attempts"] == 0 and row["run_after"] > clock.t   # ждёт, а не падает
    assert w.run_once() is False                        # пока рано
    # GPU поднялся, но повтор откладывается: backoff растёт
    clock.advance(31)
    assert w.run_once() is True and store.get(t["id"])["state"]["_defers"] == 2
    assert store.get(t["id"])["run_after"] - clock.t == 60
    r.down = False
    clock.advance(61)
    assert w.run_once() is True
    done = store.get(t["id"])
    assert done["status"] == "done" and done["attempts"] == 1
    assert [m["content"] for m in mem.get_history("c")] == ["ИТОГ"]


def test_gpu_dies_midway_progress_kept(tmp_path, clock):
    r = FakeRouter()
    w, store, *_ = boot(tmp_path, r)
    t = store.create("agent", {"goal": "g"})
    orig = r.complete
    n = {"c": 0}

    def flaky(messages, **kw):
        n["c"] += 1
        if n["c"] == 3:
            r.down = True                               # GPU пропадает на втором шаге
        return orig(messages, **kw)
    r.complete = flaky
    w.run_once()
    row = store.get(t["id"])
    assert row["status"] == "queued" and len(row["state"]["results"]) == 1
    r.down = False; r.complete = orig; r.calls.clear()
    clock.advance(31)
    w.run_once()
    assert store.get(t["id"])["status"] == "done" and r.calls == [STEP_SYSTEM, FINAL_SYSTEM]


# ------------------------------------------------------ ошибки и повторы
def test_errors_retry_with_backoff_then_fail_keeping_state(tmp_path, clock):
    @register_task("boom_test")
    def boom(ctx):
        ctx.state["x"] = 1
        ctx.checkpoint()
        raise RuntimeError("сломалось")

    w, store, *_ = boot(tmp_path, max_attempts=3)
    t = store.create("boom_test", {})
    for expected in ("queued", "queued", "failed"):
        assert w.run_once() is True
        assert store.get(t["id"])["status"] == expected
        clock.advance(100)
    row = store.get(t["id"])
    assert "RuntimeError: сломалось" in row["error"] and row["attempts"] == 3 and row["state"]["x"] == 1
    assert store.retry(t["id"]) and store.get(t["id"])["attempts"] == 0     # ручной перезапуск


def test_permanent_error_fails_immediately(tmp_path):
    w, store, *_ = boot(tmp_path)
    t = store.create("agent", {"goal": "   "})
    w.run_once()
    row = store.get(t["id"])
    assert row["status"] == "failed" and row["attempts"] == 1 and "цель" in row["error"].lower()


def test_unknown_kind_fails_after_attempts(tmp_path, clock):
    w, store, *_ = boot(tmp_path, max_attempts=1)
    t = store.create("нет_такого", {})
    w.run_once()
    assert "Неизвестный тип" in store.get(t["id"])["error"] and store.get(t["id"])["status"] == "failed"


# ---------------------------------------------------------------- отмена
def test_cancel_queued_never_runs(tmp_path):
    w, store, *_ = boot(tmp_path)
    t = store.create("agent", {"goal": "g"})
    assert store.cancel(t["id"]) and not store.cancel(t["id"])
    assert w.run_once() is False and store.get(t["id"])["status"] == "cancelled"


def test_cancel_running_stops_handler_and_status_stays_cancelled(tmp_path):
    started = threading.Event()

    @register_task("loop_test")
    def loop(ctx):
        started.set()
        while True:
            time.sleep(0.01)
            ctx.checkpoint()

    w, store, *_ = boot(tmp_path)
    t = store.create("loop_test", {})
    th = threading.Thread(target=w.run_once); th.start()
    assert started.wait(3)
    store.cancel(t["id"])
    th.join(3)
    assert not th.is_alive() and store.get(t["id"])["status"] == "cancelled"


def test_heartbeat_extends_lease_and_detects_loss(tmp_path, clock):
    w, store, *_ = boot(tmp_path, lease=60)
    t = store.create("k", {})
    store.claim_next(60); w._current = t["id"]
    clock.advance(50)
    w.beat_once()
    assert store.get(t["id"])["lease_until"] == pytest.approx(clock.t + 60)
    store.cancel(t["id"])
    w.beat_once()
    assert w._lost.is_set()
    with pytest.raises(TaskCancelled):
        w.raise_if_stopping(t["id"])


# ---------------------------------------------------- идемпотентность
def test_chat_post_not_duplicated_when_crash_after_posting(tmp_path, clock):
    w, store, mem, files, r = boot(tmp_path)
    t = store.create("agent", {"goal": "g", "chat_id": "c"})
    orig = mem.add_message

    def add_then_die(*a, **k):
        orig(*a, **k)
        raise Crash()                                   # сообщение записано, а состояние сохранить не успели
    mem.add_message = add_then_die
    with pytest.raises(Crash):
        w.run_once()
    mem.add_message = orig
    clock.advance(61)
    w.recover(); w.run_once()
    assert [m["content"] for m in mem.get_history("c")] == ["ИТОГ"]         # ровно одно сообщение


def test_agent_saves_result_file_and_rejects_bad_path_early(tmp_path):
    w, store, mem, files, r = boot(tmp_path)
    ok = store.create("agent", {"goal": "g", "save_to": "docs/plan.md"})
    bad = store.create("agent", {"goal": "g", "save_to": "../evil.py"})
    w.run_once(); w.run_once()
    assert files.read_text("docs/plan.md") == "ИТОГ"
    assert store.get(bad["id"])["status"] == "failed" and r.calls.count(PLAN_SYSTEM) == 1   # плохой путь: модель не вызывалась


def test_generate_in_background_posts_reply(tmp_path):
    w, store, mem, files, r = boot(tmp_path)
    mem.add_message("c", "user", "придумай босса")
    store.create("generate", {"chat_id": "c"})
    w.run_once()
    assert [m["content"] for m in mem.get_history("c")] == ["придумай босса", "ответ в чат"]


# ---------------------------------------------------------- план и поток
def test_parse_plan_variants():
    assert parse_plan('Вот план: ["а", "б", "в"] удачи', 2, "g") == ["а", "б"]
    assert parse_plan("1) Сделать А\n2. Сделать Б\n- Сделать В", 5, "g") == ["Сделать А", "Сделать Б", "Сделать В"]
    assert parse_plan("???\n!!!", 3, "цель") == ["цель"]          # строки без букв не считаются шагами
    assert parse_plan("", 3, "цель") == ["цель"]
    assert parse_plan("[]", 3, "цель") == ["цель"]


def test_worker_thread_processes_tasks_end_to_end(tmp_path):
    w, store, mem, *_ = boot(tmp_path)
    w.poll_s = 0.02
    ids = [store.create("agent", {"goal": f"g{i}", "chat_id": f"c{i}"})["id"] for i in range(3)]
    w.start()
    deadline = time.time() + 5
    while time.time() < deadline and any(store.get(i)["status"] != "done" for i in ids):
        time.sleep(0.05)
    w.stop()
    assert [store.get(i)["status"] for i in ids] == ["done"] * 3
    assert store.counts() == {"done": 3}


def test_restart_on_startup_recovers_automatically(tmp_path, clock):
    """Полный путь: процесс умер -> новый процесс при старте (worker.start) сам возвращает задачу и доделывает."""
    r1 = FakeRouter(); r1.crash_on_call = 2
    w1, store, *_ = boot(tmp_path, r1)
    t = store.create("agent", {"goal": "g"})
    with pytest.raises(Crash):
        w1.run_once()
    clock.advance(61)
    w2, store2, *_ = boot(tmp_path)
    w2.poll_s = 0.02
    w2.start()                                          # start() сам вызывает recover()
    deadline = time.time() + 5
    while time.time() < deadline and store2.get(t["id"])["status"] != "done":
        time.sleep(0.05)
    w2.stop()
    assert store2.get(t["id"])["status"] == "done"
