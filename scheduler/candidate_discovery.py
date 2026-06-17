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
import re
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
        discovery_source: Which discovery stream produced this candidate —
            ``"momentum"`` (the legacy 5d-return/volume filter), ``"presurge"``
            (the pre-surge base/pullback/RS/pre-earnings engine), ``"sector"``
            (a sector-rotation leader), or ``"anchor"``. Defaults to
            ``"momentum"`` so all existing constructors keep their meaning.
        setup_type: For pre-surge candidates, the matched setup —
            ``"base_pivot"`` | ``"pullback"`` | ``"rs_leader"`` |
            ``"pre_earnings"``; ``None`` for momentum/anchor candidates.
        watch_only: True when downstream gating has demoted this candidate to
            WATCH-only (e.g. a parabolic >20% / overextended momentum name) so
            it must not be logged as a new BULL.
        sector_verdict: Optional sector-rotation verdict carried for this
            candidate's sector — ``"FAVOR"`` | ``"AVOID"`` | ``"ROTATING"`` |
            ``None`` (populated by the sector-rotation consumer).
        sector_stage: Optional sector lifecycle stage — e.g. ``"early"`` |
            ``"late"`` | ``None``.
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
    discovery_source: str = "momentum"
    setup_type: Optional[str] = None
    watch_only: bool = False
    sector_verdict: Optional[str] = None
    sector_stage: Optional[str] = None


# Three KR anchors. Always included regardless of dynamic filter score so
# the prompt has stable cross-market reference. Trimmed from the previous
# 6-ticker hardcoded list (Stage A goal); NAVER, LG화학, 삼SDI, 현대차 now
# only surface if they clear the dynamic filter.
ANCHORS_KR: tuple[tuple[str, str], ...] = (
    ("005930", "삼성전자"),
    ("000660", "SK하이닉스"),
    ("069500", "KODEX 200"),
)

# Three US anchors — broad-market ETFs analogous to the KR trio. SPY = S&P 500,
# QQQ = NASDAQ 100, DIA = Dow Jones 30. These always appear in the briefing's
# candidate block regardless of dynamic filter score, giving the LLM stable
# breadth reference points alongside any individual-stock breakouts.
ANCHORS_US: tuple[tuple[str, str], ...] = (
    ("SPY", "SPDR S&P 500 ETF"),
    ("QQQ", "Invesco QQQ Trust"),
    ("DIA", "SPDR Dow Jones ETF"),
)

# Filter thresholds. Empirical: 5/13 KR breakouts (현대오토에버 +55%, LG전자
# +34%) sit far above 15%, anchor names typically below. US daily moves are
# generally smaller-magnitude than KR mid-caps, so 15% / 2x will be stricter
# in US — adjust at call sites if needed via the optional kwargs.
DEFAULT_RETURN_THRESHOLD_PCT = 15.0
DEFAULT_VOL_RATIO_THRESHOLD = 2.0


def _normalise_csv_ticker(raw: str) -> Optional[str]:
    """Validate a CSV ticker cell and return the 6-digit normalised form.

    KRX tickers are always exactly 6 decimal digits. The CSV ships them
    as-is, but manual quarterly edits can introduce typos — empty cells,
    stray text, wrong length. Reject anything that isn't 1–6 digits so
    a typo doesn't slip into ``get_price_history_batch`` and burn per-
    ticker FDR retry backoff for every cron run (Codex PR #13 finding).
    """
    stripped = (raw or "").strip()
    if not stripped or len(stripped) > 6 or not stripped.isdigit():
        return None
    return stripped.zfill(6)


STATIC_UNIVERSE_PATH = PROJECT_ROOT / "data" / "kr_universe.csv"
STATIC_US_UNIVERSE_PATH = PROJECT_ROOT / "data" / "us_universe.csv"

# Pre-compiled per gemini Stage 5 review (hot path: ~135 candidates per cron).
# Strict form per code-reviewer-pro: 1-5 uppercase letters, optionally followed
# by a single `.X` or `-X` class separator where X is a letter. Rejects digits
# entirely (no legitimate US ticker has them; class suffixes are always letter).
_US_TICKER_RE = re.compile(r"^[A-Z]{1,5}(?:[.\-][A-Z])?$")


def _normalise_us_ticker(raw: str) -> Optional[str]:
    """Validate a US CSV ticker cell.

    US tickers are 1-5 uppercase letters with an optional `.<letter>` or
    `-<letter>` class separator (BRK.B / BRK-B, BF.B). The validator
    strips whitespace, uppercases, and rejects anything that doesn't fit
    so a manual CSV refresh typo doesn't leak into
    ``get_price_history_batch``.
    """
    stripped = (raw or "").strip().upper()
    if not stripped:
        return None
    return stripped if _US_TICKER_RE.match(stripped) else None


