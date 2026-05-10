"""Live API smoke tests for the news/disclosure layer.

Every test in this file:
  - hits a real third-party API,
  - is marked ``@pytest.mark.network`` so the default ``-m "not network"``
    run skips them,
  - is **also** auto-skipped when its API key is missing — meaning a
    contributor without keys can still run ``pytest`` (just gets skips,
    not failures).

Run with:
    uv run pytest mcp-market-data/tests/test_news_live.py -m network -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# load .env so an interactive `pytest` finds keys without manual sourcing
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
except ImportError:
    pass

from providers.us import USMarketProvider
from providers.kr import KoreanMarketProvider


def _have(env_var: str) -> bool:
    return bool(os.environ.get(env_var))


# ---------------------------------------------------------------------------
# US — Finnhub primary
# ---------------------------------------------------------------------------


@pytest.mark.network
@pytest.mark.skipif(not _have("FINNHUB_API_KEY"), reason="FINNHUB_API_KEY not set")
def test_finnhub_returns_real_news_for_aapl():
    """Live Finnhub call must return >=1 item with the expected shape and
    pin three contract invariants /expect's news_score depends on:
      1. items are sorted newest-first by date
      2. ``limit`` is enforced (count never exceeds it)
      3. every item carries a non-empty headline + http url
    """
    # Force-disable AV so this test isolates the Finnhub path.
    saved_av = os.environ.pop("ALPHA_VANTAGE_API_KEY", None)
    try:
        provider = USMarketProvider()
        items = provider.get_news("AAPL", limit=5, since_days=14)
        assert items, "Finnhub returned no items for AAPL in last 14 days"
        assert len(items) <= 5, f"limit=5 not honoured: got {len(items)} items"
        # Newest-first invariant.
        dates = [i.date for i in items]
        assert dates == sorted(
            dates, reverse=True
        ), f"Finnhub items not sorted newest-first: {dates}"
        # Every item carries the contract fields.
        for item in items:
            assert item.headline.strip(), "empty headline"
            assert item.url.startswith("http"), f"non-http url: {item.url!r}"
            assert item.date, "empty date"
        # No AV key set → no sentiment on any item.
        assert all(i.sentiment_score is None for i in items)
    finally:
        if saved_av is not None:
            os.environ["ALPHA_VANTAGE_API_KEY"] = saved_av


@pytest.mark.network
@pytest.mark.skipif(not _have("FINNHUB_API_KEY"), reason="FINNHUB_API_KEY not set")
def test_finnhub_since_days_filters_out_old_items():
    """``since_days=2`` must exclude anything older than 2 days. Without
    this assertion a regression that ignores the filter would still
    return a non-empty list and pass the basic shape test.
    """
    from datetime import datetime, timedelta

    saved_av = os.environ.pop("ALPHA_VANTAGE_API_KEY", None)
    try:
        provider = USMarketProvider()
        items = provider.get_news("AAPL", limit=20, since_days=2)
        if not items:
            pytest.skip("No items in last 2 days — common over weekends")
        cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        for item in items:
            assert (
                item.date >= cutoff
            ), f"item dated {item.date} predates since_days=2 cutoff {cutoff}"
    finally:
        if saved_av is not None:
            os.environ["ALPHA_VANTAGE_API_KEY"] = saved_av


@pytest.mark.network
@pytest.mark.skipif(
    not (_have("FINNHUB_API_KEY") and _have("ALPHA_VANTAGE_API_KEY")),
    reason="Both FINNHUB_API_KEY and ALPHA_VANTAGE_API_KEY required",
)
def test_finnhub_plus_av_attaches_sentiment():
    """When both keys are present and AV quota is available, at least
    one item must carry a sentiment_score; otherwise the merge is
    silently broken — the symptom we want to catch.

    AV free tier is 25 calls/day; if quota is exhausted on the test
    runner, AV will return an empty feed and we hit the (legitimate)
    no-sentiment path. We detect that by re-querying AV directly:
    if AV says rate-limited, skip; if AV returned a real feed but the
    merge produced no scored items, FAIL.
    """
    import httpx

    from providers.us import ALPHA_VANTAGE_BASE_URL

    provider = USMarketProvider()
    items = provider.get_news("AAPL", limit=10, since_days=14)
    assert items, "Finnhub returned no items"

    scored = [i for i in items if i.sentiment_score is not None]
    if scored:
        for item in scored:
            assert (
                -1.0 <= item.sentiment_score <= 1.0
            ), f"sentiment_score out of expected range: {item.sentiment_score}"
        return

    # No items got a score — distinguish "AV quota exhausted" from
    # "merge logic broken" by hitting AV directly.
    raw = httpx.get(
        ALPHA_VANTAGE_BASE_URL,
        params={
            "function": "NEWS_SENTIMENT",
            "tickers": "AAPL",
            "apikey": os.environ["ALPHA_VANTAGE_API_KEY"],
        },
        timeout=20,
    )
    body = raw.json()
    if not body.get("feed"):
        pytest.skip(
            "AV returned no feed (likely rate-limited or no recent items): "
            f"{ {k: body.get(k) for k in ('Information', 'Note', 'Error Message')} }"
        )
    pytest.fail(
        f"AV returned {len(body['feed'])} feed items but the merge attached "
        "zero sentiment scores. URL match + average fallback both regressed."
    )


@pytest.mark.network
@pytest.mark.skipif(not _have("FMP_API_KEY"), reason="FMP_API_KEY not set")
def test_fmp_only_fallback_works_when_finnhub_and_av_blanked():
    """The middle of the documented Finnhub -> AV -> FMP -> yfinance chain
    was previously untested — FMP has its own response shape and a quiet
    regression there would have been invisible. With Finnhub + AV blanked
    and only FMP_API_KEY set, get_news must return items via the FMP path.
    """
    saved = {
        k: os.environ.pop(k, None) for k in ("FINNHUB_API_KEY", "ALPHA_VANTAGE_API_KEY")
    }
    try:
        provider = USMarketProvider()
        items = provider.get_news("AAPL", limit=5, since_days=30)
        if not items:
            pytest.skip("FMP returned no items (rate-limited or quota?)")
        first = items[0]
        assert first.headline.strip()
        assert first.url.startswith("http")
        # FMP doesn't supply sentiment scores in this endpoint.
        assert first.sentiment_score is None
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


@pytest.mark.network
def test_yfinance_fallback_works_without_any_keys():
    """All paid keys blanked → yfinance fallback should still succeed."""
    saved = {
        k: os.environ.pop(k, None)
        for k in ("FINNHUB_API_KEY", "ALPHA_VANTAGE_API_KEY", "FMP_API_KEY")
    }
    try:
        provider = USMarketProvider()
        items = provider.get_news("AAPL", limit=3, since_days=30)
        # yfinance is rate-limit-flaky; treat zero items as a skip rather than fail.
        if not items:
            pytest.skip("yfinance returned no news (likely rate-limited)")
        first = items[0]
        assert first.headline
        assert first.sentiment_score is None  # yfinance never provides this
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# KR — Naver scrape (no key needed, just internet)
# ---------------------------------------------------------------------------


@pytest.mark.network
def test_naver_scrape_returns_real_kr_news_for_samsung():
    """Naver Finance must return news for 005930 (Samsung Electronics)
    with limit and since-days filters honoured.
    """
    from datetime import datetime, timedelta

    provider = KoreanMarketProvider()
    items = provider.get_news("005930", limit=5, since_days=7)
    if not items:
        pytest.skip("Naver layout may have changed; manual inspection needed")
    assert len(items) <= 5, f"limit=5 not honoured: got {len(items)} items"
    cutoff = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d")
    for item in items:
        assert item.headline.strip()
        assert item.url.startswith("https://finance.naver.com")
        # Korean source name carries through without ascii-escaping.
        assert isinstance(item.source, str) and item.source.strip()
        # Naver date format is YYYY-MM-DD after _parse_naver_date; the
        # since_days filter is best-effort but should keep us within
        # roughly the requested window.
        assert (
            item.date >= cutoff
        ), f"item dated {item.date} predates since_days=7 cutoff {cutoff}"


# ---------------------------------------------------------------------------
# KR — Open DART disclosures
# ---------------------------------------------------------------------------


@pytest.mark.network
@pytest.mark.skipif(not _have("OPEN_DART_API_KEY"), reason="OPEN_DART_API_KEY not set")
def test_dart_disclosures_for_samsung_recent():
    """Live DART call for 005930 (Samsung) over a 90-day window — large
    issuers always have at least one filing in any given quarter.
    """
    provider = KoreanMarketProvider()
    items = provider.get_disclosures("005930", since_days=90, limit=5)
    assert items, "DART returned no disclosures for Samsung in 90 days"
    first = items[0]
    assert first.rcept_no  # 14-digit receipt number
    assert first.report_nm
    assert first.url.startswith("https://dart.fss.or.kr")
    # rcept_dt is YYYYMMDD.
    assert len(first.rcept_dt) == 8 and first.rcept_dt.isdigit()


@pytest.mark.network
@pytest.mark.skipif(not _have("OPEN_DART_API_KEY"), reason="OPEN_DART_API_KEY not set")
def test_dart_corp_code_cache_persists_across_calls(tmp_path, monkeypatch):
    """First call downloads the corp_code map; second call reuses the cache."""
    from providers import kr as kr_module

    cache_path = tmp_path / "dart_corp_codes.csv"
    monkeypatch.setattr(kr_module, "DART_CORP_CODE_CSV", cache_path)

    provider = KoreanMarketProvider()
    # First call should populate the CSV.
    provider.get_disclosures("005930", since_days=30, limit=1)
    assert cache_path.exists(), "First call must populate the corp_code cache"
    first_size = cache_path.stat().st_size
    assert first_size > 1000, "Cache CSV is suspiciously small"

    # Second call: cache is hot, file size shouldn't change.
    provider.get_disclosures("000660", since_days=30, limit=1)
    assert cache_path.stat().st_size == first_size
