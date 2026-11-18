"""Лимиты провайдера (из заголовков x-ratelimit-*), пауза до сброса и учёт расхода по дням."""
import time

import pytest

from ai_core import ModelRouter, ProvidersUnavailable, parse_duration
from db import Database
from test_ai_core import Q, STUB, servers, spec, text  # noqa: F401  (servers — фикстура)
from test_api_http import Server, make_settings
from services import build_services
from test_code_agent import Script
from usage import Usage, day_of, next_midnight

GROQ = {"x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "993", "x-ratelimit-reset-requests": "2m59.56s",
        "x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "7500", "x-ratelimit-reset-tokens": "7.66s"}


@pytest.mark.parametrize("raw,sec", [("2m59.56s", 179.56), ("7.66s", 7.66), ("45", 45.0), ("1h2m3s", 3723.0), ("250ms", 0.25), ("1m", 60.0),
                                     ("", None), (None, None), ("скоро", None), ("5x", None)])
def test_parse_duration(raw, sec):
    got = parse_duration(raw)
    assert (got is None and sec is None) or abs(got - sec) < 1e-6


def test_router_remembers_limits_from_response_headers(servers):
    gpu = servers()
    gpu.extra_headers = dict(GROQ)
    r = ModelRouter({"gpu": spec("gpu", gpu.url, key="k123456")}, ["gpu"])
    assert r.limits()[0]["data"] is False                                   # пока запросов не было
    assert text(r.open_stream(Q)) == "Привет!"
    g = r.limits()[0]
    assert g["id"] == "gpu" and g["data"] is True
    assert g["requests"]["limit"] == 1000 and g["requests"]["remaining"] == 993 and 175 <= g["requests"]["reset_in_s"] <= 180
    assert g["tokens"]["limit"] == 8000 and g["tokens"]["remaining"] == 7500 and 5 <= g["tokens"]["reset_in_s"] <= 8
    assert "k123456" not in str(r.limits())


def test_limits_are_read_even_from_a_429_and_retry_after_is_kept(servers):
    gpu = servers("429")
    gpu.extra_headers = {**GROQ, "x-ratelimit-remaining-tokens": "0", "retry-after": "12"}
    r = ModelRouter({"gpu": spec("gpu", gpu.url)}, ["gpu"])
    with pytest.raises(ProvidersUnavailable):
        r.open_stream(Q, allow_stub=False)
    g = r.limits()[0]
    assert g["tokens"]["remaining"] == 0 and 10 <= g["retry_in_s"] <= 12


def test_provider_without_ratelimit_headers_shows_no_data_and_stub_is_hidden(servers):
    gpu = servers()
    r = ModelRouter({"gpu": spec("gpu", gpu.url), "stub": STUB}, ["gpu", "stub"])
    text(r.open_stream(Q))
    out = r.limits()
    assert [x["id"] for x in out] == ["gpu"] and out[0]["data"] is False


def test_tokens_wait_only_when_the_minute_budget_is_too_small(servers):
    gpu = servers()
    gpu.extra_headers = {**GROQ, "x-ratelimit-remaining-tokens": "1000", "x-ratelimit-reset-tokens": "20s"}
    r = ModelRouter({"gpu": spec("gpu", gpu.url)}, ["gpu"])
    assert r.tokens_wait(5000) == 0                                        # данных ещё нет: не ждём
    text(r.open_stream(Q))
    assert 18 <= r.tokens_wait(5000) <= 20                                 # на запрос не хватит: ждём сброса
    assert r.tokens_wait(500) == 0                                         # хватит: не ждём
    gpu.extra_headers = {**GROQ, "x-ratelimit-remaining-tokens": "10", "x-ratelimit-reset-tokens": "500s"}
    text(r.open_stream(Q))
    assert r.tokens_wait(5000) == 65.0                                     # ожидание ограничено минутой


def test_usage_counts_requests_and_chars_per_day(servers, tmp_path):
    db = Database(tmp_path / "u.db")
    db.migrate()
    now = [1_800_000_000.0]
    usage = Usage(db, clock=lambda: now[0])
    gpu = servers()
    r = ModelRouter({"gpu": spec("gpu", gpu.url)}, ["gpu"])
    r.on_usage = usage.record
    text(r.open_stream(Q))
    text(r.open_stream(Q))
    sm = usage.summary()
    assert sm["requests"] == 2 and sm["by_provider"][0]["provider"] == "gpu" and sm["approx_tokens"] > 0
    assert sm["day"] == day_of(now[0]) and 0 < sm["reset_in_s"] <= 86400
    assert sm["reset_in_s"] == round(next_midnight(now[0]) - now[0])
    now[0] += 86400                                                         # наступили новые сутки: счётчик дня с нуля
    assert usage.summary()["requests"] == 0
    assert [d["requests"] for d in usage.summary()["last_days"]] == [2]
    usage.record("gpu", 1, 300, 600)
    assert usage.summary()["requests"] == 1 and usage.summary()["approx_tokens"] == 300


def test_http_limits_endpoint(servers, tmp_path):
    gpu = servers()
    gpu.extra_headers = dict(GROQ)
    s = Server(make_settings(tmp_path, {"gpu": gpu.url}))
    try:
        code, out = s.j("GET", "/api/limits")
        assert code == 200 and out["providers"][0]["data"] is False and out["usage"]["requests"] == 0
        assert s.call("POST", "/api/chat", {"messages": [{"role": "user", "content": "привет"}], "chat_id": "c1"})[0] == 200
        time.sleep(0.3)
        code, out = s.j("GET", "/api/limits")
        p = out["providers"][0]
        assert p["tokens"]["remaining"] == 7500 and p["requests"]["limit"] == 1000
        assert out["usage"]["requests"] == 1 and out["usage"]["by_provider"][0]["provider"] == "gpu"
        assert s.j("GET", "/api/limits", token=None)[0] == 401
    finally:
        s.stop()
