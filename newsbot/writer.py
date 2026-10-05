"""Переписывание новости в пост канала и сборка Telegram-HTML."""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass

from .fetchers import Item
from .llm import call_tool

log = logging.getLogger(__name__)

WRITE_TOOL = {
    "name": "write_post",
    "description": "Вернуть готовый пост для канала или отказ публиковать.",
    "input_schema": {
        "type": "object",
        "properties": {
            "publish": {"type": "boolean", "description": "false — если новость не подходит каналу"},
            "reason": {"type": "string", "description": "почему не подходит (если publish=false), иначе пустая строка"},
            "emoji": {"type": "string", "description": "одно эмодзи по теме"},
            "text": {"type": "string",
                     "description": "текст поста БЕЗ эмодзи в начале и без подписи; жирный — **так**, "
                                    "курсив — *так*; абзацы через пустую строку"},
        },
        "required": ["publish", "reason", "emoji", "text"],
    },
}


@dataclass
class Post:
    html: str
    headline: str  # первая фраза без разметки — для истории и защиты от повторов


def build_write_prompt(item: Item, text: str, full: bool, published_str: str,
                       recent: list[str] | None = None) -> str:
    note = ("" if full else
            "\nВНИМАНИЕ: полного текста нет (платная статья или сайт недоступен), есть только "
            "заголовок и анонс. Напиши короткий пост из 2–3 предложений строго по этим данным.\n")
    recent_txt = ""
    if recent:
        recent_txt = ("\nУже опубликовано в канале за последние дни:\n" +
                      "\n".join(f"- {h}" for h in recent[-40:]) +
                      "\nЕсли эта новость — о той же ситуации, что одна из уже опубликованных, ответь "
                      "publish=false с причиной «повтор». Это касается и других изданий, и «обновлений»: "
                      "новые округа в том же предупреждении IPMA, новые цифры той же забастовки, "
                      "очередные подробности того же события — всё это повтор. Исключение — только "
                      "резкое изменение главного (например, предупреждение стало красным, забастовку "
                      "отменили, решение окончательно приняли).\n")
    return f"""Издание: {item.source_name}
Ссылка: {item.url}
Дата публикации: {published_str}
Заголовок: {item.title}
{note}
Текст:
{text}

{recent_txt}
Напиши пост для канала по инструкциям. Ответ — JSON с полями publish, reason, emoji, text."""


EMOJI_PREFIX = re.compile(r"^\s*(?:[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]|"
                          r"[\U0001F1E6-\U0001F1FF]{2})+\s*")


SOURCE_MARK = re.compile(r"\[([^\]\n]{1,80})\]\((?:SOURCE|ИСТОЧНИК|source)\)")
ITALIC = re.compile(r"(?<![*\w])\*(?![\s*])([^*\n]+?)(?<!\s)\*(?![*\w])")


def md_to_tg_html(text: str, source_url: str | None = None) -> tuple[str, bool]:
    """**жирный** → <b>, *курсив* → <i>, [слово](SOURCE) → ссылка на источник.
    Остальное экранируется. Возвращает (html, поставлена_ли_ссылка_на_источник)."""
    out = html.escape(text.strip(), quote=False)
    placed = False
    if source_url:
        href = html.escape(source_url, quote=True)
        out, n = SOURCE_MARK.subn(lambda m: f'<a href="{href}">{m.group(1)}</a>', out, count=1)
        placed = n > 0
    out = SOURCE_MARK.sub(r"\1", out)  # лишние метки — просто текстом
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.S)
    out = out.replace("**", "")
    out = ITALIC.sub(r"<i>\1</i>", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out, placed


def compose(emoji: str, text: str, url: str, signature: str, source_link: str = "emoji",
            signature_html: str | None = None, signature_inline: bool = False) -> Post:
    """source_link: "emoji" — ссылка на эмодзи в начале (как в @ExpressPT);
    "inline" — ссылка на слово, которое модель пометила [слово](SOURCE) (как в @trueportugal).
    Если модель забыла пометить слово, ссылка ставится на эмодзи, чтобы источник не потерялся."""
    emoji = (emoji or "🇵🇹").strip().split()[0]
    body = EMOJI_PREFIX.sub("", text.strip())  # если модель всё же поставила эмодзи в начало
    linked_emoji = f'<a href="{html.escape(url, quote=True)}">{emoji}</a>'
    if source_link == "inline":
        body_html, placed = md_to_tg_html(body, url)
        head = emoji if placed else linked_emoji
    else:
        body_html, _ = md_to_tg_html(body)
        head = linked_emoji
    sig = signature_html if signature_html else html.escape(signature)
    # signature_inline: подпись в конце последней строки, как в @banksta
    post_html = f"{head} {body_html} {sig}" if signature_inline else f"{head} {body_html}\n\n{sig}"
    first_par = SOURCE_MARK.sub(r"\1", body.split("\n\n")[0]).replace("**", "").replace("*", "").strip()
    return Post(html=post_html, headline=first_par[:200])


def visible_len(text: str) -> int:
    """Длина текста так, как его увидит читатель (без разметки)."""
    return len(SOURCE_MARK.sub(r"\1", text).replace("**", "").replace("*", "").strip())


SHORTEN_PROMPT = """Этот пост слишком длинный: {length} знаков, а нужно не больше {limit}.
Сократи его до {target}–{limit} знаков. Сохрани эмодзи, оформление и ссылку
вида [слово](SOURCE), если она есть. Выбрось второстепенное: перечни названий, мелкие цифры,
цитаты, историю вопроса. Оставь главное и то, что важно читателю.

Пост:
{text}

Ответ — JSON с полями publish (true), reason (""), emoji, text."""


def write_post(client, model: str, instructions: str, item: Item, text: str, full: bool,
               signature: str, source_link: str = "emoji", signature_html: str | None = None,
               recent: list[str] | None = None, max_chars: int | None = None,
               signature_inline: bool = False) -> Post | None:
    published = item.published.strftime("%d.%m.%Y %H:%M UTC") if item.published else "неизвестна"
    result = call_tool(client, model, instructions,
                       build_write_prompt(item, text, full, published, recent), WRITE_TOOL,
                       max_tokens=2500)
    if not result.get("publish") or not (result.get("text") or "").strip():
        log.info("пропущено «%s»: %s", item.title, result.get("reason", "—"))
        return None
    length = visible_len(result["text"])
    if max_chars and length > max_chars:
        log.info("пост %d знаков при лимите %d — сокращаю", length, max_chars)
        shorter = call_tool(client, model, instructions,
                            SHORTEN_PROMPT.format(length=length, limit=max_chars,
                                                  target=int(max_chars * 0.6), text=result["text"]),
                            WRITE_TOOL, max_tokens=2000)
        if (shorter.get("text") or "").strip() and visible_len(shorter["text"]) < length:
            result = {**result, **{k: shorter[k] for k in ("emoji", "text") if shorter.get(k)}}
    return compose(result.get("emoji", ""), result["text"], item.url, signature,
                   source_link, signature_html, signature_inline)
