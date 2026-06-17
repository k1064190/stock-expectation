"""Tests for portfolio exit-action rules.

Mirrors test_evaluator.py: synthetic Position objects plus injected
metrics / atr / prediction dicts. Fully offline and deterministic — no
network, no DB.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from portfolio.models import Position
from portfolio.exit_manager import compute_exit_actions

# A long, healthy uptrend stack used as the "everything intact" baseline.
_HEALTHY_METRICS = {
    "current_price": 110.0,
    "ma20": 105.0,
    "ma50": 100.0,
    "ma200": 90.0,
    "rsi14": 55.0,
    "overextension_level": "NONE",
    # 22-bar swing high for the chandelier high-watermark.
    "swing_high_22": 112.0,
}


def _pos(ticker="NVDA", qty=10, avg=100.0):
    """Build a single Position with sane cost basis defaults."""
    return Position(
        portfolio_id="pf_us",
        ticker=ticker,
        quantity=qty,
        avg_price=avg,
        total_cost=qty * avg,
        realized_pnl=0.0,
    )


def _pred(
    direction="BULL",
    status="OPEN",
    target=130.0,
    stop=92.0,
    created_at="2026-06-01T00:00:00+00:00",
    pid="p1",
):
    """Build a linked-prediction dict in the shape compute_exit_actions expects."""
    return {
        "id": pid,
        "direction": direction,
        "status": status,
        "target_price": target,
        "stop_price": stop,
        "created_at": created_at,
    }


def _run(positions, prices, metrics=None, atr=None, preds=None):
    """Thin wrapper to call compute_exit_actions with per-ticker dicts."""
    return compute_exit_actions(
        positions,
        current_prices=prices,
        metrics_by_ticker=metrics or {},
        atr_by_ticker=atr or {},
        open_predictions_by_ticker=preds or {},
    )


def _action(result, ticker="NVDA"):
    """Pluck the single action entry for a ticker."""
    for a in result["actions"]:
        if a["ticker"] == ticker:
            return a
    raise AssertionError(f"no action for {ticker}")


# --------------------------------------------------------------------------- #
# Never-raises contract + shape
# --------------------------------------------------------------------------- #
def test_returns_one_entry_per_position():
    result = _run([_pos("NVDA"), _pos("AMD")], {"NVDA": 110.0, "AMD": 110.0})
    assert len(result["actions"]) == 2
    tickers = {a["ticker"] for a in result["actions"]}
    assert tickers == {"NVDA", "AMD"}


def test_action_entry_has_all_fields():
    result = _run(
        [_pos()],
        {"NVDA": 110.0},
        _HEALTHY_METRICS_BY(),
        {"NVDA": 4.0},
        {"NVDA": [_pred()]},
    )
    a = _action(result)
    for key in (
        "ticker",
        "qty",
        "avg_price",
        "current_price",
        "pnl_pct",
        "action",
        "triggered_rules",
        "atr",
        "trailing_stop",
        "ma_status",
        "overextension_level",
        "linked_prediction",
        "reason_ko_hint",
    ):
        assert key in a, f"missing field: {key}"
    assert a["action"] in {"EXIT", "TRIM", "ADD", "WATCH", "HOLD"}


def test_never_raises_on_empty_everything():
    # No prices, no metrics, no preds — must degrade, not explode.
    result = _run([_pos()], {})
    a = _action(result)
    assert a["action"] == "WATCH"


# --------------------------------------------------------------------------- #
# RULE 1 — EXIT
# --------------------------------------------------------------------------- #
def test_exit_below_linked_stop():
    result = _run(
        [_pos()],
        {"NVDA": 91.0},
        _HEALTHY_METRICS_BY(),
        {"NVDA": 4.0},
        {"NVDA": [_pred(stop=92.0)]},
    )
    a = _action(result)
    assert a["action"] == "EXIT"
    assert any("stop" in r.lower() for r in a["triggered_rules"])


def test_exit_on_prediction_miss():
    result = _run(
        [_pos()],
        {"NVDA": 110.0},
        _HEALTHY_METRICS_BY(),
        {"NVDA": 4.0},
        {"NVDA": [_pred(status="MISS")]},
    )
    a = _action(result)
    assert a["action"] == "EXIT"
    assert any("MISS" in r for r in a["triggered_rules"])


def test_exit_below_ma200():
    metrics = _HEALTHY_METRICS_BY()
    metrics["NVDA"]["ma200"] = 115.0  # price 110 < MA200
    result = _run(
        [_pos()], {"NVDA": 110.0}, metrics, {"NVDA": 4.0}, {"NVDA": [_pred(stop=80.0)]}
    )
    a = _action(result)
    assert a["action"] == "EXIT"
    assert any("MA200" in r for r in a["triggered_rules"])


def test_exit_below_chandelier_trailing_stop():
    # swing_high 112, atr 4, mult 3 → trailing = 112 - 12 = 100. Price 99 < 100.
    metrics = _HEALTHY_METRICS_BY()
    metrics["NVDA"]["ma200"] = 50.0  # keep MA200 from firing
    result = _run(
        [_pos()], {"NVDA": 99.0}, metrics, {"NVDA": 4.0}, {"NVDA": [_pred(stop=40.0)]}
    )
    a = _action(result)
    assert a["action"] == "EXIT"
    assert any(
        "trailing" in r.lower() or "chandelier" in r.lower()
        for r in a["triggered_rules"]
    )
    assert a["trailing_stop"] == pytest.approx(100.0)


# --------------------------------------------------------------------------- #
# RULE 2 — TRIM
# --------------------------------------------------------------------------- #
def test_trim_on_rr_target():
    # avg 100, stop 90 → risk per share 10. price 120 → R = 20/10 = 2.0 >= tp_rr.
    # ATR 10, swing_high 120, mult 3 → trailing = 120 - 30 = 90 == linked stop,
    # so effective_stop stays at 90 (does not ratchet above cost).
    metrics = _HEALTHY_METRICS_BY()
    metrics["NVDA"]["ma200"] = 50.0
    metrics["NVDA"]["swing_high_22"] = 120.0
    result = _run(
        [_pos(avg=100.0)],
        {"NVDA": 120.0},
        metrics,
        {"NVDA": 10.0},
        {"NVDA": [_pred(stop=90.0, target=150.0)]},
    )
    a = _action(result)
    assert a["action"] == "TRIM"
    assert any(
        "R:R" in r or "rr" in r.lower() or "target" in r.lower()
        for r in a["triggered_rules"]
    )


def test_trim_on_extreme_overextension_with_big_gain():
    metrics = _HEALTHY_METRICS_BY()
    metrics["NVDA"]["ma200"] = 50.0
    metrics["NVDA"]["overextension_level"] = "EXTREME"
    metrics["NVDA"]["swing_high_22"] = 126.0
    # price 125, avg 100 → +25% > 20%; EXTREME overextension.
    result = _run(
        [_pos(avg=100.0)],
        {"NVDA": 125.0},
        metrics,
        {"NVDA": 1.0},
        {"NVDA": [_pred(stop=10.0, target=999.0)]},
    )
    a = _action(result)
    assert a["action"] == "TRIM"
    assert any("EXTREME" in r or "overext" in r.lower() for r in a["triggered_rules"])


def test_trim_on_ma_stack_break_while_profitable():
    # MA20 < MA50 (stack broke) but still net profitable, MA200 intact.
    metrics = {
        "NVDA": {
            "current_price": 108.0,
            "ma20": 100.0,
            "ma50": 105.0,  # ma20 < ma50 → stack break
            "ma200": 90.0,
            "rsi14": 50.0,
            "overextension_level": "NONE",
            "swing_high_22": 109.0,  # atr 1, mult 3 → trailing 106 < price 108
        }
    }
    result = _run(
        [_pos(avg=100.0)],
        {"NVDA": 108.0},
        metrics,
        {"NVDA": 1.0},
        {"NVDA": [_pred(stop=10.0, target=999.0)]},
    )
    a = _action(result)
    assert a["action"] == "TRIM"
    assert any("MA-stack" in r or "stack" in r.lower() for r in a["triggered_rules"])


# --------------------------------------------------------------------------- #
# RULE 3 — ADD
# --------------------------------------------------------------------------- #
def test_add_when_trend_intact_and_modest_gain():
    # NONE overext, RSI 55, MA20>MA50>MA200, +10% gain, fresh BULL pred.
    result = _run(
        [_pos(avg=100.0)],
        {"NVDA": 110.0},
        _HEALTHY_METRICS_BY(),
        {"NVDA": 2.0},
        {"NVDA": [_pred(direction="BULL", stop=80.0)]},
    )
    a = _action(result)
    assert a["action"] == "ADD"


def test_no_add_when_bear_prediction_contradicts():
    result = _run(
        [_pos(avg=100.0)],
        {"NVDA": 110.0},
        _HEALTHY_METRICS_BY(),
        {"NVDA": 2.0},
        {"NVDA": [_pred(direction="BEAR", stop=80.0)]},
    )
    a = _action(result)
    assert a["action"] != "ADD"


def test_no_add_when_rsi_too_hot():
    metrics = _HEALTHY_METRICS_BY()
    metrics["NVDA"]["rsi14"] = 80.0  # outside 45-65 add band
    result = _run(
        [_pos(avg=100.0)],
        {"NVDA": 110.0},
        metrics,
        {"NVDA": 2.0},
        {"NVDA": [_pred(stop=80.0)]},
    )
    a = _action(result)
    assert a["action"] != "ADD"


# --------------------------------------------------------------------------- #
# RULE 4 — WATCH (mixed / insufficient)
# --------------------------------------------------------------------------- #
def test_watch_when_metrics_missing():
    # Have price + pred but no metrics/atr → trend rules can't evaluate.
    result = _run([_pos()], {"NVDA": 110.0}, {}, {}, {"NVDA": [_pred(stop=80.0)]})
    a = _action(result)
    assert a["action"] == "WATCH"


def test_watch_when_no_price():
    result = _run(
        [_pos()], {}, _HEALTHY_METRICS_BY(), {"NVDA": 4.0}, {"NVDA": [_pred()]}
    )
    a = _action(result)
    assert a["action"] == "WATCH"
    assert a["current_price"] is None


def test_no_linked_prediction_is_non_blocking():
    # No prediction at all: still evaluates trend rules, links None.
    result = _run(
        [_pos(avg=100.0)], {"NVDA": 110.0}, _HEALTHY_METRICS_BY(), {"NVDA": 2.0}, {}
    )
    a = _action(result)
    assert a["linked_prediction"] is None
    assert a["action"] in {"ADD", "HOLD", "WATCH"}


# --------------------------------------------------------------------------- #
# RULE 5 — HOLD (trend intact, nothing triggered)
# --------------------------------------------------------------------------- #
def test_hold_when_trend_intact_but_overextended():
    # ELEVATED overextension blocks ADD, nothing else triggers → HOLD.
    metrics = _HEALTHY_METRICS_BY()
    metrics["NVDA"]["overextension_level"] = "ELEVATED"
    result = _run(
        [_pos(avg=100.0)],
        {"NVDA": 110.0},
        metrics,
        {"NVDA": 2.0},
        {"NVDA": [_pred(stop=80.0)]},
    )
    a = _action(result)
    assert a["action"] == "HOLD"


# --------------------------------------------------------------------------- #
# Precedence — hardest-risk-off first
# --------------------------------------------------------------------------- #
def test_exit_beats_trim_when_both_apply():
    # R:R hit (would TRIM) AND price below MA200 (EXIT) → EXIT wins.
    metrics = _HEALTHY_METRICS_BY()
    metrics["NVDA"]["ma200"] = 125.0  # price 120 < MA200 → EXIT
    result = _run(
        [_pos(avg=100.0)],
        {"NVDA": 120.0},
        metrics,
        {"NVDA": 2.0},
        {"NVDA": [_pred(stop=90.0, target=150.0)]},
    )
    a = _action(result)
    assert a["action"] == "EXIT"


def test_trim_beats_add_when_both_apply():
    # Healthy add conditions but R:R target also hit → TRIM wins.
    # price 112, avg 100, linked stop 94. ATR 6, swing_high 112, mult 3 →
    # trailing = 112 - 18 = 94 == linked stop, so effective_stop = 94 and
    # R = (112-100)/(100-94) = 2.0 → TRIM band. RSI/overext/stack add-friendly.
    metrics = _HEALTHY_METRICS_BY()
    metrics["NVDA"]["ma200"] = 50.0
    metrics["NVDA"]["swing_high_22"] = 112.0
    result = _run(
        [_pos(avg=100.0)],
        {"NVDA": 112.0},
        metrics,
        {"NVDA": 6.0},
        {"NVDA": [_pred(stop=94.0, target=150.0)]},
    )
    a = _action(result)
    assert a["action"] == "TRIM"


def test_effective_stop_uses_max_of_linked_and_trailing():
    # linked stop 90, trailing = swing 121 - 3*2 = 115. effective = max(90,115)=115.
    # avg 100, price 130 → R = 30 / (100-115) is NEGATIVE → no TRIM by R:R...
    # but price 130 > trailing 115 so no EXIT. Verify effective stop math
    # surfaces in trailing_stop and rr_progress reflects the larger stop.
    metrics = _HEALTHY_METRICS_BY()
    metrics["NVDA"]["ma200"] = 50.0
    metrics["NVDA"]["swing_high_22"] = 121.0
    result = _run(
        [_pos(avg=100.0)],
        {"NVDA": 130.0},
        metrics,
        {"NVDA": 2.0},
        {"NVDA": [_pred(stop=90.0, target=160.0)]},
    )
    a = _action(result)
    assert a["trailing_stop"] == pytest.approx(115.0)


def test_linked_prediction_picks_latest_by_created_at():
    preds = [
        _pred(pid="old", created_at="2026-01-01T00:00:00+00:00", stop=80.0),
        _pred(pid="new", created_at="2026-06-10T00:00:00+00:00", stop=85.0),
    ]
    result = _run(
        [_pos(avg=100.0)],
        {"NVDA": 110.0},
        _HEALTHY_METRICS_BY(),
        {"NVDA": 2.0},
        {"NVDA": preds},
    )
    a = _action(result)
    assert a["linked_prediction"]["id"] == "new"


def _HEALTHY_METRICS_BY():
    """Fresh copy of the healthy-stack metrics keyed by NVDA (deep enough)."""
    return {"NVDA": dict(_HEALTHY_METRICS)}
