"""Weekly readiness monitor for the learned-blend capstone (Stage 11 → 18).

The capstone — a learned blend of the algo / news / llm_context pillars into a
*discriminating* confidence (vs the current base-rate isotonic recalibration) —
is data-blocked: it needs enough CLOSED predictions that carry per-pillar
``components``. The daily briefing now tags every LIVE pick's components
(discovery_source/setup_type + overextension/return_1m + pillar scores), so the
qualifying set grows as picks close.

This monitor counts the qualifying rows and, once the threshold is crossed,
sends a one-shot Telegram ping so Doctor Cho can ask Codex to build the
capstone (as a reviewed PR — it changes live confidence, so it is not built
unsupervised). It is pure-read, never mutates predictions, and never raises.

Run weekly via cron. Threshold override: ``CAPSTONE_MIN_CLOSED`` env var.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT / "scheduler") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scheduler"))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("capstone_readiness")

DB_PATH = PROJECT_ROOT / "data" / "predictions.db"
FLAG_PATH = PROJECT_ROOT / "state" / "capstone_ready.flag"
DEFAULT_THRESHOLD = 100


def _threshold() -> int:
    """Resolve the readiness threshold (env ``CAPSTONE_MIN_CLOSED`` or default)."""
    try:
        return int(os.environ.get("CAPSTONE_MIN_CLOSED", DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD


def count_components_closed(db_path: Path | None = None) -> tuple[int, int, int]:
    """Count CLOSED predictions carrying pillar components.

    A row qualifies when it is HIT/MISS and its ``components`` JSON has an
    ``algo`` key — i.e. a real per-pillar tag the learned blend can train on
    (excludes legacy rows with NULL/partial components).

    Args:
        db_path: Path to predictions.db (opened read-only). Defaults to the
            module ``DB_PATH``, resolved at call time so tests/relocation can
            override it via the module global.

    Returns:
        ``(total, hits, misses)`` qualifying counts. ``(0, 0, 0)`` if the DB is
        absent or unreadable (never raises).
    """
    db_path = db_path or DB_PATH
    if not db_path.exists():
        return (0, 0, 0)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # noqa: BLE001
        logger.warning("capstone readiness: cannot open DB: %s", exc)
        return (0, 0, 0)
    try:
        row = conn.execute(
            """SELECT COUNT(*),
                      SUM(status = 'HIT'),
                      SUM(status = 'MISS')
                 FROM predictions
                WHERE status IN ('HIT', 'MISS')
                  AND components IS NOT NULL
                  AND json_extract(components, '$.algo') IS NOT NULL"""
        ).fetchone()
    except sqlite3.Error as exc:  # noqa: BLE001 — never block the monitor
        logger.warning("capstone readiness: query failed: %s", exc)
        return (0, 0, 0)
    finally:
        conn.close()
    return (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0))


def run(threshold: int | None = None) -> dict:
    """Check readiness and ping Telegram once when the threshold is first met.

    Args:
        threshold: Override the qualifying-row threshold (defaults to env / 100).

    Returns:
        Summary dict ``{total, hits, misses, threshold, ready, already_notified,
        notified?}``.
    """
    threshold = threshold or _threshold()
    total, hits, misses = count_components_closed()
    ready = total >= threshold
    already = FLAG_PATH.exists()
    summary = {
        "total": total,
        "hits": hits,
        "misses": misses,
        "threshold": threshold,
        "ready": ready,
        "already_notified": already,
    }
    logger.info(
        "capstone readiness: %d/%d components-tagged closed (HIT %d / MISS %d), "
        "ready=%s notified=%s",
        total,
        threshold,
        hits,
        misses,
        ready,
        already,
    )
    if ready and not already:
        msg = (
            "📊 Learned-blend capstone 데이터 준비 완료\n"
            f"components 태그된 closed 예측 {total}건 "
            f"(HIT {hits} / MISS {misses}, 임계 {threshold}).\n"
            "Codex에게 'learned-blend capstone 빌드해줘'라고 요청하세요 "
            "(Stage 18 — 검증 후 PR로 제출)."
        )
        try:
            from telegram_sender import send_message

            if send_message(msg):
                FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
                FLAG_PATH.write_text(json.dumps(summary), encoding="utf-8")
                summary["notified"] = True
                logger.info("capstone readiness: Telegram ping sent; flag written")
        except Exception as exc:  # noqa: BLE001 — alert is best-effort
            logger.warning("capstone readiness: Telegram alert failed: %s", exc)
    return summary


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
