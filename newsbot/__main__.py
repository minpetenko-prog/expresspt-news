"""Запуск: python -m newsbot [--dry-run] [--url ССЫЛКА]

Переменные окружения:
  TELEGRAM_BOT_TOKEN  — токен бота от @BotFather
  TELEGRAM_CHAT_ID    — id закрытого канала/чата, куда слать посты (например -1001234567890)
  ANTHROPIC_API_KEY   — ключ Claude API; без него бот шлёт подборку заголовков по ключевым словам
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .article import get_article_text
from .fetchers import Item, make_session
from .llm import make_client
from .run import run
from .state import State
from .telegram import DryRun, Telegram
from .writer import write_post

ROOT = Path(__file__).resolve().parent.parent


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="newsbot", description="Парсер новостей для @ExpressPT")
    ap.add_argument("--dry-run", action="store_true", help="печатать в консоль, ничего не отправлять и не сохранять")
    ap.add_argument("--url", help="переписать одну статью по ссылке и выйти")
    ap.add_argument("--no-llm", action="store_true", help="не использовать Claude, даже если есть ключ")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--topics", default=str(ROOT / "topics.yaml"))
    ap.add_argument("--instructions", default=str(ROOT / "INSTRUCTIONS.md"))
    ap.add_argument("--state", default=str(ROOT / "state" / "state.json"))
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_yaml(Path(args.config))
    topics = load_yaml(Path(args.topics))
    instructions = Path(args.instructions).read_text(encoding="utf-8")
    session = make_session()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    client = make_client(api_key) if api_key and not args.no_llm else None

    if args.dry_run:
        sender = DryRun()
    else:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat:
            print("Нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID (или запусти с --dry-run)", file=sys.stderr)
            return 2
        sender = Telegram(token, chat, link_preview=cfg["settings"].get("link_preview", True))

    if args.url:
        return rewrite_one(args.url, cfg, instructions, session, client, sender)

    state = State.load(Path(args.state))
    stats = run(cfg, topics, instructions, state, session, sender, client=client,
                bootstrap=not args.dry_run)
    logging.info("готово: %s", stats)
    if not args.dry_run:
        state.save()
    return 0


def rewrite_one(url, cfg, instructions, session, client, sender) -> int:
    if client is None:
        print("Для --url нужен ANTHROPIC_API_KEY", file=sys.stderr)
        return 2
    host = urlsplit(url).netloc.removeprefix("www.")
    names = {urlsplit(s["url"]).netloc.removeprefix("www."): s["name"] for s in cfg["sources"]}
    item = Item(source_id="manual", source_name=names.get(host, host), url=url, title="")
    text, full = get_article_text(session, item)
    if not full:
        print("Не удалось получить текст статьи (пейвол или блокировка).", file=sys.stderr)
        return 1
    item.title = text.split("\n", 1)[0][:200]
    st = cfg["settings"]
    post = write_post(client, st["write_model"], instructions, item, text, full,
                      st.get("signature", "@ExpressPT 🇵🇹"), st.get("source_link", "emoji"),
                      st.get("signature_html"))
    if post is None:
        print("Claude решил, что новость не подходит каналу.")
        return 0
    sender.send(post.html, buttons=[(f"{item.source_name} ↗", url)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
