"""US market data provider using FMP API with yfinance fallback.

Fallback hierarchy: FMP → yfinance → cached data.
FMP free tier: 250 calls/day.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional

import httpx

from .base import MarketDataProvider, OHLCV, StockFundamentals, with_retry

logger = logging.getLogger(__name__)

FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"


class USMarketProvider(MarketDataProvider):
    """US stock data via FMP API with yfinance fallback.

    Set FMP_API_KEY environment variable for FMP access.
    yfinance is used as fallback (no API key needed).
    """

    def __init__(self):
        self._fmp_key = os.environ.get("FMP_API_KEY", "")

    def get_price_history(self, ticker: str, days: int = 30) -> list[OHLCV]:
        """Fetch OHLCV from FMP, falling back to yfinance.

        Args:
            ticker: US stock ticker (e.g., "NVDA", "AAPL").
            days: Number of calendar days to look back.

        Returns:
            List of OHLCV bars, oldest first.
        """
        ticker = ticker.upper()

        if self._fmp_key:
            try:
                return with_retry(lambda: self._fmp_price_history(ticker, days))
            except Exception as e:
                logger.warning("FMP failed for %s: %s. Trying yfinance.", ticker, e)

        try:
            return with_retry(lambda: self._yfinance_price_history(ticker, days))
        except Exception as e:
            logger.error("All US data providers failed for %s: %s", ticker, e)
            return []

    def get_current_price(self, ticker: str) -> Optional[float]:
        """Get latest closing price for a US stock.

        Args:
            ticker: US stock ticker symbol.

        Returns:
            Latest close price in USD, or None.
        """
        bars = self.get_price_history(ticker, days=5)
        return bars[-1].close if bars else None

    def get_fundamentals(self, ticker: str) -> Optional[StockFundamentals]:
        """Fetch fundamental data from FMP or yfinance.

        Args:
            ticker: US stock ticker symbol.

        Returns:
            StockFundamentals with available data, or None.
        """
        ticker = ticker.upper()

        if self._fmp_key:
            try:
                return with_retry(lambda: self._fmp_fundamentals(ticker))
            except Exception as e:
                logger.warning("FMP fundamentals failed for %s: %s", ticker, e)

        try:
            return with_retry(lambda: self._yfinance_fundamentals(ticker))
        except Exception as e:
            logger.error("All fundamentals providers failed for %s: %s", ticker, e)
            return None

    def get_price_history_batch(
        self, tickers: list[str], days: int = 30
    ) -> dict[str, list[OHLCV]]:
        """Fetch OHLCV for multiple tickers in a single yfinance bulk download.

        FMP does not support batch price history, so yfinance is always used
        here regardless of whether FMP_API_KEY is set.

        Args:
            tickers: List of US ticker symbols (e.g., ["NVDA", "AAPL"]).
            days: Number of calendar days to look back.

        Returns:
            Dict mapping each ticker to a list of OHLCV bars (oldest first).
            Tickers with no data map to an empty list.
        """
        import yfinance as yf

        if not tickers:
            return {}

        # Map days to yfinance period string (same logic as _yfinance_price_history).
        if days <= 5:
            period = "5d"
        elif days <= 30:
            period = "1mo"
        elif days <= 90:
            period = "3mo"
        else:
            period = "6mo"

        upper_tickers = [t.upper() for t in tickers]
        df = yf.download(
            upper_tickers,
            period=period,
            group_by="ticker",
            progress=False,
            threads=True,
        )

        result: dict[str, list[OHLCV]] = {}

        for ticker in upper_tickers:
            try:
                # When only one ticker is requested yfinance returns a flat
                # DataFrame (columns: Open, High, Low, Close, Volume).
                # For multiple tickers the columns are a MultiIndex
                # (ticker, field).
                if isinstance(df.columns, type(None)) or df.empty:
                    result[ticker] = []
                    continue

                import pandas as pd

                if isinstance(df.columns, pd.MultiIndex):
                    ticker_df = df[ticker]
                else:
                    # Single-ticker flat layout.
                    ticker_df = df

                # Drop rows where all OHLCV values are NaN.
                ticker_df = ticker_df.dropna(how="all")

                if ticker_df.empty:
                    result[ticker] = []
                    continue

                bars: list[OHLCV] = []
                for date_idx, row in ticker_df.iterrows():
                    # Skip rows where close is NaN (partial NaN row).
                    if pd.isna(row["Close"]):
                        continue
                    bars.append(
                        OHLCV(
                            date=date_idx.strftime("%Y-%m-%d"),
                            open=float(row["Open"]),
                            high=float(row["High"]),
                            low=float(row["Low"]),
                            close=float(row["Close"]),
                            volume=int(row["Volume"]) if not pd.isna(row["Volume"]) else 0,
                        )
                    )

                result[ticker] = bars[-days:] if len(bars) > days else bars

            except Exception as e:
                logger.warning("Failed to parse batch data for %s: %s", ticker, e)
                result[ticker] = []

        return result

    def get_fundamentals_batch(
        self, tickers: list[str]
    ) -> dict[str, Optional[StockFundamentals]]:
        """Fetch fundamentals for multiple tickers in parallel.

        Uses a ThreadPoolExecutor to issue concurrent individual
        ``get_fundamentals`` calls. Workers are capped at 8 to avoid
        triggering rate limits on either FMP or yfinance.

        Individual failures are logged as warnings and do not abort the
        entire batch.

        Args:
            tickers: List of US ticker symbols.

        Returns:
            Dict mapping each ticker to its StockFundamentals, or None if
            the lookup failed or returned no data.
        """
        if not tickers:
            return {}

        result: dict[str, Optional[StockFundamentals]] = {}
        max_workers = min(len(tickers), 8)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.get_fundamentals, t): t for t in tickers
            }
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    result[ticker] = future.result()
                except Exception as e:
                    logger.warning(
                        "Fundamentals batch failed for %s: %s", ticker, e
                    )
                    result[ticker] = None

        return result

    def search_stocks(self, query: str, limit: int = 10) -> list[dict]:
        """Search US stocks via FMP or basic yfinance lookup.

        Args:
            query: Company name or ticker fragment.
            limit: Maximum results.

        Returns:
            List of dicts with 'ticker', 'name', 'market' keys.
        """
        if self._fmp_key:
            try:
                return self._fmp_search(query, limit)
            except Exception as e:
                logger.warning("FMP search failed: %s", e)

        # yfinance doesn't have a search API, so try direct lookup
        try:
            import yfinance as yf

            t = yf.Ticker(query.upper())
            info = t.info
            if info.get("symbol"):
                return [
                    {
                        "ticker": info["symbol"],
                        "name": info.get("longName", info.get("shortName", query)),
                        "market": "US",
                    }
                ]
        except Exception:
            pass
        return []

    def is_healthy(self) -> bool:
        """Check if FMP or yfinance can respond."""
        if self._fmp_key:
            try:
                resp = httpx.get(
                    f"{FMP_BASE_URL}/quote/AAPL",
                    params={"apikey": self._fmp_key},
                    timeout=10,
                )
                return resp.status_code == 200
            except Exception:
                pass

        try:
            import yfinance as yf

            t = yf.Ticker("AAPL")
            hist = t.history(period="1d")
            return not hist.empty
        except Exception:
            return False

    def _fmp_price_history(self, ticker: str, days: int) -> list[OHLCV]:
        """Fetch from FMP API."""
        end = datetime.now()
        start = end - timedelta(days=days + 10)
        resp = httpx.get(
            f"{FMP_BASE_URL}/historical-price-full/{ticker}",
            params={
                "apikey": self._fmp_key,
                "from": start.strftime("%Y-%m-%d"),
                "to": end.strftime("%Y-%m-%d"),
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        if "historical" not in data:
            raise ValueError(f"No historical data from FMP for {ticker}")

        bars = []
        for item in reversed(data["historical"]):
            bars.append(
                OHLCV(
                    date=item["date"],
                    open=float(item["open"]),
                    high=float(item["high"]),
                    low=float(item["low"]),
                    close=float(item["close"]),
                    volume=int(item["volume"]),
                )
            )
        return bars[-days:] if len(bars) > days else bars

    def _yfinance_price_history(self, ticker: str, days: int) -> list[OHLCV]:
        """Fallback: fetch from yfinance."""
        import yfinance as yf

        t = yf.Ticker(ticker)
        # yfinance period string: map days to period
        if days <= 5:
            period = "5d"
        elif days <= 30:
            period = "1mo"
        elif days <= 90:
            period = "3mo"
        else:
            period = "6mo"

        df = t.history(period=period)
        if df.empty:
            raise ValueError(f"No data from yfinance for {ticker}")

        bars = []
        for date_idx, row in df.iterrows():
            bars.append(
                OHLCV(
                    date=date_idx.strftime("%Y-%m-%d"),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=int(row["Volume"]),
                )
            )
        return bars[-days:] if len(bars) > days else bars

    def _fmp_fundamentals(self, ticker: str) -> Optional[StockFundamentals]:
        """Fetch from FMP API."""
        resp = httpx.get(
            f"{FMP_BASE_URL}/profile/{ticker}",
            params={"apikey": self._fmp_key},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None

        item = data[0]
        return StockFundamentals(
            ticker=ticker,
            name=item.get("companyName", ""),
            market_cap=item.get("mktCap"),
            pe_ratio=item.get("pe"),
            pb_ratio=item.get("priceToBook"),
            dividend_yield=item.get("lastDiv"),
            sector=item.get("sector"),
            industry=item.get("industry"),
        )

    def _yfinance_fundamentals(self, ticker: str) -> Optional[StockFundamentals]:
        """Fallback: fetch from yfinance."""
        import yfinance as yf

        t = yf.Ticker(ticker)
        info = t.info
        if not info.get("symbol"):
            return None

        return StockFundamentals(
            ticker=ticker,
            name=info.get("longName", info.get("shortName", "")),
            market_cap=info.get("marketCap"),
            pe_ratio=info.get("trailingPE"),
            pb_ratio=info.get("priceToBook"),
            dividend_yield=info.get("dividendYield"),
            sector=info.get("sector"),
            industry=info.get("industry"),
        )

    def _fmp_search(self, query: str, limit: int) -> list[dict]:
        """Search via FMP API."""
        resp = httpx.get(
            f"{FMP_BASE_URL}/search",
            params={
                "query": query,
                "limit": limit,
                "apikey": self._fmp_key,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            {
                "ticker": item["symbol"],
                "name": item.get("name", ""),
                "market": "US",
            }
            for item in data
        ]
