"""Tests for Toss sync reconciliation logic."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from portfolio.models import Position
from portfolio.toss_sync import reconcile, _normalize_ticker, _market_from_toss


class TestNormalizeTicker:
    def test_kr_stock_strips_a_prefix(self):
        pos = {"symbol": "A005930", "market_type": "KR_STOCK"}
        assert _normalize_ticker(pos) == "005930"

    def test_us_stock_unchanged(self):
        pos = {"symbol": "NVDA", "market_type": "US_STOCK"}
        assert _normalize_ticker(pos) == "NVDA"

    def test_kr_etf_strips_a_prefix(self):
        pos = {"symbol": "A379800", "market_type": "KR_STOCK"}
        assert _normalize_ticker(pos) == "379800"


class TestMarketFromToss:
    def test_kr_stock(self):
        assert _market_from_toss("KR_STOCK") == "KR"

    def test_us_stock(self):
        assert _market_from_toss("US_STOCK") == "US"

    def test_unsupported(self):
        assert _market_from_toss("US_BOND") is None


class TestReconcile:
    def _make_toss_pos(self, symbol, market_type, quantity, avg_price, name="Test"):
        """Helper to create a tossctl-style position dict."""
        pos = {
            "symbol": symbol,
            "market_type": market_type,
            "quantity": quantity,
            "average_price": avg_price,
            "current_price": avg_price,
            "name": name,
        }
        if market_type == "US_STOCK":
            pos["average_price_usd"] = avg_price
            pos["current_price_usd"] = avg_price
        return pos

    def test_new_position(self):
        toss = [self._make_toss_pos("A005930", "KR_STOCK", 10, 55000, "삼성전자")]
        local = []
        actions = reconcile(toss, local, "KR")
        assert len(actions) == 1
        assert actions[0]["side"] == "BUY"
        assert actions[0]["ticker"] == "005930"
        assert actions[0]["quantity"] == 10

    def test_same_position_no_action(self):
        toss = [self._make_toss_pos("A005930", "KR_STOCK", 10, 55000)]
        local = [Position("pf_kr", "005930", 10, 55000.0, 550000.0, 0.0)]
        actions = reconcile(toss, local, "KR")
        assert len(actions) == 0

    def test_quantity_increased(self):
        toss = [self._make_toss_pos("A005930", "KR_STOCK", 15, 56000)]
        local = [Position("pf_kr", "005930", 10, 55000.0, 550000.0, 0.0)]
        actions = reconcile(toss, local, "KR")
        assert len(actions) == 1
        assert actions[0]["side"] == "BUY"
        assert actions[0]["quantity"] == 5

    def test_quantity_decreased(self):
        toss = [self._make_toss_pos("A005930", "KR_STOCK", 5, 55000)]
        local = [Position("pf_kr", "005930", 10, 55000.0, 550000.0, 0.0)]
        actions = reconcile(toss, local, "KR")
        assert len(actions) == 1
        assert actions[0]["side"] == "SELL"
        assert actions[0]["quantity"] == 5

    def test_position_closed_on_toss(self):
        toss = []
        local = [Position("pf_kr", "005930", 10, 55000.0, 550000.0, 0.0)]
        actions = reconcile(toss, local, "KR")
        assert len(actions) == 1
        assert actions[0]["side"] == "SELL"
        assert actions[0]["quantity"] == 10

    def test_us_market_uses_usd_price(self):
        toss = [self._make_toss_pos("NVDA", "US_STOCK", 5, 120.0)]
        local = []
        actions = reconcile(toss, local, "US")
        assert len(actions) == 1
        assert actions[0]["currency"] == "USD"
        assert actions[0]["price"] == 120.0

    def test_filters_by_market(self):
        toss = [
            self._make_toss_pos("A005930", "KR_STOCK", 10, 55000),
            self._make_toss_pos("NVDA", "US_STOCK", 5, 120.0),
        ]
        local = []
        kr_actions = reconcile(toss, local, "KR")
        us_actions = reconcile(toss, local, "US")
        assert len(kr_actions) == 1
        assert kr_actions[0]["ticker"] == "005930"
        assert len(us_actions) == 1
        assert us_actions[0]["ticker"] == "NVDA"
