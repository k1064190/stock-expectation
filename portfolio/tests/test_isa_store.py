"""Tests for ISA targets + decision-log persistence (portfolio.isa_store)."""

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portfolio.db import get_connection  # noqa: E402
from portfolio.isa_store import (  # noqa: E402
    get_active_target,
    init_isa_tables,
    list_decisions,
    log_decision,
    save_target,
)

ALLOCATION = {"overseas_equity": 50.0, "bond": 30.0, "gold": 20.0}
ETF_MAP = {"overseas_equity": "360750", "bond": "114260", "gold": "411060"}


@pytest.fixture
def conn():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    c = get_connection(db_path)
    init_isa_tables(c)
    yield c
    c.close()
    db_path.unlink(missing_ok=True)


def test_save_target_roundtrip_and_decision_logged(conn):
    tid = save_target(conn, ALLOCATION, ETF_MAP, note="approved 2026-07-10")
    assert isinstance(tid, int)
    active = get_active_target(conn)
    assert active["allocation"] == ALLOCATION
    assert active["etf_map"] == ETF_MAP
    assert active["note"] == "approved 2026-07-10"
    # save_target also logs a target_change decision
    decisions = list_decisions(conn)
    assert len(decisions) == 1
    assert decisions[0]["kind"] == "target_change"
    assert decisions[0]["final"]["allocation"] == ALLOCATION


def test_get_active_target_returns_latest(conn):
    save_target(conn, ALLOCATION, ETF_MAP, note="v1")
    alloc2 = {"overseas_equity": 60.0, "bond": 40.0}
    map2 = {"overseas_equity": "360750", "bond": "114260"}
    save_target(conn, alloc2, map2, note="v2")
    active = get_active_target(conn)
    assert active["allocation"] == alloc2 and active["note"] == "v2"


def test_get_active_target_none_when_empty(conn):
    assert get_active_target(conn) is None


def test_save_target_validates_sum(conn):
    with pytest.raises(ValueError, match="sum"):
        save_target(conn, {"a": 50.0, "b": 40.0}, {"a": "1", "b": "2"}, None)


def test_save_target_validates_map_coverage(conn):
    with pytest.raises(ValueError, match="etf_map"):
        save_target(conn, {"a": 50.0, "b": 50.0}, {"a": "1"}, None)  # missing b
    with pytest.raises(ValueError, match="etf_map"):
        save_target(
            conn, {"a": 50.0, "b": 50.0}, {"a": "1", "b": "2", "c": "3"}, None
        )  # extra c


def test_log_and_list_decisions_roundtrip(conn):
    did = log_decision(
        conn,
        kind="contribution",
        amount_krw=1_000_000,
        inputs={"targets": ALLOCATION},
        proposal={"tilt": {"bond": -5.0}},
        final={"buys_by_class": {"bond": 1_000_000}},
        notes=["tilt applied"],
    )
    assert isinstance(did, int)
    rows = list_decisions(conn)
    assert len(rows) == 1
    d = rows[0]
    assert d["kind"] == "contribution"
    assert d["amount_krw"] == 1_000_000
    assert d["inputs"] == {"targets": ALLOCATION}
    assert d["proposal"] == {"tilt": {"bond": -5.0}}
    assert d["final"] == {"buys_by_class": {"bond": 1_000_000}}
    assert d["notes"] == ["tilt applied"]


def test_list_decisions_newest_first_and_limit(conn):
    for i in range(3):
        log_decision(
            conn,
            kind="rebalance",
            amount_krw=None,
            inputs={"i": i},
            proposal=None,
            final={"i": i},
            notes=[],
        )
    rows = list_decisions(conn, limit=2)
    assert len(rows) == 2
    assert rows[0]["inputs"]["i"] == 2  # newest first
    assert rows[0]["proposal"] is None


def test_kind_check_constraint(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO isa_decisions "
            "(created_at, kind, amount_krw, inputs, proposal, final, notes) "
            "VALUES ('2026-07-10', 'bogus', NULL, '{}', NULL, '{}', '[]')"
        )
