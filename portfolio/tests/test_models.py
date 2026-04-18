"""Tests for portfolio data models."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from portfolio.models import Side, Currency, Portfolio, Transaction


def test_side_enum():
    assert Side.BUY.value == "BUY"
    assert Side.SELL.value == "SELL"
    assert Side("BUY") == Side.BUY


def test_currency_enum():
    assert Currency.KRW.value == "KRW"
    assert Currency.USD.value == "USD"


def test_portfolio_creation():
    pf = Portfolio(market="KR", name="Toss KR")
    assert pf.id.startswith("pf_")
    assert pf.market == "KR"
    assert pf.name == "Toss KR"
    assert pf.created_at is not None


def test_portfolio_id_format():
    pf = Portfolio(market="US", name="Toss US")
    assert pf.id.startswith("pf_")
    assert len(pf.id) > 3


def test_transaction_creation():
    tx = Transaction(
        portfolio_id="pf_kr_default",
        ticker="005930",
        side="BUY",
        quantity=10,
        price=55000.0,
        currency="KRW",
        transacted_at="2026-03-15",
    )
    assert tx.id is None
    assert tx.portfolio_id == "pf_kr_default"
    assert tx.ticker == "005930"
    assert tx.side == "BUY"
    assert tx.quantity == 10
    assert tx.note is None
    assert tx.thesis_id is None


def test_transaction_with_optional_fields():
    tx = Transaction(
        portfolio_id="pf_us_default",
        ticker="NVDA",
        side="SELL",
        quantity=5.5,
        price=120.50,
        currency="USD",
        transacted_at="2026-04-01",
        note="partial take-profit",
        thesis_id="th_abc123",
    )
    assert tx.quantity == 5.5
    assert tx.note == "partial take-profit"
    assert tx.thesis_id == "th_abc123"
