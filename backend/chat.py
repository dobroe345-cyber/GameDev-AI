"""Ответ в чате: приём истории, потоковая генерация и надёжное сохранение.

Порядок важен для надёжности:
  1. сообщение пользователя записывается в БД ДО обращения к модели (не потеряется, даже если модели недоступны);
  2. ответ пишется в БД со статусом «streaming» и периодически дополняется;
  3. при обрыве (закрыли вкладку, упал GPU, перезапуск) сохранённая часть получает статус «interrupted».
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterator

from ai_core import ModelRouter, ProviderError
from memory import Memory

TITLE_LEN = 48


@dataclass
class Reply:
    provider: str
    model: str
    fallback_from: list[str]
    chunks: Iterator[str]


class ChatService:
    def __init__(self, memory: Memory, router: ModelRouter, *, flush_every_s: float = 1.5,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.memory, self.router = memory, router
        self.flush_every_s, self._clock = flush_every_s, clock

    @staticmethod
    def normalize(messages: list[dict]) -> list[dict]:
        out = [{"role": m["role"], "content": str(m.get("content", ""))} for m in messages
               if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()]
        if not out or out[-1]["role"] != "user":
            raise ValueError("Последним должно быть непустое сообщение пользователя")
        return out

    def start_reply(
        self, chat_id: str, messages: list[dict], *, system: str | None = None, provider: str | None = None,
        model: str | None = None, temperature: float | None = None,
    ) -> Reply:
        msgs = self.normalize(messages)
        first_user = next((m["content"] for m in msgs if m["role"] == "user"), "")
        self.memory.ensure_chat(chat_id, " ".join(first_user.split())[:TITLE_LEN])
        self.memory.sync_history(chat_id, msgs)         # 1) вопрос сохранён, что бы дальше ни случилось
        handle = self.router.open_stream(msgs, system=system, provider=provider, model=model, temperature=temperature)
        return Reply(handle.provider, handle.model, handle.fallback_from,
                     self._generate(chat_id, handle.provider, handle.model, handle.chunks))

    def _generate(self, chat_id: str, provider: str, model: str, chunks: Iterator[str]) -> Iterator[str]:
        parts: list[str] = []
        mid: int | None = None   # запись ответа создаётся с первым куском: до этого нечего терять и нечего чистить
        last_flush = self._clock()
        finished = False
        try:
            for chunk in chunks:
                if mid is None:
                    mid = self.memory.add_message(chat_id, "assistant", "", status="streaming", provider=provider, model=model)
                parts.append(chunk)
                yield chunk
                if self._clock() - last_flush >= self.flush_every_s:     # 2) промежуточное сохранение
                    self.memory.update_message(mid, content="".join(parts))
                    last_flush = self._clock()
            finished = True
            text = "".join(parts)
            if mid is not None:
                self.memory.update_message(mid, content=text, status="done")
        finally:
            if not finished and mid is not None:                          # 3) обрыв: сохраняем, что успели
                try:
                    self.memory.update_message(mid, content="".join(parts), status="interrupted")
                except Exception:
                    pass  # не маскируем исходную причину обрыва
