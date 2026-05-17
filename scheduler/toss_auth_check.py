"""Daily Toss session validity check with Telegram alert on expiration.

Detection signal:
    `tossctl auth status --output json` → field `"valid": false` means the
    persistent cookie still exists but the Toss server has rejected the
    session (typically after ~1 week of inactivity).

Alert behavior (user choice 2026-05-16):
    - valid → invalid transition: alert immediately.
    - stays invalid: send one reminder per 24 hours until user re-auths.
    - invalid → valid: silently update state (no "restored" notice).

State persisted at state/last-toss-auth-alert.json so reruns within the
24 h cooldown window are suppressed.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Allow `python scheduler/toss_auth_check.py` from repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scheduler.telegram_sender import send_message  # noqa: E402

logger = logging.getLogger(__name__)

STATE_PATH = REPO_ROOT / "state" / "last-toss-auth-alert.json"
REMINDER_INTERVAL = timedelta(hours=24)
KST = timezone(timedelta(hours=9))


def get_auth_status() -> dict:
    """Shell out to tossctl and parse the JSON auth status.

    Returns:
        Parsed JSON dict from `tossctl auth status --output json`. Always
        contains at least `valid` (bool) when tossctl ran cleanly.

    Raises:
        RuntimeError: If tossctl is missing, exits non-zero, or returns
            output that is not parseable JSON.
    """
    try:
        result = subprocess.run(
            ["tossctl", "auth", "status", "--output", "json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as e:
        raise RuntimeError("tossctl not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("tossctl auth status timed out after 30s") from e

    if result.returncode != 0:
        raise RuntimeError(
            f"tossctl exited {result.returncode}: {result.stderr.strip()}"
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"tossctl returned non-JSON: {result.stdout[:200]}") from e


def load_state() -> dict:
    """Read the last-alert state file, returning a safe default on miss."""
    if not STATE_PATH.exists():
        return {"last_valid": True, "last_alert_at": None}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("state file unreadable, resetting: %s", e)
        return {"last_valid": True, "last_alert_at": None}


def save_state(state: dict) -> None:
    """Persist state atomically via tmp-rename."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def decide_alert(
    current_valid: bool,
    state: dict,
    now: datetime,
) -> tuple[bool, str]:
    """Decide whether to alert and label the reason.

    Args:
        current_valid: Truthiness of `valid` from tossctl auth status.
        state: Persisted state dict with `last_valid` and `last_alert_at`.
        now: Current timestamp (timezone-aware, KST).

    Returns:
        (alert: bool, reason: str). Reason values: "transition", "reminder",
        "cooldown", "still-valid", "restored".
    """
    if current_valid:
        if not state.get("last_valid", True):
            return False, "restored"
        return False, "still-valid"

    if state.get("last_valid", True):
        return True, "transition"

    last_alert = _parse_iso(state.get("last_alert_at"))
    if last_alert is None or now - last_alert >= REMINDER_INTERVAL:
        return True, "reminder"
    return False, "cooldown"


def format_message(status: dict, reason: str, now: datetime) -> str:
    """Render the Telegram alert body."""
    header = (
        "🔐 Toss auth expired"
        if reason == "transition"
        else "🔔 Toss auth still expired (reminder)"
    )
    checked_at = status.get("checked_at") or now.isoformat(timespec="seconds")
    error = status.get("validation_error") or "session rejected by server"
    return (
        f"{header}\n"
        f"Checked: {checked_at}\n"
        f"Reason: {error}\n"
        f"Action: run `tossctl auth login` (Chrome + Toss app QR)"
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    now = datetime.now(tz=KST)

    try:
        status = get_auth_status()
    except RuntimeError as e:
        logger.error("auth status check failed: %s", e)
        return 1

    current_valid = bool(status.get("valid"))
    state = load_state()
    alert, reason = decide_alert(current_valid, state, now)

    logger.info(
        "valid=%s reason=%s last_valid=%s last_alert_at=%s",
        current_valid,
        reason,
        state.get("last_valid"),
        state.get("last_alert_at"),
    )

    if alert:
        message = format_message(status, reason, now)
        ok = send_message(message)
        if not ok:
            logger.error("Telegram send failed; not advancing last_alert_at")
            return 2
        state["last_alert_at"] = now.isoformat(timespec="seconds")

    state["last_valid"] = current_valid
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
