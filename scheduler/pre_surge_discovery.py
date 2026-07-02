"""Pre-surge candidate discovery — surface tickers BEFORE they run.

The legacy :mod:`candidate_discovery` funnel keeps only tickers that have
*already* moved (``|5d return| >= 15%`` or a 2x volume spike) and the briefing
prompt restricts the LLM to that list, so the daily briefing can only recommend
names that have already surged. A backtest of the closed-buy history showed the
hit rate falls from ~50% for modest-momentum (10-20% trailing-month) entries to
~24% for parabolic (>40%) entries — i.e. chasing the move hurts. This module
adds the orthogonal "not yet extended" stream the funnel was missing.

It scores every universe ticker against four setups using only price/volume and
the existing :class:`HorizonMetrics`, plus an optional earnings-calendar map for
the US-only pre-earnings setup:

  1. ``base_pivot``   — volatility-contraction coil near the 20/50-day MAs with a
                        volume dry-up then pickup, not extended.
  2. ``pullback``     — pullback to MA20/MA50 inside an intact MA-stack uptrend.
  3. ``rs_leader``    — relative-strength leader vs its index that is still in the
                        5-20% trailing-month band (not parabolic).
  4. ``pre_earnings`` — (US only) earnings in 5-10 days + volatility compression
                        + slight positive drift.

:func:`score_presurge_setups` is pure (no I/O, no ``datetime.now``) so the as-of
backtest harness can replay it on historical bar slices.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCHEDULER_DIR = str(Path(__file__).resolve().parent)
_MARKET_DATA_DIR = str(PROJECT_ROOT / "mcp-market-data")
for _p in (_SCHEDULER_DIR, _MARKET_DATA_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from candidate_discovery import (  # noqa: E402
    Candidate,
    _fill_names,
    _fill_us_names,
    _load_static_us_universe,
    enumerate_kr_universe,
)
from indicators import (  # noqa: E402
    _close,
    _volume,
    compute_horizon_metrics,
    contraction_ratio,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuning constants (exposed for tests + future calibration)
# ---------------------------------------------------------------------------
# Shared parabolic cap: the trailing-month return above which an entry is a
# momentum chase rather than an early setup (backtest: >20% buckets underperform).
PARABOLIC_RETURN_1M = 0.20

# base_pivot
BASE_RSI_LOW, BASE_RSI_HIGH = 45.0, 65.0
BASE_MA20_PROXIMITY = 0.05  # price within 5% of MA20
BASE_MA50_PROXIMITY = 0.08  # price within 8% of MA50
BASE_CONTRACTION_MAX = 0.75  # recent/prior return-stdev must have contracted

# pullback
PULLBACK_RSI_LOW, PULLBACK_RSI_HIGH = 40.0, 55.0
PULLBACK_PROXIMITY = 0.05  # distance to the nearest support MA for full credit

# rs_leader
RS_RETURN_1M_LOW, RS_RETURN_1M_HIGH = 0.05, 0.20
RS_EXCESS_1M_MIN = 0.05  # must beat its index by >=5pp over the trailing month
RS_EXCESS_FULL_CREDIT = 0.15

# pre_earnings (US only)
PREEARN_DAYS_LOW, PREEARN_DAYS_HIGH = 5, 10
PREEARN_CONTRACTION_MAX = 0.85

MIN_SCORE_DEFAULT = 0.5

# Index proxies for the relative-strength benchmark.
BENCHMARK_PROXY = {"US": "SPY", "KR": "069500"}

# Tie-break priority when a ticker matches several setups at the same score.
_SETUP_PRIORITY = {"pullback": 4, "base_pivot": 3, "rs_leader": 2, "pre_earnings": 1}


@dataclass
class SetupHit:
    """One matched pre-surge setup for a ticker.

    Args:
        setup_type: ``base_pivot`` | ``pullback`` | ``rs_leader`` |
            ``pre_earnings``.
        score: Match strength in ``[0.0, 1.0]`` (higher = cleaner setup).
        detail: Diagnostic numbers behind the score (for logging / debugging).
    """

    setup_type: str
    score: float
    detail: dict = field(default_factory=dict)


def _clamp01(x: float) -> float:
    """Clamp a float to the closed interval [0, 1]."""
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _volume_pattern_score(volumes: list[float], vol_ratio: Optional[float]) -> float:
    """Score a "dry-up then pickup" volume pattern in [0, 1].

    Dry-up = the recent 15-bar average volume (excluding the last 5) sits below
    the older 30-bar average; pickup = the 5/50 ``vol_ratio`` is back in a
    healthy [0.9, 2.0] band (interest returning but not a blow-off).

    Args:
        volumes: Trading volumes, oldest-first.
        vol_ratio: ``vol_5d_avg / vol_50d_avg`` from HorizonMetrics, or None.

    Returns:
        1.0 when both dry-up and pickup hold, 0.5 when exactly one holds, else 0.0.
    """
    dry_up = False
    if len(volumes) >= 50:
        recent_base = sum(volumes[-20:-5]) / 15.0
        older_base = sum(volumes[-50:-20]) / 30.0
        dry_up = older_base > 0 and recent_base < older_base
    pickup = vol_ratio is not None and 0.9 <= vol_ratio <= 2.0
    if dry_up and pickup:
        return 1.0
    return 0.5 if (dry_up or pickup) else 0.0


def _detect_base_pivot(metrics, closes: list[float], volumes: list[float]):
    """Volatility-contraction coil near the 20/50-day MAs, not extended."""
    if metrics.overextension_level != "NONE":
        return None
    rsi = metrics.rsi14
    if rsi is None or not (BASE_RSI_LOW <= rsi <= BASE_RSI_HIGH):
        return None
    if metrics.return_1m is not None and metrics.return_1m >= PARABOLIC_RETURN_1M:
        return None
    ma20, ma50, price = metrics.ma20, metrics.ma50, metrics.current_price
    if ma20 is None or ma20 <= 0 or ma50 is None or ma50 <= 0:
        return None
    coil_dist = abs(price / ma20 - 1.0)
    if coil_dist > BASE_MA20_PROXIMITY:
        return None
    if abs(price / ma50 - 1.0) > BASE_MA50_PROXIMITY or price < ma50 * 0.97:
        return None
    cr = contraction_ratio(closes, 10, 20)
    if cr is None or cr > BASE_CONTRACTION_MAX:
        return None
    contraction_strength = _clamp01((BASE_CONTRACTION_MAX - cr) / BASE_CONTRACTION_MAX)
    coil_tightness = _clamp01(1.0 - coil_dist / BASE_MA20_PROXIMITY)
    volume_pattern = _volume_pattern_score(volumes, metrics.vol_ratio)
    score = 0.4 * contraction_strength + 0.3 * coil_tightness + 0.3 * volume_pattern
    return SetupHit(
        "base_pivot",
        round(score, 4),
        {"contraction_ratio": round(cr, 3), "coil_dist": round(coil_dist, 3)},
    )


def _detect_pullback(metrics, closes: list[float], volumes: list[float]):
    """Pullback to MA20/MA50 inside an intact MA20>MA50>MA200 uptrend."""
    ma20, ma50, ma200, price = (
        metrics.ma20,
        metrics.ma50,
        metrics.ma200,
        metrics.current_price,
    )
    if ma20 is None or ma50 is None or ma200 is None:
        return None
    if not (ma20 > ma50 > ma200):
        return None
    if metrics.overextension_level == "EXTREME":
        return None
    # Apply the shared parabolic cap: a name already up >20% over the trailing
    # month is a momentum chase even if it has dipped near its MAs — keep it out
    # of the pre-surge stream (it belongs to momentum, subject to the store gate).
    if metrics.return_1m is not None and metrics.return_1m >= PARABOLIC_RETURN_1M:
        return None
    rsi = metrics.rsi14
    if rsi is None or not (PULLBACK_RSI_LOW <= rsi <= PULLBACK_RSI_HIGH):
        return None
    # Pulled back into the MA20/MA50 support zone (at/just-above MA20, holding MA50).
    if not (ma50 * 0.98 <= price <= ma20 * 1.02):
        return None
    dist = min(abs(price / ma20 - 1.0), abs(price / ma50 - 1.0))
    proximity = _clamp01(1.0 - dist / PULLBACK_PROXIMITY)
    score = 0.5 + 0.5 * proximity
    return SetupHit("pullback", round(score, 4), {"dist_to_ma": round(dist, 3)})


def _detect_rs_leader(metrics, bench_1m: Optional[float], bench_6m: Optional[float]):
    """Relative-strength leader vs its index, still in the 5-20% momentum band."""
    if metrics.overextension_level == "EXTREME":
        return None
    r1m = metrics.return_1m
    if r1m is None or not (RS_RETURN_1M_LOW <= r1m <= RS_RETURN_1M_HIGH):
        return None
    if bench_1m is None:
        return None
    excess_1m = r1m - bench_1m
    if excess_1m < RS_EXCESS_1M_MIN:
        return None
    # Longer-term leadership confirmation when both 6m figures are available.
    if (
        bench_6m is not None
        and metrics.return_6m is not None
        and metrics.return_6m <= bench_6m
    ):
        return None
    score = 0.5 + 0.5 * _clamp01(excess_1m / RS_EXCESS_FULL_CREDIT)
    return SetupHit("rs_leader", round(score, 4), {"excess_1m": round(excess_1m, 3)})


def _detect_pre_earnings(metrics, closes: list[float], earnings_in_days: Optional[int]):
    """US-only: earnings in 5-10 days + volatility compression + slight drift."""
    if earnings_in_days is None or not (
        PREEARN_DAYS_LOW <= earnings_in_days <= PREEARN_DAYS_HIGH
    ):
        return None
    if metrics.overextension_level == "EXTREME":
        return None
    r1m = metrics.return_1m
    if r1m is None or not (0.0 < r1m < PARABOLIC_RETURN_1M):
        return None
    cr = contraction_ratio(closes, 10, 20)
    if cr is None or cr > PREEARN_CONTRACTION_MAX:
        return None
    contraction_strength = _clamp01(
        (PREEARN_CONTRACTION_MAX - cr) / PREEARN_CONTRACTION_MAX
    )
    drift = _clamp01(r1m / PARABOLIC_RETURN_1M)
    score = min(1.0, 0.5 + 0.3 * contraction_strength + 0.2 * drift)
    return SetupHit(
        "pre_earnings",
        round(score, 4),
        {"earnings_in_days": earnings_in_days, "contraction_ratio": round(cr, 3)},
    )


def score_presurge_setups(
    metrics,
    closes: list[float],
    volumes: list[float],
    earnings_in_days: Optional[int] = None,
    benchmark_return_1m: Optional[float] = None,
    benchmark_return_6m: Optional[float] = None,
) -> list[SetupHit]:
    """Score a single ticker against all four pre-surge setups (pure, no I/O).

    Args:
        metrics: The ticker's :class:`HorizonMetrics`.
        closes: Closing prices oldest-first (for the contraction detectors).
        volumes: Trading volumes oldest-first (for the volume-pattern check).
        earnings_in_days: Days until the next earnings report, or None
            (enables the US-only pre-earnings setup).
        benchmark_return_1m: The index proxy's trailing-month return (for RS).
        benchmark_return_6m: The index proxy's trailing-6-month return (for RS).

    Returns:
        A list of matched :class:`SetupHit` (possibly empty). A ticker can match
        more than one setup.
    """
    candidates = (
        _detect_base_pivot(metrics, closes, volumes),
        _detect_pullback(metrics, closes, volumes),
        _detect_rs_leader(metrics, benchmark_return_1m, benchmark_return_6m),
        _detect_pre_earnings(metrics, closes, earnings_in_days),
    )
    return [h for h in candidates if h is not None]


def best_setup(hits: list[SetupHit]) -> Optional[SetupHit]:
    """Pick the strongest setup, breaking score ties by a fixed priority.

    Priority (high→low): pullback > base_pivot > rs_leader > pre_earnings.

    Args:
        hits: Output of :func:`score_presurge_setups`.

    Returns:
        The winning :class:`SetupHit`, or None if ``hits`` is empty.
    """
    if not hits:
        return None
    return max(hits, key=lambda h: (h.score, _SETUP_PRIORITY.get(h.setup_type, 0)))


# ---------------------------------------------------------------------------
# Discovery pipeline (I/O)
# ---------------------------------------------------------------------------


def _default_provider(market: str):
    """Instantiate the default market-data provider for ``market``."""
    if market == "KR":
        from providers.kr import KoreanMarketProvider

        return KoreanMarketProvider()
    from providers.us import USMarketProvider

    return USMarketProvider()


def _enumerate_universe(
    market: str,
) -> list[tuple[str, Optional[float], Optional[float]]]:
    """Enumerate the screening universe (reuses the legacy funnel's sources)."""
    if market == "KR":
        return enumerate_kr_universe()
    return _load_static_us_universe()


def _benchmark_returns(
    provider, market: str, days: int
) -> tuple[Optional[float], Optional[float]]:
    """Trailing 1M / 6M returns of the index proxy, or (None, None) on failure."""
    proxy = BENCHMARK_PROXY.get(market)
    if not proxy:
        return (None, None)
    try:
        bars = provider.get_price_history(proxy, days=days)
        if not bars:
            return (None, None)
        metrics = compute_horizon_metrics(bars=bars, ticker=proxy, market=market)
        return (metrics.return_1m, metrics.return_6m)
    except Exception as exc:  # noqa: BLE001 — never block discovery on the benchmark
        logger.warning("presurge: benchmark fetch failed (%s); RS setup disabled", exc)
        return (None, None)


def fetch_earnings_days_map_us(
    tickers: list[str], horizon_days: int = 14
) -> dict[str, int]:
    """Best-effort map of US ticker -> calendar days until next earnings (FMP).

    Returns ``{}`` when ``FMP_API_KEY`` is unset or any error occurs — the
    pre-earnings setup simply stays inactive. Calendar days are a cheap proxy
    for the 5-10 trading-day band (good enough for a soft setup gate).

    Args:
        tickers: US tickers to look up.
        horizon_days: How far ahead to query the FMP earnings calendar.

    Returns:
        ``{ticker: days_until_earnings}`` for tickers with an upcoming report.
    """
    import os

    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        return {}
    try:
        from datetime import datetime, timedelta, timezone

        import requests

        today = datetime.now(timezone.utc).date()
        end = today + timedelta(days=horizon_days + 3)
        resp = requests.get(
            # Legacy /api/v3/earning_calendar 403s for newer keys; /stable works.
            "https://financialmodelingprep.com/stable/earnings-calendar",
            params={
                "from": today.isoformat(),
                "to": end.isoformat(),
                "apikey": api_key,
            },
            timeout=15,
        )
        resp.raise_for_status()
        wanted = {t.upper() for t in tickers}
        out: dict[str, int] = {}
        for row in resp.json() or []:
            sym = str(row.get("symbol", "")).upper()
            if sym not in wanted:
                continue
            try:
                edate = datetime.strptime(row["date"], "%Y-%m-%d").date()
            except (KeyError, ValueError, TypeError):
                continue
            days = (edate - today).days
            if days < 0:
                continue
            if sym not in out or days < out[sym]:
                out[sym] = days
        return out
    except Exception as exc:  # noqa: BLE001 — best-effort, never block discovery
        from events import _redact_key

        # requests error strings embed the full request URL (incl. apikey=…)
        # — redact before the message reaches the logs.
        logger.warning(
            "presurge: earnings fetch failed (%s); pre-earnings disabled",
            _redact_key(str(exc)),
        )
        return {}


def discover_presurge_candidates(
    market: str,
    provider=None,
    earnings_map: Optional[dict[str, int]] = None,
    with_earnings: bool = False,
    top_n_output: int = 20,
    min_score: float = MIN_SCORE_DEFAULT,
    days: int = 400,
) -> list[Candidate]:
    """End-to-end pre-surge discovery: enumerate → fetch → score → rank.

    Mirrors :func:`candidate_discovery.discover_kr_candidates` structure and its
    never-raise ethos: any provider failure collapses to an empty list so the
    caller's blended-funnel fallback still runs.

    Args:
        market: ``"US"`` or ``"KR"`` (case-insensitive).
        provider: Injectable market-data provider (defaults to the market's).
        earnings_map: Optional ``{ticker: days_until_earnings}`` for the US-only
            pre-earnings setup. When None and ``with_earnings`` is True (US), it
            is fetched best-effort via :func:`fetch_earnings_days_map_us`.
        with_earnings: Fetch the earnings map automatically (US only).
        top_n_output: Cap on returned candidates.
        min_score: Minimum best-setup score to survive.
        days: Calendar days of history per ticker (>=400 for MA200/RS/6M).

    Returns:
        Up to ``top_n_output`` tagged :class:`Candidate` objects
        (``discovery_source="presurge"``, ``setup_type`` set), sorted by
        descending setup score.
    """
    market = market.upper()
    provider = provider or _default_provider(market)
    universe = _enumerate_universe(market)
    if not universe:
        return []

    tickers = [t for (t, _, _) in universe]
    meta = {t: (cap, val) for (t, cap, val) in universe}

    try:
        bars_by_ticker = provider.get_price_history_batch(tickers, days=days)
    except Exception as exc:  # noqa: BLE001 — never block on the data provider
        logger.warning("presurge: batch price fetch failed (%s); no candidates", exc)
        return []

    if earnings_map is None and with_earnings and market == "US":
        earnings_map = fetch_earnings_days_map_us(tickers)
    earnings_map = earnings_map or {}

    bench_1m, bench_6m = _benchmark_returns(provider, market, days)

    scored: list[tuple[float, Candidate]] = []
    for ticker, bars in bars_by_ticker.items():
        # Need ~1 month of bars for return_1m + the 31-bar contraction window.
        if len(bars) < 22:
            continue
        display = ticker.upper() if market == "US" else ticker.zfill(6)
        try:
            metrics = compute_horizon_metrics(bars=bars, ticker=display, market=market)
            closes = [_close(b) for b in bars]
            volumes = [_volume(b) for b in bars]
        except Exception as exc:  # noqa: BLE001 — skip a bad ticker, keep going
            logger.debug("presurge: metrics failed for %s: %s", ticker, exc)
            continue

        hits = score_presurge_setups(
            metrics,
            closes,
            volumes,
            earnings_in_days=earnings_map.get(display, earnings_map.get(ticker)),
            benchmark_return_1m=bench_1m,
            benchmark_return_6m=bench_6m,
        )
        best = best_setup(hits)
        if best is None or best.score < min_score:
            continue

        cap, val = meta.get(ticker, (None, None))
        scored.append(
            (
                best.score,
                Candidate(
                    ticker=display,
                    name="",
                    market=market,
                    market_cap=cap,
                    trading_value=val,
                    return_5d_pct=(metrics.return_1w or 0.0) * 100.0,
                    vol_ratio_5d=(
                        metrics.vol_ratio if metrics.vol_ratio is not None else 1.0
                    ),
                    reason="presurge",
                    discovery_source="presurge",
                    setup_type=best.setup_type,
                ),
            )
        )

    scored.sort(key=lambda sc: sc[0], reverse=True)
    cands = [c for (_, c) in scored]
    cands = _fill_us_names(cands) if market == "US" else _fill_names(cands)
    return cands[:top_n_output]
