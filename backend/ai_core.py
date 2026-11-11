"""ИИ-ядро: выбор провайдера и потоковая генерация ответа.

Здесь будет основная логика, когда появится промпт. Сейчас доступны:
  * stub              — заглушка без модели (работает из коробки);
  * openai_compatible — любой OpenAI-совместимый сервер (OpenAI, Ollama,
                        llama.cpp server, LM Studio и т.д.).
Свой провайдер добавляется через register_provider().
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import AsyncIterator, Callable

Message = dict  # {"role": str, "content": str}
Provider = Callable[["AICore", list[Message], str | None, float], AsyncIterator[str]]

_PROVIDERS: dict[str, Provider] = {}


def register_provider(name: str):
    def deco(fn: Provider) -> Provider:
        _PROVIDERS[name] = fn
        return fn
    return deco


class AICore:
    def __init__(self, config: dict | None = None, system_prompt_path: str | Path | None = None) -> None:
        self.config = config or {}
        self.default_system = ""
        if system_prompt_path and Path(system_prompt_path).is_file():
            self.default_system = Path(system_prompt_path).read_text(encoding="utf-8").strip()

    @property
    def provider(self) -> str:
        return self.config.get("provider", "stub")

    def build_messages(self, messages: list[Message], system: str | None) -> list[Message]:
        sys_text = (system or self.default_system).strip()
        head = [{"role": "system", "content": sys_text}] if sys_text else []
        return head + [m for m in messages if m.get("role") in {"user", "assistant"}]

    async def stream(
        self,
        messages: list[Message],
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        fn = _PROVIDERS.get(self.provider)
        if fn is None:
            yield f"Провайдер «{self.provider}» не найден. Доступны: {', '.join(_PROVIDERS)}."
            return
        full = self.build_messages(messages, system)
        chosen = model if model and model != "default" else self.config.get("model", "")
        async for chunk in fn(self, full, chosen, temperature):
            yield chunk


@register_provider("stub")
async def _stub(core: AICore, messages: list[Message], model: str | None, temperature: float):
    last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    text = (
        "Сервер работает, но ИИ-модель ещё не подключена (провайдер `stub`).\n\n"
        f"Ты написал: {last[:300]}\n\n"
        "Укажи провайдер в `config/config.json`."
    )
    for token in text.split(" "):
        yield token + " "
        await asyncio.sleep(0.02)


@register_provider("openai_compatible")
async def _openai_compatible(core: AICore, messages: list[Message], model: str | None, temperature: float):
    import httpx  # импорт здесь, чтобы stub работал без httpx

    base = core.config.get("base_url", "http://localhost:11434/v1").rstrip("/")
    key = os.environ.get(core.config.get("api_key_env", "AI_API_KEY"), "")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    payload = {"model": model or core.config.get("model", ""), "messages": messages,
               "temperature": temperature, "stream": True}
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", f"{base}/chat/completions", json=payload, headers=headers) as r:
                if r.status_code >= 400:
                    yield f"Ошибка модели: HTTP {r.status_code}"
                    return
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0].get("delta", {}).get("content")
                    except (KeyError, IndexError, json.JSONDecodeError):
                        continue
                    if delta:
                        yield delta
    except httpx.HTTPError as e:
        yield f"Не удалось связаться с моделью: {e}"
