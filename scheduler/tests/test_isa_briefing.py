"""Tests for the monthly ISA briefing runner (prompt build + side-effect guards)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import isa_briefing

STATUS = {
    "asof": "2026-07-10",
    "target": {"allocation": {"overseas_equity": 50.0, "bond": 50.0}},
    "value_by_class": {"overseas_equity": 5_400_000.0, "bond": 3_600_000.0},
    "total_value_krw": 9_000_000.0,
    "weights_pct": {"overseas_equity": 60.0, "bond": 40.0},
    "drift_pp": {"overseas_equity": 10.0, "bond": -10.0},
    "rebalance": {"needed": True, "breaches": [], "notes": []},
    "since_inception_return_pct": 5.88,
    "recent_snapshots": [],
    "notes": [],
}
REBALANCE = {
    "needed": True,
    "breaches": [{"asset_class": "overseas_equity", "drift_pp": 10.0}],
    "min_contribution_to_restore": 818_182,
    "notes": [],
}


def test_build_prompt_contains_required_sections():
    prompt = isa_briefing.build_prompt(1_000_000, STATUS, REBALANCE, "MACRO-BLOCK-XYZ")
    assert ".claude/skills/isa-briefing/SKILL.md" in prompt
    assert "--amount 1000000" in prompt
    assert "818182" in prompt or "818,182" in prompt  # rebalance remedy present
    assert '"overseas_equity": 60.0' in prompt  # status weights present
    assert "MACRO-BLOCK-XYZ" in prompt
    assert "--tilt" in prompt  # tilts must go through the clamped CLI


def _no_call(name):
    def boom(*a, **kw):
        raise AssertionError(f"{name} must not be called")

    return boom


def test_dry_run_has_no_side_effects(monkeypatch, capsys):
    calls = []

    def fake_cli(cli_args):
        calls.append(list(cli_args))
        return STATUS

    monkeypatch.setattr(isa_briefing, "_stock_cli_json", fake_cli)
    monkeypatch.setattr(isa_briefing, "call_claude_code", _no_call("claude runner"))
    monkeypatch.setattr(isa_briefing, "call_codex_cli", _no_call("codex runner"))
    monkeypatch.setattr(isa_briefing, "send_briefing", _no_call("telegram"))
    monkeypatch.setattr(isa_briefing, "_macro_block", lambda: "MACRO")

    rc = isa_briefing.main(["--amount", "1000000", "--dry-run"])
    assert rc == 0
    # dry-run must not snapshot (and must not run the decision-logging
    # `isa rebalance` — the status payload's band check stands in).
    assert ["isa", "snapshot"] not in calls
    assert ["isa", "rebalance"] not in calls
    out = capsys.readouterr().out
    assert "isa-briefing/SKILL.md" in out  # prompt printed


def test_missing_target_exits_before_llm(monkeypatch, capsys):
    monkeypatch.setattr(
        isa_briefing,
        "_stock_cli_json",
        lambda cli_args: {"error": "no ISA target — run isa init"},
    )
    monkeypatch.setattr(isa_briefing, "call_claude_code", _no_call("claude runner"))
    monkeypatch.setattr(isa_briefing, "send_briefing", _no_call("telegram"))

    rc = isa_briefing.main(["--amount", "1000000"])
    assert rc == 1
    assert "isa init" in capsys.readouterr().err


def test_full_run_snapshots_and_sends(monkeypatch):
    calls = []

    def fake_cli(cli_args):
        calls.append(list(cli_args))
        if cli_args[:2] == ["isa", "rebalance"]:
            return REBALANCE
        if cli_args[:2] == ["isa", "snapshot"]:
            return {"id": 1, "nav_krw": 9_000_000}
        return STATUS

    sent = {}
    monkeypatch.setattr(isa_briefing, "_stock_cli_json", fake_cli)
    monkeypatch.setattr(isa_briefing, "_macro_block", lambda: "MACRO")
    monkeypatch.setattr(isa_briefing, "call_claude_code", lambda p: "브리핑 본문")
    monkeypatch.setattr(
        isa_briefing,
        "send_briefing",
        lambda text, title: sent.update({"text": text, "title": title}) or True,
    )

    rc = isa_briefing.main(["--amount", "1000000"])
    assert rc == 0
    assert ["isa", "snapshot"] in calls
    assert ["isa", "rebalance"] in calls
    assert sent["text"] == "브리핑 본문"


def test_no_telegram_flag(monkeypatch):
    def fake_cli(cli_args):
        if cli_args[:2] == ["isa", "rebalance"]:
            return REBALANCE
        if cli_args[:2] == ["isa", "snapshot"]:
            return {"id": 1}
        return STATUS

    monkeypatch.setattr(isa_briefing, "_stock_cli_json", fake_cli)
    monkeypatch.setattr(isa_briefing, "_macro_block", lambda: "MACRO")
    monkeypatch.setattr(isa_briefing, "call_claude_code", lambda p: "본문")
    monkeypatch.setattr(isa_briefing, "send_briefing", _no_call("telegram"))

    rc = isa_briefing.main(["--amount", "1000000", "--no-telegram"])
    assert rc == 0
