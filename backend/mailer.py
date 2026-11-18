"""Отправка писем через HTTP-API (Resend или Brevo).

Почему не SMTP: бесплатные сервисы Render с 26.09.2025 блокируют исходящие порты 25, 465 и 587,
поэтому Gmail/любой SMTP на бесплатном тарифе не работает. HTTP-запросы на порт 443 проходят.
Ключ хранится только в переменной окружения MAIL_API_KEY и никогда не попадает в ответы и журнал.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

PROVIDERS = {"resend": "https://api.resend.com", "brevo": "https://api.brevo.com"}


class MailError(Exception):
    """Письмо не отправлено. Текст безопасен для показа пользователю (без ключа)."""


def _split_from(value: str) -> tuple[str, str]:
    m = re.match(r"^\s*(.*?)\s*<([^<>\s]+@[^<>\s]+)>\s*$", value)
    return (m.group(1).strip('" '), m.group(2)) if m else ("", value.strip())


class Mailer:
    def __init__(self, provider: str, api_key: str, sender: str, api_url: str = "", timeout: float = 12.0) -> None:
        if provider not in PROVIDERS:
            raise ValueError(f"Неизвестный почтовый сервис «{provider}». Доступны: {', '.join(PROVIDERS)}")
        self.provider, self.api_key, self.sender, self.timeout = provider, api_key, sender, timeout
        self.api_url = (api_url or PROVIDERS[provider]).rstrip("/")

    @classmethod
    def from_settings(cls, s) -> "Mailer | None":
        if not s.mail_api_key:
            return None
        try:
            return cls(s.mail_provider, s.mail_api_key, s.mail_from, s.mail_api_url)
        except ValueError:
            return None

    def _hint(self, status: int, detail: str) -> str:
        if status in (401, 403) and self.provider == "resend" and "resend.dev" in self.sender:
            return ("Resend без своего домена шлёт только на почту, на которую зарегистрирован аккаунт Resend. "
                    "Введите именно её или подтвердите домен в Resend.")
        if status in (401, 403):
            return "Почтовый сервис не принял MAIL_API_KEY или адрес отправителя (MAIL_FROM)."
        if status == 429:
            return "Почтовый сервис просит подождать (лимит отправки). Попробуйте позже."
        return f"Почтовый сервис ответил HTTP {status}."

    def send(self, to: str, subject: str, text: str) -> None:
        name, addr = _split_from(self.sender)
        if self.provider == "resend":
            url, headers = self.api_url + "/emails", {"Authorization": f"Bearer {self.api_key}"}
            body = {"from": self.sender, "to": [to], "subject": subject, "text": text}
        else:
            url, headers = self.api_url + "/v3/smtp/email", {"api-key": self.api_key}
            body = {"sender": {"email": addr, **({"name": name} if name else {})}, "to": [{"email": to}],
                    "subject": subject, "textContent": text}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json",
                                              "User-Agent": "gamedev-ai/0.4", **headers})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                r.read(2000)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read(500).decode("utf-8", "replace")
            except Exception:
                pass
            raise MailError(self._hint(e.code, detail)) from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise MailError("Почтовый сервис недоступен. Попробуйте ещё раз через минуту.") from None
