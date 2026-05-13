"""Dynamic candidate discovery for the KR daily briefing.

Replaces the hardcoded 6-ticker blue-chip list that
``scheduler.daily_briefing.fetch_kr_market_data`` used to ship to the LLM.
Workflow:

  1. ``enumerate_kr_universe`` pulls one bulk PyKRX call and takes the union
     of (top-N by 시가총액) and (top-N by 거래대금). This gives ~200-250
     KOSPI/KOSDAQ tickers covering both mega-caps and mid-caps with
     elevated trading interest.
  2. ``score_and_filter`` runs ``KoreanMarketProvider.get_price_history_batch``
     once for the union, computes 5-day return and 5-vs-prior-20 volume
     ratio per ticker, and keeps anything that clears either threshold.
  3. ``discover_kr_candidates`` merges the filtered survivors with a small
     anchor list (Samsung / SK Hynix / KOSPI ETF) so the prompt always has
     a stable reference trio, then sorts by a simple priority score and
     truncates to ``top_n_output``.
  4. ``format_candidates_for_prompt`` renders a Korean-language block the
     LLM reads directly.

Failure mode: any PyKRX exception collapses to "anchors only" — the
briefing never crashes for lack of dynamic candidates.

Stage A leaves ``Candidate.news_count_7d = 0``;
``scheduler.theme_clusterer`` backfills it in Stage B and may trigger a
re-rank.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT / "mcp-market-data") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "mcp-market-data"))

from providers.kr import KoreanMarketProvider  # noqa: E402

logger = logging.getLogger(__name__)


@dataclass
class Candidate:
    """A single ticker selected for the briefing prompt.

    Args:
        ticker: 6-digit KRX code, zero-padded.
        name: Korean stock name; empty string when name lookup fails.
        market: Always "KR" in Stage A — kept as a field so the same
            dataclass can be reused for US in a later PR.
        market_cap: KRW 시가총액. ``None`` when the bulk call missed.
        trading_value: KRW 거래대금 from the same bulk call. ``None`` when
            absent.
        return_5d_pct: ``(close[-1] / close[-6] - 1) * 100``. ``0.0`` when
            fewer than 6 bars are available.
        vol_ratio_5d: ``mean(vol[-5:]) / mean(vol[-25:-5])``. ``1.0`` when
            fewer than 25 bars or the prior 20-day average is zero.
        news_count_7d: Headline count over the last 7 days. Stage A leaves
            this 0; Stage B's theme_clusterer backfills it.
        reason: Why this ticker made the cut — ``"anchor"`` |
            ``"momentum"`` | ``"volume"``. Anchors are always included.
    """

    ticker: str
    name: str
    market: str
    market_cap: Optional[float]
    trading_value: Optional[float]
    return_5d_pct: float
    vol_ratio_5d: float
    news_count_7d: int = 0
    reason: str = "momentum"


# Three KR anchors. Always included regardless of dynamic filter score so
# the prompt has stable cross-market reference. Trimmed from the previous
# 6-ticker hardcoded list (Stage A goal); NAVER, LG화학, 삼SDI, 현대차 now
# only surface if they clear the dynamic filter.
ANCHORS_KR: tuple[tuple[str, str], ...] = (
    ("005930", "삼성전자"),
    ("000660", "SK하이닉스"),
    ("069500", "KODEX 200"),
)

# Filter thresholds. Empirical: 5/13 breakouts (현대오토에버 +55%, LG전자
# +34%) sit far above 15%, anchor names typically below.
DEFAULT_RETURN_THRESHOLD_PCT = 15.0
DEFAULT_VOL_RATIO_THRESHOLD = 2.0


STATIC_UNIVERSE_PATH = PROJECT_ROOT / "data" / "kr_universe.csv"


def _load_static_universe() -> list[tuple[str, Optional[float], Optional[float]]]:
    """Read the curated KR universe CSV.

    Used as the fallback when PyKRX's bulk-by-ticker endpoint is broken
    (which is the current state as of 2026-05; KRX changed its response
    format and PyKRX 1.2.x does not handle it). The CSV ships
    ticker+name+market_segment for ~150 KOSPI/KOSDAQ/ETF names — the
    market_cap/trading_value columns are unknown in this mode (return
    None) since those numbers also flow through the broken endpoint.

    The companion ``_load_static_universe_names`` exposes the ticker→
    name mapping for ``_fill_names`` so survivors get readable labels
    without needing a per-ticker PyKRX name HTTP call.
    """
    if not STATIC_UNIVERSE_PATH.exists():
        logger.warning("static universe CSV not found at %s", STATIC_UNIVERSE_PATH)
        return []

    out: list[tuple[str, Optional[float], Optional[float]]] = []
    try:
        with STATIC_UNIVERSE_PATH.open("r", encoding="utf-8") as f:
            import csv

            reader = csv.DictReader(f)
            for row in reader:
                ticker = (row.get("ticker") or "").strip().zfill(6)
                if not ticker:
                    continue
                out.append((ticker, None, None))
    except Exception as exc:  # noqa: BLE001 — never block the briefing on CSV parse
        logger.error("failed to read %s: %s", STATIC_UNIVERSE_PATH, exc)
        return []
    return out


def _load_static_universe_names() -> dict[str, str]:
    """Ticker → name map from the static CSV.

    Used by ``_fill_names`` as the first-priority name source — avoids
    per-ticker ``get_market_ticker_name`` HTTP fan-out when the CSV
    already has the answer. Re-reads on every call; the cost is a single
    small CSV parse per briefing (~166 rows) so caching adds complexity
    without a measurable win.
    """
    if not STATIC_UNIVERSE_PATH.exists():
        return {}
    out: dict[str, str] = {}
    try:
        with STATIC_UNIVERSE_PATH.open("r", encoding="utf-8") as f:
            import csv

            for row in csv.DictReader(f):
                t = (row.get("ticker") or "").strip().zfill(6)
                n = (row.get("name") or "").strip()
                if t and n:
                    out[t] = n
    except Exception as exc:  # noqa: BLE001
        logger.warning("static name map read failed: %s", exc)
    return out


def enumerate_kr_universe(
    top_n_cap: int = 200,
    top_n_value: int = 50,
    today: Optional[str] = None,
) -> list[tuple[str, Optional[float], Optional[float]]]:
    """Return ticker union of (top-N by 시가총액) and (top-N by 거래대금).

    Args:
        top_n_cap: How many largest-cap tickers to include.
        top_n_value: How many highest-trading-value tickers to include.
        today: YYYYMMDD string. Defaults to today's date. PyKRX auto-falls
            back to the most recent business day on holidays.

    Returns:
        List of ``(ticker, market_cap, trading_value)`` tuples. PyKRX
        bulk endpoint failure falls back to the static CSV at
        ``data/kr_universe.csv`` — market_cap/trading_value are None in
        that path (the columns only exist on the live bulk endpoint).
        Empty list only when BOTH live and CSV are unavailable.
    """
    today_str = today or datetime.now().strftime("%Y%m%d")
    try:
        from pykrx import stock as krx_stock

        df = krx_stock.get_market_cap_by_ticker(today_str, market="ALL")
    except Exception as exc:
        logger.warning(
            "PyKRX get_market_cap_by_ticker(%s) failed (%s); "
            "falling back to static CSV universe",
            today_str,
            exc,
        )
        return _load_static_universe()

    if df is None or df.empty:
        logger.warning(
            "PyKRX returned empty market-cap frame for %s; "
            "falling back to static CSV universe",
            today_str,
        )
        return _load_static_universe()

    cap_col = "시가총액" if "시가총액" in df.columns else None
    val_col = "거래대금" if "거래대금" in df.columns else None
    if cap_col is None or val_col is None:
        logger.warning(
            "Unexpected PyKRX columns (got %s); falling back to static CSV",
            list(df.columns),
        )
        return _load_static_universe()

    cap_top = df.nlargest(top_n_cap, cap_col)
    val_top = df.nlargest(top_n_value, val_col)
    union_idx = cap_top.index.union(val_top.index)

    out: list[tuple[str, Optional[float], Optional[float]]] = []
    for ticker in union_idx:
        try:
            cap = float(df.loc[ticker, cap_col])
        except (KeyError, TypeError, ValueError):
            cap = None
        try:
            val = float(df.loc[ticker, val_col])
        except (KeyError, TypeError, ValueError):
            val = None
        out.append((str(ticker).zfill(6), cap, val))
    return out


def score_and_filter(
    universe: list[tuple[str, Optional[float], Optional[float]]],
    provider: KoreanMarketProvider,
    return_threshold_pct: float = DEFAULT_RETURN_THRESHOLD_PCT,
    vol_ratio_threshold: float = DEFAULT_VOL_RATIO_THRESHOLD,
) -> list[Candidate]:
    """Filter the universe by momentum and volume, returning Candidates.

    For each ticker the function pulls 30 trading days of OHLCV (one
    parallel batch call), computes ``return_5d_pct`` and ``vol_ratio_5d``,
    and keeps the ticker if **either** clears its threshold.

    Args:
        universe: Output of ``enumerate_kr_universe``.
        provider: A ``KoreanMarketProvider`` instance. Tests pass in a
            mock that implements ``get_price_history_batch``.
        return_threshold_pct: ``|return_5d_pct| >= this`` → reason="momentum".
        vol_ratio_threshold: ``vol_ratio_5d >= this`` → reason="volume".

    Returns:
        Surviving candidates with ``name=""`` (caller fills name lookups
        on the smaller surviving set to save sequential PyKRX HTTP).
    """
    if not universe:
        return []

    tickers = [t for (t, _, _) in universe]
    meta = {t: (cap, val) for (t, cap, val) in universe}
    bars_by_ticker = provider.get_price_history_batch(tickers, days=30)

    # Surface tickers the provider returned <6 bars for — typically delisted /
    # merged names in the static CSV. Cron operators rely on this log line
    # to decide when to refresh the CSV; without it stale entries silently
    # add 5-15s per cron via FDR retry backoff.
    stale = sorted(t for t in tickers if len(bars_by_ticker.get(t, [])) < 6)
    if stale:
        logger.info(
            "score_and_filter: %d tickers had insufficient price history "
            "(delisted/merged/new?) — CSV refresh candidates: %s",
            len(stale),
            ", ".join(stale[:20]) + ("..." if len(stale) > 20 else ""),
        )

    survivors: list[Candidate] = []
    for ticker, bars in bars_by_ticker.items():
        if len(bars) < 6:
            continue
        closes = [b.close for b in bars]
        volumes = [b.volume for b in bars]
        return_5d_pct = _pct_return(closes[-6], closes[-1])
        vol_ratio_5d = _vol_ratio(volumes)

        passes_return = abs(return_5d_pct) >= return_threshold_pct
        passes_volume = vol_ratio_5d >= vol_ratio_threshold
        if not (passes_return or passes_volume):
            continue

        cap, val = meta.get(ticker, (None, None))
        reason = "momentum" if passes_return else "volume"
        survivors.append(
            Candidate(
                ticker=ticker,
                name="",
                market="KR",
                market_cap=cap,
                trading_value=val,
                return_5d_pct=return_5d_pct,
                vol_ratio_5d=vol_ratio_5d,
                reason=reason,
            )
        )
    return survivors


def discover_kr_candidates(
    top_n_output: int = 20,
    top_n_cap: int = 200,
    top_n_value: int = 50,
    return_threshold_pct: float = DEFAULT_RETURN_THRESHOLD_PCT,
    vol_ratio_threshold: float = DEFAULT_VOL_RATIO_THRESHOLD,
    include_anchors: bool = True,
    provider: Optional[KoreanMarketProvider] = None,
    today: Optional[str] = None,
) -> list[Candidate]:
    """End-to-end: enumerate → filter → merge anchors → sort → truncate.

    Args:
        top_n_output: Cap on returned list size.
        top_n_cap / top_n_value / return_threshold_pct / vol_ratio_threshold:
            Passed through to the lower-level functions.
        include_anchors: When ``False``, return dynamic-only (used by some
            tests). Production callers always pass True.
        provider: Injectable for tests. Defaults to a fresh
            ``KoreanMarketProvider``.
        today: YYYYMMDD override; defaults to today.

    Returns:
        Up to ``top_n_output`` ``Candidate`` instances, anchors first
        (always), then dynamic survivors ordered by descending
        ``|return_5d_pct|``. Callers should not set ``top_n_output`` below
        ``len(ANCHORS_KR)`` (= 3) — the final ``[:top_n_output]`` slice will
        otherwise drop anchors from the tail.
    """
    provider = provider or KoreanMarketProvider()
    universe = enumerate_kr_universe(top_n_cap, top_n_value, today=today)
    survivors = score_and_filter(
        universe,
        provider,
        return_threshold_pct=return_threshold_pct,
        vol_ratio_threshold=vol_ratio_threshold,
    )

    # Look up names only for the surviving set (and anchors). Sequential
    # PyKRX get_market_ticker_name HTTP calls — slow if done over 250
    # universe entries, fast over the typical ~10-50 survivors.
    survivors = _fill_names(survivors)

    out: list[Candidate] = []
    seen: set[str] = set()

    if include_anchors:
        for ticker, name in ANCHORS_KR:
            # If the anchor also passed the dynamic filter, prefer the
            # dynamic entry (it has fresh return/vol numbers) and just
            # relabel the reason. Otherwise inject a bare anchor.
            existing = next((c for c in survivors if c.ticker == ticker), None)
            if existing is not None:
                existing.reason = "anchor"
                out.append(existing)
                seen.add(ticker)
            else:
                out.append(
                    Candidate(
                        ticker=ticker,
                        name=name,
                        market="KR",
                        market_cap=None,
                        trading_value=None,
                        return_5d_pct=0.0,
                        vol_ratio_5d=1.0,
                        reason="anchor",
                    )
                )
                seen.add(ticker)

    # Dynamic survivors, sorted by absolute 5-day return (biggest movers first).
    dynamic_sorted = sorted(
        (c for c in survivors if c.ticker not in seen),
        key=lambda c: abs(c.return_5d_pct),
        reverse=True,
    )
    out.extend(dynamic_sorted)

    return out[:top_n_output]


def format_candidates_for_prompt(cands: list[Candidate]) -> str:
    """Render the candidate list as a Korean-language block for the LLM.

    The output is plain markdown — anchors first, then dynamic survivors.
    Each line carries the ticker, name, reason tag, 5-day return, volume
    ratio, and (when available) market cap rounded to 조/억 units.

    Args:
        cands: Output of ``discover_kr_candidates``.

    Returns:
        Multi-line string. Empty universe → a single-line fallback the
        prompt can still parse.
    """
    if not cands:
        return (
            "## KR 후보 종목\n"
            "  (스캐너 실패 — 동적 발굴 결과 없음. LLM은 기본 앵커만 참고하라.)\n"
        )

    lines = [
        "## KR 후보 종목 (시총 top-200 ∪ 거래대금 top-50 스캔, "
        "momentum/volume 필터 통과)",
    ]
    for c in cands:
        cap_str = _format_krw_cap(c.market_cap) if c.market_cap else "n/a"
        ret_str = f"{c.return_5d_pct:+.1f}%"
        vol_str = f"{c.vol_ratio_5d:.2f}x"
        name = c.name or "?"
        lines.append(
            f"  - {c.ticker} {name} [{c.reason}]: "
            f"5d={ret_str}, vol_ratio={vol_str}, 시총={cap_str}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pct_return(prev: float, last: float) -> float:
    if not prev:
        return 0.0
    return (last / prev - 1.0) * 100.0


def _vol_ratio(volumes: list[int]) -> float:
    """5-day average volume over the prior 20-day average.

    Requires at least 25 bars to give a non-default answer. Returns 1.0
    when the prior-20 average is zero (e.g. brand-new listing) so the
    ticker neither passes nor fails the volume filter spuriously.
    """
    if len(volumes) < 25:
        return 1.0
    recent_5 = volumes[-5:]
    prior_20 = volumes[-25:-5]
    base = sum(prior_20) / len(prior_20)
    if base <= 0:
        return 1.0
    return (sum(recent_5) / len(recent_5)) / base


def _fill_names(cands: list[Candidate]) -> list[Candidate]:
    """Populate ``Candidate.name`` for each surviving candidate.

    Two-tier lookup: the static CSV map first (free, in-process), then
    PyKRX ``get_market_ticker_name`` as a backstop for tickers not in
    the CSV. PyKRX's name endpoint has been resilient even when its
    bulk-by-ticker endpoints are broken, so this remains useful even
    in CSV-fallback mode for any out-of-CSV ticker that snuck in.
    """
    static_names = _load_static_universe_names()
    # First pass: in-place mutation of c.name from the CSV map. The second
    # pass below filters by `not c.name` and sees these updates because
    # both loops iterate the same Candidate references.
    for c in cands:
        if not c.name:
            static_hit = static_names.get(c.ticker)
            if static_hit:
                c.name = static_hit
    # Second pass: PyKRX backstop for tickers still missing a name.
    remaining = [c for c in cands if not c.name]
    if not remaining:
        return cands
    try:
        from pykrx import stock as krx_stock
    except ImportError:
        return cands
    for c in remaining:
        try:
            c.name = krx_stock.get_market_ticker_name(c.ticker) or ""
        except Exception as exc:
            logger.debug("ticker name lookup failed for %s: %s", c.ticker, exc)
            c.name = ""
    return cands


def _format_krw_cap(cap_krw: float) -> str:
    """Format a market cap in 조/억 units for Korean readability."""
    jo = 10**12
    eok = 10**8
    if cap_krw >= jo:
        return f"{cap_krw / jo:,.1f}조"
    if cap_krw >= eok:
        return f"{cap_krw / eok:,.0f}억"
    return f"{cap_krw:,.0f}원"
