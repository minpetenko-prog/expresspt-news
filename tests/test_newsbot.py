from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
from types import SimpleNamespace

import yaml

from newsbot.fetchers import Item, normalize_url, parse_html_list, parse_publico_json, parse_rss
from newsbot.picker import keyword_select
from newsbot.run import run
from newsbot.state import State
from newsbot.telegram import DryRun
from newsbot.writer import compose, md_to_tg_html

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>ECO</title>
<item><title>Governo aprova descida do IRS em 2027</title>
<link>https://eco.sapo.pt/2026/10/05/governo-irs/?utm_source=rss</link>
<pubDate>Mon, 05 Oct 2026 08:00:00 +0000</pubDate>
<description><![CDATA[<p>O Conselho de Ministros aprovou &amp; anunciou.</p>]]></description>
<content:encoded><![CDATA[<p>Texto completo da noticia.</p>]]></content:encoded></item>
<item><title>Benfica vence Sporting no dérbi</title><link>https://eco.sapo.pt/2026/10/05/derbi/</link>
<pubDate>Mon, 05 Oct 2026 08:30:00 +0000</pubDate><description>Golos.</description></item>
<item><title>Rendas vão subir 2,5% em 2027</title><link>https://eco.sapo.pt/2026/10/04/rendas/</link>
<pubDate>Sun, 04 Oct 2026 08:00:00 +0000</pubDate><description>Senhorios.</description></item>
</channel></rss>""".encode("utf-8")

SRC = {"id": "eco", "name": "ECO", "type": "rss", "url": "https://eco.sapo.pt/feed/"}


def test_parse_rss():
    items = parse_rss(RSS, SRC)
    assert len(items) == 3
    a = items[0]
    assert a.title == "Governo aprova descida do IRS em 2027"
    assert a.summary == "O Conselho de Ministros aprovou & anunciou."
    assert a.content == "Texto completo da noticia."
    assert a.published == datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)


def test_normalize_url_strips_tracking():
    assert normalize_url("https://www.eco.sapo.pt/a/b/?utm_source=x&id=3#top") == "https://eco.sapo.pt/a/b?id=3"
    assert Item("e", "E", "https://eco.sapo.pt/x/?utm_medium=a", "t").id == Item("e", "E", "http://www.eco.sapo.pt/x", "t").id


def test_parse_publico_json():
    data = [{"titulo": "Preço das casas sobe", "url": "/2026/10/05/economia/noticia/casas-123",
             "descricao": "INE divulga dados", "data": "2026-10-05T09:00:00Z", "isClosed": True}]
    (it,) = parse_publico_json(data, {"id": "publico", "name": "Público"})
    assert it.url == "https://www.publico.pt/2026/10/05/economia/noticia/casas-123"
    assert it.paywalled and it.summary == "INE divulga dados"
    assert it.published.hour == 9


def test_parse_html_list():
    html = """<html><body>
      <a href="/noticias/atualidade/aviso-amarelo-calor-distritos.html">Aviso amarelo devido ao calor em vários distritos</a>
      <a href="/noticias/atualidade/aviso-amarelo-calor-distritos.html"><img src="x.jpg"></a>
      <a href="/previsao/lisboa.html">Previsão para Lisboa hoje e amanhã aqui</a>
      <a href="/noticias/x/curto.html">Curto</a></body></html>"""
    src = {"id": "tempo", "name": "tempo.pt", "link_pattern": r"tempo\.pt/noticias/[a-z0-9-]+/[a-z0-9-]+\.html"}
    items = parse_html_list(html, "https://www.tempo.pt/", src)
    assert [i.url for i in items] == ["https://www.tempo.pt/noticias/atualidade/aviso-amarelo-calor-distritos.html"]


def test_keyword_select_and_exclude():
    topics = yaml.safe_load((ROOT / "topics.yaml").read_text(encoding="utf-8"))
    items = parse_rss(RSS, SRC)
    picks = keyword_select(items, topics)
    names = {p.item.title: p.topic for p in picks}
    assert names["Governo aprova descida do IRS em 2027"] == "Финансы и экономика"
    assert "Benfica vence Sporting no dérbi" not in names           # исключение
    assert names["Rendas vão subir 2,5% em 2027"] == "Финансы и экономика"  # акценты не мешают


def test_keyword_no_partial_word():
    topics = {"topics": [{"name": "Налоги", "keywords": ["iva"]}]}
    assert keyword_select([Item("a", "A", "https://x.pt/1", "Festival em Évora atrai visitantes")], topics) == []
    assert keyword_select([Item("a", "A", "https://x.pt/2", "Taxa de IVA desce")], topics)


def test_md_to_html_and_compose():
    assert md_to_tg_html("**A < B** & c **open")[0] == "<b>A &lt; B</b> &amp; c open"
    post = compose("💶", "💶 **Лид новости.**\n\nДетали **10%**.", "https://eco.sapo.pt/a?x=1&y=2", "@ExpressPT 🇵🇹")
    assert post.html == ('<a href="https://eco.sapo.pt/a?x=1&amp;y=2">💶</a> <b>Лид новости.</b>\n\n'
                         "Детали <b>10%</b>.\n\n@ExpressPT 🇵🇹")
    assert post.headline == "Лид новости."


# ---------- полный прогон с подменой сети и Claude ----------

class FakeClient:
    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **kw):
        assert "tools" not in kw and "tool_choice" not in kw  # Sonnet 5.5 их не поддерживает
        schema = kw["output_config"]["format"]["schema"]
        assert schema["additionalProperties"] is False
        tool = "select_news" if "selected" in schema["properties"] else "write_post"
        self.calls.append(tool)
        prompt = kw["messages"][0]["content"]
        if tool == "select_news":
            assert schema["properties"]["selected"]["items"]["additionalProperties"] is False
            # выбираем всё, где есть IRS или Rendas
            sel = []
            for line in prompt.splitlines():
                if line.startswith("[") and ("IRS" in line or "Rendas" in line):
                    sel.append({"i": int(line[1:line.index("]")]), "topic": "x", "priority": 1})
            out = {"selected": sel}
        else:
            assert "Издание: ECO" in prompt
            out = {"publish": True, "reason": "", "emoji": "💶",
                   "text": "**Правительство снизит IRS.**\n\nПодробности."}
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(out, ensure_ascii=False))])


def _cfg():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg["sources"] = [SRC, {"id": "dead", "name": "Dead", "type": "rss", "url": "https://dead"}]
    cfg["settings"]["alert_after_failures"] = 2
    return cfg


def _fetch_factory(extra=None):
    def fetch(session, src):
        if src["id"] == "dead":
            raise RuntimeError("timeout")
        items = parse_rss(RSS, src)
        return items + (extra or [])
    return fetch


def test_full_run(tmp_path):
    cfg, topics = _cfg(), yaml.safe_load((ROOT / "topics.yaml").read_text(encoding="utf-8"))
    state = State(tmp_path / "s.json")
    sender = DryRun()
    article = lambda session, item: ("Texto completo " * 60, True)

    # 1) первый запуск — только отметка «прочитано»
    st = run(cfg, topics, "INSTR", state, None, sender, client=FakeClient(), now=NOW,
             fetch=_fetch_factory(), article=article)
    assert st["sent"] == 0 and len(sender.sent) == 1 and "запущен" in sender.sent[0][0]
    state.save()

    # 2) появилась новая новость про IRS + дубль того же заголовка с другого URL
    new = Item("eco", "ECO", "https://eco.sapo.pt/2026/10/05/irs-jovem/", "IRS Jovem mantém-se em 2027",
               summary="Governo confirma", published=NOW - timedelta(hours=1))
    dup = Item("eco", "ECO", "https://eco.sapo.pt/outra/", "IRS Jovem mantém-se em 2027", published=NOW)
    state = State.load(tmp_path / "s.json")
    sender = DryRun()
    client = FakeClient()
    st = run(cfg, topics, "INSTR", state, None, sender, client=client, now=NOW,
             fetch=_fetch_factory([new, dup]), article=article)
    texts = [t for t, _ in sender.sent]
    assert any("Dead" in t and "не отвечает 2" in t for t in texts)   # предупреждение об источнике
    posts = [(t, b) for t, b in sender.sent if "@ExpressPT" in t]
    assert len(posts) == 1 and st["sent"] == 1
    html_text, buttons = posts[0]
    assert html_text.startswith('<a href="https://eco.sapo.pt/2026/10/05/irs-jovem/">💶</a> <b>')
    assert buttons is None  # без служебных кнопок — пост можно сразу пересылать в канал
    assert client.calls == ["select_news", "write_post"]
    state.save()

    # 3) повторный запуск — ничего нового
    state = State.load(tmp_path / "s.json")
    sender = DryRun()
    client = FakeClient()
    st = run(cfg, topics, "INSTR", state, None, sender, client=client, now=NOW,
             fetch=_fetch_factory([new, dup]), article=article)
    assert st["new"] == 0 and client.calls == [] and sender.sent == []


def test_overflow_is_postponed(tmp_path):
    cfg, topics = _cfg(), {"topics": []}
    cfg["settings"]["max_posts_per_run"] = 1
    extra = [Item("eco", "ECO", f"https://eco.sapo.pt/irs-{n}/", f"IRS noticia numero {n} sobre impostos",
                  published=NOW) for n in range(3)]
    state = State(tmp_path / "s.json")
    state.bootstrapped = True
    state.known_sources = ["eco"]
    article = lambda s, i: ("x" * 700, True)
    sender = DryRun()
    run(cfg, topics, "I", state, None, sender, client=FakeClient(), now=NOW,
        fetch=_fetch_factory(extra), article=article)
    assert len([t for t, _ in sender.sent if "@ExpressPT" in t]) == 1
    # остальные IRS-новости не помечены прочитанными — уйдут в следующий запуск
    assert sum(state.is_new(i) for i in extra) == 2


def test_no_llm_digest(tmp_path):
    cfg, topics = _cfg(), yaml.safe_load((ROOT / "topics.yaml").read_text(encoding="utf-8"))
    state = State(tmp_path / "s.json")
    state.bootstrapped = True
    state.known_sources = ["eco"]
    sender = DryRun()
    st = run(cfg, topics, "I", state, None, sender, client=None, now=NOW, fetch=_fetch_factory())
    digest = [t for t, _ in sender.sent if "Новости по темам" in t]
    assert len(digest) == 1 and st["sent"] == 1           # «Rendas» старше 12 часов, Benfica исключён
    assert "Governo aprova descida do IRS em 2027" in digest[0]


# ---------- исправления после первого деплоя ----------

def test_rss_link_from_guid_and_empty_feed_raises():
    raw = """<?xml version="1.0"?><rss version="2.0"><channel><title>CM</title>
    <item><title>Incêndio em Lisboa</title><guid>https://www.cmjornal.pt/portugal/detalhe/incendio</guid></item>
    </channel></rss>""".encode()
    (it,) = parse_rss(raw, {"id": "cm", "name": "CM"})
    assert it.url == "https://www.cmjornal.pt/portugal/detalhe/incendio"

    bad = """<?xml version="1.0"?><rss version="2.0"><channel><title>CM</title>
    <item><title>Sem link</title><guid isPermaLink="false">123</guid></item></channel></rss>""".encode()
    import pytest
    with pytest.raises(ValueError, match="ни одной со ссылкой"):
        parse_rss(bad, {"id": "cm", "name": "CM"})


def test_fallback_with_user_agent():
    from newsbot import fetchers

    class Resp:
        def __init__(self, status, content=b""):
            self.status_code, self.content = status, content
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"{self.status_code} Forbidden")

    class Session:
        def __init__(self):
            self.uas = []
        def get(self, url, timeout, headers=None):
            ua = (headers or {}).get("User-Agent")
            self.uas.append(ua)
            return Resp(200, RSS) if ua and "Feedly" in ua else Resp(403)

    src = {"id": "idealista", "name": "idealista", "type": "rss", "url": "https://i/rss",
           "fallback": [{"type": "rss", "url": "https://i/rss", "user_agent": "Feedly/1.0"}]}
    s = Session()
    items = fetchers.fetch_source(s, src)
    assert len(items) == 3 and s.uas == [None, "Feedly/1.0"]


def test_newly_working_source_is_onboarded_silently(tmp_path):
    cfg, topics = _cfg(), yaml.safe_load((ROOT / "topics.yaml").read_text(encoding="utf-8"))
    cfg["sources"] = [SRC, {"id": "cm", "name": "CM", "type": "rss", "url": "https://cm"}]
    # состояние «как после первого деплоя»: ECO уже видели, CM не работал, known_sources нет
    state = State(tmp_path / "s.json")
    state.bootstrapped = True
    state.mark_seen(parse_rss(RSS, SRC))
    cm_item = Item("cm", "CM", "https://cm/irs", "Governo aprova novo IRS para jovens", published=NOW)

    def fetch(session, src):
        return parse_rss(RSS, src) if src["id"] == "eco" else [cm_item]

    sender = DryRun()
    st = run(cfg, topics, "I", state, None, sender, client=None, now=NOW, fetch=fetch)
    assert st["sent"] == 0 and sender.sent == []          # архив CM не вывален в чат
    assert state.known_sources == ["cm", "eco"] and not state.is_new(cm_item)


def test_relative_links_and_title_strip():
    raw = """<?xml version="1.0"?><rss version="2.0"><channel><title>CM</title>
    <item><title>Fogo em Sintra</title><link>/portugal/detalhe/fogo-em-sintra</link>
    <guid isPermaLink="false">987</guid></item></channel></rss>""".encode()
    (it,) = parse_rss(raw, {"id": "cm", "name": "CM"}, "https://www.cmjornal.pt/rss")
    assert it.url == "https://www.cmjornal.pt/portugal/detalhe/fogo-em-sintra"

    gn = """<?xml version="1.0"?><rss version="2.0"><channel><title>GN</title>
    <item><title>Preço das casas sobe 5% - idealista/news</title>
    <link>https://news.google.com/rss/articles/CBMiabc?oc=5</link></item></channel></rss>""".encode()
    (it,) = parse_rss(gn, {"id": "idealista", "name": "idealista", "title_strip": r"\s+-\s+idealista.*$"})
    assert it.title == "Preço das casas sobe 5%"


