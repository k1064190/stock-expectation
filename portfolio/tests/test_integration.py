"""End-to-end integration test for portfolio workflow."""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from portfolio.db import (
    get_connection,
    create_portfolio,
    add_transaction,
    compute_positions,
)
from portfolio.evaluator import compute_report, compute_risk


@pytest.fixture
def db_conn():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    conn = get_connection(db_path)
    yield conn
    conn.close()
    db_path.unlink(missing_ok=True)
    Path(str(db_path) + "-wal").unlink(missing_ok=True)
    Path(str(db_path) + "-shm").unlink(missing_ok=True)


def test_full_kr_portfolio_workflow(db_conn):
    """Simulate: create portfolio, buy 3 stocks, sell 1 partially, evaluate."""
    pf = create_portfolio(db_conn, market="KR", name="Toss KR")

    # Buy Samsung Electronics
    add_transaction(
        db_conn,
        portfolio_id=pf.id,
        ticker="005930",
        side="BUY",
        quantity=10,
        price=55000.0,
        currency="KRW",
        transacted_at="2026-01-10",
    )
    # Buy SK Hynix
    add_transaction(
        db_conn,
        portfolio_id=pf.id,
        ticker="000660",
        side="BUY",
        quantity=5,
        price=120000.0,
        currency="KRW",
        transacted_at="2026-01-15",
    )
    # Buy more Samsung
    add_transaction(
        db_conn,
        portfolio_id=pf.id,
        ticker="005930",
        side="BUY",
        quantity=10,
        price=60000.0,
        currency="KRW",
        transacted_at="2026-02-01",
    )
    # Sell half Samsung
    add_transaction(
        db_conn,
        portfolio_id=pf.id,
        ticker="005930",
        side="SELL",
        quantity=10,
        price=65000.0,
        currency="KRW",
        transacted_at="2026-03-01",
    )

    # Compute positions
    positions = compute_positions(db_conn, pf.id)
    assert len(positions) == 2

    samsung = next(p for p in positions if p.ticker == "005930")
    assert samsung.quantity == 10
    # avg = (10*55000 + 10*60000) / 20 = 57500
    assert samsung.avg_price == 57500.0
    # realized from selling 10 at 65000: (65000 - 57500) * 10 = 75000
    assert samsung.realized_pnl == 75000.0

    hynix = next(p for p in positions if p.ticker == "000660")
    assert hynix.quantity == 5
    assert hynix.avg_price == 120000.0

    # Report with mock prices
    current_prices = {"005930": 62000.0, "000660": 130000.0}
    report = compute_report(positions, current_prices)
    assert report["total_cost"] == 10 * 57500.0 + 5 * 120000.0
    assert report["total_realized_pnl"] == 75000.0
    assert len(report["holdings"]) == 2

    # Risk
    risk = compute_risk(positions, current_prices)
    assert risk["position_count"] == 2
    assert risk["hhi"] > 0
    assert len(risk["concentration"]) == 2


def test_full_us_portfolio_workflow(db_conn):
    """Simulate: create US portfolio, buy fractional shares, evaluate."""
    pf = create_portfolio(db_conn, market="US", name="Toss US")

    add_transaction(
        db_conn,
        portfolio_id=pf.id,
        ticker="NVDA",
        side="BUY",
        quantity=5.5,
        price=120.0,
        currency="USD",
        transacted_at="2026-02-01",
    )
    add_transaction(
        db_conn,
        portfolio_id=pf.id,
        ticker="AAPL",
        side="BUY",
        quantity=10,
        price=180.0,
        currency="USD",
        transacted_at="2026-02-01",
    )

    positions = compute_positions(db_conn, pf.id)
    assert len(positions) == 2

    nvda = next(p for p in positions if p.ticker == "NVDA")
    assert nvda.quantity == 5.5

    current_prices = {"NVDA": 130.0, "AAPL": 190.0}
    report = compute_report(positions, current_prices)
    assert report["total_return_pnl"] > 0
