"""In-process tests for the ``isa`` CLI group (init/status/allocate/rebalance/log).

Everything offline: tmp portfolio.db via a monkeypatched connection factory,
monkeypatched ETF universe (init code validation) and price provider.
"""

import importlib.util
import json
import sys
import types
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

import etf_kr  # noqa: E402 — resolvable via stock_cli's sys.path insert
from portfolio.db import add_transaction, create_portfolio, get_connection  # noqa: E402

# Universe rows for `isa init` code validation.
UNIVERSE = [
    etf_kr.EtfInfo(
        code=code,
        name=name,
        price=10000.0,
        nav=10000.0,
        deviation_pct=0.0,
        aum_100m_krw=1000,
        value_million_krw=1000,
        ret_3m_pct=1.0,
        tab_code=4,
        asset_class="overseas_equity",
        tax_type="other_type",
        hedged=False,
        leveraged_or_inverse=lev,
    )
    for code, name, lev in [
        ("360750", "TIGER 미국S&P500", False),
        ("114260", "KODEX 국고채3년", False),
        ("411060", "ACE KRX금현물", False),
        ("069500", "KODEX 200", False),
        ("122630", "KODEX 레버리지", True),
    ]
]

# Prices chosen so the fixture book is exactly 5.4M/3.6M (60/40).
PRICES = {"360750": 54_000.0, "114260": 72_000.0, "069500": 54_000.0}


