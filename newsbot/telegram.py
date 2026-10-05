"""Отправка сообщений в Telegram через Bot API."""
from __future__ import annotations

import logging
import re
import time

import requests

log = logging.getLogger(__name__)
LIMIT = 4096


class Telegram:
    def __init__(self, token: str, chat_id: str, link_preview: bool = True,
                 session: requests.Session | None = None):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.link_preview = link_preview
        self.http = session or requests.Session()

    def _post(self, payload: dict) -> dict:
        for _ in range(4):
            r = self.http.post(f"{self.base}/sendMessage", json=payload, timeout=30)
            data = r.json()
            if data.get("ok"):
                return data
            retry = (data.get("parameters") or {}).get("retry_after")
            if r.status_code == 429 and retry:
                time.sleep(int(retry) + 1)
                continue
            raise RuntimeError(f"Telegram: {data.get('description', r.text)}")
        raise RuntimeError("Telegram: слишком много запросов")

    def send(self, html_text: str, buttons: list[tuple[str, str]] | None = None,
             preview: bool | None = None) -> None:
        payload = {
            "chat_id": self.chat_id,
            "text": html_text[:LIMIT],
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": not (self.link_preview if preview is None else preview)},
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": t, "url": u}] for t, u in buttons]}
        try:
            self._post(payload)
        except RuntimeError as exc:
            if "parse" not in str(exc).lower():
                raise
            # Сломанная разметка — отправляем без форматирования, чтобы новость не потерялась
            log.warning("ошибка разметки, отправляю без форматирования: %s", exc)
            payload["text"] = re.sub(r"<[^>]+>", "", html_text)[:LIMIT]
            payload.pop("parse_mode")
            self._post(payload)
        time.sleep(1.1)  # не больше ~1 сообщения в секунду в один чат


class DryRun:
    """Печатает сообщения в консоль вместо отправки."""

    def __init__(self):
        self.sent: list[tuple[str, list | None]] = []

    def send(self, html_text: str, buttons=None, preview=None) -> None:
        self.sent.append((html_text, buttons))
        print("\n" + "─" * 60)
        print(html_text)
        for t, u in buttons or []:
            print(f"[{t}] → {u}")
