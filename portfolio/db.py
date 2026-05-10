"""Portfolio database operations.

SQLite-backed storage with WAL mode. Follows the same pattern as
mcp-prediction-store/models.py for connection management.
"""

import sqlite3
from pathlib import Path
from typing import Optional

from .models import Portfolio, Transaction, Position

DB_PATH = Path(__file__).parent.parent / "data" / "portfolio.db"

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS portfolios (
    id TEXT PRIMARY KEY,
    market TEXT NOT NULL CHECK(market IN ('US', 'KR')),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id TEXT NOT NULL REFERENCES portfolios(id),
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    quantity REAL NOT NULL CHECK(quantity > 0),
    price REAL NOT NULL CHECK(price > 0),
    currency TEXT NOT NULL CHECK(currency IN ('KRW', 'USD')),
    transacted_at TEXT NOT NULL,
    note TEXT,
    thesis_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tx_portfolio ON transactions(portfolio_id);
CREATE INDEX IF NOT EXISTS idx_tx_ticker ON transactions(ticker);
CREATE INDEX IF NOT EXISTS idx_tx_transacted ON transactions(transacted_at);
"""


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode and busy timeout.

    Args:
        db_path: Path to the database file. Defaults to data/portfolio.db.

    Returns:
        sqlite3.Connection with WAL mode and busy_timeout=5000.
    """
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(CREATE_TABLES_SQL)
    return conn


def create_portfolio(conn: sqlite3.Connection, market: str, name: str) -> Portfolio:
    """Create a new portfolio.

    Args:
        conn: SQLite connection.
        market: "US" or "KR".
        name: Display name.

    Returns:
        The created Portfolio.
    """
    pf = Portfolio(market=market.upper(), name=name)
    conn.execute(
        "INSERT INTO portfolios (id, market, name, created_at) VALUES (?, ?, ?, ?)",
        (pf.id, pf.market, pf.name, pf.created_at),
    )
    conn.commit()
    return pf


def list_portfolios(conn: sqlite3.Connection) -> list[Portfolio]:
    """List all portfolios."""
    rows = conn.execute("SELECT * FROM portfolios ORDER BY created_at").fetchall()
    return [
        Portfolio(
            id=r["id"], market=r["market"], name=r["name"], created_at=r["created_at"]
        )
        for r in rows
    ]


def get_portfolio_for_market(
    conn: sqlite3.Connection, market: str
) -> Optional[Portfolio]:
    """Get the first portfolio for a given market.

    Args:
        conn: SQLite connection.
        market: "US" or "KR".

    Returns:
        Portfolio if found, None otherwise.
    """
    row = conn.execute(
        "SELECT * FROM portfolios WHERE market = ? LIMIT 1", (market.upper(),)
    ).fetchone()
    if row is None:
        return None
    return Portfolio(
        id=row["id"],
        market=row["market"],
        name=row["name"],
        created_at=row["created_at"],
    )


def add_transaction(
    conn: sqlite3.Connection,
    portfolio_id: str,
    ticker: str,
    side: str,
    quantity: float,
    price: float,
    currency: str,
    transacted_at: str,
    note: Optional[str] = None,
    thesis_id: Optional[str] = None,
) -> Transaction:
    """Record a buy or sell transaction.

    Args:
        conn: SQLite connection.
        portfolio_id: Portfolio to add the transaction to.
        ticker: Stock ticker.
        side: "BUY" or "SELL".
        quantity: Number of shares.
        price: Price per share.
        currency: "KRW" or "USD".
        transacted_at: Trade date (YYYY-MM-DD).
        note: Optional memo.
        thesis_id: Optional thesis link.

    Returns:
        The created Transaction with assigned ID.
    """
    tx = Transaction(
        portfolio_id=portfolio_id,
        ticker=ticker.upper() if not ticker[0].isdigit() else ticker,
        side=side.upper(),
        quantity=quantity,
        price=price,
        currency=currency.upper(),
        transacted_at=transacted_at,
        note=note,
        thesis_id=thesis_id,
    )
    cursor = conn.execute(
        """INSERT INTO transactions
           (portfolio_id, ticker, side, quantity, price, currency,
            transacted_at, note, thesis_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            tx.portfolio_id,
            tx.ticker,
            tx.side,
            tx.quantity,
            tx.price,
            tx.currency,
            tx.transacted_at,
            tx.note,
            tx.thesis_id,
            tx.created_at,
        ),
    )
    conn.commit()
    tx.id = cursor.lastrowid
    return tx


def list_transactions(
    conn: sqlite3.Connection,
    portfolio_id: str,
    ticker: Optional[str] = None,
    last_n: Optional[int] = None,
) -> list[Transaction]:
    """List transactions with optional filters.

    Args:
        conn: SQLite connection.
        portfolio_id: Portfolio to list transactions for.
        ticker: Optional ticker filter.
        last_n: Optional limit to last N transactions.

    Returns:
        List of Transaction objects ordered by transacted_at ASC.
    """
    conditions = ["portfolio_id = ?"]
    params: list = [portfolio_id]
    if ticker:
        conditions.append("ticker = ?")
        params.append(ticker.upper() if not ticker[0].isdigit() else ticker)

    where = " AND ".join(conditions)
    if last_n:
        query = f"""SELECT * FROM (
            SELECT * FROM transactions WHERE {where}
            ORDER BY transacted_at DESC, id DESC LIMIT ?
        ) sub ORDER BY transacted_at ASC, id ASC"""
        params.append(last_n)
    else:
        query = f"SELECT * FROM transactions WHERE {where} ORDER BY transacted_at ASC, id ASC"

    rows = conn.execute(query, params).fetchall()
    return [_row_to_transaction(r) for r in rows]


def delete_transaction(conn: sqlite3.Connection, tx_id: int) -> bool:
    """Delete a transaction by ID.

    Args:
        conn: SQLite connection.
        tx_id: Transaction ID to delete.

    Returns:
        True if deleted, False if not found.
    """
    result = conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
    conn.commit()
    return result.rowcount > 0


def compute_positions(conn: sqlite3.Connection, portfolio_id: str) -> list[Position]:
    """Compute current positions from transaction history.

    Uses moving average cost method for avg_price calculation.
    Only returns positions with quantity > 0 (open positions).

    Args:
        conn: SQLite connection.
        portfolio_id: Portfolio to compute positions for.

    Returns:
        List of Position objects for open holdings.
    """
    rows = conn.execute(
        """SELECT * FROM transactions
           WHERE portfolio_id = ?
           ORDER BY transacted_at ASC, id ASC""",
        (portfolio_id,),
    ).fetchall()

    holdings: dict[str, dict] = {}

    for row in rows:
        ticker = row["ticker"]
        side = row["side"]
        qty = row["quantity"]
        price = row["price"]

        if ticker not in holdings:
            holdings[ticker] = {
                "quantity": 0.0,
                "avg_price": 0.0,
                "realized_pnl": 0.0,
            }

        h = holdings[ticker]
        if side == "BUY":
            total_qty = h["quantity"] + qty
            if total_qty > 0:
                h["avg_price"] = (
                    h["quantity"] * h["avg_price"] + qty * price
                ) / total_qty
            h["quantity"] = total_qty
        elif side == "SELL":
            h["realized_pnl"] += (price - h["avg_price"]) * qty
            h["quantity"] -= qty

    positions = []
    for ticker, h in holdings.items():
        if h["quantity"] > 0:
            positions.append(
                Position(
                    portfolio_id=portfolio_id,
                    ticker=ticker,
                    quantity=h["quantity"],
                    avg_price=h["avg_price"],
                    total_cost=h["quantity"] * h["avg_price"],
                    realized_pnl=h["realized_pnl"],
                )
            )

    return positions


def _row_to_transaction(row: sqlite3.Row) -> Transaction:
    """Convert a database row to a Transaction dataclass."""
    return Transaction(
        id=row["id"],
        portfolio_id=row["portfolio_id"],
        ticker=row["ticker"],
        side=row["side"],
        quantity=row["quantity"],
        price=row["price"],
        currency=row["currency"],
        transacted_at=row["transacted_at"],
        note=row["note"],
        thesis_id=row["thesis_id"],
        created_at=row["created_at"],
    )
