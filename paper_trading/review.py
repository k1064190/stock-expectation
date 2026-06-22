"""Pure metrics for the weekly paper-trading review.

Kept free of I/O so the math (drawdown, Sharpe, round-trip pairing, attribution)
is unit-tested; ``scheduler/paper_trading_review.py`` reads the DBs and renders
the markdown report on top of these.
"""

from __future__ import annotations

import math
import statistics
from typing import Optional

TRADING_DAYS_PER_YEAR = 252

# Confidence buckets for attribution (half-open [lo, hi)).
CONFIDENCE_BUCKETS = ((0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.01))


def book_metrics(nav_rows: list) -> dict:
    """Summary metrics over a NAV series (rows ordered by date).

    Returns cumulative_return, max_drawdown (most negative peak-to-trough),
    annualized Sharpe (None if undefined), final_nav, days, and benchmark_return
    (None unless benchmark_nav is populated on the first and last rows).
    """
    if not nav_rows:
        return {
            "days": 0,
            "final_nav": None,
            "cumulative_return": None,
            "max_drawdown": None,
            "sharpe": None,
            "benchmark_return": None,
        }
    navs = [r.nav for r in nav_rows]
    cumulative_return = navs[-1] / navs[0] - 1 if navs[0] else None

    peak = navs[0]
    max_dd = 0.0
    for v in navs:
        peak = max(peak, v)
        if peak > 0:
            max_dd = min(max_dd, v / peak - 1)

    daily_rets = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs)) if navs[i - 1]]
    sharpe = None
    if len(daily_rets) > 1:
        sd = statistics.stdev(daily_rets)  # sample stdev (returns are a sample)
        if sd > 0:
            sharpe = (
                statistics.fmean(daily_rets) / sd * math.sqrt(TRADING_DAYS_PER_YEAR)
            )

    benchmark_return = None
    b0 = nav_rows[0].benchmark_nav
    b1 = nav_rows[-1].benchmark_nav
    if b0 and b1:
        benchmark_return = b1 / b0 - 1

    return {
        "days": len(nav_rows),
        "final_nav": navs[-1],
        "cumulative_return": cumulative_return,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "benchmark_return": benchmark_return,
    }


def realized_trades(trades: list) -> list[dict]:
    """Pair entry BUYs with their exit SELLs by prediction_id → realized round-trips.

    Each lot is opened by one prediction and closed once, and both legs carry the
    same prediction_id, so pairing is unambiguous. Open lots (no SELL) are excluded.
    """
    buys: dict[str, object] = {}
    sells: dict[str, object] = {}
    for t in trades:
        if t.prediction_id is None:
            continue
        (buys if t.side == "BUY" else sells)[t.prediction_id] = t

    realized = []
    for pid, buy in buys.items():
        sell = sells.get(pid)
        if sell is None:
            continue  # still open
        buy_cost = -buy.net_cash_delta  # cash out is negative on the BUY leg
        sell_proceeds = sell.net_cash_delta
        pnl = sell_proceeds - buy_cost
        realized.append(
            {
                "prediction_id": pid,
                "ticker": buy.ticker,
                "buy_cost": buy_cost,
                "pnl": pnl,
                "ret": (pnl / buy_cost) if buy_cost else 0.0,
                "exit_reason": sell.reason,
            }
        )
    return realized


def win_stats(realized: list[dict]) -> dict:
    """Win rate, average return, total P&L — overall and grouped by exit reason."""
    n = len(realized)
    if n == 0:
        return {
            "n": 0,
            "win_rate": None,
            "avg_ret": None,
            "total_pnl": 0.0,
            "by_reason": {},
        }
    wins = sum(1 for r in realized if r["pnl"] > 0)
    by_reason: dict[str, dict] = {}
    for r in realized:
        b = by_reason.setdefault(
            r["exit_reason"], {"n": 0, "wins": 0, "total_pnl": 0.0}
        )
        b["n"] += 1
        b["wins"] += 1 if r["pnl"] > 0 else 0
        b["total_pnl"] += r["pnl"]
    for b in by_reason.values():
        b["win_rate"] = b["wins"] / b["n"]
    return {
        "n": n,
        "win_rate": wins / n,
        "avg_ret": statistics.fmean(r["ret"] for r in realized),
        "total_pnl": sum(r["pnl"] for r in realized),
        "by_reason": by_reason,
    }


def attribute_by_confidence(realized: list[dict], conf_by_pid: dict) -> dict:
    """Group realized round-trips into confidence buckets for attribution."""
    out: dict[str, dict] = {}
    for lo, hi in CONFIDENCE_BUCKETS:
        label = f"{lo:.2f}-{hi if hi <= 1 else 1.0:.2f}"
        members = [
            r
            for r in realized
            if (c := conf_by_pid.get(r["prediction_id"])) is not None and lo <= c < hi
        ]
        if not members:
            continue
        wins = sum(1 for r in members if r["pnl"] > 0)
        out[label] = {
            "n": len(members),
            "win_rate": wins / len(members),
            "avg_ret": statistics.fmean(r["ret"] for r in members),
            "total_pnl": sum(r["pnl"] for r in members),
        }
    return out
