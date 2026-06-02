"""Prediction schema and database management.

SQLite-backed storage for predictions with WAL mode for concurrent access.
"""

import json
import logging
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
    SIX_MONTHS = "6M"
    ONE_YEAR = "1Y"


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
    Timeframe.SIX_MONTHS: 126,
    Timeframe.ONE_YEAR: 252,
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
        timeframe: "1W", "2W", "1M", "3M", "6M", or "1Y".
        reasoning: Claude's analysis summary.
        entry_price: Stock price at prediction time.
        signals_used: List of analysis signals used.
        source: "LIVE", "BACKTEST", or "INTERACTIVE".
        target_price: Optional target price for HIT.
        stop_price: Optional stop price for MISS.
        status: Current prediction status.
        outcome_price: Final price when prediction closed.
        outcome_date: When the prediction was resolved.
        outcome_return: Position return (%) from entry to outcome. Sign is
            relative to the predicted direction: for BEAR a price drop is a
            positive return. BULL/NEUTRAL equal the raw price change.
        analysis_group_id: Optional UUID shared by predictions that come
            from the same multi-horizon analysis (e.g. the 4 horizons emitted
            by a single ``/expect MU`` run). Legacy predictions have None.
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
    analysis_group_id: Optional[str] = None


DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"

CREATE_TABLE_STMT = """
CREATE TABLE IF NOT EXISTS predictions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    market TEXT NOT NULL CHECK(market IN ('US', 'KR')),
    direction TEXT NOT NULL CHECK(direction IN ('BULL', 'BEAR', 'NEUTRAL')),
    confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
    timeframe TEXT NOT NULL CHECK(timeframe IN ('1W', '2W', '1M', '3M', '6M', '1Y')),
    reasoning TEXT NOT NULL,
    entry_price REAL NOT NULL,
    signals_used TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'LIVE' CHECK(source IN ('LIVE', 'BACKTEST', 'INTERACTIVE')),
    target_price REAL,
    stop_price REAL,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN', 'HIT', 'MISS', 'EXPIRED', 'CANCELLED')),
    outcome_price REAL,
    outcome_date TEXT,
    outcome_return REAL,
    analysis_group_id TEXT
)
"""

CREATE_INDEX_STMTS = (
    "CREATE INDEX IF NOT EXISTS idx_predictions_status ON predictions(status)",
    "CREATE INDEX IF NOT EXISTS idx_predictions_market ON predictions(market)",
    "CREATE INDEX IF NOT EXISTS idx_predictions_ticker ON predictions(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_predictions_created ON predictions(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_predictions_group ON predictions(analysis_group_id)",
    # Partial UNIQUE index = atomic backstop for the same-day duplicate guard in
    # insert_prediction (the SELECT-then-INSERT check is not race-safe under
    # concurrent writers). Scoped to OPEN rows so a fresh prediction is allowed
    # once the prior same-key one closes.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_predictions_open_dedup "
    "ON predictions(ticker, market, direction, timeframe, source, date(created_at)) "
    "WHERE status = 'OPEN'",
)


def _create_schema(conn: sqlite3.Connection) -> None:
    """Run the table + index DDL via ``execute`` (not ``executescript``).

    Why not ``executescript``: it implicitly commits the surrounding
    transaction per Python's sqlite3 contract, which would defeat the
    atomicity guarantee of the migration's ``with conn:`` block. By
    issuing each statement individually we stay inside whatever
    transaction the caller opened.
    """
    conn.execute(CREATE_TABLE_STMT)
    for stmt in CREATE_INDEX_STMTS:
        conn.execute(stmt)


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode and busy timeout.

    Runs ``_migrate_schema_if_needed`` after the idempotent CREATE so
    pre-v2 ``predictions.db`` files (no ``analysis_group_id``, old
    timeframe CHECK) get upgraded transparently.

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
    # Migration must run BEFORE the schema-create — the index on
    # analysis_group_id references the new column and would fail with
    # ``no such column`` on a legacy DB if we created indexes first.
    _migrate_schema_if_needed(conn)
    _create_schema(conn)
    return conn


