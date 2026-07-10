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


def test_info_uppercases_alnum_code(monkeypatch, capsys):
    """``etf info 193t0`` must find the post-2024 alphanumeric KRX code
    0193T0 — input is zero-padded AND uppercased."""
    alnum_payload = {
        "result": {
            "etfItemList": [
                {
                    "itemcode": "0193T0",
                    "itemname": "KODEX SK하이닉스단일종목레버리지",
                    "nowVal": 23095,
                    "nav": 23067.0,
                    "threeMonthEarnRate": None,
                    "amonut": 3108407,
                    "marketSum": 53575,
                    "etfTabCode": 2,
                }
            ]
        }
    }
    rows, _ = etf_kr._parse_universe(alnum_payload)
    monkeypatch.setattr(etf_kr, "get_etf_universe", lambda **kw: (rows, "live", []))
    seen = {}

    def fake_detail(code, **kw):
        seen["code"] = code
        return {"fund_pay_pct": 0.4, "base_index": "SK하이닉스", "notes": []}

    monkeypatch.setattr(etf_kr, "fetch_etf_detail", fake_detail)
    args = types.SimpleNamespace(code="193t0")
    rc, out = _run(capsys, stock_cli.cmd_etf_info, args)
    assert rc == 0
    assert out["code"] == "0193T0"
    assert seen["code"] == "0193T0"


def test_list_universe_unavailable_is_error_json(monkeypatch, capsys):
    """Both live and cache down → controlled error JSON + nonzero exit, never
    a raw traceback."""
    monkeypatch.setattr(
        etf_kr,
        "get_etf_universe",
        lambda **kw: (_ for _ in ()).throw(
            etf_kr.EtfDataUnavailable("etf universe cache corrupt: boom")
        ),
    )
    rc, out = _run(capsys, stock_cli.cmd_etf_list, _list_args())
    assert rc == 1
    assert "cache corrupt" in out["error"]


# --- etf compare (stage 27) -------------------------------------------------

import etf_score  # noqa: E402


def _mk_row(code, name, aum, value, dev, lev=False):
    return etf_kr.EtfInfo(
        code=code, name=name, price=10000.0, nav=10000.0, deviation_pct=dev,
        aum_100m_krw=aum, value_million_krw=value, ret_3m_pct=10.0, tab_code=4,
        asset_class="overseas_equity", tax_type="other_type", hedged=False,
        leveraged_or_inverse=lev,
    )


SNP_ROWS = [
    _mk_row("AAAAA1", "TIGER 미국S&P500", aum=202101, value=2677236, dev=0.05),
    _mk_row("BBBBB2", "KODEX 미국S&P500", aum=100000, value=900000, dev=0.10),
    _mk_row("CCCCC3", "ACE 미국S&P500", aum=50000, value=400000, dev=0.30),
    _mk_row("DDDDD4", "KODEX 미국S&P500레버리지", aum=30000, value=300000,
            dev=0.10, lev=True),
]
SNP_DETAILS = {
    "AAAAA1": {"fund_pay_pct": 0.07, "base_index": "S&P 500", "notes": []},
    "BBBBB2": {"fund_pay_pct": 0.0099, "base_index": "S&P 500", "notes": []},
    "CCCCC3": {"fund_pay_pct": None, "base_index": "S&P 500",
               "notes": ["fund fee unavailable"]},
    "DDDDD4": {"fund_pay_pct": 0.25, "base_index": "S&P 500 선물", "notes": []},
}


def _patch_compare(monkeypatch, details=SNP_DETAILS):
    monkeypatch.setattr(
        etf_kr, "get_etf_universe", lambda **kw: (SNP_ROWS, "live", [])
    )
    monkeypatch.setattr(
        etf_kr,
        "fetch_etf_detail",
        lambda code, **kw: details.get(
            code,
            {"fund_pay_pct": None, "base_index": None, "notes": ["detail failed"]},
        ),
    )


def _compare_args(**overrides):
    defaults = {"codes": None, "query": None, "include_leverage": False}
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def test_compare_codes_picks_best(monkeypatch, capsys):
    _patch_compare(monkeypatch)
    rc, out = _run(
        capsys, stock_cli.cmd_etf_compare, _compare_args(codes="AAAAA1,BBBBB2,CCCCC3")
    )
    assert rc == 0
    assert out["best"] == "BBBBB2"
    assert out["base_index_mismatch"] is False
    assert out["count"] == 3


