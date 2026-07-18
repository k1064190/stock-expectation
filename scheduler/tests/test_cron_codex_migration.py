"""Static coverage proving scheduled LLM paths cannot invoke Claude."""

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent.parent
SCHEDULED_LLM_MODULES = (
    "scheduler/daily_briefing.py",
    "scheduler/deep_dive.py",
    "scheduler/gold_trend.py",
    "scheduler/isa_briefing.py",
    "scheduler/news_bucket_audit.py",
)


def test_scheduled_llm_modules_have_no_executable_claude_path():
    forbidden = (
        re.compile(r"shutil\.which\([\"']claude[\"']\)"),
        re.compile(r"\[\s*[\"']claude[\"']"),
        re.compile(r"\bcall_claude(?:_code|_api)?\b"),
    )
    violations = []
    for relative in SCHEDULED_LLM_MODULES:
        text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in forbidden):
            violations.append(relative)
    assert violations == []


def test_crontab_source_is_codex_only():
    text = (PROJECT_ROOT / "scheduler/crontab.example").read_text(encoding="utf-8")
    assert "claude-code" not in text
    assert "claude -p" not in text
    assert "--mode api" not in text
    assert text.count("daily_briefing.py") == 3
    assert text.count("daily_briefing.py --market") == 3
    assert text.count("--mode codex-cli") >= 4  # daily x3 + monthly ISA
    assert "gold_trend.py --llm-mode codex-cli" in text


def test_anthropic_scheduler_extra_is_removed():
    text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "anthropic" not in text.lower()


def test_capstone_cron_notification_points_to_codex():
    text = (PROJECT_ROOT / "scheduler/capstone_readiness.py").read_text(
        encoding="utf-8"
    )
    assert "Claude" not in text
    assert "Codex" in text
