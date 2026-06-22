"""Tests for paper-trading DB schema, dataclasses, and CRUD."""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from paper_trading import models
from paper_trading.models import Position, Trade, NavSnapshot


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


def test_seed_account_is_idempotent(conn):
    """Seeding the same market twice returns the same account, no duplicate."""
    a1 = models.seed_account(conn, market="KR", initial_capital=100_000_000.0)
    a2 = models.seed_account(conn, market="KR", initial_capital=100_000_000.0)
    assert a1.id == a2.id
    assert a1.base_currency == "KRW"
    assert a1.cash == pytest.approx(100_000_000.0)
    # US book defaults to USD.
    us = models.seed_account(conn, market="US", initial_capital=100_000.0)
    assert us.base_currency == "USD"
    assert {models.get_account(conn, "KR").id, models.get_account(conn, "US").id} == {
        a1.id,
        us.id,
    }


def test_update_cash_persists(conn):
    a = models.seed_account(conn, market="US", initial_capital=100_000.0)
    models.update_cash(conn, a.id, 95_000.0)
    assert models.get_account(conn, "US").cash == pytest.approx(95_000.0)


def test_position_lifecycle(conn):
    """Insert two lots (same ticker allowed), list them, then close one."""
    a = models.seed_account(conn, market="US", initial_capital=100_000.0)
    p1 = Position(
        id="lot1",
        account_id=a.id,
        ticker="NVDA",
        qty=10,
        avg_cost=100.0,
        opened_at="2026-05-01",
        prediction_id="pred1",
        target_price=120.0,
        stop_price=90.0,
        horizon_end_date="2026-05-08",
    )
    p2 = Position(
        id="lot2",
        account_id=a.id,
        ticker="NVDA",
        qty=5,
        avg_cost=105.0,
        opened_at="2026-05-02",
        prediction_id="pred2",
        target_price=130.0,
        stop_price=95.0,
        horizon_end_date="2026-05-09",
    )
    models.insert_position(conn, p1)
    models.insert_position(conn, p2)
    held = models.get_open_positions(conn, a.id)
    assert len(held) == 2
    assert {h.ticker for h in held} == {"NVDA"}

    models.close_position(conn, "lot1")
    remaining = models.get_open_positions(conn, a.id)
    assert [r.id for r in remaining] == ["lot2"]


def test_insert_and_list_trades(conn):
    a = models.seed_account(conn, market="KR", initial_capital=100_000_000.0)
    t = Trade(
        account_id=a.id,
        ticker="005930",
        side="BUY",
        qty=100,
        price=55_000.0,
        gross=5_500_000.0,
        fees=825.0,
        tax=0.0,
        slippage=2_750.0,
        net_cash_delta=-5_500_825.0,
        executed_at="2026-05-01",
        reason="entry",
        prediction_id="pred1",
    )
    models.insert_trade(conn, t)
    trades = models.get_trades(conn, a.id)
    assert len(trades) == 1
    assert trades[0].side == "BUY"
    assert trades[0].reason == "entry"


def test_record_nav_is_idempotent_per_date(conn):
    """Recording NAV twice for the same (account, date) replaces, not duplicates."""
    a = models.seed_account(conn, market="US", initial_capital=100_000.0)
    models.record_nav(
        conn,
        NavSnapshot(
            account_id=a.id,
            date="2026-05-01",
            cash=100_000.0,
            positions_value=0.0,
            nav=100_000.0,
            daily_return=0.0,
            cumulative_return=0.0,
            n_positions=0,
            benchmark_nav=100_000.0,
        ),
    )
    models.record_nav(
        conn,
        NavSnapshot(
            account_id=a.id,
            date="2026-05-01",
            cash=98_000.0,
            positions_value=4_000.0,
            nav=102_000.0,
            daily_return=0.02,
            cumulative_return=0.02,
            n_positions=1,
            benchmark_nav=100_500.0,
        ),
    )
    hist = models.get_nav_history(conn, a.id)
    assert len(hist) == 1  # replaced, not duplicated
    assert hist[0].nav == pytest.approx(102_000.0)


def test_get_latest_nav_returns_most_recent_date(conn):
    a = models.seed_account(conn, market="US", initial_capital=100_000.0)
    for d, nav in [
        ("2026-05-01", 100_000.0),
        ("2026-05-03", 101_000.0),
        ("2026-05-02", 99_000.0),
    ]:
        models.record_nav(
            conn,
            NavSnapshot(
                account_id=a.id,
                date=d,
                cash=nav,
                positions_value=0.0,
                nav=nav,
                daily_return=None,
                cumulative_return=None,
                n_positions=0,
                benchmark_nav=None,
            ),
        )
    latest = models.get_latest_nav(conn, a.id)
    assert latest.date == "2026-05-03"
    assert latest.nav == pytest.approx(101_000.0)
