"""ИИ-ядро: набор провайдеров моделей и роутер с запасными вариантами.

Провайдеры описаны в config/providers.json без секретов (только имена переменных окружения).
Если GPU-узел выключен или недоступен, роутер сам переходит к следующему провайдеру цепочки.
Все вызовы синхронные (стандартная библиотека): Starlette/FastAPI гоняет их в пуле потоков.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import ssl
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit

Message = dict  # {"role": "user"|"assistant", "content": str}

DEFAULT_COOLDOWN = 30.0      # пауза после сбоя провайдера
AUTH_COOLDOWN = 300.0        # неверный ключ: не долбим сервис
MAX_CONTEXT_CHARS = 24_000


class ProviderError(RuntimeError):
    """Один провайдер не смог ответить (роутер переходит к следующему)."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class ProvidersUnavailable(RuntimeError):
    """Ни один провайдер сейчас не отвечает."""

    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        detail = "; ".join(f"{k}: {v}" for k, v in errors.items()) or "нет настроенных провайдеров"
        super().__init__(f"Модели недоступны ({detail})")


# ---------------------------------------------------------------- сообщения
def clean_messages(messages: list[Message]) -> list[Message]:
    """Только user/assistant, без пустых, роли чередуются, первой идёт реплика пользователя."""
    out: list[Message] = []
    for m in messages:
        text = str(m.get("content") or "").strip()
        role = m.get("role")
        if not text or role not in ("user", "assistant"):
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + text
        else:
            out.append({"role": role, "content": text})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def trim_messages(messages: list[Message], max_chars: int = MAX_CONTEXT_CHARS) -> list[Message]:
    """Оставляет самые свежие сообщения в пределах бюджета; последнее сообщение не теряется никогда."""
    kept: list[Message] = []
    total = 0
    for m in reversed(messages):
        total += len(m["content"])
        if kept and total > max_chars:
            break
        kept.append(m)
    kept.reverse()
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    return kept


# ----------------------------------------------------------------- HTTP
def _connect(url: str, connect_timeout: float, read_timeout: float) -> tuple[http.client.HTTPConnection, str]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ProviderError("Некорректный адрес провайдера")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if parts.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            parts.hostname, port, timeout=connect_timeout, context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(parts.hostname, port, timeout=connect_timeout)
    try:
        conn.connect()
        conn.sock.settimeout(read_timeout)  # connect_timeout — на соединение, read_timeout — на ожидание данных
    except (OSError, ssl.SSLError) as e:
        conn.close()
        raise ProviderError(f"нет соединения ({type(e).__name__})") from e
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return conn, path


_local = threading.local()   # получатель заголовков ответа: ставится роутером на время запроса к провайдеру


def _note_headers(resp: http.client.HTTPResponse) -> None:
    sink = getattr(_local, "sink", None)
    if sink:
        try:
            sink(resp)
        except Exception:   # учёт лимитов никогда не должен ломать ответ модели
            pass


_DUR = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")


def parse_duration(text: str | None) -> float | None:
    """'2m59.56s' -> 179.56; '7.66s' -> 7.66; '45' -> 45.0; нераспознанное -> None."""
    if not text:
        return None
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        pass
    parts = _DUR.findall(text)
    if not parts or "".join(a + b for a, b in parts) != text:
        return None
    mult = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
    return sum(float(a) * mult[b] for a, b in parts)


def _int_header(v: str | None) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except ValueError:
        return None


def _error_text(resp: http.client.HTTPResponse) -> str:
    try:
        raw = resp.read(2000).decode("utf-8", "replace")
        try:
            j = json.loads(raw)
            raw = j.get("error", {}).get("message") if isinstance(j.get("error"), dict) else (j.get("error") or j.get("message") or raw)
        except (json.JSONDecodeError, AttributeError):
            pass
        return str(raw)[:200]
    except Exception:
        return ""


