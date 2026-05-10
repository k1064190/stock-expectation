"""Portfolio evaluation functions.

Computes P&L reports, risk analysis, prediction comparison, and advice.
All functions return dicts suitable for JSON serialization.
"""

import sqlite3
from pathlib import Path
from typing import Optional

from .models import Position

_OVERWEIGHT_PCT = 30.0


def compute_report(positions: list[Position], current_prices: dict[str, float]) -> dict:
    """Compute P&L report for positions.

    Args:
        positions: List of computed positions.
        current_prices: Map of ticker -> current market price.

    Returns:
        Dict with per-holding details and portfolio totals.
    """
    holdings = []
    total_cost = 0.0
    total_market_value = 0.0
    total_unrealized = 0.0
    total_realized = 0.0

    for pos in positions:
        price = current_prices.get(pos.ticker)
        if price is not None:
            market_value = pos.quantity * price
            unrealized_pnl = market_value - pos.total_cost
            unrealized_pct = (
                (unrealized_pnl / pos.total_cost * 100) if pos.total_cost else 0.0
            )
            total_market_value += market_value
            total_unrealized += unrealized_pnl
        else:
            market_value = None
            unrealized_pnl = None
            unrealized_pct = None

        total_cost += pos.total_cost
        total_realized += pos.realized_pnl

        holdings.append(
            {
                "ticker": pos.ticker,
                "quantity": pos.quantity,
                "avg_price": pos.avg_price,
                "current_price": price,
                "market_value": market_value,
                "unrealized_pnl": unrealized_pnl,
                "unrealized_pnl_pct": (
                    round(unrealized_pct, 2) if unrealized_pct is not None else None
                ),
                "realized_pnl": pos.realized_pnl,
            }
        )

    return {
        "holdings": holdings,
        "total_cost": total_cost,
        "total_market_value": total_market_value,
        "total_unrealized_pnl": total_unrealized,
        "total_realized_pnl": total_realized,
        "total_return_pnl": total_unrealized + total_realized,
        "total_return_pct": round(
            (
                ((total_unrealized + total_realized) / total_cost * 100)
                if total_cost
                else 0.0
            ),
            2,
        ),
    }


def compute_risk(
    positions: list[Position],
    current_prices: dict[str, float],
    sector_map: Optional[dict[str, str]] = None,
) -> dict:
    """Compute risk analysis for positions.

    Args:
        positions: List of computed positions.
        current_prices: Map of ticker -> current market price.
        sector_map: Optional map of ticker -> sector name.

    Returns:
        Dict with concentration, HHI, sector breakdown, and warnings.
    """
    values: list[tuple[str, float]] = []
    total_value = 0.0
    for pos in positions:
        price = current_prices.get(pos.ticker, pos.avg_price)
        mv = pos.quantity * price
        values.append((pos.ticker, mv))
        total_value += mv

    concentration = []
    for ticker, mv in sorted(values, key=lambda x: -x[1]):
        weight = (mv / total_value * 100) if total_value else 0.0
        concentration.append(
            {"ticker": ticker, "market_value": mv, "weight_pct": round(weight, 2)}
        )

    hhi = sum(c["weight_pct"] ** 2 for c in concentration)

    sectors: dict[str, float] = {}
    if sector_map:
        for ticker, mv in values:
            sector = sector_map.get(ticker, "Unknown")
            sectors[sector] = sectors.get(sector, 0.0) + mv
    sector_breakdown = [
        {
            "sector": s,
            "weight_pct": round(v / total_value * 100, 2) if total_value else 0.0,
        }
        for s, v in sorted(sectors.items(), key=lambda x: -x[1])
    ]

    warnings = []
    for c in concentration:
        if c["weight_pct"] > _OVERWEIGHT_PCT:
            warnings.append(
                f"{c['ticker']} is {c['weight_pct']:.1f}% of portfolio (>{_OVERWEIGHT_PCT}%)"
            )

    return {
        "concentration": concentration,
        "hhi": round(hhi),
        "sector_breakdown": sector_breakdown,
        "warnings": warnings,
        "position_count": len(positions),
        "total_value": total_value,
    }


def compute_vs_predictions(
    positions: list[Position], predictions_db_path: Path
) -> dict:
    """Compare portfolio holdings against prediction store.

    Opens predictions.db in read-only mode. Matches by ticker.

    Args:
        positions: Current positions.
        predictions_db_path: Path to predictions.db.

    Returns:
        Dict with matched predictions, direction mismatches, and stats.
    """
    if not predictions_db_path.exists():
        return {"matches": [], "mismatches": [], "error": "predictions.db not found"}

    conn = sqlite3.connect(f"file:{predictions_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    held_tickers = {pos.ticker for pos in positions}
    matches = []
    mismatches = []

    for ticker in held_tickers:
        rows = conn.execute(
            """SELECT id, direction, confidence, timeframe, status, entry_price,
                      outcome_return, created_at
               FROM predictions WHERE ticker = ? ORDER BY created_at DESC LIMIT 5""",
            (ticker,),
        ).fetchall()

        for row in rows:
            entry = {
                "ticker": ticker,
                "prediction_id": row["id"],
                "direction": row["direction"],
                "confidence": row["confidence"],
                "timeframe": row["timeframe"],
                "status": row["status"],
                "entry_price": row["entry_price"],
                "outcome_return": row["outcome_return"],
                "created_at": row["created_at"],
            }
            matches.append(entry)
            if row["direction"] == "BEAR" and row["status"] == "OPEN":
                mismatches.append(
                    {
                        "ticker": ticker,
                        "prediction_id": row["id"],
                        "message": f"BEAR prediction (conf={row['confidence']}) but holding {ticker}",
                    }
                )

    conn.close()
    return {
        "matches": matches,
        "mismatches": mismatches,
        "held_tickers": len(held_tickers),
        "tickers_with_predictions": len({m["ticker"] for m in matches}),
    }


def compute_advice(
    positions: list[Position],
    current_prices: dict[str, float],
    ma50: Optional[dict[str, float]] = None,
    ma200: Optional[dict[str, float]] = None,
) -> dict:
    """Compute trading advice signals for positions.

    Args:
        positions: Current positions.
        current_prices: Map of ticker -> current price.
        ma50: Optional map of ticker -> 50-day moving average.
        ma200: Optional map of ticker -> 200-day moving average.

    Returns:
        Dict with per-ticker technical signals and flags.
    """
    stop_loss_pct = -10.0
    advice = []

    for pos in positions:
        price = current_prices.get(pos.ticker)
        if price is None:
            continue

        pnl_pct = (
            ((price - pos.avg_price) / pos.avg_price * 100) if pos.avg_price else 0.0
        )
        signals = []
        if pnl_pct <= stop_loss_pct:
            signals.append(f"STOP_LOSS: down {pnl_pct:.1f}% from avg cost")

        ma_status = {}
        ticker_ma50 = (ma50 or {}).get(pos.ticker)
        ticker_ma200 = (ma200 or {}).get(pos.ticker)
        if ticker_ma50 is not None:
            ma_status["above_ma50"] = price > ticker_ma50
            ma_status["ma50"] = ticker_ma50
        if ticker_ma200 is not None:
            ma_status["above_ma200"] = price > ticker_ma200
            ma_status["ma200"] = ticker_ma200

        advice.append(
            {
                "ticker": pos.ticker,
                "quantity": pos.quantity,
                "avg_price": pos.avg_price,
                "current_price": price,
                "pnl_pct": round(pnl_pct, 2),
                "signals": signals,
                "ma_status": ma_status,
            }
        )

    return {"advice": advice}
