"""Агент без GitHub (проект на сервере), вопросы агента к человеку и кнопка «Решай сам», предпросмотр и zip."""
import io
import json
import zipfile

import pytest

import agents
from fake_github import TOKEN, FakeGitHub
from github_client import GitHub
from projects import ProjectError, ProjectStore
from services import build_services
from tasks import TaskStore, TaskWorker
from test_api_http import Server, make_settings
from test_code_agent import FILES, Script, boot, call, final, Clock, Crash  # noqa: F401

HTML = "<!doctype html><html><body><canvas id=c></canvas><script src=\"game.js\"></script><link rel=stylesheet href=\"css/s.css\"></body></html>"


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


def boot_p(tmp_path, router, gh=None):
    w, store, mem = boot(tmp_path, router, gh)
    w.deps["projects"] = ProjectStore(store.db)
    return w, store, mem, w.deps["projects"]


def run(tmp_path, replies, payload=None, chat="chat-1", gh=None):
    r = Script(*replies)
    w, store, mem, proj = boot_p(tmp_path, r, gh)
    t = store.create("code", {"goal": "создай html игру", "mode": "project", **(payload or {})}, chat_id=chat)
    w.run_once()
    return r, w, store, mem, proj, t["id"]


# ----------------------------------------------------------------- проект без GitHub
def test_agent_creates_project_files_without_github(tmp_path):
    r, w, store, mem, proj, tid = run(tmp_path, [
        call("write", path="index.html", content=HTML),
        call("write", path="game.js", content="const c = 1;\n"),
        call("append", path="game.js", content="const d = 2;\n"),
        call("write", path="css/s.css", content="body{margin:0}"),
        call("save", title="Игра"),
        final("Сделал игру: открой index.html."),
    ])
    t = store.get(tid)
    assert t["status"] == "done", t.get("error")                          # GitHub не подключён, но задача выполнена
    assert proj.files("chat-1") == {"index.html": HTML, "game.js": "const c = 1;\nconst d = 2;\n", "css/s.css": "body{margin:0}"}
    res = t["result"]
    assert res["mode"] == "project" and res["project"] == "chat-1" and res["pr"] is None
    assert res["saved"]["files"] == ["css/s.css", "game.js", "index.html"]
    msgs = mem.get_history("chat-1")
    assert [m["role"] for m in msgs] == ["user", "assistant"] and "Проект сохранён на сервере" in msgs[1]["content"]
    assert "Репозиторий" not in r.seen[0][0]["content"] and "Проект пока пустой" in r.seen[0][0]["content"]
    assert "save" in r.systems[0] and "pull request" not in r.systems[0].split("Правила")[0].split("- save")[0].lower().replace("gh_", "")


def test_second_task_in_same_chat_edits_the_same_project(tmp_path):
    r, w, store, mem, proj, tid = run(tmp_path, [call("write", path="index.html", content="<h1>v1</h1>"), call("save", title="v1"), final("ок")])
    r2 = Script(call("read", path="index.html"), call("replace", path="index.html", old="v1", new="v2"), call("save", title="v2"), final("исправил"))
    w2, store2, mem2, proj2 = boot_p(tmp_path, r2)
    t2 = store2.create("code", {"goal": "поменяй на v2", "mode": "project"}, chat_id="chat-1")
    w2.run_once()
    assert store2.get(t2["id"])["status"] == "done" and proj2.files("chat-1") == {"index.html": "<h1>v2</h1>"}
    assert "index.html" in r2.seen[0][0]["content"] and "<h1>v1</h1>" in r2.seen[1][-1]["content"]      # агент видит прежние файлы


def test_project_changes_are_saved_even_if_model_forgets_save(tmp_path):
    r, w, store, mem, proj, tid = run(tmp_path, [call("write", path="a.html", content="<p>x</p>"), final("готово"), final("точно готово")])
    assert store.get(tid)["status"] == "done" and proj.files("chat-1") == {"a.html": "<p>x</p>"}


