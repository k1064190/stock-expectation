"""Tests for the pure metrics in the paper-trading review."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from paper_trading import review
from paper_trading.models import NavSnapshot, Trade


def _nav(date, nav):
    return NavSnapshot(
        account_id="a",
        date=date,
        cash=nav,
        positions_value=0.0,
        nav=nav,
        daily_return=None,
        cumulative_return=None,
        n_positions=0,
    )


def _trade(side, pid, net, reason, ticker="AAA"):
    return Trade(
        account_id="a",
        ticker=ticker,
        side=side,
        qty=1,
        price=1.0,
        gross=abs(net),
        fees=0.0,
        tax=0.0,
        slippage=0.0,
        net_cash_delta=net,
        executed_at="2026-05-01",
        reason=reason,
        prediction_id=pid,
    )


def test_book_metrics_cumret_and_drawdown():
    navs = [
        _nav("2026-05-01", 100.0),
        _nav("2026-05-02", 110.0),
        _nav("2026-05-03", 99.0),
    ]
    m = review.book_metrics(navs)
    assert m["cumulative_return"] == pytest.approx(-0.01)  # 99/100 - 1
    # Peak 110 → trough 99 → drawdown -10%.
    assert m["max_drawdown"] == pytest.approx(-0.10, abs=1e-9)


def test_realized_trades_pairs_buy_and_sell_by_prediction_id():
    trades = [
        _trade("BUY", "p1", -1000.0, "entry"),
        _trade("SELL", "p1", 1100.0, "target_hit"),
        _trade("BUY", "p2", -1000.0, "entry"),
        _trade("SELL", "p2", 900.0, "stop_hit"),
        _trade("BUY", "p3", -1000.0, "entry"),  # still open: no matching SELL
    ]
    realized = review.realized_trades(trades)
    by_pid = {r["prediction_id"]: r for r in realized}
    assert set(by_pid) == {"p1", "p2"}  # p3 open → excluded
    assert by_pid["p1"]["pnl"] == pytest.approx(100.0)
    assert by_pid["p1"]["ret"] == pytest.approx(0.10)
    assert by_pid["p1"]["exit_reason"] == "target_hit"
    assert by_pid["p2"]["pnl"] == pytest.approx(-100.0)


def test_win_stats_counts_wins_and_groups_by_reason():
    realized = [
        {"prediction_id": "p1", "pnl": 100.0, "ret": 0.10, "exit_reason": "target_hit"},
        {"prediction_id": "p2", "pnl": -50.0, "ret": -0.05, "exit_reason": "stop_hit"},
        {
            "prediction_id": "p3",
            "pnl": 30.0,
            "ret": 0.03,
            "exit_reason": "horizon_exit",
        },
    ]
    s = review.win_stats(realized)
    assert s["n"] == 3
    assert s["win_rate"] == pytest.approx(2 / 3)
    assert s["by_reason"]["stop_hit"]["n"] == 1
    assert s["by_reason"]["target_hit"]["total_pnl"] == pytest.approx(100.0)