def _load_static_us_universe() -> list[tuple[str, Optional[float], Optional[float]]]:
    """Read the curated US universe CSV (primary data source, no fallback).

    Unlike KR, US has no bulk-by-ticker endpoint — this CSV IS the universe.
    Market cap and trading value are not collected (would need an FMP key +
    per-ticker fan-out); ``score_and_filter`` works fine without them since
    the filter is return/volume-based, and ``format_candidates_for_prompt``
    renders ``cap=n/a`` for US entries.

    Maintenance: refresh quarterly as S&P 500 composition changes;
    delisted tickers are automatically surfaced by the stale-ticker
    logging in ``score_and_filter``.
    """
    if not STATIC_US_UNIVERSE_PATH.exists():
        logger.warning(
            "static US universe CSV not found at %s", STATIC_US_UNIVERSE_PATH
        )
        return []

    out: list[tuple[str, Optional[float], Optional[float]]] = []
    try:
        with STATIC_US_UNIVERSE_PATH.open("r", encoding="utf-8") as f:
            import csv

            reader = csv.DictReader(f)
            for row in reader:
                ticker = _normalise_us_ticker(row.get("ticker", ""))
                if ticker is None:
                    continue
                out.append((ticker, None, None))
    except Exception as exc:  # noqa: BLE001 — never block on CSV parse
        # Log exception class so encoding vs permission vs malformed
        # rows are distinguishable in cron logs (gemini Stage 5 nit).
        logger.error(
            "failed to read %s: %s: %s",
            STATIC_US_UNIVERSE_PATH,
            type(exc).__name__,
            exc,
        )
        return []
    return out


def _load_static_us_universe_names() -> dict[str, str]:
    """Ticker → name map from the US static CSV. Mirrors the KR helper."""
    if not STATIC_US_UNIVERSE_PATH.exists():
        return {}
    out: dict[str, str] = {}
    try:
        with STATIC_US_UNIVERSE_PATH.open("r", encoding="utf-8") as f:
            import csv

            for row in csv.DictReader(f):
                ticker = _normalise_us_ticker(row.get("ticker", ""))
                name = (row.get("name") or "").strip()
                if ticker and name:
                    out[ticker] = name
    except Exception as exc:  # noqa: BLE001
        logger.warning("US static name map read failed: %s", exc)
    return out


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
                ticker = _normalise_csv_ticker(row.get("ticker", ""))
                if ticker is None:
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
                ticker = _normalise_csv_ticker(row.get("ticker", ""))
                name = (row.get("name") or "").strip()
                if ticker and name:
                    out[ticker] = name
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
    market: str = "KR",
) -> list[Candidate]:
    """Filter the universe by momentum and volume, returning Candidates.

    For each ticker the function pulls 30 trading days of OHLCV (one
    parallel batch call), computes ``return_5d_pct`` and ``vol_ratio_5d``,
    and keeps the ticker if **either** clears its threshold.

    Args:
        universe: Output of ``enumerate_kr_universe`` or
            ``_load_static_us_universe``.
        provider: A ``MarketDataProvider`` instance. Tests pass in a
            mock that implements ``get_price_history_batch``.
        return_threshold_pct: ``|return_5d_pct| >= this`` → reason="momentum".
        vol_ratio_threshold: ``vol_ratio_5d >= this`` → reason="volume".
        market: ``"KR"`` or ``"US"``. Stamped on every returned
            ``Candidate.market`` so ``format_candidates_for_prompt`` can
            branch on it without callers needing a post-hoc relabel pass.

    Returns:
        Surviving candidates. Network/provider failure during the batch
        fetch returns ``[]`` so the caller's anchor-only fallback path
        still runs — never raises (Codex Stage 5 P2 finding).
    """
    if not universe:
        return []

    tickers = [t for (t, _, _) in universe]
    meta = {t: (cap, val) for (t, cap, val) in universe}
    # days=35 (bumped from 30) ensures both providers return ≥ 25 bars
    # for _vol_ratio's 25-bar window (recent 5 + prior 20). PyKRX (KR)
    # returns ~26 bars at days=30; yfinance (US) returns ~23, below the
    # threshold. Raising to days=35 lifts both safely. KR unaffected
    # (30 → 35 bars, harmless).
    # Wrapped in try/except so transient yfinance/network failures
    # collapse to an empty survivor set instead of aborting the briefing
    # before the anchor-only fallback path can run.
    try:
        bars_by_ticker = provider.get_price_history_batch(tickers, days=35)
    except Exception as exc:  # noqa: BLE001 — never block on data provider
        logger.warning(
            "score_and_filter: batch price fetch failed (%s); returning empty "
            "survivor set, caller's anchor fallback should still run",
            exc,
        )
        return []

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
                market=market,
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


