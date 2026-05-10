"""Tests for the news + disclosure data layer.

Mocks Finnhub, Alpha Vantage, FMP, and yfinance for the US provider, and
Naver Finance + Open DART for the KR provider. No tests in this file
require network access.
"""

import io
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from providers.base import Disclosure, NewsItem
from providers.kr import KoreanMarketProvider
from providers.us import USMarketProvider


# ---------------------------------------------------------------------------
# Dataclass smoke tests
# ---------------------------------------------------------------------------


def test_news_item_optional_sentiment():
    n = NewsItem(headline="x", source="y", date="2026-05-10", url="u")
    assert n.sentiment_score is None
    assert n.sentiment_label is None


def test_disclosure_basic():
    d = Disclosure(
        rcept_no="20260510000001",
        report_nm="분기보고서",
        flr_nm="삼성전자",
        rcept_dt="20260510",
        url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260510000001",
    )
    assert d.rcept_no == "20260510000001"


# ---------------------------------------------------------------------------
# US news — Finnhub primary
# ---------------------------------------------------------------------------


def _mock_response(json_payload, status: int = 200):
    """Build a mock httpx.Response with the given JSON body."""
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = status
    mock.json.return_value = json_payload
    mock.raise_for_status = MagicMock()
    return mock


def test_us_finnhub_news_parses_response(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "fake-key")
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    provider = USMarketProvider()

    payload = [
        {
            "headline": "NVDA hits new high",
            "source": "Reuters",
            "datetime": 1746878400,  # 2025-05-10 in epoch
            "url": "https://example.com/a",
        },
        {
            "headline": "NVDA earnings tomorrow",
            "source": "MarketWatch",
            "datetime": 1746792000,
            "url": "https://example.com/b",
        },
    ]

    with patch("providers.us.httpx.get", return_value=_mock_response(payload)):
        items = provider.get_news("NVDA", limit=5, since_days=7)

    assert len(items) == 2
    assert items[0].headline == "NVDA hits new high"
    assert items[0].source == "Reuters"
    assert items[0].sentiment_score is None  # no AV key


def test_us_av_sentiment_merges_by_url(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "fake-finnhub")
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "fake-av")
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    provider = USMarketProvider()

    finnhub_payload = [
        {
            "headline": "NVDA hits new high",
            "source": "Reuters",
            "datetime": 1746878400,
            "url": "https://example.com/a",
        }
    ]
    av_payload = {
        "feed": [
            {
                "url": "https://example.com/a",
                "ticker_sentiment": [
                    {
                        "ticker": "NVDA",
                        "ticker_sentiment_score": "0.42",
                        "ticker_sentiment_label": "Bullish",
                    }
                ],
            }
        ]
    }

    call_count = {"n": 0}

    def fake_get(url, **kwargs):
        call_count["n"] += 1
        if "finnhub" in url:
            return _mock_response(finnhub_payload)
        if "alphavantage" in url:
            return _mock_response(av_payload)
        raise AssertionError(f"unexpected URL: {url}")

    with patch("providers.us.httpx.get", side_effect=fake_get):
        items = provider.get_news("NVDA", limit=5, since_days=7)

    assert len(items) == 1
    assert items[0].sentiment_score == pytest.approx(0.42)
    assert items[0].sentiment_label == "Bullish"
    assert call_count["n"] == 2


def test_us_av_unmatched_urls_apply_average(monkeypatch):
    """When the AV feed doesn't match item URLs, fall back to ticker average."""
    monkeypatch.setenv("FINNHUB_API_KEY", "fake-finnhub")
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "fake-av")
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    provider = USMarketProvider()

    finnhub_payload = [
        {"headline": "h1", "source": "s", "datetime": 1746878400, "url": "url-1"},
        {"headline": "h2", "source": "s", "datetime": 1746792000, "url": "url-2"},
    ]
    av_payload = {
        "feed": [
            {
                "url": "different-url-x",
                "ticker_sentiment": [
                    {
                        "ticker": "NVDA",
                        "ticker_sentiment_score": "0.10",
                        "ticker_sentiment_label": "Neutral",
                    }
                ],
            },
            {
                "url": "different-url-y",
                "ticker_sentiment": [
                    {
                        "ticker": "NVDA",
                        "ticker_sentiment_score": "0.30",
                        "ticker_sentiment_label": "Bullish",
                    }
                ],
            },
        ]
    }

    def fake_get(url, **kwargs):
        if "finnhub" in url:
            return _mock_response(finnhub_payload)
        return _mock_response(av_payload)

    with patch("providers.us.httpx.get", side_effect=fake_get):
        items = provider.get_news("NVDA", limit=5, since_days=7)

    assert len(items) == 2
    # Both items should get the average of [0.10, 0.30] = 0.20
    for item in items:
        assert item.sentiment_score == pytest.approx(0.20)


