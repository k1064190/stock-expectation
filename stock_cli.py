"""stock-cli — Unified CLI for stock-expectation.

Provides all functionality previously exposed via MCP servers through a
simple command-line interface. Each subcommand outputs JSON for easy
parsing by Claude Code (or jq, or direct shell use).

Examples:
    stock-cli price NVDA --market US --days 30
    stock-cli price 005930 --market KR --days 10
    stock-cli fundamentals AAPL --market US
    stock-cli search "삼성" --market KR
    stock-cli health

    stock-cli predict create --ticker NVDA --market US --direction BULL \\
        --confidence 0.70 --timeframe 1W --entry-price 120.50 \\
        --target-price 128 --stop-price 116 \\
        --reasoning "Strong breakout" --signals technical,momentum

    stock-cli predict list --status OPEN
    stock-cli predict list --market KR --limit 10
    stock-cli predict detail <id>
    stock-cli predict cancel <id>

    stock-cli track-record --days 30
    stock-cli track-record --market US --timeframe 1W
    stock-cli calibration
"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Optional

# Add provider and store paths
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-prediction-store"))
sys.path.insert(0, str(PROJECT_ROOT / "mcp-market-data"))

from models import (
    Prediction,
    Direction,
    Market,
    Source,
    Timeframe,
    get_connection,
    insert_prediction,
    get_prediction as db_get_prediction,
    list_predictions as db_list_predictions,
    cancel_prediction as db_cancel_prediction,
)
from metrics import (
    get_track_record,
    get_calibration_report,
    get_signal_performance,
)
from providers.us import USMarketProvider
from providers.kr import KoreanMarketProvider


def _print_json(data) -> None:
    """Print data as indented JSON to stdout.

    Args:
        data: Any JSON-serializable object (dict, list, etc.).
    """
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _get_provider(market: str):
    """Return the market data provider for a given market.

    Args:
        market: "US" or "KR" (case-insensitive).

    Returns:
        Provider instance.

    Raises:
        ValueError: If market is not recognized.
    """
    market = market.upper()
    if market == "US":
        return USMarketProvider()
    elif market == "KR":
        return KoreanMarketProvider()
    else:
        raise ValueError(f"Unknown market: {market}. Use 'US' or 'KR'.")


# ---------------------------------------------------------------------------
# Market data commands
# ---------------------------------------------------------------------------


def cmd_price(args) -> int:
    """Fetch OHLCV price history for a stock."""
    try:
        provider = _get_provider(args.market)
        bars = provider.get_price_history(args.ticker, days=args.days)

        if not bars:
            _print_json(
                {"error": f"No price data for {args.ticker} on {args.market}"}
            )
            return 1

        ticker_display = (
            args.ticker.upper()
            if args.market.upper() == "US"
            else args.ticker.zfill(6)
        )

        _print_json(
            {
                "ticker": ticker_display,
                "market": args.market.upper(),
                "current_price": bars[-1].close,
                "bars_count": len(bars),
                "bars": [asdict(b) for b in bars],
            }
        )
        return 0
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_fundamentals(args) -> int:
    """Fetch fundamental data for a stock."""
    try:
        provider = _get_provider(args.market)
        fund = provider.get_fundamentals(args.ticker)

        if fund is None:
            _print_json(
                {"error": f"No fundamentals for {args.ticker} on {args.market}"}
            )
            return 1

        _print_json(asdict(fund))
        return 0
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_search(args) -> int:
    """Search stocks by name or ticker."""
    try:
        provider = _get_provider(args.market)
        results = provider.search_stocks(args.query, limit=args.limit)
        _print_json(results)
        return 0
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_health(args) -> int:
    """Check if market data providers are responsive."""
    us = USMarketProvider()
    kr = KoreanMarketProvider()
    _print_json(
        {
            "us": us.is_healthy(),
            "kr": kr.is_healthy(),
        }
    )
    return 0


# ---------------------------------------------------------------------------
# Prediction commands
# ---------------------------------------------------------------------------


def cmd_predict_create(args) -> int:
    """Create a new prediction."""
    try:
        # Validate enums
        Market(args.market.upper())
        Direction(args.direction.upper())
        Timeframe(args.timeframe)
        Source(args.source.upper())

        if not 0.0 <= args.confidence <= 1.0:
            _print_json({"error": "confidence must be between 0.0 and 1.0"})
            return 1

        signals = (
            [s.strip() for s in args.signals.split(",")] if args.signals else []
        )

        pred = Prediction(
            ticker=args.ticker.upper(),
            market=args.market.upper(),
            direction=args.direction.upper(),
            confidence=args.confidence,
            timeframe=args.timeframe,
            reasoning=args.reasoning,
            entry_price=args.entry_price,
            signals_used=signals,
            source=args.source.upper(),
            target_price=args.target_price,
            stop_price=args.stop_price,
        )

        conn = get_connection()
        try:
            insert_prediction(conn, pred)
            _print_json(asdict(pred))
            return 0
        finally:
            conn.close()
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_predict_list(args) -> int:
    """List predictions with optional filters."""
    conn = get_connection()
    try:
        preds = db_list_predictions(
            conn,
            status=args.status,
            market=args.market,
            ticker=args.ticker.upper() if args.ticker else None,
            source=args.source,
            limit=args.limit,
        )
        _print_json([asdict(p) for p in preds])
        return 0
    finally:
        conn.close()


def cmd_predict_detail(args) -> int:
    """Get full details for a single prediction."""
    conn = get_connection()
    try:
        pred = db_get_prediction(conn, args.prediction_id)
        if pred is None:
            _print_json({"error": f"Prediction {args.prediction_id} not found"})
            return 1
        _print_json(asdict(pred))
        return 0
    finally:
        conn.close()


def cmd_predict_cancel(args) -> int:
    """Cancel an open prediction."""
    conn = get_connection()
    try:
        success = db_cancel_prediction(conn, args.prediction_id)
        if success:
            _print_json({"status": "cancelled", "id": args.prediction_id})
            return 0
        _print_json(
            {"error": f"Prediction {args.prediction_id} not found or not OPEN"}
        )
        return 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Metrics commands
# ---------------------------------------------------------------------------


def cmd_track_record(args) -> int:
    """Show accuracy statistics."""
    conn = get_connection()
    try:
        record = get_track_record(
            conn,
            market=args.market,
            timeframe=args.timeframe,
            source=args.source,
            days=args.days,
        )
        _print_json(
            {
                "period_days": args.days,
                "market": args.market or "ALL",
                "timeframe": args.timeframe or "ALL",
                "total_predictions": record.total,
                "wins": record.wins,
                "losses": record.losses,
                "expired": record.expired,
                "win_rate": record.win_rate,
                "avg_return_pct": record.avg_return,
                "current_streak": record.current_streak,
                "brier_score": record.brier_score,
            }
        )
        return 0
    finally:
        conn.close()


def cmd_calibration(args) -> int:
    """Show calibration report (predicted vs actual accuracy)."""
    conn = get_connection()
    try:
        buckets = get_calibration_report(conn)
        signals = get_signal_performance(conn, min_count=args.min_signal_count)
        _print_json(
            {
                "calibration": [
                    {
                        "range": b.confidence_range,
                        "predicted": b.predicted_confidence,
                        "actual": b.actual_accuracy,
                        "count": b.count,
                    }
                    for b in buckets
                ],
                "signal_performance": [
                    {
                        "signal": s.signal,
                        "total": s.total,
                        "wins": s.wins,
                        "win_rate": s.win_rate,
                    }
                    for s in signals
                ],
            }
        )
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Argparse setup
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="stock-cli",
        description="Stock Expectation CLI — market data and predictions",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- price ---
    p = sub.add_parser("price", help="Fetch OHLCV price history")
    p.add_argument("ticker", help="Stock ticker (e.g. NVDA or 005930)")
    p.add_argument("--market", default="US", choices=["US", "KR", "us", "kr"])
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=cmd_price)

    # --- fundamentals ---
    p = sub.add_parser("fundamentals", help="Fetch fundamental data")
    p.add_argument("ticker")
    p.add_argument("--market", default="US", choices=["US", "KR", "us", "kr"])
    p.set_defaults(func=cmd_fundamentals)

    # --- search ---
    p = sub.add_parser("search", help="Search stocks by name/ticker")
    p.add_argument("query")
    p.add_argument("--market", default="US", choices=["US", "KR", "us", "kr"])
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_search)

    # --- health ---
    p = sub.add_parser("health", help="Check provider health")
    p.set_defaults(func=cmd_health)

    # --- predict ---
    predict = sub.add_parser("predict", help="Prediction CRUD")
    predict_sub = predict.add_subparsers(dest="predict_command", required=True)

    pc = predict_sub.add_parser("create", help="Create a new prediction")
    pc.add_argument("--ticker", required=True)
    pc.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    pc.add_argument(
        "--direction", required=True, choices=["BULL", "BEAR", "NEUTRAL"]
    )
    pc.add_argument("--confidence", type=float, required=True)
    pc.add_argument(
        "--timeframe", required=True, choices=["1W", "2W", "1M", "3M"]
    )
    pc.add_argument("--entry-price", type=float, required=True)
    pc.add_argument("--reasoning", required=True)
    pc.add_argument(
        "--signals",
        default="",
        help="Comma-separated signals (e.g. technical,breadth)",
    )
    pc.add_argument(
        "--source",
        default="INTERACTIVE",
        choices=["LIVE", "BACKTEST", "INTERACTIVE", "live", "backtest", "interactive"],
    )
    pc.add_argument("--target-price", type=float, default=None)
    pc.add_argument("--stop-price", type=float, default=None)
    pc.set_defaults(func=cmd_predict_create)

    pl = predict_sub.add_parser("list", help="List predictions")
    pl.add_argument(
        "--status",
        choices=["OPEN", "HIT", "MISS", "EXPIRED", "CANCELLED"],
        default=None,
    )
    pl.add_argument("--market", choices=["US", "KR"], default=None)
    pl.add_argument("--ticker", default=None)
    pl.add_argument(
        "--source", choices=["LIVE", "BACKTEST", "INTERACTIVE"], default=None
    )
    pl.add_argument("--limit", type=int, default=20)
    pl.set_defaults(func=cmd_predict_list)

    pd = predict_sub.add_parser("detail", help="Get full prediction details")
    pd.add_argument("prediction_id")
    pd.set_defaults(func=cmd_predict_detail)

    px = predict_sub.add_parser("cancel", help="Cancel an open prediction")
    px.add_argument("prediction_id")
    px.set_defaults(func=cmd_predict_cancel)

    # --- track-record ---
    tr = sub.add_parser("track-record", help="Show accuracy statistics")
    tr.add_argument("--market", choices=["US", "KR"], default=None)
    tr.add_argument(
        "--timeframe", choices=["1W", "2W", "1M", "3M"], default=None
    )
    tr.add_argument(
        "--source", choices=["LIVE", "BACKTEST", "INTERACTIVE"], default=None
    )
    tr.add_argument("--days", type=int, default=30)
    tr.set_defaults(func=cmd_track_record)

    # --- calibration ---
    cal = sub.add_parser(
        "calibration", help="Show calibration curve and signal performance"
    )
    cal.add_argument(
        "--min-signal-count",
        type=int,
        default=10,
        help="Minimum prediction count to report a signal (default 10)",
    )
    cal.set_defaults(func=cmd_calibration)

    return parser


def main() -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
