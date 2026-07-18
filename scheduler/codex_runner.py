"""Shared non-interactive Codex CLI runner for scheduled jobs."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent
CODEX_MODEL = "gpt-5.6-sol"
CODEX_REASONING_EFFORT = "high"


def run_codex(
    prompt: str,
    *,
    cwd: Path = PROJECT_ROOT,
    timeout: int = 900,
    sandbox: str = "workspace-write",
) -> str:
    """Run one bounded Codex CLI task and return its final message.

    Args:
        prompt: Instructions passed to ``codex exec``.
        cwd: Working directory exposed to Codex.
        timeout: Subprocess wall-clock limit in seconds.
        sandbox: Codex sandbox policy (``workspace-write`` or ``read-only``).

    Returns:
        The stripped final assistant message written to stdout.

    Raises:
        RuntimeError: If Codex is unavailable, exits non-zero, or returns no text.
        subprocess.TimeoutExpired: If the wall-clock limit is exceeded.
    """
    codex_path = shutil.which("codex")
    if not codex_path:
        raise RuntimeError("codex CLI not found")

    command = [
        codex_path,
        "exec",
        "--skip-git-repo-check",
        "--disable",
        "apps",
        "-m",
        os.environ.get("CODEX_MODEL", CODEX_MODEL),
        "--config",
        f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"',
    ]
    if sandbox == "workspace-write":
        command.extend(
            ["--config", "sandbox_workspace_write.network_access=true"]
        )
    command.extend(["--sandbox", sandbox, "-C", str(cwd), prompt])

    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"codex CLI failed (exit {result.returncode}): {result.stderr[:500]}"
        )
    output = result.stdout.strip()
    if not output:
        raise RuntimeError("codex CLI returned empty output")
    return output
