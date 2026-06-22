"""Paper-trading database: schema, dataclasses, and CRUD.

SQLite-backed (WAL), isolated in ``data/paper_trading.db`` — never the real
portfolio DB. Follows the connection pattern of ``portfolio/db.py`` and
``mcp-prediction-store/models.py``.

Schema:
    accounts      — one simulated book per market (seeded cash).
    positions     — open/closed lots; each lot links to the prediction that
                    opened it and carries its own target/stop/horizon.
    trades        — every simulated fill, with cost breakdown.
    nav_history   — one daily mark-to-market row per (account, date).

CRUD helpers do NOT commit — the caller owns the transaction so a full daily
cycle (exits + entries + cash + NAV) commits atomically (see engine.run_day).
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent.parent / "data" / "paper_trading.db"

CURRENCY_BY_MARKET = {"KR": "KRW", "US": "USD"}

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    market TEXT NOT NULL UNIQUE CHECK(market IN ('US', 'KR')),
    base_currency TEXT NOT NULL CHECK(base_currency IN ('USD', 'KRW')),
    initial_capital REAL NOT NULL CHECK(initial_capital > 0),
    cash REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    ticker TEXT NOT NULL,
    qty REAL NOT NULL CHECK(qty > 0),
    avg_cost REAL NOT NULL CHECK(avg_cost > 0),
    opened_at TEXT NOT NULL,
    prediction_id TEXT,
    target_price REAL,
    stop_price REAL,
    horizon_end_date TEXT,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN', 'CLOSED')),
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    qty REAL NOT NULL CHECK(qty > 0),
    price REAL NOT NULL CHECK(price > 0),
    gross REAL NOT NULL,
    fees REAL NOT NULL,
    tax REAL NOT NULL,
    slippage REAL NOT NULL,
    net_cash_delta REAL NOT NULL,
    executed_at TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('entry', 'target_hit', 'stop_hit', 'horizon_exit')),
    prediction_id TEXT
);

CREATE TABLE IF NOT EXISTS nav_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    date TEXT NOT NULL,
    cash REAL NOT NULL,
    positions_value REAL NOT NULL,
    nav REAL NOT NULL,
    daily_return REAL,
    cumulative_return REAL,
    n_positions INTEGER NOT NULL,
    benchmark_nav REAL,
    UNIQUE(account_id, date)
);

CREATE INDEX IF NOT EXISTS idx_pt_positions_account ON positions(account_id, status);
CREATE INDEX IF NOT EXISTS idx_pt_trades_account ON trades(account_id);
CREATE INDEX IF NOT EXISTS idx_pt_nav_account ON nav_history(account_id, date);
"""


@dataclass
class Account:
    """A simulated single-currency book for one market."""

    market: str
    base_currency: str
    initial_capital: float
    cash: float
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class Position:
    """An open (or closed) lot opened by one prediction."""

    account_id: str
    ticker: str
    qty: float
    avg_cost: float
    opened_at: str
    prediction_id: Optional[str] = None
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    horizon_end_date: Optional[str] = None
    status: str = "OPEN"
    closed_at: Optional[str] = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class Trade:
    """A single simulated fill with its cost breakdown."""

    account_id: str
    ticker: str
    side: str
    qty: float
    price: float
    gross: float
    fees: float
    tax: float
    slippage: float
    net_cash_delta: float
    executed_at: str
    reason: str
    prediction_id: Optional[str] = None
    id: Optional[int] = None


@dataclass
class NavSnapshot:
    """A daily mark-to-market snapshot for one account."""

    account_id: str
    date: str
    cash: float
    positions_value: float
    nav: float
    daily_return: Optional[float]
    cumulative_return: Optional[float]
    n_positions: int
    benchmark_nav: Optional[float] = None
    id: Optional[int] = None


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a WAL-mode connection and ensure the schema exists."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(CREATE_TABLES_SQL)
    return conn


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #


def seed_account(
    conn: sqlite3.Connection,
    market: str,
    initial_capital: float,
    base_currency: Optional[str] = None,
) -> Account:
    """Create the book for ``market`` if absent; return the existing one otherwise.

    Idempotent: a second call with the same market returns the already-seeded
    account untouched (cash is not reset).
    """
    existing = get_account(conn, market)
    if existing is not None:
        return existing
    currency = base_currency or CURRENCY_BY_MARKET[market]
    acct = Account(
        market=market,
        base_currency=currency,
        initial_capital=initial_capital,
        cash=initial_capital,
    )
    conn.execute(
        "INSERT INTO accounts (id, market, base_currency, initial_capital, cash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            acct.id,
            acct.market,
            acct.base_currency,
            acct.initial_capital,
            acct.cash,
            acct.created_at,
        ),
    )
    return acct


def get_account(conn: sqlite3.Connection, market: str) -> Optional[Account]:
    row = conn.execute("SELECT * FROM accounts WHERE market = ?", (market,)).fetchone()
    if row is None:
        return None
    return Account(
        id=row["id"],
        market=row["market"],
        base_currency=row["base_currency"],
        initial_capital=row["initial_capital"],
        cash=row["cash"],
        created_at=row["created_at"],
    )


def update_cash(conn: sqlite3.Connection, account_id: str, new_cash: float) -> None:
    conn.execute("UPDATE accounts SET cash = ? WHERE id = ?", (new_cash, account_id))