def discover_us_candidates(
    top_n_output: int = 20,
    return_threshold_pct: float = DEFAULT_RETURN_THRESHOLD_PCT,
    vol_ratio_threshold: float = DEFAULT_VOL_RATIO_THRESHOLD,
    include_anchors: bool = True,
    provider=None,
) -> list[Candidate]:
    """End-to-end US: load CSV universe → filter → merge anchors → sort → truncate.

    Mirrors ``discover_kr_candidates`` but uses the static CSV directly
    (no bulk-by-ticker endpoint exists for US providers like yfinance/FMP
    on a free tier, so the CSV is the universe).

    Args:
        top_n_output: Cap on returned list size.
        return_threshold_pct / vol_ratio_threshold: Filter thresholds.
        include_anchors: When ``False``, return dynamic-only.
        provider: Injectable for tests. Defaults to a fresh
            ``USMarketProvider``.

    Returns:
        Up to ``top_n_output`` ``Candidate`` instances, anchors first
        (always), then dynamic survivors ordered by descending
        ``|return_5d_pct|``.
    """
    # Lazy import — keeps the module importable even when the US provider
    # isn't installed (it brings yfinance etc.) and matches the pattern
    # used by the KR side.
    if provider is None:
        import sys as _sys
        from pathlib import Path as _Path

        _sys.path.insert(
            0, str(_Path(__file__).resolve().parent.parent / "mcp-market-data")
        )
        from providers.us import USMarketProvider

        provider = USMarketProvider()

    universe = _load_static_us_universe()
    survivors = score_and_filter(
        universe,
        provider,
        return_threshold_pct=return_threshold_pct,
        vol_ratio_threshold=vol_ratio_threshold,
        market="US",
    )

    # Name fill from CSV (no provider HTTP needed for the common path —
    # the CSV ships ticker → name as authoritative).
    survivors = _fill_us_names(survivors)

    out: list[Candidate] = []
    seen: set[str] = set()
    if include_anchors:
        for ticker, name in ANCHORS_US:
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
                        market="US",
                        market_cap=None,
                        trading_value=None,
                        return_5d_pct=0.0,
                        vol_ratio_5d=1.0,
                        reason="anchor",
                    )
                )
                seen.add(ticker)

    dynamic_sorted = sorted(
        (c for c in survivors if c.ticker not in seen),
        key=lambda c: abs(c.return_5d_pct),
        reverse=True,
    )
    out.extend(dynamic_sorted)
    return out[:top_n_output]


def format_candidates_for_prompt(cands: list[Candidate]) -> str:
    """Render the candidate list as a markdown block for the LLM.

    Branches on ``cands[0].market`` to choose KR vs US labels (시총
    vs market_cap, 조/억 vs T/B, 후보 종목 vs Candidates). Mixed-market
    lists are not expected from the cron pipeline.

    Args:
        cands: Output of ``discover_kr_candidates`` or
            ``discover_us_candidates``.

    Returns:
        Multi-line string. Empty universe → a single-line fallback the
        prompt can still parse.
    """
    if not cands:
        # Empty path keeps the KR fallback since US briefings always
        # carry the 3 ETF anchors and never hit this branch in practice.
        # If a future caller produces an empty US list, the LLM still
        # gets a parseable section.
        return (
            "## KR 후보 종목\n"
            "  (스캐너 실패 — 동적 발굴 결과 없음. LLM은 기본 앵커만 참고하라.)\n"
        )
    market = cands[0].market.upper()

    if market == "US":
        header = (
            "## US Candidates (static S&P 500 + ETF universe scan, "
            "momentum/volume filter)"
        )
        cap_label = "cap"
        cap_fmt = _format_usd_cap
    else:
        header = (
            "## KR 후보 종목 (시총 top-200 ∪ 거래대금 top-50 스캔, "
            "momentum/volume 필터 통과)"
        )
        cap_label = "시총"
        cap_fmt = _format_krw_cap

    lines = [header]
    for c in cands:
        cap_str = cap_fmt(c.market_cap) if c.market_cap else "n/a"
        ret_str = f"{c.return_5d_pct:+.1f}%"
        vol_str = f"{c.vol_ratio_5d:.2f}x"
        name = c.name or "?"
        lines.append(
            f"  - {c.ticker} {name} [{c.reason}]: "
            f"5d={ret_str}, vol_ratio={vol_str}, {cap_label}={cap_str}"
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


def _format_usd_cap(cap_usd: float) -> str:
    """Format a market cap in $T / $B / $M for US readability."""
    t = 10**12
    b = 10**9
    m = 10**6
    if cap_usd >= t:
        return f"${cap_usd / t:,.2f}T"
    if cap_usd >= b:
        return f"${cap_usd / b:,.1f}B"
    if cap_usd >= m:
        return f"${cap_usd / m:,.0f}M"
    return f"${cap_usd:,.0f}"


def _fill_us_names(cands: list[Candidate]) -> list[Candidate]:
    """Populate ``Candidate.name`` from the US static CSV map.

    Unlike the KR path there's no PyKRX-style ticker-name endpoint to
    fall back on for US — the CSV is the authoritative source. Out-of-
    CSV tickers (rare; only possible if a caller hands us a custom
    universe) keep ``name=""`` and render as "?" in the prompt.
    """
    static_names = _load_static_us_universe_names()
    for c in cands:
        if not c.name:
            c.name = static_names.get(c.ticker, "")
    return cands
