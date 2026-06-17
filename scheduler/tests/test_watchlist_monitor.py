"""Tests for the watchlist monitor: triggers, dedup, cooldown, gate."""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import scheduler.watchlist_monitor as wm
from scheduler.watchlist_monitor import (
    evaluate_triggers,
    is_market_open,
    should_fire,
    run_monitor,
    COOLDOWN,
)
from scheduler.watchlist_store import WatchTarget, get_connection, add_watch

KR_TZ = ZoneInfo("Asia/Seoul")


def _bull(entry_low=None, entry_high=None, stop=None, target=None, reentry=None):
    return WatchTarget(
        ticker="NVDA",
        market="US",
        direction="BULL",
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        target=target,
        reentry=reentry,
        source="saved",
        label="saved:1",
    )


def _bear(entry_low=None, entry_high=None, stop=None, target=None):
    return WatchTarget(
        ticker="TSLA",
        market="US",
        direction="BEAR",
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        target=target,
        source="saved",
        label="saved:2",
    )


# --- Trigger boundaries: BULL ---


def test_bull_entry_zone_inside():
    t = _bull(entry_low=100, entry_high=105)
    assert "ENTRY" in evaluate_triggers(t, 102)


def test_bull_entry_zone_boundary():
    t = _bull(entry_low=100, entry_high=105)
    assert "ENTRY" in evaluate_triggers(t, 100)
    assert "ENTRY" in evaluate_triggers(t, 105)


def test_bull_entry_zone_outside():
    t = _bull(entry_low=100, entry_high=105)
    assert "ENTRY" not in evaluate_triggers(t, 99.9)
    assert "ENTRY" not in evaluate_triggers(t, 105.1)


def test_single_point_entry_band():
    """A single-point entry (prediction/position) uses a +/-1% band."""
    t = _bull(entry_low=100, entry_high=100)  # single point
    assert "ENTRY" in evaluate_triggers(t, 100.5)  # within +1%
    assert "ENTRY" in evaluate_triggers(t, 99.5)  # within -1%
    assert "ENTRY" not in evaluate_triggers(t, 102)  # outside band


def test_bull_stop_touch():
    t = _bull(stop=95)
    assert "STOP" in evaluate_triggers(t, 95)
    assert "STOP" in evaluate_triggers(t, 94)
    assert "STOP" not in evaluate_triggers(t, 96)


def test_bull_target_touch():
    t = _bull(target=130)
    assert "TARGET" in evaluate_triggers(t, 130)
    assert "TARGET" in evaluate_triggers(t, 131)
    assert "TARGET" not in evaluate_triggers(t, 129)


def test_reentry_saved_only():
    t = _bull(reentry=110)
    assert "REENTRY" in evaluate_triggers(t, 110)
    assert "REENTRY" in evaluate_triggers(t, 111)
    assert "REENTRY" not in evaluate_triggers(t, 109)


# --- Trigger boundaries: BEAR (mirrored) ---


def test_bear_stop_touch():
    """BEAR stop fires when price rises to/through the stop."""
    t = _bear(stop=260)
    assert "STOP" in evaluate_triggers(t, 260)
    assert "STOP" in evaluate_triggers(t, 261)
    assert "STOP" not in evaluate_triggers(t, 259)


def test_bear_target_touch():
    """BEAR target fires when price falls to/through the target."""
    t = _bear(target=200)
    assert "TARGET" in evaluate_triggers(t, 200)
    assert "TARGET" in evaluate_triggers(t, 199)
    assert "TARGET" not in evaluate_triggers(t, 201)


# --- Dedup / re-arm state machine ---


def test_dedup_same_state_fires_once():
    """A trigger satisfied across two runs fires only once."""
    state = {}
    now = datetime(2026, 6, 17, 12, 0, tzinfo=KR_TZ)
    assert should_fire(state, "k", True, now) is True
    # Same state, later but cooldown-irrelevant since no transition out.
    later = now + timedelta(hours=10)
    assert should_fire(state, "k", True, later) is False


