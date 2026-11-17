from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from kwanttrades.data import news
from kwanttrades.data.news import CompositeNewsProvider, dedupe_sort, normalise_headline, parse_rss, parse_yf_news
from kwanttrades.types import NY, NewsItem, NewsProvider

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=NY)


def rfc(dt):
    return format_datetime(dt.astimezone(timezone.utc))


def rss(items):
    body = "".join(
        f"<item><title>{t}</title><link>https://x.test/{i}</link><pubDate>{rfc(d)}</pubDate>"
        f"<description>&lt;p&gt;Sum {i}&lt;/p&gt;</description>{s}</item>"
        for i, (t, d, s) in enumerate(items)
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


def test_parse_rss_basic():
    xml = rss([("Apple &amp; Co beats estimates", NOW - timedelta(hours=1), ""),
               ("No date item", NOW, "")])
    items = parse_rss(xml, "AAPL", default_source="Yahoo Finance")
    assert len(items) == 2
    assert items[0].headline == "Apple & Co beats estimates"
    assert items[0].summary == "Sum 0" and items[0].source == "Yahoo Finance"
    assert items[0].published.tzinfo is not None and items[0].published == NOW - timedelta(hours=1)
    assert items[0].symbol == "AAPL"


def test_parse_rss_google_source_suffix():
    xml = rss([("Tesla recall widens - Reuters", NOW, "<source url='https://reuters.com'>Reuters</source>")])
    (it,) = parse_rss(xml, "TSLA", strip_source_suffix=True)
    assert it.headline == "Tesla recall widens" and it.source == "Reuters"


def test_parse_rss_rejects_bad_and_entity_documents():
    with pytest.raises(ValueError):
        parse_rss(b"<rss><channel>", "X")
    bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>'
    with pytest.raises(ValueError):
        parse_rss(bomb, "X")


def test_yfinance_flat_format():
    ts = int((NOW - timedelta(hours=2)).timestamp())
    flat = [{"title": "SPY hits record high", "publisher": "Reuters", "link": "https://r.test/1",
             "providerPublishTime": ts, "relatedTickers": ["SPY"]},
            {"title": "", "providerPublishTime": ts}, "junk", {"title": "no time"}]
    (it,) = parse_yf_news(flat, "SPY")
    assert it.headline == "SPY hits record high" and it.source == "Reuters" and it.url == "https://r.test/1"
    assert it.published == NOW - timedelta(hours=2)


def test_yfinance_nested_format():
    nested = [{"id": "1", "content": {
        "title": "Nvidia unveils new chip", "summary": "<b>Big</b> chip",
        "pubDate": (NOW - timedelta(hours=3)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": {"displayName": "Motley Fool"},
        "canonicalUrl": {"url": "https://mf.test/1"}}},
        {"content": {"title": "Fallback url", "displayTime": "2026-11-13T15:00:00Z", "clickThroughUrl": {"url": "https://c.test"}}}]
    a, b = parse_yf_news(nested, "NVDA")
    assert a.headline == "Nvidia unveils new chip" and a.source == "Motley Fool" and a.summary == "Big chip"
    assert a.url == "https://mf.test/1" and a.published == NOW - timedelta(hours=3)
    assert b.url == "https://c.test"
    assert parse_yf_news(None, "X") == []


def item(h, hours_ago, summary=""):
    return NewsItem("SPY", h, NOW - timedelta(hours=hours_ago), "s", "", summary)


def test_dedupe_normalisation_and_order():
    items = [item("Stocks Rally!", 3), item("stocks rally", 1, "has summary"), item("Other story", 2)]
    out = dedupe_sort(items)
    assert [i.headline for i in out] == ["stocks rally", "Other story"]
    assert out[0].published >= out[1].published
    assert normalise_headline("  Stocks, RALLY!! ") == "stocks rally"


def make_provider(fetch=None, yf=None, **kw):
    return CompositeNewsProvider(fetch=fetch, yf_news=yf or (lambda s: []), clock=lambda: NOW, **kw)


def test_composite_merges_dedupes_filters_and_sorts():
    ts_old = int((NOW - timedelta(hours=40)).timestamp())
    ts_new = int((NOW - timedelta(hours=5)).timestamp())
    yf = lambda s: [{"title": "Shared headline", "providerPublishTime": ts_new, "publisher": "A"},
                    {"title": "Ancient news", "providerPublishTime": ts_old}]
    urls = []

    def fetch(url):
        urls.append(url)
        if "yahoo" in url:
            return rss([("Shared headline", NOW - timedelta(hours=5), ""), ("Yahoo only", NOW - timedelta(hours=1), "")])
        return rss([("Google story - Foo", NOW - timedelta(hours=2), "<source>Foo</source>"),
                    ("Future dated", NOW + timedelta(days=3), ""), ("Stale", NOW - timedelta(hours=30), "")])

    out = make_provider(fetch, yf).headlines("spy", lookback_hours=24)
    heads = [i.headline for i in out]
    assert heads == ["Yahoo only", "Google story", "Shared headline"]
    assert isinstance(make_provider(), NewsProvider)
    assert any("s=SPY" in u for u in urls) and any("news.google.com" in u for u in urls)


def test_each_source_failure_is_skipped(caplog):
    def yf(s):
        raise RuntimeError("YAHOO IS DOWN")

    def fetch(url):
        if "yahoo" in url:
            raise TimeoutError("timed out")
        return rss([("Survivor", NOW - timedelta(hours=1), "")])

    with caplog.at_level("WARNING"):
        out = make_provider(fetch, yf).headlines("AAPL")
    assert [i.headline for i in out] == ["Survivor"]
    assert "yfinance failed" in caplog.text and "yahoo_rss failed" in caplog.text
    assert make_provider(lambda u: b"garbage", yf).headlines("AAPL") == []


def test_odd_symbol_rejected_and_market_wide():
    called = []
    p = make_provider(lambda u: called.append(u) or rss([]))
    assert p.headlines("AAPL&evil=1") == []
    assert called == []
    p.headlines("")
    assert len(called) == 1 and "stock+market" in called[0]  # market-wide: Google only


def test_http_get_uses_generic_user_agent(monkeypatch):
    seen = {}

    class Resp:
        def read(self, n):
            return b"<rss/>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        seen["headers"] = dict(req.header_items())
        seen["timeout"] = timeout
        return Resp()

    monkeypatch.setattr(news.urllib.request, "urlopen", fake_urlopen)
    news.http_get("https://example.test/feed")
    assert seen["timeout"] == 5.0
    assert set(k.lower() for k in seen["headers"]) <= {"user-agent", "accept"}
    assert "Mozilla/5.0" in seen["headers"]["User-agent"]
