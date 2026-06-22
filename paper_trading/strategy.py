"""Pure decision logic for the paper-trading engine.

No I/O: callers (the engine) resolve prices and persistence; these functions
just turn the day's prediction candidates and the held book into entry/exit
orders. Long-only v1.

Sizing is risk-based, mirroring the position-sizer skill: each new lot risks a
fixed fraction of NAV between the entry and its stop, capped by a max position
size and by available cash.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class StrategyParams:
    """Tunable knobs for the paper-trading strategy (defaults adopted 2026-06-23)."""

    risk_per_trade_pct: float = 0.01  # fraction of NAV risked per new lot
    max_position_pct: float = 0.20  # max fraction of NAV in one lot
    max_new_positions_per_day: int = 5
    fallback_stop_pct: float = 0.08  # stop = entry*(1-this) when prediction has no stop
    confidence_floor: float = 0.60  # ignore predictions below this confidence
    cost_buffer_rate: float = 0.01  # headroom left in cash sizing for fees/slippage


@dataclass
class Candidate:
    """A potential long entry derived from a BULL prediction for the day."""

    ticker: str
    ref_price: float  # fill reference (e.g. next-session open)
    confidence: float
    prediction_id: Optional[str] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    horizon_end_date: Optional[str] = None


@dataclass
class EntryOrder:
    ticker: str
    qty: int
    ref_price: float
    stop_price: float
    target_price: Optional[float]
    prediction_id: Optional[str]
    horizon_end_date: Optional[str]


@dataclass
class Position_:
    """The fields of an open lot the exit logic needs (subset of models.Position)."""

    id: str
    ticker: str
    qty: float
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    horizon_end_date: Optional[str] = None
    prediction_id: Optional[str] = None


@dataclass
class PriceBar:
    """A day's price range plus the fill reference for exits."""

    high: float
    low: float
    ref_price: float  # used for horizon exits (e.g. session open/close)


@dataclass
class ExitOrder:
    position_id: str
    ticker: str
    qty: float
    fill_price: float
    reason: str  # 'target_hit' | 'stop_hit' | 'horizon_exit'
    prediction_id: Optional[str] = None


def decide_entries(
    *,
    nav: float,
    cash: float,
    held_tickers: set,
    candidates: list[Candidate],
    params: StrategyParams = StrategyParams(),
) -> list[EntryOrder]:
    """Turn the day's candidates into sized long entry orders.

    Args:
        nav: Current account net asset value (cash + positions), the sizing base.
        cash: Available cash; entries are sized so their notional fits with a
            small cost buffer, and cash is drawn down as orders are placed.
        held_tickers: Tickers already held — skipped to avoid stacking.
        candidates: Candidate longs for the day.
        params: Strategy knobs.

    Returns:
        Up to ``max_new_positions_per_day`` EntryOrders, highest-confidence first.
        Orders that cannot afford a whole share are dropped.
    """
    eligible = [
        c
        for c in candidates
        if c.confidence >= params.confidence_floor
        and c.ticker not in held_tickers
        and c.ref_price > 0
    ]
    eligible.sort(key=lambda c: c.confidence, reverse=True)

    orders: list[EntryOrder] = []
    ordered_tickers: set = set()  # at most one new lot per ticker per day
    remaining_cash = cash
    risk_budget = nav * params.risk_per_trade_pct
    position_cap_value = nav * params.max_position_pct

    for c in eligible:
        if len(orders) >= params.max_new_positions_per_day:
            break
        if c.ticker in ordered_tickers:
            continue
        # Resolve the stop (use the prediction's only if it's below entry).
        if c.stop_price is not None and 0 < c.stop_price < c.ref_price:
            stop = c.stop_price
        else:
            stop = c.ref_price * (1 - params.fallback_stop_pct)
        risk_per_share = c.ref_price - stop
        if risk_per_share <= 0:
            continue

        qty_by_risk = risk_budget / risk_per_share
        qty_by_position_cap = position_cap_value / c.ref_price
        qty_by_cash = remaining_cash / (c.ref_price * (1 + params.cost_buffer_rate))
        qty = int(math.floor(min(qty_by_risk, qty_by_position_cap, qty_by_cash)))
        if qty < 1:
            continue

        orders.append(
            EntryOrder(
                ticker=c.ticker,
                qty=qty,
                ref_price=c.ref_price,
                stop_price=stop,
                target_price=c.target_price,
                prediction_id=c.prediction_id,
                horizon_end_date=c.horizon_end_date,
            )
        )
        ordered_tickers.add(c.ticker)
        remaining_cash -= qty * c.ref_price * (1 + params.cost_buffer_rate)

    return orders


def decide_exits(
    positions: list[Position_],
    market_data: dict,
    today: str,
) -> list[ExitOrder]:
    """Decide which open lots to close today.

    For each lot with a price bar: a stop touch takes priority (conservative —
    when the day's range spans both stop and target we assume the adverse fill),
    then a target touch, then horizon expiry. A lot with no price bar (data gap)
    is carried, not exited.

    Args:
        positions: Open lots.
        market_data: ticker -> PriceBar for the day.
        today: ISO date 'YYYY-MM-DD' to compare against horizon_end_date.

    Returns:
        ExitOrders for the lots to close.
    """
    exits: list[ExitOrder] = []
    for pos in positions:
        bar = market_data.get(pos.ticker)
        if bar is None:
            continue  # data gap → carry the position
        reason: Optional[str] = None
        fill_price: Optional[float] = None
        if pos.stop_price is not None and bar.low <= pos.stop_price:
            reason, fill_price = "stop_hit", pos.stop_price
        elif pos.target_price is not None and bar.high >= pos.target_price:
            reason, fill_price = "target_hit", pos.target_price
        elif pos.horizon_end_date is not None and today >= pos.horizon_end_date:
            reason, fill_price = "horizon_exit", bar.ref_price
        if reason is None:
            continue
        exits.append(
            ExitOrder(
                position_id=pos.id,
                ticker=pos.ticker,
                qty=pos.qty,
                fill_price=fill_price,
                reason=reason,
                prediction_id=pos.prediction_id,
            )
        )
    return exits
