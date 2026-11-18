"""Проекты без GitHub: текстовые файлы, которые создаёт агент, хранятся в базе (Neon), а не на диске Render.

Проект = чат: имя проекта совпадает с идентификатором чата, поэтому «добавь врагов» в том же чате правит те же файлы.
Из проекта можно скачать zip и открыть предпросмотр (html с подставленными локальными css/js).
"""
from __future__ import annotations

import io
import posixpath
import re
import time
import zipfile

from db import Database
from github_client import GitHubRejected, check_path

MAX_FILES = 200
MAX_FILE_BYTES = 200_000
MAX_PROJECT_BYTES = 3_000_000
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ProjectError(Exception):
    pass


def check_project_id(project: str) -> str:
    if not ID_RE.match(project or ""):
        raise ProjectError("Недопустимое имя проекта")
    return project


class ProjectStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def files(self, project: str) -> dict[str, str]:
        return {r["path"]: r["content"] for r in self.db.query(
            "SELECT path, content FROM project_files WHERE project = ? ORDER BY path", (check_project_id(project),))}

    def listing(self, project: str) -> list[dict]:
        return self.db.query("SELECT path, size, updated_at FROM project_files WHERE project = ? ORDER BY path",
                             (check_project_id(project),))

    def get(self, project: str, path: str) -> str | None:
        r = self.db.one("SELECT content FROM project_files WHERE project = ? AND path = ?",
                        (check_project_id(project), check_path(path)))
        return r["content"] if r else None

    def projects(self) -> list[dict]:
        return self.db.query("SELECT project, COUNT(*) AS files, SUM(size) AS size, MAX(updated_at) AS updated_at "
                             "FROM project_files GROUP BY project ORDER BY updated_at DESC")

    def apply(self, project: str, changes: dict[str, str | None]) -> dict:
        """Записывает и удаляет файлы одной транзакцией. Лимиты проверяются до записи."""
        project = check_project_id(project)
        now = time.time()
        clean: dict[str, str | None] = {}
        for p, c in changes.items():
            try:
                clean[check_path(p)] = c
            except GitHubRejected as e:
                raise ProjectError(str(e)) from None
        with self.db.tx() as tx:
            cur = {r["path"]: r["size"] for r in tx.query("SELECT path, size FROM project_files WHERE project = ?", (project,))}
            after = dict(cur)
            for p, c in clean.items():
                if c is None:
                    after.pop(p, None)
                else:
                    n = len(c.encode("utf-8"))
                    if n > MAX_FILE_BYTES:
                        raise ProjectError(f"Файл {p} больше {MAX_FILE_BYTES // 1000} КБ")
                    after[p] = n
            if len(after) > MAX_FILES:
                raise ProjectError(f"В проекте не может быть больше {MAX_FILES} файлов")
            if sum(after.values()) > MAX_PROJECT_BYTES:
                raise ProjectError("Проект больше 3 МБ")
            written = deleted = 0
            for p, c in clean.items():
                if c is None:
                    deleted += tx.execute("DELETE FROM project_files WHERE project = ? AND path = ?", (project, p))
                else:
                    tx.execute(
                        "INSERT INTO project_files (project, path, content, size, updated_at) VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT (project, path) DO UPDATE SET content = excluded.content, size = excluded.size, updated_at = excluded.updated_at",
                        (project, p, c, len(c.encode("utf-8")), now))
                    written += 1
        return {"written": written, "deleted": deleted}

    def delete_project(self, project: str) -> int:
        return self.db.execute("DELETE FROM project_files WHERE project = ?", (check_project_id(project),))

    def zip_bytes(self, project: str) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p, c in self.files(project).items():
                z.writestr(p, c)
        return buf.getvalue()

    def preview_html(self, project: str, entry: str = "index.html") -> str | None:
        """Один html-документ для предпросмотра: локальные <link rel=stylesheet> и <script src> заменяются их содержимым."""
        files = self.files(project)
        if entry not in files:
            htmls = sorted(p for p in files if p.endswith(".html"))
            if not htmls:
                return None
            entry = htmls[0]
        base = posixpath.dirname(entry)

        def local(ref: str) -> str | None:
            if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//|#|data:)", ref, re.I):
                return None
            p = posixpath.normpath(posixpath.join(base, ref.split("?")[0].split("#")[0]))
            return p if p in files else None

        def css(m: re.Match) -> str:
            tag = m.group(0)
            href = re.search(r"href\s*=\s*[\"']([^\"']+)[\"']", tag, re.I)
            p = local(href.group(1)) if href and re.search(r"rel\s*=\s*[\"']?stylesheet", tag, re.I) else None
            return f"<style>\n{files[p].replace('</style', '<\\/style')}\n</style>" if p else tag

        def js(m: re.Match) -> str:
            src = re.search(r"src\s*=\s*[\"']([^\"']+)[\"']", m.group(1), re.I)
            p = local(src.group(1)) if src else None
            if not p:
                return m.group(0)
            attrs = re.sub(r"\s*src\s*=\s*[\"'][^\"']*[\"']", "", m.group(1), flags=re.I)
            return f"<script{attrs}>\n{files[p].replace('</script', '<\\/script')}\n</script>"

        html = re.sub(r"<link\b[^>]*>", css, files[entry], flags=re.I)
        return re.sub(r"<script\b([^>]*)>\s*</script>", js, html, flags=re.I)
