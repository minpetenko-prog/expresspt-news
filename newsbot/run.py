"""Один запуск парсера: собрать → отфильтровать → переписать → отправить."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from .article import get_article_text
from .fetchers import Item, fetch_source
from .picker import keyword_select, llm_select
from .state import State
from .writer import write_post

log = logging.getLogger(__name__)

DIGEST_MAX = 25  # новостей в одном запуске без нейросети


def _sort_key(it: Item):
    return it.published or datetime.min.replace(tzinfo=timezone.utc)


def collect(cfg: dict, session, state: State, fetch=fetch_source) -> tuple[list[Item], list[str], int]:
    """Возвращает (новости без дублей, предупреждения об источниках, число рабочих источников)."""
    items, alerts, ok = [], [], 0
    limit = cfg["settings"].get("alert_after_failures", 6)
    for src in cfg["sources"]:
        try:
            got = fetch(session, src)
            state.source_ok(src["id"])
            items += got
            ok += 1
        except Exception as exc:  # noqa: BLE001
            n = state.source_failed(src["id"])
            if n == limit:
                alerts.append(f"⚠️ <b>{html.escape(src['name'])}</b> не отвечает {n} запусков подряд.\n"
                              f"<code>{html.escape(str(exc)[:300])}</code>")
    uniq: dict[str, Item] = {}
    titles: set[str] = set()
    for it in items:
        if it.id in uniq or it.title_key in titles:
            continue
        uniq[it.id] = it
        titles.add(it.title_key)
    return list(uniq.values()), alerts, ok


def run(cfg: dict, topics: dict, instructions: str, state: State, session, sender,
        client=None, now: datetime | None = None, fetch=fetch_source,
        article=get_article_text, bootstrap: bool = True) -> dict:
    s = cfg["settings"]
    now = now or datetime.now(timezone.utc)
    items, alerts, ok = collect(cfg, session, state, fetch)
    for a in alerts:
        sender.send(a, preview=False)

    # Источник, который заработал впервые (например, после починки), не должен
    # вывалить в чат весь свой архив: его текущие новости молча помечаем прочитанными.
    got = {it.source_id for it in items}
    if state.known_sources is None:
        # старое состояние: известными считаем источники, чьи новости уже встречались
        state.known_sources = sorted({it.source_id for it in items if not state.is_new(it)})
        if not state.bootstrapped:
            state.known_sources = sorted(got)
    onboarded = got - set(state.known_sources)
    if onboarded:
        state.mark_seen([it for it in items if it.source_id in onboarded])
        state.known_sources = sorted(set(state.known_sources) | onboarded)
        log.info("новые источники подключены: %s", ", ".join(sorted(onboarded)))

    new = [it for it in items if state.is_new(it)]
    stats = {"sources_ok": ok, "sources": len(cfg["sources"]), "new": len(new), "sent": 0}

    if bootstrap and not state.bootstrapped:
        # Первый запуск: всё текущее считаем прочитанным, чтобы не завалить чат
        state.mark_seen(new)
        state.bootstrapped = True
        sender.send(f"✅ Парсер новостей для @ExpressPT запущен.\n"
                    f"Источников работает: {ok} из {len(cfg['sources'])}.\n"
                    f"Текущие {len(new)} новостей отмечены как прочитанные — дальше будут приходить только новые.",
                    preview=False)
        return stats

    cutoff = now - timedelta(hours=s.get("max_age_hours", 12))
    fresh = sorted((it for it in new if it.published is None or it.published >= cutoff),
                   key=_sort_key, reverse=True)
    stale = [it for it in new if it not in fresh]
    state.mark_seen(stale)
    if not fresh:
        return stats

    postpone: list[Item] = []
    if client is not None:
        picks = llm_select(client, s["select_model"], fresh, topics, state.recent_sent())
        max_posts = s.get("max_posts_per_run", 5)
        for p in picks[max_posts:]:
            postpone.append(p.item)  # не влезли — рассмотрим в следующий запуск
        for p in picks[:max_posts]:
            try:
                text, full = article(session, p.item)
                post = write_post(client, s["write_model"], instructions, p.item, text, full,
                                  s.get("signature", "@ExpressPT 🇵🇹"))
            except Exception as exc:  # noqa: BLE001
                log.error("не удалось написать пост для %s: %s", p.item.url, exc)
                postpone.append(p.item)
                continue
            if post is None:
                continue
            label = f"{p.item.source_name} ↗" + ("" if full else " · только анонс")
            sender.send(post.html, buttons=[(label, p.item.url)])
            state.add_sent(p.item, post.headline)
            stats["sent"] += 1
    else:
        picks = keyword_select(fresh, topics)
        for p in picks[DIGEST_MAX:]:
            postpone.append(p.item)
        picks = picks[:DIGEST_MAX]
        if picks:
            send_digest(sender, picks)
            for p in picks:
                state.add_sent(p.item, p.item.title)
            stats["sent"] = len(picks)

    state.mark_seen([it for it in fresh if it not in postpone])
    return stats


def send_digest(sender, picks) -> None:
    blocks = []
    for p in picks:
        it = p.item
        lock = " 🔒" if it.paywalled else ""
        blocks.append(f"▪️ <b>{html.escape(p.topic)}</b> · {html.escape(it.source_name)}{lock}\n"
                      f'<a href="{html.escape(it.url, quote=True)}">{html.escape(it.title)}</a>')
    header = f"🗞 <b>Новости по темам: {len(picks)}</b>\n\n"
    chunk = header
    for b in blocks:
        if len(chunk) + len(b) + 2 > 3900:
            sender.send(chunk.rstrip(), preview=False)
            chunk = ""
        chunk += b + "\n\n"
    if chunk.strip():
        sender.send(chunk.rstrip(), preview=False)