def test_dedup_transition_out_and_back_realerts():
    """Leaving the satisfied state and returning re-arms the alert."""
    state = {}
    t0 = datetime(2026, 6, 17, 9, 0, tzinfo=KR_TZ)
    assert should_fire(state, "k", True, t0) is True
    # Transition out.
    t1 = t0 + timedelta(hours=7)
    assert should_fire(state, "k", False, t1) is False
    # Transition back in, past cooldown → re-alert.
    t2 = t1 + timedelta(hours=1)
    assert should_fire(state, "k", True, t2) is True


def test_cooldown_suppresses_rapid_reentry():
    """A fresh rising edge within the 6h cooldown is suppressed."""
    state = {}
    t0 = datetime(2026, 6, 17, 9, 0, tzinfo=KR_TZ)
    assert should_fire(state, "k", True, t0) is True
    # Out and back within cooldown window.
    assert should_fire(state, "k", False, t0 + timedelta(hours=1)) is False
    within = t0 + timedelta(hours=2)  # < COOLDOWN since last alert
    assert within - t0 < COOLDOWN
    assert should_fire(state, "k", True, within) is False
    # And once cooldown elapses, a fresh edge fires.
    later = t0 + COOLDOWN + timedelta(minutes=1)
    # Must be a fresh edge: currently last_state is True, so go out first.
    should_fire(state, "k", False, t0 + timedelta(hours=3))
    assert should_fire(state, "k", True, later) is True


# --- Market-hours gate ---


def test_kr_open_weekday():
    # Wed 2026-06-17 10:00 KST.
    now = datetime(2026, 6, 17, 10, 0, tzinfo=KR_TZ)
    assert is_market_open("KR", now) is True


def test_kr_closed_after_hours():
    now = datetime(2026, 6, 17, 16, 0, tzinfo=KR_TZ)
    assert is_market_open("KR", now) is False


def test_kr_closed_weekend():
    # Sat 2026-06-20 10:00 KST.
    now = datetime(2026, 6, 20, 10, 0, tzinfo=KR_TZ)
    assert is_market_open("KR", now) is False


def test_us_open_evening_kst_weekday():
    # Wed 2026-06-17 23:00 KST → US Wed daytime session.
    now = datetime(2026, 6, 17, 23, 0, tzinfo=KR_TZ)
    assert is_market_open("US", now) is True


def test_us_open_early_morning_weekday():
    # Wed 2026-06-17 03:00 KST → US Tue evening session (prev KST day Tue).
    now = datetime(2026, 6, 17, 3, 0, tzinfo=KR_TZ)
    assert is_market_open("US", now) is True


def test_us_closed_monday_morning_kst():
    # Mon 2026-06-15 03:00 KST → Sun US, market closed.
    now = datetime(2026, 6, 15, 3, 0, tzinfo=KR_TZ)
    assert is_market_open("US", now) is False


def test_us_closed_midday_kst():
    now = datetime(2026, 6, 17, 14, 0, tzinfo=KR_TZ)
    assert is_market_open("US", now) is False


# --- run_monitor integration (monkeypatched provider + sender) ---


class _FixedProvider:
    """Provider stub returning a fixed price (None to simulate a fetch miss)."""

    def __init__(self, price):
        self._price = price

    def get_current_price(self, ticker):
        return self._price


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """Capture sent alerts and route providers to a fixed price.

    Yields a dict the test mutates: ``price`` controls the stubbed provider,
    ``sent`` collects send_watch_alert/send_message payloads.
    """
    ctx = {"price": 102.0, "sent": []}

    def fake_provider(market):
        return _FixedProvider(ctx["price"])

    def fake_watch_alert(**kwargs):
        ctx["sent"].append(("watch", kwargs))
        return True

    def fake_message(text, *a, **k):
        ctx["sent"].append(("message", text))
        return True

    monkeypatch.setattr(wm, "_get_provider", fake_provider)
    monkeypatch.setattr(wm, "send_watch_alert", fake_watch_alert)
    # Digest path imports send_message lazily from the module; patch there.
    import scheduler.telegram_sender as ts

    monkeypatch.setattr(ts, "send_message", fake_message)

    ctx["state_path"] = tmp_path / "state" / "alerts.json"
    ctx["wl_path"] = tmp_path / "watchlist.db"
    ctx["pred_path"] = tmp_path / "predictions.db"
    ctx["pf_path"] = tmp_path / "portfolio.db"
    yield ctx


