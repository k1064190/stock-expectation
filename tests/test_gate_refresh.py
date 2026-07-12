"""Tests for store-time overextension refresh in ``predict create``.

Covers ``stock_cli._refresh_overextension_components``: LIVE BULL creates that
omit the ``overextension``/``return_1m`` components get them recomputed from
fresh bars so the store-level gate (RULE R2) cannot be bypassed by simply
dropping ``--components``. Fail-open on fetch errors.
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
    "stock_cli", PROJECT_ROOT / "stock_cli.py"
)
stock_cli = importlib.util.module_from_spec(_spec)
sys.modules["stock_cli"] = stock_cli
_spec.loader.exec_module(stock_cli)


@pytest.fixture
def db_path():
    """Path to a fresh temp predictions.db file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        yield Path(f.name)


def _flat_bars(n=60, close=100.0):
    """Bars with a flat close — overextension NONE, return_1m ~0."""
    return [{"close": close, "volume": 1000} for _ in range(n)]


def _parabolic_bars(n=60):
    """Bars ending in a vertical blow-off: last close far above MA20."""
    bars = [{"close": 100.0, "volume": 1000} for _ in range(n - 1)]
    bars.append({"close": 150.0, "volume": 1000})  # +50% above MA20 → EXTREME
    return bars


class _FakeProvider:
    def __init__(self, bars=None, exc=None):
        self.bars = bars
        self.exc = exc
        self.calls = 0

    def get_price_history(self, ticker, days=30):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.bars


def _patch_provider(monkeypatch, provider):
    monkeypatch.setattr(stock_cli, "_get_provider", lambda market: provider)


# ---------------------------------------------------------------------------
# _refresh_overextension_components (helper)
# ---------------------------------------------------------------------------


def test_refresh_injects_missing_fields(monkeypatch):
    provider = _FakeProvider(bars=_parabolic_bars())
    _patch_provider(monkeypatch, provider)

    out, refreshed = stock_cli._refresh_overextension_components(None, "NVDA", "US")

    assert refreshed is True
    assert out["overextension"] == "EXTREME"
    assert out["return_1m"] == pytest.approx(0.5)
    assert provider.calls == 1


def test_refresh_skips_when_fields_present(monkeypatch):
    provider = _FakeProvider(exc=AssertionError("must not fetch"))
    _patch_provider(monkeypatch, provider)

    comps = {"overextension": "NONE", "return_1m": 0.05}
    out, refreshed = stock_cli._refresh_overextension_components(comps, "NVDA", "US")

    assert refreshed is False
    assert out is comps
    assert provider.calls == 0


def test_refresh_fills_only_missing_field(monkeypatch):
    provider = _FakeProvider(bars=_flat_bars())
    _patch_provider(monkeypatch, provider)

    comps = {"overextension": "ELEVATED", "algo": 6.5}
    out, refreshed = stock_cli._refresh_overextension_components(comps, "NVDA", "US")

    assert refreshed is True
    assert out["overextension"] == "ELEVATED"  # caller value preserved
    assert out["return_1m"] == pytest.approx(0.0)
    assert out["algo"] == 6.5


def test_refresh_fail_open_on_fetch_error(monkeypatch, capsys):
    provider = _FakeProvider(exc=RuntimeError("network down"))
    _patch_provider(monkeypatch, provider)

    out, refreshed = stock_cli._refresh_overextension_components(None, "NVDA", "US")

    assert refreshed is False
    assert out is None
    assert "gate refresh" in capsys.readouterr().err.lower()


def test_refresh_sparse_bars_degrades_gracefully(monkeypatch):
    """<22 bars: return_1m is None (not injected), overextension still set."""
    provider = _FakeProvider(bars=_flat_bars(n=10))
    _patch_provider(monkeypatch, provider)

    out, refreshed = stock_cli._refresh_overextension_components(None, "NVDA", "US")

    assert refreshed is True
    assert out["overextension"] == "NONE"
    assert "return_1m" not in out


