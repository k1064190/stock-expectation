"""Tests for prediction store models and database operations."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import (
    Prediction,
    get_connection,
    insert_prediction,
    get_prediction,
    list_predictions,
    update_prediction_outcome,
    cancel_prediction,
    validate_prediction_dict,
)


@pytest.fixture
def db_conn():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    conn = get_connection(db_path)
    yield conn
    conn.close()
    db_path.unlink(missing_ok=True)
    # Clean up WAL files
    Path(str(db_path) + "-wal").unlink(missing_ok=True)
    Path(str(db_path) + "-shm").unlink(missing_ok=True)


@pytest.fixture
def sample_prediction():
    """Create a sample prediction for testing."""
    return Prediction(
        ticker="NVDA",
        market="US",
        direction="BULL",
        confidence=0.75,
        timeframe="1W",
        reasoning="Strong technical breakout with volume confirmation",
        entry_price=120.0,
        signals_used=["technical", "breadth"],
        source="INTERACTIVE",
        target_price=130.0,
        stop_price=115.0,
    )


def test_insert_and_get_prediction(db_conn, sample_prediction):
    """Insert a prediction and retrieve it by ID."""
    inserted = insert_prediction(db_conn, sample_prediction)
    assert inserted.id == sample_prediction.id

    fetched = get_prediction(db_conn, sample_prediction.id)
    assert fetched is not None
    assert fetched.ticker == "NVDA"
    assert fetched.market == "US"
    assert fetched.direction == "BULL"
    assert fetched.confidence == 0.75
    assert fetched.entry_price == 120.0
    assert fetched.signals_used == ["technical", "breadth"]
    assert fetched.status == "OPEN"


def test_get_nonexistent_prediction(db_conn):
    """Getting a non-existent prediction returns None."""
    result = get_prediction(db_conn, "nonexistent-id")
    assert result is None


def test_list_predictions_no_filter(db_conn, sample_prediction):
    """List predictions without filters."""
    insert_prediction(db_conn, sample_prediction)
    results = list_predictions(db_conn)
    assert len(results) == 1
    assert results[0].ticker == "NVDA"


def test_list_predictions_with_status_filter(db_conn):
    """Filter predictions by status."""
    pred1 = Prediction(
        ticker="AAPL",
        market="US",
        direction="BULL",
        confidence=0.6,
        timeframe="1M",
        reasoning="test",
        entry_price=150.0,
    )
    pred2 = Prediction(
        ticker="MSFT",
        market="US",
        direction="BEAR",
        confidence=0.7,
        timeframe="1W",
        reasoning="test",
        entry_price=400.0,
        # INTERACTIVE: LIVE BEAR is hard-rejected by the store gate.
        source="INTERACTIVE",
    )
    insert_prediction(db_conn, pred1)
    insert_prediction(db_conn, pred2)
    update_prediction_outcome(db_conn, pred1.id, "HIT", 160.0, 6.67)

    open_preds = list_predictions(db_conn, status="OPEN")
    assert len(open_preds) == 1
    assert open_preds[0].ticker == "MSFT"

    hit_preds = list_predictions(db_conn, status="HIT")
    assert len(hit_preds) == 1
    assert hit_preds[0].ticker == "AAPL"


def test_list_predictions_with_market_filter(db_conn):
    """Filter predictions by market."""
    us_pred = Prediction(
        ticker="AAPL",
        market="US",
        direction="BULL",
        confidence=0.6,
        timeframe="1W",
        reasoning="test",
        entry_price=150.0,
    )
    kr_pred = Prediction(
        ticker="005930",
        market="KR",
        direction="BULL",
        confidence=0.7,
        timeframe="2W",
        reasoning="test",
        entry_price=70000.0,
    )
    insert_prediction(db_conn, us_pred)
    insert_prediction(db_conn, kr_pred)

    us_results = list_predictions(db_conn, market="US")
    assert len(us_results) == 1
    assert us_results[0].ticker == "AAPL"

    kr_results = list_predictions(db_conn, market="KR")
    assert len(kr_results) == 1
    assert kr_results[0].ticker == "005930"


def test_update_prediction_outcome(db_conn, sample_prediction):
    """Update an open prediction with outcome data."""
    insert_prediction(db_conn, sample_prediction)

    success = update_prediction_outcome(
        db_conn, sample_prediction.id, "HIT", 130.0, 8.33
    )
    assert success is True

    updated = get_prediction(db_conn, sample_prediction.id)
    assert updated.status == "HIT"
    assert updated.outcome_price == 130.0
    assert updated.outcome_return == 8.33
    assert updated.outcome_date is not None


def test_update_closed_prediction_fails(db_conn, sample_prediction):
    """Cannot update a prediction that is already closed."""
    insert_prediction(db_conn, sample_prediction)
    update_prediction_outcome(db_conn, sample_prediction.id, "HIT", 130.0, 8.33)

    # Try to update again — should fail
    success = update_prediction_outcome(
        db_conn, sample_prediction.id, "MISS", 110.0, -8.33
    )
    assert success is False

    # Status should still be HIT
    pred = get_prediction(db_conn, sample_prediction.id)
    assert pred.status == "HIT"


def test_cancel_prediction(db_conn, sample_prediction):
    """Cancel an open prediction."""
    insert_prediction(db_conn, sample_prediction)

    success = cancel_prediction(db_conn, sample_prediction.id)
    assert success is True

    pred = get_prediction(db_conn, sample_prediction.id)
    assert pred.status == "CANCELLED"


def test_cancel_closed_prediction_fails(db_conn, sample_prediction):
    """Cannot cancel a prediction that is already closed."""
    insert_prediction(db_conn, sample_prediction)
    update_prediction_outcome(db_conn, sample_prediction.id, "MISS", 110.0, -8.33)

    success = cancel_prediction(db_conn, sample_prediction.id)
    assert success is False


def test_wal_mode_enabled(db_conn):
    """Verify WAL journal mode is active."""
    mode = db_conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_busy_timeout_set(db_conn):
    """Verify busy timeout is configured."""
    timeout = db_conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout == 5000


def test_analysis_group_id_roundtrip(db_conn):
    """Two predictions sharing an analysis_group_id roundtrip correctly."""
    group_id = "group-uuid-test-1"
    pred_short = Prediction(
        ticker="MU",
        market="US",
        direction="BULL",
        confidence=0.72,
        timeframe="1W",
        reasoning="short-term",
        entry_price=426.56,
        analysis_group_id=group_id,
    )
    pred_cycle = Prediction(
        ticker="MU",
        market="US",
        direction="BEAR",
        confidence=0.65,
        timeframe="1Y",
        reasoning="cycle",
        entry_price=426.56,
        analysis_group_id=group_id,
        # INTERACTIVE: LIVE BEAR is hard-rejected by the store gate.
        source="INTERACTIVE",
    )
    insert_prediction(db_conn, pred_short)
    insert_prediction(db_conn, pred_cycle)

    fetched_short = get_prediction(db_conn, pred_short.id)
    fetched_cycle = get_prediction(db_conn, pred_cycle.id)
    assert fetched_short.analysis_group_id == group_id
    assert fetched_cycle.analysis_group_id == group_id

    rows = db_conn.execute(
        "SELECT id FROM predictions WHERE analysis_group_id = ?", (group_id,)
    ).fetchall()
    assert len(rows) == 2


def test_analysis_group_id_nullable(db_conn, sample_prediction):
    """Existing behavior: analysis_group_id is optional and defaults to None."""
    insert_prediction(db_conn, sample_prediction)
    fetched = get_prediction(db_conn, sample_prediction.id)
    assert fetched.analysis_group_id is None


def test_timeframe_6m_accepted(db_conn):
    """Schema accepts the newly added 6M timeframe."""
    pred = Prediction(
        ticker="NVDA",
        market="US",
        direction="BULL",
        confidence=0.65,
        timeframe="6M",
        reasoning="medium-term trend",
        entry_price=120.0,
    )
    insert_prediction(db_conn, pred)
    fetched = get_prediction(db_conn, pred.id)
    assert fetched is not None
    assert fetched.timeframe == "6M"


# ---------------------------------------------------------------------------
# Stage 2: store-layer LIVE BEAR gate + same-day duplicate guard
# ---------------------------------------------------------------------------


def _pred(ticker="NVDA", direction="BULL", timeframe="1W", source="LIVE"):
    return Prediction(
        ticker=ticker,
        market="US",
        direction=direction,
        confidence=0.6,
        timeframe=timeframe,
        reasoning="test",
        entry_price=100.0,
        source=source,
    )


def test_live_bear_is_rejected(db_conn):
    """LIVE BEAR predictions are hard-gated (measured win rate ~0%)."""
    with pytest.raises(ValueError, match="BEAR"):
        insert_prediction(db_conn, _pred(direction="BEAR", source="LIVE"))


def test_interactive_bear_is_allowed(db_conn):
    """INTERACTIVE BEAR (manual override) is still permitted."""
    pred = _pred(direction="BEAR", source="INTERACTIVE")
    insert_prediction(db_conn, pred)
    assert get_prediction(db_conn, pred.id) is not None


def test_live_bull_is_allowed(db_conn):
    """The gate only blocks BEAR — LIVE BULL is unaffected."""
    pred = _pred(direction="BULL", source="LIVE")
    insert_prediction(db_conn, pred)
    assert get_prediction(db_conn, pred.id) is not None


def test_duplicate_same_day_open_is_rejected(db_conn):
    """A second OPEN row with the same (ticker, market, direction, timeframe,
    source, created date) is a same-day duplicate and is rejected."""
    insert_prediction(db_conn, _pred(ticker="AMD", timeframe="1W"))
    with pytest.raises(ValueError, match="duplicate"):
        insert_prediction(db_conn, _pred(ticker="AMD", timeframe="1W"))


def test_duplicate_different_timeframe_is_allowed(db_conn):
    """Same ticker, different timeframe is a normal multi-horizon row."""
    insert_prediction(db_conn, _pred(ticker="AMD", timeframe="1W"))
    insert_prediction(db_conn, _pred(ticker="AMD", timeframe="1M"))
    assert len(list_predictions(db_conn, ticker="AMD")) == 2


def test_create_schema_self_heals_legacy_open_duplicates():
    """A legacy DB with OPEN duplicates is cleaned, not bricked, on connect."""
    import models

    raw = sqlite3.connect(":memory:")
    raw.execute(models.CREATE_TABLE_STMT)
    # Two OPEN rows with the same dedup key (simulates a pre-guard database).
    for i in (1, 2):
        raw.execute(
            "INSERT INTO predictions (id, created_at, ticker, market, direction, "
            "confidence, timeframe, reasoning, entry_price, signals_used, source, "
            "status) VALUES (?, ?, 'AMD', 'US', 'BULL', 0.6, '1W', 'x', 100.0, "
            "'[]', 'LIVE', 'OPEN')",
            (f"id{i}", f"2026-05-18T0{i}:00:00+00:00"),
        )
    raw.commit()

    # Should NOT raise despite the duplicate OPEN rows.
    models._create_schema(raw)
    models._ensure_open_dedup_index(raw)

    remaining = raw.execute(
        "SELECT COUNT(*) FROM predictions WHERE ticker='AMD'"
    ).fetchone()[0]
    assert remaining == 1  # collapsed to the earliest
    idx = raw.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='idx_predictions_open_dedup'"
    ).fetchone()
    assert idx is not None
    raw.close()


def test_legacy_migration_with_open_dupes_does_not_brick():
    """A legacy DB (no analysis_group_id) holding OPEN duplicates migrates and
    self-heals via get_connection instead of failing the UNIQUE copy."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        legacy = sqlite3.connect(str(db_path))
        # Legacy schema: no analysis_group_id column, no dedup index.
        legacy.execute(
            "CREATE TABLE predictions ("
            "id TEXT PRIMARY KEY, created_at TEXT, ticker TEXT, market TEXT, "
            "direction TEXT, confidence REAL, timeframe TEXT, reasoning TEXT, "
            "entry_price REAL, signals_used TEXT, source TEXT, target_price REAL, "
            "stop_price REAL, status TEXT, outcome_price REAL, outcome_date TEXT, "
            "outcome_return REAL)"
        )
        for i in (1, 2):
            legacy.execute(
                "INSERT INTO predictions (id, created_at, ticker, market, "
                "direction, confidence, timeframe, reasoning, entry_price, "
                "signals_used, source, status) VALUES (?, ?, 'AMD', 'US', 'BULL', "
                "0.6, '1W', 'x', 100.0, '[]', 'LIVE', 'OPEN')",
                (f"id{i}", f"2026-05-18T0{i}:00:00+00:00"),
            )
        legacy.commit()
        legacy.close()

        # Should migrate (add analysis_group_id) AND dedup the OPEN rows.
        # Simulate a read-only first caller (no writes/commit of its own) to
        # confirm the self-heal cleanup + index are committed, not rolled back.
        conn = get_connection(db_path)
        conn.close()

        # Reopen: the dedup and the UNIQUE index must have persisted.
        conn = get_connection(db_path)
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM predictions WHERE ticker='AMD'"
                ).fetchone()[0]
                == 1
            )
            assert (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' "
                    "AND name='idx_predictions_open_dedup'"
                ).fetchone()
                is not None
            )
        finally:
            conn.close()
    finally:
        db_path.unlink(missing_ok=True)
        Path(str(db_path) + "-wal").unlink(missing_ok=True)
        Path(str(db_path) + "-shm").unlink(missing_ok=True)