def http_lines(url: str, headers: dict, body: dict | None, connect_timeout: float, read_timeout: float) -> Iterator[str]:
    """POST (или GET без тела) и построчное чтение ответа."""
    conn, path = _connect(url, connect_timeout, read_timeout)
    try:
        try:
            payload = json.dumps(body).encode() if body is not None else None
            conn.request("POST" if body is not None else "GET", path, body=payload,
                         headers={"Content-Type": "application/json", "Accept": "text/event-stream, application/json", **headers})
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            raise ProviderError(f"ошибка запроса ({type(e).__name__})") from e
        _note_headers(resp)
        if resp.status >= 400:
            raise ProviderError(f"HTTP {resp.status} {_error_text(resp)}".strip(), resp.status)
        while True:
            try:
                line = resp.readline()
            except (OSError, http.client.HTTPException) as e:
                raise ProviderError(f"соединение оборвано ({type(e).__name__})") from e
            if not line:
                return
            yield line.decode("utf-8", "replace").rstrip("\r\n")
    finally:
        conn.close()


def _sse_data(lines: Iterator[str]) -> Iterator[str]:
    for line in lines:
        if line.startswith("data:"):
            data = line[5:].strip()
            if data:
                yield data


# ------------------------------------------------------------- провайдеры
@dataclass
class ProviderSpec:
    id: str
    type: str
    label: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    models: tuple[str, ...] = ()
    connect_timeout: float = 8.0
    read_timeout: float = 90.0
    reason: str = ""  # почему не настроен (без секретов)

    @property
    def configured(self) -> bool:
        return not self.reason


ProviderFn = Callable[["ProviderSpec", list[Message], str, float | None, int | None, str], Iterator[str]]
_TYPES: dict[str, ProviderFn] = {}


def register_provider_type(name: str):
    """Добавляет новый тип провайдера: @register_provider_type("мой_тип")."""
    def deco(fn: ProviderFn) -> ProviderFn:
        _TYPES[name] = fn
        return fn
    return deco


@register_provider_type("openai_compatible")
def _openai(spec, messages, model, temperature, max_tokens, system):
    msgs = ([{"role": "system", "content": system}] if system else []) + messages
    body: dict[str, Any] = {"model": model, "messages": msgs, "stream": True}
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens:
        body["max_tokens"] = max_tokens
    headers = {"Authorization": f"Bearer {spec.api_key}"} if spec.api_key else {}
    url = spec.base_url.rstrip("/") + "/chat/completions"
    for data in _sse_data(http_lines(url, headers, body, spec.connect_timeout, spec.read_timeout)):
        if data == "[DONE]":
            return
        try:
            ev = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(ev.get("error"), dict):
            raise ProviderError("ошибка потока: " + str(ev["error"].get("message", ""))[:200])
        delta = ((ev.get("choices") or [{}])[0].get("delta") or {}).get("content")
        if delta:
            yield delta