def test_project_mode_keeps_all_protection_rules(tmp_path):
    secret = "const k = 'gsk_" + "A" * 30 + "';"
    r, w, store, mem, proj, tid = run(tmp_path, [
        call("write", path="../evil.html", content="x"), call("write", path="a/.git/x", content="x"),
        call("write", path="k.js", content=secret), call("write", path="big.txt", content="x" * 200_001), final("не вышло")])
    assert all("Отказ" in m[-1]["content"] for m in r.seen[1:5])
    assert proj.files("chat-1") == {} and store.get(tid)["result"]["saved"] is None


def test_project_name_comes_from_payload_and_must_be_valid(tmp_path):
    r, w, store, mem, proj, tid = run(tmp_path, [call("write", path="a.txt", content="1"), call("save"), final("ок")], payload={"project": "my-game"})
    assert proj.files("my-game") == {"a.txt": "1"}
    r, w, store, mem, proj, tid2 = run(tmp_path, [], payload={"project": "../etc"}, chat=None)
    assert store.get(tid2)["status"] == "failed" and "имя проекта" in store.get(tid2)["error"]


def test_old_tool_names_still_work_and_unknown_mode_is_refused(tmp_path):
    r, w, store, mem, proj, tid = run(tmp_path, [call("gh_write", path="a.txt", content="1"), call("gh_pr", title="t"), final("ок")])
    assert store.get(tid)["status"] == "done" and proj.files("chat-1") == {"a.txt": "1"}
    r, w, store, mem, proj, tid = run(tmp_path, [], payload={"mode": "ftp"})
    assert store.get(tid)["status"] == "failed" and "mode" in store.get(tid)["error"]


def test_github_error_message_points_to_project_mode(tmp_path):
    r = Script()
    w, store, mem = boot(tmp_path, r, None)
    t = store.create("code", {"goal": "g"})
    w.run_once()
    assert "режим «Проект»" in store.get(t["id"])["error"]


# ----------------------------------------------------------------- хранилище, zip, предпросмотр
def test_project_store_limits_and_atomic_apply(tmp_path):
    from db import Database
    db = Database(tmp_path / "p.db"); db.migrate()
    ps = ProjectStore(db)
    ps.apply("p1", {"a.txt": "1", "b.txt": "2"})
    with pytest.raises(ProjectError):
        ps.apply("p1", {"c.txt": "3", "huge.txt": "x" * 200_001})          # всё или ничего
    assert ps.files("p1") == {"a.txt": "1", "b.txt": "2"}
    with pytest.raises(ProjectError):
        ps.apply("p1", {f"f{i}.txt": "1" for i in range(201)})
    with pytest.raises(ProjectError):
        ps.apply("p1", {f"m{i}.txt": "x" * 190_000 for i in range(16)})   # >3 МБ
    assert ps.apply("p1", {"a.txt": None, "b.txt": "22"}) == {"written": 1, "deleted": 1}
    assert ps.files("p1") == {"b.txt": "22"} and ps.get("p1", "b.txt") == "22" and ps.get("p1", "nope") is None
    assert ps.apply("p2", {"x.txt": "1"}) and {p["project"] for p in ps.projects()} == {"p1", "p2"}
    with pytest.raises(ProjectError):
        ps.files("../x")
    assert ps.delete_project("p1") == 1 and ps.files("p1") == {}


def test_zip_and_preview_inline_local_css_js(tmp_path):
    from db import Database
    db = Database(tmp_path / "p.db"); db.migrate()
    ps = ProjectStore(db)
    ps.apply("g", {"index.html": HTML, "game.js": "if (a < b) { x = '</script>'; }", "css/s.css": "body{color:red}", "readme.txt": "hi"})
    z = zipfile.ZipFile(io.BytesIO(ps.zip_bytes("g")))
    assert sorted(z.namelist()) == ["css/s.css", "game.js", "index.html", "readme.txt"] and z.read("css/s.css") == b"body{color:red}"
    html = ps.preview_html("g")
    assert "<style>" in html and "color:red" in html and 'src="game.js"' not in html and "x = '<\\/script>'" in html
    assert html.count("</script>") == 1                                       # скрипт игры не может закрыть тег раньше времени
    ps.apply("g2", {"page.html": '<script src="https://cdn.example/x.js"></script><script src="missing.js"></script>'})
    assert ps.preview_html("g2") == '<script src="https://cdn.example/x.js"></script><script src="missing.js"></script>'
    assert ps.preview_html("empty") is None
    ps.apply("g3", {"sub/index.html": '<script src="a.js"></script>', "sub/a.js": "go()"})
    assert "go()" in ps.preview_html("g3") and ps.preview_html("g3", "sub/index.html") == ps.preview_html("g3")


