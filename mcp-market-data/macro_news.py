"""Global macro / geopolitical news via the GDELT DOC 2.0 API.

Feeds the daily-briefing macro / LLM_CONTEXT layer with broad market-moving
world news (wars, oil/energy shocks, central-bank moves, tariffs/sanctions)
that the per-ticker feeds (Finnhub / Naver) miss. GDELT is free and needs no
API key — but it is rate-limited during peak events (exactly the geopolitical
moments this layer cares about), so responses are cached with a short TTL and
the last good result is served on error, and naive requests need a User-Agent
or GDELT returns 429.

Returns plain ``NewsItem`` objects (headline / source / date / url); GDELT's
``artlist`` mode carries no per-article tone, so ``sentiment_score`` is None and
sentiment is left to the downstream LLM read.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

import httpx

from providers.base import NewsItem

logger = logging.getLogger(__name__)

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
USER_AGENT = (
    "stock-expectation/1.0 (macro-news; +https://github.com/k1064190/stock-expectation)"
)

# No-key wire-service RSS feeds for the macro fallback. GDELT is rate-limited
# per-IP (unusable from shared IPs); RSS is per-publisher and reliable. Verified
# 200 + parseable RSS 2.0: global macro/geopolitics + Korea.
MACRO_RSS_FEEDS = (
    ("BBC", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("CNBC", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("Yonhap", "https://en.yna.co.kr/RSS/news.xml"),
)

# Curated market-moving macro / geopolitical query (GDELT Boolean syntax:
# space = AND, OR explicit, quotes = phrase). Tunable via the CLI --query flag.
DEFAULT_MACRO_QUERY = (
    # One OR-group ANDed with sourcelang — GDELT does not allow nested OR groups,
    # so the whole expression is NOT wrapped in an extra outer paren.
    '(inflation OR recession OR tariff OR "trade war" OR sanctions OR OPEC OR '
    '"interest rate" OR "central bank" OR "Federal Reserve" OR "oil price" OR '
    '"rate cut" OR "rate hike" OR "stock market") sourcelang:eng'
)
DEFAULT_TIMESPAN = "24h"
CACHE_TTL_SECONDS = 900  # 15 min — matches GDELT's ingestion cadence
CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "cache"


def _cache_file(cache_dir: Path, query: str, timespan: str, limit: int) -> Path:
    key = hashlib.md5(f"{query}|{timespan}|{limit}".encode()).hexdigest()[:12]
    return cache_dir / f"macro_news_{key}.json"


def _read_cache(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None  # reject wrong-shape JSON


def _cache_is_fresh(entry: dict, ttl_seconds: int) -> bool:
    try:
        return (time.time() - float(entry.get("fetched_at", 0))) < ttl_seconds
    except (TypeError, ValueError):
        return False  # bad/missing fetched_at → treat as stale


def _items_from_cache(entry: dict) -> Optional[list[NewsItem]]:
    """Reconstruct NewsItems from a cache entry; None if the shape is invalid.

    Requires an actual ``items`` list — a missing or null ``items`` is treated as
    a malformed cache (None → refetch), not a valid empty result.
    """
    items = entry.get("items")
    if not isinstance(items, list):
        return None
    try:
        return [NewsItem(**d) for d in items]
    except (TypeError, AttributeError):
        return None


def _parse_seendate(seendate: str) -> str:
    """GDELT seendate 'YYYYMMDDTHHMMSSZ' → ISO date 'YYYY-MM-DD' (best-effort)."""
    try:
        return datetime.strptime(seendate, "%Y%m%dT%H%M%SZ").date().isoformat()
    except (ValueError, TypeError):
        return ""


def fetch_macro_news(
    query: str = DEFAULT_MACRO_QUERY,
    timespan: str = DEFAULT_TIMESPAN,
    limit: int = 20,
    cache_dir: Path = CACHE_DIR,
    ttl_seconds: int = CACHE_TTL_SECONDS,
    max_attempts: int = 2,
    retry_delay: float = 6.0,
) -> list[NewsItem]:
    """Fetch recent global macro/geopolitical headlines from GDELT.

    Args:
        query: GDELT Boolean query (defaults to a curated macro/geopolitical set).
        timespan: Look-back window, e.g. "24h", "1h", "3d".
        limit: Max articles (GDELT ``maxrecords``; capped at 250 by GDELT).
        cache_dir: Where to cache responses for rate-limit resilience.
        ttl_seconds: Serve the cache without refetching if younger than this.
        max_attempts: Fetch attempts before giving up. GDELT limits to one
            request per 5 seconds per IP, so a 429 is retried after ``retry_delay``.
        retry_delay: Seconds to wait between attempts (>= GDELT's 5s cadence).

    Returns:
        NewsItem list (newest-first), ``sentiment_score=None``. On a fresh cache
        hit returns the cached items; on a fetch error returns the last cached
        result if any, else an empty list — never raises.
    """
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("macro-news cache dir unavailable (%s); caching disabled", e)
    path = _cache_file(cache_dir, query, timespan, limit)

    cached = _read_cache(path)
    if cached and _cache_is_fresh(cached, ttl_seconds):
        fresh_items = _items_from_cache(cached)
        if fresh_items is not None:
            return fresh_items

    last_error: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            resp = httpx.get(
                GDELT_DOC_URL,
                params={
                    "query": query,
                    "mode": "artlist",
                    "format": "json",
                    "timespan": timespan,
                    "maxrecords": min(limit, 250),  # GDELT caps ArticleList at 250
                    "sort": "datedesc",
                },
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            items = [
                NewsItem(
                    headline=(a.get("title") or "").strip(),
                    source=a.get("domain", ""),
                    date=_parse_seendate(a.get("seendate", "")),
                    url=a.get("url", ""),
                )
                for a in data.get("articles", [])
                if (a.get("title") or "").strip()
            ]
            # Cache write is best-effort: a permissions/disk error must not
            # discard freshly-fetched headlines (it's inside the fetch try).
            try:
                path.write_text(
                    json.dumps(
                        {
                            "fetched_at": time.time(),
                            "items": [asdict(i) for i in items],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            except OSError as e:
                logger.warning("macro-news cache write failed: %s", e)
            return items
        except Exception as e:  # network / 429 / bad JSON
            last_error = e
            if attempt < max_attempts - 1:
                time.sleep(retry_delay)  # respect GDELT's 1-req-per-5s limit

    # All attempts failed — degrade gracefully, never crash.
    if cached:
        stale = _items_from_cache(cached)
        if stale is not None:
            logger.warning("GDELT fetch failed (%s); serving stale cache", last_error)
            return stale
    logger.warning("GDELT fetch failed (%s) and no cache; returning empty", last_error)
    return []


def _parse_rss_date(raw: str) -> tuple[str, Optional[datetime]]:
    """Parse an RSS RFC-822 pubDate into (ISO date, tz-aware datetime)."""
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.date().isoformat(), dt
    except Exception:
        return "", None


def fetch_rss_macro_news(
    feeds: tuple = MACRO_RSS_FEEDS,
    limit: int = 20,
    since_days: int = 2,
) -> list[NewsItem]:
    """Fetch recent macro headlines from no-key wire-service RSS feeds.

    The reliable, non-IP-throttled fallback for when GDELT is rate-limited.
    Each feed contributes its recent items; results are de-duplicated by URL,
    filtered to the last ``since_days``, sorted newest-first, and capped.
    A feed that errors is skipped (never raises).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    dated: list[tuple[datetime, NewsItem]] = []
    seen: set[str] = set()
    for publisher, url in feeds:
        try:
            resp = httpx.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=15,
                follow_redirects=True,
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as e:
            logger.warning("Macro RSS feed %s failed: %s", url, e)
            continue
        for node in root.findall(".//item"):
            headline = (node.findtext("title") or "").strip()
            link = (node.findtext("link") or "").strip()
            iso_date, dt = _parse_rss_date(node.findtext("pubDate") or "")
            key = link or headline
            if not headline or key in seen:
                continue
            # Require a parseable date so the since_days cutoff is strict and
            # ordering is by actual publish time, not date-string ties.
            if dt is None or dt < cutoff:
                continue
            seen.add(key)
            dated.append(
                (
                    dt,
                    NewsItem(
                        headline=headline, source=publisher, date=iso_date, url=link
                    ),
                )
            )
    dated.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in dated[:limit]]


