"""Tests for batch CLI commands (price-batch, fundamentals-batch).

Tests verify batch provider methods and CLI integration.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp-market-data"))

from providers.base import OHLCV, StockFundamentals
from providers.us import USMarketProvider
from providers.kr import KoreanMarketProvider


# --- Unit tests (no network) ---


def test_us_provider_has_batch_methods():
    """US provider exposes batch methods."""
    us = USMarketProvider()
    assert hasattr(us, "get_price_history_batch")
    assert hasattr(us, "get_fundamentals_batch")


def test_kr_provider_has_batch_methods():
    """KR provider exposes batch methods."""
    kr = KoreanMarketProvider()
    assert hasattr(kr, "get_price_history_batch")
    assert hasattr(kr, "get_fundamentals_batch")


# --- Network tests ---


@pytest.mark.network
def test_us_price_batch_yfinance():
    """Batch fetch US price data for 3 tickers."""
    us = USMarketProvider()
    us._fmp_key = ""  # Force yfinance
    results = us.get_price_history_batch(["AAPL", "MSFT", "NVDA"], days=5)

    assert len(results) == 3
    for ticker in ["AAPL", "MSFT", "NVDA"]:
        assert ticker in results
        assert len(results[ticker]) > 0
        assert results[ticker][-1].close > 0


@pytest.mark.network
def test_kr_price_batch():
    """Batch fetch KR price data for 2 tickers."""
    kr = KoreanMarketProvider()
    results = kr.get_price_history_batch(["005930", "000660"], days=5)

    assert len(results) == 2
    for ticker in ["005930", "000660"]:
        assert ticker in results
        assert len(results[ticker]) > 0
        assert results[ticker][-1].close > 0


@pytest.mark.network
def test_us_fundamentals_batch():
    """Batch fetch US fundamentals for 3 tickers."""
    us = USMarketProvider()
    us._fmp_key = ""  # Force yfinance
    results = us.get_fundamentals_batch(["AAPL", "MSFT", "NVDA"])

    assert len(results) == 3
    for ticker in ["AAPL", "MSFT", "NVDA"]:
        assert ticker in results
        assert results[ticker] is not None
        assert results[ticker].name != ""


@pytest.mark.network
def test_kr_fundamentals_batch():
    """Batch fetch KR fundamentals for 2 tickers."""
    kr = KoreanMarketProvider()
    results = kr.get_fundamentals_batch(["005930", "000660"])

    assert len(results) == 2
    for ticker in ["005930", "000660"]:
        assert ticker in results
        assert results[ticker] is not None


@pytest.mark.network
def test_price_batch_handles_invalid_ticker():
    """Batch gracefully handles invalid tickers mixed with valid ones."""
    us = USMarketProvider()
    us._fmp_key = ""
    results = us.get_price_history_batch(["AAPL", "ZZZZINVALID99"], days=5)

    assert "AAPL" in results
    assert len(results["AAPL"]) > 0
    # Invalid ticker should return empty list, not crash the batch
    assert "ZZZZINVALID99" in results
    assert len(results["ZZZZINVALID99"]) == 0


@pytest.mark.network
def test_kr_fundamentals_fixed():
    """KR fundamentals returns actual P/E, P/B values (bug fix verification)."""
    kr = KoreanMarketProvider()
    fund = kr.get_fundamentals("005930")
    assert fund is not None
    assert fund.name != ""
    # After bug fix, at least one of these should have a value
    has_data = fund.pe_ratio is not None or fund.pb_ratio is not None
    assert has_data, f"Samsung fundamentals still returning null: {fund}"
