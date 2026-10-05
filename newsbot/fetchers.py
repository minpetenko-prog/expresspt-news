"""Сбор новостей из RSS, JSON-API Público и HTML-страниц со списком статей."""
from __future__ import annotations

import email.utils
import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
TIMEOUT = 20
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|ref$|ref_|xtor|ns_|cmpid|ito$)", re.I)


@dataclass
class Item:
    source_id: str
    source_name: str
    url: str
    title: str
    summary: str = ""
    content: str = ""  # полный текст, если лента его отдаёт
    published: datetime | None = None
    paywalled: bool = False

    @property
    def id(self) -> str:
        return hashlib.sha1(normalize_url(self.url).encode()).hexdigest()[:16]

    @property
    def title_key(self) -> str:
        return title_key(self.title)


# ---------- утилиты ----------

def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not TRACKING_PARAMS.match(k)]
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(query), ""))


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def title_key(title: str) -> str:
    s = strip_accents(title.lower())
    return re.sub(r"[^a-z0-9]+", "", s)[:80]


def clean_text(html: str | None) -> str:
    if not html:
        return ""
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def parse_date(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        ts = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    s = str(value).strip()
    for parser in (
        lambda x: datetime.fromisoformat(x.replace("Z", "+00:00")),
        email.utils.parsedate_to_datetime,
    ):
        try:
            dt = parser(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, IndexError):
            continue
    return None


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.6",
        "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
    })
    return s


def http_get(session: requests.Session, url: str, user_agent: str | None = None) -> requests.Response:
    headers = {"User-Agent": user_agent} if user_agent else None
    resp = session.get(url, timeout=TIMEOUT, headers=headers)
    resp.raise_for_status()
    return resp


# ---------- парсеры ----------

def parse_rss(raw: bytes, src: dict, base_url: str = "") -> list[Item]:
    feed = feedparser.parse(raw)
    title_strip = re.compile(src["title_strip"]) if src.get("title_strip") else None
    if not feed.entries:
        raise ValueError(f"в ленте нет записей ({feed.get('bozo_exception', 'пусто')})")
    items = []
    for e in feed.entries:
        link = re.sub(r"#utm_.*$", "", _entry_link(e, base_url or src.get("url", "")))
        title = clean_text(e.get("title")) or clean_text(e.get("summary"))[:160]
        if title_strip:
            title = title_strip.sub("", title).strip()
        if not link or not title:
            continue
        content = ""
        if e.get("content"):
            content = clean_text(e.content[0].get("value"))
        published = None
        for key in ("published_parsed", "updated_parsed"):
            if e.get(key):
                published = datetime(*e[key][:6], tzinfo=timezone.utc)
                break
        items.append(Item(
            source_id=src["id"], source_name=src["name"], url=link, title=title,
            summary=clean_text(e.get("summary"))[:1500], content=content, published=published,
            # через Google News полный текст статьи не получить
            paywalled="news.google.com" in link,
        ))
    if not items:
        first = feed.entries[0]
        raise ValueError(f"в ленте {len(feed.entries)} записей, но ни одной со ссылкой и заголовком "
                         f"(пример: link={str(first.get('link'))[:80]!r}, "
                         f"title={str(first.get('title'))[:60]!r}, id={str(first.get('id'))[:60]!r})")
    return items


def _entry_link(e, base_url: str = "") -> str:
    """Ссылка на статью: link, затем links[], затем guid. Относительные ссылки достраиваются."""
    cands = [e.get("link")] + [l.get("href") for l in e.get("links", [])] + [e.get("id")]
    for cand in cands:
        if cand and str(cand).strip().startswith("http"):
            return str(cand).strip()
    if base_url:
        for cand in cands[:-1]:  # guid без http — обычно просто номер, не ссылка
            c = str(cand or "").strip()
            if c.startswith("//"):
                return "https:" + c
            if re.match(r"^(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+/", c, re.I):
                return "https://" + c
            if c.startswith("/") or re.match(r"^[\w-]+/", c):
                return urljoin(base_url, c)
    return ""


def _find_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "data", "results", "noticias", "list", "content"):
            if isinstance(data.get(key), list):
                return data[key]
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def _first(d: dict, *keys):
    for k in keys:
        if d.get(k):
            return d[k]
    return None


def parse_publico_json(data, src: dict) -> list[Item]:
    rows = _find_list(data)
    if not rows:
        raise ValueError("в JSON не найден список новостей")
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = clean_text(_first(row, "titulo", "title", "tituloNoticia", "name"))
        url = _first(row, "url", "fullUrl", "link", "shareUrl")
        if not title or not url:
            continue
        items.append(Item(
            source_id=src["id"], source_name=src["name"],
            url=urljoin("https://www.publico.pt/", url), title=title,
            summary=clean_text(_first(row, "descricao", "lead", "description", "subtitulo")),
            published=parse_date(_first(row, "data", "dataPublicacao", "publishedAt", "date",
                                         "data_publicacao", "dataPub")),
            paywalled=bool(row.get("isClosed")),
        ))
    return items


def parse_html_list(html: str, base_url: str, src: dict, limit: int = 40) -> list[Item]:
    pattern = re.compile(src["link_pattern"])
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"]).split("#")[0]
        if not pattern.search(url):
            continue
        title = a.get_text(" ", strip=True) or a.get("title") or a.get("aria-label") or ""
        title = re.sub(r"\s+", " ", title).strip()
        if len(title) < 20:
            title = found.get(url, title)
        if len(title) >= len(found.get(url, "")):
            found[url] = title
    items = [
        Item(source_id=src["id"], source_name=src["name"], url=u, title=t)
        for u, t in found.items() if len(t) >= 20
    ]
    if not items:
        raise ValueError("на странице не найдено ссылок на статьи")
    return items[:limit]


def fetch_one(session: requests.Session, spec: dict, src: dict) -> list[Item]:
    resp = http_get(session, spec["url"], spec.get("user_agent"))
    kind = spec["type"]
    if kind == "rss":
        return parse_rss(resp.content, {**src, **spec}, spec["url"])
    if kind == "publico_json":
        return parse_publico_json(resp.json(), src)
    if kind == "html":
        return parse_html_list(resp.text, spec["url"], {**src, **spec})
    raise ValueError(f"неизвестный тип источника: {kind}")


def fetch_source(session: requests.Session, src: dict) -> list[Item]:
    """Пробует основной способ, затем запасные. Бросает исключение, если не сработал ни один."""
    specs = [{k: src[k] for k in ("type", "url", "link_pattern", "user_agent", "title_strip") if k in src}]
    specs += src.get("fallback", [])
    errors = []
    for spec in specs:
        try:
            items = fetch_one(session, spec, src)
            if not items:
                raise ValueError("0 новостей")
            log.info("%s: %d новостей (%s)", src["id"], len(items), spec["url"])
            return items
        except Exception as exc:  # noqa: BLE001 — любой сбой источника не должен ронять запуск
            errors.append(f"{spec['url']}: {exc}")
            log.warning("%s: не удалось %s — %s", src["id"], spec["url"], exc)
    raise RuntimeError("; ".join(errors))