def test_refresh_kr_market_uses_kr_provider(monkeypatch):
    """KR tickers route through _get_provider('KR') with the raw ticker."""
    seen = {}

    class _KRProvider(_FakeProvider):
        def get_price_history(self, ticker, days=30):
            seen["ticker"] = ticker
            return super().get_price_history(ticker, days)

    provider = _KRProvider(bars=_flat_bars())

    def _fake_get_provider(market):
        seen["market"] = market
        return provider

    monkeypatch.setattr(stock_cli, "_get_provider", _fake_get_provider)

    out, refreshed = stock_cli._refresh_overextension_components(None, "5930", "KR")

    assert refreshed is True
    assert seen == {"market": "KR", "ticker": "5930"}
    assert out["overextension"] == "NONE"


def test_refresh_fail_open_on_empty_bars(monkeypatch):
    provider = _FakeProvider(bars=[])
    _patch_provider(monkeypatch, provider)

    out, refreshed = stock_cli._refresh_overextension_components(None, "NVDA", "US")

    assert refreshed is False
    assert out is None


# ---------------------------------------------------------------------------
# cmd_predict_create integration
# ---------------------------------------------------------------------------


def _make_args(**overrides):
    base = dict(
        ticker="NVDA",
        market="US",
        direction="BULL",
        confidence=0.6,
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
        no_gate_refresh=False,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


@pytest.fixture
def use_temp_db(db_path, monkeypatch):
    orig = stock_cli.get_connection
    monkeypatch.setattr(stock_cli, "get_connection", lambda: orig(db_path))
    return db_path


def _run_create(args, capsys):
    rc = stock_cli.cmd_predict_create(args)
    return rc, json.loads(capsys.readouterr().out)


def test_create_live_bull_parabolic_rejected(monkeypatch, use_temp_db, capsys):
    _patch_provider(monkeypatch, _FakeProvider(bars=_parabolic_bars()))

    rc, out = _run_create(_make_args(), capsys)

    assert rc == 1
    assert "gated" in out["error"]


def test_create_live_bull_healthy_gets_components(monkeypatch, use_temp_db, capsys):
    _patch_provider(monkeypatch, _FakeProvider(bars=_flat_bars()))

    rc, out = _run_create(_make_args(ticker="AAPL"), capsys)

    assert rc == 0
    assert out["components"]["overextension"] == "NONE"
    assert out["components"]["return_1m"] == pytest.approx(0.0)


def test_create_no_gate_refresh_skips_fetch(monkeypatch, use_temp_db, capsys):
    provider = _FakeProvider(exc=AssertionError("must not fetch"))
    _patch_provider(monkeypatch, provider)

    rc, _ = _run_create(_make_args(ticker="MSFT", no_gate_refresh=True), capsys)

    assert rc == 0
    assert provider.calls == 0


def test_create_interactive_skips_refresh(monkeypatch, use_temp_db, capsys):
    provider = _FakeProvider(exc=AssertionError("must not fetch"))
    _patch_provider(monkeypatch, provider)

    rc, _ = _run_create(_make_args(ticker="TSLA", source="INTERACTIVE"), capsys)

    assert rc == 0
    assert provider.calls == 0


def test_create_bear_skips_refresh(monkeypatch, use_temp_db, capsys):
    """BEAR never triggers the refresh — no fetch even on the LIVE path.

    (The LIVE BEAR create itself is hard-rejected by the store's separate
    BEAR gate, hence rc == 1; the assertion that matters is calls == 0.)
    """
    provider = _FakeProvider(exc=AssertionError("must not fetch"))
    _patch_provider(monkeypatch, provider)

    rc, out = _run_create(_make_args(ticker="META", direction="BEAR"), capsys)

    assert rc == 1
    assert "BEAR" in out["error"]
    assert provider.calls == 0


def test_create_complete_components_skips_fetch(monkeypatch, use_temp_db, capsys):
    provider = _FakeProvider(exc=AssertionError("must not fetch"))
    _patch_provider(monkeypatch, provider)

    comps = json.dumps({"overextension": "NONE", "return_1m": 0.03, "algo": 7.0})
    rc, out = _run_create(_make_args(ticker="GOOG", components=comps), capsys)

    assert rc == 0
    assert provider.calls == 0
    assert out["components"]["algo"] == 7.0


def test_create_fail_open_still_inserts(monkeypatch, use_temp_db, capsys):
    _patch_provider(monkeypatch, _FakeProvider(exc=RuntimeError("network down")))

    rc, out = _run_create(_make_args(ticker="AMD"), capsys)

    assert rc == 0
    assert out.get("components") is None
