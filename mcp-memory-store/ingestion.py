"""Ingestion helpers — pull from predictions.db / news / outcomes into mem0.

Watermark file at ``state/memory-watermark.json`` tracks the last ingested
``created_at`` per source so re-runs are incremental.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    from .client import MemoryStore
    from .schemas import MemoryRecord
except ImportError:
    from client import MemoryStore  # type: ignore[no-redef]
    from schemas import MemoryRecord  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


@dataclass
class IngestStats:
    """Per-source counts for an ingestion run."""

    predictions_added: int = 0
    outcomes_added: int = 0
    transmission_chains_added: int = 0
    errors: int = 0


def _load_watermark(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        logger.warning("Watermark file %s corrupt; starting fresh.", path)
        return {}


def _save_watermark(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _row_to_prediction_record(row: sqlite3.Row) -> MemoryRecord:
    """Render a predictions-table row as a memory record.

    The ``content`` is a compact narrative the embedder can index well;
    structured fields go into metadata for filter searches.
    """
    content = (
        f"{row['ticker']} ({row['market']}) {row['direction']} "
        f"@{row['confidence']:.2f} {row['timeframe']} entry={row['entry_price']}: "
        f"{row['reasoning']}"
    )
    return MemoryRecord(
        category="predictions",
        content=content,
        metadata={
            "prediction_id": row["id"],
            "ticker": row["ticker"],
            "market": row["market"],
            "direction": row["direction"],
            "confidence": row["confidence"],
            "timeframe": row["timeframe"],
            "status": row["status"],
            "entry_price": row["entry_price"],
            "target_price": row["target_price"],
            "stop_price": row["stop_price"],
            "signals_used": row["signals_used"],
            "analysis_group_id": row["analysis_group_id"],
            "created_at": row["created_at"],
        },
    )


def _row_to_outcome_record(row: sqlite3.Row) -> MemoryRecord:
    """Closed prediction -> outcome memory."""
    direction_label = "matched" if row["status"] == "HIT" else "missed"
    content = (
        f"{row['ticker']} ({row['market']}) {row['direction']} "
        f"@{row['confidence']:.2f} {row['timeframe']} {direction_label}: "
        f"return={row['outcome_return']}; reasoning={row['reasoning']}"
    )
    return MemoryRecord(
        category="outcomes",
        content=content,
        metadata={
            "prediction_id": row["id"],
            "ticker": row["ticker"],
            "market": row["market"],
            "status": row["status"],
            "confidence": row["confidence"],
            "outcome_return": row["outcome_return"],
            "outcome_date": row["outcome_date"],
            "signals_used": row["signals_used"],
            "analysis_group_id": row["analysis_group_id"],
        },
    )


def _select_since(
    conn: sqlite3.Connection, table: str, since_iso: str | None
) -> list[sqlite3.Row]:
    """Read rows newer than ``since_iso`` (or all rows when None).

    Uses ``created_at`` as the watermark column for both predictions and
    outcomes — outcomes don't have a separate timestamp; they just flip
    ``status`` and set ``outcome_date``, so the second pass uses
    ``outcome_date`` instead.
    """
    if table == "predictions":
        if since_iso:
            return conn.execute(
                "SELECT * FROM predictions WHERE created_at > ? ORDER BY created_at ASC",
                (since_iso,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM predictions ORDER BY created_at ASC"
        ).fetchall()
    if table == "outcomes":
        if since_iso:
            return conn.execute(
                "SELECT * FROM predictions "
                "WHERE status IN ('HIT', 'MISS', 'EXPIRED') "
                "AND outcome_date IS NOT NULL "
                "AND outcome_date > ? "
                "ORDER BY outcome_date ASC",
                (since_iso,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM predictions "
            "WHERE status IN ('HIT', 'MISS', 'EXPIRED') "
            "AND outcome_date IS NOT NULL "
            "ORDER BY outcome_date ASC"
        ).fetchall()
    raise ValueError(f"unknown table for ingestion: {table}")


def ingest_predictions(
    conn: sqlite3.Connection,
    store: MemoryStore,
    watermark_path: Path,
) -> IngestStats:
    """Push new prediction rows + closed outcomes into the memory store.

    Idempotent: maintains a watermark per source so subsequent runs only
    process new rows.
    """
    watermarks = _load_watermark(watermark_path)
    stats = IngestStats()

    pred_rows = _select_since(conn, "predictions", watermarks.get("predictions"))
    for row in pred_rows:
        try:
            store.add(_row_to_prediction_record(row))
            stats.predictions_added += 1
        except Exception as exc:
            logger.warning("ingest predictions row %s failed: %s", row["id"], exc)
            stats.errors += 1
    if pred_rows:
        watermarks["predictions"] = pred_rows[-1]["created_at"]

    outcome_rows = _select_since(conn, "outcomes", watermarks.get("outcomes"))
    for row in outcome_rows:
        try:
            store.add(_row_to_outcome_record(row))
            stats.outcomes_added += 1
        except Exception as exc:
            logger.warning("ingest outcomes row %s failed: %s", row["id"], exc)
            stats.errors += 1
    if outcome_rows:
        watermarks["outcomes"] = outcome_rows[-1]["outcome_date"]

    watermarks["last_run_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save_watermark(watermark_path, watermarks)
    return stats


def ingest_transmission_chains(
    sidecar_paths: Iterable[Path],
    store: MemoryStore,
) -> int:
    """Index the transmission chains emitted by `/expect` into mem0.

    Each ``state/last-outcome-expect.json`` (or older snapshots) lists picks
    with their {tech, news, risk} chain. Embed these for "find past trades
    that matched the same pattern" queries.

    Args:
        sidecar_paths: Iterable of JSON paths.
        store: MemoryStore.

    Returns:
        Count of chains successfully ingested.
    """
    count = 0
    for path in sidecar_paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning("skip sidecar %s: %s", path, exc)
            continue

        run_id = payload.get("run_id")
        generated_at = payload.get("generated_at")
        for pick in payload.get("picks", []):
            chain = pick.get("transmission_chain") or {}
            content = (
                f"{pick.get('ticker', '?')} {pick.get('label', '?')} "
                f"composite={pick.get('composite', '?')}: "
                f"TECH: {chain.get('tech', '—')}; "
                f"NEWS: {chain.get('news', '—')}; "
                f"RISK: {chain.get('risk', '—')}"
            )
            try:
                store.add(
                    MemoryRecord(
                        category="transmission_chains",
                        content=content,
                        metadata={
                            "ticker": pick.get("ticker"),
                            "market": pick.get("market"),
                            "label": pick.get("label"),
                            "composite": pick.get("composite"),
                            "run_id": run_id,
                            "generated_at": generated_at,
                            "analysis_group_id": pick.get("analysis_group_id"),
                        },
                    )
                )
                count += 1
            except Exception as exc:
                logger.warning(
                    "transmission chain ingest failed for %s: %s",
                    pick.get("ticker"),
                    exc,
                )
    return count
