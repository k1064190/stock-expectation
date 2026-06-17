"""Watchlist storage and unified watchlist assembly.

Owns a saved-ticker watchlist in a SIBLING database ``data/watchlist.db`` so
the predictions schema is never touched. The watchlist monitor (a delayed /
EOD-ish alerter — see ``watchlist_monitor.py``) reads from three sources and
merges them into one normalized list:

1. Saved rows (this DB) — explicit entry zone / stop / target / reentry levels.
2. OPEN predictions (predictions.db, read-only) — entry/target/stop/direction.
3. Portfolio positions (portfolio.db) — avg cost as entry, default protective
   stop, no target.

Dedup is by ``(ticker, market)`` with precedence ``saved > prediction >
position``. This module never mutates predictions or portfolio data.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "data" / "watchlist.db"
PREDICTIONS_DB_PATH = PROJECT_ROOT / "data" / "predictions.db"
PORTFOLIO_DB_PATH = PROJECT_ROOT / "data" / "portfolio.db"

# Default protective stop for a bare portfolio position with no prediction-
# supplied stop: 8% below average cost. Mirrors the conservative defaults used
# elsewhere (outcome_tracker's 5% MISS default is for predictions, which carry
# their own stops more often; raw holdings get a slightly looser band).
POSITION_DEFAULT_STOP_PCT = 0.92

CREATE_TABLE_STMT = """
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    market TEXT NOT NULL CHECK(market IN ('US', 'KR')),
    direction TEXT NOT NULL DEFAULT 'BULL' CHECK(direction IN ('BULL', 'BEAR')),
    entry_low REAL,
    entry_high REAL,
    stop REAL,
    target REAL,
    reentry REAL,
    note TEXT,
    created_at TEXT NOT NULL
)
"""


@dataclass
class WatchTarget:
    """A single normalized monitoring target assembled from any source.

    Args:
        ticker: Stock ticker ("NVDA" or "005930").
        market: "US" or "KR".
        direction: "BULL" or "BEAR" (default "BULL").
        entry_low: Lower bound of the entry zone, or None.
        entry_high: Upper bound of the entry zone, or None.
        stop: Stop-loss price, or None.
        target: Target price, or None.
        reentry: Re-entry trigger price (saved rows only), or None.
        source: Origin of this target — "saved", "prediction", or "position".
        label: Short human-readable identifier (saved id, prediction id, or
            "position") for logging and dedup-key context.
    """

    ticker: str
    market: str
    direction: str = "BULL"
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    reentry: Optional[float] = None
    source: str = "saved"
    label: str = ""


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Get a SQLite connection to the watchlist DB with WAL + busy timeout.

    Mirrors the connection pattern in portfolio/db.py and
    mcp-prediction-store/models.py: WAL journal mode, a 5s busy timeout, and an
    idempotent ``CREATE TABLE IF NOT EXISTS``.

    Args:
        db_path: Path to the database file. Defaults to data/watchlist.db.

    Returns:
        sqlite3.Connection with row_factory set to sqlite3.Row.
    """
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(CREATE_TABLE_STMT)
    conn.commit()
    return conn


def _normalize_ticker(ticker: str) -> str:
    """Uppercase US tickers; leave numeric KR codes untouched.

    Mirrors portfolio/db.add_transaction's convention so a saved KR code like
    ``005930`` is not mangled while ``nvda`` becomes ``NVDA``.
    """
    ticker = ticker.strip()
    return ticker if ticker[:1].isdigit() else ticker.upper()


