"""Prediction schema and database management.

SQLite-backed storage for predictions with WAL mode for concurrent access.
"""

import json
import sqlite3
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional


class Direction(str, Enum):
    BULL = "BULL"
    BEAR = "BEAR"
    NEUTRAL = "NEUTRAL"


class Status(str, Enum):
    OPEN = "OPEN"
    HIT = "HIT"
    MISS = "MISS"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class Timeframe(str, Enum):
    ONE_WEEK = "1W"
    TWO_WEEKS = "2W"
    ONE_MONTH = "1M"
    THREE_MONTHS = "3M"


class Market(str, Enum):
    US = "US"
    KR = "KR"


class Source(str, Enum):
    LIVE = "LIVE"
    BACKTEST = "BACKTEST"
    INTERACTIVE = "INTERACTIVE"


# Trading days per timeframe
TIMEFRAME_TRADING_DAYS = {
    Timeframe.ONE_WEEK: 5,
    Timeframe.TWO_WEEKS: 10,
    Timeframe.ONE_MONTH: 21,
    Timeframe.THREE_MONTHS: 63,
}


@dataclass
class Prediction:
    """A single stock prediction with outcome tracking.

    Args:
        id: Unique identifier (UUID).
        created_at: When the prediction was created (UTC ISO format).
        ticker: Stock ticker (e.g., "NVDA" or "005930").
        market: "US" or "KR".
        direction: "BULL", "BEAR", or "NEUTRAL".
        confidence: Predicted probability of correctness (0.0-1.0).
        timeframe: "1W", "2W", "1M", or "3M".
        reasoning: Claude's analysis summary.
        entry_price: Stock price at prediction time.
        signals_used: List of analysis signals used.
        source: "LIVE", "BACKTEST", or "INTERACTIVE".
        target_price: Optional target price for HIT.
        stop_price: Optional stop price for MISS.
        status: Current prediction status.
        outcome_price: Final price when prediction closed.
        outcome_date: When the prediction was resolved.
        outcome_return: Percentage return from entry to outcome.
    """

    ticker: str
    market: str
    direction: str
    confidence: float
    timeframe: str
    reasoning: str
    entry_price: float
    signals_used: list[str] = field(default_factory=list)
    source: str = Source.LIVE.value
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    status: str = Status.OPEN.value
    outcome_price: Optional[float] = None
    outcome_date: Optional[str] = None
    outcome_return: Optional[float] = None


DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"

CREATE_TABLE_SQL = """
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


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode and busy timeout.

    Args:
        db_path: Path to the database file. Defaults to data/predictions.db.

    Returns:
        sqlite3.Connection with WAL mode and busy_timeout=5000.
    """
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(CREATE_TABLE_SQL)
    return conn


def insert_prediction(conn: sqlite3.Connection, pred: Prediction) -> Prediction:
    """Insert a new prediction into the database.

    Args:
        conn: SQLite connection.
        pred: Prediction to insert.

    Returns:
        The inserted Prediction.
    """
    conn.execute(
        """INSERT INTO predictions
           (id, created_at, ticker, market, direction, confidence, timeframe,
            reasoning, entry_price, signals_used, source, target_price, stop_price,
            status, outcome_price, outcome_date, outcome_return)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            pred.id,
            pred.created_at,
            pred.ticker,
            pred.market,
            pred.direction,
            pred.confidence,
            pred.timeframe,
            pred.reasoning,
            pred.entry_price,
            json.dumps(pred.signals_used),
            pred.source,
            pred.target_price,
            pred.stop_price,
            pred.status,
            pred.outcome_price,
            pred.outcome_date,
            pred.outcome_return,
        ),
    )
    conn.commit()
    return pred


def get_prediction(conn: sqlite3.Connection, pred_id: str) -> Optional[Prediction]:
    """Fetch a single prediction by ID.

    Args:
        conn: SQLite connection.
        pred_id: Prediction UUID.

    Returns:
        Prediction if found, None otherwise.
    """
    row = conn.execute(
        "SELECT * FROM predictions WHERE id = ?", (pred_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_prediction(row)


def list_predictions(
    conn: sqlite3.Connection,
    status: Optional[str] = None,
    market: Optional[str] = None,
    ticker: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 50,
) -> list[Prediction]:
    """Query predictions with optional filters.

    Args:
        conn: SQLite connection.
        status: Filter by status (OPEN, HIT, MISS, EXPIRED, CANCELLED).
        market: Filter by market (US, KR).
        ticker: Filter by ticker symbol.
        source: Filter by source (LIVE, BACKTEST, INTERACTIVE).
        limit: Maximum number of results.

    Returns:
        List of matching Predictions, ordered by created_at descending.
    """
    conditions = []
    params: list = []
    if status:
        conditions.append("status = ?")
        params.append(status)
    if market:
        conditions.append("market = ?")
        params.append(market)
    if ticker:
        conditions.append("ticker = ?")
        params.append(ticker)
    if source:
        conditions.append("source = ?")
        params.append(source)

    where = " AND ".join(conditions) if conditions else "1=1"
    query = f"SELECT * FROM predictions WHERE {where} ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [_row_to_prediction(r) for r in rows]


def update_prediction_outcome(
    conn: sqlite3.Connection,
    pred_id: str,
    status: str,
    outcome_price: float,
    outcome_return: float,
) -> bool:
    """Update a prediction with its outcome. Only OPEN predictions can be updated.

    Args:
        conn: SQLite connection.
        pred_id: Prediction UUID.
        status: New status (HIT, MISS, EXPIRED).
        outcome_price: Price at outcome time.
        outcome_return: Percentage return from entry.

    Returns:
        True if updated, False if prediction not found or not OPEN.
    """
    outcome_date = datetime.now(timezone.utc).isoformat()
    result = conn.execute(
        """UPDATE predictions
           SET status = ?, outcome_price = ?, outcome_date = ?, outcome_return = ?
           WHERE id = ? AND status = 'OPEN'""",
        (status, outcome_price, outcome_date, outcome_return, pred_id),
    )
    conn.commit()
    return result.rowcount > 0


def cancel_prediction(conn: sqlite3.Connection, pred_id: str) -> bool:
    """Cancel an open prediction. Only OPEN predictions can be cancelled.

    Args:
        conn: SQLite connection.
        pred_id: Prediction UUID.

    Returns:
        True if cancelled, False if not found or not OPEN.
    """
    result = conn.execute(
        "UPDATE predictions SET status = 'CANCELLED' WHERE id = ? AND status = 'OPEN'",
        (pred_id,),
    )
    conn.commit()
    return result.rowcount > 0


def _row_to_prediction(row: sqlite3.Row) -> Prediction:
    """Convert a database row to a Prediction dataclass."""
    return Prediction(
        id=row["id"],
        created_at=row["created_at"],
        ticker=row["ticker"],
        market=row["market"],
        direction=row["direction"],
        confidence=row["confidence"],
        timeframe=row["timeframe"],
        reasoning=row["reasoning"],
        entry_price=row["entry_price"],
        signals_used=json.loads(row["signals_used"]),
        source=row["source"],
        target_price=row["target_price"],
        stop_price=row["stop_price"],
        status=row["status"],
        outcome_price=row["outcome_price"],
        outcome_date=row["outcome_date"],
        outcome_return=row["outcome_return"],
    )
