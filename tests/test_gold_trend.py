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
