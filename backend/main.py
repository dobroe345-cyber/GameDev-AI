"""HTTP API GameDev AI (FastAPI).

Запуск локально:   uvicorn main:app --reload --port 8000     (из папки backend)
Запуск на Render:  см. render.yaml и docs/DEPLOY.md

Обработчики тонкие: вся логика живёт в chat.py, tasks.py, memory.py, file_manager.py, ai_core.py.
"""
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from ai_core import ProviderError, ProvidersUnavailable
from db import DatabaseError
from file_manager import FileError, is_text
from services import Services, build_services, shutdown, startup
from settings import Settings
from storage import StorageError
from tasks import HANDLERS

VERSION = "0.3.0"
ID_RE = r"^[A-Za-z0-9_-]{1,64}$"
KEY_RE = r"^[A-Za-z0-9_.-]{1,64}$"
SECRET_WORDS = ("key", "token", "secret", "password", "passwd")
MAX_JSON_BYTES = 20_000
log = logging.getLogger("gdai.api")


# ------------------------------------------------------------------- модели
class Msg(BaseModel):
    role: str = Field(max_length=20)
    content: str = Field(default="", max_length=100_000)


class ChatRequest(BaseModel):
    chat_id: str = Field(pattern=ID_RE)
    messages: list[Msg] = Field(min_length=1, max_length=200)
    provider: Optional[str] = Field(default=None, max_length=40)
    model: Optional[str] = Field(default=None, max_length=120)
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    system: Optional[str] = Field(default=None, max_length=8000)


class RenameBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class SettingBody(BaseModel):
    value: Any = None


class TaskCreate(BaseModel):
    kind: str = Field(max_length=40)
    payload: dict = Field(default_factory=dict)
    title: str = Field(default="", max_length=200)
    chat_id: Optional[str] = Field(default=None, pattern=ID_RE)


class ArchiveBody(BaseModel):
    prefix: str = Field(default="", max_length=300)
    name: str = Field(default="project", max_length=60)


class RestoreBody(BaseModel):
    key: str = Field(max_length=200)


# --------------------------------------------------------------- вспомогательное
def svc(request: Request) -> Services:
    return request.app.state.svc


def require_auth(request: Request) -> None:
    """Все /api/*, кроме /api/health, требуют `Authorization: Bearer <API_TOKEN>`."""
    s: Settings = request.app.state.settings
    if s.allow_no_auth:
        return
    if not s.api_token:
        raise HTTPException(503, "На сервере не задан API_TOKEN")
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not hmac.compare_digest(token.encode(), s.api_token.encode()):
        raise HTTPException(401, "Неверный или отсутствующий токен доступа")


def attachment_headers(name: str) -> dict:
    fname = quote(name.rsplit("/", 1)[-1] or "file")
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{fname}", "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store"}


def too_big(value) -> bool:
    return len(json.dumps(value, ensure_ascii=False, default=str)) > MAX_JSON_BYTES


public = APIRouter(prefix="/api")
private = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


# ------------------------------------------------------------------- состояние
@public.get("/health")
def health(request: Request):
    """Дешёвая проверка «процесс жив» для Render. Намеренно НЕ обращается к БД и R2:
    частые проверки не давали бы бесплатной базе Neon «заснуть» и съели бы её часы."""
    return {"ok": True, "version": VERSION, "time": time.time()}


@private.get("/status")
def status(request: Request, deep: bool = False):
    s = svc(request)
    out = {
        "version": VERSION,
        "database": {"kind": s.db.dialect, "persistent": s.db.persistent},
        "storage": {"kind": s.storage.kind, "persistent": s.storage.persistent},
        "models": s.router.status(),
        "worker": {"enabled": s.settings.worker_enabled},
        "github": {"connected": s.github is not None, "repos": list(s.github.repos) if s.github else []},
        "tasks": s.tasks.counts(),
        "warnings": s.warnings(),
    }
    if deep:
        out["database"]["ok"] = s.db.ping()
        out["storage"]["ok"] = s.storage.ping()
        out["models_alive"] = {m["id"]: s.router.check(m["id"]) for m in out["models"]
                               if m["configured"] and m["type"] != "stub"}
    return out