# ---------- режим «готовые посты для мамы» ----------

def _api_setup(tmp_path, extra):
    cfg, topics = _cfg(), {"topics": []}
    state = State(tmp_path / "s.json")
    state.bootstrapped = True
    state.known_sources = ["eco"]
    return cfg, topics, state, _fetch_factory(extra)


def test_quiet_hours_postpone_everything(tmp_path):
    from newsbot.run import in_quiet_hours
    assert in_quiet_hours(datetime(2026, 10, 5, 23, 30, tzinfo=timezone.utc), "23-7")   # 00:30 Лиссабон
    assert not in_quiet_hours(datetime(2026, 10, 5, 6, 30, tzinfo=timezone.utc), "23-7")  # 07:30
    extra = [Item("eco", "ECO", "https://eco.sapo.pt/irs-n/", "IRS noticia", published=NOW)]
    cfg, topics, state, fetch = _api_setup(tmp_path, extra)
    night = datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc)
    sender = DryRun()
    run(cfg, topics, "I", state, None, sender, client=FakeClient(), now=night, fetch=fetch,
        article=lambda s, i: ("x" * 700, True))
    assert sender.sent == [] and state.is_new(extra[0])  # утром рассмотрим


def test_summary_only_is_skipped(tmp_path):
    extra = [Item("eco", "ECO", "https://eco.sapo.pt/irs-n/", "IRS noticia", published=NOW)]
    cfg, topics, state, fetch = _api_setup(tmp_path, extra)
    client = FakeClient()
    sender = DryRun()
    st = run(cfg, topics, "I", state, None, sender, client=client, now=NOW, fetch=fetch,
             article=lambda s, i: ("só o lead", False))
    assert st["sent"] == 0 and "write_post" not in client.calls and not state.is_new(extra[0])


