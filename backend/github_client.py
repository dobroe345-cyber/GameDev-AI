"""Клиент GitHub для ИИ-агента: найти файлы, прочитать, подготовить изменения и открыть pull request.

Только стандартная библиотека. Что здесь защищает репозиторий:
  * работа только с репозиториями из белого списка (GITHUB_REPOS);
  * запись только в новые ветки gdai/*, в main агент не пишет и ничего не сливает сам (Merge нажимает человек);
  * запрещены .git/, .github/ и пути с «..»; запрещено коммитить то, что похоже на ключи и пароли;
  * лимиты на размер файла и число файлов в одном pull request.
Токен нигде не попадает в сообщения об ошибках.
"""
from __future__ import annotations

import base64  # noqa: F401  (оставлено для расширений: загрузка бинарных файлов)
import fnmatch
import io
import json
import posixpath
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field

API = "https://api.github.com"
BRANCH_PREFIX = "gdai/"
MAX_FILE_BYTES = 200_000          # больше этого агент не читает и не пишет как текст
MAX_CHANGED_FILES = 30
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}
SECRET_PATTERNS = [
    (re.compile(r"gsk_[A-Za-z0-9]{20,}"), "ключ Groq"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "ключ API (sk-…)"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), "токен GitHub"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{30,}"), "токен GitHub"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "ключ AWS"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "приватный ключ"),
    (re.compile(r"postgres(?:ql)?://[^\s:/@]+:[^\s@]+@"), "строка подключения к базе с паролем"),
]


class GitHubError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class GitHubRejected(GitHubError):
    """Действие запрещено правилами защиты (не ошибка сети): агенту нужно объяснить, почему."""


def check_path(path: str) -> str:
    p = str(path or "").replace("\\", "/").strip().lstrip("/")
    p = posixpath.normpath(p) if p else ""
    parts = p.split("/")
    if not p or p == "." or ".." in parts or "\x00" in p:
        raise GitHubRejected(f"Недопустимый путь: «{path}»")
    if parts[0] == ".git" or ".git" in parts or parts[0] == ".github":
        raise GitHubRejected("Нельзя менять .git и .github (в том числе workflow-файлы)")
    if len(p) > 300:
        raise GitHubRejected("Слишком длинный путь")
    return p


def scan_secrets(text: str) -> str | None:
    for rx, label in SECRET_PATTERNS:
        if rx.search(text):
            return label
    return None


@dataclass
class Snapshot:
    repo: str
    sha: str
    paths: list[str]                        # все файлы репозитория
    text: dict[str, str] = field(default_factory=dict)   # прочитанные текстовые файлы


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):   # редиректы обрабатываем вручную, чтобы не отправлять токен на чужой хост
        return None