@private.get("/models")
def models(request: Request):
    return [{k: m[k] for k in ("id", "label", "model", "state", "in_chain")} for m in svc(request).router.status() if m["configured"]]


# ------------------------------------------------------------------------ чат
@private.post("/chat")
def chat(req: ChatRequest, request: Request):
    s = svc(request)
    try:
        reply = s.chat.start_reply(
            req.chat_id, [m.model_dump() for m in req.messages], system=req.system,
            provider=req.provider, model=req.model, temperature=req.temperature,
        )
    except (ValueError, ProviderError) as e:
        raise HTTPException(400, str(e))
    headers = {
        "X-Provider": reply.provider, "X-Model": reply.model or "", "X-Fallback": ",".join(reply.fallback_from),
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
    }
    return StreamingResponse(reply.chunks, media_type="text/plain; charset=utf-8", headers=headers)


@private.get("/chats")
def list_chats(request: Request, limit: int = 200):
    return svc(request).memory.list_chats(max(1, min(limit, 500)))


@private.get("/chats/{chat_id}")
def get_chat(chat_id: str, request: Request):
    m = svc(request).memory
    chat_row = m.get_chat(chat_id)
    if not chat_row:
        raise HTTPException(404, "Чат не найден")
    return {"chat": chat_row, "messages": m.get_history(chat_id, 500)}


@private.patch("/chats/{chat_id}")
def rename_chat(chat_id: str, body: RenameBody, request: Request):
    if not svc(request).memory.rename_chat(chat_id, body.title):
        raise HTTPException(404, "Чат не найден")
    return {"ok": True}


@private.delete("/chats/{chat_id}")
def delete_chat(chat_id: str, request: Request):
    return {"deleted": svc(request).memory.delete_chat(chat_id)}


# ------------------------------------------------------------------ настройки
def _check_key(key: str) -> None:
    import re
    if not re.match(KEY_RE, key):
        raise HTTPException(400, "Недопустимое имя настройки")
    if any(w in key.lower() for w in SECRET_WORDS):
        raise HTTPException(400, "Секреты (ключи, токены, пароли) нельзя хранить в настройках: задавайте их в переменных окружения")


@private.get("/settings")
def get_settings(request: Request):
    return svc(request).memory.all_settings()


@private.put("/settings/{key}")
def put_setting(key: str, body: SettingBody, request: Request):
    _check_key(key)
    if too_big(body.value):
        raise HTTPException(413, "Значение слишком большое")
    svc(request).memory.set_setting(key, body.value)
    return {"ok": True}


@private.delete("/settings/{key}")
def delete_setting(key: str, request: Request):
    return {"deleted": svc(request).memory.delete_setting(key)}


# --------------------------------------------------------------------- задачи
@private.post("/tasks")
def create_task(body: TaskCreate, request: Request):
    s = svc(request)
    if body.kind not in HANDLERS:
        raise HTTPException(400, f"Неизвестный тип задачи. Доступны: {', '.join(sorted(HANDLERS))}")
    if too_big(body.payload):
        raise HTTPException(413, "Слишком большие данные задачи")
    task = s.tasks.create(body.kind, body.payload, body.title, body.chat_id)
    s.worker.notify()
    return task


@private.get("/tasks")
def list_tasks(request: Request, status: Optional[str] = None, chat_id: Optional[str] = None, limit: int = 50):
    return svc(request).tasks.list(status, chat_id, limit)


@private.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request):
    s = svc(request)
    task = s.tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    return {**task, "events": s.tasks.events(task_id)}


@private.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, request: Request):
    return {"cancelled": svc(request).tasks.cancel(task_id)}


@private.post("/tasks/{task_id}/retry")
def retry_task(task_id: str, request: Request):
    s = svc(request)
    ok = s.tasks.retry(task_id)
    if ok:
        s.worker.notify()
    return {"retried": ok}


# ---------------------------------------------------------------------- файлы
@private.get("/files")
def list_files(request: Request, prefix: str = ""):
    return svc(request).files.list(prefix)


