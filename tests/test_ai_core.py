"""Тесты роутера моделей: настоящие HTTP-серверы на localhost имитируют GPU-узел и облачные API."""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ai_core import (
    ModelRouter, ProviderError, ProviderSpec, ProvidersUnavailable, clean_messages, load_specs,
    register_provider_type, trim_messages,
)


class Fake:
    """Имитация OpenAI-совместимого или Anthropic-сервера. behavior: ok | 500 | 401 | die | hang."""

    def __init__(self, behavior="ok", kind="openai", tokens=("При", "вет", "!")):
        self.behavior, self.kind, self.tokens = behavior, kind, tokens
        self.requests: list[dict] = []
        self.extra_headers: dict[str, str] = {}   # например x-ratelimit-*: как их отдаёт Groq
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200 if outer.behavior != "500" else 500)
                body = b'{"data":[]}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                b = outer.behavior
                if b == "hang":
                    threading.Event().wait(3)
                    return
                if b in ("500", "401", "429"):
                    msg = json.dumps({"error": {"message": "Incorrect API key provided: sk-SECRET123456"}}).encode()
                    self.send_response(int(b))
                    for k, v in outer.extra_headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(msg)))
                    self.end_headers()
                    self.wfile.write(msg)
                    return
                self.send_response(200)
                for k, v in outer.extra_headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def send(line):
                    data = line.encode()
                    self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()

                try:
                    self._stream(send, b)
                except (BrokenPipeError, ConnectionResetError):
                    pass   # клиент закрыл соединение раньше, чем мы дописали: это нормально

            def _stream(self, send, b):
                for i, t in enumerate(outer.tokens):
                    if outer.kind == "openai":
                        send("data: " + json.dumps({"choices": [{"delta": {"content": t}}]}) + "\n\n")
                    else:
                        send("event: content_block_delta\ndata: " + json.dumps(
                            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}}) + "\n\n")
                    if b == "die" and i == 1:
                        self.connection.close()   # обрыв посреди ответа
                        return
                send("data: [DONE]\n\n" if outer.kind == "openai" else "event: message_stop\ndata: {}\n\n")
                self.wfile.write(b"0\r\n\r\n")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def servers():
    made = []

    def make(*a, **k):
        f = Fake(*a, **k)
        made.append(f)
        return f

    yield make
    for f in made:
        try:
            f.stop()
        except Exception:
            pass


def spec(pid, url, type="openai_compatible", model="m1", key="", **kw):
    return ProviderSpec(id=pid, type=type, label=pid, base_url=url, api_key=key, model=model,
                        connect_timeout=1, read_timeout=kw.pop("read_timeout", 2), **kw)


STUB = ProviderSpec(id="stub", type="stub", label="stub")
Q = [{"role": "user", "content": "привет"}]


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def text(handle):
    return "".join(handle.chunks)


# -------------------------------------------------------------- базовое
def test_openai_stream_and_request_shape(servers):
    gpu = servers("ok")
    r = ModelRouter({"gpu": spec("gpu", gpu.url, key="k123456")}, ["gpu"], default_system="СИСТЕМА")
    h = r.open_stream([{"role": "assistant", "content": "лишнее"}, *Q, {"role": "user", "content": "ещё"}], temperature=0.3)
    assert text(h) == "Привет!" and h.provider == "gpu" and h.fallback_from == []
    req = gpu.requests[0]
    assert req["path"] == "/chat/completions" and req["headers"]["Authorization"] == "Bearer k123456"
    assert req["body"]["model"] == "m1" and req["body"]["temperature"] == 0.3 and req["body"]["stream"] is True
    assert req["body"]["messages"][0] == {"role": "system", "content": "СИСТЕМА"}
    assert req["body"]["messages"][1:] == [{"role": "user", "content": "привет\n\nещё"}]   # склеено, лишнее убрано


def test_anthropic_stream_and_system_separate(servers):
    a = servers("ok", kind="anthropic", tokens=("Hel", "lo"))
    r = ModelRouter({"c": spec("c", a.url, type="anthropic", model="claude-x", key="k123456")}, ["c"], "SYS")
    h = r.open_stream(Q, temperature=1.7)
    assert text(h) == "Hello"
    b = a.requests[0]
    assert b["path"] == "/v1/messages" and b["headers"]["x-api-key"] == "k123456"
    assert b["body"]["system"] == "SYS" and b["body"]["temperature"] == 1.0 and b["body"]["messages"] == Q


