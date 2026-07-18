"""Tests for the shared non-interactive Codex CLI runner."""

import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _runner_module():
    spec = importlib.util.find_spec("codex_runner")
    assert spec is not None, "scheduler/codex_runner.py must provide the shared runner"
    return importlib.import_module("codex_runner")


def test_run_codex_uses_cron_safe_noninteractive_command(monkeypatch, tmp_path):
    runner = _runner_module()
    captured = {}

    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/codex")

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="  completed briefing  \n", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.run_codex("PROMPT", cwd=tmp_path, timeout=123)

    assert result == "completed briefing"
    assert captured["command"] == [
        "/usr/bin/codex",
        "exec",
        "--skip-git-repo-check",
        "--disable",
        "apps",
        "-m",
        "gpt-5.6-sol",
        "--config",
        'model_reasoning_effort="high"',
        "--config",
        "sandbox_workspace_write.network_access=true",
        "--sandbox",
        "workspace-write",
        "-C",
        str(tmp_path),
        "PROMPT",
    ]
    assert "--full-auto" not in captured["command"]
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    assert captured["kwargs"]["timeout"] == 123
    assert captured["kwargs"]["cwd"] == str(tmp_path)


def test_run_codex_supports_read_only_jobs(monkeypatch, tmp_path):
    runner = _runner_module()
    captured = {}

    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/codex")

    def fake_run(command, **kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="summary", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    assert runner.run_codex("PROMPT", cwd=tmp_path, sandbox="read-only") == "summary"
    assert "read-only" in captured["command"]
    assert "sandbox_workspace_write.network_access=true" not in captured["command"]


def test_run_codex_respects_model_override(monkeypatch, tmp_path):
    runner = _runner_module()
    captured = {}

    monkeypatch.setenv("CODEX_MODEL", "gpt-account-fallback")
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/codex")

    def fake_run(command, **kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="summary", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    assert runner.run_codex("PROMPT", cwd=tmp_path) == "summary"
    model_index = captured["command"].index("-m") + 1
    assert captured["command"][model_index] == "gpt-account-fallback"


def test_run_codex_reports_missing_binary_and_cli_errors(monkeypatch, tmp_path):
    runner = _runner_module()
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="codex CLI not found"):
        runner.run_codex("PROMPT", cwd=tmp_path)

    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=7, stdout="", stderr="authentication expired"
        ),
    )
    with pytest.raises(RuntimeError, match="authentication expired"):
        runner.run_codex("PROMPT", cwd=tmp_path)

    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="  ", stderr=""),
    )
    with pytest.raises(RuntimeError, match="empty output"):
        runner.run_codex("PROMPT", cwd=tmp_path)
