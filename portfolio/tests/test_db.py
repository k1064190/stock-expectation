"""Tests for portfolio database operations."""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from portfolio.models import Portfolio, Transaction, Position
from portfolio.db import (
    get_connection,
    create_portfolio,
    list_portfolios,
    get_portfolio_for_market,
    add_transaction,
    list_transactions,
    delete_transaction,
    compute_positions,
)


@pytest.fixture
def db_conn():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    conn = get_connection(db_path)
    yield conn
    conn.close()
    db_path.unlink(missing_ok=True)
    Path(str(db_path) + "-wal").unlink(missing_ok=True)
    Path(str(db_path) + "-shm").unlink(missing_ok=True)


@pytest.fixture
def kr_portfolio(db_conn):
    """Create a KR portfolio for testing."""
    return create_portfolio(db_conn, market="KR", name="Toss KR")


class TestPortfolioCRUD:
    def test_create_portfolio(self, db_conn):
        pf = create_portfolio(db_conn, market="KR", name="Toss KR")
        assert pf.market == "KR"
        assert pf.name == "Toss KR"
        assert pf.id.startswith("pf_")

    def test_list_portfolios(self, db_conn):
        create_portfolio(db_conn, market="KR", name="Toss KR")
        create_portfolio(db_conn, market="US", name="Toss US")
        pfs = list_portfolios(db_conn)
        assert len(pfs) == 2

    def test_get_portfolio_for_market(self, db_conn):
        create_portfolio(db_conn, market="KR", name="Toss KR")
        pf = get_portfolio_for_market(db_conn, "KR")
        assert pf is not None
        assert pf.market == "KR"

    def test_get_portfolio_for_market_returns_none(self, db_conn):
        pf = get_portfolio_for_market(db_conn, "KR")
        assert pf is None


class TestTransactions:
    def test_add_buy_transaction(self, db_conn, kr_portfolio):
        tx = add_transaction(
            db_conn,
            portfolio_id=kr_portfolio.id,
            ticker="005930",
            side="BUY",
            quantity=10,
            price=55000.0,
            currency="KRW",
            transacted_at="2026-03-15",
        )
        assert tx.id is not None
        assert tx.ticker == "005930"
        assert tx.side == "BUY"

    def test_add_sell_transaction(self, db_conn, kr_portfolio):
        add_transaction(
            db_conn,
            portfolio_id=kr_portfolio.id,
            ticker="005930",
            side="BUY",
            quantity=10,
            price=55000.0,
            currency="KRW",
            transacted_at="2026-03-15",
        )
        tx = add_transaction(
            db_conn,
            portfolio_id=kr_portfolio.id,
            ticker="005930",
            side="SELL",
            quantity=5,
            price=60000.0,
            currency="KRW",
            transacted_at="2026-04-01",
        )
        assert tx.side == "SELL"
        assert tx.quantity == 5

    def test_list_transactions(self, db_conn, kr_portfolio):
        add_transaction(
            db_conn,
            portfolio_id=kr_portfolio.id,
            ticker="005930",
            side="BUY",
            quantity=10,
            price=55000.0,
            currency="KRW",
            transacted_at="2026-03-15",
        )
        add_transaction(
            db_conn,
            portfolio_id=kr_portfolio.id,
            ticker="000660",
            side="BUY",
            quantity=5,
            price=120000.0,
            currency="KRW",
            transacted_at="2026-03-16",
        )
        txs = list_transactions(db_conn, portfolio_id=kr_portfolio.id)
        assert len(txs) == 2

    def test_list_transactions_filter_ticker(self, db_conn, kr_portfolio):
        add_transaction(
            db_conn,
            portfolio_id=kr_portfolio.id,
            ticker="005930",
            side="BUY",
            quantity=10,
            price=55000.0,
            currency="KRW",
            transacted_at="2026-03-15",
        )
        add_transaction(
            db_conn,
            portfolio_id=kr_portfolio.id,
            ticker="000660",
            side="BUY",
            quantity=5,
            price=120000.0,
            currency="KRW",
            transacted_at="2026-03-16",
        )
        txs = list_transactions(db_conn, portfolio_id=kr_portfolio.id, ticker="005930")
        assert len(txs) == 1
        assert txs[0].ticker == "005930"

    def test_delete_transaction(self, db_conn, kr_portfolio):
        tx = add_transaction(
            db_conn,
            portfolio_id=kr_portfolio.id,
            ticker="005930",
            side="BUY",
            quantity=10,
            price=55000.0,
            currency="KRW",
            transacted_at="2026-03-15",
        )
        assert delete_transaction(db_conn, tx.id) is True
        txs = list_transactions(db_conn, portfolio_id=kr_portfolio.id)
        assert len(txs) == 0

    def test_delete_nonexistent_transaction(self, db_conn):
        assert delete_transaction(db_conn, 9999) is False