# ----------------------------------------------------------------- вопросы агента
def waiting(tmp_path, replies, payload=None, chat="c-q"):
    r, w, store, mem, proj, tid = run(tmp_path, replies, payload=payload, chat=chat)
    return r, w, store, mem, proj, tid


def test_agent_asks_waits_without_burning_attempts_and_continues_after_answer(tmp_path):
    q = call("ask", question="Один файл или отдельные html/js/css?", options=["Один index.html", "Раздельные файлы"])
    r, w, store, mem, proj, tid = waiting(tmp_path, [q])
    t = store.get(tid)
    assert t["status"] == "waiting" and t["attempts"] == 0 and w.run_once() is False        # ждёт человека, воркер её не трогает
    assert t["state"]["question"] == {"id": 1, "text": "Один файл или отдельные html/js/css?", "options": ["Один index.html", "Раздельные файлы"]}
    assert any("Вопрос:" in e["message"] for e in store.events(tid)) and len(r.seen) == 1

    assert store.answer(tid, {"text": "Один index.html", "auto": False}, qid=1) == "ok"
    r.replies += [call("write", path="index.html", content="<p>game</p>"), call("save"), final("Готово: один файл")]
    assert w.run_once() is True
    t = store.get(tid)
    assert t["status"] == "done", t.get("error")
    # модель получила ответ человека и роли чередуются
    sent = r.seen[1]
    assert "ОТВЕТ ПОЛЬЗОВАТЕЛЯ" in sent[-1]["content"] and "Один index.html" in sent[-1]["content"]
    roles = [m["role"] for m in sent]
    assert roles[0] == "user" and all(a != b for a, b in zip(roles, roles[1:]))
    assert proj.files("c-q") == {"index.html": "<p>game</p>"} and "question" not in t["state"]


def test_dont_ask_button_sets_auto_and_later_questions_are_answered_by_the_agent_itself(tmp_path):
    r, w, store, mem, proj, tid = waiting(tmp_path, [call("ask", question="Жанр?", options=["Платформер", "Гонки"])])
    assert store.answer(tid, {"text": "решай сам", "auto": True}, qid=1) == "ok"
    r.replies += [call("ask", question="Цвет?", options=["Красный"]), call("write", path="i.html", content="x"), call("save"), final("сам выбрал")]
    w.run_once()
    t = store.get(tid)
    assert t["status"] == "done" and t["state"]["auto"] is True and t["state"]["asked"] == 1
    assert "Больше не спрашивай" in r.seen[1][-1]["content"]
    assert "Спрашивать нельзя" in r.seen[2][-1]["content"]                 # второй вопрос не дошёл до человека


def test_at_most_three_questions_per_task(tmp_path):
    r, w, store, mem, proj, tid = waiting(tmp_path, [call("ask", question="q1")])
    for i in (1, 2, 3):
        assert store.get(tid)["status"] == "waiting"
        store.answer(tid, {"text": f"a{i}", "auto": False}, qid=i)
        r.replies += [call("ask", question=f"q{i + 1}")] if i < 3 else [call("ask", question="q4"), final("всё")]
        w.run_once()
    t = store.get(tid)
    assert t["status"] == "done" and t["state"]["asked"] == 3          # четвёртый вопрос человеку не дошёл
    assert "Спрашивать нельзя" in r.seen[4][-1]["content"] and "question" not in t["state"]


