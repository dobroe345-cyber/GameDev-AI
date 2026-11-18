"""Настройки сервера. Всё берётся из переменных окружения; секретов в репозитории нет."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parent.parent


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, "").strip() or default)
    except ValueError:
        return default


def _flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


DEFAULT_REPO = "dobroe345-cyber/GameDev-AI"  # репозиторий, с которым агент работает по умолчанию
DEFAULT_ORIGIN = "https://dobroe345-cyber.github.io"  # интерфейс на GitHub Pages: работает без настройки CORS_ORIGINS


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: Path
    api_token: str
    allow_no_auth: bool
    cors_origins: tuple[str, ...]
    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str
    r2_endpoint: str
    max_file_bytes: int
    providers_file: Path
    system_prompt_file: Path
    lease_seconds: int
    task_max_attempts: int
    worker_enabled: bool
    github_token: str = ""
    github_repos: tuple[str, ...] = ()
    github_api_url: str = "https://api.github.com"
    mail_provider: str = "resend"
    mail_api_key: str = ""
    mail_from: str = "GameDev AI <onboarding@resend.dev>"
    mail_api_url: str = ""
    allowed_emails: tuple[str, ...] = ()

    @property
    def r2_configured(self) -> bool:
        return bool(
            (self.r2_account_id or self.r2_endpoint)
            and self.r2_access_key_id
            and self.r2_secret_access_key
            and self.r2_bucket
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        data_dir = Path(env.get("DATA_DIR", "").strip() or ROOT / "data")
        # CORS: допускаем только схему://хост (путь и «/» на конце отбрасываем, это частая ошибка при вводе)
        origins = []
        for o in env.get("CORS_ORIGINS", "").split(","):
            m = re.match(r"^\s*(https?://[^/\s]+)", o)
            if m and m.group(1).lower() not in origins:
                origins.append(m.group(1).lower())
        if not origins:
            origins = [DEFAULT_ORIGIN, "http://localhost:5500", "http://127.0.0.1:5500"]
        account = env.get("R2_ACCOUNT_ID", "").strip()
        return cls(
            database_url=env.get("DATABASE_URL", "").strip() or f"sqlite:///{data_dir / 'gdai.db'}",
            data_dir=data_dir,
            api_token=env.get("API_TOKEN", "").strip(),
            allow_no_auth=_flag(env, "ALLOW_NO_AUTH"),
            cors_origins=tuple(origins),
            r2_account_id=account,
            r2_access_key_id=env.get("R2_ACCESS_KEY_ID", "").strip(),
            r2_secret_access_key=env.get("R2_SECRET_ACCESS_KEY", "").strip(),
            r2_bucket=env.get("R2_BUCKET", "").strip(),
            r2_endpoint=env.get("R2_ENDPOINT", "").strip()
            or (f"https://{account}.r2.cloudflarestorage.com" if account else ""),
            max_file_bytes=_int(env, "MAX_FILE_MB", 10) * 1024 * 1024,
            providers_file=Path(env.get("PROVIDERS_FILE", "").strip() or ROOT / "config" / "providers.json"),
            system_prompt_file=Path(env.get("SYSTEM_PROMPT_FILE", "").strip() or ROOT / "config" / "system_prompt.txt"),
            lease_seconds=max(10, _int(env, "LEASE_SECONDS", 60)),
            task_max_attempts=max(1, _int(env, "TASK_MAX_ATTEMPTS", 3)),
            worker_enabled=not _flag(env, "DISABLE_WORKER"),
            github_token=env.get("GITHUB_TOKEN", "").strip(),
            github_repos=tuple(r.strip() for r in (env.get("GITHUB_REPOS", "").strip() or DEFAULT_REPO).split(",") if r.strip()),
            github_api_url=env.get("GITHUB_API_URL", "").strip() or "https://api.github.com",
            mail_provider=(env.get("MAIL_PROVIDER", "").strip() or "resend").lower(),
            mail_api_key=env.get("MAIL_API_KEY", "").strip(),
            mail_from=env.get("MAIL_FROM", "").strip() or "GameDev AI <onboarding@resend.dev>",
            mail_api_url=env.get("MAIL_API_URL", "").strip(),
            allowed_emails=tuple(dict.fromkeys(e.strip().lower() for e in env.get("ALLOWED_EMAILS", "").split(",") if "@" in e)),
        )
