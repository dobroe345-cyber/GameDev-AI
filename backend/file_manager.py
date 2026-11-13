"""Файлы проектов: проверка путей и типов, хранение в R2, индекс в БД, zip-архивы.

Содержимое лежит в объектном хранилище (R2), а список файлов (путь, размер, sha256) в базе,
поэтому листинг быстрый, а при потере базы индекс восстанавливается из хранилища (reindex).
"""
from __future__ import annotations

import hashlib
import io
import mimetypes
import re
import time
import zipfile
from pathlib import PurePosixPath

from db import Database
from storage import ObjectStorage, StorageNotFound

TEXT_EXT = {
    ".txt", ".md", ".json", ".yaml", ".yml", ".toml", ".csv", ".xml", ".ini", ".cfg",
    ".py", ".js", ".ts", ".html", ".css", ".cpp", ".h", ".hpp", ".c", ".cs", ".lua",
    ".gd", ".glsl", ".shader", ".tscn", ".tres",
}
BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".wav", ".ogg", ".mp3",
    ".glb", ".gltf", ".obj", ".fbx", ".ttf", ".otf", ".zip",
}
ALLOWED_EXT = TEXT_EXT | BINARY_EXT

FILES_PREFIX = "files/"
ARCHIVES_PREFIX = "archives/"
MAX_PATH_LEN = 300
MAX_DEPTH = 12
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024  # защита памяти бесплатного Render
MAX_ARCHIVE_FILES = 2000


class FileError(Exception):
    status = 400


class FilePathError(FileError): status = 400
class FileTypeError(FileError): status = 415
class FileTooLarge(FileError): status = 413
class FileNotFound(FileError): status = 404


def normalize_path(rel: str) -> str:
    """Чистая лексическая проверка пути: без «..», без выхода за корень, абсолютный путь становится относительным."""
    if not isinstance(rel, str):
        raise FilePathError("Путь должен быть строкой")
    p = rel.strip().replace("\\", "/")
    if not p or len(p) > MAX_PATH_LEN or any(ord(c) < 32 for c in p):
        raise FilePathError("Пустой или недопустимый путь")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or ".." in parts or len(parts) > MAX_DEPTH:
        raise FilePathError("Путь выходит за пределы рабочей папки или слишком глубокий")
    return "/".join(parts)


def check_ext(path: str) -> str:
    ext = PurePosixPath(path).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise FileTypeError(f"Тип файла «{ext or 'без расширения'}» не разрешён")
    return ext