# ------------------------------------------------------- GPU выключен
def test_gpu_off_falls_back_to_next_provider(servers):
    gpu, cloud = servers("ok"), servers("ok", tokens=("облако",))
    gpu.stop()                                          # GPU отключён: соединение отклоняется
    r = ModelRouter({"gpu": spec("gpu", gpu.url), "cloud": spec("cloud", cloud.url)}, ["gpu", "cloud"])
    h = r.open_stream(Q)
    assert text(h) == "облако" and h.provider == "cloud" and h.fallback_from == ["gpu"]
    st = {s["id"]: s for s in r.status()}
    assert st["gpu"]["state"] == "down" and st["cloud"]["state"] == "ok"


def test_down_provider_is_skipped_during_cooldown_then_retried(servers):
    gpu, cloud = servers("500"), servers("ok")
    clock = Clock()
    r = ModelRouter({"gpu": spec("gpu", gpu.url), "cloud": spec("cloud", cloud.url)}, ["gpu", "cloud"], clock=clock)
    text(r.open_stream(Q))
    assert len(gpu.requests) == 1
    text(r.open_stream(Q))                              # пауза: GPU не дёргаем
    assert len(gpu.requests) == 1
    gpu.behavior = "ok"
    clock.t += 31                                       # пауза прошла: GPU вернулся
    h = r.open_stream(Q)
    assert h.provider == "gpu" and len(gpu.requests) == 2
    assert r.status()[0]["state"] == "ok"


def test_hanging_gpu_times_out_and_falls_back(servers):
    gpu, cloud = servers("hang"), servers("ok")
    r = ModelRouter({"gpu": spec("gpu", gpu.url, read_timeout=0.4), "cloud": spec("cloud", cloud.url)}, ["gpu", "cloud"])
    h = r.open_stream(Q)
    assert h.provider == "cloud" and "gpu" in h.fallback_from


def test_unreachable_host_fails_fast_by_connect_timeout():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()   # порт без слушателя
    r = ModelRouter({"gpu": spec("gpu", f"http://127.0.0.1:{port}"), "stub": STUB}, ["gpu", "stub"])
    h = r.open_stream(Q)
    assert h.provider == "stub" and h.fallback_from == ["gpu"]
    assert "gpu" in text(h)                             # заглушка называет, кто не ответил


def test_all_down_without_stub_raises_unavailable(servers):
    gpu = servers("500")
    r = ModelRouter({"gpu": spec("gpu", gpu.url)}, ["gpu"])
    with pytest.raises(ProvidersUnavailable) as e:
        r.open_stream(Q)
    assert "gpu" in e.value.errors
    with pytest.raises(ProvidersUnavailable):           # complete() для задач никогда не отвечает заглушкой
        ModelRouter({"gpu": spec("gpu", gpu.url), "stub": STUB}, ["gpu", "stub"]).complete(Q)


def test_midstream_failure_raises_after_partial_text(servers):
    gpu = servers("die", tokens=("а", "б", "в", "г"))
    r = ModelRouter({"gpu": spec("gpu", gpu.url)}, ["gpu"])
    h = r.open_stream(Q)
    got = []
    with pytest.raises(ProviderError):
        for c in h.chunks:
            got.append(c)
    assert got[:1] == ["а"]                              # часть ответа получена и может быть сохранена


# ------------------------------------------------------------ безопасность
def test_error_text_never_leaks_api_key(servers):
    bad = servers("401")
    key = "sk-SECRET123456"
    r = ModelRouter({"x": spec("x", bad.url, key=key), "stub": STUB}, ["x", "stub"])
    h = r.open_stream(Q)
    dump = json.dumps(r.status(), ensure_ascii=False) + text(h)
    assert key not in dump and "SECRET123456" in "sk-SECRET123456"   # ключ был в ответе сервера, но не в наших данных
    assert r.status()[0]["retry_in_s"] > 100             # неверный ключ: долгая пауза


def test_status_exposes_no_urls_or_keys(servers):
    gpu = servers("ok")
    r = ModelRouter({"gpu": spec("gpu", gpu.url, key="k-very-secret")}, ["gpu"])
    dump = json.dumps(r.status())
    assert "k-very-secret" not in dump and "127.0.0.1" not in dump


def test_client_cannot_choose_arbitrary_model(servers):
    gpu = servers("ok")
    r = ModelRouter({"gpu": spec("gpu", gpu.url, models=("m1", "m2"))}, ["gpu"])
    r.open_stream(Q, model="m2"); r.open_stream(Q, model="дорогая-модель"); r.open_stream(Q, model="default")
    assert [q["body"]["model"] for q in gpu.requests] == ["m2", "m1", "m1"]


def test_client_can_choose_provider_but_still_falls_back(servers):
    a, b = servers("ok", tokens=("A",)), servers("ok", tokens=("B",))
    r = ModelRouter({"a": spec("a", a.url), "b": spec("b", b.url)}, ["a", "b"])
    assert text(r.open_stream(Q, provider="b")) == "B"
    b.stop()
    h = r.open_stream(Q, provider="b")
    assert text(h) == "A" and h.fallback_from == ["b"]


