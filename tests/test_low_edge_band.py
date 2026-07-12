"""Tests for the low-edge-band tag on LIVE BULL creates.

The 0.60-0.70 raw-confidence band shows negative realized edge in both paper
books; predictions there are still logged (training rows keep flowing for the
learned blend) but get ``components.low_edge_band = true`` so capital-touching
consumers (paper trading) can skip them.
"""

import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "stock_cli_leb", PROJECT_ROOT / "stock_cli.py"
)
stock_cli = importlib.util.module_from_spec(_spec)
sys.modules["stock_cli_leb"] = stock_cli
_spec.loader.exec_module(stock_cli)


@pytest.fixture
def use_temp_db(monkeypatch):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    orig = stock_cli.get_connection
    monkeypatch.setattr(stock_cli, "get_connection", lambda: orig(db_path))
    yield db_path
    try:
        db_path.unlink()
    except OSError:
        pass


def _make_args(**overrides):
    base = dict(
        ticker="NVDA",
        market="US",
        direction="BULL",
        confidence=0.65,
        timeframe="1W",
        reasoning="test",
        entry_price=100.0,
        signals="",
        source="LIVE",
        target_price=None,
        stop_price=None,
        analysis_group_id=None,
        components=None,
        recalibrate=False,
        no_gate_refresh=True,  # keep these tests offline
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _run_create(args, capsys):
    rc = stock_cli.cmd_predict_create(args)
    return rc, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("conf", [0.60, 0.65, 0.70])
def test_live_bull_in_band_gets_tagged(use_temp_db, capsys, conf):
    rc, out = _run_create(
        _make_args(ticker=f"T{int(conf * 100)}", confidence=conf), capsys
    )

    assert rc == 0
    assert out["components"]["low_edge_band"] is True


@pytest.mark.parametrize("conf", [0.55, 0.75])
def test_live_bull_outside_band_not_tagged(use_temp_db, capsys, conf):
    rc, out = _run_create(
        _make_args(ticker=f"U{int(conf * 100)}", confidence=conf), capsys
    )

    assert rc == 0
    assert out.get("components") is None


def test_interactive_not_tagged(use_temp_db, capsys):
    rc, out = _run_create(_make_args(source="INTERACTIVE"), capsys)

    assert rc == 0
    assert out.get("components") is None


def test_existing_components_preserved(use_temp_db, capsys):
    comps = json.dumps({"algo": 7.0, "overextension": "NONE", "return_1m": 0.03})
    rc, out = _run_create(_make_args(ticker="AAPL", components=comps), capsys)

    assert rc == 0
    assert out["components"]["low_edge_band"] is True
    assert out["components"]["algo"] == 7.0
