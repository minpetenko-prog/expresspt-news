"""Отбор новостей по темам: через Claude (если есть ключ) или по ключевым словам."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .fetchers import Item, strip_accents
from .llm import call_tool

log = logging.getLogger(__name__)

BATCH = 60  # новостей в одном запросе на отбор


@dataclass
class Pick:
    item: Item
    topic: str
    priority: int = 2


# ---------- без нейросети ----------

def _norm(s: str) -> str:
    return strip_accents(s.lower())


def _kw_regex(keywords: list[str]) -> re.Pattern | None:
    kws = [re.escape(_norm(k.strip())) for k in keywords if k and k.strip()]
    if not kws:
        return None
    return re.compile(r"(?<![a-z0-9])(" + "|".join(sorted(kws, key=len, reverse=True)) + r")(?![a-z0-9])")


def keyword_select(items: list[Item], topics_cfg: dict) -> list[Pick]:
    exclude = _kw_regex(topics_cfg.get("exclude", {}).get("keywords", []))
    rules = [(t["name"], _kw_regex(t.get("keywords", []))) for t in topics_cfg.get("topics", [])]
    picks = []
    for it in items:
        text = _norm(f"{it.title} {it.summary}")
        if exclude and exclude.search(text):
            continue
        for name, rx in rules:
            if rx and rx.search(text):
                picks.append(Pick(it, name))
                break
    return picks


# ---------- через Claude ----------

SELECT_TOOL = {
    "name": "select_news",
    "description": "Вернуть номера новостей, которые стоит опубликовать в канале.",
    "input_schema": {
        "type": "object",
        "properties": {
            "selected": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "i": {"type": "integer", "description": "номер новости из списка"},
                        "topic": {"type": "string", "description": "тема из списка тем"},
                        "priority": {"type": "integer", "enum": [1, 2, 3],
                                     "description": "1 — важно для многих жителей, 2 — полезно, 3 — можно"},
                    },
                    "required": ["i", "topic", "priority"],
                },
            }
        },
        "required": ["selected"],
    },
}


def _topics_text(cfg: dict) -> str:
    lines = [f"- {t['name']}: {t.get('description', '')}" for t in cfg.get("topics", [])]
    exc = cfg.get("exclude", {}).get("description", "").strip()
    return "\n".join(lines) + (f"\n\nВсегда пропускать: {exc}" if exc else "")


def build_select_prompt(batch: list[Item], topics_cfg: dict, recent: list[dict]) -> str:
    cands = []
    for i, it in enumerate(batch):
        flag = " [только анонс]" if it.paywalled else ""
        summary = (it.summary or "")[:250]
        cands.append(f"[{i}] {it.source_name}{flag} | {it.title}" + (f" — {summary}" if summary else ""))
    recent_txt = "\n".join(f"- {s['headline_ru']} ({s['title_pt']})" for s in recent[-60:]) or "—"
    return f"""Ты отбираешь новости для русскоязычного Telegram-канала о Португалии.

Аудитория: {topics_cfg.get('audience', '').strip()}

Темы канала:
{_topics_text(topics_cfg)}

Уже опубликовано за последние дни (не бери повторно те же события):
{recent_txt}

Новые новости из португальских СМИ:
{chr(10).join(cands)}

Правила:
- Бери только новости, которые явно подходят под темы и интересны аудитории. Будь строгим: обычно подходит 5–15% списка.
- Если несколько новостей про одно и то же событие — выбери одну, с самым содержательным заголовком и анонсом; помеченные [только анонс] не бери, если есть то же событие в другом издании.
- Пропускай повторы уже опубликованного — в том числе «обновления» той же ситуации (то же предупреждение IPMA с новыми округами, та же забастовка с новыми цифрами). Исключение — только резкое изменение главного (красный уровень, отмена, окончательное решение).
- Пропускай мнения и колонки, прямые трансляции, анонсы ТВ-передач, новости отдельных маленьких муниципалитетов (их бюджеты, ремонт местных дорог, назначения), если они не касаются многих людей.
Ответ — JSON: {{"selected": [{{"i": номер, "topic": тема, "priority": 1–3}}]}}. Если ничего не подходит — пустой список."""


def llm_select(client, model: str, items: list[Item], topics_cfg: dict,
               recent: list[dict]) -> list[Pick]:
    picks: list[Pick] = []
    for start in range(0, len(items), BATCH):
        batch = items[start:start + BATCH]
        result = call_tool(client, model, None, build_select_prompt(batch, topics_cfg, recent),
                           SELECT_TOOL, max_tokens=1500)
        for row in result.get("selected", []):
            i = row.get("i")
            if isinstance(i, int) and 0 <= i < len(batch):
                picks.append(Pick(batch[i], row.get("topic", ""), int(row.get("priority", 2))))
    picks.sort(key=lambda p: p.priority)
    log.info("отобрано %d из %d", len(picks), len(items))
    return picks