def test_us_falls_back_to_yfinance_when_no_keys(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    provider = USMarketProvider()

    # Mock yfinance.Ticker.get_news() returning a couple of items.
    fake_yf_news = [
        {
            "content": {
                "title": "Yahoo headline 1",
                "pubDate": "2026-05-10T10:00:00Z",
                "canonicalUrl": {"url": "https://yh/a"},
                "provider": {"displayName": "YH Provider"},
            }
        },
        {
            "title": "Yahoo headline 2",
            "providerPublishTime": 1746792000,
            "link": "https://yh/b",
            "publisher": "YHP",
        },
    ]
    fake_ticker = MagicMock()
    fake_ticker.get_news.return_value = fake_yf_news
    fake_yf = MagicMock()
    fake_yf.Ticker.return_value = fake_ticker

    with patch.dict(sys.modules, {"yfinance": fake_yf}):
        items = provider.get_news("AAPL", limit=5, since_days=7)

    assert len(items) == 2
    assert items[0].headline == "Yahoo headline 1"
    assert items[0].source == "YH Provider"
    assert items[1].headline == "Yahoo headline 2"


def test_us_finnhub_failure_falls_back_silently(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "fake-key")
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    provider = USMarketProvider()

    fake_yf = MagicMock()
    fake_yf.Ticker.return_value.get_news.return_value = []

    with (
        patch("providers.us.httpx.get", side_effect=httpx.HTTPError("boom")),
        patch.dict(sys.modules, {"yfinance": fake_yf}),
    ):
        items = provider.get_news("NVDA", limit=5)

    assert items == []  # graceful, not an exception


# ---------------------------------------------------------------------------
# KR news — Naver scrape
# ---------------------------------------------------------------------------


NAVER_HTML_FIXTURE = """
<html><body>
<table class="type5"><tbody>
<tr>
  <td class="title"><a href="/item/news_read.naver?article_id=1&office_id=001&code=005930">삼성전자, 1분기 호실적</a></td>
  <td class="info">한국경제</td>
  <td class="date">2026.05.10 09:30</td>
</tr>
<tr>
  <td class="title"><a href="/item/news_read.naver?article_id=2">반도체 업황 회복세</a></td>
  <td class="info">매일경제</td>
  <td class="date">2026.05.09 14:15</td>
</tr>
<tr>
  <td class="title"><a href="https://example.com/x">아주 오래된 기사</a></td>
  <td class="info">뉴스원</td>
  <td class="date">2024.01.01 09:00</td>
</tr>
</tbody></table>
</body></html>
"""


def test_kr_naver_scrape_parses_table():
    provider = KoreanMarketProvider()

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.text = NAVER_HTML_FIXTURE
    mock_resp.raise_for_status = MagicMock()

    with patch("providers.kr.httpx.get", return_value=mock_resp):
        items = provider.get_news("005930", limit=10, since_days=365 * 10)

    assert len(items) == 3
    assert items[0].headline == "삼성전자, 1분기 호실적"
    assert items[0].source == "한국경제"
    assert items[0].url.startswith("https://finance.naver.com")
    assert items[0].date == "2026-05-10"


def test_kr_naver_filters_by_since_days():
    provider = KoreanMarketProvider()

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.text = NAVER_HTML_FIXTURE
    mock_resp.raise_for_status = MagicMock()

    with patch("providers.kr.httpx.get", return_value=mock_resp):
        # since_days=7 should drop the 2024.01.01 item.
        items = provider.get_news("005930", limit=10, since_days=7)

    headlines = [i.headline for i in items]
    assert "아주 오래된 기사" not in headlines


def test_kr_naver_failure_returns_empty():
    provider = KoreanMarketProvider()
    with patch("providers.kr.httpx.get", side_effect=httpx.HTTPError("network")):
        items = provider.get_news("005930", limit=5)
    assert items == []


# ---------------------------------------------------------------------------
# KR disclosures — Open DART
# ---------------------------------------------------------------------------


def _build_corpcode_zip() -> bytes:
    """Build an in-memory zip matching DART's corpCode.xml schema."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<result>"
        "<list>"
        "<corp_code>00126380</corp_code>"
        "<corp_name>삼성전자</corp_name>"
        "<stock_code>005930</stock_code>"
        "<modify_date>20260101</modify_date>"
        "</list>"
        "<list>"
        "<corp_code>00164779</corp_code>"
        "<corp_name>SK하이닉스</corp_name>"
        "<stock_code>000660</stock_code>"
        "<modify_date>20260101</modify_date>"
        "</list>"
        "<list>"
        # An unlisted entry (no stock_code) — should be skipped.
        "<corp_code>99999999</corp_code>"
        "<corp_name>비상장사</corp_name>"
        "<stock_code></stock_code>"
        "<modify_date>20260101</modify_date>"
        "</list>"
        "</result>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", xml)
    return buf.getvalue()


def test_kr_disclosure_no_key_returns_empty(monkeypatch):
    monkeypatch.delenv("OPEN_DART_API_KEY", raising=False)
    provider = KoreanMarketProvider()
    items = provider.get_disclosures("005930")
    assert items == []


def test_kr_disclosure_downloads_and_uses_corp_code(monkeypatch, tmp_path):
    monkeypatch.setenv("OPEN_DART_API_KEY", "fake-dart-key")

    cache_path = tmp_path / "dart_corp_codes.csv"
    monkeypatch.setattr("providers.kr.DART_CORP_CODE_CSV", cache_path)

    corp_zip = _build_corpcode_zip()
    list_payload = {
        "status": "000",
        "message": "정상",
        "list": [
            {
                "rcept_no": "20260510000001",
                "report_nm": "분기보고서",
                "flr_nm": "삼성전자",
                "rcept_dt": "20260510",
            }
        ],
    }

    def fake_get(url, **kwargs):
        if url.endswith("/corpCode.xml"):
            mock = MagicMock(spec=httpx.Response)
            mock.status_code = 200
            mock.content = corp_zip
            mock.raise_for_status = MagicMock()
            return mock
        if url.endswith("/list.json"):
            return _mock_response(list_payload)
        raise AssertionError(f"unexpected URL: {url}")

    provider = KoreanMarketProvider()
    with patch("providers.kr.httpx.get", side_effect=fake_get):
        items = provider.get_disclosures("005930", since_days=7, limit=5)

    assert len(items) == 1
    assert items[0].rcept_no == "20260510000001"
    assert items[0].report_nm == "분기보고서"
    assert items[0].url == (
        "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260510000001"
    )
    # Cache should have been written and exclude the unlisted entry.
    assert cache_path.exists()
    cached = cache_path.read_text(encoding="utf-8")
    assert "005930" in cached
    assert "비상장사" not in cached


def test_kr_disclosure_unknown_ticker_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("OPEN_DART_API_KEY", "fake-key")
    cache_path = tmp_path / "dart_corp_codes.csv"
    monkeypatch.setattr("providers.kr.DART_CORP_CODE_CSV", cache_path)

    # Pre-populate cache with only Samsung — query for an unrelated ticker.
    cache_path.write_text(
        "corp_code,corp_name,stock_code,modify_date\n"
        "00126380,삼성전자,005930,20260101\n",
        encoding="utf-8",
    )

    provider = KoreanMarketProvider()
    items = provider.get_disclosures("999999", since_days=7)
    assert items == []


def test_kr_disclosure_status_013_treated_as_no_data(monkeypatch, tmp_path):
    monkeypatch.setenv("OPEN_DART_API_KEY", "fake-key")
    cache_path = tmp_path / "dart_corp_codes.csv"
    monkeypatch.setattr("providers.kr.DART_CORP_CODE_CSV", cache_path)
    cache_path.write_text(
        "corp_code,corp_name,stock_code,modify_date\n"
        "00126380,삼성전자,005930,20260101\n",
        encoding="utf-8",
    )

    payload = {"status": "013", "message": "조회된 데이터가 없습니다.", "list": []}
    provider = KoreanMarketProvider()
    with patch("providers.kr.httpx.get", return_value=_mock_response(payload)):
        items = provider.get_disclosures("005930", since_days=7)
    assert items == []