def test_ask_disabled_by_payload_is_never_waiting_and_not_in_prompt(tmp_path):
    r, w, store, mem, proj, tid = run(tmp_path, [call("ask", question="Что?"), final("решил сам")], payload={"ask": False})
    assert store.get(tid)["status"] == "done" and "Неизвестный инструмент" in r.seen[1][-1]["content"]
    assert "ask" not in r.systems[0].replace("task", "") .split("Правила")[0] and "ТОЛЬКО если" not in r.systems[0]


def test_answer_validation_stale_and_not_waiting(tmp_path):
    r, w, store, mem, proj, tid = waiting(tmp_path, [call("ask", question="q?")])
    assert store.answer(tid, {"text": "x", "auto": False}, qid=7) == "stale"
    assert store.get(tid)["status"] == "waiting"
    assert store.answer(tid, {"text": "x", "auto": False}, qid=1) == "ok"
    assert store.answer(tid, {"text": "y", "auto": False}, qid=1) == "not_waiting"     # повторный ответ не принимается
    assert store.answer("нет-такой", {"text": "x"}) == "not_waiting"


def test_waiting_task_can_be_cancelled(tmp_path):
    r, w, store, mem, proj, tid = waiting(tmp_path, [call("ask", question="q?")])
    assert store.cancel(tid) and store.get(tid)["status"] == "cancelled"
    assert store.answer(tid, {"text": "x"}, 1) == "not_waiting"


def test_restart_while_waiting_keeps_question_and_resumes_after_answer(tmp_path, clock):
    r, w, store, mem, proj, tid = waiting(tmp_path, [call("write", path="a.txt", content="1"), call("ask", question="Продолжать?", options=["Да"])])
    assert store.get(tid)["status"] == "waiting"
    clock.advance(1000)                                                  # «перезапуск Render»: новый процесс, та же БД
    r2 = Script(call("save"), final("ок"))
    w2, store2, mem2, proj2 = boot_p(tmp_path, r2)
    assert w2.recover() == {"requeued": [], "failed": []} and store2.get(tid)["status"] == "waiting"
    store2.answer(tid, {"text": "Да", "auto": False}, qid=1)
    w2.run_once()
    assert store2.get(tid)["status"] == "done" and proj2.files("c-q") == {"a.txt": "1"}


def test_question_text_and_options_are_clipped_and_cleaned(tmp_path):
    r, w, store, mem, proj, tid = waiting(tmp_path, [call("ask", question="Q" * 500, options=["  ", "а" * 200, 5, "b", "c", "d", "e"])])
    q = store.get(tid)["state"]["question"]
    assert len(q["text"]) == 300 and len(q["options"]) == 4 and len(q["options"][0]) == 80
    r, w, store, mem, proj, tid = waiting(tmp_path, [call("ask", question="  "), final("без вопроса")], chat="c-2")
    assert store.get(tid)["status"] == "done" and "Нужен текст вопроса" in r.seen[1][-1]["content"]


# ----------------------------------------------------------------- пауза до сброса лимита токенов
@pytest.mark.parametrize("wait,noisy", [(2.5, False), (7.0, True)])
def test_agent_step_waits_for_minute_limit_reset(tmp_path, monkeypatch, wait, noisy):
    sleeps = []
    monkeypatch.setattr(agents.time, "sleep", lambda s: sleeps.append(s))

    class Paced(Script):
        def __init__(self, *a):
            super().__init__(*a)
            self.waits = [wait, 0]

        def tokens_wait(self, need):
            self.need = need
            return self.waits.pop(0) if self.waits else 0

    r = Paced(final("ок"))
    w, store, mem, proj = boot_p(tmp_path, r)
    t = store.create("code", {"goal": "g", "mode": "project"})
    w.run_once()
    assert store.get(t["id"])["status"] == "done"
    assert abs(sum(sleeps) - wait) < 1e-6                              # подождал ровно до сброса, а не ушёл в 429
    assert r.need > 3000                                               # в расчёт взят и max_tokens ответа
    logged = any("минутного лимита" in e["message"] for e in store.events(t["id"]))
    assert logged is noisy                                             # про короткую паузу не шумим, про длинную говорим