def test_open_dedup_unique_index_exists(db_conn):
    """The partial UNIQUE index backstops the same-day duplicate guard."""
    row = db_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='idx_predictions_open_dedup'"
    ).fetchone()
    assert row is not None


def test_duplicate_allowed_after_prior_closed(db_conn):
    """Once the prior prediction is closed, a fresh same-key one is allowed."""
    first = _pred(ticker="AMD", timeframe="1W")
    insert_prediction(db_conn, first)
    update_prediction_outcome(db_conn, first.id, "HIT", 110.0, 10.0)
    second = _pred(ticker="AMD", timeframe="1W")
    insert_prediction(db_conn, second)
    assert get_prediction(db_conn, second.id) is not None


def test_timeframe_1y_accepted(db_conn):
    """Schema accepts the newly added 1Y timeframe."""
    pred = Prediction(
        ticker="NVDA",
        market="US",
        direction="BEAR",
        confidence=0.65,
        timeframe="1Y",
        reasoning="cycle peak risk",
        entry_price=120.0,
        # INTERACTIVE: LIVE BEAR is hard-rejected by the store gate.
        source="INTERACTIVE",
    )
    insert_prediction(db_conn, pred)
    fetched = get_prediction(db_conn, pred.id)
    assert fetched is not None
    assert fetched.timeframe == "1Y"


