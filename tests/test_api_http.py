"""Сквозные тесты API по настоящему HTTP: uvicorn + реальные запросы + подставные GPU-узлы.

Работают и с настоящим FastAPI, и с тестовой заменой из tests/_shim (если fastapi не установлен).
"""
import dataclasses
import http.client
import io
import json
import sqlite3
import threading
import time
import zipfile
from urllib.parse import quote

import pytest
import uvicorn

from main import create_app
from services import build_services
from settings import Settings
from storage import R2Storage
from test_ai_core import Fake
from test_storage import FakeS3

TOKEN = "t" * 40
ORIGIN = "https://me.github.io"


class Server:
    def __init__(self, settings, factory=None):
        app = create_app(settings, factory) if factory else create_app(settings)
        self.srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="on"))
        self.thread = threading.Thread(target=self.srv.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 15
        while not self.srv.started and time.time() < deadline:
            time.sleep(0.02)
        assert self.srv.started, "сервер не запустился"
        self.port = self.srv.servers[0].sockets[0].getsockname()[1]
        self.app = app

    def stop(self):
        self.srv.should_exit = True
        self.thread.join(15)

    def call(self, method, path, body=None, token=TOKEN, headers=None, raw=None):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request(method, quote(path, safe="/?=&%:"), body=data, headers=h)
        r = conn.getresponse()
        payload = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        conn.close()
        return r.status, hdrs, payload

    def j(self, method, path, body=None, **kw):
        st, h, p = self.call(method, path, body, **kw)
        try:
            return st, json.loads(p)
        except json.JSONDecodeError:
            return st, p


def make_settings(tmp_path, gpu_urls, *, chain=None, token=TOKEN, name="data", **env):
    providers = {pid: {"type": "openai_compatible", "base_url": url, "model": f"m-{pid}", "connect_timeout": 1, "read_timeout": 3}
                 for pid, url in gpu_urls.items()}
    pf = tmp_path / f"providers-{name}.json"
    pf.write_text(json.dumps({"default_chain": chain or list(gpu_urls), "providers": providers}))
    e = {"DATA_DIR": str(tmp_path / name), "API_TOKEN": token, "PROVIDERS_FILE": str(pf),
         "SYSTEM_PROMPT_FILE": str(tmp_path / "нет.txt"), "CORS_ORIGINS": ORIGIN, **env}
    return Settings.from_env(e)


@pytest.fixture
def world(tmp_path):
    """Два подставных GPU-узла (основной и резервный) + сервер. Возвращает (server, gpu, backup)."""
    gpu, backup = Fake("ok", tokens=("GPU:", " ответ")), Fake("ok", tokens=("РЕЗЕРВ",))
    s = Server(make_settings(tmp_path, {"gpu": gpu.url, "backup": backup.url}))
    yield s, gpu, backup
    s.stop()
    for f in (gpu, backup):
        try:
            f.stop()
        except Exception:
            pass


def wait_for(fn, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("не дождались условия")


# ---------------------------------------------------------------- доступ
def test_health_is_public_but_everything_else_needs_token(world):
    s, *_ = world
    assert s.j("GET", "/api/health", token=None)[1]["ok"] is True
    for path in ("/api/status", "/api/chats", "/api/files", "/api/tasks", "/api/export", "/api/settings"):
        assert s.call("GET", path, token=None)[0] == 401
        assert s.call("GET", path, token="x" * 40)[0] == 401
        assert s.call("GET", path)[0] == 200
    assert s.call("POST", "/api/chat", {"chat_id": "a", "messages": [{"role": "user", "content": "q"}]}, token=None)[0] == 401
    assert "Bearer" not in json.dumps(s.j("GET", "/api/chats", token="bad")[1])


def test_server_without_api_token_refuses_all_api_calls(tmp_path):
    s = Server(make_settings(tmp_path, {}, token=""))
    try:
        assert s.call("GET", "/api/health", token=None)[0] == 200
        st, body = s.j("GET", "/api/chats", token="whatever")
        assert st == 503 and "API_TOKEN" in body["detail"]
    finally:
        s.stop()


def test_cors_allows_only_configured_origin_and_exposes_provider_headers(world):
    s, *_ = world
    st, h, _ = s.call("OPTIONS", "/api/chat", token=None, headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"})
    assert st == 200 and h["access-control-allow-origin"] == ORIGIN
    _, h2, _ = s.call("OPTIONS", "/api/chat", token=None, headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in h2
    st, h3, _ = s.call("POST", "/api/chat", {"chat_id": "cors", "messages": [{"role": "user", "content": "q"}]}, headers={"Origin": ORIGIN})
    assert "x-provider" in h3["access-control-expose-headers"].lower()


# ------------------------------------------------------------------- чат
def test_chat_streams_saves_and_lists(world):
    s, gpu, backup = world
    st, h, body = s.call("POST", "/api/chat", {"chat_id": "c1", "messages": [{"role": "user", "content": "привет"}]})
    assert st == 200 and body.decode() == "GPU: ответ" and h["x-provider"] == "gpu" and h["x-model"] == "m-gpu" and h["x-fallback"] == ""
    assert s.j("GET", "/api/chats")[1][0]["title"] == "привет"
    st, chat = s.j("GET", "/api/chats/c1")
    assert [(m["role"], m["content"], m["status"]) for m in chat["messages"]] == [("user", "привет", "done"), ("assistant", "GPU: ответ", "done")]
    assert s.j("GET", "/api/chats/нет")[0] == 404
    # продолжение: клиент присылает всю историю, дублей на сервере нет
    s.call("POST", "/api/chat", {"chat_id": "c1", "messages": [
        {"role": "user", "content": "привет"}, {"role": "assistant", "content": "GPU: ответ"}, {"role": "user", "content": "ещё"}]})
    assert len(s.j("GET", "/api/chats/c1")[1]["messages"]) == 4


def test_gpu_off_switches_to_backup_and_tells_the_client(world):
    s, gpu, backup = world
    gpu.stop()
    st, h, body = s.call("POST", "/api/chat", {"chat_id": "c2", "messages": [{"role": "user", "content": "привет"}]})
    assert st == 200 and body.decode() == "РЕЗЕРВ" and h["x-provider"] == "backup" and h["x-fallback"] == "gpu"
    names = {m["id"]: m for m in s.j("GET", "/api/status")[1]["models"]}
    assert names["gpu"]["state"] == "down" and names["backup"]["state"] == "ok"


def test_all_models_down_returns_503_but_question_is_saved(world):
    s, gpu, backup = world
    gpu.stop(); backup.stop()
    st, body = s.j("POST", "/api/chat", {"chat_id": "c3", "messages": [{"role": "user", "content": "важный вопрос"}]})
    assert st == 503 and set(body["providers"]) == {"gpu", "backup"}
    assert [m["content"] for m in s.j("GET", "/api/chats/c3")[1]["messages"]] == ["важный вопрос"]


def test_chat_validation_errors(world):
    s, *_ = world
    assert s.call("POST", "/api/chat", {"chat_id": "bad id!", "messages": [{"role": "user", "content": "q"}]})[0] in (400, 422)
    assert s.call("POST", "/api/chat", {"chat_id": "ok", "messages": []})[0] in (400, 422)
    assert s.call("POST", "/api/chat", {"chat_id": "ok", "messages": [{"role": "assistant", "content": "q"}]})[0] == 400


def test_rename_and_delete_chat(world):
    s, *_ = world
    s.call("POST", "/api/chat", {"chat_id": "c4", "messages": [{"role": "user", "content": "q"}]})
    assert s.j("PATCH", "/api/chats/c4", {"title": "Мой чат"})[0] == 200
    assert s.j("GET", "/api/chats")[1][0]["title"] == "Мой чат"
    assert s.j("DELETE", "/api/chats/c4")[1] == {"deleted": True}
    assert s.j("GET", "/api/chats/c4")[0] == 404


# ------------------------------------------------------------ настройки
def test_settings_roundtrip_and_secret_names_rejected(world):
    s, *_ = world
    assert s.j("PUT", "/api/settings/theme", {"value": {"mode": "dark"}})[0] == 200
    assert s.j("GET", "/api/settings")[1] == {"theme": {"mode": "dark"}}
    for bad in ("api_key", "my-token", "PASSWORD", "client_secret"):
        st, body = s.j("PUT", f"/api/settings/{bad}", {"value": "x"})
        assert st == 400 and "переменных окружения" in body["detail"]
    assert s.j("PUT", "/api/settings/big", {"value": "x" * 30000})[0] == 413
    assert s.j("DELETE", "/api/settings/theme")[1] == {"deleted": True}


# ---------------------------------------------------------------- файлы
def test_files_full_cycle_over_http(world):
    s, *_ = world
    st, _, _ = s.call("PUT", "/api/files/game/main.js", raw="console.log('привет')".encode())
    assert st == 200
    st, meta = s.j("PUT", "/api/files/game/hero.png", raw=bytes(range(256)))
    assert meta["size"] == 256 and len(meta["sha256"]) == 64
    assert [f["path"] for f in s.j("GET", "/api/files")[1]] == ["game/hero.png", "game/main.js"]
    assert s.j("GET", "/api/files?prefix=game")[1][0]["path"] == "game/hero.png"
    assert s.j("GET", "/api/files/game/main.js")[1]["content"] == "console.log('привет')"
    st, h, body = s.call("GET", "/api/files/game/hero.png")                  # бинарный: сразу скачивание
    assert body == bytes(range(256)) and "attachment" in h["content-disposition"] and h["x-content-type-options"] == "nosniff"
    st, h, _ = s.call("GET", "/api/files/game/main.js?raw=1")
    assert "attachment" in h["content-disposition"]
    assert s.j("DELETE", "/api/files/game/main.js")[1] == {"deleted": "game/main.js"}
    assert s.j("GET", "/api/files/game/main.js")[0] == 404


def test_file_security_limits_over_http(tmp_path):
    base = make_settings(tmp_path, {})
    s = Server(dataclasses.replace(base, max_file_bytes=100))
    try:
        assert s.j("PUT", "/api/files/..%2Fevil.py", raw=b"x")[0] == 400
        assert s.j("PUT", "/api/files/a/..%2F..%2Fevil.py", raw=b"x")[0] == 400
        assert s.j("PUT", "/api/files/run.exe", raw=b"x")[0] == 415
        assert s.j("PUT", "/api/files/archives/x.zip", raw=b"x")[0] == 400
        assert s.j("PUT", "/api/files/big.txt", raw=b"x" * 101)[0] == 413
        assert s.j("PUT", "/api/files/ok.txt", raw=b"x" * 100)[0] == 200
        assert not (tmp_path / "evil.py").exists()
    finally:
        s.stop()


def test_archives_and_reindex_over_http(world):
    s, *_ = world
    s.call("PUT", "/api/files/p/a.py", raw=b"print(1)")
    s.call("PUT", "/api/files/p/b.md", raw=b"# b")
    st, arch = s.j("POST", "/api/archives", {"prefix": "p", "name": "мой проект"})
    assert st == 200 and arch["name"].endswith(".zip")
    assert [a["name"] for a in s.j("GET", "/api/archives")[1]] == [arch["name"]]
    st, h, data = s.call("GET", f"/api/archives/{arch['name']}")
    assert h["content-type"] == "application/zip"
    assert sorted(zipfile.ZipFile(io.BytesIO(data)).namelist()) == ["p/a.py", "p/b.md"]
    assert s.j("POST", "/api/archives", {"prefix": "нет"})[0] == 404
    assert s.j("POST", "/api/reindex")[1] == {"added": 0, "removed": 0}


def test_r2_outage_gives_503_not_crash(tmp_path):
    fake = FakeS3()
    settings = make_settings(tmp_path, {})
    s = Server(settings, lambda st: build_services(st, storage=R2Storage("https://x", "k", "s", "b", client=fake)))
    try:
        assert s.j("PUT", "/api/files/a.txt", raw=b"1")[0] == 200
        fake.down = True
        st, body = s.j("PUT", "/api/files/b.txt", raw=b"2")
        assert st == 503 and "Хранилище" in body["detail"]
        assert s.j("GET", "/api/chats")[0] == 200                              # остальное API работает
        fake.down = False
        assert s.j("PUT", "/api/files/b.txt", raw=b"2")[0] == 200
    finally:
        s.stop()


# ----------------------------------------------------- состояние и задачи
def test_status_reports_persistence_warnings_and_deep_check(world):
    s, gpu, backup = world
    st, body = s.j("GET", "/api/status?deep=1")
    assert body["database"] == {"kind": "sqlite", "persistent": False, "ok": True}
    assert body["storage"]["kind"] == "local" and body["storage"]["persistent"] is False
    assert body["models_alive"] == {"gpu": True, "backup": True}
    assert any("DATABASE_URL" in w for w in body["warnings"]) and any("R2" in w for w in body["warnings"])
    gpu.stop()
    assert s.j("GET", "/api/status?deep=1")[1]["models_alive"]["gpu"] is False
    dump = json.dumps(s.j("GET", "/api/status")[1])
    assert "127.0.0.1" not in dump and TOKEN not in dump                         # адреса GPU и токен не раскрываются


def test_agent_task_runs_in_background_and_posts_to_chat(tmp_path):
    gpu = Fake("ok", tokens=('["Шаг один"]',))
    s = Server(make_settings(tmp_path, {"gpu": gpu.url}))
    try:
        assert s.j("POST", "/api/tasks", {"kind": "нет"})[0] == 400
        st, task = s.j("POST", "/api/tasks", {"kind": "agent", "payload": {"goal": "придумай уровень", "save_to": "docs/level.md"}, "chat_id": "t1"})
        assert st == 200 and task["status"] in ("queued", "running")
        done = wait_for(lambda: (lambda t: t if t["status"] == "done" else None)(s.j("GET", f"/api/tasks/{task['id']}")[1]))
        assert done["progress"] == 1 and done["result"]["steps"] == 1
        assert any("Готово" == e["message"] for e in done["events"])
        assert s.j("GET", "/api/files/docs/level.md")[1]["content"] == '["Шаг один"]'
        assert s.j("GET", "/api/chats/t1")[1]["messages"][-1]["role"] == "assistant"
        assert s.j("GET", "/api/tasks?status=done")[1][0]["id"] == task["id"]
        assert s.j("POST", f"/api/tasks/{task['id']}/cancel")[1] == {"cancelled": False}     # уже завершена
    finally:
        s.stop(); gpu.stop()


def test_task_with_missing_goal_fails_fast_and_can_be_retried(tmp_path):
    gpu = Fake("ok", tokens=("x",))
    s = Server(make_settings(tmp_path, {"gpu": gpu.url}))
    try:
        _, task = s.j("POST", "/api/tasks", {"kind": "agent", "payload": {"goal": " "}})
        t = wait_for(lambda: (lambda t: t if t["status"] == "failed" else None)(s.j("GET", f"/api/tasks/{task['id']}")[1]))
        assert t["attempts"] == 1 and "цель" in t["error"].lower()
        assert s.j("POST", f"/api/tasks/{task['id']}/retry")[1] == {"retried": True}
    finally:
        s.stop(); gpu.stop()


# ------------------------------------------ главное: перезапуск и выключенный GPU
def test_data_survives_server_restart(tmp_path):
    gpu = Fake("ok", tokens=("ответ",))
    settings = make_settings(tmp_path, {"gpu": gpu.url})
    s1 = Server(settings)
    s1.call("POST", "/api/chat", {"chat_id": "keep", "messages": [{"role": "user", "content": "запомни меня"}]})
    s1.call("PUT", "/api/files/keep/notes.md", raw="заметки".encode())
    s1.j("PUT", "/api/settings/lang", {"value": "ru"})
    s1.stop()                                                                    # «Render перезапущен»
    s2 = Server(settings)
    try:
        assert [m["content"] for m in s2.j("GET", "/api/chats/keep")[1]["messages"]] == ["запомни меня", "ответ"]
        assert s2.j("GET", "/api/files/keep/notes.md")[1]["content"] == "заметки"
        assert s2.j("GET", "/api/settings")[1] == {"lang": "ru"}
    finally:
        s2.stop(); gpu.stop()


def test_task_waits_through_gpu_outage_and_restart_then_completes(tmp_path):
    dead = Fake("ok"); dead.stop()                                               # GPU выключен
    s1 = Server(make_settings(tmp_path, {"gpu": dead.url}))
    _, task = s1.j("POST", "/api/tasks", {"kind": "agent", "payload": {"goal": "собери прототип"}, "chat_id": "outage"})
    tid = task["id"]
    t = wait_for(lambda: (lambda t: t if any("недоступны" in e["message"] for e in t["events"]) else None)(s1.j("GET", f"/api/tasks/{tid}")[1]))
    assert t["status"] == "queued" and t["attempts"] == 0 and t["run_after"] > time.time()     # ждёт, попытка не потрачена
    s1.stop()                                                                    # и сервер перезапускается, пока GPU выключен

    con = sqlite3.connect(tmp_path / "data" / "gdai.db")                         # проходит время ожидания
    con.execute("UPDATE tasks SET run_after = 0"); con.commit(); con.close()

    gpu = Fake("ok", tokens=('["Шаг"]',))                                        # GPU снова в сети (адрес мог измениться)
    s2 = Server(make_settings(tmp_path, {"gpu": gpu.url}, name="data"))
    try:
        done = wait_for(lambda: (lambda t: t if t["status"] == "done" else None)(s2.j("GET", f"/api/tasks/{tid}")[1]))
        assert done["attempts"] == 1
        assert s2.j("GET", "/api/chats/outage")[1]["messages"][-1]["role"] == "assistant"
    finally:
        s2.stop(); gpu.stop()


def test_backup_and_restore_after_database_loss(tmp_path):
    gpu = Fake("ok", tokens=("ответ",))
    s1 = Server(make_settings(tmp_path, {"gpu": gpu.url}, name="d1"))
    shared = FakeS3()
    # R2 общий между «старой» и «новой» БД
    factory = lambda st: build_services(st, storage=R2Storage("https://x", "k", "s", "b", client=shared))
    s1.stop()
    a = Server(make_settings(tmp_path, {"gpu": gpu.url}, name="d1"), factory)
    try:
        a.call("POST", "/api/chat", {"chat_id": "bk", "messages": [{"role": "user", "content": "важное"}]})
        a.call("PUT", "/api/files/x/a.md", raw=b"file")
        st, b = a.j("POST", "/api/backups")
        assert st == 200 and b["chats"] == 1 and b["key"].startswith("backups/")
        assert a.j("GET", "/api/backups")[1][0]["key"] == b["key"]
    finally:
        a.stop()
    new = Server(make_settings(tmp_path, {"gpu": gpu.url}, name="d2-empty"), factory)    # БД потеряна: пустая база, тот же R2
    try:
        assert new.j("GET", "/api/chats")[1] == [] and new.j("GET", "/api/files")[1] == []
        assert new.j("POST", "/api/reindex")[1] == {"added": 1, "removed": 0}
        assert new.j("POST", "/api/backups/restore", {"key": b["key"]})[1] == {"chats": 1, "messages": 2}
        assert [m["content"] for m in new.j("GET", "/api/chats/bk")[1]["messages"]] == ["важное", "ответ"]
        assert new.j("GET", "/api/files/x/a.md")[1]["content"] == "file"
        assert new.j("POST", "/api/backups/restore", {"key": "../secret"})[0] == 400
    finally:
        new.stop(); gpu.stop()


def test_cors_works_by_default_for_github_pages_and_ignores_paths_in_config(tmp_path):
    base = make_settings(tmp_path, {}, CORS_ORIGINS="")
    assert "https://dobroe345-cyber.github.io" in base.cors_origins
    fixed = Settings.from_env({"CORS_ORIGINS": " https://a.github.io/gamedev-ai/ , https://B.example "})
    assert fixed.cors_origins == ("https://a.github.io", "https://b.example")