class GitHub:
    def __init__(self, token: str, repos: list[str], api_url: str = API, timeout: float = 25.0, max_zip_mb: int = 40) -> None:
        self.token = token.strip()
        self.repos = [r.strip() for r in repos if r.strip()]
        self.api = api_url.rstrip("/")
        self.timeout = timeout
        self.max_zip = max_zip_mb * 1024 * 1024
        self._cache: dict[tuple[str, str], Snapshot] = {}
        self._lock = threading.Lock()
        self._opener = urllib.request.build_opener(_NoRedirect)

    @classmethod
    def from_settings(cls, s) -> "GitHub | None":
        if not s.github_token:
            return None
        return cls(s.github_token, list(s.github_repos), s.github_api_url)

    # ------------------------------------------------------------ HTTP
    def _open(self, req: urllib.request.Request):
        try:
            return self._opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            return e

    def _call(self, method: str, path: str, body: dict | None = None, *, raw: bool = False, retry: bool = True):
        url = path if path.startswith("http") else self.api + path
        redirected = False   # после любого редиректа токен больше не отправляется (архив скачивается с другого хоста)
        for attempt in range(3 if (method == "GET" and retry) else 1):
            headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "gamedev-ai"}
            if not redirected:
                headers["Authorization"] = f"Bearer {self.token}"
            data = json.dumps(body).encode() if body is not None else None
            if data:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                resp = self._open(req)
                status, payload, loc = resp.status if hasattr(resp, "status") else resp.code, resp.read(self.max_zip + 1), resp.headers.get("Location")
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                if attempt < 2 and method == "GET" and retry:
                    time.sleep(0.5 * (attempt * 2 + 1)); continue
                raise GitHubError(f"GitHub недоступен: {type(e).__name__}") from None
            if status in (301, 302, 303, 307, 308) and loc:
                url, redirected = loc, True
                continue
            if status in (502, 503, 504) and attempt < 2 and method == "GET" and retry:
                time.sleep(0.5 * (attempt * 2 + 1)); continue
            if status >= 400:
                raise self._error(status, payload)
            if raw:
                return payload
            return json.loads(payload) if payload else {}
        raise GitHubError("GitHub: слишком много перенаправлений")

    @staticmethod
    def _error(status: int, payload: bytes) -> GitHubError:
        try:
            msg = json.loads(payload).get("message", "")
        except (ValueError, AttributeError):
            msg = ""
        hint = {
            401: "Токен GitHub не принят. Проверь GITHUB_TOKEN в Render (не истёк ли срок действия).",
            403: "Нет прав или исчерпан лимит запросов GitHub. Проверь права токена: Contents и Pull requests, Read and write.",
            404: "Не найдено, либо у токена нет доступа к этому репозиторию (выдай доступ в настройках токена).",
            409: "Конфликт состояния репозитория (возможно, пустой репозиторий).",
            422: "GitHub отклонил запрос",
        }.get(status, "Ошибка GitHub")
        return GitHubError(f"{hint} [HTTP {status}] {msg}".strip()[:400], status)

    # ------------------------------------------------------------ репозиторий
    def default_repo(self) -> str:
        if not self.repos:
            raise GitHubRejected("Не задан GITHUB_REPOS (список разрешённых репозиториев)")
        return self.repos[0]

    def allowed(self, repo: str | None) -> str:
        repo = (repo or self.default_repo()).strip()
        if repo.lower() not in [r.lower() for r in self.repos]:
            raise GitHubRejected(f"Репозиторий «{repo}» не разрешён. Разрешены: {', '.join(self.repos)}")
        return next(r for r in self.repos if r.lower() == repo.lower())

    def default_branch(self, repo: str) -> str:
        return self._call("GET", f"/repos/{self.allowed(repo)}")["default_branch"]

    def head(self, repo: str, branch: str | None = None) -> tuple[str, str]:
        """(имя ветки, sha последнего коммита)."""
        repo = self.allowed(repo)
        branch = branch or self.default_branch(repo)
        ref = self._call("GET", f"/repos/{repo}/git/ref/heads/{urllib.parse.quote(branch, safe='/')}")
        return branch, ref["object"]["sha"]

    # ------------------------------------------------------------ снимок для поиска и чтения
    def snapshot(self, repo: str, sha: str) -> Snapshot:
        repo = self.allowed(repo)
        with self._lock:
            hit = self._cache.get((repo, sha))
        if hit:
            return hit
        blob = self._call("GET", f"/repos/{repo}/zipball/{sha}", raw=True)
        if len(blob) > self.max_zip:
            raise GitHubRejected("Репозиторий слишком большой для разбора агентом")
        paths, text = [], {}
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                rel = info.filename.split("/", 1)[1] if "/" in info.filename else info.filename
                parts = rel.split("/")
                if not rel or any(p in SKIP_DIRS for p in parts[:-1]):
                    continue
                paths.append(rel)
                if info.file_size > MAX_FILE_BYTES:
                    continue
                data = z.read(info)
                if b"\x00" in data[:4096]:
                    continue
                try:
                    text[rel] = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
        snap = Snapshot(repo, sha, sorted(paths), text)
        with self._lock:
            self._cache[(repo, sha)] = snap
            while len(self._cache) > 3:
                self._cache.pop(next(iter(self._cache)))
        return snap

    @staticmethod
    def find(snap: Snapshot, query: str = "", limit: int = 60) -> list[str]:
        """Поиск по пути: подстрока или шаблон (*.js). Пустой запрос возвращает всё (до limit)."""
        q = query.strip().lower()
        if not q:
            return snap.paths[:limit]
        if any(ch in q for ch in "*?["):
            return [p for p in snap.paths if fnmatch.fnmatch(p.lower(), q) or fnmatch.fnmatch(posixpath.basename(p).lower(), q)][:limit]
        return [p for p in snap.paths if q in p.lower()][:limit]

    @staticmethod
    def grep(snap: Snapshot, pattern: str, prefix: str = "", limit: int = 40, regex: bool = False) -> list[dict]:
        try:
            rx = re.compile(pattern if regex else re.escape(pattern), re.I)
        except re.error as e:
            raise GitHubRejected(f"Некорректное регулярное выражение: {e}") from None
        out = []
        for path, content in snap.text.items():
            if prefix and not path.startswith(prefix):
                continue
            for n, line in enumerate(content.splitlines(), 1):
                if rx.search(line):
                    out.append({"path": path, "line": n, "text": line.strip()[:200]})
                    if len(out) >= limit:
                        return out
        return out

    # ------------------------------------------------------------ запись: ветка, коммит, pull request
    def validate_changes(self, changes: dict[str, str | None]) -> None:
        if not changes:
            raise GitHubRejected("Нет изменений для отправки")
        if len(changes) > MAX_CHANGED_FILES:
            raise GitHubRejected(f"Слишком много файлов в одном pull request (максимум {MAX_CHANGED_FILES})")
        for path, content in changes.items():
            check_path(path)
            if content is None:
                continue
            if len(content.encode("utf-8")) > MAX_FILE_BYTES:
                raise GitHubRejected(f"Файл {path} больше {MAX_FILE_BYTES // 1000} КБ")
            label = scan_secrets(content)
            if label:
                raise GitHubRejected(f"В файле {path} найден {label}: такое нельзя отправлять в репозиторий")

    def ensure_branch(self, repo: str, name: str, base_sha: str) -> None:
        repo = self.allowed(repo)
        if not name.startswith(BRANCH_PREFIX):
            raise GitHubRejected(f"Агент пишет только в ветки {BRANCH_PREFIX}*")
        try:
            self._call("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{name}", "sha": base_sha})
        except GitHubError as e:
            if e.status != 422:                 # 422: ветка уже есть (повтор после сбоя), это нормально
                raise

    def commit_files(self, repo: str, branch: str, message: str, changes: dict[str, str | None]) -> str:
        """Один коммит со всеми изменениями. None вместо содержимого = удалить файл. Возвращает sha коммита."""
        repo = self.allowed(repo)
        if not branch.startswith(BRANCH_PREFIX):
            raise GitHubRejected(f"Агент пишет только в ветки {BRANCH_PREFIX}*")
        self.validate_changes(changes)
        ref = self._call("GET", f"/repos/{repo}/git/ref/heads/{urllib.parse.quote(branch, safe='/')}")
        parent = ref["object"]["sha"]
        base_tree = self._call("GET", f"/repos/{repo}/git/commits/{parent}")["tree"]["sha"]
        tree = [{"path": check_path(p), "mode": "100644", "type": "blob", **({"content": c} if c is not None else {"sha": None})}
                for p, c in sorted(changes.items())]
        new_tree = self._call("POST", f"/repos/{repo}/git/trees", {"base_tree": base_tree, "tree": tree})["sha"]
        commit = self._call("POST", f"/repos/{repo}/git/commits", {"message": message[:2000], "tree": new_tree, "parents": [parent]})["sha"]
        self._call("PATCH", f"/repos/{repo}/git/refs/heads/{urllib.parse.quote(branch, safe='/')}", {"sha": commit, "force": False})
        return commit

    def open_pr(self, repo: str, branch: str, base: str, title: str, body: str) -> dict:
        repo = self.allowed(repo)
        if not branch.startswith(BRANCH_PREFIX):
            raise GitHubRejected(f"Агент пишет только в ветки {BRANCH_PREFIX}*")
        try:
            pr = self._call("POST", f"/repos/{repo}/pulls", {"title": title[:200], "head": branch, "base": base, "body": body[:6000]})
        except GitHubError as e:
            if e.status != 422:
                raise
            owner = repo.split("/")[0]       # уже есть открытый PR из этой ветки (повтор после сбоя)
            found = self._call("GET", f"/repos/{repo}/pulls?head={urllib.parse.quote(owner + ':' + branch)}&state=open")
            if not found:
                raise
            pr = found[0]
        return {"number": pr["number"], "url": pr["html_url"]}
