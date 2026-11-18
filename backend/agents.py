"""Агенты: многошаговые задачи, которые переживают перезапуск сервера.

Принцип: после каждого шага результат сохраняется в state (в БД). Если процесс умер,
агент читает state и продолжает со следующего шага, а не с нуля. Побочные действия
(сообщение в чат, запись файла) идемпотентны: повторное выполнение не плодит дубли.

Новый тип задачи добавляется так:  @register_task("мой_тип")  def handler(ctx): ...
"""
from __future__ import annotations

import json
import re
import time

from file_manager import FileError, check_ext, normalize_path
from tasks import PermanentError, TaskContext, register_task

PLAN_SYSTEM = "Ты планировщик задач по разработке игр. Отвечай только JSON-массивом коротких строк, без пояснений."
STEP_SYSTEM = "Ты помощник по разработке игр. Выполни ровно один указанный шаг, конкретно и по делу. Код давай в блоках."
FINAL_SYSTEM = "Ты помощник по разработке игр. Собери из результатов шагов цельный итоговый ответ на русском языке."
CLIP = 1500  # сколько символов результата предыдущих шагов попадает в запрос


def parse_plan(text: str, max_steps: int, goal: str) -> list[str]:
    """Достаёт список шагов из ответа модели; при любой неудаче план состоит из одного шага: самой цели."""
    steps: list[str] = []
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            steps = [str(x).strip() for x in data if isinstance(x, (str, int, float)) and str(x).strip()]
        except json.JSONDecodeError:
            steps = []
    if not steps:
        steps = [re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", ln).strip() for ln in text.splitlines()]
        steps = [s for s in steps if len(s) > 2 and re.search(r"\w", s)]
    return (steps or [goal])[: max(1, max_steps)]


def _clamp(v, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _llm(ctx: TaskContext):
    p = ctx.payload

    def call(messages, system, max_tokens):
        ctx.check()
        wait = getattr(ctx.router, "tokens_wait", None)
        if wait:   # у бесплатных провайдеров мал минутный лимит токенов: лучше подождать сброса, чем получить 429
            need = (sum(len(m["content"]) for m in messages) + len(system or "")) / 3 + (max_tokens or 0)
            left = wait(need)
            if left > 3:
                ctx.log(f"Жду сброса минутного лимита модели (~{int(left)} с)", "warn")
            while left > 0:
                step = min(1.0, left)
                time.sleep(step)
                ctx.check()
                left -= step
        return ctx.router.complete(
            messages, system=system, provider=p.get("provider"), model=p.get("model"),
            temperature=p.get("temperature"), max_tokens=max_tokens,
        )
    return call


def _post_once(ctx: TaskContext, chat_id: str | None, text: str) -> None:
    """Пишет ответ в чат один раз, даже если процесс упал между записью и сохранением состояния."""
    if not chat_id or ctx.state.get("posted"):
        return
    last = ctx.memory.get_history(chat_id, limit=3)
    if not (last and last[-1]["role"] == "assistant" and last[-1]["content"] == text):
        ctx.memory.add_message(chat_id, "assistant", text, provider="task")
    ctx.state["posted"] = True
    ctx.checkpoint()


@register_task("agent")
def agent(ctx: TaskContext) -> dict:
    """payload: goal (обязательно), chat_id, max_steps (1-6), save_to (путь файла), provider, model, temperature."""
    goal = str(ctx.payload.get("goal", "")).strip()
    if not goal or len(goal) > 4000:
        raise PermanentError("Нужна цель длиной от 1 до 4000 символов")
    save_to = ctx.payload.get("save_to")
    if save_to:
        try:
            check_ext(normalize_path(str(save_to)))
        except FileError as e:
            raise PermanentError(f"Некорректный путь для сохранения: {e}") from e
    max_steps = _clamp(ctx.payload.get("max_steps"), 1, 6, 4)
    chat_id = ctx.payload.get("chat_id") or ctx.task.get("chat_id")
    llm, st = _llm(ctx), ctx.state

    if "plan" not in st:
        text = llm([{"role": "user", "content": f"Цель: {goal}\nРазбей на 1-{max_steps} коротких шагов. Верни только JSON-массив строк."}],
                   PLAN_SYSTEM, 600)
        st["plan"], st["results"] = parse_plan(text, max_steps, goal), []
        ctx.log(f"План из {len(st['plan'])} шагов")
        ctx.checkpoint(1 / (len(st["plan"]) + 2))
    plan = st["plan"]
    n = len(plan)

    for i in range(len(st["results"]), n):   # уже выполненные шаги пропускаются
        done = "\n\n".join(f"Результат шага {j + 1} ({plan[j]}):\n{r[:CLIP]}" for j, r in enumerate(st["results"]))
        prompt = (f"Общая цель: {goal}\nПлан: " + "; ".join(f"{j + 1}) {s}" for j, s in enumerate(plan))
                  + (f"\n\n{done}" if done else "") + f"\n\nВыполни шаг {i + 1}: {plan[i]}")
        st["results"].append(llm([{"role": "user", "content": prompt}], STEP_SYSTEM, 1200))
        ctx.log(f"Шаг {i + 1}/{n} выполнен")
        ctx.checkpoint((i + 2) / (n + 2))

    if "final" not in st:
        body = "\n\n".join(f"Шаг {j + 1} ({plan[j]}):\n{r[:CLIP]}" for j, r in enumerate(st["results"]))
        st["final"] = llm([{"role": "user", "content": f"Цель: {goal}\n\n{body}\n\nСобери итоговый ответ."}], FINAL_SYSTEM, 1500)
        ctx.checkpoint((n + 1) / (n + 2))

    if save_to:
        ctx.files.write(str(save_to), st["final"])   # перезапись безопасна при повторе
    _post_once(ctx, chat_id, st["final"])
    return {"final": st["final"], "steps": n, "saved_to": save_to or None}


@register_task("generate")
def generate(ctx: TaskContext) -> dict:
    """Фоновый ответ в чат: payload chat_id (обязательно), prompt (необязательно), provider, model, system."""
    chat_id = ctx.payload.get("chat_id") or ctx.task.get("chat_id")
    if not chat_id:
        raise PermanentError("Нужен chat_id")
    if "text" not in ctx.state:   # уже сгенерированный текст при повторе не запрашивается заново
        prompt = str(ctx.payload.get("prompt", "")).strip()
        history = [{"role": m["role"], "content": m["content"]} for m in ctx.memory.get_history(chat_id, limit=40)
                   if m["status"] == "done" and m.get("provider") != "stub"]
        if prompt:
            history.append({"role": "user", "content": prompt})
        if not history:
            raise PermanentError("В чате нет сообщений")
        ctx.state["text"] = ctx.router.complete(
            history, system=ctx.payload.get("system"), provider=ctx.payload.get("provider"),
            model=ctx.payload.get("model"), temperature=ctx.payload.get("temperature"), max_tokens=2000,
        )
        ctx.checkpoint(0.8)
    _post_once(ctx, chat_id, ctx.state["text"])
    return {"text": ctx.state["text"]}
