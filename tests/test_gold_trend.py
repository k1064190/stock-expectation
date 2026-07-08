import pathlib
import sys
import textwrap

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scheduler import gold_trend as gt  # noqa: E402


def test_load_config_missing_file_returns_defaults(tmp_path):
    cfg = gt.load_config(tmp_path / "does_not_exist.yaml")
    assert cfg == gt.DEFAULT_CONFIG


def test_load_config_fills_missing_keys(tmp_path):
    p = tmp_path / "factors.yaml"
    p.write_text(
        textwrap.dedent(
            """
        central_bank:
          trailing_4q_tonnes: 700
        risk_off: true
    """
        )
    )
    cfg = gt.load_config(p)
    assert cfg["central_bank"]["trailing_4q_tonnes"] == 700
    # unspecified sub-key falls back to default
    assert (
        cfg["central_bank"]["baseline_tonnes"]
        == gt.DEFAULT_CONFIG["central_bank"]["baseline_tonnes"]
    )
    assert cfg["risk_off"] is True
    # untouched section falls back entirely
    assert cfg["scoring"]["weights"] == gt.DEFAULT_CONFIG["scoring"]["weights"]


def test_load_config_position_cost_defaults_none(tmp_path):
    cfg = gt.load_config(tmp_path / "nope.yaml")
    assert cfg["position"]["avg_cost_krw_per_g"] is None


def test_rsi_all_gains_is_100():
    closes = [float(i) for i in range(1, 40)]  # strictly increasing
    assert gt._rsi(closes) == 100.0


def test_sma_and_none_when_short():
    assert gt._sma([1, 2, 3, 4], 2) == 3.5
    assert gt._sma([1, 2], 5) is None


def test_pullback_subscore_bands():
    assert gt._pullback_subscore(-2) == 40  # near high
    assert gt._pullback_subscore(-12) == 100  # healthy zone
    assert gt._pullback_subscore(-27) == 70  # deep
    assert gt._pullback_subscore(-40) == 30  # broken


def test_pullback_subscore_boundaries():
    # strict > semantics: the boundary value itself falls into the *next* (lower) band
    assert gt._pullback_subscore(-5) == 100  # -5 > -5 is False -> not the >-5 band
    assert gt._pullback_subscore(-20) == 70  # -20 > -20 is False -> not the >-20 band
    assert gt._pullback_subscore(-35) == 30  # -35 > -35 is False -> falls to else


def test_momentum_subscore_bands():
    assert gt._momentum_subscore(25) == 100
    assert gt._momentum_subscore(40) == 80
    assert gt._momentum_subscore(50) == 60
    assert gt._momentum_subscore(65) == 40
    assert gt._momentum_subscore(80) == 15


def test_momentum_subscore_boundaries():
    # strict < semantics: the boundary value itself falls into the *next* band
    assert gt._momentum_subscore(30) == 80  # 30 < 30 is False
    assert gt._momentum_subscore(45) == 60  # 45 < 45 is False
    assert gt._momentum_subscore(60) == 40  # 60 < 60 is False
    assert gt._momentum_subscore(70) == 40  # 70 <= 70 is True
    assert gt._momentum_subscore(70.0001) == 15  # 70.0001 <= 70 is False


def test_trend_subscore_uptrend_rising():
    assert gt._trend_subscore(110, 100, 90, True) == 100
    assert gt._trend_subscore(110, 100, 90, False) == 60
    assert gt._trend_subscore(80, 100, 90, False) == 30
    # degraded (no ma200)
    assert gt._trend_subscore(110, 100, None, False) == 60
    assert gt._trend_subscore(90, 100, None, False) == 30


def test_trend_subscore_price_equals_ma200_boundary():
    assert gt._trend_subscore(100, 90, 100, True) == 100  # price >= ma200 and rising
    assert gt._trend_subscore(100, 90, 100, False) == 60  # price >= ma200, not rising


def test_compute_technical_healthy_pullback_in_uptrend():
    # Rising series then a mild pullback -> above ma200, healthy drawdown, mid RSI.
    closes = [100 + i for i in range(260)]  # 100..359 rising
    closes += [359 * 0.90]  # -10% pullback tick
    tech = gt.compute_technical(closes)
    assert tech["ma200"] is not None
    assert -20 < tech["drawdown_pct"] < -5
    # deterministic exact values: trend=100 (price>=ma200, rising), pullback=100
    # (drawdown -10 is >-20), momentum=100 (RSI well under 30 after a -10% tick)
    assert tech["trend"] == 100
    assert tech["pullback"] == 100
    assert tech["momentum"] == 100
    assert tech["score"] == 100.0
    assert tech["label"] == "양호"
