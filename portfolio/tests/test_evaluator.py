"""Tests for portfolio evaluation functions."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from portfolio.models import Position
from portfolio.evaluator import compute_report, compute_risk


class TestReport:
    def test_single_position_report(self):
        positions = [
            Position(
                portfolio_id="pf_kr",
                ticker="005930",
                quantity=10,
                avg_price=55000.0,
                total_cost=550000.0,
                realized_pnl=0.0,
            )
        ]
        current_prices = {"005930": 60000.0}
        report = compute_report(positions, current_prices)

        assert len(report["holdings"]) == 1
        h = report["holdings"][0]
        assert h["ticker"] == "005930"
        assert h["quantity"] == 10
        assert h["avg_price"] == 55000.0
        assert h["current_price"] == 60000.0
        assert h["market_value"] == 600000.0
        assert h["unrealized_pnl"] == 50000.0
        assert h["unrealized_pnl_pct"] == pytest.approx(9.09, abs=0.01)
        assert h["realized_pnl"] == 0.0
        assert report["total_cost"] == 550000.0
        assert report["total_market_value"] == 600000.0
        assert report["total_unrealized_pnl"] == 50000.0
        assert report["total_realized_pnl"] == 0.0

    def test_multiple_positions_report(self):
        positions = [
            Position("pf_kr", "005930", 10, 55000.0, 550000.0, 0.0),
            Position("pf_kr", "000660", 5, 120000.0, 600000.0, 25000.0),
        ]
        current_prices = {"005930": 60000.0, "000660": 110000.0}
        report = compute_report(positions, current_prices)
        assert len(report["holdings"]) == 2
        assert report["total_cost"] == 1150000.0
        assert report["total_market_value"] == 600000.0 + 550000.0
        assert report["total_realized_pnl"] == 25000.0

    def test_report_missing_price_uses_none(self):
        positions = [
            Position("pf_kr", "005930", 10, 55000.0, 550000.0, 0.0),
        ]
        current_prices = {}
        report = compute_report(positions, current_prices)
        h = report["holdings"][0]
        assert h["current_price"] is None
        assert h["unrealized_pnl"] is None


class TestRisk:
    def test_concentration_single_stock(self):
        positions = [Position("pf_kr", "005930", 10, 55000.0, 550000.0, 0.0)]
        current_prices = {"005930": 55000.0}
        risk = compute_risk(positions, current_prices)
        assert len(risk["concentration"]) == 1
        assert risk["concentration"][0]["weight_pct"] == 100.0
        assert risk["hhi"] == 10000

    def test_concentration_two_stocks(self):
        positions = [
            Position("pf_kr", "005930", 10, 55000.0, 550000.0, 0.0),
            Position("pf_kr", "000660", 5, 110000.0, 550000.0, 0.0),
        ]
        current_prices = {"005930": 55000.0, "000660": 110000.0}
        risk = compute_risk(positions, current_prices)
        assert len(risk["concentration"]) == 2
        for c in risk["concentration"]:
            assert c["weight_pct"] == pytest.approx(50.0, abs=0.1)
        assert risk["hhi"] == pytest.approx(5000, abs=1)

    def test_overweight_warning(self):
        positions = [
            Position("pf_kr", "005930", 100, 55000.0, 5500000.0, 0.0),
            Position("pf_kr", "000660", 1, 110000.0, 110000.0, 0.0),
        ]
        current_prices = {"005930": 55000.0, "000660": 110000.0}
        risk = compute_risk(positions, current_prices)
        assert len(risk["warnings"]) >= 1
        assert any("005930" in w for w in risk["warnings"])
