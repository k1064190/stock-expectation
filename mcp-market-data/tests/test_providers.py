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


# --- Unit tests for KR fundamentals column resolution ---

import pandas as pd


def _make_fundamental_row(columns: list[str], values: list) -> pd.Series:
    """Build a minimal pandas Series mimicking one row of a fundamentals DataFrame."""
    return pd.Series(dict(zip(columns, values)))


def test_extract_fundamentals_primary_columns():
    """Primary column set PER/PBR/DIV is recognised."""
    row = _make_fundamental_row(["PER", "PBR", "DIV", "EPS"], [15.2, 1.8, 2.5, 1000])
    result = KoreanMarketProvider._extract_fundamentals_from_row(row, "005930", "Samsung")
    assert result is not None
    assert result.pe_ratio == pytest.approx(15.2)
    assert result.pb_ratio == pytest.approx(1.8)
    assert result.dividend_yield == pytest.approx(2.5)


def test_extract_fundamentals_korean_div_column():
    """Alternative column set PER/PBR/배당수익률 is recognised."""
    row = _make_fundamental_row(["PER", "PBR", "배당수익률"], [10.0, 1.1, 3.0])
    result = KoreanMarketProvider._extract_fundamentals_from_row(row, "000660", "SK Hynix")
    assert result is not None
    assert result.dividend_yield == pytest.approx(3.0)


def test_extract_fundamentals_lowercase_columns():
    """Lowercase column set per/pbr/div is recognised."""
    row = _make_fundamental_row(["per", "pbr", "div"], [8.5, 0.9, 1.2])
    result = KoreanMarketProvider._extract_fundamentals_from_row(row, "035420", "Naver")
    assert result is not None
    assert result.pe_ratio == pytest.approx(8.5)


def test_extract_fundamentals_zero_values_become_none():
    """Zero values for PER/PBR/DIV are mapped to None (zero is not meaningful)."""
    row = _make_fundamental_row(["PER", "PBR", "DIV"], [0, 0, 0])
    result = KoreanMarketProvider._extract_fundamentals_from_row(row, "005930", "Samsung")
    assert result is not None
    assert result.pe_ratio is None
    assert result.pb_ratio is None
    assert result.dividend_yield is None


def test_extract_fundamentals_unknown_columns_returns_none():
    """Unrecognised column set returns None so callers can fall back."""
    row = _make_fundamental_row(["A", "B", "C"], [1, 2, 3])
    result = KoreanMarketProvider._extract_fundamentals_from_row(row, "005930", "Samsung")
    assert result is None


# --- Unit tests for KR batch methods ---


def test_get_price_history_batch_normalises_tickers(monkeypatch):
    """Batch method normalises short ticker codes before fetching."""
    kr = KoreanMarketProvider()
    calls = []

    def mock_get_price_history(ticker: str, days: int = 30):
        calls.append(ticker)
        return [OHLCV(date="2026-04-01", open=60000, high=61000, low=59000, close=60500, volume=10000)]

    monkeypatch.setattr(kr, "get_price_history", mock_get_price_history)
    results = kr.get_price_history_batch(["5930", "660"], days=5)

    assert "005930" in results
    assert "000660" in results
    assert set(calls) == {"005930", "000660"}


def test_get_price_history_batch_failed_ticker_returns_empty_list(monkeypatch):
    """Batch returns empty list for a ticker whose fetch raises."""
    kr = KoreanMarketProvider()

    def mock_get_price_history(ticker: str, days: int = 30):
        if ticker == "000001":
            raise ValueError("no data")
        return [OHLCV(date="2026-04-01", open=100, high=105, low=99, close=103, volume=500)]

    monkeypatch.setattr(kr, "get_price_history", mock_get_price_history)
    results = kr.get_price_history_batch(["005930", "000001"], days=5)

    assert len(results["005930"]) == 1
    assert results["000001"] == []


def test_get_fundamentals_batch_uses_bulk_call(monkeypatch):
    """Batch fundamentals extracts rows from a single bulk DataFrame."""
    kr = KoreanMarketProvider()

    bulk_df = pd.DataFrame(
        {"PER": [15.0, 10.0], "PBR": [1.5, 0.8], "DIV": [2.0, 3.5]},
        index=["005930", "000660"],
    )

    import providers.kr as kr_module

    monkeypatch.setattr(
        "providers.kr.KoreanMarketProvider.get_fundamentals_batch.__code__",
        kr.get_fundamentals_batch.__code__,
    )

    # Patch pykrx inside the module's namespace via a fake module
    class FakeKrxStock:
        @staticmethod
        def get_market_fundamental_by_ticker(date, market="ALL"):
            return bulk_df

        @staticmethod
        def get_market_ticker_name(ticker):
            return {"005930": "Samsung", "000660": "SK Hynix"}.get(ticker, "")

    import sys
    fake_pykrx = type(sys)("pykrx")
    fake_pykrx.stock = FakeKrxStock
    monkeypatch.setitem(sys.modules, "pykrx", fake_pykrx)
    monkeypatch.setitem(sys.modules, "pykrx.stock", FakeKrxStock)

    results = kr.get_fundamentals_batch(["005930", "000660"])

    assert "005930" in results
    assert "000660" in results
    assert results["005930"].pe_ratio == pytest.approx(15.0)
    assert results["000660"].dividend_yield == pytest.approx(3.5)


def test_get_fundamentals_batch_falls_back_on_bulk_failure(monkeypatch):
    """When bulk call raises, batch falls back to per-ticker get_fundamentals."""
    kr = KoreanMarketProvider()

    import sys
    fake_pykrx = type(sys)("pykrx")

    class FakeKrxStockFail:
        @staticmethod
        def get_market_fundamental_by_ticker(date, market="ALL"):
            raise RuntimeError("KRX down")

    fake_pykrx.stock = FakeKrxStockFail
    monkeypatch.setitem(sys.modules, "pykrx", fake_pykrx)
    monkeypatch.setitem(sys.modules, "pykrx.stock", FakeKrxStockFail)

    per_ticker_calls = []

    def mock_get_fundamentals(ticker: str):
        per_ticker_calls.append(ticker)
        return StockFundamentals(ticker=ticker, name="Test", pe_ratio=9.9)

    monkeypatch.setattr(kr, "get_fundamentals", mock_get_fundamentals)

    results = kr.get_fundamentals_batch(["005930", "000660"])

    assert set(per_ticker_calls) == {"005930", "000660"}
    assert results["005930"].pe_ratio == pytest.approx(9.9)


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
