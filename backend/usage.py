"""Учёт расхода моделей по дням (UTC) и сводка лимитов для Настроек."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from db import Database

CHARS_PER_TOKEN = 3.0   # грубая оценка для русского и кода; в интерфейсе помечается «≈»


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def next_midnight(ts: float) -> float:
    return (int(ts) // 86400 + 1) * 86400


class Usage:
    def __init__(self, db: Database, clock=time.time) -> None:
        self.db, self.clock = db, clock

    def record(self, provider: str, requests: int, in_chars: int, out_chars: int) -> None:
        self.db.execute(
            "INSERT INTO usage (day, provider, requests, in_chars, out_chars) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (day, provider) DO UPDATE SET requests = usage.requests + excluded.requests, "
            "in_chars = usage.in_chars + excluded.in_chars, out_chars = usage.out_chars + excluded.out_chars",
            (day_of(self.clock()), provider, requests, in_chars, out_chars),
        )

    def summary(self, days: int = 7) -> dict:
        now = self.clock()
        rows = self.db.query("SELECT day, provider, requests, in_chars, out_chars FROM usage WHERE day >= ? ORDER BY day DESC",
                             (day_of(now - (days - 1) * 86400),))
        tok = lambda r: round((r["in_chars"] + r["out_chars"]) / CHARS_PER_TOKEN)
        today = day_of(now)
        by_provider = [{"provider": r["provider"], "requests": r["requests"], "approx_tokens": tok(r)} for r in rows if r["day"] == today]
        per_day: dict[str, dict] = {}
        for r in rows:
            d = per_day.setdefault(r["day"], {"day": r["day"], "requests": 0, "approx_tokens": 0})
            d["requests"] += r["requests"]
            d["approx_tokens"] += tok(r)
        t = per_day.get(today, {"requests": 0, "approx_tokens": 0})
        return {
            "day": today, "reset_in_s": round(next_midnight(now) - now),
            "requests": t["requests"], "approx_tokens": t["approx_tokens"],
            "by_provider": by_provider, "last_days": sorted(per_day.values(), key=lambda d: d["day"], reverse=True),
        }
