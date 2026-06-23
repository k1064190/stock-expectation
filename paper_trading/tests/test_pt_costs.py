"""Tests for the paper-trading cost model."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from paper_trading import costs


def test_buy_applies_slippage_up_and_commission():
    """A US buy fills above the raw price (slippage) and pays commission."""
    r = costs.simulate_buy("US", qty=10, raw_price=100.0)
    assert r["fill_price"] > 100.0  # slippage makes a buy worse
    assert r["fill_price"] == pytest.approx(100.0 * (1 + costs.SLIPPAGE_RATE))
    assert r["commission"] == pytest.approx(r["gross"] * costs.US_COMMISSION_RATE)
    # Cash leaves the account: gross at fill price + commission.
    assert r["cash_out"] == pytest.approx(r["gross"] + r["commission"])
    assert r["cash_out"] > 0


def test_sell_applies_slippage_down_and_kr_tax():
    """A KR sell fills below the raw price and pays commission + transaction tax."""
    r = costs.simulate_sell("KR", qty=10, raw_price=1000.0)
    assert r["fill_price"] < 1000.0  # slippage makes a sell worse
    assert r["fill_price"] == pytest.approx(1000.0 * (1 - costs.SLIPPAGE_RATE))
    assert r["tax"] == pytest.approx(r["gross"] * costs.KR_SELL_TAX_RATE)
    assert r["commission"] == pytest.approx(r["gross"] * costs.KR_COMMISSION_RATE)
    # Net proceeds are gross minus both commission and tax.
    assert r["cash_in"] == pytest.approx(r["gross"] - r["commission"] - r["tax"])
    assert r["cash_in"] < r["gross"]


def test_us_sell_has_no_transaction_tax():
    """US sells pay no Korean transaction tax."""
    r = costs.simulate_sell("US", qty=5, raw_price=200.0)
    assert r["tax"] == 0.0


def test_round_trip_loses_to_costs():
    """Buying then immediately selling at the same raw price loses money to costs."""
    buy = costs.simulate_buy("KR", qty=10, raw_price=1000.0)
    sell = costs.simulate_sell("KR", qty=10, raw_price=1000.0)
    assert sell["cash_in"] < buy["cash_out"]