def test_compare_query_space_insensitive_excludes_leverage(monkeypatch, capsys):
    _patch_compare(monkeypatch)
    rc, out = _run(
        capsys, stock_cli.cmd_etf_compare, _compare_args(query="s&p 500")
    )
    assert rc == 0
    codes = {s["code"] for s in out["scored"]}
    assert codes == {"AAAAA1", "BBBBB2", "CCCCC3"}  # DDDDD4 leveraged, excluded


def test_compare_mixed_index_flagged(monkeypatch, capsys):
    _patch_compare(monkeypatch)
    rc, out = _run(
        capsys,
        stock_cli.cmd_etf_compare,
        _compare_args(codes="AAAAA1,DDDDD4", include_leverage=True),
    )
    assert rc == 0
    assert out["base_index_mismatch"] is True
    assert any("base_index" in n or "base index" in n for n in out["notes"])


def test_compare_requires_exactly_one_selector(monkeypatch, capsys):
    _patch_compare(monkeypatch)
    rc, out = _run(capsys, stock_cli.cmd_etf_compare, _compare_args())
    assert rc == 1 and "error" in out
    rc, out = _run(
        capsys,
        stock_cli.cmd_etf_compare,
        _compare_args(codes="AAAAA1", query="s&p"),
    )
    assert rc == 1 and "error" in out


def test_compare_unknown_code_errors_with_code(monkeypatch, capsys):
    _patch_compare(monkeypatch)
    rc, out = _run(
        capsys, stock_cli.cmd_etf_compare, _compare_args(codes="AAAAA1,ZZZZZ9")
    )
    assert rc == 1
    assert "ZZZZZ9" in out["error"]


def test_compare_whitespace_codes_errors(monkeypatch, capsys):
    """`etf compare " "` must give a correct empty-code-list error, not the
    misleading "no ETFs matched query: None"."""
    _patch_compare(monkeypatch)
    rc, out = _run(capsys, stock_cli.cmd_etf_compare, _compare_args(codes=" "))
    assert rc == 1
    assert "code" in out["error"] and "query: None" not in out["error"]


def test_compare_duplicate_codes_deduplicated(monkeypatch, capsys):
    _patch_compare(monkeypatch)
    rc, out = _run(
        capsys, stock_cli.cmd_etf_compare, _compare_args(codes="AAAAA1,AAAAA1")
    )
    assert rc == 0
    assert out["count"] == 1
    assert [s["code"] for s in out["scored"]] == ["AAAAA1"]


def test_compare_query_include_leverage(monkeypatch, capsys):
    _patch_compare(monkeypatch)
    rc, out = _run(
        capsys,
        stock_cli.cmd_etf_compare,
        _compare_args(query="s&p 500", include_leverage=True),
    )
    assert rc == 0
    assert "DDDDD4" in {s["code"] for s in out["scored"]}
    assert out["count"] == 4


def test_compare_query_truncates_at_cap_with_note(monkeypatch, capsys):
    many = [
        _mk_row(f"MANY{i:02d}", f"KODEX 미국S&P500 {i}호", aum=1000 + i, value=100, dev=0.1)
        for i in range(20)
    ]
    monkeypatch.setattr(etf_kr, "get_etf_universe", lambda **kw: (many, "live", []))
    monkeypatch.setattr(
        etf_kr,
        "fetch_etf_detail",
        lambda code, **kw: {"fund_pay_pct": 0.1, "base_index": "I", "notes": []},
    )
    rc, out = _run(capsys, stock_cli.cmd_etf_compare, _compare_args(query="s&p500"))
    assert rc == 0
    assert out["count"] == stock_cli.MAX_COMPARE_CANDIDATES == 15
    assert any("comparing top 15 by AUM" in n for n in out["notes"])
    # AUM-desc: the 5 smallest (MANY00..MANY04) fell off.
    assert "MANY00" not in {s["code"] for s in out["scored"]}


def test_compare_query_no_match_errors(monkeypatch, capsys):
    _patch_compare(monkeypatch)
    rc, out = _run(
        capsys, stock_cli.cmd_etf_compare, _compare_args(query="nonexistent")
    )
    assert rc == 1
    assert "no ETFs matched" in out["error"]


def test_compare_blank_query_errors(monkeypatch, capsys):
    """`etf compare --query "   "` normalizes to "" which would match every
    name — must error out, not silently compare the top 15 by AUM."""
    _patch_compare(monkeypatch)
    rc, out = _run(capsys, stock_cli.cmd_etf_compare, _compare_args(query="   "))
    assert rc == 1
    assert "empty query" in out["error"]
    assert "scored" not in out
