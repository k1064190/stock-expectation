"""In-process tests for the ``catalyst timeline`` CLI availability semantics.

Covers ``stock_cli.cmd_catalyst_timeline``'s partial-failure behavior (fetchers
monkeypatched — no network): ``gate_unavailable`` must match the
``evaluate_gate`` semantics and flip to true only when EVERY requested
calendar failed; partial success keeps the timeline live with a note.
"""

import importlib.util
import json
import sys
import types
from datetime import date, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# stock_cli.py lives at the project root and sets up the mcp-* sys.path inserts
# at import time; load it by file path so the test doesn't depend on cwd.
_spec = importlib.util.spec_from_file_location(
    "stock_cli", PROJECT_ROOT / "stock_cli.py"
)
stock_cli = importlib.util.module_from_spec(_spec)
sys.modules["stock_cli"] = stock_cli
_spec.loader.exec_module(stock_cli)


def _timeline_args(**overrides):
    """Build a parsed-args namespace for ``cmd_catalyst_timeline``."""
    defaults = {"tickers": "NVDA", "market": "US", "days": 14, "include_macro": True}
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _run_timeline(capsys, args):
    """Invoke the command and return (exit_code, parsed JSON output)."""
    rc = stock_cli.cmd_catalyst_timeline(args)
    return rc, json.loads(capsys.readouterr().out)


def _boom(*_args, **_kwargs):
    raise RuntimeError("simulated fetch failure")


@pytest.fixture
def fmp_key(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "fake-key")


def test_timeline_macro_fail_earnings_ok_not_unavailable(monkeypatch, capsys, fmp_key):
    """Macro 402 with a working earnings calendar → live timeline + note."""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    monkeypatch.setattr(
        stock_cli,
        "_fetch_earnings_window",
        lambda _f, _t, _m: [{"symbol": "NVDA", "date": tomorrow, "time": "amc"}],
    )
    monkeypatch.setattr(stock_cli, "_fetch_macro_window", _boom)

    rc, out = _run_timeline(capsys, _timeline_args())
    assert rc == 0
    assert out["gate_unavailable"] is False
    assert len(out["by_ticker"]["NVDA"]) == 1
    assert "macro calendar fetch failed" in out["note"]


def test_timeline_earnings_fail_macro_ok_not_unavailable(monkeypatch, capsys, fmp_key):
    """Earnings 403 with working macro events → live timeline + note (the
    fixed Codex finding: valid macro data must not be marked unavailable)."""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    monkeypatch.setattr(stock_cli, "_fetch_earnings_window", _boom)
    monkeypatch.setattr(
        stock_cli,
        "_fetch_macro_window",
        lambda _f, _t: [
            {"date": tomorrow, "event": "CPI", "impact": "High", "country": "US"}
        ],
    )

    rc, out = _run_timeline(capsys, _timeline_args())
    assert rc == 0
    assert out["gate_unavailable"] is False
    assert out["by_ticker"]["NVDA"] == []
    assert len(out["market_wide"]) == 1
    assert "earnings calendar fetch failed" in out["note"]


def test_timeline_all_requested_calendars_fail_unavailable(
    monkeypatch, capsys, fmp_key
):
    """Every requested calendar down → gate_unavailable, still exit 0."""
    monkeypatch.setattr(stock_cli, "_fetch_earnings_window", _boom)
    monkeypatch.setattr(stock_cli, "_fetch_macro_window", _boom)

    rc, out = _run_timeline(capsys, _timeline_args())
    assert rc == 0
    assert out["gate_unavailable"] is True
    assert "earnings calendar fetch failed" in out["note"]
    assert "macro calendar fetch failed" in out["note"]


def test_timeline_earnings_fail_without_macro_requested_unavailable(
    monkeypatch, capsys, fmp_key
):
    """US without --include-macro: earnings is the only requested calendar,
    so its failure alone still means unavailable (pre-existing behavior)."""
    monkeypatch.setattr(stock_cli, "_fetch_earnings_window", _boom)

    rc, out = _run_timeline(capsys, _timeline_args(include_macro=False))
    assert rc == 0
    assert out["gate_unavailable"] is True
    assert "earnings calendar fetch failed" in out["note"]


def test_gate_json_includes_availability_fields(monkeypatch, capsys, fmp_key):
    """``catalyst gate`` JSON must carry earnings_source / macro_available /
    notes (the SKILL.md contract) so consumers like the expect skill can tell
    "feed down" from "no events". Exercises the real evaluate_gate → asdict
    pass-through with the events-module fetchers monkeypatched (no network)."""
    events = sys.modules["events"]  # loaded via stock_cli's sys.path insert

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    monkeypatch.setattr(
        events,
        "_fetch_earnings_window",
        lambda _f, _t, _m: [{"symbol": "NVDA", "date": tomorrow, "time": "amc"}],
    )
    monkeypatch.setattr(events, "_fetch_macro_window", lambda _f, _t: [])

    args = types.SimpleNamespace(tickers="NVDA", market="US")
    rc = stock_cli.cmd_catalyst_gate(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["earnings_source"] == "fmp"
    assert out["macro_available"] is True
    assert out["gate_unavailable"] is False
    assert out["notes"] == []
    # tomorrow is always <= 1 trading day away → WATCH cap
    assert out["by_ticker"]["NVDA"]["cap_label"] == "WATCH"
