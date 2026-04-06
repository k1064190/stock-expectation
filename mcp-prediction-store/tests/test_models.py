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
        ticker="AAPL", market="US", direction="BULL", confidence=0.6,
        timeframe="1M", reasoning="test", entry_price=150.0,
    )
    pred2 = Prediction(
        ticker="MSFT", market="US", direction="BEAR", confidence=0.7,
        timeframe="1W", reasoning="test", entry_price=400.0,
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
        ticker="AAPL", market="US", direction="BULL", confidence=0.6,
        timeframe="1W", reasoning="test", entry_price=150.0,
    )
    kr_pred = Prediction(
        ticker="005930", market="KR", direction="BULL", confidence=0.7,
        timeframe="2W", reasoning="test", entry_price=70000.0,
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