def test_timeframe_invalid_rejected(db_conn):
    """Schema still rejects timeframe values outside the enum."""
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            """INSERT INTO predictions
               (id, created_at, ticker, market, direction, confidence, timeframe,
                reasoning, entry_price, signals_used, source, status)
               VALUES ('tf-bad', '2026-01-01', 'AAPL', 'US', 'BULL', 0.5,
                       '5Y', 'test', 100.0, '[]', 'LIVE', 'OPEN')"""
        )


def test_prediction_schema_constraints(db_conn):
    """Schema rejects invalid market/direction/status values."""
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            """INSERT INTO predictions
               (id, created_at, ticker, market, direction, confidence, timeframe,
                reasoning, entry_price, signals_used, source, status)
               VALUES ('test', '2026-01-01', 'AAPL', 'INVALID', 'BULL', 0.5,
                       '1W', 'test', 100.0, '[]', 'LIVE', 'OPEN')"""
        )

    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            """INSERT INTO predictions
               (id, created_at, ticker, market, direction, confidence, timeframe,
                reasoning, entry_price, signals_used, source, status)
               VALUES ('test2', '2026-01-01', 'AAPL', 'US', 'BULL', 1.5,
                       '1W', 'test', 100.0, '[]', 'LIVE', 'OPEN')"""
        )