def test_daily_cap(tmp_path):
    extra = [Item("eco", "ECO", f"https://eco.sapo.pt/irs-{n}/", f"IRS noticia numero {n}", published=NOW)
             for n in range(4)]
    cfg, topics, state, fetch = _api_setup(tmp_path, extra)
    cfg["settings"]["max_posts_per_day"] = 3
    for n in range(2):
        state.add_sent(Item("eco", "ECO", f"https://x/{n}", "old"), "старый пост", post=True)
    sender = DryRun()
    st = run(cfg, topics, "I", state, None, sender, client=FakeClient(), now=NOW, fetch=fetch,
             article=lambda s, i: ("x" * 700, True))
    assert st["sent"] == 1


# ---------- стиль @trueportugal ----------

TP_SIG = 'Оставайтесь с <a href="https://t.me/trueportugal">Португалия без розовых очков</a>🇵🇹'


def test_inline_source_link_italic_and_html_signature():
    text = ("**В Португалии предлагают ввести плату за пробки**\n\n"
            "AMT [предлагает](SOURCE) ввести **taxa de congestionamento** (сбор за перегруженность).\n\n"
            "*Теперь может появиться и у нас 😁*\n\nЯ за - 👍\nЯ против - 👎")
    post = compose("🚗", text, "https://www.razaoautomovel.com/x?a=1&b=2", "", "inline", TP_SIG)
    assert post.html.startswith("🚗 <b>В Португалии предлагают ввести плату за пробки</b>")
    assert 'AMT <a href="https://www.razaoautomovel.com/x?a=1&amp;b=2">предлагает</a> ввести' in post.html
    assert "<i>Теперь может появиться и у нас 😁</i>" in post.html
    assert post.html.endswith(TP_SIG)
    assert post.headline == "В Португалии предлагают ввести плату за пробки"


