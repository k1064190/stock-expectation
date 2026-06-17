"""Point-in-time (as-of) discovery functions for the backtest harness.

These mirror the two production candidate streams but operate on *already-sliced*
bars (bars dated on or before an as-of date) and perform **no I/O and no
``datetime.now``** so the harness can replay them deterministically at any
historical date without look-ahead:

  * :func:`discover_momentum_asof` — replicates the legacy
    ``candidate_discovery`` filter (``|5d return| >= 15%`` OR ``vol_ratio >= 2``)
    but, since the briefing only ever logs BULL predictions, the BULL cohort
    keeps up-surges (and volume spikes), sorted by descending 5-day return.
  * :func:`discover_presurge_asof` — replays
    ``pre_surge_discovery.score_presurge_setups`` on the sliced bars.

Both return :class:`AsofPick` rows tagged with ``discovery_source`` and the
trailing-20-day return at entry (for bucketed comparison).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(Path(__file__).resolve().parent), str(PROJECT_ROOT / "mcp-market-data")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from candidate_discovery import _pct_return, _vol_ratio  # noqa: E402
from indicators import (  # noqa: E402
    _close,
    _return_over,
    _volume,
    compute_horizon_metrics,
)
from pre_surge_discovery import best_setup, score_presurge_setups  # noqa: E402


@dataclass
class AsofPick:
    """One simulated entry candidate at an as-of date.

    Args:
        ticker: Universe key (e.g. ``"AAPL"`` or ``"005930"``).
        discovery_source: ``"momentum"`` or ``"presurge"``.
        setup_type: Pre-surge setup name, or ``"momentum"`` for the legacy stream.
        entry_close: Close on the as-of date (the simulated long entry price).
        trailing_20d_return: ``return_1m`` (21-bar) at entry, decimal, or None.
        score: Ranking score — ``|5d return %|`` for momentum, the setup score
            (0-1) for pre-surge.
    """

    ticker: str
    discovery_source: str
    setup_type: Optional[str]
    entry_close: float
    trailing_20d_return: Optional[float]
    score: float


def discover_momentum_asof(
    bars_by_ticker: dict[str, list],
    return_threshold_pct: float = 15.0,
    vol_ratio_threshold: float = 2.0,
    top_n: int = 20,
) -> list[AsofPick]:
    """Replicate the legacy momentum funnel as a BULL cohort at the as-of date.

    Args:
        bars_by_ticker: ``{ticker: bars}`` already sliced to bars <= as-of.
        return_threshold_pct: 5-day return threshold (default 15%).
        vol_ratio_threshold: 5-vs-prior-20 volume-ratio threshold (default 2.0).
        top_n: Cap on returned picks.

    Returns:
        Up to ``top_n`` :class:`AsofPick` sorted by descending 5-day return.
        The BULL cohort keeps positive 5-day surges OR volume spikes (the names
        that become BUY candidates), mirroring what the briefing actually logs.
    """
    picks: list[AsofPick] = []
    for ticker, bars in bars_by_ticker.items():
        if len(bars) < 6:
            continue
        closes = [_close(b) for b in bars]
        volumes = [_volume(b) for b in bars]
        ret5 = _pct_return(closes[-6], closes[-1])
        vr = _vol_ratio(volumes)
        # BULL cohort: an up-surge OR a volume spike (down-crashes are not BUYs).
        if not (ret5 >= return_threshold_pct or vr >= vol_ratio_threshold):
            continue
        picks.append(
            AsofPick(
                ticker=ticker,
                discovery_source="momentum",
                setup_type="momentum",
                entry_close=closes[-1],
                trailing_20d_return=_return_over(closes, 21),
                score=ret5,
            )
        )
    picks.sort(key=lambda p: p.score, reverse=True)
    return picks[:top_n]


def discover_presurge_asof(
    bars_by_ticker: dict[str, list],
    benchmark_bars: Optional[list] = None,
    market: str = "US",
    top_n: int = 20,
    min_score: float = 0.5,
) -> list[AsofPick]:
    """Replay the pre-surge scorer on sliced bars at the as-of date.

    Args:
        bars_by_ticker: ``{ticker: bars}`` already sliced to bars <= as-of.
        benchmark_bars: Index-proxy bars (sliced to <= as-of) for the RS setup,
            or None to disable the relative-strength detector.
        market: ``"US"`` or ``"KR"`` (labelling only).
        top_n: Cap on returned picks.
        min_score: Minimum best-setup score to survive.

    Returns:
        Up to ``top_n`` :class:`AsofPick` sorted by descending setup score.
    """
    bench_1m: Optional[float] = None
    bench_6m: Optional[float] = None
    if benchmark_bars and len(benchmark_bars) >= 22:
        bm = compute_horizon_metrics(bars=benchmark_bars, ticker="BENCH", market=market)
        bench_1m, bench_6m = bm.return_1m, bm.return_6m

    picks: list[AsofPick] = []
    for ticker, bars in bars_by_ticker.items():
        if len(bars) < 22:
            continue
        metrics = compute_horizon_metrics(bars=bars, ticker=ticker, market=market)
        closes = [_close(b) for b in bars]
        volumes = [_volume(b) for b in bars]
        hits = score_presurge_setups(
            metrics,
            closes,
            volumes,
            benchmark_return_1m=bench_1m,
            benchmark_return_6m=bench_6m,
        )
        best = best_setup(hits)
        if best is None or best.score < min_score:
            continue
        picks.append(
            AsofPick(
                ticker=ticker,
                discovery_source="presurge",
                setup_type=best.setup_type,
                entry_close=closes[-1],
                trailing_20d_return=metrics.return_1m,
                score=best.score,
            )
        )
    picks.sort(key=lambda p: p.score, reverse=True)
    return picks[:top_n]
