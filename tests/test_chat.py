import pytest

from ai_core import ModelRouter, ProviderError, ProviderSpec, ProvidersUnavailable, register_provider_type
from chat import ChatService
from db import Database
from memory import Memory


@register_provider_type("scripted")
def _scripted(spec, messages, model, temperature, max_tokens, system):
    plan = spec.base_url            # сценарий передаём через base_url: "ok", "die", "empty", "down"
    if plan == "down":
        raise ProviderError("недоступен")
    if plan == "empty":
        return
    yield "Один "
    yield "два "
    if plan == "die":
        raise ProviderError("соединение оборвано")
    yield "три"


def svc(tmp_path, *plans, clock=None):
    db = Database(tmp_path / "c.db"); db.migrate()
    specs = {f"p{i}": ProviderSpec(id=f"p{i}", type="scripted", base_url=p, model="m") for i, p in enumerate(plans)}
    router = ModelRouter(specs, list(specs))
    kw = {"clock": clock} if clock else {}
    return ChatService(Memory(db), router, **kw), router.specs, Memory(db)


H = [{"role": "user", "content": "придумай босса для игры"}]


def contents(mem, cid="c"):
    return [(m["role"], m["content"], m["status"]) for m in mem.get_history(cid)]


def test_full_reply_is_streamed_and_saved(tmp_path):
    chat, _, mem = svc(tmp_path, "ok")
    r = chat.start_reply("c", H)
    assert r.provider == "p0" and r.fallback_from == []
    assert [m[0] for m in contents(mem)] == ["user"]                  # до первого куска ответа в БД ничего лишнего
    it = r.chunks
    assert next(it) == "Один "
    assert contents(mem)[-1] == ("assistant", "", "streaming")       # пока идёт, ответ уже виден в БД
    assert "Один " + "".join(it) == "Один два три"
    assert contents(mem) == [("user", "придумай босса для игры", "done"), ("assistant", "Один два три", "done")]
    assert mem.get_chat("c")["title"] == "придумай босса для игры"


def test_user_message_saved_even_when_all_models_down(tmp_path):
    chat, _, mem = svc(tmp_path, "down")
    with pytest.raises(ProvidersUnavailable):
        chat.start_reply("c", H)
    assert contents(mem) == [("user", "придумай босса для игры", "done")]   # вопрос не потерян


def test_midstream_failure_saves_partial_as_interrupted(tmp_path):
    chat, _, mem = svc(tmp_path, "die")
    r = chat.start_reply("c", H)
    got = []
    with pytest.raises(ProviderError):
        for c in r.chunks:
            got.append(c)
    assert got == ["Один ", "два "]
    assert contents(mem)[-1] == ("assistant", "Один два ", "interrupted")


def test_client_disconnect_saves_partial(tmp_path):
    chat, _, mem = svc(tmp_path, "ok")
    r = chat.start_reply("c", H)
    it = r.chunks
    next(it)
    it.close()                                          # вкладку закрыли: генератор закрывается
    assert contents(mem)[-1] == ("assistant", "Один ", "interrupted")


def test_disconnect_before_first_chunk_leaves_no_empty_message(tmp_path):
    chat, _, mem = svc(tmp_path, "ok")
    r = chat.start_reply("c", H)
    r.chunks.close()
    assert [m[0] for m in contents(mem)] == ["user"]


def test_empty_model_reply_not_stored(tmp_path):
    chat, _, mem = svc(tmp_path, "empty")
    assert "".join(chat.start_reply("c", H).chunks) == ""
    assert [m[0] for m in contents(mem)] == ["user"]


def test_conversation_continues_and_regenerates_without_duplicates(tmp_path):
    chat, _, mem = svc(tmp_path, "ok")
    "".join(chat.start_reply("c", H).chunks)
    h2 = H + [{"role": "assistant", "content": "Один два три"}, {"role": "user", "content": "ещё"}]
    "".join(chat.start_reply("c", h2).chunks)
    assert len(contents(mem)) == 4
    "".join(chat.start_reply("c", h2).chunks)           # «перегенерировать»: тот же вопрос
    assert len(contents(mem)) == 4 and contents(mem)[-1][2] == "done"


def test_regenerate_after_interrupted_replaces_partial(tmp_path):
    chat, specs, mem = svc(tmp_path, "die")
    r = chat.start_reply("c", H)
    with pytest.raises(ProviderError):
        list(r.chunks)
    specs["p0"].base_url = "ok"                         # GPU вернулся
    chat.router._down_until.clear()
    assert "".join(chat.start_reply("c", H).chunks) == "Один два три"
    assert contents(mem)[1:] == [("assistant", "Один два три", "done")]


def test_history_missing_on_server_is_restored_from_client(tmp_path):
    chat, _, mem = svc(tmp_path, "ok")
    long = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}, {"role": "user", "content": "q2"}]
    "".join(chat.start_reply("new", long).chunks)       # сервер этого чата не знал (например, БД была пустой)
    assert [m[:2] for m in contents(mem, "new")] == [("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "Один два три")]


def test_periodic_flush_keeps_progress_if_process_dies(tmp_path):
    t = {"v": 0.0}
    chat, _, mem = svc(tmp_path, "ok", clock=lambda: t["v"])
    r = chat.start_reply("c", H)
    it = r.chunks
    next(it); t["v"] += 2                               # прошло больше интервала сохранения
    next(it)                                            # здесь сработает промежуточное сохранение
    row = mem.get_history("c")[-1]
    assert row["status"] == "streaming" and row["content"] == "Один "   # «процесс убит» тут: в БД уже есть часть ответа
    assert mem.mark_interrupted(0) == 1                  # после рестарта помечается как прерванный
    assert mem.get_history("c")[-1]["status"] == "interrupted"


def test_rejects_conversation_without_user_question(tmp_path):
    chat, _, _ = svc(tmp_path, "ok")
    for bad in ([], [{"role": "assistant", "content": "x"}], [{"role": "user", "content": "   "}]):
        with pytest.raises(ValueError):
            chat.start_reply("c", bad)