# ---------------------------------------------------------------------------
# Legacy-schema migration (codex P1 finding on PR #12/#13)
# ---------------------------------------------------------------------------


# Exact schema before the multi-horizon /expect redesign — no
# ``analysis_group_id`` column, ``timeframe`` CHECK only allows the four
# short horizons. Anyone whose ``predictions.db`` was created before
# this PR carries this schema; ``_migrate_schema_if_needed`` must
# transparently upgrade them to the current one.
_LEGACY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS predictions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    market TEXT NOT NULL CHECK(market IN ('US', 'KR')),
    direction TEXT NOT NULL CHECK(direction IN ('BULL', 'BEAR', 'NEUTRAL')),
    confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
    timeframe TEXT NOT NULL CHECK(timeframe IN ('1W', '2W', '1M', '3M')),
    reasoning TEXT NOT NULL,
    entry_price REAL NOT NULL,
    signals_used TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'LIVE' CHECK(source IN ('LIVE', 'BACKTEST', 'INTERACTIVE')),
    target_price REAL,
    stop_price REAL,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN', 'HIT', 'MISS', 'EXPIRED', 'CANCELLED')),
    outcome_price REAL,
    outcome_date TEXT,
    outcome_return REAL
);

CREATE INDEX IF NOT EXISTS idx_predictions_status ON predictions(status);
CREATE INDEX IF NOT EXISTS idx_predictions_market ON predictions(market);
CREATE INDEX IF NOT EXISTS idx_predictions_ticker ON predictions(ticker);
CREATE INDEX IF NOT EXISTS idx_predictions_created ON predictions(created_at);
"""


@pytest.fixture
def legacy_db_path():
    """A predictions.db file that starts on the pre-v2 schema and carries
    one realistic row. The fixture deliberately bypasses ``get_connection``
    so the migration code path is exercised by the test, not bypassed by
    the fixture setup."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    raw = sqlite3.connect(str(path))
    raw.executescript(_LEGACY_SCHEMA_SQL)
    raw.execute(
        """INSERT INTO predictions
           (id, created_at, ticker, market, direction, confidence, timeframe,
            reasoning, entry_price, signals_used, source, status)
           VALUES ('legacy-1', '2026-04-01T00:00:00', 'NVDA', 'US', 'BULL',
                   0.7, '1W', 'pre-v2 row', 120.0, '[]', 'LIVE', 'OPEN')"""
    )
    raw.commit()
    raw.close()
    yield path
    path.unlink(missing_ok=True)
    Path(str(path) + "-wal").unlink(missing_ok=True)
    Path(str(path) + "-shm").unlink(missing_ok=True)