def add_watch(
    conn: sqlite3.Connection,
    ticker: str,
    market: str,
    direction: str = "BULL",
    entry_low: Optional[float] = None,
    entry_high: Optional[float] = None,
    stop: Optional[float] = None,
    target: Optional[float] = None,
    reentry: Optional[float] = None,
    note: Optional[str] = None,
) -> int:
    """Insert a saved watchlist row.

    Args:
        conn: SQLite connection from get_connection.
        ticker: Stock ticker.
        market: "US" or "KR" (case-insensitive).
        direction: "BULL" or "BEAR".
        entry_low: Lower bound of the entry zone.
        entry_high: Upper bound of the entry zone.
        stop: Stop-loss price.
        target: Target price.
        reentry: Re-entry trigger price.
        note: Optional free-text note.

    Returns:
        The autoincrement row id of the inserted watch.
    """
    created_at = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """INSERT INTO watchlist
           (ticker, market, direction, entry_low, entry_high, stop, target,
            reentry, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            _normalize_ticker(ticker),
            market.upper(),
            direction.upper(),
            entry_low,
            entry_high,
            stop,
            target,
            reentry,
            note,
            created_at,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def remove_watch(conn: sqlite3.Connection, watch_id: int) -> bool:
    """Delete a saved watchlist row by id.

    Args:
        conn: SQLite connection.
        watch_id: Row id to delete.

    Returns:
        True if a row was deleted, False if no matching id existed.
    """
    result = conn.execute("DELETE FROM watchlist WHERE id = ?", (watch_id,))
    conn.commit()
    return result.rowcount > 0


def list_watches(
    conn: sqlite3.Connection, market: Optional[str] = None
) -> list[WatchTarget]:
    """List saved watchlist rows as WatchTargets.

    Args:
        conn: SQLite connection.
        market: Optional "US"/"KR" filter (case-insensitive).

    Returns:
        List of saved WatchTargets ordered by id ascending.
    """
    if market:
        rows = conn.execute(
            "SELECT * FROM watchlist WHERE market = ? ORDER BY id ASC",
            (market.upper(),),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM watchlist ORDER BY id ASC").fetchall()
    return [_row_to_target(r) for r in rows]


def _row_to_target(row: sqlite3.Row) -> WatchTarget:
    """Convert a saved watchlist DB row into a WatchTarget."""
    return WatchTarget(
        ticker=row["ticker"],
        market=row["market"],
        direction=row["direction"] or "BULL",
        entry_low=row["entry_low"],
        entry_high=row["entry_high"],
        stop=row["stop"],
        target=row["target"],
        reentry=row["reentry"],
        source="saved",
        label=f"saved:{row['id']}",
    )


def _load_prediction_targets(market: Optional[str], db_path: Path) -> list[WatchTarget]:
    """Load OPEN predictions as WatchTargets (read-only).

    Opens predictions.db in read-only URI mode so the monitor can never mutate
    predictions — the outcome tracker is the sole writer of prediction state.
    Returns an empty list when the DB is absent.

    Args:
        market: Optional market filter.
        db_path: Path to predictions.db.

    Returns:
        List of WatchTargets sourced from OPEN predictions.
    """
    if not db_path.exists():
        return []

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        query = (
            "SELECT id, ticker, market, direction, entry_price, target_price, "
            "stop_price FROM predictions WHERE status = 'OPEN'"
        )
        params: tuple = ()
        if market:
            query += " AND market = ?"
            params = (market.upper(),)
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    targets = []
    for r in rows:
        # NEUTRAL predictions have no directional entry/stop/target semantics
        # the monitor can act on; map them to BULL defaults only if levels
        # exist, otherwise treat as BULL (the WatchTarget default).
        direction = r["direction"] if r["direction"] in ("BULL", "BEAR") else "BULL"
        targets.append(
            WatchTarget(
                ticker=r["ticker"],
                market=r["market"],
                direction=direction,
                entry_low=r["entry_price"],
                entry_high=r["entry_price"],
                stop=r["stop_price"],
                target=r["target_price"],
                reentry=None,
                source="prediction",
                label=f"prediction:{r['id']}",
            )
        )
    return targets


def _load_position_targets(market: Optional[str], db_path: Path) -> list[WatchTarget]:
    """Load portfolio positions as WatchTargets.

    Uses avg cost as the entry reference and a default protective stop of
    ``avg * POSITION_DEFAULT_STOP_PCT``. No target is supplied (a prediction
    must provide one, which precedence handles). Returns an empty list when the
    portfolio DB is absent.

    Args:
        market: Optional market filter.
        db_path: Path to portfolio.db.

    Returns:
        List of WatchTargets sourced from open positions.
    """
    if not db_path.exists():
        return []

    # Import lazily so the store module stays importable in stripped envs that
    # don't carry the portfolio package on the path.
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from portfolio.db import (
        get_connection as pf_get_connection,
        list_portfolios,
        compute_positions,
    )

    conn = pf_get_connection(db_path)
    try:
        portfolios = list_portfolios(conn)
        targets = []
        for pf in portfolios:
            if market and pf.market != market.upper():
                continue
            for pos in compute_positions(conn, pf.id):
                if pos.avg_price <= 0:
                    continue
                targets.append(
                    WatchTarget(
                        ticker=pos.ticker,
                        market=pf.market,
                        direction="BULL",
                        entry_low=pos.avg_price,
                        entry_high=pos.avg_price,
                        stop=round(pos.avg_price * POSITION_DEFAULT_STOP_PCT, 4),
                        target=None,
                        reentry=None,
                        source="position",
                        label="position",
                    )
                )
        return targets
    finally:
        conn.close()


def load_unified_watchlist(
    market: Optional[str] = None,
    watchlist_db_path: Optional[Path] = None,
    predictions_db_path: Optional[Path] = None,
    portfolio_db_path: Optional[Path] = None,
) -> list[WatchTarget]:
    """Merge all three sources into one deduplicated watchlist.

    Sources are merged in precedence order ``saved > prediction > position``:
    the first source to claim a ``(ticker, market)`` key wins, and later
    sources for the same key are dropped. This means an explicitly saved level
    set overrides whatever a prediction or position would have produced.

    Args:
        market: Optional "US"/"KR" filter applied to every source.
        watchlist_db_path: Override for the saved watchlist DB (tests).
        predictions_db_path: Override for predictions.db (tests).
        portfolio_db_path: Override for portfolio.db (tests).

    Returns:
        Deduplicated list of WatchTargets, saved rows first.
    """
    saved_conn = get_connection(watchlist_db_path)
    try:
        saved = list_watches(saved_conn, market=market)
    finally:
        saved_conn.close()

    predictions = _load_prediction_targets(
        market, predictions_db_path or PREDICTIONS_DB_PATH
    )
    positions = _load_position_targets(market, portfolio_db_path or PORTFOLIO_DB_PATH)

    merged: dict[tuple[str, str], WatchTarget] = {}
    # Order matters: insert highest-precedence source first and never overwrite.
    for target in [*saved, *predictions, *positions]:
        key = (target.ticker, target.market)
        if key not in merged:
            merged[key] = target

    return list(merged.values())