def _timespan_to_days(timespan: str) -> int:
    """Approximate any GDELT timespan as whole days for the day-granular RSS
    window (minimum 1; defaults to 2 if unparseable).

    Handles both short and word units GDELT accepts — min/minutes, h/hours,
    d/days, w/weeks, m/months, y/years — by extracting the alpha unit so e.g.
    '24hours' isn't read as 24 days and 'min' isn't mistaken for months.
    """
    try:
        ts = timespan.strip().lower()
        n = int("".join(ch for ch in ts if ch.isdigit()) or "0")
        if not n:
            return 2
        unit = "".join(ch for ch in ts if ch.isalpha())
        if unit.startswith("min"):  # minutes (sub-day)
            return 1
        if unit in ("h", "hour", "hours"):
            return max(1, -(-n // 24))  # ceil hours → days
        if unit in ("w", "week", "weeks"):
            return max(1, n * 7)
        if unit in ("m", "month", "months"):
            return max(1, n * 30)
        if unit in ("y", "year", "years"):
            return max(1, n * 365)
        return max(1, n)  # d/day/days or a bare number
    except Exception:
        return 2


def get_macro_news(
    query: str = DEFAULT_MACRO_QUERY,
    timespan: str = DEFAULT_TIMESPAN,
    limit: int = 20,
    cache_dir: Path = CACHE_DIR,
) -> tuple[list[NewsItem], str]:
    """Best-available macro news: editorial RSS primary, GDELT fallback.

    RSS (BBC/CNBC/Yonhap) is reliable, English, and editorially curated — higher
    precision than GDELT's broad multilingual keyword recall, and not IP-throttled
    like GDELT (which 429s frequently from shared IPs). GDELT is the breadth
    fallback used only when RSS returns nothing.

    ``timespan`` is honored on both paths — passed verbatim to GDELT and mapped to
    a whole-day window for RSS. ``query`` only constrains the GDELT path (RSS is a
    fixed curated feed set, not keyword-queryable).

    Returns ``(items, source)`` where source is "rss", "gdelt", or "none" — so
    callers can see which path served.
    """
    rss = fetch_rss_macro_news(limit=limit, since_days=_timespan_to_days(timespan))
    if rss:
        return rss, "rss"
    gdelt = fetch_macro_news(
        query=query, timespan=timespan, limit=limit, cache_dir=cache_dir
    )
    if gdelt:
        logger.info("RSS empty; macro news via GDELT fallback (%d)", len(gdelt))
        return gdelt, "gdelt"
    return [], "none"


def format_macro_for_prompt(items: list[NewsItem]) -> str:
    """Render a compact macro-headlines block for the daily-briefing prompt."""
    lines = [
        "## Global macro / geopolitical headlines (last 24h)",
        "Context for the macro-regime / LLM_CONTEXT read — NOT pick slots.",
        "",
    ]
    if not items:
        lines.append("_(no macro headlines available)_")
        return "\n".join(lines)
    for it in items:
        date = f"[{it.date}] " if it.date else ""
        lines.append(f"- {date}{it.source}: {it.headline}")
    return "\n".join(lines)
