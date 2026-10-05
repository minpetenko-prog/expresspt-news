"""Состояние между запусками: какие новости уже видели и что отправили."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .fetchers import Item

SEEN_DAYS = 14
SENT_DAYS = 4


def _now() -> datetime:
    return datetime.now(timezone.utc)


class State:
    def __init__(self, path: Path, data: dict | None = None):
        self.path = path
        data = data or {}
        self.bootstrapped: bool = data.get("bootstrapped", False)
        self.seen: dict[str, str] = data.get("seen", {})          # id или title_key -> ISO-время
        self.sent: list[dict] = data.get("sent", [])               # что ушло в чат
        self.failures: dict[str, int] = data.get("failures", {})   # источник -> падений подряд

    @classmethod
    def load(cls, path: Path) -> "State":
        if path.exists():
            return cls(path, json.loads(path.read_text(encoding="utf-8")))
        return cls(path)

    def save(self) -> None:
        self.prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"bootstrapped": self.bootstrapped, "seen": self.seen,
                "sent": self.sent, "failures": self.failures}
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True),
                             encoding="utf-8")

    # --- новости ---
    def is_new(self, item: Item) -> bool:
        return item.id not in self.seen and ("t:" + item.title_key) not in self.seen

    def mark_seen(self, items: list[Item]) -> None:
        ts = _now().isoformat(timespec="seconds")
        for it in items:
            self.seen[it.id] = ts
            if it.title_key:
                self.seen["t:" + it.title_key] = ts

    def add_sent(self, item: Item, headline_ru: str) -> None:
        self.sent.append({
            "ts": _now().isoformat(timespec="seconds"),
            "source": item.source_name, "url": item.url,
            "title_pt": item.title, "headline_ru": headline_ru,
        })

    def recent_sent(self, hours: int = 72) -> list[dict]:
        cutoff = _now() - timedelta(hours=hours)
        return [s for s in self.sent if datetime.fromisoformat(s["ts"]) >= cutoff]

    # --- здоровье источников ---
    def source_ok(self, source_id: str) -> None:
        self.failures.pop(source_id, None)

    def source_failed(self, source_id: str) -> int:
        self.failures[source_id] = self.failures.get(source_id, 0) + 1
        return self.failures[source_id]

    def prune(self) -> None:
        seen_cut = _now() - timedelta(days=SEEN_DAYS)
        self.seen = {k: v for k, v in self.seen.items() if datetime.fromisoformat(v) >= seen_cut}
        sent_cut = _now() - timedelta(days=SENT_DAYS)
        self.sent = [s for s in self.sent if datetime.fromisoformat(s["ts"]) >= sent_cut]
