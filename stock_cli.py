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
sys.path.insert(0, str(PROJECT_ROOT / "mcp-memory-store"))
sys.path.insert(0, str(PROJECT_ROOT / "mcp-graph-store"))

# Auto-load API keys from .env at the project root. The file is gitignored,
# so committing this call is safe — it just reads whatever the user has
# placed there. Existing env vars take precedence over .env values.
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    # python-dotenv is a base dependency, but tolerate its absence so
    # downstream callers can still import this module in stripped envs.
    pass

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
    get_track_record_ci,
    get_calibration_report,
    get_signal_performance,
    get_signal_decay,
    permutation_test_confidence,
    build_recalibration_map,
    apply_recalibration,
    recalibrate_confidence,
    get_component_contribution,
)
from providers.us import USMarketProvider
from providers.kr import KoreanMarketProvider
from indicators import compute_horizon_metrics
from regime import aggregate_regime, compute_regime, compute_realized_vol
from news_features import summarize_news
from llm_context import validate_llm_context, score_from_debate

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
from portfolio.toss_sync import fetch_positions, reconcile


def _positive_int(value: str) -> int:
    """argparse type for arguments that must be a positive integer.

    Catches `--limit 0`, `--limit -1`, `--since-days 0` etc. before they
    reach the provider, where negative slicing or zero-window queries
    would silently return wrong results.
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {parsed}")
    return parsed


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


def cmd_horizon_metrics_batch(args) -> int:
    """Compute horizon-metrics for multiple tickers in one call.

    Mirrors ``price-batch`` — accepts comma-separated tickers, calls the
    provider's bulk price-history fetch, then runs ``compute_horizon_metrics``
    per ticker. Failures for individual tickers are reported in the result
    object; the call itself succeeds as long as at least one ticker resolved.

    Args:
        args: Parsed CLI arguments with tickers, market, days.

    Returns:
        0 on success (≥1 ticker resolved), 1 on total failure.
    """
    try:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
        if not tickers:
            _print_json({"error": "No tickers provided"})
            return 1

        provider = _get_provider(args.market)
        bars_by_ticker = provider.get_price_history_batch(tickers, days=args.days)

        market = args.market.upper()
        results: dict[str, dict] = {}
        for ticker, bars in bars_by_ticker.items():
            display = ticker.upper() if market == "US" else ticker.zfill(6)
            if not bars:
                results[display] = {"error": "No price data"}
                continue
            try:
                metrics = compute_horizon_metrics(
                    bars=[asdict(b) for b in bars],
                    ticker=display,
                    market=market,
                )
                results[display] = asdict(metrics)
            except Exception as exc:
                results[display] = {"error": str(exc)}

        any_ok = any("error" not in r for r in results.values())
        _print_json(
            {
                "market": market,
                "tickers_requested": len(tickers),
                "tickers_with_data": sum(
                    1 for r in results.values() if "error" not in r
                ),
                "results": results,
            }
        )
        return 0 if any_ok else 1
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_screen_presurge(args) -> int:
    """Screen the universe for pre-surge setups (before a stock runs).

    Complements the legacy momentum funnel: surfaces base/pivot, pullback,
    relative-strength-leader, and (US-only) pre-earnings candidates that are NOT
    yet extended. Outputs a JSON list tagged with discovery_source/setup_type.

    Args:
        args: Parsed CLI args (market, top_n, min_score, days, with_earnings).

    Returns:
        0 on success, 1 on failure.
    """
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "scheduler"))
        from pre_surge_discovery import discover_presurge_candidates

        cands = discover_presurge_candidates(
            market=args.market,
            with_earnings=bool(getattr(args, "with_earnings", False)),
            top_n_output=args.top_n,
            min_score=args.min_score,
            days=args.days,
        )
        results = [
            {
                "ticker": c.ticker,
                "name": c.name,
                "market": c.market,
                "discovery_source": c.discovery_source,
                "setup_type": c.setup_type,
                "return_5d_pct": round(c.return_5d_pct, 2),
                "vol_ratio": round(c.vol_ratio_5d, 2),
                "market_cap": c.market_cap,
            }
            for c in cands
        ]
        _print_json(
            {
                "market": args.market.upper(),
                "min_score": args.min_score,
                "count": len(results),
                "candidates": results,
            }
        )
        return 0
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


# Default liquid index proxies for the market-regime gate. US uses both the
# broad market (SPY) and the tech/growth proxy (QQQ) because the June 2026
# drawdown was growth-led — SPY stayed calm while QQQ broke down — and the gate
# takes the more risk-off of the two.
REGIME_INDEX = {"US": ["SPY", "QQQ"], "KR": ["069500"]}  # KR: KODEX 200 ETF


def _regime_for_proxy(provider, ticker, market, days):
    """Fetch one index proxy and compute its RegimeVerdict, or None if no data."""
    bars = provider.get_price_history(ticker, days=days)
    if not bars:
        return None
    bar_dicts = [asdict(b) for b in bars]
    metrics = compute_horizon_metrics(bars=bar_dicts, ticker=ticker, market=market)
    closes = [b["close"] for b in bar_dicts if b.get("close") is not None]
    return compute_regime(metrics, compute_realized_vol(closes))


def cmd_regime(args) -> int:
    """Classify the current market regime (RISK_ON / NEUTRAL / RISK_OFF).

    Fetches ~400 calendar days for each index proxy of the market (SPY + QQQ for
    US, KODEX 200 for KR; override with ``--index``), computes each one's
    HorizonMetrics + 20-day realized volatility via ``compute_regime``, and
    aggregates to the most risk-off verdict. This is the hard gate the /expect
    and daily-briefing skills consult before issuing new BULL calls. Read-only;
    JSON output.

    Args:
        args: Parsed CLI arguments with market, optional index, days.

    Returns:
        0 on success, 1 on error.
    """
    try:
        market = args.market.upper()
        proxies = [args.index] if args.index else REGIME_INDEX.get(market)
        if not proxies:
            _print_json({"error": f"no default index for market {market}"})
            return 1
        provider = _get_provider(market)
        verdicts = []
        missing = []
        for t in proxies:
            v = _regime_for_proxy(provider, t, market, args.days)
            (verdicts if v is not None else missing).append(v if v is not None else t)
        if not verdicts:
            _print_json(
                {"error": f"No price data for index proxies {proxies} on {market}"}
            )
            return 1
        verdict = aggregate_regime(verdicts)
        if missing:
            # A dropped proxy weakens the gate (e.g. losing QQQ blinds it to a
            # growth-led drawdown). Don't certify RISK_ON on a partial proxy set —
            # the /expect hard gate would then re-enable longs in exactly the
            # case the worse-of gate exists to catch. Floor to NEUTRAL and flag.
            note = f"⚠️ regime computed without proxies {missing} (no data)"
            if verdict.label == "RISK_ON":
                verdict.label = "NEUTRAL"
                note += " — floored RISK_ON to NEUTRAL"
            verdict.notes.append(note)
        _print_json(asdict(verdict))
        return 0
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_news(args) -> int:
    """Fetch recent news headlines for a ticker.

    US: Finnhub primary + Alpha Vantage sentiment merge + FMP/yfinance
    fallbacks. KR: Naver Finance scrape (no sentiment).

    Args:
        args: Parsed CLI arguments with ticker, market, limit, since_days.

    Returns:
        0 on success (even if 0 items returned), 1 on provider error.
    """
    try:
        provider = _get_provider(args.market)
        items = provider.get_news(
            args.ticker, limit=args.limit, since_days=args.since_days
        )
        market = args.market.upper()
        ticker_display = args.ticker.upper() if market == "US" else args.ticker.zfill(6)
        now = datetime.now()
        signal = summarize_news(items, asof_date=now.date().isoformat())
        _print_json(
            {
                "ticker": ticker_display,
                "market": market,
                "generated_at": now.isoformat(timespec="seconds"),
                "since_days": args.since_days,
                "count": len(items),
                "signal": asdict(signal),
                "items": [asdict(n) for n in items],
            }
        )
        return 0
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_disclosure(args) -> int:
    """Fetch recent KR regulatory disclosures from Open DART.

    Requires ``OPEN_DART_API_KEY`` env var. On first call, downloads and
    caches the corp_code mapping CSV at ``data/dart_corp_codes.csv``.

    Args:
        args: Parsed CLI arguments with ticker, since_days, limit.

    Returns:
        0 on success (even if 0 items), 1 on provider error.
    """
    try:
        provider = KoreanMarketProvider()
        items = provider.get_disclosures(
            args.ticker, since_days=args.since_days, limit=args.limit
        )
        _print_json(
            {
                "ticker": args.ticker.zfill(6),
                "market": "KR",
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "since_days": args.since_days,
                "count": len(items),
                "items": [asdict(d) for d in items],
            }
        )
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
# Memory commands (Stage 7-A: mem0 layer)
# ---------------------------------------------------------------------------


def _memory_store():
    """Lazy-import the MemoryStore. Returns the instance or prints a hint and
    raises ``SystemExit`` if the ``memory`` extra is not installed.
    """
    try:
        from client import MemoryStore  # type: ignore[import-not-found]
    except ImportError:
        _print_json(
            {
                "error": "mcp-memory-store module unreadable",
                "hint": "ensure mcp-memory-store/ is on sys.path (it is by default)",
            }
        )
        raise SystemExit(1)
    return MemoryStore()


def cmd_memory_search(args) -> int:
    """Semantic search within a memory category."""
    try:
        from schemas import CATEGORIES  # type: ignore[import-not-found]
    except ImportError:
        _print_json({"error": "mcp-memory-store unavailable"})
        return 1
    if args.category not in CATEGORIES:
        _print_json(
            {"error": f"unknown category {args.category!r}", "valid": list(CATEGORIES)}
        )
        return 1
    store = _memory_store()
    try:
        hits = store.search(args.query, category=args.category, limit=args.limit)
    except Exception as exc:
        _print_json({"error": str(exc)})
        return 1
    _print_json(
        {
            "query": args.query,
            "category": args.category,
            "count": len(hits),
            "hits": [
                {
                    "memory": h.memory,
                    "score": h.score,
                    "metadata": h.metadata,
                    "memory_id": h.memory_id,
                }
                for h in hits
            ],
        }
    )
    return 0


def cmd_memory_add(args) -> int:
    """Add a memory record. ``--content`` is the embedded text;
    ``--metadata-json`` is a JSON string for filterable metadata.
    """
    try:
        from schemas import MemoryRecord  # type: ignore[import-not-found]
    except ImportError:
        _print_json({"error": "mcp-memory-store unavailable"})
        return 1

    metadata: dict = {}
    if args.metadata_json:
        try:
            metadata = json.loads(args.metadata_json)
        except json.JSONDecodeError as exc:
            _print_json({"error": f"--metadata-json invalid: {exc}"})
            return 1

    store = _memory_store()
    try:
        memory_id = store.add(
            MemoryRecord(
                category=args.category, content=args.content, metadata=metadata
            )
        )
    except Exception as exc:
        _print_json({"error": str(exc)})
        return 1
    _print_json({"memory_id": memory_id, "category": args.category})
    return 0


def cmd_memory_stats(args) -> int:
    """Return per-category memory counts."""
    store = _memory_store()
    try:
        counts = store.stats()
    except Exception as exc:
        _print_json({"error": str(exc)})
        return 1
    _print_json(
        {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "counts": counts,
        }
    )
    return 0


def cmd_memory_purge(args) -> int:
    """Drop all memories in a category. Requires ``--yes`` to actually run."""
    if not args.yes:
        _print_json(
            {
                "error": "purge is destructive",
                "hint": f"re-run with --yes to drop all memories in {args.category!r}",
            }
        )
        return 1
    store = _memory_store()
    try:
        deleted = store.purge(args.category)
    except Exception as exc:
        _print_json({"error": str(exc)})
        return 1
    _print_json({"category": args.category, "deleted": deleted})
    return 0


# ---------------------------------------------------------------------------
# Graph commands (Stage 7-B: Neo4j Community)
# ---------------------------------------------------------------------------


def _graph_driver():
    """Lazy-load the GraphDriver. Errors print as JSON and exit non-zero."""
    try:
        from driver import GraphDriver  # type: ignore[import-not-found]
    except ImportError:
        _print_json({"error": "mcp-graph-store unavailable"})
        raise SystemExit(1)
    return GraphDriver()


def cmd_graph_init(args) -> int:
    """Create constraints + indexes. Idempotent."""
    try:
        from cypher import INIT_STATEMENTS  # type: ignore[import-not-found]
    except ImportError:
        _print_json({"error": "mcp-graph-store unavailable"})
        return 1
    driver = _graph_driver()
    try:
        applied = driver.run_many(list(INIT_STATEMENTS))
    except Exception as exc:
        _print_json({"error": str(exc)})
        return 1
    finally:
        driver.close()
    _print_json({"statements_total": len(INIT_STATEMENTS), "applied": applied})
    return 0


def cmd_graph_query(args) -> int:
    """Run a raw Cypher statement. Use sparingly; prefer canned shortcuts."""
    driver = _graph_driver()
    params = {}
    if args.params_json:
        try:
            params = json.loads(args.params_json)
        except json.JSONDecodeError as exc:
            _print_json({"error": f"--params-json invalid: {exc}"})
            return 1
    try:
        rows = driver.run(args.cypher, **params)
    except Exception as exc:
        _print_json({"error": str(exc)})
        return 1
    finally:
        driver.close()
    _print_json(
        {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "count": len(rows),
            "rows": rows,
        }
    )
    return 0


def cmd_graph_similar_stocks(args) -> int:
    """Find stocks sharing themes with the given ticker."""
    try:
        from cypher import CANNED_QUERIES  # type: ignore[import-not-found]
    except ImportError:
        _print_json({"error": "mcp-graph-store unavailable"})
        return 1
    driver = _graph_driver()
    try:
        rows = driver.run(
            CANNED_QUERIES["similar_stocks_by_theme"],
            ticker=args.ticker,
            market=args.market.upper(),
            limit=args.limit,
        )
    except Exception as exc:
        _print_json({"error": str(exc)})
        return 1
    finally:
        driver.close()
    _print_json({"ticker": args.ticker, "market": args.market.upper(), "results": rows})
    return 0


def cmd_graph_theme_winners(args) -> int:
    """Win rate per theme over the last N weeks."""
    try:
        from cypher import CANNED_QUERIES  # type: ignore[import-not-found]
    except ImportError:
        _print_json({"error": "mcp-graph-store unavailable"})
        return 1
    driver = _graph_driver()
    try:
        rows = driver.run(
            CANNED_QUERIES["theme_winners_recent"],
            weeks=args.weeks,
            limit=args.limit,
        )
    except Exception as exc:
        _print_json({"error": str(exc)})
        return 1
    finally:
        driver.close()
    _print_json({"weeks": args.weeks, "results": rows})
    return 0


# ---------------------------------------------------------------------------
# Prediction commands
# ---------------------------------------------------------------------------


# Minimum closed (HIT/MISS) predictions of a source before its recalibration
# curve is trustworthy enough to apply live. Below this the isotonic map is
# fit on too few outcomes to be anything but noise, so --recalibrate is a no-op.
MIN_CLOSED_FOR_RECAL = 30


def _recalibrated_confidence(
    conn, raw_confidence: float, source: str
) -> tuple[float, bool]:
    """Map a raw confidence through the source's isotonic recalibration curve.

    The curve is built from *closed* (HIT/MISS) predictions of the same source
    so live confidences are calibrated against realised accuracy rather than the
    model's near-constant raw output. A global (all-horizon) map is used: the
    backfill A/B found it as accurate as per-horizon maps and far more robust on
    the current sample size.

    Args:
        conn: SQLite connection to predictions.db.
        raw_confidence: The model's raw confidence in [0, 1].
        source: Prediction source (LIVE/BACKTEST/INTERACTIVE) whose history
            defines the calibration curve.

    Returns:
        (confidence_to_store, applied). ``applied`` is False — and the raw
        confidence is returned unchanged — when there are fewer than
        ``MIN_CLOSED_FOR_RECAL`` closed predictions for the source.
    """
    return recalibrate_confidence(
        conn, raw_confidence, source, min_closed=MIN_CLOSED_FOR_RECAL
    )


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

        components = None
        if args.components:
            try:
                # Reject non-finite numbers — the NaN/Infinity literals (via
                # parse_constant) and ordinary float tokens that overflow to inf
                # like 1e999 (via parse_float). Either would serialize back out
                # as non-standard JSON and skew the positive/negative split.
                def _no_nan(_tok):
                    raise ValueError(f"non-finite number in --components: {_tok}")

                def _finite_float(_tok):
                    v = float(_tok)
                    if v in (float("inf"), float("-inf")):
                        raise ValueError(f"non-finite number in --components: {_tok}")
                    return v

                components = json.loads(
                    args.components, parse_constant=_no_nan, parse_float=_finite_float
                )
            except (json.JSONDecodeError, ValueError) as exc:
                _print_json({"error": f"--components is not valid JSON: {exc}"})
                return 1
            if not isinstance(components, dict):
                _print_json({"error": "--components must be a JSON object"})
                return 1

        conn = get_connection()
        try:
            raw_confidence = args.confidence
            recal_applied = False
            stored_confidence = raw_confidence
            if args.recalibrate:
                stored_confidence, recal_applied = _recalibrated_confidence(
                    conn, raw_confidence, args.source.upper()
                )

            pred = Prediction(
                ticker=args.ticker.upper(),
                market=args.market.upper(),
                direction=args.direction.upper(),
                confidence=stored_confidence,
                # Always persist the model's raw confidence so the calibration
                # curve trains on raw output, never on recalibrated values.
                raw_confidence=raw_confidence,
                timeframe=args.timeframe,
                reasoning=args.reasoning,
                entry_price=args.entry_price,
                signals_used=signals,
                source=args.source.upper(),
                target_price=args.target_price,
                stop_price=args.stop_price,
                analysis_group_id=args.analysis_group_id,
                components=components,
            )

            insert_prediction(conn, pred)
            out = asdict(pred)
            if args.recalibrate:
                # Surface the transform so the caller/log can see what happened.
                out["raw_confidence"] = round(raw_confidence, 4)
                out["recalibration_applied"] = recal_applied
            _print_json(out)
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
        out = {
            "period_days": args.days,
            "market": args.market or "ALL",
            "timeframe": args.timeframe or "ALL",
            "source": args.source or "ALL",
            "total_predictions": record.total,
            "wins": record.wins,
            "losses": record.losses,
            "expired": record.expired,
            "win_rate": record.win_rate,
            "avg_return_pct": record.avg_return,
            "current_streak": record.current_streak,
            "brier_score": record.brier_score,
        }
        # When sources are blended, break out per-source: LIVE (real cron/skill
        # performance) and INTERACTIVE (manual deep-dives) are NOT comparable —
        # they cover different periods and selection, so the blended win rate
        # misleads. See docs/stage-11/leakage-audit.md.
        if args.source is None:
            out["by_source"] = {}
            for src in ("LIVE", "INTERACTIVE", "BACKTEST"):
                r = get_track_record(
                    conn,
                    market=args.market,
                    timeframe=args.timeframe,
                    source=src,
                    days=args.days,
                )
                if r.total:
                    out["by_source"][src] = {
                        "total": r.total,
                        "win_rate": r.win_rate,
                        "brier_score": r.brier_score,
                    }
        _print_json(out)
        return 0
    finally:
        conn.close()


def cmd_lint_llm_context(args) -> int:
    """Lint a structured LLM_CONTEXT debate for rigor (range, sign, evidence).

    Returns 0 when clean, 1 when issues are found (so a skill or CI can gate on
    it). Echoes the clamped score and any violations as JSON.

    Args:
        args: Parsed CLI arguments with ``debate`` (a JSON string).

    Returns:
        0 if the debate passes all rigor checks, 1 otherwise.
    """
    try:
        # ValueError (not just JSONDecodeError) covers oversized integer literals
        # that exceed Python's int-string digit limit — a hostile debate must get
        # the JSON error response, not an uncaught traceback.
        debate = json.loads(args.debate)
    except (json.JSONDecodeError, ValueError) as exc:
        _print_json({"error": f"debate is not valid JSON: {exc}"})
        return 1
    issues = validate_llm_context(debate)
    _print_json(
        {
            "clean": not issues,
            "clamped_score": score_from_debate(debate),
            "issues": issues,
        }
    )
    return 0 if not issues else 1


def cmd_component_contribution(args) -> int:
    """Show win-rate by each stored per-pillar component (algo/news/llm/gates)."""
    conn = get_connection()
    try:
        _print_json(get_component_contribution(conn, min_count=args.min_count))
        return 0
    except Exception as e:
        _print_json({"error": str(e)})
        return 1
    finally:
        conn.close()


def cmd_calibration(args) -> int:
    """Show calibration report (predicted vs actual accuracy)."""
    conn = get_connection()
    try:
        buckets = get_calibration_report(conn, timeframe=args.timeframe)
        signals = get_signal_performance(conn, min_count=args.min_signal_count)
        decay = get_signal_decay(conn, min_count=args.min_signal_count)
        recal_map = build_recalibration_map(conn, timeframe=args.timeframe)
        tr_ci = get_track_record_ci(conn, days=None, timeframe=args.timeframe)
        conf_perm = permutation_test_confidence(
            conn, days=None, timeframe=args.timeframe
        )
        _print_json(
            {
                "timeframe": args.timeframe or "ALL",
                "calibration": [
                    {
                        "range": b.confidence_range,
                        "predicted": b.predicted_confidence,
                        "actual": b.actual_accuracy,
                        "recalibrated": round(
                            apply_recalibration(b.predicted_confidence, recal_map), 3
                        ),
                        "count": b.count,
                    }
                    for b in buckets
                ],
                "recalibration_map": [
                    {"predicted": round(p, 3), "recalibrated": round(a, 3)}
                    for p, a in recal_map
                ],
                "signal_performance": [
                    {
                        "signal": s.signal,
                        "total": s.total,
                        "wins": s.wins,
                        "win_rate": s.win_rate,
                        "p_value": s.p_value,
                        "verdict": s.verdict,
                    }
                    for s in signals
                ],
                "signal_decay": [
                    {
                        "signal": s.signal,
                        "train": f"{s.train_wins}/{s.train_total}",
                        "test": f"{s.test_wins}/{s.test_total}",
                        "train_verdict": s.train_verdict,
                        "test_verdict": s.test_verdict,
                        "label": s.label,
                    }
                    for s in decay
                ],
                "robustness": {
                    "n": tr_ci.n,
                    "win_rate": tr_ci.win_rate,
                    "win_rate_ci95": tr_ci.win_rate_ci,
                    "brier_score": tr_ci.brier_score,
                    "brier_ci95": tr_ci.brier_ci,
                    "prob_better_than_coin": tr_ci.prob_better_than_coin,
                    "confidence_permutation": conf_perm,
                },
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


def cmd_portfolio_sync(args) -> int:
    """Sync portfolio from Toss Securities.

    Fetches current positions via the official Toss Open API (falling back to
    the tossctl CLI), compares with the local DB, and records synthetic
    transactions to reconcile differences.
    """
    try:
        toss_positions, source = fetch_positions(args.source)

        markets = [args.market.upper()] if args.market else ["KR", "US"]
        total_actions = []

        conn = pf_get_connection()
        try:
            for market in markets:
                pf = get_portfolio_for_market(conn, market)
                if pf is None:
                    # Auto-create portfolio
                    name = f"Toss {market}"
                    pf = create_portfolio(conn, market=market, name=name)

                local_positions = compute_positions(conn, pf.id)
                actions = reconcile(toss_positions, local_positions, market)

                if args.dry_run:
                    for a in actions:
                        a["market"] = market
                    total_actions.extend(actions)
                    continue

                today = datetime.now().strftime("%Y-%m-%d")
                for a in actions:
                    add_transaction(
                        conn,
                        portfolio_id=pf.id,
                        ticker=a["ticker"],
                        side=a["side"],
                        quantity=a["quantity"],
                        price=a["price"],
                        currency=a["currency"],
                        transacted_at=today,
                        note=a["note"],
                    )
                    a["market"] = market
                total_actions.extend(actions)

            if args.dry_run:
                _print_json(
                    {
                        "dry_run": True,
                        "source": source,
                        "actions": total_actions,
                        "action_count": len(total_actions),
                    }
                )
            else:
                _print_json(
                    {
                        "synced": True,
                        "source": source,
                        "actions": total_actions,
                        "action_count": len(total_actions),
                    }
                )
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

    # --- horizon-metrics-batch ---
    p = sub.add_parser(
        "horizon-metrics-batch",
        help="Compute horizon-metrics for multiple tickers in one call",
    )
    p.add_argument("tickers", help="Comma-separated tickers (e.g. NVDA,AMD,AVGO)")
    p.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    p.add_argument(
        "--days",
        type=int,
        default=400,
        help="Calendar days of history per ticker (default 400)",
    )
    p.set_defaults(func=cmd_horizon_metrics_batch)

    # --- screen-presurge ---
    p = sub.add_parser(
        "screen-presurge",
        help="Screen the universe for pre-surge setups (base/pullback/RS/pre-earnings)",
    )
    p.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    p.add_argument(
        "--top-n", type=int, default=20, help="Max candidates to return (default 20)"
    )
    p.add_argument(
        "--min-score",
        type=float,
        default=0.5,
        help="Minimum best-setup score to survive (0-1, default 0.5)",
    )
    p.add_argument(
        "--days",
        type=int,
        default=400,
        help="Calendar days of history per ticker (default 400)",
    )
    p.add_argument(
        "--with-earnings",
        action="store_true",
        help="Enable the US-only pre-earnings setup (best-effort FMP fetch)",
    )
    p.set_defaults(func=cmd_screen_presurge)

    # --- regime ---
    p = sub.add_parser(
        "regime",
        help="Classify market regime RISK_ON/NEUTRAL/RISK_OFF (hard BULL gate)",
    )
    p.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    p.add_argument(
        "--index",
        default=None,
        help="Index proxy ticker (default: worst-of SPY+QQQ for US, 069500 for KR)",
    )
    p.add_argument(
        "--days",
        type=int,
        default=400,
        help="Calendar days of index history to fetch (default 400)",
    )
    p.set_defaults(func=cmd_regime)

    # --- news ---
    p = sub.add_parser("news", help="Fetch recent news headlines for a ticker")
    p.add_argument("ticker", help="Stock ticker (US: NVDA, KR: 005930)")
    p.add_argument("--market", default="US", choices=["US", "KR", "us", "kr"])
    p.add_argument("--limit", type=_positive_int, default=10)
    p.add_argument(
        "--since-days",
        type=_positive_int,
        default=7,
        help="Only items within last N days (default 7)",
    )
    p.set_defaults(func=cmd_news)

    # --- disclosure (KR only) ---
    p = sub.add_parser(
        "disclosure",
        help="Fetch recent regulatory disclosures from Open DART (KR only)",
    )
    p.add_argument("ticker", help="6-digit KRX ticker code (e.g. 005930)")
    p.add_argument(
        "--since-days",
        type=_positive_int,
        default=7,
        help="Look-back window in days (default 7)",
    )
    p.add_argument("--limit", type=_positive_int, default=30)
    p.set_defaults(func=cmd_disclosure)

    # --- memory (Stage 7-A: mem0 semantic memory) ---
    memory = sub.add_parser(
        "memory",
        help="Semantic memory layer (mem0 + Qdrant). Requires `uv sync --extra memory`.",
    )
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)

    ms = memory_sub.add_parser("search", help="Semantic search within a category")
    ms.add_argument("query", help="Free-text query (will be embedded)")
    ms.add_argument(
        "--category",
        required=True,
        help="One of: predictions, news_events, themes, outcomes, transmission_chains",
    )
    ms.add_argument("--limit", type=int, default=5)
    ms.set_defaults(func=cmd_memory_search)

    ma = memory_sub.add_parser("add", help="Add a memory record")
    ma.add_argument("--category", required=True)
    ma.add_argument("--content", required=True, help="Text/JSON to embed")
    ma.add_argument(
        "--metadata-json",
        default="",
        help='JSON string of filterable metadata (e.g. {"ticker": "NVDA"})',
    )
    ma.set_defaults(func=cmd_memory_add)

    mt = memory_sub.add_parser("stats", help="Per-category memory counts")
    mt.set_defaults(func=cmd_memory_stats)

    mp = memory_sub.add_parser(
        "purge", help="Drop all memories in a category (destructive)"
    )
    mp.add_argument("--category", required=True)
    mp.add_argument("--yes", action="store_true", help="Confirm destructive operation")
    mp.set_defaults(func=cmd_memory_purge)

    # --- graph (Stage 7-B: Neo4j) ---
    graph = sub.add_parser(
        "graph",
        help=(
            "Graph layer (Neo4j Community). Requires `uv sync --extra graph` "
            "and `docker compose up -d neo4j`."
        ),
    )
    graph_sub = graph.add_subparsers(dest="graph_command", required=True)

    gi = graph_sub.add_parser("init", help="Create constraints + indexes (idempotent)")
    gi.set_defaults(func=cmd_graph_init)

    gq = graph_sub.add_parser("query", help="Run a raw Cypher statement")
    gq.add_argument("cypher", help="Cypher statement (use $param placeholders)")
    gq.add_argument(
        "--params-json",
        default="",
        help='JSON dict of parameters, e.g. {"ticker": "NVDA"}',
    )
    gq.set_defaults(func=cmd_graph_query)

    gs = graph_sub.add_parser(
        "similar-stocks", help="Find stocks sharing themes with the given ticker"
    )
    gs.add_argument("ticker")
    gs.add_argument("--market", default="US", choices=["US", "KR", "us", "kr"])
    gs.add_argument("--limit", type=int, default=10)
    gs.set_defaults(func=cmd_graph_similar_stocks)

    gw = graph_sub.add_parser(
        "theme-winners", help="Win rate per theme over the last N weeks"
    )
    gw.add_argument("--weeks", type=int, default=12)
    gw.add_argument("--limit", type=int, default=20)
    gw.set_defaults(func=cmd_graph_theme_winners)

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
    pc.add_argument(
        "--components",
        default=None,
        help=(
            "JSON object of per-pillar contributions, e.g. "
            '\'{"algo":7.0,"news":1.0,"llm_context":-1.5,'
            '"overextension":"ELEVATED","regime":"NEUTRAL"}\'. Stored for '
            "capability-contribution analysis and future blended confidence."
        ),
    )
    pc.add_argument(
        "--recalibrate",
        action="store_true",
        help=(
            "Map the raw --confidence through the isotonic recalibration curve "
            "(built from closed predictions of the same source) before storing, "
            "so logged confidence reflects observed accuracy. No-op until enough "
            "closed predictions exist."
        ),
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

    # --- component-contribution ---
    cc = sub.add_parser(
        "component-contribution",
        help="Win-rate split by each stored per-pillar component (algo/news/llm/gates)",
    )
    cc.add_argument(
        "--min-count",
        type=int,
        default=8,
        help="Minimum closed rows in a bucket to report it (default 8)",
    )
    cc.set_defaults(func=cmd_component_contribution)

    # --- lint-llm-context ---
    lc = sub.add_parser(
        "lint-llm-context",
        help="Lint a structured LLM_CONTEXT debate JSON for rigor (range/sign/evidence)",
    )
    lc.add_argument(
        "debate", help="JSON debate object {score, winner, bull_points, bear_points}"
    )
    lc.set_defaults(func=cmd_lint_llm_context)

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

    # portfolio sync
    psync = pf_sub.add_parser(
        "sync", help="Sync from Toss Securities (Open API, tossctl fallback)"
    )
    psync.add_argument(
        "--market",
        default=None,
        choices=["US", "KR", "us", "kr"],
        help="Sync one market only (default: both)",
    )
    psync.add_argument(
        "--source",
        default="auto",
        choices=["auto", "toss-api", "tossctl"],
        help="Data source: auto (Open API if configured, else tossctl), "
        "toss-api, or tossctl (default: auto)",
    )
    psync.add_argument("--dry-run", action="store_true", help="Preview without writing")
    psync.set_defaults(func=cmd_portfolio_sync)

    return parser


def main() -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