def test_inline_without_marker_falls_back_to_emoji_link():
    post = compose("🏠", "**Цены выросли на 7%**", "https://eco.sapo.pt/a", "", "inline", TP_SIG)
    assert post.html.startswith('<a href="https://eco.sapo.pt/a">🏠</a> <b>Цены выросли на 7%</b>')


def test_italic_does_not_break_multiplication_or_bullets():
    assert md_to_tg_html("2 * 3 = 6")[0] == "2 * 3 = 6"
    assert md_to_tg_html("• пункт")[0] == "• пункт"


def test_trueportugal_channel_end_to_end(tmp_path):
    tp = ROOT / "trueportugal"
    cfg = yaml.safe_load((tp / "config.yaml").read_text(encoding="utf-8"))
    topics = yaml.safe_load((tp / "topics.yaml").read_text(encoding="utf-8"))
    instructions = (tp / "INSTRUCTIONS.md").read_text(encoding="utf-8")
    assert {"lisboasecreta", "meteored", "razaoautomovel"} <= {s["id"] for s in cfg["sources"]}
    assert "cmjornal" not in {s["id"] for s in cfg["sources"]}
    cfg["sources"] = [SRC]

    class TPClient(FakeClient):
        def create(self, **kw):
            schema = kw["output_config"]["format"]["schema"]
            if "selected" in schema["properties"]:
                return super().create(**kw)
            assert "[слово](SOURCE)" in kw["system"][0]["text"]
            self.calls.append("write_post")
            out = {"publish": True, "reason": "", "emoji": "🚗",
                   "text": "**Плата за пробки**\n\nAMT [предлагает](SOURCE) сбор.\n\n*Посмотрим 😁*"}
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(out, ensure_ascii=False))])

    extra = [Item("eco", "ECO", "https://eco.sapo.pt/irs-x/", "IRS muda em 2027", published=NOW)]
    state = State(tmp_path / "tp.json")
    state.bootstrapped, state.known_sources = True, ["eco"]
    sender = DryRun()
    st = run(cfg, topics, instructions, state, None, sender, client=TPClient(), now=NOW,
             fetch=_fetch_factory(extra), article=lambda s, i: ("x" * 700, True))
    posts = [t for t, _ in sender.sent if "розовых очков" in t]
    assert st["sent"] >= 1 and posts
    assert posts[0].startswith("🚗 <b>Плата за пробки</b>")
    assert '<a href="https://' in posts[0] and ">предлагает</a>" in posts[0]
    assert posts[0].endswith('Оставайтесь с <a href="https://t.me/trueportugal">Португалия без розовых очков</a>🇵🇹')


