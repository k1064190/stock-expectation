"""Sync portfolio data from Toss Securities via tossctl CLI.

Calls `tossctl portfolio positions --output json` and reconciles
the result against the local portfolio.db. Differences are recorded
as synthetic BUY transactions so that compute_positions() matches
the Toss snapshot.
"""

import json
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Optional

from . import toss_api
from .models import Position


def tossctl_available() -> bool:
    """Check if tossctl binary is on PATH."""
    return shutil.which("tossctl") is not None


def fetch_positions(source: str = "auto") -> tuple[list[dict], str]:
    """Fetch Toss positions, preferring the official Open API.

    Args:
        source: "auto" (Open API if credentials are set, else tossctl),
            "toss-api" (force the official Open API), or "tossctl"
            (force the legacy CLI).

    Returns:
        Tuple of (positions, source_used) where source_used is
        "toss-api" or "tossctl".

    Raises:
        RuntimeError: If the requested source is unavailable, or if neither
            source is available under "auto".
    """
    if source == "toss-api":
        return toss_api.fetch_toss_positions_api(), "toss-api"
    if source == "tossctl":
        return fetch_toss_positions(), "tossctl"

    # auto
    if toss_api.toss_api_configured():
        return toss_api.fetch_toss_positions_api(), "toss-api"
    if tossctl_available():
        return fetch_toss_positions(), "tossctl"
    raise RuntimeError(
        "Toss credentials not set (TOSS_CLIENT_ID/TOSS_CLIENT_SECRET) "
        "and tossctl is not installed."
    )


def fetch_toss_positions() -> list[dict]:
    """Fetch current positions from Toss Securities.

    Returns:
        List of position dicts from tossctl, each with keys:
        symbol, name, market_type, quantity, average_price,
        current_price, market_value, unrealized_pnl, profit_rate,
        and optionally average_price_usd for US stocks.

    Raises:
        RuntimeError: If tossctl is not installed or call fails.
    """
    if not tossctl_available():
        raise RuntimeError("tossctl is not installed. Run the install script first.")

    result = subprocess.run(
        ["tossctl", "portfolio", "positions", "--output", "json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"tossctl failed: {result.stderr.strip()}")

    return json.loads(result.stdout)


def _normalize_ticker(pos: dict) -> str:
    """Extract normalized ticker from tossctl position.

    KR stocks come as 'A005930' -> '005930'.
    US stocks come as 'AAPL' -> 'AAPL'.

    Args:
        pos: Single position dict from tossctl.

    Returns:
        Normalized ticker string.
    """
    symbol = pos["symbol"]
    if pos["market_type"] == "KR_STOCK" and symbol.startswith("A"):
        return symbol[1:]  # strip leading 'A'
    return symbol


def _market_from_toss(market_type: str) -> Optional[str]:
    """Convert tossctl market_type to our market enum.

    Args:
        market_type: e.g. "KR_STOCK", "US_STOCK", "US_BOND".

    Returns:
        "KR" or "US", or None if unsupported.
    """
    if market_type == "KR_STOCK":
        return "KR"
    elif market_type == "US_STOCK":
        return "US"
    return None


def reconcile(
    toss_positions: list[dict],
    local_positions: list[Position],
    market: str,
) -> list[dict]:
    """Compare Toss snapshot with local positions and generate sync actions.

    For each Toss position:
    - If not in local: generate a BUY for the full quantity at avg price.
    - If in local with different quantity: generate a BUY or SELL for the delta.
    - If in local with same quantity: no action (avg_price difference is noted).

    For each local position not in Toss:
    - Generate a SELL for the full quantity at avg price (position closed on Toss).

    Args:
        toss_positions: Positions from tossctl, filtered to one market.
        local_positions: Current local positions from compute_positions().
        market: "US" or "KR".

    Returns:
        List of action dicts with keys: ticker, side, quantity, price,
        currency, note.
    """
    # Build lookup maps
    toss_map: dict[str, dict] = {}
    for tp in toss_positions:
        m = _market_from_toss(tp["market_type"])
        if m != market:
            continue
        ticker = _normalize_ticker(tp)
        toss_map[ticker] = tp

    local_map: dict[str, Position] = {p.ticker: p for p in local_positions}

    actions = []
    currency = "KRW" if market == "KR" else "USD"

    # Positions in Toss but not local, or quantity changed
    for ticker, tp in toss_map.items():
        toss_qty = tp["quantity"]
        if market == "US":
            avg_price = tp.get("average_price_usd", tp["average_price"])
        else:
            avg_price = tp["average_price"]

        local_pos = local_map.get(ticker)
        if local_pos is None:
            # New position — synthetic BUY
            actions.append(
                {
                    "ticker": ticker,
                    "side": "BUY",
                    "quantity": toss_qty,
                    "price": avg_price,
                    "currency": currency,
                    "note": f"toss-sync: new position ({tp['name']})",
                }
            )
        else:
            delta = toss_qty - local_pos.quantity
            if abs(delta) < 0.0001:
                continue  # same quantity
            elif delta > 0:
                actions.append(
                    {
                        "ticker": ticker,
                        "side": "BUY",
                        "quantity": delta,
                        "price": avg_price,
                        "currency": currency,
                        "note": f"toss-sync: quantity increased ({tp['name']})",
                    }
                )
            else:
                # Sold some shares — use current price as sell price
                sell_price = (
                    tp.get("current_price_usd", tp["current_price"])
                    if market == "US"
                    else tp["current_price"]
                )
                actions.append(
                    {
                        "ticker": ticker,
                        "side": "SELL",
                        "quantity": abs(delta),
                        "price": sell_price,
                        "currency": currency,
                        "note": f"toss-sync: quantity decreased ({tp['name']})",
                    }
                )

    # Positions in local but not in Toss — fully closed
    for ticker, local_pos in local_map.items():
        if ticker not in toss_map:
            actions.append(
                {
                    "ticker": ticker,
                    "side": "SELL",
                    "quantity": local_pos.quantity,
                    "price": local_pos.avg_price,
                    "currency": currency,
                    "note": "toss-sync: position closed on Toss",
                }
            )

    return actions
