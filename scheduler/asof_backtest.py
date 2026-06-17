"""As-of A/B backtest: does the pre-surge cohort beat the momentum cohort?

This is the **ship gate** for the daily-briefing momentum-bias fix (WT-A.3). It
reconstructs both candidate streams at a series of historical as-of dates,
simulates a BULL long entry at each pick's as-of close under the SAME exit rules
the live ``outcome_tracker`` uses (MISS-before-HIT on close; default +3% target /
-5% stop; EXPIRED at the timeframe's trading-day count), forward-evaluates each
pick on its actual subsequent bars, and compares hit-rate / return / payoff per
``discovery_source`` and per trailing-20-day bucket.

Look-ahead is avoided structurally:
  * Discovery sees only bars dated on or before the as-of date (in-memory slice,
    since ``get_price_history`` has no ``end=`` argument — we over-fetch a long
    window once and slice per date).
  * A pick is evaluated only if its FULL forward horizon is available; otherwise
    it is skipped (censoring guard), so recent as-of dates don't bias results.

Caveats (reported, not hidden): the static universe CSVs are survivorship-biased
(currently-listed only) — the pre-surge-minus-momentum RELATIVE delta is far more
robust than absolute hit-rates, which is why the gate is a bootstrap CI on the
delta. yfinance closes are split/dividend-adjusted, so a split AFTER an as-of date
retroactively rewrites pre-as-of bars (a minor leak near split events).

Run:
    python scheduler/asof_backtest.py --market US --start 2025-09-01 \
        --end 2026-03-01 --step-days 14 --horizon 1M
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import ceil
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _p in (
    str(Path(__file__).resolve().parent),
    str(PROJECT_ROOT / "mcp-market-data"),
    str(PROJECT_ROOT / "mcp-prediction-store"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from asof_discovery import (  # noqa: E402
    AsofPick,
    discover_momentum_asof,
    discover_presurge_asof,
)
from indicators import _close  # noqa: E402
from models import TIMEFRAME_TRADING_DAYS, Timeframe  # noqa: E402
from pre_surge_discovery import (  # noqa: E402
    BENCHMARK_PROXY,
    _enumerate_universe,
    _default_provider,
)

# Exit thresholds — kept identical to scheduler.outcome_tracker so simulated
# outcomes match how the live tracker would have scored these entries.
DEFAULT_HIT_PCT = 0.03
DEFAULT_MISS_PCT = 0.05

# Trailing-20d buckets matching the investigation's cohort analysis.
_BUCKETS = (
    ("<0%", None, 0.0),
    ("0-10%", 0.0, 0.10),
    ("10-20%", 0.10, 0.20),
    ("20-40%", 0.20, 0.40),
    (">40%", 0.40, None),
)


@dataclass
class SimResult:
    """One simulated, fully forward-evaluated pick."""

    ticker: str
    discovery_source: str
    setup_type: Optional[str]
    as_of: str
    entry: float
    status: str  # HIT | MISS | EXPIRED
    ret: float  # realised position return, decimal
    trailing_20d: Optional[float]


def _bar_date(bar) -> str:
    """Bar date as an ISO string, from a dict or OHLCV-like object."""
    return str(bar["date"] if isinstance(bar, dict) else bar.date)


def slice_bars_asof(bars: list, as_of: str) -> list:
    """Return only the bars dated on or before ``as_of`` (oldest-first input)."""
    return [b for b in bars if _bar_date(b) <= as_of]


def simulate_pick(
    pick: AsofPick, future_closes: list[float], required_days: int
) -> Optional[tuple[str, float]]:
    """Forward-evaluate a BULL long entry under outcome_tracker's rules.

    Args:
        pick: The candidate (entry = its as-of close).
        future_closes: Closes strictly AFTER the as-of date, in chronological order.
        required_days: Trading days in the horizon (EXPIRED boundary).

    Returns:
        ``(status, return)`` where status is HIT/MISS/EXPIRED and return is the
        realised decimal position return, or None when fewer than
        ``required_days`` forward bars exist (censoring guard — skip the pick).
    """
    entry = pick.entry_close
    if entry <= 0 or len(future_closes) < required_days:
        return None
    target = entry * (1 + DEFAULT_HIT_PCT)
    stop = entry * (1 - DEFAULT_MISS_PCT)
    for c in future_closes[:required_days]:
        if c <= stop:  # MISS checked before HIT (matches outcome_tracker)
            return "MISS", (c - entry) / entry
        if c >= target:
            return "HIT", (c - entry) / entry
    last = future_closes[required_days - 1]
    return "EXPIRED", (last - entry) / entry


def _gen_as_of_dates(start: str, end: str, step_days: int) -> list[str]:
    """Weekday as-of dates from start to end inclusive, every ``step_days``."""
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    out: list[str] = []
    cur = d0
    while cur <= d1:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=step_days)
    return out


def run_backtest(
    market: str,
    as_of_dates: list[str],
    horizon: str,
    provider=None,
    fetch_days: int = 700,
    top_n: int = 20,
    min_score: float = 0.5,
) -> tuple[list[SimResult], int]:
    """Run both discovery streams across all as-of dates and forward-evaluate.

    Returns:
        ``(results, skipped)`` — completed SimResults and the count of picks
        dropped for insufficient forward data.
    """
    market = market.upper()
    provider = provider or _default_provider(market)
    required_days = TIMEFRAME_TRADING_DAYS[Timeframe(horizon)]

    universe = _enumerate_universe(market)
    tickers = [t for (t, _, _) in universe]
    bars_by_ticker = provider.get_price_history_batch(tickers, days=fetch_days)
    proxy = BENCHMARK_PROXY.get(market)
    benchmark_full = provider.get_price_history(proxy, days=fetch_days) if proxy else []

    # Precompute (date, close) for fast forward lookups per ticker.
    closes_dated = {
        t: [(_bar_date(b), _close(b)) for b in bars]
        for t, bars in bars_by_ticker.items()
    }

    results: list[SimResult] = []
    skipped = 0
    for as_of in as_of_dates:
        sliced = {t: slice_bars_asof(b, as_of) for t, b in bars_by_ticker.items()}
        sliced = {t: b for t, b in sliced.items() if b}
        bench_sliced = slice_bars_asof(benchmark_full, as_of) if benchmark_full else []

        picks = discover_momentum_asof(sliced, top_n=top_n) + discover_presurge_asof(
            sliced,
            benchmark_bars=bench_sliced,
            market=market,
            top_n=top_n,
            min_score=min_score,
        )
        for pick in picks:
            future = [c for (d, c) in closes_dated.get(pick.ticker, []) if d > as_of]
            sim = simulate_pick(pick, future, required_days)
            if sim is None:
                skipped += 1
                continue
            status, ret = sim
            results.append(
                SimResult(
                    ticker=pick.ticker,
                    discovery_source=pick.discovery_source,
                    setup_type=pick.setup_type,
                    as_of=as_of,
                    entry=pick.entry_close,
                    status=status,
                    ret=ret,
                    trailing_20d=pick.trailing_20d_return,
                )
            )
    return results, skipped


# ---------------------------------------------------------------------------
# Aggregation + ship gate
# ---------------------------------------------------------------------------


def _cohort_stats(rows: list[SimResult]) -> dict:
    """Hit-rate (HIT/(HIT+MISS)), return and payoff stats for a cohort."""
    n = len(rows)
    hits = sum(1 for r in rows if r.status == "HIT")
    misses = sum(1 for r in rows if r.status == "MISS")
    expired = sum(1 for r in rows if r.status == "EXPIRED")
    resolved = hits + misses
    wins = [r.ret for r in rows if r.status == "HIT"]
    losses = [r.ret for r in rows if r.status == "MISS"]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    return {
        "n": n,
        "hit": hits,
        "miss": misses,
        "expired": expired,
        "hit_rate": (hits / resolved) if resolved else None,
        # hit_rate_all counts EXPIRED as a non-hit (dead money). Reported so a
        # cohort that rarely resolves but wins-when-it-does (high HIT/(HIT+MISS)
        # but capital-inefficient) is visible; this is the ship-gate basis.
        "hit_rate_all": (hits / n) if n else None,
        "avg_return": (sum(r.ret for r in rows) / n) if n else None,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff_ratio": (avg_win / abs(avg_loss)) if avg_loss else None,
    }


def _bucket_of(trailing: Optional[float]) -> Optional[str]:
    """Map a trailing-20d return to its investigation bucket label."""
    if trailing is None:
        return None
    for label, lo, hi in _BUCKETS:
        if (lo is None or trailing >= lo) and (hi is None or trailing < hi):
            return label
    return None


def _bucket_table(rows: list[SimResult]) -> dict[str, dict]:
    """Per-trailing-bucket hit-rate for one cohort."""
    out: dict[str, dict] = {}
    for label, _, _ in _BUCKETS:
        sub = [r for r in rows if _bucket_of(r.trailing_20d) == label]
        if sub:
            out[label] = _cohort_stats(sub)
    return out


def _bootstrap_delta_ci(
    pre_outcomes: list[int],
    mom_outcomes: list[int],
    n_resamples: int = 2000,
    seed: int = 12345,
    alpha: float = 0.05,
) -> Optional[dict]:
    """Bootstrap CI for (presurge hit-rate − momentum hit-rate).

    Resamples each cohort's 1/0 (HIT/MISS) outcomes with replacement and takes a
    percentile interval on the per-resample delta. The ship gate passes when the
    lower bound is strictly above 0 (pre-surge beats momentum, not noise).
    """
    if not pre_outcomes or not mom_outcomes:
        return None
    rng = random.Random(seed)
    np_, nm = len(pre_outcomes), len(mom_outcomes)
    deltas: list[float] = []
    for _ in range(n_resamples):
        wp = sum(pre_outcomes[rng.randrange(np_)] for _ in range(np_)) / np_
        wm = sum(mom_outcomes[rng.randrange(nm)] for _ in range(nm)) / nm
        deltas.append(wp - wm)
    deltas.sort()
    lo = deltas[max(0, ceil((alpha / 2) * n_resamples) - 1)]
    hi = deltas[min(n_resamples - 1, ceil((1 - alpha / 2) * n_resamples) - 1)]
    point = (sum(pre_outcomes) / np_) - (sum(mom_outcomes) / nm)
    return {"delta": point, "ci_low": lo, "ci_high": hi, "pass": lo > 0}


def _bootstrap_delta_ci_blocked(
    by_date: dict[str, dict],
    n_resamples: int = 2000,
    seed: int = 12345,
    alpha: float = 0.05,
) -> Optional[dict]:
    """Block bootstrap CI for the cohort hit-rate delta, resampling by as-of date.

    Picks from the same as-of date share a market regime and forward window, so
    they are not independent. Resampling individual picks (the naive bootstrap)
    underestimates the variance and can false-pass the gate. This resamples
    whole as-of dates with replacement (a moving-block bootstrap over the date
    clusters), which is the correct unit of independence (codex review).

    Args:
        by_date: ``{as_of: {"pre": [1/0...], "mom": [1/0...]}}`` outcomes
            (EXPIRED already encoded as 0).
        n_resamples / seed / alpha: standard bootstrap controls.

    Returns:
        ``{delta, ci_low, ci_high, pass, method}`` or None if either cohort is empty.
    """
    dates = list(by_date.keys())
    all_pre = [o for d in dates for o in by_date[d]["pre"]]
    all_mom = [o for d in dates for o in by_date[d]["mom"]]
    if not dates or not all_pre or not all_mom:
        return None
    rng = random.Random(seed)
    nd = len(dates)
    deltas: list[float] = []
    for _ in range(n_resamples):
        sampled = [dates[rng.randrange(nd)] for _ in range(nd)]
        pre = [o for d in sampled for o in by_date[d]["pre"]]
        mom = [o for d in sampled for o in by_date[d]["mom"]]
        if not pre or not mom:
            continue
        deltas.append(sum(pre) / len(pre) - sum(mom) / len(mom))
    if not deltas:
        return None
    deltas.sort()
    m = len(deltas)
    lo = deltas[max(0, ceil((alpha / 2) * m) - 1)]
    hi = deltas[min(m - 1, ceil((1 - alpha / 2) * m) - 1)]
    point = (sum(all_pre) / len(all_pre)) - (sum(all_mom) / len(all_mom))
    return {
        "delta": point,
        "ci_low": lo,
        "ci_high": hi,
        "pass": lo > 0,
        "method": "block-bootstrap-by-asof",
    }


def build_report(results: list[SimResult], skipped: int) -> dict:
    """Assemble the full cohort comparison + ship-gate verdict."""
    pre = [r for r in results if r.discovery_source == "presurge"]
    mom = [r for r in results if r.discovery_source == "momentum"]
    # Ship gate counts EXPIRED as a non-hit (0) alongside MISS, over every
    # forward-evaluated pick — so a cohort that expires (dead money) more often
    # cannot look better via a HIT/(HIT+MISS)-only denominator (gemini review) —
    # and resamples by as-of DATE (block bootstrap), since same-date picks share
    # a regime/forward window and are not independent (codex review).
    by_date: dict[str, dict] = {}
    for r in results:
        if r.status not in ("HIT", "MISS", "EXPIRED"):
            continue
        slot = by_date.setdefault(r.as_of, {"pre": [], "mom": []})
        key = "pre" if r.discovery_source == "presurge" else "mom"
        slot[key].append(1 if r.status == "HIT" else 0)
    return {
        "total_simulated": len(results),
        "skipped_insufficient_forward": skipped,
        "presurge": _cohort_stats(pre),
        "momentum": _cohort_stats(mom),
        "presurge_by_bucket": _bucket_table(pre),
        "momentum_by_bucket": _bucket_table(mom),
        "ship_gate": _bootstrap_delta_ci_blocked(by_date),
    }


def _fmt_pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def _fmt_rate(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def format_report(report: dict, market: str, horizon: str) -> str:
    """Human-readable summary of the cohort comparison + gate verdict."""
    lines = [
        f"=== As-of backtest — {market} {horizon} ===",
        f"simulated={report['total_simulated']}  "
        f"skipped(insufficient forward)={report['skipped_insufficient_forward']}",
        "",
        f"{'cohort':<10}{'n':>5}{'hit_rate':>10}{'hit_all':>9}{'expired':>9}"
        f"{'avg_ret':>10}{'payoff':>9}",
    ]
    for name in ("presurge", "momentum"):
        s = report[name]
        payoff = "n/a" if s["payoff_ratio"] is None else f"{s['payoff_ratio']:.2f}"
        lines.append(
            f"{name:<10}{s['n']:>5}{_fmt_rate(s['hit_rate']):>10}"
            f"{_fmt_rate(s['hit_rate_all']):>9}{s['expired']:>9}"
            f"{_fmt_pct(s['avg_return']):>10}{payoff:>9}"
        )
    lines.append(
        "(hit_rate=HIT/(HIT+MISS); hit_all & ship gate count EXPIRED as non-hit)"
    )
    lines.append("")
    lines.append("trailing-20d bucket hit-rate (presurge | momentum):")
    for label, _, _ in _BUCKETS:
        p = report["presurge_by_bucket"].get(label)
        m = report["momentum_by_bucket"].get(label)
        p_s = f"{_fmt_rate(p['hit_rate'])} (n={p['n']})" if p else "—"
        m_s = f"{_fmt_rate(m['hit_rate'])} (n={m['n']})" if m else "—"
        lines.append(f"  {label:<8} {p_s:<18} | {m_s}")
    lines.append("")
    gate = report["ship_gate"]
    if gate is None:
        lines.append("SHIP GATE: n/a (insufficient resolved outcomes in one cohort)")
    else:
        verdict = "PASS ✅" if gate["pass"] else "FAIL ❌"
        lines.append(
            f"SHIP GATE: {verdict}  delta(presurge−momentum hit-rate)="
            f"{gate['delta'] * 100:+.1f}pp  95% CI=[{gate['ci_low'] * 100:+.1f}, "
            f"{gate['ci_high'] * 100:+.1f}]pp (pass requires CI low > 0)"
        )
    return "\n".join(lines)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="As-of A/B backtest: pre-surge vs momentum")
    p.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    p.add_argument("--start", required=True, help="First as-of date YYYY-MM-DD")
    p.add_argument("--end", required=True, help="Last as-of date YYYY-MM-DD")
    p.add_argument("--step-days", type=int, default=14, help="Days between as-of dates")
    p.add_argument(
        "--horizon",
        default="1M",
        choices=["1W", "2W", "1M", "3M", "6M", "1Y"],
        help="Holding horizon (sets the EXPIRED boundary)",
    )
    p.add_argument("--top-n", type=int, default=20)
    p.add_argument("--min-score", type=float, default=0.5)
    p.add_argument(
        "--fetch-days",
        type=int,
        default=700,
        help="Calendar days of history to over-fetch per ticker (default 700)",
    )
    p.add_argument(
        "--json", action="store_true", help="Emit raw JSON instead of a table"
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    as_of_dates = _gen_as_of_dates(args.start, args.end, args.step_days)
    if not as_of_dates:
        print("No as-of dates in range", file=sys.stderr)
        return 1
    results, skipped = run_backtest(
        market=args.market,
        as_of_dates=as_of_dates,
        horizon=args.horizon,
        fetch_days=args.fetch_days,
        top_n=args.top_n,
        min_score=args.min_score,
    )
    report = build_report(results, skipped)
    if args.json:
        import json

        print(json.dumps(report, indent=2))
    else:
        print(format_report(report, args.market.upper(), args.horizon))
    gate = report["ship_gate"]
    return 0 if (gate and gate["pass"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
