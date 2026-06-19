"""Tests for scheduler.capstone_readiness (offline, no network)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import capstone_readiness as cr  # noqa: E402


def _seed_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE predictions (
               id TEXT, status TEXT, components TEXT
           )"""
    )
    rows = [
        ("a", "HIT", '{"algo": 6.0}'),  # qualifies
        ("b", "MISS", '{"algo": 3.0}'),  # qualifies
        ("c", "HIT", '{"algo": 5.0}'),  # qualifies
        ("d", "OPEN", '{"algo": 4.0}'),  # excluded: open
        ("e", "HIT", None),  # excluded: no components
        ("f", "MISS", '{"news": 1.0}'),  # excluded: no algo pillar
    ]
    conn.executemany("INSERT INTO predictions VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()


def test_count_components_closed_counts_only_qualifying(tmp_path):
    db = tmp_path / "predictions.db"
    _seed_db(db)
    total, hits, misses = cr.count_components_closed(db)
    assert (total, hits, misses) == (3, 2, 1)


def test_count_missing_db_returns_zeros(tmp_path):
    assert cr.count_components_closed(tmp_path / "nope.db") == (0, 0, 0)


def test_run_below_threshold_does_not_notify(tmp_path, monkeypatch):
    db = tmp_path / "predictions.db"
    _seed_db(db)
    monkeypatch.setattr(cr, "DB_PATH", db)
    monkeypatch.setattr(cr, "FLAG_PATH", tmp_path / "flag")
    sent = []
    monkeypatch.setattr(cr, "_threshold", lambda: 100)

    import telegram_sender

    monkeypatch.setattr(
        telegram_sender, "send_message", lambda *a, **k: sent.append(a) or True
    )

    out = cr.run()
    assert out["ready"] is False and sent == []
    assert not (tmp_path / "flag").exists()


def test_run_at_threshold_notifies_once_and_writes_flag(tmp_path, monkeypatch):
    db = tmp_path / "predictions.db"
    _seed_db(db)
    flag = tmp_path / "state" / "flag"
    monkeypatch.setattr(cr, "DB_PATH", db)
    monkeypatch.setattr(cr, "FLAG_PATH", flag)

    sent = []
    import telegram_sender

    monkeypatch.setattr(
        telegram_sender, "send_message", lambda *a, **k: sent.append(a[0]) or True
    )

    # threshold = 3 → exactly met by the seeded qualifying rows
    out = cr.run(threshold=3)
    assert out["ready"] is True and out.get("notified") is True
    assert len(sent) == 1 and "capstone" in sent[0].lower()
    assert flag.exists()

    # second run: flag present → no duplicate ping
    out2 = cr.run(threshold=3)
    assert out2["already_notified"] is True
    assert len(sent) == 1
