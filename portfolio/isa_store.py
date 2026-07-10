"""ISA targets + decision-log persistence.

Two tables in the existing data/portfolio.db (WAL, CREATE TABLE IF NOT
EXISTS — same conventions as portfolio/db.py; no existing table is altered):

  - ``isa_targets``: append-only history of approved target allocations
    (latest row is the active target). ``allocation``/``etf_map`` are JSON.
  - ``isa_decisions``: append-only log of every ISA decision (contribution /
    rebalance / target_change) with inputs, the (possibly LLM-)proposed
    action, and the final clamped action — the audit trail behind the
    "LLM proposes, code disposes" gate.

Connections come from ``portfolio.db.get_connection``; callers pass the conn.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Optional

CREATE_ISA_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS isa_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    allocation TEXT NOT NULL,
    etf_map TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS isa_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('contribution','rebalance','target_change')),
    amount_krw INTEGER,
    inputs TEXT NOT NULL,
    proposal TEXT,
    final TEXT NOT NULL,
    notes TEXT
);
"""

# Weight-sum validation tolerance (float inputs like 33.3+33.3+33.4).
SUM_TOLERANCE = 0.01


def init_isa_tables(conn: sqlite3.Connection) -> None:
    """Create the ISA tables if absent (idempotent).

    Args:
        conn: portfolio.db connection (from portfolio.db.get_connection).
    """
    conn.executescript(CREATE_ISA_TABLES_SQL)
    conn.commit()


def save_target(
    conn: sqlite3.Connection,
    allocation: dict[str, float],
    etf_map: dict[str, str],
    note: Optional[str],
) -> int:
    """Store a new approved target allocation (becomes the active target).

    Args:
        conn: portfolio.db connection (ISA tables must exist).
        allocation: asset class → target weight in %, each within [0, 100],
            summing to 100 ±0.01.
        etf_map: asset class → ETF code; must cover exactly the allocation's
            classes (no missing, no extra).
        note: optional free-form approval note.

    Returns:
        The new isa_targets row id. Also logs a ``target_change`` decision.

    Raises:
        ValueError: a weight outside [0, 100], weights don't sum to 100, or
            etf_map coverage mismatch.
    """
    out_of_range = sorted(cls for cls, w in allocation.items() if not 0.0 <= w <= 100.0)
    if out_of_range:
        raise ValueError(
            f"allocation weights must be between 0 and 100 (offending: {out_of_range})"
        )
    total = sum(allocation.values())
    if abs(total - 100.0) > SUM_TOLERANCE:
        raise ValueError(f"allocation weights must sum to 100 (got {total:g})")
    missing = set(allocation) - set(etf_map)
    extra = set(etf_map) - set(allocation)
    if missing or extra:
        raise ValueError(
            "etf_map must cover exactly the allocation classes "
            f"(missing: {sorted(missing)}, extra: {sorted(extra)})"
        )
    now = datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO isa_targets (created_at, allocation, etf_map, note) "
        "VALUES (?, ?, ?, ?)",
        (now, json.dumps(allocation), json.dumps(etf_map), note),
    )
    target_id = cur.lastrowid
    conn.commit()
    log_decision(
        conn,
        kind="target_change",
        amount_krw=None,
        inputs={"target_id": target_id},
        proposal=None,
        final={"allocation": allocation, "etf_map": etf_map, "note": note},
        notes=[],
    )
    return target_id


def get_active_target(conn: sqlite3.Connection) -> Optional[dict]:
    """Return the latest stored target, JSON-decoded, or None if none exists.

    Args:
        conn: portfolio.db connection (ISA tables must exist).

    Returns:
        ``{"id", "created_at", "allocation", "etf_map", "note"}`` or None.
    """
    row = conn.execute("SELECT * FROM isa_targets ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "allocation": json.loads(row["allocation"]),
        "etf_map": json.loads(row["etf_map"]),
        "note": row["note"],
    }


def log_decision(
    conn: sqlite3.Connection,
    kind: str,
    amount_krw: Optional[int],
    inputs: dict,
    proposal: Optional[dict],
    final: dict,
    notes: list[str],
) -> int:
    """Append one decision to the audit log.

    Args:
        conn: portfolio.db connection (ISA tables must exist).
        kind: 'contribution' | 'rebalance' | 'target_change' (DB CHECK).
        amount_krw: contribution amount, if applicable.
        inputs: JSON-serializable snapshot of the decision inputs.
        proposal: the raw (e.g. LLM-)proposed action, or None.
        final: the final action after code-level clamps/validation.
        notes: visible notes attached to the decision.

    Returns:
        The new isa_decisions row id.
    """
    now = datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO isa_decisions "
        "(created_at, kind, amount_krw, inputs, proposal, final, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            now,
            kind,
            amount_krw,
            json.dumps(inputs),
            json.dumps(proposal) if proposal is not None else None,
            json.dumps(final),
            json.dumps(notes),
        ),
    )
    conn.commit()
    return cur.lastrowid


def list_decisions(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Return the newest decisions first, JSON fields decoded.

    Args:
        conn: portfolio.db connection (ISA tables must exist).
        limit: max rows (default 20).

    Returns:
        List of ``{"id", "created_at", "kind", "amount_krw", "inputs",
        "proposal", "final", "notes"}`` dicts.
    """
    rows = conn.execute(
        "SELECT * FROM isa_decisions ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {
            "id": r["id"],
            "created_at": r["created_at"],
            "kind": r["kind"],
            "amount_krw": r["amount_krw"],
            "inputs": json.loads(r["inputs"]),
            "proposal": (
                json.loads(r["proposal"]) if r["proposal"] is not None else None
            ),
            "final": json.loads(r["final"]),
            "notes": json.loads(r["notes"]) if r["notes"] is not None else [],
        }
        for r in rows
    ]
