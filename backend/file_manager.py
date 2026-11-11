"""Безопасная работа с файлами проекта внутри одной папки-песочницы."""
from __future__ import annotations

from pathlib import Path

ALLOWED_EXT = {
    ".txt", ".md", ".json", ".yaml", ".yml", ".toml", ".csv",
    ".py", ".js", ".ts", ".html", ".css", ".cpp", ".h", ".hpp", ".c",
    ".cs", ".lua", ".gd", ".glsl", ".shader",
}
MAX_BYTES = 1_000_000


class FileManagerError(Exception):
    """Ошибка файловой операции (неверный путь, лимит, запрещённый тип)."""


class FileManager:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, rel: str) -> Path:
        if not rel or rel.strip() in {".", "/"}:
            raise FileManagerError("Пустой путь")
        p = (self.root / rel.lstrip("/\\")).resolve()
        if p != self.root and self.root not in p.parents:
            raise FileManagerError("Путь выходит за пределы рабочей папки")
        return p

    def _check_ext(self, p: Path) -> None:
        if p.suffix.lower() not in ALLOWED_EXT:
            raise FileManagerError(f"Тип файла «{p.suffix}» не разрешён")

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.root.rglob("*")):
            if p.is_file():
                out.append({"path": p.relative_to(self.root).as_posix(), "size": p.stat().st_size})
        return out

    def read(self, rel: str) -> str:
        p = self._resolve(rel)
        if not p.is_file():
            raise FileManagerError("Файл не найден")
        self._check_ext(p)
        return p.read_text(encoding="utf-8", errors="replace")

    def write(self, rel: str, data: str | bytes) -> int:
        p = self._resolve(rel)
        self._check_ext(p)
        raw = data.encode("utf-8") if isinstance(data, str) else data
        if len(raw) > MAX_BYTES:
            raise FileManagerError("Файл слишком большой")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)
        return len(raw)

    def delete(self, rel: str) -> None:
        p = self._resolve(rel)
        if not p.is_file():
            raise FileManagerError("Файл не найден")
        p.unlink()