class _StubProvider:
    def get_current_price(self, ticker):
        return PRICES.get(ticker)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Tmp portfolio.db + patched universe/provider. Returns the db factory."""
    db_path = tmp_path / "portfolio.db"

    def factory(*a, **kw):
        return get_connection(db_path)

    monkeypatch.setattr(stock_cli, "pf_get_connection", factory)
    monkeypatch.setattr(etf_kr, "get_etf_universe", lambda **kw: (UNIVERSE, "live", []))
    monkeypatch.setattr(stock_cli, "_get_provider", lambda market: _StubProvider())
    return factory


def _run(capsys, fn, args):
    rc = fn(args)
    return rc, json.loads(capsys.readouterr().out)


def _init_args(**overrides):
    defaults = {
        "allocation": "overseas_equity=60,bond=40",
        "map": "overseas_equity=360750,bond=114260",
        "note": None,
    }
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _do_init(capsys, **overrides):
    rc, out = _run(capsys, stock_cli.cmd_isa_init, _init_args(**overrides))
    assert rc == 0, out
    return out


def _seed_positions(factory):
    """KR portfolio named ISA holding 100x360750 + 50x114260 (5.4M/3.6M)."""
    conn = factory()
    pf = create_portfolio(conn, "KR", "ISA")
    add_transaction(
        conn,
        portfolio_id=pf.id,
        ticker="360750",
        side="BUY",
        quantity=100,
        price=50_000,
        currency="KRW",
        transacted_at="2026-01-02",
    )
    add_transaction(
        conn,
        portfolio_id=pf.id,
        ticker="114260",
        side="BUY",
        quantity=50,
        price=70_000,
        currency="KRW",
        transacted_at="2026-01-02",
    )
    conn.close()


# --- init --------------------------------------------------------------------


def test_init_happy_path(env, capsys):
    out = _do_init(capsys)
    assert out["allocation"] == {"overseas_equity": 60.0, "bond": 40.0}
    assert out["etf_map"] == {"overseas_equity": "360750", "bond": "114260"}


def test_init_rejects_bad_sum(env, capsys):
    rc, out = _run(
        capsys,
        stock_cli.cmd_isa_init,
        _init_args(allocation="overseas_equity=60,bond=30"),
    )
    assert rc == 1 and "sum" in out["error"]


def test_init_rejects_unknown_and_leveraged_codes(env, capsys):
    rc, out = _run(
        capsys,
        stock_cli.cmd_isa_init,
        _init_args(map="overseas_equity=999999,bond=114260"),
    )
    assert rc == 1 and "999999" in out["error"]
    rc, out = _run(
        capsys,
        stock_cli.cmd_isa_init,
        _init_args(map="overseas_equity=122630,bond=114260"),
    )
    assert rc == 1 and "122630" in out["error"]


def test_init_universe_down_proceeds_with_note(env, capsys, monkeypatch):
    monkeypatch.setattr(
        etf_kr,
        "get_etf_universe",
        lambda **kw: (_ for _ in ()).throw(etf_kr.EtfDataUnavailable("down")),
    )
    out = _do_init(capsys)
    assert any("code validation skipped" in n for n in out["notes"])


# --- status ------------------------------------------------------------------


def test_status_requires_target(env, capsys):
    rc, out = _run(capsys, stock_cli.cmd_isa_status, types.SimpleNamespace())
    assert rc == 1 and "isa init" in out["error"]


def test_status_requires_isa_portfolio(env, capsys):
    _do_init(capsys)
    rc, out = _run(capsys, stock_cli.cmd_isa_status, types.SimpleNamespace())
    assert rc == 1 and "portfolio create" in out["error"]


def test_status_weights_and_drift(env, capsys):
    _do_init(capsys)
    _seed_positions(env)
    rc, out = _run(capsys, stock_cli.cmd_isa_status, types.SimpleNamespace())
    assert rc == 0
    assert out["value_by_class"] == {
        "overseas_equity": 5_400_000.0,
        "bond": 3_600_000.0,
    }
    assert round(out["drift_pp"]["overseas_equity"], 1) == 0.0  # 60/40 target
    assert out["rebalance"]["needed"] is False


# --- allocate ----------------------------------------------------------------


def _alloc_args(**overrides):
    defaults = {"amount": 1_000_000, "tilt": None, "dry_run": False}
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def test_allocate_underweight_first(env, capsys):
    # 50/50 target against the 60/40 book → all to bond.
    _do_init(capsys, allocation="overseas_equity=50,bond=50")
    _seed_positions(env)
    rc, out = _run(capsys, stock_cli.cmd_isa_allocate, _alloc_args())
    assert rc == 0
    assert out["buys_by_class"] == {"overseas_equity": 0, "bond": 1_000_000}
    per_etf = {r["code"]: r for r in out["per_etf"]}
    assert per_etf["114260"]["buy_krw"] == 1_000_000
    assert per_etf["114260"]["est_shares"] == 13  # floor(1,000,000 / 72,000)
    assert out["decision_id"] is not None


def test_allocate_tilt_clamped_with_note(env, capsys):
    _do_init(capsys, allocation="overseas_equity=50,bond=50")
    _seed_positions(env)
    rc, out = _run(
        capsys,
        stock_cli.cmd_isa_allocate,
        _alloc_args(tilt="overseas_equity=+15,bond=-15"),
    )
    assert rc == 0
    assert out["buys_by_class"] == {"overseas_equity": 600_000, "bond": 400_000}
    assert any("clamped" in n for n in out["notes"])


def test_allocate_unmapped_ticker_warned_and_excluded(env, capsys):
    _do_init(capsys, allocation="overseas_equity=50,bond=50")
    _seed_positions(env)
    conn = env()
    pf_id = conn.execute("SELECT id FROM portfolios").fetchone()["id"]
    add_transaction(
        conn,
        portfolio_id=pf_id,
        ticker="999999",
        side="BUY",
        quantity=1,
        price=1_000,
        currency="KRW",
        transacted_at="2026-01-03",
    )
    conn.close()
    rc, out = _run(capsys, stock_cli.cmd_isa_allocate, _alloc_args())
    assert rc == 0
    assert any("999999" in n for n in out["notes"])
    # unmapped value excluded → class values unchanged → same waterfall
    assert out["buys_by_class"] == {"overseas_equity": 0, "bond": 1_000_000}


def test_allocate_dry_run_does_not_log(env, capsys):
    _do_init(capsys, allocation="overseas_equity=50,bond=50")
    _seed_positions(env)
    rc, out = _run(capsys, stock_cli.cmd_isa_allocate, _alloc_args(dry_run=True))
    assert rc == 0 and out["dry_run"] is True and out["decision_id"] is None
    rc, log_out = _run(capsys, stock_cli.cmd_isa_log, types.SimpleNamespace(limit=10))
    kinds = [d["kind"] for d in log_out["decisions"]]
    assert "contribution" not in kinds  # only the init target_change
    rc, out = _run(capsys, stock_cli.cmd_isa_allocate, _alloc_args())
    rc, log_out = _run(capsys, stock_cli.cmd_isa_log, types.SimpleNamespace(limit=10))
    kinds = [d["kind"] for d in log_out["decisions"]]
    assert kinds.count("contribution") == 1


# --- rebalance ---------------------------------------------------------------


def test_rebalance_breach_remedy_and_logged(env, capsys):
    # 50/50 target against the 60/40 book → both classes breach at ±10.
    _do_init(capsys, allocation="overseas_equity=50,bond=50")
    _seed_positions(env)
    rc, out = _run(capsys, stock_cli.cmd_isa_rebalance, types.SimpleNamespace())
    assert rc == 0
    assert out["needed"] is True
    assert {b["asset_class"] for b in out["breaches"]} == {"overseas_equity", "bond"}
    assert out["min_contribution_to_restore"] == 818_182
    rc, log_out = _run(capsys, stock_cli.cmd_isa_log, types.SimpleNamespace(limit=10))
    assert log_out["decisions"][0]["kind"] == "rebalance"


# --- log ---------------------------------------------------------------------


def test_log_limit(env, capsys):
    _do_init(capsys)
    _do_init(capsys)  # two target_change decisions
    rc, out = _run(capsys, stock_cli.cmd_isa_log, types.SimpleNamespace(limit=1))
    assert rc == 0 and len(out["decisions"]) == 1


def test_init_rejects_negative_weight(env, capsys):
    rc, out = _run(
        capsys,
        stock_cli.cmd_isa_init,
        _init_args(allocation="overseas_equity=110,bond=-10"),
    )
    assert rc == 1 and "between 0 and 100" in out["error"]


# --- codex round: portfolio routing + ticker normalization --------------------


def _buy_args(**overrides):
    defaults = {
        "ticker": "360750",
        "qty": 10.0,
        "price": 50_000.0,
        "market": "KR",
        "date": None,
        "note": None,
        "thesis_id": None,
        "portfolio": None,
    }
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _tx_portfolio_names(factory):
    conn = factory()
    rows = conn.execute(
        "SELECT p.name AS name FROM transactions t "
        "JOIN portfolios p ON t.portfolio_id = p.id"
    ).fetchall()
    conn.close()
    return [r["name"] for r in rows]


def test_portfolio_buy_routes_to_named_portfolio(env, capsys):
    conn = env()
    create_portfolio(conn, "KR", "Toss KR")
    create_portfolio(conn, "KR", "ISA")
    conn.close()
    rc, out = _run(capsys, stock_cli.cmd_portfolio_buy, _buy_args(portfolio="ISA"))
    assert rc == 0
    assert _tx_portfolio_names(env) == ["ISA"]


def test_portfolio_buy_default_unchanged_first_kr(env, capsys):
    conn = env()
    create_portfolio(conn, "KR", "Toss KR")
    create_portfolio(conn, "KR", "ISA")
    conn.close()
    rc, out = _run(capsys, stock_cli.cmd_portfolio_buy, _buy_args())
    assert rc == 0
    assert _tx_portfolio_names(env) == ["Toss KR"]  # zero behavior change


def test_portfolio_buy_unknown_name_errors(env, capsys):
    conn = env()
    create_portfolio(conn, "KR", "Toss KR")
    conn.close()
    rc, out = _run(capsys, stock_cli.cmd_portfolio_buy, _buy_args(portfolio="Nope"))
    assert rc == 1 and "Nope" in out["error"]


def test_portfolio_import_routes_to_named_portfolio(env, capsys, tmp_path):
    conn = env()
    create_portfolio(conn, "KR", "Toss KR")
    create_portfolio(conn, "KR", "ISA")
    conn.close()
    csv_file = tmp_path / "trades.csv"
    csv_file.write_text(
        "date,ticker,side,quantity,price\n2026-01-02,360750,BUY,10,50000\n",
        encoding="utf-8",
    )
    args = types.SimpleNamespace(
        csv_file=str(csv_file), market="KR", dry_run=False, portfolio="ISA"
    )
    rc, out = _run(capsys, stock_cli.cmd_portfolio_import, args)
    assert rc == 0 and out["inserted"] == 1
    assert _tx_portfolio_names(env) == ["ISA"]


def test_status_normalizes_held_ticker_codes(env, capsys):
    """A position recorded as '69500' must match the normalized map code
    '069500' instead of being silently excluded (distorting weights)."""
    _do_init(
        capsys,
        allocation="domestic_equity=50,bond=50",
        map="domestic_equity=069500,bond=114260",
    )
    conn = env()
    pf = create_portfolio(conn, "KR", "ISA")
    add_transaction(
        conn,
        portfolio_id=pf.id,
        ticker="69500",
        side="BUY",
        quantity=100,
        price=50_000,
        currency="KRW",
        transacted_at="2026-01-02",
    )
    add_transaction(
        conn,
        portfolio_id=pf.id,
        ticker="114260",
        side="BUY",
        quantity=50,
        price=70_000,
        currency="KRW",
        transacted_at="2026-01-02",
    )
    conn.close()
    rc, out = _run(capsys, stock_cli.cmd_isa_status, types.SimpleNamespace())
    assert rc == 0
    assert out["value_by_class"]["domestic_equity"] == 5_400_000.0
    assert not any("excluded" in n for n in out["notes"])