def test_wp_json_and_feed_diagnostics():
    from newsbot.fetchers import parse_wp_json
    import pytest
    data = [{"title": {"rendered": "Lisboa &#8211; destino mais feliz"}, "link": "https://lisboasecreta.co/x/",
             "date_gmt": "2026-10-05T11:24:59", "excerpt": {"rendered": "<p>Resumo</p>"},
             "content": {"rendered": "<p>" + "Texto completo. " * 50 + "</p>"}}]
    (it,) = parse_wp_json(data, {"id": "ls", "name": "Lisboa Secreta"})
    assert it.title == "Lisboa – destino mais feliz" and it.published.hour == 11 and len(it.content) > 600
    with pytest.raises(ValueError, match="Just a moment"):
        parse_rss(b"<!DOCTYPE html><html><head><title>Just a moment...</title></head><body><p></div></body></html>",
                  {"id": "ls", "name": "LS"})


def test_rss2json():
    from newsbot.fetchers import parse_rss2json
    data = {"status": "ok", "items": [{"title": "Mosteiro reabre", "link": "https://lisboasecreta.co/m/",
            "pubDate": "2026-10-05 11:24:59", "description": "<p>Resumo</p>", "content": "<p>Texto</p>"}]}
    (it,) = parse_rss2json(data, {"id": "ls", "name": "Lisboa Secreta"})
    assert it.url == "https://lisboasecreta.co/m/" and it.published.hour == 11 and it.content == "Texto"


