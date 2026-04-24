"""stock-cli — Unified CLI for stock-expectation.

Provides all functionality previously exposed via MCP servers through a
simple command-line interface. Each subcommand outputs JSON for easy
parsing by Claude Code (or jq, or direct shell use).

Examples:
    stock-cli price NVDA --market US --days 30
    stock-cli price 005930 --market KR --days 10
    stock-cli price-batch AAPL,MSFT,NVDA --market US --days 30
    stock-cli price-batch 005930,000660,035420 --market KR --days 10
    stock-cli fundamentals AAPL --market US
    stock-cli fundamentals-batch AAPL,MSFT,NVDA --market US
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
from datetime import datetime
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
from indicators import compute_horizon_metrics

from portfolio.db import (
    get_connection as pf_get_connection,
    create_portfolio,
    list_portfolios,
    get_portfolio_for_market,
    add_transaction,
    list_transactions,
    delete_transaction,
    compute_positions,
)
from portfolio.csv_import import parse_csv
from portfolio.evaluator import (
    compute_report,
    compute_risk,
    compute_vs_predictions,
    compute_advice,
)


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
            _print_json({"error": f"No price data for {args.ticker} on {args.market}"})
            return 1

        ticker_display = (
            args.ticker.upper() if args.market.upper() == "US" else args.ticker.zfill(6)
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


def cmd_price_batch(args) -> int:
    """Fetch OHLCV price history for multiple tickers at once.

    Accepts comma-separated ticker list. Uses yfinance download() for
    efficient bulk fetching (US) or threaded PyKRX calls (KR).

    Args:
        args: Parsed CLI arguments with tickers, market, days.

    Returns:
        0 on success, 1 on error.
    """
    try:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
        if not tickers:
            _print_json({"error": "No tickers provided"})
            return 1

        provider = _get_provider(args.market)
        results = provider.get_price_history_batch(tickers, days=args.days)

        output = {}
        for ticker, bars in results.items():
            output[ticker] = {
                "bars_count": len(bars),
                "current_price": bars[-1].close if bars else None,
                "bars": [asdict(b) for b in bars],
            }

        _print_json(
            {
                "market": args.market.upper(),
                "tickers_requested": len(tickers),
                "tickers_with_data": sum(
                    1 for v in output.values() if v["bars_count"] > 0
                ),
                "results": output,
            }
        )
        return 0
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_fundamentals_batch(args) -> int:
    """Fetch fundamental data for multiple tickers at once.

    Accepts comma-separated ticker list. Uses ThreadPoolExecutor for
    parallel fetching.

    Args:
        args: Parsed CLI arguments with tickers, market.

    Returns:
        0 on success, 1 on error.
    """
    try:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
        if not tickers:
            _print_json({"error": "No tickers provided"})
            return 1

        provider = _get_provider(args.market)
        results = provider.get_fundamentals_batch(tickers)

        output = {}
        for ticker, fund in results.items():
            output[ticker] = asdict(fund) if fund else None

        _print_json(
            {
                "market": args.market.upper(),
                "tickers_requested": len(tickers),
                "tickers_with_data": sum(1 for v in output.values() if v is not None),
                "results": output,
            }
        )
        return 0
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_horizon_metrics(args) -> int:
    """Compute multi-horizon technical metrics for a ticker.

    Fetches up to ``--days`` calendar days of OHLCV bars (default 400, giving
    ~280 trading days — enough for MA200 and 1Y returns with buffer) and
    runs ``compute_horizon_metrics`` on them. Output is JSON with all MA/RSI/
    return fields plus the composite ``cycle_risk_flag`` used by the expect
    skill's conflict-gating rules.

    Args:
        args: Parsed CLI arguments with ticker, market, days.

    Returns:
        0 on success, 1 on error.
    """
    try:
        provider = _get_provider(args.market)
        bars = provider.get_price_history(args.ticker, days=args.days)
        if not bars:
            _print_json({"error": f"No price data for {args.ticker} on {args.market}"})
            return 1

        ticker_display = (
            args.ticker.upper() if args.market.upper() == "US" else args.ticker.zfill(6)
        )

        metrics = compute_horizon_metrics(
            bars=[asdict(b) for b in bars],
            ticker=ticker_display,
            market=args.market.upper(),
        )
        _print_json(asdict(metrics))
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

        signals = [s.strip() for s in args.signals.split(",")] if args.signals else []

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
            analysis_group_id=args.analysis_group_id,
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
        _print_json({"error": f"Prediction {args.prediction_id} not found or not OPEN"})
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
        buckets = get_calibration_report(conn, timeframe=args.timeframe)
        signals = get_signal_performance(conn, min_count=args.min_signal_count)
        _print_json(
            {
                "timeframe": args.timeframe or "ALL",
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
# Portfolio commands
# ---------------------------------------------------------------------------

_PREDICTIONS_DB = PROJECT_ROOT / "data" / "predictions.db"


def cmd_portfolio_create(args) -> int:
    """Create a new portfolio."""
    try:
        conn = pf_get_connection()
        try:
            pf = create_portfolio(conn, market=args.market, name=args.name)
            _print_json(
                {
                    "id": pf.id,
                    "market": pf.market,
                    "name": pf.name,
                    "created_at": pf.created_at,
                }
            )
            return 0
        finally:
            conn.close()
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_portfolio_list(args) -> int:
    """List all portfolios."""
    conn = pf_get_connection()
    try:
        pfs = list_portfolios(conn)
        _print_json(
            [
                {
                    "id": p.id,
                    "market": p.market,
                    "name": p.name,
                    "created_at": p.created_at,
                }
                for p in pfs
            ]
        )
        return 0
    finally:
        conn.close()


def _portfolio_trade(args, side: str) -> int:
    """Shared logic for buy and sell commands.

    Args:
        args: Parsed CLI arguments with ticker, qty, price, market, etc.
        side: "BUY" or "SELL".

    Returns:
        0 on success, 1 on error.
    """
    try:
        conn = pf_get_connection()
        try:
            pf = get_portfolio_for_market(conn, args.market)
            if pf is None:
                _print_json(
                    {
                        "error": f"No portfolio for market {args.market}. Run 'portfolio create' first."
                    }
                )
                return 1

            currency = "KRW" if args.market.upper() == "KR" else "USD"
            date = args.date or datetime.now().strftime("%Y-%m-%d")
            tx = add_transaction(
                conn,
                portfolio_id=pf.id,
                ticker=args.ticker,
                side=side,
                quantity=args.qty,
                price=args.price,
                currency=currency,
                transacted_at=date,
                note=args.note,
                thesis_id=args.thesis_id,
            )
            _print_json(
                {
                    "id": tx.id,
                    "portfolio_id": tx.portfolio_id,
                    "ticker": tx.ticker,
                    "side": tx.side,
                    "quantity": tx.quantity,
                    "price": tx.price,
                    "currency": tx.currency,
                    "transacted_at": tx.transacted_at,
                    "note": tx.note,
                    "thesis_id": tx.thesis_id,
                }
            )
            return 0
        finally:
            conn.close()
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_portfolio_buy(args) -> int:
    """Record a buy transaction."""
    return _portfolio_trade(args, "BUY")


def cmd_portfolio_sell(args) -> int:
    """Record a sell transaction."""
    return _portfolio_trade(args, "SELL")


def cmd_portfolio_transactions(args) -> int:
    """List transactions."""
    conn = pf_get_connection()
    try:
        pf = get_portfolio_for_market(conn, args.market)
        if pf is None:
            _print_json({"error": f"No portfolio for market {args.market}"})
            return 1
        txs = list_transactions(conn, pf.id, ticker=args.ticker, last_n=args.last)
        _print_json(
            [
                {
                    "id": t.id,
                    "ticker": t.ticker,
                    "side": t.side,
                    "quantity": t.quantity,
                    "price": t.price,
                    "transacted_at": t.transacted_at,
                    "note": t.note,
                }
                for t in txs
            ]
        )
        return 0
    finally:
        conn.close()


def cmd_portfolio_delete_tx(args) -> int:
    """Delete a transaction."""
    conn = pf_get_connection()
    try:
        if delete_transaction(conn, args.tx_id):
            _print_json({"status": "deleted", "id": args.tx_id})
            return 0
        _print_json({"error": f"Transaction {args.tx_id} not found"})
        return 1
    finally:
        conn.close()


def cmd_portfolio_import(args) -> int:
    """Import transactions from CSV."""
    try:
        csv_path = Path(args.csv_file)
        if not csv_path.exists():
            _print_json({"error": f"File not found: {args.csv_file}"})
            return 1

        result = parse_csv(csv_path, market=args.market)

        if args.dry_run:
            _print_json(
                {
                    "dry_run": True,
                    "valid_rows": len(result.rows),
                    "errors": result.errors,
                    "duplicates": result.duplicates,
                }
            )
            return 0

        conn = pf_get_connection()
        try:
            pf = get_portfolio_for_market(conn, args.market)
            if pf is None:
                _print_json(
                    {
                        "error": f"No portfolio for market {args.market}. Run 'portfolio create' first."
                    }
                )
                return 1

            currency = "KRW" if args.market.upper() == "KR" else "USD"
            inserted = 0
            for row in result.rows:
                add_transaction(
                    conn,
                    portfolio_id=pf.id,
                    ticker=row["ticker"],
                    side=row["side"],
                    quantity=row["quantity"],
                    price=row["price"],
                    currency=currency,
                    transacted_at=row["date"],
                    note=row.get("note"),
                    thesis_id=row.get("thesis_id"),
                )
                inserted += 1

            _print_json(
                {
                    "inserted": inserted,
                    "errors": result.errors,
                    "duplicates": result.duplicates,
                }
            )
            return 0
        finally:
            conn.close()
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_portfolio_positions(args) -> int:
    """Show current positions."""
    conn = pf_get_connection()
    try:
        pf = get_portfolio_for_market(conn, args.market)
        if pf is None:
            _print_json({"error": f"No portfolio for market {args.market}"})
            return 1
        positions = compute_positions(conn, pf.id)
        pos_list = [
            {
                "ticker": p.ticker,
                "quantity": p.quantity,
                "avg_price": p.avg_price,
                "total_cost": p.total_cost,
                "realized_pnl": p.realized_pnl,
            }
            for p in positions
        ]
        _print_json({"portfolio": pf.name, "market": pf.market, "positions": pos_list})
        return 0
    finally:
        conn.close()


def cmd_portfolio_summary(args) -> int:
    """Portfolio summary with current prices."""
    try:
        conn = pf_get_connection()
        try:
            pf = get_portfolio_for_market(conn, args.market)
            if pf is None:
                _print_json({"error": f"No portfolio for market {args.market}"})
                return 1
            positions = compute_positions(conn, pf.id)
            provider = _get_provider(args.market)
            current_prices = {}
            for pos in positions:
                price = provider.get_current_price(pos.ticker)
                if price is not None:
                    current_prices[pos.ticker] = price
            report = compute_report(positions, current_prices)
            _print_json({"portfolio": pf.name, "market": pf.market, **report})
            return 0
        finally:
            conn.close()
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_portfolio_report(args) -> int:
    """Full P&L report."""
    try:
        conn = pf_get_connection()
        try:
            pf = get_portfolio_for_market(conn, args.market)
            if pf is None:
                _print_json({"error": f"No portfolio for market {args.market}"})
                return 1
            positions = compute_positions(conn, pf.id)
            provider = _get_provider(args.market)
            current_prices = {}
            for pos in positions:
                price = provider.get_current_price(pos.ticker)
                if price is not None:
                    current_prices[pos.ticker] = price
            report = compute_report(positions, current_prices)
            _print_json({"portfolio": pf.name, "market": pf.market, **report})
            return 0
        finally:
            conn.close()
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_portfolio_risk(args) -> int:
    """Risk analysis."""
    try:
        conn = pf_get_connection()
        try:
            pf = get_portfolio_for_market(conn, args.market)
            if pf is None:
                _print_json({"error": f"No portfolio for market {args.market}"})
                return 1
            positions = compute_positions(conn, pf.id)
            provider = _get_provider(args.market)
            current_prices = {}
            sector_map = {}
            for pos in positions:
                price = provider.get_current_price(pos.ticker)
                if price is not None:
                    current_prices[pos.ticker] = price
                fund = provider.get_fundamentals(pos.ticker)
                if fund and fund.sector:
                    sector_map[pos.ticker] = fund.sector
            risk = compute_risk(positions, current_prices, sector_map)
            _print_json({"portfolio": pf.name, "market": pf.market, **risk})
            return 0
        finally:
            conn.close()
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_portfolio_vs_predictions(args) -> int:
    """Compare holdings against predictions."""
    try:
        conn = pf_get_connection()
        try:
            pf = get_portfolio_for_market(conn, args.market)
            if pf is None:
                _print_json({"error": f"No portfolio for market {args.market}"})
                return 1
            positions = compute_positions(conn, pf.id)
            result = compute_vs_predictions(positions, _PREDICTIONS_DB)
            _print_json({"portfolio": pf.name, "market": pf.market, **result})
            return 0
        finally:
            conn.close()
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_portfolio_advice(args) -> int:
    """Trading advice signals."""
    try:
        conn = pf_get_connection()
        try:
            pf = get_portfolio_for_market(conn, args.market)
            if pf is None:
                _print_json({"error": f"No portfolio for market {args.market}"})
                return 1
            positions = compute_positions(conn, pf.id)
            provider = _get_provider(args.market)
            current_prices = {}
            ma50 = {}
            ma200 = {}
            for pos in positions:
                bars = provider.get_price_history(pos.ticker, days=300)
                if bars:
                    current_prices[pos.ticker] = bars[-1].close
                    closes = [b.close for b in bars]
                    if len(closes) >= 50:
                        ma50[pos.ticker] = sum(closes[-50:]) / 50
                    if len(closes) >= 200:
                        ma200[pos.ticker] = sum(closes[-200:]) / 200
            advice = compute_advice(positions, current_prices, ma50, ma200)
            _print_json({"portfolio": pf.name, "market": pf.market, **advice})
            return 0
        finally:
            conn.close()
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


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

    # --- price-batch ---
    p = sub.add_parser("price-batch", help="Fetch OHLCV for multiple tickers")
    p.add_argument("tickers", help="Comma-separated tickers (e.g. AAPL,MSFT,NVDA)")
    p.add_argument("--market", default="US", choices=["US", "KR", "us", "kr"])
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=cmd_price_batch)

    # --- fundamentals ---
    p = sub.add_parser("fundamentals", help="Fetch fundamental data")
    p.add_argument("ticker")
    p.add_argument("--market", default="US", choices=["US", "KR", "us", "kr"])
    p.set_defaults(func=cmd_fundamentals)

    # --- fundamentals-batch ---
    p = sub.add_parser(
        "fundamentals-batch", help="Fetch fundamentals for multiple tickers"
    )
    p.add_argument("tickers", help="Comma-separated tickers (e.g. AAPL,MSFT,NVDA)")
    p.add_argument("--market", default="US", choices=["US", "KR", "us", "kr"])
    p.set_defaults(func=cmd_fundamentals_batch)

    # --- search ---
    p = sub.add_parser("search", help="Search stocks by name/ticker")
    p.add_argument("query")
    p.add_argument("--market", default="US", choices=["US", "KR", "us", "kr"])
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_search)

    # --- health ---
    p = sub.add_parser("health", help="Check provider health")
    p.set_defaults(func=cmd_health)

    # --- horizon-metrics ---
    p = sub.add_parser(
        "horizon-metrics",
        help="Compute multi-horizon technical metrics (MA/RSI/returns/cycle risk)",
    )
    p.add_argument("ticker", help="Stock ticker (e.g. NVDA or 005930)")
    p.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    p.add_argument(
        "--days",
        type=int,
        default=400,
        help="Calendar days of history to fetch (default 400 ~ 280 trading days)",
    )
    p.set_defaults(func=cmd_horizon_metrics)

    # --- predict ---
    predict = sub.add_parser("predict", help="Prediction CRUD")
    predict_sub = predict.add_subparsers(dest="predict_command", required=True)

    pc = predict_sub.add_parser("create", help="Create a new prediction")
    pc.add_argument("--ticker", required=True)
    pc.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    pc.add_argument("--direction", required=True, choices=["BULL", "BEAR", "NEUTRAL"])
    pc.add_argument("--confidence", type=float, required=True)
    pc.add_argument(
        "--timeframe",
        required=True,
        choices=["1W", "2W", "1M", "3M", "6M", "1Y"],
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
    pc.add_argument(
        "--analysis-group-id",
        default=None,
        help="UUID linking predictions from the same multi-horizon analysis",
    )
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
        "--timeframe",
        choices=["1W", "2W", "1M", "3M", "6M", "1Y"],
        default=None,
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
    cal.add_argument(
        "--timeframe",
        choices=["1W", "2W", "1M", "3M", "6M", "1Y"],
        default=None,
        help="Restrict calibration buckets to a single horizon (default: all)",
    )
    cal.set_defaults(func=cmd_calibration)

    # --- portfolio ---
    pf = sub.add_parser("portfolio", help="Portfolio tracking and evaluation")
    pf_sub = pf.add_subparsers(dest="portfolio_command", required=True)

    pc = pf_sub.add_parser("create", help="Create a portfolio")
    pc.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    pc.add_argument("--name", required=True, help="Portfolio display name")
    pc.set_defaults(func=cmd_portfolio_create)

    pl = pf_sub.add_parser("list", help="List all portfolios")
    pl.set_defaults(func=cmd_portfolio_list)

    pb = pf_sub.add_parser("buy", help="Record a buy")
    pb.add_argument("ticker", help="Stock ticker")
    pb.add_argument("--qty", type=float, required=True)
    pb.add_argument("--price", type=float, required=True)
    pb.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    pb.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    pb.add_argument("--note", default=None)
    pb.add_argument("--thesis-id", default=None)
    pb.set_defaults(func=cmd_portfolio_buy)

    ps = pf_sub.add_parser("sell", help="Record a sell")
    ps.add_argument("ticker", help="Stock ticker")
    ps.add_argument("--qty", type=float, required=True)
    ps.add_argument("--price", type=float, required=True)
    ps.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    ps.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ps.add_argument("--note", default=None)
    ps.add_argument("--thesis-id", default=None)
    ps.set_defaults(func=cmd_portfolio_sell)

    pt = pf_sub.add_parser("transactions", help="List transactions")
    pt.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    pt.add_argument("--ticker", default=None)
    pt.add_argument("--last", type=int, default=None)
    pt.set_defaults(func=cmd_portfolio_transactions)

    pd_tx = pf_sub.add_parser("delete-tx", help="Delete a transaction")
    pd_tx.add_argument("tx_id", type=int)
    pd_tx.set_defaults(func=cmd_portfolio_delete_tx)

    pi = pf_sub.add_parser("import", help="Import from CSV")
    pi.add_argument("csv_file")
    pi.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    pi.add_argument("--dry-run", action="store_true")
    pi.set_defaults(func=cmd_portfolio_import)

    pp = pf_sub.add_parser("positions", help="Current positions")
    pp.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    pp.set_defaults(func=cmd_portfolio_positions)

    psum = pf_sub.add_parser("summary", help="Portfolio summary with live prices")
    psum.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    psum.set_defaults(func=cmd_portfolio_summary)

    pr = pf_sub.add_parser("report", help="Full P&L report")
    pr.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    pr.set_defaults(func=cmd_portfolio_report)

    prk = pf_sub.add_parser("risk", help="Risk analysis")
    prk.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    prk.set_defaults(func=cmd_portfolio_risk)

    pvp = pf_sub.add_parser("vs-predictions", help="Compare with predictions")
    pvp.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    pvp.set_defaults(func=cmd_portfolio_vs_predictions)

    pa = pf_sub.add_parser("advice", help="Trading advice signals")
    pa.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    pa.set_defaults(func=cmd_portfolio_advice)

    return parser


def main() -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
