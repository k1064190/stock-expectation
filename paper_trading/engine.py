"""One daily paper-trading cycle for a single account.

``run_day`` is the orchestrator: it closes any lots whose target/stop/horizon
triggered, opens new lots from the day's candidates, marks the book to market at
the close, and records a NAV snapshot. The whole cycle commits as ONE transaction
(rolled back on any error), so a crash never leaves a partially-traded day; it is
idempotent per (account, date): a second call for a date that already has a NAV
row is a no-op.

Fill model: entries fill at the signal day's close (a once-daily EOD decision —
no look-ahead into future sessions); target/stop exits fill at the trigger
level; horizon exits fill at the close. Slippage and commission/tax are applied
by ``costs``.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from . import costs, models
from .models import Account, NavSnapshot, Position, Trade
from .strategy import (
    Candidate,
    Position_,
    PriceBar,
    StrategyParams,
    decide_entries,
    decide_exits,
)


def _value_positions(
    positions: list[Position], prices: dict, marks: Optional[dict] = None
) -> float:
    """Mark open lots to market.

    Per lot, prefer the day's fresh close; on a data gap fall back to the most
    recent known close (``marks``) so a missing bar doesn't fabricate NAV
    movement; only if neither exists fall back to ``avg_cost`` (entry day).
    """
    marks = marks or {}
    total = 0.0
    for p in positions:
        bar = prices.get(p.ticker)
        if bar is not None:
            mark = bar["close"]
        elif p.ticker in marks:
            mark = marks[p.ticker]
        else:
            mark = p.avg_cost
        total += p.qty * mark
    return total


def _to_position_(p: Position) -> Position_:
    return Position_(
        id=p.id,
        ticker=p.ticker,
        qty=p.qty,
        target_price=p.target_price,
        stop_price=p.stop_price,
        horizon_end_date=p.horizon_end_date,
        prediction_id=p.prediction_id,
    )


def run_day(
    conn: sqlite3.Connection,
    account: Account,
    date: str,
    prices: dict,
    candidates: list[Candidate],
    params: StrategyParams = StrategyParams(),
    benchmark_nav: Optional[float] = None,
    marks: Optional[dict] = None,
) -> NavSnapshot:
    """Run exits → entries → mark-to-market for ``account`` on ``date``.

    Args:
        conn: paper_trading DB connection.
        account: The book to trade.
        date: ISO 'YYYY-MM-DD' trading date being processed.
        prices: ticker -> {"open","high","low","close"} for the date. Only
            tickers with a fresh bar here can fill or trigger an exit; a missing
            ticker is a data gap (no fill, position carried).
        candidates: Candidate longs for the date (ref_price is resolved here from
            the close).
        params: Strategy knobs.
        benchmark_nav: Optional passive-benchmark NAV to log alongside.
        marks: Optional ticker -> last-known-close map used only to mark held
            lots that lack a fresh bar today (carry-forward), avoiding a NAV jump
            on a data gap.

    Returns:
        The recorded NavSnapshot for the date.
    """
    history = models.get_nav_history(conn, account.id)
    existing = next((n for n in history if n.date == date), None)
    if existing is not None:
        return existing  # idempotent: already processed this date

    try:
        snap = _execute_day(
            conn,
            account,
            date,
            prices,
            candidates,
            params,
            benchmark_nav,
            marks,
            history,
        )
        conn.commit()
        return snap
    except Exception:
        conn.rollback()
        raise


def _execute_day(
    conn: sqlite3.Connection,
    account: Account,
    date: str,
    prices: dict,
    candidates: list[Candidate],
    params: StrategyParams,
    benchmark_nav: Optional[float],
    marks: Optional[dict],
    history: list,
) -> NavSnapshot:
    """The (uncommitted) body of one daily cycle; ``run_day`` wraps it in a tx."""
    market = account.market
    cash = account.cash

    # --- exits ------------------------------------------------------------- #
    open_positions = models.get_open_positions(conn, account.id)
    exit_bars = {
        p.ticker: PriceBar(
            high=prices[p.ticker]["high"],
            low=prices[p.ticker]["low"],
            ref_price=prices[p.ticker]["close"],
        )
        for p in open_positions
        if p.ticker in prices
    }
    for order in decide_exits(
        [_to_position_(p) for p in open_positions], exit_bars, date
    ):
        sell = costs.simulate_sell(market, order.qty, order.fill_price)
        models.insert_trade(
            conn,
            Trade(
                account_id=account.id,
                ticker=order.ticker,
                side="SELL",
                qty=order.qty,
                price=sell["fill_price"],
                gross=sell["gross"],
                fees=sell["commission"],
                tax=sell["tax"],
                slippage=sell["slippage"],
                net_cash_delta=sell["cash_in"],
                executed_at=date,
                reason=order.reason,
                prediction_id=order.prediction_id,
            ),
        )
        models.close_position(conn, order.position_id, closed_at=date)
        cash += sell["cash_in"]

    # --- entries ----------------------------------------------------------- #
    open_after_exits = models.get_open_positions(conn, account.id)
    held = {p.ticker for p in open_after_exits}
    nav_for_sizing = cash + _value_positions(open_after_exits, prices, marks)

    resolved: list[Candidate] = []
    for c in candidates:
        bar = prices.get(c.ticker)
        if bar is None:
            continue  # cannot fill without a price
        resolved.append(
            Candidate(
                ticker=c.ticker,
                ref_price=bar["close"],
                confidence=c.confidence,
                prediction_id=c.prediction_id,
                stop_price=c.stop_price,
                target_price=c.target_price,
                horizon_end_date=c.horizon_end_date,
            )
        )

    for order in decide_entries(
        nav=nav_for_sizing,
        cash=cash,
        held_tickers=held,
        candidates=resolved,
        params=params,
    ):
        buy = costs.simulate_buy(market, order.qty, order.ref_price)
        if buy["cash_out"] > cash:
            continue  # safety: never go negative cash
        models.insert_trade(
            conn,
            Trade(
                account_id=account.id,
                ticker=order.ticker,
                side="BUY",
                qty=order.qty,
                price=buy["fill_price"],
                gross=buy["gross"],
                fees=buy["commission"],
                tax=0.0,
                slippage=buy["slippage"],
                net_cash_delta=-buy["cash_out"],
                executed_at=date,
                reason="entry",
                prediction_id=order.prediction_id,
            ),
        )
        models.insert_position(
            conn,
            Position(
                account_id=account.id,
                ticker=order.ticker,
                qty=order.qty,
                avg_cost=buy["fill_price"],
                opened_at=date,
                prediction_id=order.prediction_id,
                target_price=order.target_price,
                stop_price=order.stop_price,
                horizon_end_date=order.horizon_end_date,
            ),
        )
        cash -= buy["cash_out"]

    # --- mark-to-market ---------------------------------------------------- #
    open_final = models.get_open_positions(conn, account.id)
    positions_value = _value_positions(open_final, prices, marks)
    nav = cash + positions_value

    prior = [n for n in history if n.date < date]
    prev_nav = max(prior, key=lambda n: n.date).nav if prior else None
    daily_return = (nav - prev_nav) / prev_nav if prev_nav else None
    cumulative_return = (nav - account.initial_capital) / account.initial_capital

    snap = NavSnapshot(
        account_id=account.id,
        date=date,
        cash=cash,
        positions_value=positions_value,
        nav=nav,
        daily_return=daily_return,
        cumulative_return=cumulative_return,
        n_positions=len(open_final),
        benchmark_nav=benchmark_nav,
    )
    models.update_cash(conn, account.id, cash)
    models.record_nav(conn, snap)
    return snap
