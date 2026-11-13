"""Хранилище объектов: Cloudflare R2 (S3 API) в боевом режиме и локальная папка для разработки.

Сервер на Render не хранит файлы у себя: диск стирается при каждом перезапуске.
"""
from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class StorageError(RuntimeError):
    """Хранилище недоступно или вернуло ошибку."""


class StorageNotFound(StorageError):
    """Объекта нет."""


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    modified: float


class ObjectStorage:
    kind = "base"
    persistent = False

    def put(self, key: str, data: bytes, content_type: str = "") -> None: raise NotImplementedError
    def get(self, key: str) -> bytes: raise NotImplementedError
    def delete(self, key: str) -> None: raise NotImplementedError
    def list(self, prefix: str = "") -> list[ObjectInfo]: raise NotImplementedError
    def ping(self) -> bool: raise NotImplementedError

    def exists(self, key: str) -> bool:
        try:
            self.get(key)
            return True
        except StorageNotFound:
            return False


class LocalStorage(ObjectStorage):
    kind = "local"
    persistent = False  # на Render диск временный

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise StorageError("Недопустимый ключ")
        return p

    def put(self, key: str, data: bytes, content_type: str = "") -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, p)  # запись атомарна: файл либо старый, либо новый целиком
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, key: str) -> bytes:
        p = self._path(key)
        try:
            return p.read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise StorageNotFound(key) from None

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink()
        except (FileNotFoundError, IsADirectoryError):
            pass

    def list(self, prefix: str = "") -> list[ObjectInfo]:
        out = []
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and not p.name.startswith(".tmp-"):
                key = p.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    st = p.stat()
                    out.append(ObjectInfo(key, st.st_size, st.st_mtime))
        return out

    def ping(self) -> bool:
        return self.root.is_dir()


class R2Storage(ObjectStorage):
    kind = "r2"
    persistent = True

    def __init__(self, endpoint: str, access_key_id: str, secret_access_key: str, bucket: str, *, client: Any = None) -> None:
        self.bucket = bucket
        if client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as e:  # pragma: no cover - зависит от окружения
                raise StorageError("Для R2 нужен пакет boto3: pip install boto3") from e
            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                region_name="auto",
                config=Config(
                    signature_version="s3v4",
                    retries={"max_attempts": 4, "mode": "standard"},
                    connect_timeout=10,
                    read_timeout=30,
                ),
            )
        self._c = client

    @staticmethod
    def _code(e: Exception) -> str:
        return str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))

    def _wrap(self, e: Exception, key: str = "") -> StorageError:
        if self._code(e) in {"NoSuchKey", "404", "NotFound"}:
            return StorageNotFound(key)
        return StorageError(f"R2 недоступен или отклонил запрос: {self._code(e) or e}")

    def put(self, key: str, data: bytes, content_type: str = "") -> None:
        try:
            self._c.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type or "application/octet-stream")
        except Exception as e:
            raise self._wrap(e, key) from e

    def get(self, key: str) -> bytes:
        try:
            return self._c.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as e:
            raise self._wrap(e, key) from e

    def delete(self, key: str) -> None:
        try:
            self._c.delete_object(Bucket=self.bucket, Key=key)
        except Exception as e:
            raise self._wrap(e, key) from e

    def list(self, prefix: str = "") -> list[ObjectInfo]:
        out: list[ObjectInfo] = []
        try:
            for page in self._c.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
                for o in page.get("Contents", []) or []:
                    mod = o.get("LastModified")
                    out.append(ObjectInfo(o["Key"], int(o["Size"]), mod.timestamp() if hasattr(mod, "timestamp") else time.time()))
        except Exception as e:
            raise self._wrap(e) from e
        return out

    def ping(self) -> bool:
        try:
            self._c.head_bucket(Bucket=self.bucket)
            return True
        except Exception:
            return False


def build_storage(settings) -> ObjectStorage:
    """R2, если заданы все переменные окружения, иначе локальная папка (данные временные)."""
    if settings.r2_configured:
        return R2Storage(settings.r2_endpoint, settings.r2_access_key_id, settings.r2_secret_access_key, settings.r2_bucket)
    return LocalStorage(settings.data_dir / "storage")
