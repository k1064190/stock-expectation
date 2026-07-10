"""In-process tests for the ``etf list`` / ``etf info`` CLI commands.

Covers filtering (leverage exclusion, --min-aum), AUM-descending order, the
universe/detail merge in ``etf info``, and the unknown-code error path — the
etf_kr fetchers are monkeypatched (no network).
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# stock_cli.py lives at the project root and sets up the mcp-* sys.path inserts
# at import time; load it by file path so the test doesn't depend on cwd.
_spec = importlib.util.spec_from_file_location(
    "stock_cli", PROJECT_ROOT / "stock_cli.py"
)
stock_cli = importlib.util.module_from_spec(_spec)
sys.modules["stock_cli"] = stock_cli
_spec.loader.exec_module(stock_cli)

import etf_kr  # noqa: E402 — resolvable via stock_cli's sys.path insert

PAYLOAD = {
    "result": {
        "etfItemList": [
            {
                "itemcode": "069500",
                "itemname": "KODEX 200",
                "nowVal": 123895,
                "nav": 123899.0,
                "threeMonthEarnRate": 35.4985,
                "quant": 12813727,
                "amonut": 1563670,
                "marketSum": 269224,
                "etfTabCode": 1,
            },
            {
                "itemcode": "360750",
                "itemname": "TIGER 미국S&P500",
                "nowVal": 28252,
                "nav": 28276.0,
                "threeMonthEarnRate": 12.8732,
                "quant": 94872086,
                "amonut": 2677236,
                "marketSum": 202101,
                "etfTabCode": 4,
            },
            {
                "itemcode": "371460",
                "itemname": "TIGER 차이나전기차SOLACTIVE(H)",
                "nowVal": 10000,
                "nav": 10010.0,
                "threeMonthEarnRate": 1.0,
                "quant": 10,
                "amonut": 5,
                "marketSum": 30,
                "etfTabCode": 4,
            },
            {
                "itemcode": "122630",
                "itemname": "KODEX 레버리지",
                "nowVal": 20000,
                "nav": None,
                "threeMonthEarnRate": 60.0,
                "quant": 100,
                "amonut": 50,
                "marketSum": 20000,
                "etfTabCode": 3,
            },
        ]
    }
}

ROWS, _ = etf_kr._parse_universe(PAYLOAD)


def _patch_universe(monkeypatch):
    monkeypatch.setattr(etf_kr, "get_etf_universe", lambda **kw: (ROWS, "live", []))


def _list_args(**overrides):
    """Build a parsed-args namespace for ``cmd_etf_list``."""
    defaults = {
        "asset_class": None,
        "min_aum": None,
        "include_leverage": False,
        "limit": None,
    }
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _run(capsys, fn, args):
    """Invoke the command and return (exit_code, parsed JSON output)."""
    rc = fn(args)
    return rc, json.loads(capsys.readouterr().out)


def test_list_excludes_leverage_by_default(monkeypatch, capsys):
    _patch_universe(monkeypatch)
    rc, out = _run(capsys, stock_cli.cmd_etf_list, _list_args())
    codes = [e["code"] for e in out["etfs"]]
    assert rc == 0
    assert "122630" not in codes
    assert out["count"] == 3
    assert out["source"] == "live"
    # sorted by AUM (억원) descending
    assert codes == ["069500", "360750", "371460"]


def test_list_include_leverage_flag(monkeypatch, capsys):
    _patch_universe(monkeypatch)
    rc, out = _run(capsys, stock_cli.cmd_etf_list, _list_args(include_leverage=True))
    assert rc == 0
    assert "122630" in [e["code"] for e in out["etfs"]]


def test_list_min_aum_filter(monkeypatch, capsys):
    _patch_universe(monkeypatch)
    rc, out = _run(capsys, stock_cli.cmd_etf_list, _list_args(min_aum=100000))
    assert rc == 0
    assert [e["code"] for e in out["etfs"]] == ["069500", "360750"]


def test_list_asset_class_filter(monkeypatch, capsys):
    _patch_universe(monkeypatch)
    rc, out = _run(
        capsys, stock_cli.cmd_etf_list, _list_args(asset_class="overseas_equity")
    )
    assert rc == 0
    assert [e["code"] for e in out["etfs"]] == ["360750", "371460"]


def test_info_merges_universe_and_detail(monkeypatch, capsys):
    _patch_universe(monkeypatch)
    monkeypatch.setattr(
        etf_kr,
        "fetch_etf_detail",
        lambda code, **kw: {
            "fund_pay_pct": 0.007,
            "base_index": "S&P 500",
            "notes": [],
        },
    )
    args = types.SimpleNamespace(code="360750")
    rc, out = _run(capsys, stock_cli.cmd_etf_info, args)
    assert rc == 0
    assert out["name"] == "TIGER 미국S&P500"
    assert out["fund_pay_pct"] == 0.007
    assert out["base_index"] == "S&P 500"


def test_info_zero_pads_short_code(monkeypatch, capsys):
    """``etf info 69500`` must find KODEX 200 (069500) — lookup AND detail
    fetch use the zero-padded code."""
    _patch_universe(monkeypatch)
    seen = {}

    def fake_detail(code, **kw):
        seen["code"] = code
        return {"fund_pay_pct": 0.15, "base_index": "KOSPI 200", "notes": []}

    monkeypatch.setattr(etf_kr, "fetch_etf_detail", fake_detail)
    args = types.SimpleNamespace(code="69500")
    rc, out = _run(capsys, stock_cli.cmd_etf_info, args)
    assert rc == 0
    assert out["name"] == "KODEX 200"
    assert seen["code"] == "069500"


def test_info_unknown_code_errors(monkeypatch, capsys):
    _patch_universe(monkeypatch)
    args = types.SimpleNamespace(code="999999")
    rc, out = _run(capsys, stock_cli.cmd_etf_info, args)
    assert rc != 0
    assert "error" in out
