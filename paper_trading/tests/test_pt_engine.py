"""Integration tests for one daily paper-trading cycle (engine.run_day)."""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from paper_trading import engine, models
from paper_trading.strategy import Candidate, StrategyParams


@pytest.fixture
def conn():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    c = models.get_connection(db_path)
    yield c
    c.close()
    db_path.unlink(missing_ok=True)
    Path(str(db_path) + "-wal").unlink(missing_ok=True)
    Path(str(db_path) + "-shm").unlink(missing_ok=True)


def _cand(ticker="AAA", conf=0.7, stop=90.0, target=120.0, horizon="2026-05-08"):
    return Candidate(
        ticker=ticker,
        ref_price=0.0,  # engine resolves from the day's close
        confidence=conf,
        stop_price=stop,
        target_price=target,
        horizon_end_date=horizon,
        prediction_id=f"pred-{ticker}",
    )


def test_buy_then_target_exit_updates_cash_and_nav(conn):
    acct = models.seed_account(conn, market="US", initial_capital=100_000.0)

    # Day 1: buy AAA at close 100. risk 1% / (100-90) → 100 shares.
    engine.run_day(
        conn,
        acct,
        date="2026-05-01",
        prices={"AAA": {"open": 100, "high": 101, "low": 99, "close": 100}},
        candidates=[_cand()],
    )
    acct = models.get_account(conn, "US")
    positions = models.get_open_positions(conn, acct.id)
    assert len(positions) == 1
    assert positions[0].qty == 100
    trades = models.get_trades(conn, acct.id)
    assert len(trades) == 1 and trades[0].side == "BUY" and trades[0].reason == "entry"
    # buy fill = 100*(1+0.0005)=100.05 → cash_out 10005 → cash 89995.
    assert acct.cash == pytest.approx(89_995.0, abs=1.0)
    d1 = models.get_latest_nav(conn, acct.id)
    assert d1.date == "2026-05-01"
    assert d1.nav == pytest.approx(99_995.0, abs=1.0)  # cash + 100*close(100)

    # Day 2: target 120 is touched → exit at target.
    engine.run_day(
        conn,
        acct,
        date="2026-05-02",
        prices={"AAA": {"open": 120, "high": 125, "low": 118, "close": 122}},
        candidates=[],
    )
    acct = models.get_account(conn, "US")
    assert models.get_open_positions(conn, acct.id) == []
    trades = models.get_trades(conn, acct.id)
    assert len(trades) == 2
    assert trades[1].side == "SELL" and trades[1].reason == "target_hit"
    # sell fill = 120*(1-0.0005)=119.94 → +11994 → cash 101989.
    assert acct.cash == pytest.approx(101_989.0, abs=1.0)
    d2 = models.get_latest_nav(conn, acct.id)
    assert d2.nav == pytest.approx(101_989.0, abs=1.0)
    assert d2.nav > d1.nav


def test_rerun_same_date_is_idempotent(conn):
    acct = models.seed_account(conn, market="US", initial_capital=100_000.0)
    prices = {"AAA": {"open": 100, "high": 101, "low": 99, "close": 100}}
    engine.run_day(conn, acct, "2026-05-01", prices, [_cand()])
    engine.run_day(
        conn, models.get_account(conn, "US"), "2026-05-01", prices, [_cand()]
    )
    acct = models.get_account(conn, "US")
    assert len(models.get_trades(conn, acct.id)) == 1  # not duplicated
    assert len(models.get_nav_history(conn, acct.id)) == 1
    assert len(models.get_open_positions(conn, acct.id)) == 1


def test_records_nav_with_no_activity(conn):
    acct = models.seed_account(conn, market="KR", initial_capital=100_000_000.0)
    engine.run_day(conn, acct, "2026-05-01", prices={}, candidates=[])
    nav = models.get_latest_nav(conn, acct.id)
    assert nav is not None
    assert nav.nav == pytest.approx(100_000_000.0)
    assert nav.n_positions == 0


def test_horizon_exit_fills_at_close(conn):
    acct = models.seed_account(conn, market="US", initial_capital=100_000.0)
    engine.run_day(
        conn,
        acct,
        "2026-05-01",
        {"AAA": {"open": 100, "high": 101, "low": 99, "close": 100}},
        [_cand(horizon="2026-05-04")],
    )
    # Day at horizon end, neither target nor stop touched → exit at close.
    engine.run_day(
        conn,
        models.get_account(conn, "US"),
        "2026-05-04",
        {"AAA": {"open": 104, "high": 106, "low": 103, "close": 105}},
        [],
    )
    acct = models.get_account(conn, "US")
    assert models.get_open_positions(conn, acct.id) == []
    trades = models.get_trades(conn, acct.id)
    assert trades[1].reason == "horizon_exit"
    # sell fill ≈ close 105 * (1-0.0005).
    assert trades[1].price == pytest.approx(105 * (1 - 0.0005), abs=0.01)


def test_missing_price_carries_position_and_marks_at_avg_cost(conn):
    acct = models.seed_account(conn, market="US", initial_capital=100_000.0)
    engine.run_day(
        conn,
        acct,
        "2026-05-01",
        {"AAA": {"open": 100, "high": 101, "low": 99, "close": 100}},
        [_cand(horizon="2026-05-20")],
    )
    # Next day: no price for AAA (data gap) → carry; NAV marks the lot at avg_cost.
    engine.run_day(conn, models.get_account(conn, "US"), "2026-05-02", {}, [])
    acct = models.get_account(conn, "US")
    assert len(models.get_open_positions(conn, acct.id)) == 1
    nav = models.get_latest_nav(conn, acct.id)
    # positions_value uses avg_cost (≈100.05*100) when no fresh price and no marks.
    assert nav.positions_value == pytest.approx(100 * 100.05, abs=1.0)


def test_missing_price_uses_carry_forward_mark(conn):
    """On a data gap, a held lot is marked at the last-known close (marks), not avg_cost."""
    acct = models.seed_account(conn, market="US", initial_capital=100_000.0)
    engine.run_day(
        conn,
        acct,
        "2026-05-01",
        {"AAA": {"open": 100, "high": 101, "low": 99, "close": 100}},
        [_cand(horizon="2026-05-20")],
    )
    # No fresh AAA bar today, but a carried mark of 110 is supplied.
    engine.run_day(
        conn,
        models.get_account(conn, "US"),
        "2026-05-02",
        {},
        [],
        marks={"AAA": 110.0},
    )
    nav = models.get_latest_nav(conn, models.get_account(conn, "US").id)
    assert nav.positions_value == pytest.approx(100 * 110.0, abs=1e-6)
