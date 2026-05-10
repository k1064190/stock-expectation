"""Portfolio data models.

Dataclasses and enums for portfolio tracking. Mirrors the pattern
used in mcp-prediction-store/models.py.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Currency(str, Enum):
    KRW = "KRW"
    USD = "USD"


@dataclass
class Portfolio:
    """A market-specific portfolio container.

    Args:
        market: "US" or "KR".
        name: Display name (e.g. "Toss KR").
        id: Unique identifier (auto-generated as pf_{uuid4_short}).
        created_at: Creation timestamp (UTC ISO format).
    """

    market: str
    name: str
    id: str = field(default_factory=lambda: f"pf_{uuid.uuid4().hex[:8]}")
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class Transaction:
    """A single buy or sell trade record.

    Args:
        portfolio_id: Foreign key to Portfolio.id.
        ticker: Stock ticker (e.g. "NVDA", "005930").
        side: "BUY" or "SELL".
        quantity: Number of shares (REAL for fractional US shares).
        price: Execution price per share.
        currency: "KRW" or "USD".
        transacted_at: Actual trade date (YYYY-MM-DD).
        note: Free-form memo (nullable).
        thesis_id: Link to trader-memory-core thesis (nullable).
        id: Auto-generated integer-style unique ID (assigned by DB).
        created_at: Record creation timestamp (UTC ISO format).
    """

    portfolio_id: str
    ticker: str
    side: str
    quantity: float
    price: float
    currency: str
    transacted_at: str
    note: Optional[str] = None
    thesis_id: Optional[str] = None
    id: Optional[int] = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class Position:
    """Computed current holding for a ticker in a portfolio.

    Not stored in DB — aggregated from transactions.

    Args:
        portfolio_id: Portfolio this position belongs to.
        ticker: Stock ticker.
        quantity: Net shares held (buys - sells).
        avg_price: Moving average cost basis.
        total_cost: Total capital invested in current shares.
        realized_pnl: Cumulative realized P&L from sells.
    """

    portfolio_id: str
    ticker: str
    quantity: float
    avg_price: float
    total_cost: float
    realized_pnl: float
