"""Сборка всех частей сервера в одном месте (без привязки к веб-фреймворку, поэтому легко тестируется)."""
from __future__ import annotations

from dataclasses import dataclass

import agents  # noqa: F401  (регистрирует типы задач)
import code_agent  # noqa: F401  (задача «code»: агент работает с GitHub)
from ai_core import ModelRouter
from chat import ChatService
from db import Database
from file_manager import FileService
from github_client import GitHub
from memory import Memory
from settings import Settings
from storage import ObjectStorage, build_storage
from tasks import TaskStore, TaskWorker


@dataclass
class Services:
    settings: Settings
    db: Database
    memory: Memory
    storage: ObjectStorage
    files: FileService
    router: ModelRouter
    chat: ChatService
    tasks: TaskStore
    worker: TaskWorker
    github: GitHub | None = None

    def warnings(self) -> list[str]:
        w = []
        if not self.db.persistent:
            w.append("База данных временная (SQLite): задайте DATABASE_URL от Neon, иначе переписки и задачи пропадут при перезапуске Render.")
        if not self.storage.persistent:
            w.append("Файлы лежат на временном диске: задайте переменные R2_*, иначе они пропадут при перезапуске Render.")
        if self.settings.allow_no_auth:
            w.append("ALLOW_NO_AUTH=1: API открыт без токена. Не используйте это на публичном сервере.")
        elif 0 < len(self.settings.api_token) < 24:
            w.append("API_TOKEN слишком короткий: используйте случайную строку от 32 символов.")
        if not any(s["state"] == "ok" and s["type"] != "stub" for s in self.router.status()):
            w.append("Ни одна настоящая ИИ-модель не настроена: отвечает заглушка. Задайте GPU_BASE_URL/GPU_MODEL или ключ резервного провайдера.")
        return w


def build_services(settings: Settings, *, router: ModelRouter | None = None, storage: ObjectStorage | None = None,
                   github: GitHub | None = None) -> Services:
    db = Database(settings.database_url)
    db.migrate()
    memory = Memory(db)
    storage = storage or build_storage(settings)
    files = FileService(db, storage, settings.max_file_bytes)
    router = router or ModelRouter.from_settings(settings)
    github = github or GitHub.from_settings(settings)
    tasks = TaskStore(db)
    worker = TaskWorker(
        tasks, {"router": router, "memory": memory, "files": files, "github": github},
        lease_s=settings.lease_seconds, max_attempts=settings.task_max_attempts,
    )
    return Services(settings, db, memory, storage, files, router, ChatService(memory, router), tasks, worker, github)


def startup(svc: Services) -> None:
    """Восстановление после перезапуска: оборванные ответы помечаются, задачи возвращаются в очередь."""
    svc.memory.mark_interrupted(svc.settings.lease_seconds)
    if svc.settings.worker_enabled:
        svc.worker.start()


def shutdown(svc: Services) -> None:
    svc.worker.stop()
    svc.db.close()