@register_provider_type("anthropic")
def _anthropic(spec, messages, model, temperature, max_tokens, system):
    body: dict[str, Any] = {"model": model, "max_tokens": max_tokens or 4096, "stream": True, "messages": messages}
    if system:
        body["system"] = system
    if temperature is not None:
        body["temperature"] = min(temperature, 1.0)
    headers = {"x-api-key": spec.api_key, "anthropic-version": "2023-06-01"}
    url = (spec.base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
    for data in _sse_data(http_lines(url, headers, body, spec.connect_timeout, spec.read_timeout)):
        try:
            ev = json.loads(data)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
            yield ev["delta"]["text"]
        elif ev.get("type") == "error":
            raise ProviderError("ошибка потока: " + str((ev.get("error") or {}).get("message", ""))[:200])


@register_provider_type("stub")
def _stub(spec, messages, model, temperature, max_tokens, system):
    last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    text = f"ИИ-модель сейчас недоступна, поэтому отвечает заглушка. Твоё сообщение сохранено: «{last[:200]}»."
    for word in text.split(" "):
        yield word + " "


# ------------------------------------------------------------ конфигурация
def load_specs(path: str | Path, env: Mapping[str, str] | None = None) -> tuple[dict[str, ProviderSpec], list[str]]:
    env = os.environ if env is None else env
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cfg = {"default_chain": ["stub"], "providers": {"stub": {"type": "stub", "label": "Заглушка"}}}
    specs: dict[str, ProviderSpec] = {}
    for pid, raw in (cfg.get("providers") or {}).items():
        def val(name: str, default: str = "") -> str:
            return (env.get(raw.get(f"{name}_env", ""), "").strip() if raw.get(f"{name}_env") else "") or str(raw.get(name, default))
        spec = ProviderSpec(
            id=pid, type=raw.get("type", ""), label=raw.get("label", pid),
            base_url=val("base_url"), api_key=val("api_key"), model=val("model"),
            models=tuple(raw.get("models") or ()),
            connect_timeout=float(raw.get("connect_timeout", 8)), read_timeout=float(raw.get("read_timeout", 90)),
        )
        if spec.type not in _TYPES:
            spec.reason = f"неизвестный тип «{spec.type}»"
        elif spec.type == "openai_compatible":
            if not spec.base_url.startswith(("http://", "https://")):
                spec.reason = f"не задан {raw.get('base_url_env', 'base_url')}"
            elif not spec.model:
                spec.reason = f"не задана модель ({raw.get('model_env', 'model')})"
        elif spec.type == "anthropic":
            if not spec.api_key:
                spec.reason = f"не задан {raw.get('api_key_env', 'api_key')}"
            elif not spec.model:
                spec.reason = "не задана модель"
        specs[pid] = spec
    chain = [p for p in cfg.get("default_chain", list(specs)) if p in specs]
    return specs, chain


# --------------------------------------------------------------------- роутер
@dataclass
class StreamHandle:
    provider: str
    model: str
    chunks: Iterator[str]
    fallback_from: list[str] = field(default_factory=list)  # кто не ответил до выбранного


class ModelRouter:
    def __init__(
        self, specs: dict[str, ProviderSpec], chain: list[str], default_system: str = "",
        *, clock: Callable[[], float] = time.monotonic, max_context_chars: int = MAX_CONTEXT_CHARS,
    ) -> None:
        self.specs, self.chain, self.default_system = specs, chain, default_system.strip()
        self._clock = clock
        self.max_context_chars = max_context_chars
        self._lock = threading.Lock()
        self._down_until: dict[str, float] = {}
        self._last_error: dict[str, str] = {}
        self._limits: dict[str, dict] = {}
        self.on_usage: Callable[[str, int, int, int], None] | None = None   # (провайдер, запросов, симв. на входе, симв. на выходе)

    @classmethod
    def from_settings(cls, settings, env: Mapping[str, str] | None = None) -> "ModelRouter":
        specs, chain = load_specs(settings.providers_file, env)
        sys_text = ""
        if Path(settings.system_prompt_file).is_file():
            sys_text = Path(settings.system_prompt_file).read_text(encoding="utf-8")
        return cls(specs, chain, sys_text)

    # ------------------------------------------------------------ состояние
    def _is_down(self, pid: str) -> bool:
        with self._lock:
            return self._down_until.get(pid, 0) > self._clock()

    def _mark_down(self, pid: str, error: ProviderError) -> None:
        cool = AUTH_COOLDOWN if error.status in (401, 403) else DEFAULT_COOLDOWN
        text = str(error)
        key = self.specs[pid].api_key
        if len(key) >= 6:  # некоторые сервисы возвращают ключ в тексте ошибки
            text = text.replace(key, "***")
        with self._lock:
            self._down_until[pid] = self._clock() + cool
            self._last_error[pid] = text

    def _mark_up(self, pid: str) -> None:
        with self._lock:
            self._down_until.pop(pid, None)
            self._last_error.pop(pid, None)

    def status(self) -> list[dict]:
        """Публичное состояние провайдеров (адреса и ключи не раскрываются)."""
        now = self._clock()
        out = []
        for pid in dict.fromkeys(self.chain + list(self.specs)):
            s = self.specs[pid]
            with self._lock:
                down = self._down_until.get(pid, 0) - now
                err = self._last_error.get(pid, "")
            out.append({
                "id": pid, "label": s.label, "type": s.type, "configured": s.configured,
                "reason": s.reason, "model": s.model, "in_chain": pid in self.chain,
                "state": "unconfigured" if not s.configured else ("down" if down > 0 else "ok"),
                "retry_in_s": max(0, round(down)), "last_error": err,
            })
        return out

    # -------------------------------------------------------------- лимиты
    def _record_limits(self, pid: str, resp) -> None:
        """Запоминает заголовки x-ratelimit-* (OpenAI-совместимый формат, их отдаёт Groq): сколько осталось и когда сброс."""
        get = lambda n: resp.getheader(n)
        now = time.time()
        info: dict[str, Any] = {"seen_at": now}
        for kind in ("requests", "tokens"):
            lim, rem = _int_header(get(f"x-ratelimit-limit-{kind}")), _int_header(get(f"x-ratelimit-remaining-{kind}"))
            reset = parse_duration(get(f"x-ratelimit-reset-{kind}"))
            if lim is not None or rem is not None:
                info[kind] = {"limit": lim, "remaining": rem, "reset_at": None if reset is None else now + reset}
        retry = parse_duration(get("retry-after"))
        if retry is not None and resp.status == 429:
            info["retry_at"] = now + retry
        if len(info) > 1:
            with self._lock:
                self._limits[pid] = info

    def tokens_wait(self, need_tokens: float) -> float:
        """Сколько секунд стоит подождать до сброса минутного лимита токенов, чтобы запрос на ~need_tokens не упёрся в 429.
        0, если лимит неизвестен, достаточен или уже сброшен."""
        now = time.time()
        for pid in self.chain:
            s = self.specs.get(pid)
            if not s or not s.configured or s.type == "stub" or self._is_down(pid):
                continue
            with self._lock:
                k = (self._limits.get(pid) or {}).get("tokens")
            if k and k["remaining"] is not None and k["reset_at"] and k["remaining"] < need_tokens:
                return max(0.0, min(65.0, k["reset_at"] - now))
            return 0.0
        return 0.0

    def limits(self) -> list[dict]:
        """Что провайдер сообщил о лимитах в последнем ответе (пусто, пока не было ни одного запроса)."""
        now = time.time()
        out = []
        for pid in dict.fromkeys(self.chain + list(self.specs)):
            s = self.specs[pid]
            if s.type == "stub" or not s.configured:
                continue
            with self._lock:
                info = dict(self._limits.get(pid) or {})
            item: dict[str, Any] = {"id": pid, "label": s.label or pid, "model": s.model, "data": bool(info)}
            if info:
                item["seen_ago_s"] = max(0, round(now - info["seen_at"]))
                for kind in ("requests", "tokens"):
                    k = info.get(kind)
                    if k:
                        reset_in = None if k["reset_at"] is None else max(0, round(k["reset_at"] - now))
                        # если время сброса уже прошло, лимит восстановился: остаток точно неизвестен, но не меньше прежнего
                        item[kind] = {"limit": k["limit"], "remaining": k["remaining"], "reset_in_s": reset_in,
                                      "reset_passed": k["reset_at"] is not None and k["reset_at"] < now}
                if info.get("retry_at") and info["retry_at"] > now:
                    item["retry_in_s"] = round(info["retry_at"] - now)
            out.append(item)
        return out

    def check(self, pid: str, timeout: float = 4.0) -> bool:
        """Активная проверка (для /api/health?deep=1): GET /models у OpenAI-совместимых узлов."""
        s = self.specs.get(pid)
        if not s or not s.configured:
            return False
        if s.type != "openai_compatible":
            return True
        try:
            headers = {"Authorization": f"Bearer {s.api_key}"} if s.api_key else {}
            for _ in http_lines(s.base_url.rstrip("/") + "/models", headers, None, min(timeout, s.connect_timeout), timeout):
                break
            self._mark_up(pid)
            return True
        except ProviderError as e:
            self._mark_down(pid, e)
            return False

    # -------------------------------------------------------------- запросы
    def _prepare(self, messages: list[Message], system: str | None) -> tuple[list[Message], str]:
        msgs = trim_messages(clean_messages(messages), self.max_context_chars)
        return msgs, (system or "").strip() or self.default_system

    def _model_for(self, spec: ProviderSpec, requested: str | None) -> str:
        if requested and requested != "default" and requested in spec.models:
            return requested  # чужие названия моделей не принимаем: расходы задаёт конфигурация
        return spec.model

    def open_stream(
        self, messages: list[Message], *, system: str | None = None, provider: str | None = None,
        model: str | None = None, temperature: float | None = None, max_tokens: int | None = None,
        allow_stub: bool = True,
    ) -> StreamHandle:
        msgs, sys_text = self._prepare(messages, system)
        if not msgs:
            raise ProviderError("Нет сообщения пользователя")
        order = list(dict.fromkeys(([provider] if provider in self.specs else []) + self.chain))
        order = [p for p in order if self.specs[p].configured and (allow_stub or self.specs[p].type != "stub")]
        errors: dict[str, str] = {}
        live = [p for p in order if not self._is_down(p)]
        for p in live or order:  # недавно упавших пропускаем; если других нет, всё же пробуем
            spec = self.specs[p]
            if spec.type == "stub" and (errors or self._unconfigured()):  # заглушка объясняет, что именно не так
                self._mark_up(p)
                return StreamHandle(p, "", _stub_notice(errors, self._unconfigured()), list(errors))
            use_model = self._model_for(spec, model)
            _local.sink = lambda resp, p=p: self._record_limits(p, resp)
            try:
                gen = _TYPES[spec.type](spec, msgs, use_model, temperature, max_tokens, sys_text)
                first = next(gen, None)  # здесь выясняется, жив ли провайдер
            except ProviderError as e:
                self._mark_down(p, e)
                errors[p] = self._last_error[p]
                continue
            finally:
                _local.sink = None
            self._mark_up(p)
            chunks = iter(()) if first is None else _chain_first(first, gen)
            chunks = self._counted(p, chunks, sum(len(m["content"]) for m in msgs) + len(sys_text))
            return StreamHandle(p, use_model, chunks, list(errors))
        for p in order:
            errors.setdefault(p, self._last_error.get(p, "недоступен"))
        raise ProvidersUnavailable(errors)

    def _counted(self, pid: str, chunks: Iterator[str], in_chars: int) -> Iterator[str]:
        """Считает расход: один запрос, символы на входе и выходе (записывается, когда поток закончился или закрыт)."""
        out = 0
        try:
            for c in chunks:
                out += len(c)
                yield c
        finally:
            if self.on_usage and self.specs[pid].type != "stub":
                try:
                    self.on_usage(pid, 1, in_chars, out)
                except Exception:
                    pass

    def _unconfigured(self) -> dict[str, str]:
        return {p: self.specs[p].reason for p in self.chain if p in self.specs and self.specs[p].reason}

    def stream(self, messages: list[Message], **kw) -> Iterator[str]:
        return self.open_stream(messages, **kw).chunks

    def complete(self, messages: list[Message], **kw) -> str:
        kw.setdefault("allow_stub", False)   # для задач заглушка недопустима: это не результат
        return "".join(self.open_stream(messages, **kw).chunks)


def _chain_first(first: str, rest: Iterator[str]) -> Iterator[str]:
    yield first
    yield from rest


def _stub_notice(errors: dict[str, str], unconfigured: dict[str, str] | None = None) -> Iterator[str]:
    parts = ["Сейчас ни одна ИИ-модель не отвечает."]
    for p, e in errors.items():
        parts.append(f"• {p}: {e}")
    for p, why in (unconfigured or {}).items():
        parts.append(f"• {p}: не настроен ({why})")
    parts.append("Твоё сообщение сохранено. Проверь переменные в Render → Environment или повтори позже.")
    text = "\n".join(parts)
    for line in text.split("\n"):
        for word in line.split(" "):
            yield word + " "
        yield "\n"