@private.get("/files/{path:path}")
def read_file(path: str, request: Request, raw: bool = False):
    data, meta = svc(request).files.read(path)
    if raw or not is_text(meta["path"]):
        return Response(data, media_type=meta["content_type"] or "application/octet-stream", headers=attachment_headers(meta["path"]))
    return {"path": meta["path"], "size": meta["size"], "content": data.decode("utf-8", "replace")}


@private.put("/files/{path:path}")
async def put_file(path: str, request: Request):
    s = svc(request)
    limit = s.settings.max_file_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"Файл больше {limit // (1024 * 1024)} МБ")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(413, f"Файл больше {limit // (1024 * 1024)} МБ")
        chunks.append(chunk)
    return await run_in_threadpool(s.files.write, path, b"".join(chunks))


@private.delete("/files/{path:path}")
def delete_file(path: str, request: Request):
    svc(request).files.delete(path)
    return {"deleted": path}


@private.post("/reindex")
def reindex(request: Request):
    """Восстановление списка файлов из R2 (например, после потери БД)."""
    return svc(request).files.reindex()


@private.post("/archives")
def create_archive(body: ArchiveBody, request: Request):
    meta = svc(request).files.archive(body.prefix, body.name)
    return {**meta, "name": meta["path"].split("/", 1)[1]}


@private.get("/archives")
def list_archives(request: Request):
    return [{**a, "name": a["path"].split("/", 1)[1]} for a in svc(request).files.list_archives()]


@private.get("/archives/{name}")
def download_archive(name: str, request: Request):
    data, meta = svc(request).files.read_archive("archives/" + name)
    return Response(data, media_type="application/zip", headers=attachment_headers(name))


# -------------------------------------------------------- резервные копии
@private.get("/export")
def export_all(request: Request):
    return svc(request).memory.export_all()


@private.post("/import")
def import_all(body: dict, request: Request):
    return svc(request).memory.import_all(body)


@private.post("/backups")
def create_backup(request: Request):
    """Сохраняет все переписки и настройки в R2 одним JSON-файлом."""
    s = svc(request)
    dump = s.memory.export_all()
    key = f"backups/backup-{time.strftime('%Y%m%d-%H%M%S')}.json"
    raw = json.dumps(dump, ensure_ascii=False).encode("utf-8")
    s.storage.put(key, raw, "application/json")
    return {"key": key, "size": len(raw), "chats": len(dump["chats"])}


@private.get("/backups")
def list_backups(request: Request):
    return [{"key": o.key, "size": o.size, "modified": o.modified}
            for o in sorted(svc(request).storage.list("backups/"), key=lambda o: o.key, reverse=True)]


@private.post("/backups/restore")
def restore_backup(body: RestoreBody, request: Request):
    if not body.key.startswith("backups/") or ".." in body.key:
        raise HTTPException(400, "Некорректный ключ резервной копии")
    s = svc(request)
    try:
        dump = json.loads(s.storage.get(body.key).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "Файл резервной копии повреждён")
    return s.memory.import_all(dump)


# ------------------------------------------------------------------ приложение
def create_app(settings: Optional[Settings] = None, factory: Callable[[Settings], Services] = build_services) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.svc = await run_in_threadpool(factory, settings)
        await run_in_threadpool(startup, app.state.svc)
        for w in app.state.svc.warnings():
            log.warning(w)
        yield
        await run_in_threadpool(shutdown, app.state.svc)

    app = FastAPI(title="GameDev AI", version=VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["*"], allow_headers=["*"],
        expose_headers=["X-Provider", "X-Model", "X-Fallback"], allow_credentials=False,
    )
    app.include_router(public)
    app.include_router(private)

    @app.exception_handler(FileError)
    async def _file_error(request: Request, exc: FileError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(StorageError)
    async def _storage_error(request: Request, exc: StorageError):
        return JSONResponse({"detail": f"Хранилище файлов недоступно: {exc}"}, status_code=503)

    @app.exception_handler(DatabaseError)
    async def _db_error(request: Request, exc: DatabaseError):
        return JSONResponse({"detail": f"База данных недоступна: {exc}"}, status_code=503)

    @app.exception_handler(ProvidersUnavailable)
    async def _models_down(request: Request, exc: ProvidersUnavailable):
        return JSONResponse({"detail": str(exc), "providers": exc.errors}, status_code=503)

    return app


app = create_app()
