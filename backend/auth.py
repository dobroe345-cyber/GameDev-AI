"""Вход по почте: одноразовый код на e-mail → сессия на устройстве.

Безопасность:
- войти могут только разрешённые адреса: из ALLOWED_EMAILS или привязанные владельцем (по токену) в Настройках;
- ответ на запрос кода одинаков для любого адреса, поэтому посторонний не узнает, какие почты разрешены;
- код: 6 цифр, живёт 10 минут, 5 попыток, хранится только хэш (HMAC); запросы кода ограничены по времени и числу;
- сессионный токен случайный (256 бит), в базе лежит только его SHA-256; живёт 90 дней; выход удаляет его.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from typing import Callable

from db import Database
from mailer import Mailer, MailError

CODE_TTL = 600
MAX_ATTEMPTS = 5
SESSION_TTL = 90 * 86400
RESEND_GAP = 45
PER_EMAIL_HOUR = 5
GLOBAL_HOUR = 20
MAX_SESSIONS = 10
EMAIL_RE = re.compile(r"^[^@\s<>\"',;]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,}$")
TOKEN_PREFIX = "gdai_"


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def normalize_email(email: str) -> str:
    e = (email or "").strip().lower()
    if len(e) > 120 or not EMAIL_RE.match(e):
        raise AuthError("Введите корректный адрес почты")
    return e


class AuthService:
    def __init__(self, db: Database, mailer: Mailer | None, secret: str, allowed: tuple[str, ...] = (),
                 clock: Callable[[], float] = time.time) -> None:
        self.db, self.mailer, self.allowed, self.clock = db, mailer, tuple(allowed), clock
        self._key = (secret or "gdai-dev").encode()

    @property
    def enabled(self) -> bool:
        return self.mailer is not None

    # ------------------------------------------------------------ помощники
    def _hash_code(self, email: str, purpose: str, code: str) -> str:
        return hmac.new(self._key, f"{email}|{purpose}|{code}".encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def is_allowed(self, email: str) -> bool:
        return email in self.allowed or self.db.one("SELECT 1 AS x FROM users WHERE email = ?", (email,)) is not None

    # --------------------------------------------------------------- коды
    def request_code(self, email: str, link: bool = False) -> None:
        """link=True: код для привязки почты (вызывать только для уже вошедшего владельца)."""
        email = normalize_email(email)
        if not self.mailer:
            raise AuthError("Отправка почты не настроена на сервере: добавьте MAIL_API_KEY в Render (docs/AUTH.md).", 503)
        now = self.clock()
        hour = now - 3600
        mine = self.db.query("SELECT created_at FROM auth_codes WHERE email = ? AND created_at > ? ORDER BY id DESC", (email, hour))
        if mine and now - mine[0]["created_at"] < RESEND_GAP:
            raise AuthError(f"Код уже отправлен. Повторить можно через {int(RESEND_GAP - (now - mine[0]['created_at'])) + 1} с.", 429)
        if len(mine) >= PER_EMAIL_HOUR:
            raise AuthError("Слишком много запросов кода для этой почты. Подождите час.", 429)
        # общий лимит считает только настоящие письма (они стоят квоты почтового сервиса); «пустые» запросы чужих адресов не считаются,
        # иначе посторонний мог бы заблокировать вход владельцу
        if not link and not self.is_allowed(email):
            pass
        elif self.db.one("SELECT COUNT(*) AS n FROM auth_codes WHERE created_at > ? AND purpose != 'ghost'", (hour,))["n"] >= GLOBAL_HOUR:
            raise AuthError("Слишком много запросов кода. Подождите немного.", 429)
        if not link and not self.is_allowed(email):
            # Для посторонних ничего не отправляем, но отвечаем так же, как для своих.
            self.db.execute("INSERT INTO auth_codes (email, purpose, code_hash, created_at, expires_at, used) VALUES (?, 'ghost', '', ?, ?, 1)",
                            (email, now, now))
            return
        purpose = "link" if link else "login"
        code = f"{secrets.randbelow(10 ** 6):06d}"
        self.db.execute("UPDATE auth_codes SET used = 1 WHERE email = ? AND used = 0", (email,))
        self.db.execute("INSERT INTO auth_codes (email, purpose, code_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                        (email, purpose, self._hash_code(email, purpose, code), now, now + CODE_TTL))
        text = (f"Код для входа в GameDev AI: {code}\n\nОн действует 10 минут. "
                "Если вы не запрашивали код, просто проигнорируйте это письмо.")
        try:
            self.mailer.send(email, f"Код входа GameDev AI: {code}", text)
        except MailError as e:
            self.db.execute("UPDATE auth_codes SET used = 1 WHERE email = ? AND used = 0", (email,))
            raise AuthError(str(e), 502) from None

    def verify(self, email: str, code: str) -> dict:
        email = normalize_email(email)
        code = re.sub(r"\D", "", code or "")
        now = self.clock()
        row = self.db.one("SELECT * FROM auth_codes WHERE email = ? AND used = 0 AND expires_at > ? ORDER BY id DESC LIMIT 1", (email, now))
        if not row:
            raise AuthError("Код не найден или истёк. Запросите новый.")
        if row["attempts"] >= MAX_ATTEMPTS:
            raise AuthError("Слишком много неверных попыток. Запросите новый код.", 429)
        if len(code) != 6 or not hmac.compare_digest(self._hash_code(email, row["purpose"], code), row["code_hash"]):
            self.db.execute("UPDATE auth_codes SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
            left = MAX_ATTEMPTS - row["attempts"] - 1
            raise AuthError(f"Неверный код. Осталось попыток: {max(0, left)}." if left > 0 else "Неверный код. Запросите новый.")
        if not self.db.execute("UPDATE auth_codes SET used = 1 WHERE id = ? AND used = 0", (row["id"],)):
            raise AuthError("Код уже использован. Запросите новый.")
        if row["purpose"] == "link":
            self.db.execute("INSERT INTO users (email, created_at) VALUES (?, ?) ON CONFLICT (email) DO NOTHING", (email, now))
        elif not self.is_allowed(email):
            raise AuthError("Эта почта не разрешена.", 403)
        out = self._new_session(email, now)
        out["linked"] = row["purpose"] == "link"
        self.db.execute("DELETE FROM auth_codes WHERE created_at < ?", (now - 86400,))
        return out

    # ------------------------------------------------------------- сессии
    def _new_session(self, email: str, now: float) -> dict:
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        self.db.execute("INSERT INTO sessions (token_hash, email, created_at, last_used, expires_at) VALUES (?, ?, ?, ?, ?)",
                        (self._hash_token(token), email, now, now, now + SESSION_TTL))
        old = self.db.query("SELECT token_hash FROM sessions WHERE email = ? ORDER BY created_at DESC", (email,))
        for r in old[MAX_SESSIONS:]:
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (r["token_hash"],))
        self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        return {"token": token, "email": email, "expires_at": now + SESSION_TTL}

    def check_session(self, token: str) -> str | None:
        if not token.startswith(TOKEN_PREFIX):
            return None
        now = self.clock()
        row = self.db.one("SELECT email, last_used, expires_at FROM sessions WHERE token_hash = ?", (self._hash_token(token),))
        if not row or row["expires_at"] < now:
            return None
        if now - row["last_used"] > 600:
            self.db.execute("UPDATE sessions SET last_used = ? WHERE token_hash = ?", (now, self._hash_token(token)))
        return row["email"]

    def logout(self, token: str) -> bool:
        return self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (self._hash_token(token),)) > 0
