"""Korean market data provider using PyKRX and FinanceDataReader.

Fallback hierarchy: PyKRX → FinanceDataReader → cached data.
News: Naver Finance scrape. Disclosures: Open DART API.
"""

import csv
import io
import logging
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx

from .base import (
    Disclosure,
    MarketDataProvider,
    NewsItem,
    OHLCV,
    StockFundamentals,
    with_retry,
)

logger = logging.getLogger(__name__)

NAVER_NEWS_URL = "https://finance.naver.com/item/news_news.naver"
DART_BASE_URL = "https://opendart.fss.or.kr/api"
DART_REPORT_URL_TEMPLATE = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={}"

# Cached corp_code mapping path. Lazily downloaded from DART on first use.
DART_CORP_CODE_CSV = (
    Path(__file__).resolve().parents[2] / "data" / "dart_corp_codes.csv"
)


class KoreanMarketProvider(MarketDataProvider):
    """Korean stock data via PyKRX with FinanceDataReader fallback.

    PyKRX scrapes KRX directly (free, no API key). FinanceDataReader
    is a secondary source for KOSPI/KOSDAQ data.
    """

    def get_price_history(self, ticker: str, days: int = 30) -> list[OHLCV]:
        """Fetch OHLCV from PyKRX, falling back to FinanceDataReader.

        Args:
            ticker: 6-digit KRX ticker code (e.g., "005930").
            days: Number of calendar days to look back.

        Returns:
            List of OHLCV bars, oldest first.
        """
        ticker = self._normalize_ticker(ticker)

        try:
            return with_retry(lambda: self._pykrx_price_history(ticker, days))
        except Exception as e:
            logger.warning(
                "PyKRX failed for %s: %s. Trying FinanceDataReader.", ticker, e
            )

        try:
            return with_retry(lambda: self._fdr_price_history(ticker, days))
        except Exception as e:
            logger.error("All Korean data providers failed for %s: %s", ticker, e)
            return []

    def get_current_price(self, ticker: str) -> Optional[float]:
        """Get latest closing price for a Korean stock.

        Args:
            ticker: 6-digit KRX ticker code.

        Returns:
            Latest close price in KRW, or None.
        """
        bars = self.get_price_history(ticker, days=5)
        return bars[-1].close if bars else None

    def get_fundamentals(self, ticker: str) -> Optional[StockFundamentals]:
        """Fetch fundamental data from PyKRX.

        Args:
            ticker: 6-digit KRX ticker code.

        Returns:
            StockFundamentals with available data, or None.
        """
        ticker = self._normalize_ticker(ticker)
        try:
            return with_retry(lambda: self._pykrx_fundamentals(ticker))
        except Exception as e:
            logger.warning(
                "PyKRX fundamentals failed for %s: %s. Trying yfinance.", ticker, e
            )

        try:
            return with_retry(lambda: self._yfinance_fundamentals(ticker))
        except Exception as e:
            logger.error("All fundamentals providers failed for %s: %s", ticker, e)
            return None

    def get_price_history_batch(
        self, tickers: list[str], days: int = 30
    ) -> dict[str, list[OHLCV]]:
        """Fetch OHLCV history for multiple tickers in parallel.

        PyKRX has no native batch endpoint, so individual ``get_price_history``
        calls are parallelised with a ThreadPoolExecutor.  Workers are capped at
        5 to avoid overwhelming KRX's web interface.

        Args:
            tickers: List of KRX ticker codes (zero-padded to 6 digits if needed).
            days: Number of calendar days to look back for each ticker.

        Returns:
            Dict mapping each (normalised) ticker to its list of OHLCV bars.
            Tickers that fail return an empty list.
        """
        normalised = [self._normalize_ticker(t) for t in tickers]
        results: dict[str, list[OHLCV]] = {}

        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_ticker = {
                executor.submit(self.get_price_history, ticker, days): ticker
                for ticker in normalised
            }
            for future in as_completed(future_to_ticker):
                ticker = future_to_ticker[future]
                try:
                    results[ticker] = future.result()
                except Exception as exc:
                    logger.warning("Batch price fetch failed for %s: %s", ticker, exc)
                    results[ticker] = []

        return results

    def get_fundamentals_batch(
        self, tickers: list[str]
    ) -> dict[str, Optional[StockFundamentals]]:
        """Fetch fundamentals for multiple tickers efficiently.

        Calls ``get_market_fundamental_by_ticker`` once for the entire market,
        then extracts each requested ticker's row — far faster than N individual
        calls.  If the bulk call fails, falls back to per-ticker
        ``get_fundamentals`` via a ThreadPoolExecutor (max 5 workers).

        Args:
            tickers: List of KRX ticker codes (zero-padded to 6 digits if needed).

        Returns:
            Dict mapping each (normalised) ticker to its StockFundamentals, or
            None when data is unavailable.
        """
        from pykrx import stock as krx_stock

        normalised = [self._normalize_ticker(t) for t in tickers]
        results: dict[str, Optional[StockFundamentals]] = {}

        # --- Fast path: one bulk call for all tickers ---
        try:
            today = datetime.now().strftime("%Y%m%d")
            df = krx_stock.get_market_fundamental_by_ticker(today, market="ALL")

            for ticker in normalised:
                name = ""
                try:
                    name = krx_stock.get_market_ticker_name(ticker)
                except Exception:
                    pass

                if ticker not in df.index:
                    results[ticker] = StockFundamentals(ticker=ticker, name=name)
                    continue

                row = df.loc[ticker]
                extracted = self._extract_fundamentals_from_row(row, ticker, name)
                # extracted is None only when columns are unrecognised — in
                # that case mark the ticker for the per-ticker fallback below.
                results[ticker] = extracted

            # If every result was populated (no None from unknown columns) return now.
            if all(v is not None for v in results.values()):
                return results

            logger.debug(
                "Bulk fundamental call returned unrecognised columns; "
                "falling back to per-ticker calls for affected tickers."
            )
        except Exception as exc:
            logger.warning(
                "Bulk fundamental call failed (%s); falling back to per-ticker calls.",
                exc,
            )

        # --- Slow path: per-ticker fallback for any ticker not yet resolved ---
        missing = [t for t in normalised if results.get(t) is None]
        if not missing:
            return results

        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_ticker = {
                executor.submit(self.get_fundamentals, ticker): ticker
                for ticker in missing
            }
            for future in as_completed(future_to_ticker):
                ticker = future_to_ticker[future]
                try:
                    results[ticker] = future.result()
                except Exception as exc:
                    logger.warning(
                        "Per-ticker fundamental fallback failed for %s: %s", ticker, exc
                    )
                    results[ticker] = None

        return results

    def get_news(
        self, ticker: str, limit: int = 10, since_days: int = 7
    ) -> list[NewsItem]:
        """Scrape recent news headlines from Naver Finance.

        Naver does not expose a JSON API, so we parse the per-stock news
        page (HTML). The layout is stable enough that a small set of CSS
        selectors covers it; failures degrade silently to an empty list.

        Args:
            ticker: 6-digit KRX ticker code.
            limit: Max items to return.
            since_days: Filter to items within last N days (best-effort —
                Naver date strings are sometimes "MM.DD HH:MM" without year,
                so the cutoff is approximate).

        Returns:
            List of NewsItem, newest first. No sentiment field (Naver
            doesn't supply scores).
        """
        ticker = self._normalize_ticker(ticker)
        try:
            return self._scrape_naver_news(ticker, limit, since_days)
        except Exception as e:
            logger.warning("Naver news scrape failed for %s: %s", ticker, e)
            return []

    def get_disclosures(
        self, ticker: str, since_days: int = 7, limit: int = 30
    ) -> list[Disclosure]:
        """Fetch recent regulatory disclosures from Open DART.

        Requires ``OPEN_DART_API_KEY`` env var. Translates the 6-digit
        KRX ticker to the 8-digit DART corp_code via a cached mapping
        (downloaded from DART on first use).

        Args:
            ticker: 6-digit KRX ticker code.
            since_days: Look-back window in calendar days.
            limit: Maximum disclosures to return.

        Returns:
            List of Disclosure newest first. Empty when no key is set,
            no corp_code is found, or DART returns no items.
        """
        api_key = os.environ.get("OPEN_DART_API_KEY", "")
        if not api_key:
            logger.warning("OPEN_DART_API_KEY not set; cannot fetch disclosures.")
            return []

        ticker = self._normalize_ticker(ticker)
        corp_code = self._lookup_dart_corp_code(ticker, api_key)
        if not corp_code:
            logger.warning("No DART corp_code for ticker %s", ticker)
            return []

        end = datetime.now()
        start = end - timedelta(days=since_days)
        try:
            resp = httpx.get(
                f"{DART_BASE_URL}/list.json",
                params={
                    "crtfc_key": api_key,
                    "corp_code": corp_code,
                    "bgn_de": start.strftime("%Y%m%d"),
                    "end_de": end.strftime("%Y%m%d"),
                    "page_count": min(limit, 100),
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("DART list.json failed for %s: %s", ticker, e)
            return []

        if data.get("status") not in ("000", "013"):
            # 013 = no data found within the window — not an error.
            logger.warning(
                "DART error for %s: status=%s message=%s",
                ticker,
                data.get("status"),
                data.get("message"),
            )
            return []

        items: list[Disclosure] = []
        for entry in data.get("list", [])[:limit]:
            rcept_no = entry.get("rcept_no", "")
            items.append(
                Disclosure(
                    rcept_no=rcept_no,
                    report_nm=entry.get("report_nm", ""),
                    flr_nm=entry.get("flr_nm", ""),
                    rcept_dt=entry.get("rcept_dt", ""),
                    url=DART_REPORT_URL_TEMPLATE.format(rcept_no) if rcept_no else "",
                )
            )
        return items

    def search_stocks(self, query: str, limit: int = 10) -> list[dict]:
        """Search Korean stocks by name or ticker.

        Uses yfinance search (Korean tickers with .KS/.KQ suffix), falling
        back to direct PyKRX ticker name lookup if query looks like a ticker code.

        Args:
            query: Company name (Korean or English) or ticker code.
            limit: Maximum results.

        Returns:
            List of dicts with 'ticker' and 'name' keys.
        """
        # If query looks like a ticker code, try direct lookup
        if query.isdigit():
            try:
                from pykrx import stock as krx_stock

                ticker = query.zfill(6)
                name = krx_stock.get_market_ticker_name(ticker)
                if name:
                    return [{"ticker": ticker, "name": name, "market": "KR"}]
            except Exception:
                pass

        # Use yfinance search for Korean stocks
        try:
            import yfinance as yf

            results = []
            # Try direct ticker with .KS and .KQ suffixes
            for suffix in [".KS", ".KQ"]:
                try:
                    t = yf.Ticker(f"{query}{suffix}")
                    info = t.info
                    if info.get("symbol") and info.get("regularMarketPrice"):
                        ticker_code = info["symbol"].split(".")[0]
                        results.append(
                            {
                                "ticker": ticker_code,
                                "name": info.get("longName", info.get("shortName", "")),
                                "market": "KR",
                            }
                        )
                except Exception:
                    continue

            # If query is Korean text, try known major tickers
            if not results and any("\uac00" <= c <= "\ud7a3" for c in query):
                from pykrx import stock as krx_stock

                # Try well-known tickers for name matching
                well_known = [
                    "005930",
                    "000660",
                    "035420",
                    "051910",
                    "006400",
                    "035720",
                    "005380",
                    "068270",
                    "105560",
                    "003670",
                    "055550",
                    "034730",
                    "028260",
                    "012330",
                    "066570",
                    "032830",
                    "096770",
                    "003550",
                    "015760",
                    "017670",
                ]
                for t in well_known:
                    try:
                        name = krx_stock.get_market_ticker_name(t)
                        if query.lower() in name.lower():
                            results.append({"ticker": t, "name": name, "market": "KR"})
                            if len(results) >= limit:
                                break
                    except Exception:
                        continue

            return results[:limit]
        except Exception as e:
            logger.error("Stock search failed: %s", e)
            return []

    def is_healthy(self) -> bool:
        """Check if PyKRX can respond."""
        try:
            from pykrx import stock as krx_stock

            today = datetime.now().strftime("%Y%m%d")
            # Quick check: get Samsung Electronics price
            krx_stock.get_market_ticker_name("005930")
            return True
        except Exception:
            return False

    def _normalize_ticker(self, ticker: str) -> str:
        """Pad ticker to 6 digits if needed."""
        return ticker.zfill(6)

    def _pykrx_price_history(self, ticker: str, days: int) -> list[OHLCV]:
        """Fetch from PyKRX."""
        from pykrx import stock as krx_stock

        end = datetime.now()
        start = end - timedelta(days=days + 10)  # buffer for non-trading days
        df = krx_stock.get_market_ohlcv_by_date(
            start.strftime("%Y%m%d"),
            end.strftime("%Y%m%d"),
            ticker,
        )
        if df.empty:
            raise ValueError(f"No data returned from PyKRX for {ticker}")

        bars = []
        for date_idx, row in df.iterrows():
            bars.append(
                OHLCV(
                    date=date_idx.strftime("%Y-%m-%d"),
                    open=float(row["시가"]),
                    high=float(row["고가"]),
                    low=float(row["저가"]),
                    close=float(row["종가"]),
                    volume=int(row["거래량"]),
                )
            )
        return bars[-days:] if len(bars) > days else bars

    def _fdr_price_history(self, ticker: str, days: int) -> list[OHLCV]:
        """Fallback: fetch from FinanceDataReader."""
        import FinanceDataReader as fdr

        end = datetime.now()
        start = end - timedelta(days=days + 10)
        df = fdr.DataReader(
            ticker, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
        )
        if df.empty:
            raise ValueError(f"No data returned from FDR for {ticker}")

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

    # Column name variants across PyKRX versions, in preference order.
    # Each tuple is (per_col, pbr_col, div_col).
    _FUNDAMENTAL_COLUMN_CANDIDATES: list[tuple[str, str, str]] = [
        ("PER", "PBR", "DIV"),
        ("PER", "PBR", "배당수익률"),
        ("per", "pbr", "div"),
    ]

    @staticmethod
    def _extract_fundamentals_from_row(
        row, ticker: str, name: str
    ) -> Optional[StockFundamentals]:
        """Try known column-name variants and return StockFundamentals, or None.

        Args:
            row: A pandas Series representing one ticker row from the bulk
                 fundamentals DataFrame.
            ticker: 6-digit KRX ticker code (used for the result object).
            name: Human-readable company name.

        Returns:
            StockFundamentals populated from the first matching column set,
            or None if no known variant matches.
        """
        for (
            per_col,
            pbr_col,
            div_col,
        ) in KoreanMarketProvider._FUNDAMENTAL_COLUMN_CANDIDATES:
            if per_col in row.index and pbr_col in row.index and div_col in row.index:
                return StockFundamentals(
                    ticker=ticker,
                    name=name,
                    pe_ratio=float(row[per_col]) if row[per_col] != 0 else None,
                    pb_ratio=float(row[pbr_col]) if row[pbr_col] != 0 else None,
                    dividend_yield=float(row[div_col]) if row[div_col] != 0 else None,
                )
        return None

    def _pykrx_fundamentals(self, ticker: str) -> Optional[StockFundamentals]:
        """Fetch fundamentals from PyKRX.

        Tries the bulk market-wide call first and extracts the row for
        *ticker*.  If the columns don't match any known variant, falls back to
        the per-ticker ``get_market_fundamental`` date-range call.

        Args:
            ticker: 6-digit KRX ticker code.

        Returns:
            StockFundamentals with available data, or None.

        Raises:
            ValueError: When both the bulk call and the per-ticker fallback
                produce incompatible column names.
        """
        from pykrx import stock as krx_stock

        today = datetime.now().strftime("%Y%m%d")
        name = krx_stock.get_market_ticker_name(ticker)

        # --- Attempt 1: bulk market-wide snapshot ---
        try:
            df = krx_stock.get_market_fundamental_by_ticker(today, market="ALL")
            if ticker not in df.index:
                return StockFundamentals(ticker=ticker, name=name)

            row = df.loc[ticker]
            result = self._extract_fundamentals_from_row(row, ticker, name)
            if result is not None:
                return result
            # Column set not recognised — log and try the per-ticker fallback.
            logger.debug(
                "Unknown fundamental columns for %s in bulk call: %s",
                ticker,
                list(df.columns),
            )
        except KeyError as exc:
            logger.debug("KeyError in bulk fundamental call for %s: %s", ticker, exc)

        # --- Attempt 2: per-ticker date-range call ---
        try:
            end = datetime.now()
            start = end - timedelta(days=5)
            df2 = krx_stock.get_market_fundamental(
                start.strftime("%Y%m%d"),
                end.strftime("%Y%m%d"),
                ticker,
            )
            if df2.empty:
                return StockFundamentals(ticker=ticker, name=name)

            row2 = df2.iloc[-1]
            result = self._extract_fundamentals_from_row(row2, ticker, name)
            if result is not None:
                return result
            raise ValueError(
                f"PyKRX fundamental columns incompatible for {ticker}; "
                f"got columns: {list(df2.columns)}"
            )
        except KeyError as exc:
            raise ValueError(
                f"PyKRX fundamental columns incompatible for {ticker}: {exc}"
            ) from exc

    def _yfinance_fundamentals(self, ticker: str) -> Optional[StockFundamentals]:
        """Fallback: fetch Korean stock fundamentals via yfinance.

        Korean KOSPI tickers use .KS suffix, KOSDAQ uses .KQ.
        """
        import yfinance as yf

        # Try KOSPI first (.KS), then KOSDAQ (.KQ)
        for suffix in [".KS", ".KQ"]:
            yf_ticker = f"{ticker}{suffix}"
            try:
                t = yf.Ticker(yf_ticker)
                info = t.info
                if info.get("symbol") and info.get("regularMarketPrice"):
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
            except Exception:
                continue
        raise ValueError(f"yfinance failed for {ticker}")

    # ------------------------------------------------------------------
    # News & disclosure helpers
    # ------------------------------------------------------------------

    def _scrape_naver_news(
        self, ticker: str, limit: int, since_days: int
    ) -> list[NewsItem]:
        """Parse the per-stock news listing on finance.naver.com.

        The page returns a table of articles with title, source, and a
        date in either ``YYYY.MM.DD HH:MM`` or ``MM.DD HH:MM`` form. We
        emit ISO ``YYYY-MM-DD`` and apply a best-effort since_days filter.
        """
        from bs4 import BeautifulSoup

        # Naver loads news_news.naver inside an iframe on news.naver and
        # only renders the actual listing when both the parent Referer and
        # an (empty) clusterId param are present. Without them the page
        # comes back as a placeholder showing "no news" — fooled us in
        # an earlier version. Both must stay.
        resp = httpx.get(
            NAVER_NEWS_URL,
            params={"code": ticker, "page": 1, "clusterId": ""},
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"
                ),
                "Referer": f"https://finance.naver.com/item/news.naver?code={ticker}",
            },
            timeout=15,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        rows = soup.select("table.type5 tr")
        cutoff = datetime.now() - timedelta(days=since_days)
        items: list[NewsItem] = []
        for row in rows:
            title_cell = row.select_one("td.title a")
            source_cell = row.select_one("td.info")
            date_cell = row.select_one("td.date")
            if not (title_cell and date_cell):
                continue

            headline = title_cell.get_text(strip=True)
            href = title_cell.get("href", "")
            if href and not href.startswith("http"):
                href = f"https://finance.naver.com{href}"

            source = (
                source_cell.get_text(strip=True) if source_cell else "Naver Finance"
            )

            raw_date = date_cell.get_text(strip=True)
            iso_date, dt_obj = self._parse_naver_date(raw_date)
            if dt_obj and dt_obj < cutoff:
                continue

            items.append(
                NewsItem(
                    headline=headline,
                    source=source,
                    date=iso_date,
                    url=href,
                )
            )
            if len(items) >= limit:
                break

        return items

    @staticmethod
    def _parse_naver_date(raw: str) -> tuple[str, Optional[datetime]]:
        """Parse Naver date strings into (ISO date, datetime) — best-effort."""
        raw = raw.strip()
        # Format: "2026.05.10 13:42" or "2026-05-10 13:42"
        m_full = re.match(r"^(\d{4})[.\-](\d{2})[.\-](\d{2})", raw)
        if m_full:
            y, mo, d = m_full.groups()
            try:
                dt = datetime(int(y), int(mo), int(d))
                return f"{y}-{mo.zfill(2)}-{d.zfill(2)}", dt
            except ValueError:
                pass
        # Format: "05.10 13:42" (current year implied)
        m_short = re.match(r"^(\d{2})[.\-](\d{2})\s", raw)
        if m_short:
            mo, d = m_short.groups()
            year = datetime.now().year
            try:
                dt = datetime(year, int(mo), int(d))
                return f"{year}-{mo.zfill(2)}-{d.zfill(2)}", dt
            except ValueError:
                pass
        return raw, None

    def _lookup_dart_corp_code(self, ticker: str, api_key: str) -> Optional[str]:
        """Map 6-digit KRX ticker → 8-digit DART corp_code.

        Reads the CSV cache at ``data/dart_corp_codes.csv``. If absent,
        downloads the corpCode.xml zip from DART, parses it, and writes
        the cache. Subsequent calls are cheap dict lookups.
        """
        if not DART_CORP_CODE_CSV.exists():
            try:
                self._download_dart_corp_codes(api_key, DART_CORP_CODE_CSV)
            except Exception as e:
                logger.warning("DART corp_code download failed: %s", e)
                return None

        try:
            with DART_CORP_CODE_CSV.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("stock_code", "").strip() == ticker:
                        return row.get("corp_code", "").strip()
        except Exception as e:
            logger.warning("Reading DART corp_code cache failed: %s", e)
        return None

    @staticmethod
    def _download_dart_corp_codes(api_key: str, dest: Path) -> None:
        """Download and unzip CORPCODE.xml, write a CSV mapping to dest.

        DART ships the full mapping as a zip at /api/corpCode.xml. We
        keep only listed companies (those with a non-empty stock_code).

        Concurrency-safe: writes to a temp file in the destination dir
        and atomically renames into place. Two concurrent callers may
        both download; the last writer wins on the rename, but neither
        sees a partially-written CSV.
        """
        resp = httpx.get(
            f"{DART_BASE_URL}/corpCode.xml",
            params={"crtfc_key": api_key},
            timeout=60,
        )
        resp.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            with zf.open("CORPCODE.xml") as fh:
                tree = ET.parse(fh)

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest.with_suffix(dest.suffix + f".tmp.{os.getpid()}")
        try:
            with tmp_path.open("w", newline="", encoding="utf-8") as out:
                writer = csv.writer(out)
                writer.writerow(["corp_code", "corp_name", "stock_code", "modify_date"])
                for node in tree.getroot().findall("list"):
                    stock_code = (node.findtext("stock_code") or "").strip()
                    if not stock_code:
                        # Skip unlisted entities — they won't match a KRX ticker.
                        continue
                    writer.writerow(
                        [
                            (node.findtext("corp_code") or "").strip(),
                            (node.findtext("corp_name") or "").strip(),
                            stock_code,
                            (node.findtext("modify_date") or "").strip(),
                        ]
                    )
            os.replace(tmp_path, dest)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