# --------------------------------------------------------------------------- #
# Positions
# --------------------------------------------------------------------------- #


def insert_position(conn: sqlite3.Connection, pos: Position) -> Position:
    conn.execute(
        "INSERT INTO positions (id, account_id, ticker, qty, avg_cost, opened_at, "
        "prediction_id, target_price, stop_price, horizon_end_date, status, closed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            pos.id,
            pos.account_id,
            pos.ticker,
            pos.qty,
            pos.avg_cost,
            pos.opened_at,
            pos.prediction_id,
            pos.target_price,
            pos.stop_price,
            pos.horizon_end_date,
            pos.status,
            pos.closed_at,
        ),
    )
    return pos


def get_open_positions(conn: sqlite3.Connection, account_id: str) -> list[Position]:
    rows = conn.execute(
        "SELECT * FROM positions WHERE account_id = ? AND status = 'OPEN' "
        "ORDER BY opened_at, id",
        (account_id,),
    ).fetchall()
    return [_row_to_position(r) for r in rows]


def close_position(
    conn: sqlite3.Connection, position_id: str, closed_at: Optional[str] = None
) -> None:
    conn.execute(
        "UPDATE positions SET status = 'CLOSED', closed_at = ? WHERE id = ?",
        (closed_at or datetime.now(timezone.utc).isoformat(), position_id),
    )


def _row_to_position(row: sqlite3.Row) -> Position:
    return Position(
        id=row["id"],
        account_id=row["account_id"],
        ticker=row["ticker"],
        qty=row["qty"],
        avg_cost=row["avg_cost"],
        opened_at=row["opened_at"],
        prediction_id=row["prediction_id"],
        target_price=row["target_price"],
        stop_price=row["stop_price"],
        horizon_end_date=row["horizon_end_date"],
        status=row["status"],
        closed_at=row["closed_at"],
    )


# --------------------------------------------------------------------------- #
# Trades
# --------------------------------------------------------------------------- #


def insert_trade(conn: sqlite3.Connection, trade: Trade) -> Trade:
    cur = conn.execute(
        "INSERT INTO trades (account_id, ticker, side, qty, price, gross, fees, tax, "
        "slippage, net_cash_delta, executed_at, reason, prediction_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            trade.account_id,
            trade.ticker,
            trade.side,
            trade.qty,
            trade.price,
            trade.gross,
            trade.fees,
            trade.tax,
            trade.slippage,
            trade.net_cash_delta,
            trade.executed_at,
            trade.reason,
            trade.prediction_id,
        ),
    )
    trade.id = cur.lastrowid
    return trade


def get_trades(conn: sqlite3.Connection, account_id: str) -> list[Trade]:
    rows = conn.execute(
        "SELECT * FROM trades WHERE account_id = ? ORDER BY executed_at, id",
        (account_id,),
    ).fetchall()
    return [
        Trade(
            id=r["id"],
            account_id=r["account_id"],
            ticker=r["ticker"],
            side=r["side"],
            qty=r["qty"],
            price=r["price"],
            gross=r["gross"],
            fees=r["fees"],
            tax=r["tax"],
            slippage=r["slippage"],
            net_cash_delta=r["net_cash_delta"],
            executed_at=r["executed_at"],
            reason=r["reason"],
            prediction_id=r["prediction_id"],
        )
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# NAV history
# --------------------------------------------------------------------------- #


def record_nav(conn: sqlite3.Connection, snap: NavSnapshot) -> None:
    """Insert or replace the daily NAV row for ``(account_id, date)`` (idempotent)."""
    conn.execute(
        "INSERT INTO nav_history (account_id, date, cash, positions_value, nav, "
        "daily_return, cumulative_return, n_positions, benchmark_nav) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(account_id, date) DO UPDATE SET "
        "cash=excluded.cash, positions_value=excluded.positions_value, nav=excluded.nav, "
        "daily_return=excluded.daily_return, cumulative_return=excluded.cumulative_return, "
        "n_positions=excluded.n_positions, benchmark_nav=excluded.benchmark_nav",
        (
            snap.account_id,
            snap.date,
            snap.cash,
            snap.positions_value,
            snap.nav,
            snap.daily_return,
            snap.cumulative_return,
            snap.n_positions,
            snap.benchmark_nav,
        ),
    )


def get_nav_history(conn: sqlite3.Connection, account_id: str) -> list[NavSnapshot]:
    rows = conn.execute(
        "SELECT * FROM nav_history WHERE account_id = ? ORDER BY date",
        (account_id,),
    ).fetchall()
    return [_row_to_nav(r) for r in rows]


def get_latest_nav(conn: sqlite3.Connection, account_id: str) -> Optional[NavSnapshot]:
    row = conn.execute(
        "SELECT * FROM nav_history WHERE account_id = ? ORDER BY date DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    return _row_to_nav(row) if row else None


def _row_to_nav(row: sqlite3.Row) -> NavSnapshot:
    return NavSnapshot(
        id=row["id"],
        account_id=row["account_id"],
        date=row["date"],
        cash=row["cash"],
        positions_value=row["positions_value"],
        nav=row["nav"],
        daily_return=row["daily_return"],
        cumulative_return=row["cumulative_return"],
        n_positions=row["n_positions"],
        benchmark_nav=row["benchmark_nav"],
    )
