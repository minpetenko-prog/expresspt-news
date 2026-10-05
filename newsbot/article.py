"""Получение полного текста статьи для переписывания."""
from __future__ import annotations

import logging

import requests
import trafilatura

from datetime import datetime
from zoneinfo import ZoneInfo

from .fetchers import Item, http_get

LISBON = ZoneInfo("Europe/Lisbon")

log = logging.getLogger(__name__)

MIN_FULL = 600     # меньше — считаем, что полного текста нет (пейвол, блокировка)
MAX_CHARS = 9000   # больше не нужно для поста на 1000 знаков


def get_article_text(session: requests.Session, item: Item) -> tuple[str, bool]:
    """Возвращает (текст, есть_ли_полный_текст)."""
    try:
        html = http_get(session, item.url).text
        if item.published is None:
            # у новостей со страниц (Lusa, tempo.pt…) нет даты в ленте — берём её из статьи
            try:
                meta = trafilatura.extract_metadata(html)
                if meta and meta.date:
                    d = datetime.strptime(meta.date[:10], "%Y-%m-%d")
                    item.published = d.replace(tzinfo=LISBON)
            except Exception:  # noqa: BLE001
                pass
        text = trafilatura.extract(html, include_comments=False, include_tables=True,
                                   favor_precision=True) or ""
        if len(text) >= MIN_FULL:
            return text[:MAX_CHARS], True
    except Exception as exc:  # noqa: BLE001
        log.info("статья %s недоступна: %s", item.url, exc)

    if len(item.content) >= MIN_FULL:
        return item.content[:MAX_CHARS], True
    best = item.content if len(item.content) > len(item.summary) else item.summary
    return (best or item.title), False
