"""Nightly outcome tracker for open predictions.

Checks all OPEN predictions, fetches current prices, and scores each as
HIT/MISS/EXPIRED based on precise rules. Timezone-aware for US (ET) and
Korean (KST) markets. Holiday calendar support.

Run nightly at 00:00 KST (after both markets close):
    python outcome_tracker.py
"""

import json
import logging
import sys
from datetime import datetime, timedelta, timezone, time
from pathlib import Path
from zoneinfo import ZoneInfo

# Add project paths
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-prediction-store"))
sys.path.insert(0, str(PROJECT_ROOT / "mcp-market-data"))

from models import (
    Prediction,
    Timeframe,
    TIMEFRAME_TRADING_DAYS,
    get_connection,
    list_predictions,
    update_prediction_outcome,
)
from providers.us import USMarketProvider
from providers.kr import KoreanMarketProvider
from telegram_sender import send_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("outcome_tracker")

US_TZ = ZoneInfo("America/New_York")
KR_TZ = ZoneInfo("Asia/Seoul")
US_MARKET_CLOSE = time(16, 0)
KR_MARKET_CLOSE = time(15, 30)

# Default HIT threshold when no target_price is set (3%)
DEFAULT_HIT_PCT = 0.03
# Default MISS threshold when no stop_price is set (5%)
DEFAULT_MISS_PCT = 0.05


def count_trading_days(start_date: str, market: str) -> int:
    """Count trading days elapsed since prediction creation.

    Excludes weekends. Does not yet exclude market holidays
    (to be added as holiday calendars are maintained).

    Args:
        start_date: ISO format datetime string of prediction creation.
        market: "US" or "KR".

    Returns:
        Number of trading days elapsed.
    """
    start = datetime.fromisoformat(start_date)
    tz = US_TZ if market == "US" else KR_TZ
    now = datetime.now(tz)

    # Iterate through calendar days, count weekdays
    trading_days = 0
    current = start.astimezone(tz).date() + timedelta(days=1)
    end = now.date()

    while current <= end:
        if current.weekday() < 5:  # Monday=0 through Friday=4
            trading_days += 1
        current += timedelta(days=1)

    return trading_days


def evaluate_prediction(
    pred: Prediction,
    current_price: float,
) -> tuple[str | None, float]:
    """Evaluate whether a prediction should be closed.

    Scoring rules (from design doc):
    - HIT (BULL): close >= target_price, or close >= entry * 1.03 if no target
    - HIT (BEAR): close <= target_price, or close <= entry * 0.97 if no target
    - HIT (NEUTRAL): price within +/-3% for entire timeframe
    - MISS: stop breached (BULL: close <= stop, BEAR: close >= stop)
    - EXPIRED: timeframe elapsed without HIT or MISS
    - Priority: MISS checked before HIT

    Args:
        pred: The prediction to evaluate.
        current_price: Latest closing price.

    Returns:
        Tuple of (new_status or None if still open, outcome_return percentage).
    """
    entry = pred.entry_price
    if entry <= 0:
        return None, 0.0

    pct_change = (current_price - entry) / entry
    outcome_return = round(pct_change * 100, 2)

    # --- Check MISS first (conservative: MISS takes priority) ---
    if pred.direction == "BULL":
        stop = pred.stop_price if pred.stop_price else entry * (1 - DEFAULT_MISS_PCT)
        if current_price <= stop:
            return "MISS", outcome_return

    elif pred.direction == "BEAR":
        stop = pred.stop_price if pred.stop_price else entry * (1 + DEFAULT_MISS_PCT)
        if current_price >= stop:
            return "MISS", outcome_return

    # --- Check HIT ---
    if pred.direction == "BULL":
        target = pred.target_price if pred.target_price else entry * (1 + DEFAULT_HIT_PCT)
        if current_price >= target:
            return "HIT", outcome_return

    elif pred.direction == "BEAR":
        target = pred.target_price if pred.target_price else entry * (1 - DEFAULT_HIT_PCT)
        if current_price <= target:
            return "HIT", outcome_return

    elif pred.direction == "NEUTRAL":
        # NEUTRAL hits if price stays within +/-3%
        if abs(pct_change) <= DEFAULT_HIT_PCT:
            # Only HIT if timeframe has elapsed
            timeframe = Timeframe(pred.timeframe)
            required_days = TIMEFRAME_TRADING_DAYS[timeframe]
            elapsed = count_trading_days(pred.created_at, pred.market)
            if elapsed >= required_days:
                return "HIT", outcome_return
        else:
            # Price moved outside neutral range
            return "MISS", outcome_return

    # --- Check EXPIRED ---
    timeframe = Timeframe(pred.timeframe)
    required_days = TIMEFRAME_TRADING_DAYS[timeframe]
    elapsed = count_trading_days(pred.created_at, pred.market)

    if elapsed >= required_days:
        return "EXPIRED", outcome_return

    # Still open
    return None, outcome_return