def test_migration_adds_analysis_group_id_column(legacy_db_path):
    """get_connection() on a legacy DB transparently upgrades the schema
    so the new column is present and queryable."""
    conn = get_connection(legacy_db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(predictions)")}
        assert "analysis_group_id" in cols
    finally:
        conn.close()


def test_migration_widens_timeframe_check(legacy_db_path):
    """Post-migration, inserts with the new 6M/1Y timeframes succeed."""
    conn = get_connection(legacy_db_path)
    try:
        for tf in ("6M", "1Y"):
            conn.execute(
                """INSERT INTO predictions
                   (id, created_at, ticker, market, direction, confidence,
                    timeframe, reasoning, entry_price, signals_used, source,
                    status)
                   VALUES (?, '2026-05-14', 'AAPL', 'US', 'BULL', 0.65, ?,
                           'migration test', 200.0, '[]', 'LIVE', 'OPEN')""",
                (f"new-{tf}", tf),
            )
        conn.commit()
        timeframes = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT timeframe FROM predictions WHERE id LIKE 'new-%'"
            )
        }
        assert timeframes == {"6M", "1Y"}
    finally:
        conn.close()


def test_migration_preserves_existing_rows(legacy_db_path):
    """Legacy rows survive the table swap intact."""
    conn = get_connection(legacy_db_path)
    try:
        row = conn.execute("SELECT * FROM predictions WHERE id = 'legacy-1'").fetchone()
        assert row is not None
        assert row["ticker"] == "NVDA"
        assert row["entry_price"] == 120.0
        assert row["timeframe"] == "1W"
        # The new column is NULL for legacy rows.
        assert row["analysis_group_id"] is None
    finally:
        conn.close()


def test_migration_is_idempotent(legacy_db_path):
    """Calling get_connection() twice — once to migrate, once after —
    must not double-swap or corrupt indexes."""
    conn = get_connection(legacy_db_path)
    conn.close()
    # Second call: schema is now current, so _migrate_schema_if_needed
    # must early-return and the table must remain healthy.
    conn = get_connection(legacy_db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(predictions)")}
        assert "analysis_group_id" in cols
        # Index for the new column is present (was missing in legacy schema).
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='predictions'"
            )
        }
        assert "idx_predictions_group" in indexes
        # Row count unchanged from the one legacy row.
        (n,) = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()
        assert n == 1
    finally:
        conn.close()


