"""Tests for the GDELT global macro/geopolitical news fetcher.

No test here requires network — GDELT HTTP is mocked.
"""

import sys
from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import macro_news
from providers.base import NewsItem


def _resp(payload):
    m = MagicMock(spec=httpx.Response)
    m.status_code = 200
    m.json.return_value = payload
    m.raise_for_status = MagicMock()
    return m


GDELT_PAYLOAD = {
    "articles": [
        {
            "url": "https://www.reuters.com/a",
            "title": "Oil spikes as Iran closes Strait of Hormuz",
            "seendate": "20260623T120000Z",
            "domain": "reuters.com",
            "language": "English",
            "sourcecountry": "United States",
        },
        {
            "url": "https://apnews.com/b",
            "title": "Fed signals rates on hold amid inflation",
            "seendate": "20260623T090000Z",
            "domain": "apnews.com",
            "language": "English",
            "sourcecountry": "United States",
        },
    ]
}


def test_fetch_macro_news_parses_gdelt_articles(tmp_path):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(url=url, params=params, headers=headers)
        return _resp(GDELT_PAYLOAD)

    with patch("macro_news.httpx.get", side_effect=fake_get):
        items = macro_news.fetch_macro_news(
            timespan="24h", limit=20, cache_dir=tmp_path
        )

    assert captured["url"].startswith("https://api.gdeltproject.org/api/v2/doc/doc")
    assert captured["params"]["mode"] == "artlist"
    assert captured["params"]["format"] == "json"
    assert captured["params"]["timespan"] == "24h"
    assert captured["params"]["maxrecords"] == 20
    # GDELT 429s naive clients without a User-Agent.
    assert "User-Agent" in captured["headers"]

    assert [i.headline for i in items] == [
        "Oil spikes as Iran closes Strait of Hormuz",
        "Fed signals rates on hold amid inflation",
    ]
    assert items[0].source == "reuters.com"
    assert items[0].date == "2026-06-23"  # parsed from seendate
    assert items[0].url == "https://www.reuters.com/a"
    assert items[0].sentiment_score is None  # artlist carries no per-item tone


def test_fetch_macro_news_uses_fresh_cache(tmp_path):
    with patch("macro_news.httpx.get", return_value=_resp(GDELT_PAYLOAD)) as g:
        macro_news.fetch_macro_news(timespan="24h", limit=20, cache_dir=tmp_path)
        assert g.call_count == 1
    # Within TTL → served from cache, no second HTTP call.
    with patch("macro_news.httpx.get", return_value=_resp(GDELT_PAYLOAD)) as g2:
        items = macro_news.fetch_macro_news(
            timespan="24h", limit=20, cache_dir=tmp_path
        )
        assert g2.call_count == 0
        assert len(items) == 2


def test_fetch_macro_news_serves_stale_cache_on_error(tmp_path):
    # Populate the cache.
    with patch("macro_news.httpx.get", return_value=_resp(GDELT_PAYLOAD)):
        macro_news.fetch_macro_news(timespan="24h", limit=20, cache_dir=tmp_path)
    # Force a refetch (ttl=0) that fails (e.g. 429) → serve the stale cache.
    with patch("macro_news.httpx.get", side_effect=httpx.HTTPError("429 rate limited")):
        items = macro_news.fetch_macro_news(
            timespan="24h", limit=20, cache_dir=tmp_path, ttl_seconds=0, max_attempts=1
        )
    assert [i.headline for i in items] == [
        "Oil spikes as Iran closes Strait of Hormuz",
        "Fed signals rates on hold amid inflation",
    ]


def test_fetch_macro_news_empty_on_error_without_cache(tmp_path):
    with patch("macro_news.httpx.get", side_effect=httpx.HTTPError("boom")):
        items = macro_news.fetch_macro_news(
            timespan="24h", limit=20, cache_dir=tmp_path, ttl_seconds=0, max_attempts=1
        )
    assert items == []