def run_outcome_tracking() -> dict:
    """Main outcome tracking loop.

    Fetches all open predictions, checks current prices, and updates
    statuses. Sends Telegram alerts for state changes.

    Returns:
        Summary dict with counts of updates by status.
    """
    conn = get_connection()
    us_provider = USMarketProvider()
    kr_provider = KoreanMarketProvider()

    open_preds = list_predictions(conn, status="OPEN", limit=200)
    logger.info("Found %d open predictions to evaluate", len(open_preds))

    summary = {"evaluated": 0, "HIT": 0, "MISS": 0, "EXPIRED": 0, "still_open": 0, "errors": 0}

    for pred in open_preds:
        try:
            # Fetch current price
            provider = us_provider if pred.market == "US" else kr_provider
            current_price = provider.get_current_price(pred.ticker)

            if current_price is None:
                logger.warning(
                    "Could not fetch price for %s (%s) — skipping",
                    pred.ticker,
                    pred.market,
                )
                summary["errors"] += 1
                continue

            summary["evaluated"] += 1

            # Evaluate
            new_status, outcome_return = evaluate_prediction(pred, current_price)

            if new_status is None:
                summary["still_open"] += 1
                logger.info(
                    "%s (%s): still open — current: %.2f, entry: %.2f, P&L: %+.1f%%",
                    pred.ticker,
                    pred.market,
                    current_price,
                    pred.entry_price,
                    outcome_return,
                )
                continue

            # Update prediction
            success = update_prediction_outcome(
                conn, pred.id, new_status, current_price, outcome_return
            )

            if success:
                summary[new_status] += 1
                logger.info(
                    "%s (%s): %s — entry: %.2f → outcome: %.2f (%+.1f%%)",
                    pred.ticker,
                    pred.market,
                    new_status,
                    pred.entry_price,
                    current_price,
                    outcome_return,
                )

                # Send Telegram alert
                try:
                    send_alert(
                        ticker=pred.ticker,
                        market=pred.market,
                        alert_type=new_status,
                        message=(
                            f"Entry: {pred.entry_price:.2f} → {current_price:.2f} "
                            f"({outcome_return:+.1f}%)\n"
                            f"Direction: {pred.direction} | "
                            f"Confidence: {pred.confidence:.0%}\n"
                            f"Thesis: {pred.reasoning[:100]}"
                        ),
                    )
                except Exception as e:
                    logger.warning("Telegram alert failed: %s", e)
            else:
                logger.warning(
                    "%s (%s): update failed — prediction may have been modified concurrently",
                    pred.ticker,
                    pred.market,
                )

        except Exception as e:
            logger.error("Error evaluating %s (%s): %s", pred.ticker, pred.market, e)
            summary["errors"] += 1

    conn.close()
    return summary


if __name__ == "__main__":
    logger.info("Starting outcome tracking run")
    result = run_outcome_tracking()
    logger.info(
        "Outcome tracking complete: %d evaluated, %d HIT, %d MISS, %d EXPIRED, "
        "%d still open, %d errors",
        result["evaluated"],
        result["HIT"],
        result["MISS"],
        result["EXPIRED"],
        result["still_open"],
        result["errors"],
    )