def test_migration_rollback_preserves_legacy_data(legacy_db_path, monkeypatch):
    """If something raises mid-migration (e.g. the new-schema CREATE fails),
    the ``with conn:`` block must roll back and leave the legacy table
    fully intact — including its row count. This regression-tests the
    critical fix that swapped ``conn.executescript`` (which auto-commits)
    for ``conn.execute`` per-statement (which stays inside the
    transaction)."""
    import models as _models

    real_create = _models._create_schema

    def boom(_conn):
        # First call is from get_connection's post-migration step; we
        # want the migration-internal call to fail. Easiest path: fail
        # on every call and verify the migration block rolled back.
        real_create  # touch to keep linters quiet
        raise sqlite3.OperationalError("simulated DDL failure")

    monkeypatch.setattr(_models, "_create_schema", boom)

    with pytest.raises(sqlite3.OperationalError):
        _models.get_connection(legacy_db_path)

    # Reopen with the real implementation restored. If the rollback
    # worked, the legacy table is still here with its original row;
    # if it didn't, the DROP would have committed and the row is gone.
    monkeypatch.setattr(_models, "_create_schema", real_create)
    conn = _models.get_connection(legacy_db_path)
    try:
        row = conn.execute(
            "SELECT id, ticker, entry_price FROM predictions WHERE id = 'legacy-1'"
        ).fetchone()
        assert row is not None, (
            "legacy row vanished — migration rollback failed; the DROP "
            "committed before the CREATE failure"
        )
        assert row["ticker"] == "NVDA"
        # Migration now succeeds (real _create_schema), so the new column
        # is present on this second pass.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(predictions)")}
        assert "analysis_group_id" in cols
    finally:
        conn.close()


def test_migration_fresh_db_is_noop():
    """A brand-new DB hits the migration function but returns immediately
    (no rows to copy, no legacy schema to swap). Verifies the early-return
    branches don't mis-fire on first-time installs."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    try:
        conn = get_connection(path)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(predictions)")}
            assert "analysis_group_id" in cols
            (n,) = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()
            assert n == 0
        finally:
            conn.close()
    finally:
        path.unlink(missing_ok=True)
        Path(str(path) + "-wal").unlink(missing_ok=True)
        Path(str(path) + "-shm").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# S9: validate_prediction_dict — validate, don't just request, the JSON contract
# ---------------------------------------------------------------------------


def _valid_pred_dict():
    return {
        "ticker": "NVDA",
        "market": "US",
        "direction": "BULL",
        "confidence": 0.7,
        "timeframe": "1W",
        "reasoning": "RSI 62, MA20 stack",
        "entry_price": 130.5,
        "target_price": 138.0,
        "stop_price": 125.0,
    }


def test_validate_prediction_dict_accepts_valid():
    assert validate_prediction_dict(_valid_pred_dict()) == []


def test_validate_prediction_dict_missing_required():
    p = _valid_pred_dict()
    del p["entry_price"]
    errors = validate_prediction_dict(p)
    assert any("entry_price" in e for e in errors)


def test_validate_prediction_dict_bad_enum():
    p = _valid_pred_dict()
    p["direction"] = "UP"
    p["market"] = "JP"
    p["timeframe"] = "5Y"
    errors = validate_prediction_dict(p)
    assert any("direction" in e for e in errors)
    assert any("market" in e for e in errors)
    assert any("timeframe" in e for e in errors)


def test_validate_prediction_dict_confidence_range():
    p = _valid_pred_dict()
    p["confidence"] = 1.5
    assert any("confidence" in e for e in validate_prediction_dict(p))


def test_validate_prediction_dict_nonpositive_entry():
    p = _valid_pred_dict()
    p["entry_price"] = 0
    assert any("entry_price" in e for e in validate_prediction_dict(p))


def test_validate_prediction_dict_optional_prices_may_be_omitted():
    p = _valid_pred_dict()
    del p["target_price"]
    del p["stop_price"]
    assert validate_prediction_dict(p) == []


def test_validate_prediction_dict_rejects_nan_inf_prices():
    p = _valid_pred_dict()
    p["entry_price"] = float("nan")
    assert any("entry_price" in e for e in validate_prediction_dict(p))
    p2 = _valid_pred_dict()
    p2["target_price"] = float("inf")
    assert any("target_price" in e for e in validate_prediction_dict(p2))
