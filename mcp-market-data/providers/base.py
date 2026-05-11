"""Base provider interface and health check mixin.

All market data providers implement this interface with fallback + retry logic.
"""

import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class OHLCV:
    """Open-High-Low-Close-Volume bar.

    Args:
        date: Bar date (YYYY-MM-DD).
        open: Opening price.
        high: High price.
        low: Low price.
        close: Closing price.
        volume: Trading volume.
    """

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class StockFundamentals:
    """Fundamental data for a stock.

    Args:
        ticker: Stock ticker symbol.
        name: Company name.
        market_cap: Market capitalization.
        pe_ratio: Price-to-earnings ratio.
        pb_ratio: Price-to-book ratio.
        dividend_yield: Dividend yield as percentage.
        sector: GICS sector.
        industry: Industry classification.
    """

    ticker: str
    name: str
    market_cap: Optional[float] = None
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    dividend_yield: Optional[float] = None
    sector: Optional[str] = None
    industry: Optional[str] = None


@dataclass
class SectorData:
    """Sector performance data.

    Args:
        sector: Sector name.
        change_1d: 1-day percentage change.
        change_1w: 1-week percentage change.
        change_1m: 1-month percentage change.
    """

    sector: str
    change_1d: Optional[float] = None
    change_1w: Optional[float] = None
    change_1m: Optional[float] = None


@dataclass
class NewsItem:
    """Single news headline for a ticker.

    Args:
        headline: Article title.
        source: Publisher name (e.g. "Reuters", "MarketWatch", "한국경제").
        date: ISO 8601 date of publication (YYYY-MM-DD or full timestamp).
        url: Link to the original article.
        sentiment_score: Sentiment in [-1.0, +1.0] when available
            (Alpha Vantage is the only provider that supplies this in our stack).
            None means the provider didn't return a score.
        sentiment_label: Optional human label such as "Bullish", "Neutral",
            "Bearish" when the provider attaches one.
    """

    headline: str
    source: str
    date: str
    url: str
    sentiment_score: Optional[float] = None
    sentiment_label: Optional[str] = None


@dataclass
class Disclosure:
    """Single Korean regulatory disclosure (Open DART).

    Args:
        rcept_no: DART receipt number (14-digit string).
        report_nm: Report name (e.g. "분기보고서", "주요사항보고서(유상증자결정)").
        flr_nm: Filer name (the company filing the report).
        rcept_dt: Receipt date (YYYYMMDD).
        url: Direct link to the report on dart.fss.or.kr.
    """

    rcept_no: str
    report_nm: str
    flr_nm: str
    rcept_dt: str
    url: str


class MarketDataProvider(ABC):
    """Abstract base class for market data providers."""

    @abstractmethod
    def get_price_history(self, ticker: str, days: int = 30) -> list[OHLCV]:
        """Fetch OHLCV price history.

        Args:
            ticker: Stock ticker symbol.
            days: Number of trading days to fetch.

        Returns:
            List of OHLCV bars, oldest first.
        """
        ...

    @abstractmethod
    def get_current_price(self, ticker: str) -> Optional[float]:
        """Fetch the latest closing price.

        Args:
            ticker: Stock ticker symbol.

        Returns:
            Latest close price or None if unavailable.
        """
        ...

    @abstractmethod
    def get_fundamentals(self, ticker: str) -> Optional[StockFundamentals]:
        """Fetch fundamental data.

        Args:
            ticker: Stock ticker symbol.

        Returns:
            StockFundamentals or None if unavailable.
        """
        ...

    @abstractmethod
    def get_news(
        self, ticker: str, limit: int = 10, since_days: int = 7
    ) -> list["NewsItem"]:
        """Fetch recent news headlines for a ticker.

        Args:
            ticker: Stock ticker symbol (US: "NVDA", KR: 6-digit code).
            limit: Maximum items to return.
            since_days: Only return items published within the last N days.

        Returns:
            List of NewsItem, newest first. Empty list on failure or no news.
        """
        ...

    @abstractmethod
    def search_stocks(self, query: str, limit: int = 10) -> list[dict]:
        """Search stocks by name or ticker.

        Args:
            query: Search query string.
            limit: Maximum results.

        Returns:
            List of dicts with 'ticker' and 'name' keys.
        """
        ...

    @abstractmethod
    def is_healthy(self) -> bool:
        """Check if the data provider is responsive.

        Returns:
            True if the provider can serve requests.
        """
        ...


def with_retry(func, max_attempts: int = 3, backoff_base: float = 1.0):
    """Retry a function with exponential backoff.

    Args:
        func: Callable to retry.
        max_attempts: Maximum number of attempts.
        backoff_base: Base delay in seconds (delays: 1s, 4s, 16s).

    Returns:
        Result of func() on success.

    Raises:
        The last exception if all attempts fail.
    """
    last_error = None
    for attempt in range(max_attempts):
        try:
            return func()
        except Exception as e:
            last_error = e
            if attempt < max_attempts - 1:
                delay = backoff_base * (4**attempt)
                logger.warning(
                    "Attempt %d/%d failed: %s. Retrying in %.1fs",
                    attempt + 1,
                    max_attempts,
                    e,
                    delay,
                )
                time.sleep(delay)
    raise last_error