# ----------------------------------------------------------- конфигурация
def test_load_specs_resolves_env_and_reports_reasons(tmp_path):
    cfg = tmp_path / "p.json"
    cfg.write_text(json.dumps({"default_chain": ["gpu", "or", "an", "bad", "stub", "ghost"], "providers": {
        "gpu": {"type": "openai_compatible", "base_url_env": "GB", "api_key_env": "GK", "model_env": "GM"},
        "or": {"type": "openai_compatible", "base_url": "https://o.example/v1", "api_key_env": "OK", "model_env": "OM"},
        "an": {"type": "anthropic", "api_key_env": "AK", "model": "claude-haiku-5-5"},
        "bad": {"type": "nonsense"}, "stub": {"type": "stub"}}}))
    specs, chain = load_specs(cfg, {"GB": "http://gpu:8000/v1", "GM": "qwen", "AK": "sk-ant"})
    assert chain == ["gpu", "or", "an", "bad", "stub"]            # несуществующий ghost отброшен
    assert specs["gpu"].configured and specs["gpu"].model == "qwen" and specs["gpu"].api_key == ""
    assert not specs["or"].configured and "OM" in specs["or"].reason
    assert specs["an"].configured and specs["an"].model == "claude-haiku-5-5"
    assert "nonsense" in specs["bad"].reason and specs["stub"].configured
    r = ModelRouter(specs, chain)
    assert [s["id"] for s in r.status() if s["state"] != "unconfigured"] == ["gpu", "an", "stub"]


def test_repo_providers_json_is_valid_and_has_no_secrets():
    import pathlib
    path = pathlib.Path(__file__).resolve().parent.parent / "config" / "providers.json"
    specs, chain = load_specs(path, {})
    assert chain[-1] == "stub" and specs["stub"].configured
    assert not specs["gpu"].configured and "GPU_BASE_URL" in specs["gpu"].reason
    assert not any(s.api_key for s in specs.values())
    assert "sk-" not in path.read_text()


def test_missing_config_falls_back_to_stub(tmp_path):
    specs, chain = load_specs(tmp_path / "нет.json", {})
    assert chain == ["stub"]
    assert "недоступна" in text(ModelRouter(specs, chain).open_stream(Q))


def test_custom_provider_type_can_be_registered():
    @register_provider_type("echo_test")
    def _echo(spec, messages, model, temperature, max_tokens, system):
        yield "эхо:" + messages[-1]["content"]
    r = ModelRouter({"e": ProviderSpec(id="e", type="echo_test")}, ["e"])
    assert text(r.open_stream(Q)) == "эхо:привет"


def test_deep_health_check(servers):
    gpu = servers("ok")
    r = ModelRouter({"gpu": spec("gpu", gpu.url)}, ["gpu"])
    assert r.check("gpu") is True
    gpu.stop()
    assert r.check("gpu") is False and r.status()[0]["state"] == "down"


# ------------------------------------------------------------- сообщения
def test_clean_and_trim_messages():
    m = clean_messages([{"role": "system", "content": "x"}, {"role": "assistant", "content": "a"},
                        {"role": "user", "content": " 1 "}, {"role": "user", "content": "2"},
                        {"role": "assistant", "content": ""}, {"role": "assistant", "content": "3"}])
    assert m == [{"role": "user", "content": "1\n\n2"}, {"role": "assistant", "content": "3"}]
    long = [{"role": "user", "content": "x" * 100}, {"role": "assistant", "content": "y" * 100}, {"role": "user", "content": "z" * 100}]
    t = trim_messages(long, 150)
    assert t == [{"role": "user", "content": "z" * 100}]
    assert trim_messages([{"role": "user", "content": "q" * 999}], 10)[0]["content"] == "q" * 999   # последнее не теряется


def test_empty_conversation_is_rejected():
    r = ModelRouter({"s": STUB}, ["s"])
    with pytest.raises(ProviderError):
        r.open_stream([{"role": "assistant", "content": "x"}])


def test_stub_names_missing_settings_when_nothing_is_configured():
    cfg = {"default_chain": ["gpu", "stub"], "providers": {
        "gpu": {"type": "openai_compatible", "base_url_env": "GPU_BASE_URL", "model_env": "GPU_MODEL", "api_key_env": "GPU_API_KEY"},
        "stub": {"type": "stub"}}}
    import tempfile, pathlib
    f = pathlib.Path(tempfile.mkdtemp()) / "p.json"; f.write_text(json.dumps(cfg))
    from ai_core import load_specs
    specs, chain = load_specs(f, env={})
    r = ModelRouter(specs, chain)
    h = r.open_stream(Q)
    msg = text(h)
    assert h.provider == "stub" and "GPU_BASE_URL" in msg and "Render" in msg