def _migrate_schema_if_needed(conn: sqlite3.Connection) -> None:
    """Bring a legacy ``predictions`` table up to the current schema.

    Two changes vs the pre-v2 schema:

    1. New ``analysis_group_id TEXT`` column (nullable) so all the
       horizons emitted by one ``/expect`` run can be grouped together.
    2. ``timeframe`` CHECK constraint widened to include ``6M`` and ``1Y``.

    SQLite cannot modify a CHECK constraint in place, so the migration
    is a full table swap: snapshot the legacy rows into a TEMP table,
    drop the legacy table (cascading its indexes), re-create the schema,
    then copy rows back. The whole thing runs inside a single
    ``with conn:`` transaction.

    Critical detail: every statement inside the transaction uses
    ``conn.execute``, NEVER ``conn.executescript``. Python's sqlite3
    contract has ``executescript`` implicitly commit any open
    transaction *before* it runs, which would silently break the
    ``with conn:`` rollback guarantee — a DROP followed by a failing
    INSERT would leave the table permanently gone. The ``_create_schema``
    helper uses per-statement ``execute`` for exactly this reason.

    The presence of ``analysis_group_id`` is the migration flag — if
    it is already a column, the function is a no-op. Idempotent across
    repeated calls and cron runs.
    """
    logger = logging.getLogger(__name__)
    try:
        cur = conn.execute("PRAGMA table_info(predictions)")
        cols = {row[1] for row in cur.fetchall()}
    except sqlite3.DatabaseError as exc:
        # PRAGMA failure here usually means a corrupt or locked DB.
        # Surface the error rather than silently treating it as a
        # brand-new DB and risking a wrong-schema CREATE on top.
        logger.error("Failed to read predictions table schema: %s", exc)
        raise

    if not cols:
        # Brand-new DB — the caller's ``_create_schema`` will handle it.
        return
    if "analysis_group_id" in cols:
        return  # Already on current schema.

    logger.info(
        "predictions schema migration: adding analysis_group_id + "
        "widening timeframe CHECK (legacy schema detected)"
    )

    # Python 3.11 sqlite3 has a long-standing isolation quirk: in the
    # default (legacy) mode, ``conn.execute`` *implicitly commits any
    # open transaction before a DDL statement*. ``with conn:`` therefore
    # cannot wrap a DROP+CREATE+INSERT block atomically — by the time
    # the INSERT runs, the DROP is already committed and the legacy
    # table is permanently gone if anything fails. The fix is to drive
    # transaction state manually: explicit BEGIN, then COMMIT on
    # success or ROLLBACK on failure. SQLite honours the explicit
    # transaction even for DDL, so DROP stays uncommitted until the
    # final COMMIT.
    try:
        conn.execute("BEGIN")
        # Snapshot legacy rows. TEMP tables carry no indexes so we
        # don't collide with the post-migration ``CREATE INDEX``.
        conn.execute(
            "CREATE TEMP TABLE _predictions_legacy AS SELECT * FROM predictions"
        )
        # Drop the legacy table — indexes cascade away with it.
        conn.execute("DROP TABLE predictions")
        # Re-materialise the schema using per-statement execute (see
        # ``_create_schema`` docstring for why ``executescript`` is
        # banned here).
        _create_schema(conn)
        # Copy legacy rows back. ``analysis_group_id`` is NULL —
        # legacy rows predate the multi-horizon /expect feature.
        conn.execute(
            """
            INSERT INTO predictions
            (id, created_at, ticker, market, direction, confidence,
             timeframe, reasoning, entry_price, signals_used, source,
             target_price, stop_price, status, outcome_price,
             outcome_date, outcome_return)
            SELECT id, created_at, ticker, market, direction, confidence,
                   timeframe, reasoning, entry_price, signals_used, source,
                   target_price, stop_price, status, outcome_price,
                   outcome_date, outcome_return
            FROM _predictions_legacy
            """
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        # Best-effort cleanup. The TEMP table is session-scoped, so
        # this DROP IF EXISTS just keeps the namespace tidy for any
        # retry the caller may attempt.
        try:
            conn.execute("DROP TABLE IF EXISTS _predictions_legacy")
        except sqlite3.DatabaseError:
            pass


def insert_prediction(conn: sqlite3.Connection, pred: Prediction) -> Prediction:
    """Insert a new prediction into the database.

    Two guards run before insertion:

    1. LIVE BEAR predictions are hard-rejected. The track record shows a ~0%
       hit rate for automated BEAR calls, so the scheduler must not log them.
       Manual (INTERACTIVE) BEAR is still allowed as a deliberate override.
    2. Same-day duplicates are rejected: a second OPEN row matching an existing
       one on (ticker, market, direction, timeframe, source, created date)
       indicates a re-run logging the same call twice. Different timeframes
       (normal multi-horizon rows) and re-predictions after the prior one
       closed are both allowed.

    Args:
        conn: SQLite connection.
        pred: Prediction to insert.

    Returns:
        The inserted Prediction.

    Raises:
        ValueError: If the prediction is a LIVE BEAR call or a same-day
            duplicate of an existing OPEN prediction.
    """
    if pred.source == "LIVE" and pred.direction == "BEAR":
        raise ValueError(
            "LIVE BEAR predictions are gated (measured win rate ~0%); "
            "use direction NEUTRAL or source INTERACTIVE."
        )

    dupe = conn.execute(
        """SELECT 1 FROM predictions
           WHERE ticker = ? AND market = ? AND direction = ? AND timeframe = ?
             AND source = ? AND status = 'OPEN'
             AND date(created_at) = date(?)
           LIMIT 1""",
        (
            pred.ticker,
            pred.market,
            pred.direction,
            pred.timeframe,
            pred.source,
            pred.created_at,
        ),
    ).fetchone()
    if dupe is not None:
        raise ValueError(
            f"duplicate same-day prediction: an OPEN {pred.direction} "
            f"{pred.timeframe} {pred.ticker} ({pred.source}) already exists today"
        )

    conn.execute(
        """INSERT INTO predictions
           (id, created_at, ticker, market, direction, confidence, timeframe,
            reasoning, entry_price, signals_used, source, target_price, stop_price,
            status, outcome_price, outcome_date, outcome_return, analysis_group_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            pred.analysis_group_id,
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
    row = conn.execute("SELECT * FROM predictions WHERE id = ?", (pred_id,)).fetchone()
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
        analysis_group_id=row["analysis_group_id"],
    )
