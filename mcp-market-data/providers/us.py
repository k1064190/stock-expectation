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

from .base import MarketDataProvider, NewsItem, OHLCV, StockFundamentals, with_retry

logger = logging.getLogger(__name__)

FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"
FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
ALPHA_VANTAGE_BASE_URL = "https://www.alphavantage.co/query"


class USMarketProvider(MarketDataProvider):
    """US stock data via FMP API with yfinance fallback.

    Set FMP_API_KEY environment variable for FMP access.
    yfinance is used as fallback (no API key needed).
    """

    def __init__(self):
        self._fmp_key = os.environ.get("FMP_API_KEY", "")
        self._finnhub_key = os.environ.get("FINNHUB_API_KEY", "")
        self._av_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "")

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
        elif days <= 180:
            period = "6mo"
        elif days <= 365:
            period = "1y"
        elif days <= 730:
            period = "2y"
        elif days <= 1825:
            period = "5y"
        else:
            period = "max"

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
                            volume=(
                                int(row["Volume"]) if not pd.isna(row["Volume"]) else 0
                            ),
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
            futures = {executor.submit(self.get_fundamentals, t): t for t in tickers}
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    result[ticker] = future.result()
                except Exception as e:
                    logger.warning("Fundamentals batch failed for %s: %s", ticker, e)
                    result[ticker] = None

        return result

    def get_news(
        self, ticker: str, limit: int = 10, since_days: int = 7
    ) -> list[NewsItem]:
        """Fetch recent US stock news.

        Provider chain:
          1. Finnhub /company-news (if FINNHUB_API_KEY set) — primary
          2. Alpha Vantage NEWS_SENTIMENT merge (if ALPHA_VANTAGE_API_KEY set)
             — adds sentiment_score by URL match
          3. FMP /stock_news (if FMP_API_KEY set) — backup
          4. yfinance.Ticker(t).get_news() — last-resort fallback, no sentiment

        Args:
            ticker: US ticker (case-insensitive).
            limit: Max items to return.
            since_days: Only items within last N days.

        Returns:
            List of NewsItem, newest first. Empty on failure.
        """
        ticker = ticker.upper()
        items: list[NewsItem] = []

        if self._finnhub_key:
            try:
                items = self._finnhub_news(ticker, limit, since_days)
            except Exception as e:
                logger.warning("Finnhub news failed for %s: %s", ticker, e)

        if items and self._av_key:
            try:
                self._merge_alpha_vantage_sentiment(ticker, items)
                # Detailed breakdown (URL-matched vs ticker-avg fallback) is
                # logged inside _merge_alpha_vantage_sentiment at DEBUG.
            except Exception as e:
                logger.warning("Alpha Vantage sentiment failed for %s: %s", ticker, e)

        if not items and self._fmp_key:
            try:
                items = self._fmp_news(ticker, limit, since_days=since_days)
            except Exception as e:
                logger.warning("FMP news failed for %s: %s", ticker, e)

        if not items:
            try:
                items = self._yfinance_news(ticker, limit)
            except Exception as e:
                logger.warning("yfinance news failed for %s: %s", ticker, e)

        return items[:limit]

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
        elif days <= 180:
            period = "6mo"
        elif days <= 365:
            period = "1y"
        elif days <= 730:
            period = "2y"
        elif days <= 1825:
            period = "5y"
        else:
            period = "max"

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

    def _finnhub_news(self, ticker: str, limit: int, since_days: int) -> list[NewsItem]:
        """Fetch headlines via Finnhub /company-news.

        Free-tier rate limit: 60 calls/min. Each call returns up to a few
        hundred items, so we slice to ``limit`` after sort.
        """
        end = datetime.now()
        start = end - timedelta(days=since_days)
        resp = httpx.get(
            f"{FINNHUB_BASE_URL}/company-news",
            params={
                "symbol": ticker,
                "from": start.strftime("%Y-%m-%d"),
                "to": end.strftime("%Y-%m-%d"),
                "token": self._finnhub_key,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return []

        items: list[NewsItem] = []
        for entry in data:
            ts = entry.get("datetime")
            date_str = (
                datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                if isinstance(ts, (int, float))
                else end.strftime("%Y-%m-%d")
            )
            items.append(
                NewsItem(
                    headline=entry.get("headline", "").strip(),
                    source=entry.get("source", "Finnhub"),
                    date=date_str,
                    url=entry.get("url", ""),
                )
            )
        items.sort(key=lambda n: n.date, reverse=True)
        return items[:limit]

    def _merge_alpha_vantage_sentiment(self, ticker: str, items: list[NewsItem]) -> int:
        """Annotate items with Alpha Vantage sentiment scores in place.

        Alpha Vantage NEWS_SENTIMENT returns ``ticker_sentiment`` per
        article (with overall score and label). We match by URL when
        possible; if no URL matches, we apply the ticker-level average to
        any item missing a score so downstream code at least gets a
        directional signal.

        Reality check (2026-05-11 E2E observation): Finnhub returns its
        own redirect URLs (``https://finnhub.io/api/news?id=...``) rather
        than publisher URLs. AV stores publisher URLs. As a result the
        per-item URL match path effectively never fires for Finnhub items,
        and every item receives the ticker-level average. The breakdown
        is logged at DEBUG so operators can see when this happens; future
        work could follow Finnhub's redirect to recover publisher URLs.

        Free-tier limit is 25 calls/day, so callers should reserve this
        for finalist tickers, not the discovery set.

        Returns:
            Number of items annotated with a sentiment_score (URL-matched
            or fallback-averaged). 0 means AV returned data but nothing
            could be applied — useful for spotting stale URL matching.
        """
        resp = httpx.get(
            ALPHA_VANTAGE_BASE_URL,
            params={
                "function": "NEWS_SENTIMENT",
                "tickers": ticker,
                "apikey": self._av_key,
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        # AV signals rate limits and bad inputs by returning HTTP 200 with
        # an "Information"/"Note"/"Error Message" key and no feed array.
        # Return 0 (not bare return) so the caller's count-formatted log
        # doesn't TypeError on None.
        feed = data.get("feed", [])
        if not feed:
            return 0

        url_to_av: dict[str, dict] = {}
        scores: list[float] = []
        for art in feed:
            url = art.get("url", "")
            for ts in art.get("ticker_sentiment", []):
                if ts.get("ticker") != ticker:
                    continue
                try:
                    score = float(ts.get("ticker_sentiment_score"))
                except (TypeError, ValueError):
                    continue
                label = ts.get("ticker_sentiment_label")
                if url:
                    url_to_av[url] = {"score": score, "label": label}
                scores.append(score)

        avg_score = sum(scores) / len(scores) if scores else None

        url_matched = 0
        fallback_applied = 0
        for item in items:
            match = url_to_av.get(item.url)
            if match:
                item.sentiment_score = match["score"]
                item.sentiment_label = match["label"]
                url_matched += 1
            elif item.sentiment_score is None and avg_score is not None:
                item.sentiment_score = avg_score
                item.sentiment_label = None
                fallback_applied += 1

        logger.debug(
            "AV sentiment merge for %s: %d URL-matched, %d ticker-avg fallback "
            "(AV feed: %d articles, %d ticker_sentiment entries)",
            ticker,
            url_matched,
            fallback_applied,
            len(feed),
            len(scores),
        )
        return url_matched + fallback_applied

    def _fmp_news(self, ticker: str, limit: int, since_days: int = 7) -> list[NewsItem]:
        """Fetch headlines via FMP /stock_news. Backup when no Finnhub key.

        FMP's /stock_news endpoint accepts only ``tickers`` and ``limit`` —
        it does not expose date-range filtering. To keep the contract
        consistent with Finnhub and yfinance (every provider honours
        ``since_days``), we over-fetch and filter in memory.
        """
        cutoff = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")
        resp = httpx.get(
            f"{FMP_BASE_URL}/stock_news",
            params={
                "tickers": ticker,
                # Over-fetch so the post-filter still has enough items to
                # honour the caller's ``limit`` after dropping out-of-window
                # entries. 4× is generous; FMP's free tier caps at 250 items
                # per call anyway.
                "limit": min(limit * 4, 100),
                "apikey": self._fmp_key,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return []

        items: list[NewsItem] = []
        for entry in data:
            published = entry.get("publishedDate", "")
            date_str = published.split(" ")[0] if published else ""
            if date_str and date_str < cutoff:
                continue
            items.append(
                NewsItem(
                    headline=entry.get("title", "").strip(),
                    source=entry.get("site", "FMP"),
                    date=date_str,
                    url=entry.get("url", ""),
                )
            )
        return items[:limit]

    def _yfinance_news(self, ticker: str, limit: int) -> list[NewsItem]:
        """Fallback: yfinance.Ticker(t).get_news(). Free, no key, no sentiment."""
        import yfinance as yf

        t = yf.Ticker(ticker)
        try:
            raw = t.get_news() or []
        except Exception:
            raw = []

        items: list[NewsItem] = []
        for entry in raw:
            content = entry.get("content", entry)
            ts = (
                entry.get("providerPublishTime")
                or content.get("pubDate")
                or content.get("displayTime")
            )
            if isinstance(ts, (int, float)):
                date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            elif isinstance(ts, str):
                date_str = ts.split("T")[0]
            else:
                date_str = datetime.now().strftime("%Y-%m-%d")

            headline = content.get("title") or entry.get("title", "")
            url = (
                content.get("canonicalUrl", {}).get("url")
                if isinstance(content.get("canonicalUrl"), dict)
                else (entry.get("link") or content.get("link", ""))
            )
            source = (
                content.get("provider", {}).get("displayName")
                if isinstance(content.get("provider"), dict)
                else entry.get("publisher", "Yahoo Finance")
            )

            if not headline:
                continue
            items.append(
                NewsItem(
                    headline=headline.strip(),
                    source=source or "Yahoo Finance",
                    date=date_str,
                    url=url or "",
                )
            )
        return items[:limit]