def test_writer_sees_recent_posts_for_dedupe(tmp_path):
    seen_prompts = []

    class DedupClient(FakeClient):
        def create(self, **kw):
            seen_prompts.append(kw["messages"][0]["content"])
            return super().create(**kw)

    extra = [Item("eco", "ECO", "https://eco.sapo.pt/irs-new/", "IRS noticia nova", published=NOW)]
    cfg, topics, state, fetch = _api_setup(tmp_path, extra)
    state.add_sent(Item("sapo", "SAPO", "https://x/1", "old"), "IPMA объявила оранжевый уровень", post=True)
    run(cfg, topics, "I", state, None, DryRun(), client=DedupClient(), now=NOW, fetch=fetch,
        article=lambda s, i: ("x" * 700, True))
    write_prompts = [p for p in seen_prompts if p.startswith("Издание:")]
    assert write_prompts and "IPMA объявила оранжевый уровень" in write_prompts[0] and "повтор" in write_prompts[0]


def test_long_post_is_shortened():
    from newsbot.writer import write_post, visible_len
    calls = []

    class LongThenShort:
        def __init__(self):
            self.messages = self

        def create(self, **kw):
            prompt = kw["messages"][0]["content"]
            calls.append(prompt)
            text = ("**Лид.**\n\n" + "Деталь. " * 200) if len(calls) == 1 else "**Лид.**\n\nКоротко."
            out = {"publish": True, "reason": "", "emoji": "💶", "text": text}
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(out, ensure_ascii=False))])

    it = Item("eco", "ECO", "https://eco.sapo.pt/a", "T", published=NOW)
    post = write_post(LongThenShort(), "m", "I", it, "x" * 700, True, "@ExpressPT 🇵🇹", max_chars=850)
    assert len(calls) == 2 and "слишком длинный" in calls[1]
    assert "Коротко." in post.html and "Деталь" not in post.html
    assert visible_len("**Лид** [слово](SOURCE) *к*") == len("Лид слово к")