class TestPositions:
    def test_single_buy(self, db_conn, kr_portfolio):
        add_transaction(
            db_conn,
            portfolio_id=kr_portfolio.id,
            ticker="005930",
            side="BUY",
            quantity=10,
            price=55000.0,
            currency="KRW",
            transacted_at="2026-03-15",
        )
        positions = compute_positions(db_conn, kr_portfolio.id)
        assert len(positions) == 1
        pos = positions[0]
        assert pos.ticker == "005930"
        assert pos.quantity == 10
        assert pos.avg_price == 55000.0
        assert pos.total_cost == 550000.0
        assert pos.realized_pnl == 0.0

    def test_multiple_buys_avg_price(self, db_conn, kr_portfolio):
        add_transaction(
            db_conn,
            portfolio_id=kr_portfolio.id,
            ticker="005930",
            side="BUY",
            quantity=10,
            price=55000.0,
            currency="KRW",
            transacted_at="2026-03-15",
        )
        add_transaction(
            db_conn,
            portfolio_id=kr_portfolio.id,
            ticker="005930",
            side="BUY",
            quantity=10,
            price=60000.0,
            currency="KRW",
            transacted_at="2026-03-20",
        )
        positions = compute_positions(db_conn, kr_portfolio.id)
        pos = positions[0]
        assert pos.quantity == 20
        assert pos.avg_price == 57500.0  # (10*55000 + 10*60000) / 20
        assert pos.total_cost == 20 * 57500.0
        assert pos.realized_pnl == 0.0

    def test_buy_then_sell_realized_pnl(self, db_conn, kr_portfolio):
        add_transaction(
            db_conn,
            portfolio_id=kr_portfolio.id,
            ticker="005930",
            side="BUY",
            quantity=10,
            price=55000.0,
            currency="KRW",
            transacted_at="2026-03-15",
        )
        add_transaction(
            db_conn,
            portfolio_id=kr_portfolio.id,
            ticker="005930",
            side="SELL",
            quantity=5,
            price=60000.0,
            currency="KRW",
            transacted_at="2026-04-01",
        )
        positions = compute_positions(db_conn, kr_portfolio.id)
        pos = positions[0]
        assert pos.quantity == 5
        assert pos.avg_price == 55000.0  # avg unchanged after sell
        assert pos.total_cost == 5 * 55000.0
        assert pos.realized_pnl == 25000.0  # (60000-55000)*5

    def test_fully_closed_position_excluded(self, db_conn, kr_portfolio):
        add_transaction(
            db_conn,
            portfolio_id=kr_portfolio.id,
            ticker="005930",
            side="BUY",
            quantity=10,
            price=55000.0,
            currency="KRW",
            transacted_at="2026-03-15",
        )
        add_transaction(
            db_conn,
            portfolio_id=kr_portfolio.id,
            ticker="005930",
            side="SELL",
            quantity=10,
            price=60000.0,
            currency="KRW",
            transacted_at="2026-04-01",
        )
        positions = compute_positions(db_conn, kr_portfolio.id)
        assert len(positions) == 0

    def test_multiple_tickers(self, db_conn, kr_portfolio):
        add_transaction(
            db_conn,
            portfolio_id=kr_portfolio.id,
            ticker="005930",
            side="BUY",
            quantity=10,
            price=55000.0,
            currency="KRW",
            transacted_at="2026-03-15",
        )
        add_transaction(
            db_conn,
            portfolio_id=kr_portfolio.id,
            ticker="000660",
            side="BUY",
            quantity=5,
            price=120000.0,
            currency="KRW",
            transacted_at="2026-03-16",
        )
        positions = compute_positions(db_conn, kr_portfolio.id)
        assert len(positions) == 2
        tickers = {p.ticker for p in positions}
        assert tickers == {"005930", "000660"}