def is_text(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in TEXT_EXT


class FileService:
    def __init__(self, db: Database, storage: ObjectStorage, max_bytes: int = 10 * 1024 * 1024) -> None:
        self.db = db
        self.storage = storage
        self.max_bytes = max_bytes

    # ------------------------------------------------------------- запись
    def _store(self, path: str, key: str, raw: bytes, kind: str, content_type: str) -> dict:
        now = time.time()
        self.storage.put(key, raw, content_type)  # сначала данные; если упадёт БД, reindex подхватит
        meta = {"path": path, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "kind": kind, "content_type": content_type}
        self.db.execute(
            "INSERT INTO files (path, storage_key, size, sha256, content_type, kind, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (path) DO UPDATE SET storage_key = excluded.storage_key, "
            "size = excluded.size, sha256 = excluded.sha256, content_type = excluded.content_type, "
            "kind = excluded.kind, updated_at = excluded.updated_at",
            (path, key, meta["size"], meta["sha256"], content_type, kind, now, now),
        )
        meta["updated_at"] = now
        return meta

    def write(self, path: str, data: bytes | str, content_type: str | None = None) -> dict:
        path = normalize_path(path)
        if (path + "/").startswith(ARCHIVES_PREFIX):
            raise FilePathError("Папка archives/ зарезервирована для архивов")
        check_ext(path)
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        if len(raw) > self.max_bytes:
            raise FileTooLarge(f"Файл больше {self.max_bytes // (1024 * 1024)} МБ")
        ctype = content_type or mimetypes.guess_type(path)[0] or "application/octet-stream"
        return self._store(path, FILES_PREFIX + path, raw, "file", ctype)

    # ------------------------------------------------------------- чтение
    def meta(self, path: str) -> dict:
        path = normalize_path(path)
        row = self.db.one("SELECT * FROM files WHERE path = ?", (path,))
        if not row:
            raise FileNotFound("Файл не найден")
        return row

    def read(self, path: str) -> tuple[bytes, dict]:
        meta = self.meta(path)
        try:
            return self.storage.get(meta["storage_key"]), meta
        except StorageNotFound:
            raise FileNotFound("Файл есть в индексе, но отсутствует в хранилище (запусти reindex)") from None

    def read_text(self, path: str) -> str:
        if not is_text(normalize_path(path)):
            raise FileTypeError("Это не текстовый файл, используй скачивание")
        return self.read(path)[0].decode("utf-8", errors="replace")

    def list(self, prefix: str = "", kind: str = "file") -> list[dict]:
        pre = prefix.strip().strip("/")
        rows = self.db.query("SELECT path, size, sha256, content_type, kind, updated_at FROM files WHERE kind = ? ORDER BY path", (kind,))
        return [r for r in rows if not pre or r["path"] == pre or r["path"].startswith(pre + "/")]

    def delete(self, path: str) -> None:
        meta = self.meta(path)
        self.storage.delete(meta["storage_key"])  # сначала данные, потом индекс
        self.db.execute("DELETE FROM files WHERE path = ?", (meta["path"],))

    # ----------------------------------------------------------- архивы
    def archive(self, prefix: str = "", name: str = "project") -> dict:
        files = self.list(prefix)
        if not files:
            raise FileNotFound("Нет файлов для архива")
        if len(files) > MAX_ARCHIVE_FILES or sum(f["size"] for f in files) > MAX_ARCHIVE_BYTES:
            raise FileTooLarge("Слишком много данных для одного архива, выбери папку поменьше")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.writestr(f["path"], self.read(f["path"])[0])
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._") or "project"
        arch_path = f"{ARCHIVES_PREFIX}{safe}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        return self._store(arch_path, arch_path, buf.getvalue(), "archive", "application/zip")

    def list_archives(self) -> list[dict]:
        return self.list(kind="archive")

    def read_archive(self, path: str) -> tuple[bytes, dict]:
        path = normalize_path(path)
        row = self.db.one("SELECT * FROM files WHERE path = ? AND kind = 'archive'", (path,))
        if not row:
            raise FileNotFound("Архив не найден")
        return self.storage.get(row["storage_key"]), row

    # -------------------------------------------------------- восстановление
    def reindex(self) -> dict:
        """Сверяет индекс с содержимым хранилища: добавляет «осиротевшие» объекты, убирает записи без объектов."""
        now = time.time()
        found: dict[str, tuple[str, str, int]] = {}
        for pre, kind in ((FILES_PREFIX, "file"), (ARCHIVES_PREFIX, "archive")):
            for o in self.storage.list(pre):
                path = o.key[len(pre):] if kind == "file" else o.key
                found[path] = (o.key, kind, o.size)
        known = {r["path"]: r for r in self.db.query("SELECT path, storage_key FROM files")}
        added = removed = 0
        for path, (key, kind, size) in found.items():
            if path not in known:
                ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
                self.db.execute(
                    "INSERT INTO files (path, storage_key, size, sha256, content_type, kind, created_at, updated_at) "
                    "VALUES (?, ?, ?, '', ?, ?, ?, ?) ON CONFLICT (path) DO NOTHING",
                    (path, key, size, ctype, kind, now, now),
                )
                added += 1
        for path in known:
            if path not in found:
                self.db.execute("DELETE FROM files WHERE path = ?", (path,))
                removed += 1
        return {"added": added, "removed": removed}
