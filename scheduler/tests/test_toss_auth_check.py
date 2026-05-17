"""Unit tests for scheduler/toss_auth_check.py.

Covers the pure decision logic (`decide_alert`) and message rendering. The
subprocess-shell-out to `tossctl` and the live Telegram send are mocked at
the call sites in the integration test below.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import toss_auth_check as mod  # noqa: E402


KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 5, 16, 7, 30, tzinfo=KST)


# ------------------------------ decide_alert ------------------------------


def test_transition_valid_to_invalid_alerts():
    state = {"last_valid": True, "last_alert_at": None}
    alert, reason = mod.decide_alert(current_valid=False, state=state, now=NOW)
    assert alert is True
    assert reason == "transition"


def test_reminder_fires_after_24h():
    last = (NOW - timedelta(hours=25)).isoformat(timespec="seconds")
    state = {"last_valid": False, "last_alert_at": last}
    alert, reason = mod.decide_alert(current_valid=False, state=state, now=NOW)
    assert alert is True
    assert reason == "reminder"


def test_cooldown_suppresses_within_24h():
    last = (NOW - timedelta(hours=1)).isoformat(timespec="seconds")
    state = {"last_valid": False, "last_alert_at": last}
    alert, reason = mod.decide_alert(current_valid=False, state=state, now=NOW)
    assert alert is False
    assert reason == "cooldown"


def test_still_valid_no_alert():
    state = {"last_valid": True, "last_alert_at": None}
    alert, reason = mod.decide_alert(current_valid=True, state=state, now=NOW)
    assert alert is False
    assert reason == "still-valid"


def test_restored_no_alert():
    state = {"last_valid": False, "last_alert_at": NOW.isoformat()}
    alert, reason = mod.decide_alert(current_valid=True, state=state, now=NOW)
    assert alert is False
    assert reason == "restored"


def test_invalid_with_no_prior_alert_fires_reminder():
    state = {"last_valid": False, "last_alert_at": None}
    alert, reason = mod.decide_alert(current_valid=False, state=state, now=NOW)
    assert alert is True
    assert reason == "reminder"


def test_unparseable_last_alert_treated_as_none():
    state = {"last_valid": False, "last_alert_at": "not-a-date"}
    alert, reason = mod.decide_alert(current_valid=False, state=state, now=NOW)
    assert alert is True
    assert reason == "reminder"


# ------------------------------ format_message ------------------------------


def test_format_transition_uses_lock_emoji():
    status = {
        "validation_error": "401 unauthorized",
        "checked_at": "2026-05-16T07:30:00Z",
    }
    msg = mod.format_message(status, reason="transition", now=NOW)
    assert "🔐 Toss auth expired" in msg
    assert "401 unauthorized" in msg
    assert "tossctl auth login" in msg


def test_format_reminder_uses_bell_and_reminder_label():
    status = {"validation_error": "401", "checked_at": "2026-05-16T07:30:00Z"}
    msg = mod.format_message(status, reason="reminder", now=NOW)
    assert "🔔" in msg
    assert "reminder" in msg.lower()


def test_format_falls_back_when_fields_missing():
    msg = mod.format_message(status={}, reason="transition", now=NOW)
    assert "session rejected by server" in msg
    # checked_at falls back to `now` ISO
    assert "2026-05-16T07:30:00" in msg


# ------------------------------ state persistence ------------------------------


def test_load_state_returns_default_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "STATE_PATH", tmp_path / "missing.json")
    state = mod.load_state()
    assert state == {"last_valid": True, "last_alert_at": None}


def test_save_and_load_state_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    monkeypatch.setattr(mod, "STATE_PATH", p)
    mod.save_state({"last_valid": False, "last_alert_at": NOW.isoformat()})
    loaded = mod.load_state()
    assert loaded["last_valid"] is False
    assert loaded["last_alert_at"] == NOW.isoformat()


def test_load_state_recovers_from_corrupt_file(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    p.write_text("{ broken json", encoding="utf-8")
    monkeypatch.setattr(mod, "STATE_PATH", p)
    state = mod.load_state()
    assert state == {"last_valid": True, "last_alert_at": None}


# ------------------------------ main integration ------------------------------


def test_main_sends_alert_on_transition(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "STATE_PATH", tmp_path / "state.json")
    mod.save_state({"last_valid": True, "last_alert_at": None})

    sent: list[str] = []

    def fake_status():
        return {"valid": False, "validation_error": "401", "checked_at": "x"}

    def fake_send(text, *args, **kwargs):
        sent.append(text)
        return True

    monkeypatch.setattr(mod, "get_auth_status", fake_status)
    monkeypatch.setattr(mod, "send_message", fake_send)

    assert mod.main() == 0
    assert len(sent) == 1
    assert "Toss auth expired" in sent[0]
    after = mod.load_state()
    assert after["last_valid"] is False
    assert after["last_alert_at"] is not None


def test_main_skips_within_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "STATE_PATH", tmp_path / "state.json")
    mod.save_state(
        {
            "last_valid": False,
            "last_alert_at": (datetime.now(tz=KST) - timedelta(hours=1)).isoformat(
                timespec="seconds"
            ),
        }
    )

    sent: list[str] = []
    monkeypatch.setattr(
        mod,
        "get_auth_status",
        lambda: {"valid": False, "validation_error": "401"},
    )
    monkeypatch.setattr(mod, "send_message", lambda *a, **k: sent.append(a) or True)

    assert mod.main() == 0
    assert sent == []


def test_main_returns_1_when_tossctl_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "STATE_PATH", tmp_path / "state.json")

    def boom():
        raise RuntimeError("tossctl not found on PATH")

    monkeypatch.setattr(mod, "get_auth_status", boom)
    assert mod.main() == 1