def test_fetch_macro_news_retries_then_succeeds(tmp_path):
    calls = {"n": 0}

    def flaky_get(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.HTTPError("429 rate limited")
        return _resp(GDELT_PAYLOAD)

    with (
        patch("macro_news.httpx.get", side_effect=flaky_get),
        patch("macro_news.time.sleep") as slept,
    ):
        items = macro_news.fetch_macro_news(
            timespan="24h", limit=20, cache_dir=tmp_path, ttl_seconds=0
        )
    assert len(items) == 2
    assert calls["n"] == 2  # retried once after the 429
    slept.assert_called_once()  # waited between attempts (5s cadence)


def test_format_macro_for_prompt_renders_headlines():
    items = [
        NewsItem(
            headline="Oil spikes as Iran closes Strait of Hormuz",
            source="reuters.com",
            date="2026-06-23",
            url="https://www.reuters.com/a",
        )
    ]
    block = macro_news.format_macro_for_prompt(items)
    assert "Oil spikes as Iran closes Strait of Hormuz" in block
    assert "reuters.com" in block


def test_format_macro_for_prompt_empty():
    assert "no macro" in macro_news.format_macro_for_prompt([]).lower()


# --- RSS fallback + orchestrator ------------------------------------------- #


def _rss_resp(xml: str):
    m = MagicMock(spec=httpx.Response)
    m.status_code = 200
    m.content = xml.encode("utf-8")
    m.raise_for_status = MagicMock()
    return m


def _rss_xml(recent_pubdate: str, old_pubdate: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>BBC Business</title>'
        f"<item><title>Oil jumps as Hormuz tension flares</title>"
        f"<link>https://www.bbc.com/news/1</link><pubDate>{recent_pubdate}</pubDate></item>"
        f"<item><title>Stale macro story</title>"
        f"<link>https://www.bbc.com/news/2</link><pubDate>{old_pubdate}</pubDate></item>"
        "</channel></rss>"
    )


def test_fetch_rss_macro_news_parses_and_filters_old():
    recent = format_datetime(datetime.now().astimezone() - timedelta(days=1))
    old = format_datetime(datetime.now().astimezone() - timedelta(days=400))
    with patch("macro_news.httpx.get", return_value=_rss_resp(_rss_xml(recent, old))):
        items = macro_news.fetch_rss_macro_news(
            feeds=(("BBC", "http://x"),), limit=20, since_days=7
        )
    assert [i.headline for i in items] == ["Oil jumps as Hormuz tension flares"]
    assert items[0].source == "BBC"
    assert items[0].url == "https://www.bbc.com/news/1"


def test_fetch_rss_macro_news_skips_failed_feed():
    with patch("macro_news.httpx.get", side_effect=httpx.HTTPError("down")):
        items = macro_news.fetch_rss_macro_news(feeds=(("BBC", "http://x"),), limit=5)
    assert items == []  # feed error → skipped, never raises


def test_get_macro_news_prefers_rss():
    rss_items = [NewsItem(headline="R", source="BBC", date="2026-06-23", url="r")]
    with (
        patch("macro_news.fetch_rss_macro_news", return_value=rss_items),
        patch("macro_news.fetch_macro_news") as gdelt,
    ):
        items, source = macro_news.get_macro_news()
    assert source == "rss"
    assert items == rss_items
    gdelt.assert_not_called()  # RSS had items → GDELT not queried


def test_get_macro_news_falls_back_to_gdelt_when_rss_empty():
    gdelt_items = [
        NewsItem(headline="G", source="reuters.com", date="2026-06-23", url="g")
    ]
    with (
        patch("macro_news.fetch_rss_macro_news", return_value=[]),
        patch("macro_news.fetch_macro_news", return_value=gdelt_items),
    ):
        items, source = macro_news.get_macro_news()
    assert source == "gdelt"
    assert items == gdelt_items


def test_timespan_to_days_maps_to_rss_window():
    assert macro_news._timespan_to_days("24h") == 1
    assert macro_news._timespan_to_days("1h") == 1
    assert macro_news._timespan_to_days("15min") == 1  # minutes, not 15 days
    assert macro_news._timespan_to_days("3d") == 3
    assert macro_news._timespan_to_days("2w") == 14
    assert macro_news._timespan_to_days("2m") == 60  # months, not 2 days
    assert macro_news._timespan_to_days("1y") == 365
    # GDELT word units, not just short forms:
    assert macro_news._timespan_to_days("24hours") == 1
    assert macro_news._timespan_to_days("10minutes") == 1
    assert macro_news._timespan_to_days("2weeks") == 14
    assert macro_news._timespan_to_days("2months") == 60
    assert macro_news._timespan_to_days("3days") == 3
    assert macro_news._timespan_to_days("garbage") == 2


def test_fetch_macro_news_fetches_when_cache_dir_unmakeable(tmp_path):
    """A read-only/un-creatable cache dir must not abort the fetch (never raises)."""
    with (
        patch("macro_news.Path.mkdir", side_effect=OSError("read-only")),
        patch("macro_news.httpx.get", return_value=_resp(GDELT_PAYLOAD)),
    ):
        items = macro_news.fetch_macro_news(
            timespan="24h", limit=20, cache_dir=tmp_path, ttl_seconds=0
        )
    assert len(items) == 2


def test_fetch_macro_news_refetches_when_cache_missing_items(tmp_path):
    """A fresh cache file lacking an items list must trigger a refetch, not []."""
    path = macro_news._cache_file(tmp_path, macro_news.DEFAULT_MACRO_QUERY, "24h", 20)
    path.write_text(
        '{"fetched_at": 9999999999}', encoding="utf-8"
    )  # fresh but no items
    with patch("macro_news.httpx.get", return_value=_resp(GDELT_PAYLOAD)) as g:
        items = macro_news.fetch_macro_news(
            query=macro_news.DEFAULT_MACRO_QUERY,
            timespan="24h",
            limit=20,
            cache_dir=tmp_path,
        )
    assert g.call_count == 1  # malformed fresh cache did not suppress the fetch
    assert len(items) == 2


def test_fetch_macro_news_returns_items_when_cache_write_fails(tmp_path):
    """A cache-write error (e.g. read-only dir) must not discard fetched items."""
    with (
        patch("macro_news.httpx.get", return_value=_resp(GDELT_PAYLOAD)),
        patch("macro_news.Path.write_text", side_effect=OSError("read-only")),
    ):
        items = macro_news.fetch_macro_news(
            timespan="24h", limit=20, cache_dir=tmp_path, ttl_seconds=0
        )
    assert len(items) == 2  # freshly fetched, returned despite cache-write failure


def test_fetch_macro_news_clamps_maxrecords_to_250(tmp_path):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(params=params)
        return _resp(GDELT_PAYLOAD)

    with patch("macro_news.httpx.get", side_effect=fake_get):
        macro_news.fetch_macro_news(
            timespan="24h", limit=500, cache_dir=tmp_path, ttl_seconds=0
        )
    assert captured["params"]["maxrecords"] == 250  # GDELT's cap


def test_get_macro_news_maps_timespan_onto_rss_window():
    captured = {}

    def fake_rss(limit=20, since_days=2):
        captured["since_days"] = since_days
        return [NewsItem(headline="r", source="BBC", date="2026-06-24", url="u")]

    with patch("macro_news.fetch_rss_macro_news", side_effect=fake_rss):
        items, source = macro_news.get_macro_news(timespan="3d")
    assert captured["since_days"] == 3  # timespan honored on the RSS path
    assert source == "rss"


# --- Macro risk-off switch (deterministic tripwire) ------------------------- #


def _item(headline: str) -> NewsItem:
    return NewsItem(headline=headline, source="BBC", date="2026-06-05", url="u")


def test_assess_macro_risk_normal_on_benign_headlines():
    risk = macro_news.assess_macro_risk(
        [
            _item("Tech stocks rise as earnings season kicks off"),
            _item("Fed signals rates on hold amid sticky inflation"),
            _item("Samsung unveils new foldable phone"),
        ]
    )
    assert risk["risk_level"] == "NORMAL"
    assert risk["risk_score"] == 0
    assert risk["matched"] == []
    assert risk["note"] is None


def test_assess_macro_risk_elevated_on_one_severe_headline():
    risk = macro_news.assess_macro_risk(
        [
            _item("Russia invades neighboring country, markets reel"),
            _item("Tech stocks rise as earnings season kicks off"),
        ]
    )
    assert risk["risk_level"] == "ELEVATED"
    assert risk["risk_score"] == 2
    assert risk["matched"][0]["bucket"] == "war_conflict"


def test_assess_macro_risk_elevated_on_two_moderate_headlines():
    risk = macro_news.assess_macro_risk(
        [
            _item("US imposes sanctions on major oil producer"),
            _item("China announces retaliatory tariffs on US goods"),
        ]
    )
    assert risk["risk_level"] == "ELEVATED"
    assert risk["risk_score"] == 2
    assert {m["bucket"] for m in risk["matched"]} == {"tariff_sanctions"}


def test_assess_macro_risk_risk_off_on_corroborated_shock():
    risk = macro_news.assess_macro_risk(
        [
            _item("Iran blockades Strait of Hormuz as conflict widens"),
            _item("US launches airstrike on military sites"),
            _item("Stock market crash fears as circuit breaker halts trading"),
        ]
    )
    assert risk["risk_level"] == "RISK_OFF"
    assert risk["risk_score"] == 6
    buckets = {m["bucket"] for m in risk["matched"]}
    assert "war_conflict" in buckets
    assert "market_crash" in buckets


def test_assess_macro_risk_korean_headlines_match():
    risk = macro_news.assess_macro_risk(
        [
            _item("코스피 폭락에 서킷브레이커 발동"),
            _item("중동 전쟁 확산 우려에 유가 급등"),
        ]
    )
    assert risk["risk_level"] == "ELEVATED"
    assert risk["risk_score"] == 4  # market_crash (2) + war_conflict (2)
    buckets = {m["bucket"] for m in risk["matched"]}
    assert buckets == {"market_crash", "war_conflict"}


def test_assess_macro_risk_item_counts_once_at_most_severe_bucket():
    # One headline hitting both war (2) and oil (1) buckets scores once, at 2.
    risk = macro_news.assess_macro_risk(
        [_item("Oil spikes after missile strike near Strait of Hormuz")]
    )
    assert risk["risk_score"] == 2
    assert len(risk["matched"]) == 1
    assert risk["matched"][0]["bucket"] == "war_conflict"


def test_assess_macro_risk_fail_open_on_empty():
    """RSS + GDELT both unreachable → empty items → NORMAL + visible note."""
    risk = macro_news.assess_macro_risk([])
    assert risk["risk_level"] == "NORMAL"
    assert risk["risk_score"] == 0
    assert "fail-open" in risk["note"]


def test_format_macro_for_prompt_renders_risk_off_instruction():
    items = [_item("Iran blockades Strait of Hormuz as conflict widens")]
    risk = {
        "risk_level": "RISK_OFF",
        "risk_score": 6,
        "matched": [
            {
                "headline": items[0].headline,
                "source": "BBC",
                "date": "2026-06-05",
                "bucket": "war_conflict",
                "weight": 2,
            }
        ],
        "note": None,
    }
    block = macro_news.format_macro_for_prompt(items, risk)
    assert "MACRO RISK: RISK_OFF" in block
    assert "NO new BULL" in block
    assert "war_conflict" in block  # matched evidence surfaced


def test_format_macro_for_prompt_renders_elevated_trim_instruction():
    items = [_item("Russia invades neighboring country, markets reel")]
    block = macro_news.format_macro_for_prompt(
        items, macro_news.assess_macro_risk(items)
    )
    assert "MACRO RISK: ELEVATED" in block
    assert "-0.05" in block


def test_format_macro_for_prompt_risk_fail_open_note_visible():
    block = macro_news.format_macro_for_prompt([], macro_news.assess_macro_risk([]))
    assert "MACRO RISK: NORMAL" in block
    assert "fail-open" in block


def test_format_macro_for_prompt_without_risk_unchanged():
    block = macro_news.format_macro_for_prompt([_item("Some headline")])
    assert "MACRO RISK" not in block  # risk line only rendered when passed