def _seed_saved(ctx, **kwargs):
    conn = get_connection(ctx["wl_path"])
    add_watch(conn, ticker="NVDA", market="US", **kwargs)
    conn.close()


def _run(ctx, **overrides):
    params = dict(
        market="US",
        force=True,
        now=datetime(2026, 6, 17, 23, 0, tzinfo=KR_TZ),
        state_path=ctx["state_path"],
        watchlist_db_path=ctx["wl_path"],
        predictions_db_path=ctx["pred_path"],
        portfolio_db_path=ctx["pf_path"],
    )
    params.update(overrides)
    return run_monitor(**params)


def test_run_fires_entry_alert(patched):
    _seed_saved(patched, entry_low=100, entry_high=105, stop=95, target=130)
    patched["price"] = 102.0
    summary = _run(patched)
    assert summary["fired"] == 1
    assert summary["triggers"][0]["trigger"] == "ENTRY"
    assert any(s[0] == "watch" for s in patched["sent"])


def test_run_dedup_second_run_no_realert(patched):
    _seed_saved(patched, entry_low=100, entry_high=105)
    patched["price"] = 102.0
    first = _run(patched)
    assert first["fired"] == 1
    patched["sent"].clear()
    second = _run(patched, now=datetime(2026, 6, 17, 23, 30, tzinfo=KR_TZ))
    assert second["fired"] == 0
    assert patched["sent"] == []


def test_run_none_price_skips_and_counts_error(patched):
    _seed_saved(patched, entry_low=100, entry_high=105)
    patched["price"] = None
    summary = _run(patched)
    assert summary["checked"] == 0
    assert summary["errors"] == 1
    assert summary["fired"] == 0


def test_run_market_hours_gate_noop_when_closed(patched):
    _seed_saved(patched, entry_low=100, entry_high=105)
    patched["price"] = 102.0
    # US closed at 14:00 KST, force=False → gate suppresses the whole run.
    summary = _run(patched, force=False, now=datetime(2026, 6, 17, 14, 0, tzinfo=KR_TZ))
    assert summary["markets"] == []
    assert summary["checked"] == 0
    assert summary["fired"] == 0
    assert patched["sent"] == []


def test_run_dry_run_sends_nothing_but_fires(patched):
    _seed_saved(patched, entry_low=100, entry_high=105)
    patched["price"] = 102.0
    summary = _run(patched, dry_run=True)
    assert summary["fired"] == 1
    assert patched["sent"] == []


def test_run_digest_when_more_than_three(patched):
    """More than 3 fired triggers collapse into a single digest message."""
    # One saved row with 4 simultaneously-satisfied triggers: ENTRY (band
    # around 100), STOP (>=... no), TARGET, REENTRY. Construct levels so all of
    # ENTRY/TARGET/REENTRY + STOP fire at once is impossible for BULL (stop is
    # below, target above). Instead seed 4 separate saved rows at one price.
    conn = get_connection(patched["wl_path"])
    for i in range(4):
        add_watch(
            conn,
            ticker=f"T{i}",
            market="US",
            entry_low=100,
            entry_high=105,
        )
    conn.close()
    patched["price"] = 102.0
    summary = _run(patched)
    assert summary["fired"] == 4
    msgs = [s for s in patched["sent"] if s[0] == "message"]
    watches = [s for s in patched["sent"] if s[0] == "watch"]
    assert len(msgs) == 1  # single digest
    assert watches == []  # no individual sends
    assert "워치리스트 알림 4건" in msgs[0][1]


def test_run_returns_json_serializable_triggers(patched):
    """Returned triggers must not carry the internal WatchTarget object."""
    import json

    _seed_saved(patched, entry_low=100, entry_high=105)
    patched["price"] = 102.0
    summary = _run(patched, dry_run=True)
    # Should serialize without error (no WatchTarget left behind).
    json.dumps(summary)
    assert "target" not in summary["triggers"][0]
