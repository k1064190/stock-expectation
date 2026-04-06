"""Tests for market data providers.

These tests verify provider interfaces work correctly.
Tests that hit real APIs are marked with pytest.mark.network.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from providers.base import OHLCV, StockFundamentals, with_retry
from providers.us import USMarketProvider
from providers.kr import KoreanMarketProvider


# --- Unit tests (no network) ---


def test_ohlcv_dataclass():
    """OHLCV dataclass holds bar data."""
    bar = OHLCV(date="2026-04-01", open=100.0, high=105.0, low=99.0, close=103.0, volume=1000000)
    assert bar.close == 103.0
    assert bar.date == "2026-04-01"


def test_stock_fundamentals_optional_fields():
    """StockFundamentals works with minimal data."""
    fund = StockFundamentals(ticker="TEST", name="Test Corp")
    assert fund.pe_ratio is None
    assert fund.sector is None


def test_with_retry_success():
    """with_retry returns on first success."""
    call_count = 0

    def succeeds():
        nonlocal call_count
        call_count += 1
        return "ok"

    result = with_retry(succeeds, max_attempts=3, backoff_base=0.01)
    assert result == "ok"
    assert call_count == 1


def test_with_retry_eventual_success():
    """with_retry retries on failure then returns on success."""
    call_count = 0

    def fails_then_succeeds():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError("down")
        return "ok"

    result = with_retry(fails_then_succeeds, max_attempts=3, backoff_base=0.01)
    assert result == "ok"
    assert call_count == 3


def test_with_retry_all_fail():
    """with_retry raises after all attempts exhausted."""

    def always_fails():
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        with_retry(always_fails, max_attempts=2, backoff_base=0.01)


def test_kr_normalize_ticker():
    """Korean provider pads tickers to 6 digits."""
    kr = KoreanMarketProvider()
    assert kr._normalize_ticker("5930") == "005930"
    assert kr._normalize_ticker("005930") == "005930"


# --- Network tests (hit real APIs) ---


@pytest.mark.network
def test_kr_price_history_real():
    """Fetch Samsung Electronics price data from PyKRX."""
    kr = KoreanMarketProvider()
    bars = kr.get_price_history("005930", days=5)
    assert len(bars) > 0
    assert bars[-1].close > 0
    assert bars[-1].volume > 0


@pytest.mark.network
def test_kr_fundamentals_real():
    """Fetch Samsung Electronics fundamentals from PyKRX."""
    kr = KoreanMarketProvider()
    fund = kr.get_fundamentals("005930")
    assert fund is not None
    assert fund.name != ""
    assert fund.ticker == "005930"


@pytest.mark.network
def test_kr_search_real():
    """Search Korean stocks by name."""
    kr = KoreanMarketProvider()
    results = kr.search_stocks("삼성전자", limit=5)
    assert len(results) > 0
    assert any("005930" in r["ticker"] for r in results)


@pytest.mark.network
def test_us_price_history_yfinance():
    """Fetch AAPL price data via yfinance (no API key needed)."""
    us = USMarketProvider()
    us._fmp_key = ""  # Force yfinance fallback
    bars = us.get_price_history("AAPL", days=5)
    assert len(bars) > 0
    assert bars[-1].close > 0


@pytest.mark.network
def test_us_fundamentals_yfinance():
    """Fetch AAPL fundamentals via yfinance."""
    us = USMarketProvider()
    us._fmp_key = ""  # Force yfinance fallback
    fund = us.get_fundamentals("AAPL")
    assert fund is not None
    assert fund.name != ""
    assert fund.sector is not None


@pytest.mark.network
def test_us_health_check():
    """US provider health check passes."""
    us = USMarketProvider()
    us._fmp_key = ""  # Force yfinance
    assert us.is_healthy() is True
