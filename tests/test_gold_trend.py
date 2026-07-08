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


def test_momentum_subscore_bands():
    assert gt._momentum_subscore(25) == 100
    assert gt._momentum_subscore(40) == 80
    assert gt._momentum_subscore(50) == 60
    assert gt._momentum_subscore(65) == 40
    assert gt._momentum_subscore(80) == 15


def test_trend_subscore_uptrend_rising():
    assert gt._trend_subscore(110, 100, 90, True) == 100
    assert gt._trend_subscore(110, 100, 90, False) == 60
    assert gt._trend_subscore(80, 100, 90, False) == 30
    # degraded (no ma200)
    assert gt._trend_subscore(110, 100, None, False) == 60
    assert gt._trend_subscore(90, 100, None, False) == 30


def test_compute_technical_healthy_pullback_in_uptrend():
    # Rising series then a mild pullback -> above ma200, healthy drawdown, mid RSI.
    closes = [100 + i for i in range(260)]  # 100..359 rising
    closes += [359 * 0.90]  # -10% pullback tick
    tech = gt.compute_technical(closes)
    assert tech["ma200"] is not None
    assert -20 < tech["drawdown_pct"] < -5
    assert 0 <= tech["score"] <= 100
    assert tech["label"] in {"양호", "보통", "비권장"}