# ----------------------------------------------------------------- через настоящий HTTP
def test_http_project_agent_question_answer_preview_zip(tmp_path):
    script = Script(call("ask", question="Один файл или несколько?", options=["Один", "Несколько"]),
                    call("write", path="index.html", content="<script src=\"g.js\"></script>"), call("write", path="g.js", content="go()"),
                    call("save", title="t"), final("Готово"))
    s = Server(make_settings(tmp_path, {}), lambda st: build_services(st, router=script))
    import time

    def wait(pred, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(0.05)
        raise AssertionError("не дождались")

    try:
        _, chat = 200, "chat-http"
        code, task = s.j("POST", "/api/tasks", {"kind": "code", "chat_id": chat, "payload": {"goal": "создай html", "mode": "project", "chat_id": chat}})
        assert code == 200
        t = wait(lambda: (lambda x: x if x["status"] == "waiting" else None)(s.j("GET", f"/api/tasks/{task['id']}")[1]))
        assert t["state"]["question"]["options"] == ["Один", "Несколько"]
        assert set(t["state"]) <= {"question", "mode", "project", "pr", "saved"}                  # внутренняя история наружу не уходит
        assert "history" not in json.dumps(t) and "changes" not in json.dumps(t["state"])
        assert s.j("POST", f"/api/tasks/{task['id']}/answer", {"text": ""})[0] == 400
        assert s.j("POST", f"/api/tasks/{task['id']}/answer", {"text": "Один", "qid": 9})[0] == 409
        assert s.j("POST", f"/api/tasks/{task['id']}/answer", {"auto": True, "qid": 1}, token=None)[0] == 401
        assert s.j("POST", f"/api/tasks/{task['id']}/answer", {"auto": True, "qid": 1})[0] == 200
        assert s.j("POST", f"/api/tasks/{task['id']}/answer", {"auto": True, "qid": 1})[0] == 409
        done = wait(lambda: (lambda x: x if x["status"] in ("done", "failed") else None)(s.j("GET", f"/api/tasks/{task['id']}")[1]))
        assert done["status"] == "done", done.get("error")
        assert done["result"]["project"] == chat and "ОТВЕТ" not in json.dumps(done["events"])

        assert s.j("GET", "/api/projects")[1][0]["project"] == chat
        assert [f["path"] for f in s.j("GET", f"/api/projects/{chat}/files")[1]] == ["g.js", "index.html"]
        st, h, body = s.call("GET", f"/api/projects/{chat}/file?path=g.js")
        assert st == 200 and body == b"go()" and h["content-type"].startswith("text/plain") and h["x-content-type-options"] == "nosniff"
        assert s.call("GET", f"/api/projects/{chat}/file?path=nope.js")[0] == 404
        assert s.call("GET", f"/api/projects/{chat}/file?path=../x")[0] == 400
        st, h, body = s.call("GET", f"/api/projects/{chat}/preview")
        assert st == 200 and b"go()" in body and h["content-type"].startswith("text/plain")      # не text/html: на нашем домене не исполняется
        st, h, body = s.call("GET", f"/api/projects/{chat}/zip")
        assert st == 200 and h["content-type"] == "application/zip" and "attachment" in h["content-disposition"]
        assert sorted(zipfile.ZipFile(io.BytesIO(body)).namelist()) == ["g.js", "index.html"]
        assert s.call("GET", "/api/projects/bad.name/files")[0] == 400
        assert s.call("GET", f"/api/projects/{chat}/zip", token=None)[0] == 401
        assert s.call("GET", "/api/projects/nothing/zip")[0] == 404
        assert s.j("DELETE", f"/api/projects/{chat}")[1] == {"deleted": 2} and s.j("GET", f"/api/projects/{chat}/files")[1] == []
        # итог и запрос человека сохранились в чате на сервере
        msgs = s.j("GET", f"/api/chats/{chat}")[1]["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant"]
    finally:
        s.stop()
