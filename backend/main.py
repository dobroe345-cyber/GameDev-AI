"""FastAPI-сервер GameDev AI.

Запуск (из папки backend):  uvicorn main:app --reload --port 8000
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ai_core import AICore
from file_manager import FileManager, FileManagerError
from memory import Memory

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "config.json"


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


config = load_config()
core = AICore(config.get("ai", {}), ROOT / "config" / "system_prompt.txt")
memory = Memory(ROOT / config.get("memory_db", "backend/memory.db"))
files = FileManager(ROOT / config.get("workspace_dir", "workspace"))

app = FastAPI(title="GameDev AI", version="0.1.0")

# GitHub Pages -> ваш сервер: укажи свой адрес в config.json (cors_origins) или в CORS_ORIGINS
origins = os.environ.get("CORS_ORIGINS", "").split(",") if os.environ.get("CORS_ORIGINS") \
    else config.get("cors_origins", ["http://localhost:5500", "http://127.0.0.1:5500"])
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatMessage(BaseModel):
    role: str
    content: str = Field(max_length=200_000)


class ChatRequest(BaseModel):
    chat_id: str = Field(min_length=1, max_length=64)
    messages: list[ChatMessage] = Field(min_length=1, max_length=200)
    model: str | None = None
    temperature: float = Field(default=0.7, ge=0, le=2)
    system: str | None = None


class FileWrite(BaseModel):
    path: str
    content: str


@app.get("/api/health")
def health():
    return {"ok": True, "provider": core.provider}


@app.post("/api/chat")
async def chat(req: ChatRequest):
    msgs = [m.model_dump() for m in req.messages]
    last = msgs[-1]
    if last["role"] == "user":
        memory.add_message(req.chat_id, "user", last["content"])

    async def gen():
        parts: list[str] = []
        async for chunk in core.stream(msgs, req.system, req.model, req.temperature):
            parts.append(chunk)
            yield chunk
        memory.add_message(req.chat_id, "assistant", "".join(parts))

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


@app.get("/api/chats")
def list_chats():
    return memory.list_chats()


@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: str):
    return memory.get_history(chat_id)


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str):
    return {"deleted": memory.clear(chat_id)}


@app.get("/api/files")
def list_files():
    return files.list()


@app.get("/api/files/{path:path}")
def read_file(path: str):
    try:
        return {"path": path, "content": files.read(path)}
    except FileManagerError as e:
        raise HTTPException(400, str(e))


@app.put("/api/files")
def write_file(body: FileWrite):
    try:
        return {"path": body.path, "size": files.write(body.path, body.content)}
    except FileManagerError as e:
        raise HTTPException(400, str(e))


@app.post("/api/files/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    try:
        return {"path": file.filename, "size": files.write(file.filename or "", data)}
    except FileManagerError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/files/{path:path}")
def delete_file(path: str):
    try:
        files.delete(path)
        return {"deleted": path}
    except FileManagerError as e:
        raise HTTPException(400, str(e))
