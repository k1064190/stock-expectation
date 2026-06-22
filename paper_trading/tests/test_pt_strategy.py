"""Tests for the pure paper-trading decision logic (entries + exits)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from paper_trading import strategy
from paper_trading.strategy import Candidate, PriceBar, Position_, StrategyParams


P = StrategyParams()


def _cand(ticker, ref_price, confidence, stop_price=None, **kw):
    return Candidate(
        ticker=ticker,
        ref_price=ref_price,
        confidence=confidence,
        stop_price=stop_price,
        **kw,
    )


# --- entries --------------------------------------------------------------- #


def test_entry_sizes_by_risk_budget():
    """qty = (nav * risk_per_trade) / (entry - stop), when that's the binding cap."""
    orders = strategy.decide_entries(
        nav=100_000,
        cash=100_000,
        held_tickers=set(),
        candidates=[_cand("AAA", ref_price=100.0, confidence=0.7, stop_price=90.0)],
        params=P,
    )
    assert len(orders) == 1
    # risk budget = 1000; risk/share = 10 → 100 shares.
    assert orders[0].qty == 100
    assert orders[0].stop_price == pytest.approx(90.0)


def test_entry_capped_by_max_position_pct():
    """A loose stop lets risk sizing run large, so the position-size cap binds."""
    params = StrategyParams(risk_per_trade_pct=0.50)  # huge risk budget
    orders = strategy.decide_entries(
        nav=100_000,
        cash=100_000,
        held_tickers=set(),
        candidates=[_cand("AAA", ref_price=100.0, confidence=0.7, stop_price=90.0)],
        params=params,
    )
    # cap = 20% * 100k / 100 = 200 shares.
    assert orders[0].qty == 200


def test_entry_capped_by_cash():
    """Limited cash binds; qty leaves headroom for costs."""
    orders = strategy.decide_entries(
        nav=100_000,
        cash=500.0,
        held_tickers=set(),
        candidates=[_cand("AAA", ref_price=100.0, confidence=0.7, stop_price=90.0)],
        params=P,
    )
    # floor(500 / (100 * 1.01)) = 4
    assert orders[0].qty == 4


def test_entry_skipped_when_cannot_afford_one_share():
    orders = strategy.decide_entries(
        nav=100_000,
        cash=50.0,
        held_tickers=set(),
        candidates=[_cand("AAA", ref_price=100.0, confidence=0.7, stop_price=90.0)],
        params=P,
    )
    assert orders == []


def test_entry_uses_fallback_stop_when_missing():
    """No stop_price → fallback stop at ref*(1-fallback_pct); sizing uses it."""
    orders = strategy.decide_entries(
        nav=100_000,
        cash=1_000_000,
        held_tickers=set(),
        candidates=[_cand("AAA", ref_price=100.0, confidence=0.7, stop_price=None)],
        params=P,
    )
    # fallback stop 8% → stop 92, risk/share 8, risk budget 1000 → 125 shares.
    assert orders[0].stop_price == pytest.approx(92.0)
    assert orders[0].qty == 125


def test_entry_excludes_below_confidence_floor():
    orders = strategy.decide_entries(
        nav=100_000,
        cash=100_000,
        held_tickers=set(),
        candidates=[_cand("AAA", ref_price=100.0, confidence=0.55, stop_price=90.0)],
        params=P,
    )
    assert orders == []


def test_entry_skips_already_held_ticker():
    orders = strategy.decide_entries(
        nav=100_000,
        cash=100_000,
        held_tickers={"AAA"},
        candidates=[_cand("AAA", ref_price=100.0, confidence=0.7, stop_price=90.0)],
        params=P,
    )
    assert orders == []


def test_entry_limits_count_and_prefers_highest_confidence():
    cands = [
        _cand(f"T{i}", ref_price=100.0, confidence=0.60 + i * 0.01, stop_price=90.0)
        for i in range(7)
    ]
    orders = strategy.decide_entries(
        nav=100_000,
        cash=100_000_000,
        held_tickers=set(),
        candidates=cands,
        params=StrategyParams(max_new_positions_per_day=5),
    )
    assert len(orders) == 5
    # Highest-confidence tickers chosen: T6..T2.
    assert {o.ticker for o in orders} == {"T6", "T5", "T4", "T3", "T2"}


# --- exits ----------------------------------------------------------------- #


def _pos(ticker, target=None, stop=None, horizon=None):
    return Position_(
        id=f"lot-{ticker}",
        ticker=ticker,
        qty=10,
        target_price=target,
        stop_price=stop,
        horizon_end_date=horizon,
        prediction_id="pred",
    )


def test_exit_on_target_hit():
    pos = _pos("AAA", target=120.0, stop=90.0, horizon="2026-12-31")
    bar = PriceBar(high=125.0, low=118.0, ref_price=121.0)
    exits = strategy.decide_exits([pos], {"AAA": bar}, today="2026-05-01")
    assert exits[0].reason == "target_hit"
    assert exits[0].fill_price == pytest.approx(120.0)


def test_exit_on_stop_hit():
    pos = _pos("AAA", target=120.0, stop=90.0, horizon="2026-12-31")
    bar = PriceBar(high=95.0, low=85.0, ref_price=88.0)
    exits = strategy.decide_exits([pos], {"AAA": bar}, today="2026-05-01")
    assert exits[0].reason == "stop_hit"
    assert exits[0].fill_price == pytest.approx(90.0)


def test_stop_takes_priority_when_both_touched():
    """If the day's range spans both stop and target, assume the adverse stop fill."""
    pos = _pos("AAA", target=120.0, stop=90.0, horizon="2026-12-31")
    bar = PriceBar(high=125.0, low=85.0, ref_price=100.0)
    exits = strategy.decide_exits([pos], {"AAA": bar}, today="2026-05-01")
    assert exits[0].reason == "stop_hit"


def test_exit_on_horizon_expiry():
    pos = _pos("AAA", target=120.0, stop=90.0, horizon="2026-05-01")
    bar = PriceBar(high=110.0, low=95.0, ref_price=105.0)  # neither target nor stop hit
    exits = strategy.decide_exits([pos], {"AAA": bar}, today="2026-05-01")
    assert exits[0].reason == "horizon_exit"
    assert exits[0].fill_price == pytest.approx(105.0)


def test_no_exit_when_within_range_and_before_horizon():
    pos = _pos("AAA", target=120.0, stop=90.0, horizon="2026-12-31")
    bar = PriceBar(high=110.0, low=95.0, ref_price=105.0)
    assert strategy.decide_exits([pos], {"AAA": bar}, today="2026-05-01") == []


def test_missing_price_carries_position():
    """No price for a held ticker (data gap) → no exit, position carried."""
    pos = _pos("AAA", target=120.0, stop=90.0, horizon="2026-05-01")
    assert strategy.decide_exits([pos], {}, today="2026-06-01") == []
