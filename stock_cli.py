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
import os
import sys
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# Add provider and store paths
PROJECT_ROOT = Path(__file__).parent
# Project root itself so `import scheduler.*` (watchlist monitor/store) resolves
# — scheduler is a source package, not an installed one.
sys.path.insert(0, str(PROJECT_ROOT))
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
from indicators import compute_horizon_metrics, compute_atr
from regime import aggregate_regime, compute_regime, compute_realized_vol
from sector_rs import (
    US_BENCHMARK,
    US_SECTOR_CONSTITUENTS,
    US_SECTOR_ETFS,
    compute_sector_verdict,
    rank_sectors,
)
from events import (
    build_timeline,
    evaluate_gate,
    _fetch_earnings_window,
    _fetch_macro_window,
    _redact_key,
)

# Imported as a module (not from-imports) so tests can monkeypatch
# etf_kr.get_etf_universe / etf_kr.fetch_etf_detail at call time.
import etf_kr
from etf_score import score_candidates
from news_features import summarize_news
from macro_news import DEFAULT_MACRO_QUERY, assess_macro_risk, get_macro_news
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
from portfolio.isa_allocator import (
    allocate_contribution,
    check_rebalance,
    compute_drift,
    min_contribution_to_restore,
)
from portfolio.isa_store import (
    get_active_target,
    init_isa_tables,
    list_decisions,
    list_nav_snapshots,
    log_decision,
    save_nav_snapshot,
    save_target,
)
from portfolio.evaluator import (
    compute_report,
    compute_risk,
    compute_vs_predictions,
    compute_advice,
)
from portfolio.exit_manager import compute_exit_actions
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


# The KR market benchmark all sector proxies are measured against (KODEX 200).
KR_BENCHMARK = "069500"
KR_SECTOR_MAP_PATH = PROJECT_ROOT / "data" / "kr_sector_map.csv"


def _load_kr_sector_map():
    """Read data/kr_sector_map.csv into a list of sector specs.

    Returns:
        List of ``(sector, proxy_etf, constituents)`` tuples where
        ``constituents`` is a list of 6-digit codes. Returns ``[]`` on any read
        failure so ``cmd_sector_rs`` degrades to an empty result rather than
        raising (mirrors the never-raise idiom in candidate_discovery).
    """
    if not KR_SECTOR_MAP_PATH.exists():
        return []
    out = []
    try:
        import csv

        with KR_SECTOR_MAP_PATH.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sector = (row.get("sector") or "").strip()
                proxy = (row.get("proxy_etf") or "").strip()
                raw = (row.get("constituents") or "").strip()
                constituents = [c.strip() for c in raw.split(";") if c.strip()]
                if sector and proxy:
                    out.append((sector, proxy, constituents))
    except Exception:  # noqa: BLE001 — never block on CSV parse
        return []
    return out


def _sector_specs(market: str):
    """Return ``(benchmark, [(sector, etf, constituents)])`` for the market.

    US specs come from the static maps in ``sector_rs``; KR specs from
    ``data/kr_sector_map.csv``.
    """
    if market == "US":
        specs = [
            (sector, etf, US_SECTOR_CONSTITUENTS.get(sector, []))
            for sector, etf in US_SECTOR_ETFS.items()
        ]
        return US_BENCHMARK, specs
    return KR_BENCHMARK, _load_kr_sector_map()


def _metrics_and_closes(bars_by_ticker, ticker, market):
    """Build HorizonMetrics + closes for one ticker, or ``(None, None)``.

    Never raises: a ticker with no bars (provider miss / delisting) yields
    ``(None, None)`` so the sector still scores via its NEUTRAL floor.
    """
    bars = bars_by_ticker.get(ticker) or []
    if not bars:
        return None, None
    try:
        bar_dicts = [asdict(b) for b in bars]
        metrics = compute_horizon_metrics(bars=bar_dicts, ticker=ticker, market=market)
        closes = [b["close"] for b in bar_dicts if b.get("close") is not None]
        return metrics, closes
    except Exception:  # noqa: BLE001 — degrade this ticker, never the whole run
        return None, None


def _write_sector_rs_json(market: str, payload: dict) -> Path:
    """Atomically write ``data/sector_rs_{market}.json`` (per-market file).

    Writes to a sibling temp file then ``os.replace`` so a concurrent reader
    (discovery) never sees a half-written file, and uses a per-market filename
    so a US run never clobbers the KR snapshot (or vice versa).

    Args:
        market: "US" or "KR" (used lower-cased in the filename).
        payload: The JSON-serializable snapshot to persist.

    Returns:
        The path written.
    """
    import os
    import tempfile

    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / f"sector_rs_{market.lower()}.json"
    fd, tmp_name = tempfile.mkstemp(dir=str(data_dir), suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp_name, target)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    return target


def cmd_sector_rs(args) -> int:
    """Rank sectors by relative strength + breadth + lifecycle stage.

    For the market, fetches the benchmark, every sector proxy ETF, and every
    constituent in one batch call, builds a SectorVerdict per sector via
    ``sector_rs.compute_sector_verdict`` (FAVOR / ROTATING_IN / ROTATING_OUT /
    AVOID / NEUTRAL), and ranks by score. A missing benchmark floors every
    sector to NEUTRAL (the verdict module's contract). Read-only JSON output
    unless ``--write`` is set, which atomically persists the snapshot to the
    per-market ``data/sector_rs_{market}.json`` for discovery to consume.

    Args:
        args: Parsed CLI args with market, days, write.

    Returns:
        0 on success, 1 on error.
    """
    try:
        market = args.market.upper()
        benchmark, specs = _sector_specs(market)
        if not specs:
            _print_json({"error": f"no sector map for market {market}"})
            return 1

        # One batch fetch for the benchmark + every ETF + every constituent.
        wanted = {benchmark}
        for _sector, etf, constituents in specs:
            wanted.add(etf)
            wanted.update(constituents)
        provider = _get_provider(market)
        bars_by_ticker = provider.get_price_history_batch(
            sorted(wanted), days=args.days
        )

        bench_metrics, bench_closes = _metrics_and_closes(
            bars_by_ticker, benchmark, market
        )

        verdicts = []
        for sector, etf, constituents in specs:
            etf_metrics, etf_closes = _metrics_and_closes(bars_by_ticker, etf, market)
            cons_metrics = []
            for tk in constituents:
                m, _ = _metrics_and_closes(bars_by_ticker, tk, market)
                if m is not None:
                    cons_metrics.append(m)
            v = compute_sector_verdict(
                sector=sector,
                etf_metrics=etf_metrics,
                etf_closes=etf_closes,
                benchmark_metrics=bench_metrics,
                benchmark_closes=bench_closes,
                constituent_metrics=cons_metrics,
            )
            verdicts.append((v, etf, constituents))

        ranked = rank_sectors([v for v, _etf, _c in verdicts])
        cons_by_sector = {sector: c for sector, _etf, c in specs}
        etf_by_sector = {sector: etf for sector, etf, _c in specs}

        sectors_out = []
        for v in ranked:
            row = asdict(v)
            row["etf"] = etf_by_sector.get(v.sector, "")
            row["constituents"] = cons_by_sector.get(v.sector, [])
            sectors_out.append(row)

        payload = {
            "market": market,
            "benchmark": bench_metrics.ticker if bench_metrics else benchmark,
            "benchmark_available": bench_metrics is not None,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "sectors": sectors_out,
        }

        if getattr(args, "write", False):
            path = _write_sector_rs_json(market, payload)
            payload["written_to"] = str(path)

        _print_json(payload)
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


def cmd_macro_news(args) -> int:
    """Fetch global macro/geopolitical headlines from GDELT (free, no API key).

    Market-agnostic context (wars, oil/energy, central banks, tariffs) for the
    macro / LLM_CONTEXT layer — distinct from the per-ticker `news` command.
    Output includes a deterministic `risk` assessment (NORMAL / ELEVATED /
    RISK_OFF keyword tripwire + matched evidence) over the fetched headlines.

    Returns:
        0 on success (even if 0 items), 1 on unexpected error.
    """
    try:
        items, source = get_macro_news(
            query=args.query, timespan=args.timespan, limit=args.limit
        )
        now = datetime.now()
        _print_json(
            {
                "source": source,  # "rss" | "gdelt" | "gdelt-stale" (expired cache) | "none"
                "generated_at": now.isoformat(timespec="seconds"),
                "timespan": args.timespan,
                "query": args.query,
                "count": len(items),
                "risk": assess_macro_risk(items, stale=(source == "gdelt-stale")),
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


def _catalyst_tickers(raw: str) -> list[str]:
    """Split a comma-separated ticker argument, dropping blanks.

    Args:
        raw: e.g. "NVDA,AMD" or "005930".

    Returns:
        List of trimmed, non-empty ticker strings.
    """
    return [t.strip() for t in raw.split(",") if t.strip()]


def cmd_catalyst_timeline(args) -> int:
    """Emit the merged forward catalyst timeline for one or more tickers.

    Fetches the FMP earnings (US-listed only) + economic calendars once for the
    requested window, normalizes them into ``CatalystEvent`` rows, and groups
    them into per-ticker earnings + market-wide macro lists. Macro is omitted
    unless ``--include-macro`` is passed. FAIL-OPEN: a missing FMP key or fetch
    error yields empty lists plus ``gate_unavailable=true``, never an error
    exit. The two calendars fail independently — a macro outage (e.g. the FMP
    economic calendar needs a paid plan) only omits ``market_wide`` with a note.

    Args:
        args: Parsed CLI arguments with tickers, market, days, include_macro.

    Returns:
        0 always (read-only, fail-open). 1 only on an unexpected internal error.
    """
    try:
        market = args.market.upper()
        tickers = _catalyst_tickers(args.tickers)
        norm = [t.upper() if market == "US" else t for t in tickers]
        asof = datetime.now().date().isoformat()
        window_to = (datetime.now().date() + timedelta(days=args.days)).isoformat()

        out = {
            "asof": asof,
            "market": market,
            "days": args.days,
            "tickers": norm,
            "include_macro": args.include_macro,
            "gate_unavailable": False,
            "by_ticker": {},
            "market_wide": [],
        }

        if not os.environ.get("FMP_API_KEY", ""):
            out["gate_unavailable"] = True
            out["note"] = "FMP_API_KEY not set — timeline unavailable (fail-open)"
            _print_json(out)
            return 0

        # Fetch the two calendars independently so a macro outage cannot hide
        # a working earnings timeline — partial failures are noted, not silent.
        notes = []
        earnings_rows: list = []
        macro_rows: list = []
        earnings_failed = False
        macro_failed = False
        if market == "US":
            try:
                earnings_rows = _fetch_earnings_window(asof, window_to, market)
            except Exception as e:
                earnings_failed = True
                notes.append(
                    f"earnings calendar fetch failed: {_redact_key(str(e))} (fail-open)"
                )
        if args.include_macro:
            try:
                macro_rows = _fetch_macro_window(asof, window_to)
            except Exception as e:
                macro_failed = True
                notes.append(
                    f"macro calendar fetch failed: {_redact_key(str(e))} — "
                    "market_wide omitted (fail-open)"
                )
        # Match evaluate_gate semantics: unavailable only when EVERY requested
        # calendar failed. Partial success keeps the timeline live (with notes).
        requested_failures = []
        if market == "US":
            requested_failures.append(earnings_failed)
        if args.include_macro:
            requested_failures.append(macro_failed)
        if requested_failures and all(requested_failures):
            out["gate_unavailable"] = True
        if notes:
            out["note"] = "; ".join(notes)

        timeline = build_timeline(asof, earnings_rows, macro_rows, market)
        # Keep only the requested tickers in the per-ticker view.
        out["by_ticker"] = {
            t: [asdict(e) for e in timeline["by_ticker"].get(t, [])] for t in norm
        }
        if args.include_macro:
            out["market_wide"] = [asdict(e) for e in timeline["market_wide"]]
        _print_json(out)
        return 0
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_catalyst_gate(args) -> int:
    """Emit the deterministic R3 event-risk gate for one or more tickers.

    Per-ticker earnings cap/trim (US only) + market-wide macro trim (US + KR).
    KR returns macro_trim only — no per-ticker earnings cap, since FMP has no
    forward KR EPS feed. FAIL-OPEN, visibly: a failed FMP earnings fetch falls
    back to keyless yfinance earnings dates; ``gate_unavailable=true`` only
    when no source produced data. Partial outages are flagged via
    ``earnings_source`` / ``macro_available`` + ``notes``.

    Args:
        args: Parsed CLI arguments with tickers, market.

    Returns:
        0 always (read-only, fail-open). 1 only on an unexpected internal error.
    """
    try:
        market = args.market.upper()
        tickers = _catalyst_tickers(args.tickers)
        asof = datetime.now().date().isoformat()
        gate = evaluate_gate(asof, tickers, market)
        _print_json(asdict(gate))
        return 0
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_etf_list(args) -> int:
    """List the KR ETF universe, filtered and sorted for ISA screening.

    Serves live Naver data with the stale-CSV fallback of
    ``etf_kr.get_etf_universe`` (source + notes are surfaced in the JSON).
    Leverage/inverse ETFs are EXCLUDED unless ``--include-leverage`` — they are
    unsuitable for the long-term ISA book this layer feeds.

    Args:
        args: Parsed CLI arguments with asset_class, min_aum (억원),
            include_leverage, limit.

    Returns:
        0 on success (including cache-stale). 1 when no universe source
        (live or cache) is available.
    """
    try:
        rows, source, notes = etf_kr.get_etf_universe()
    except etf_kr.EtfDataUnavailable as e:
        _print_json({"error": str(e)})
        return 1
    if not args.include_leverage:
        rows = [r for r in rows if not r.leveraged_or_inverse]
    if args.asset_class:
        rows = [r for r in rows if r.asset_class == args.asset_class]
    if args.min_aum is not None:
        rows = [r for r in rows if r.aum_100m_krw >= args.min_aum]
    rows = sorted(rows, key=lambda r: r.aum_100m_krw, reverse=True)
    if args.limit is not None:
        rows = rows[: args.limit]
    _print_json(
        {
            "asof": datetime.now().date().isoformat(),
            "source": source,
            "count": len(rows),
            "notes": notes,
            "etfs": [asdict(r) for r in rows],
        }
    )
    return 0


def _normalize_etf_code(raw: str) -> str:
    """Normalize user-supplied ETF code input.

    Args:
        raw: e.g. "69500" or "193t0".

    Returns:
        Zero-padded, uppercased 6-char code — "69500" → "069500", "193t0" →
        "0193T0" (KRX codes are uppercase).
    """
    return raw.strip().zfill(6).upper()


def cmd_etf_info(args) -> int:
    """Show one KR ETF: universe row merged with per-ETF detail.

    The detail fetch (펀드보수/기초지수) is fail-open enrichment — its notes
    are combined with the universe-source notes rather than failing the call.

    Args:
        args: Parsed CLI arguments with code.

    Returns:
        0 on success. 1 when the universe is unavailable or the code is not a
        listed KR ETF.
    """
    code = _normalize_etf_code(args.code)
    try:
        rows, source, notes = etf_kr.get_etf_universe()
    except etf_kr.EtfDataUnavailable as e:
        _print_json({"error": str(e)})
        return 1
    match = next((r for r in rows if r.code == code), None)
    if match is None:
        _print_json({"error": f"unknown KR ETF code: {code}"})
        return 1
    detail = etf_kr.fetch_etf_detail(code)
    out = asdict(match)
    out["fund_pay_pct"] = detail["fund_pay_pct"]
    out["base_index"] = detail["base_index"]
    out["asof"] = datetime.now().date().isoformat()
    out["source"] = source
    out["notes"] = notes + detail["notes"]
    _print_json(out)
    return 0


# --query prefilter cap: bounds per-candidate detail fetches (fail-open but
# each one is a live HTTP call).
MAX_COMPARE_CANDIDATES = 15


def cmd_etf_compare(args) -> int:
    """Compare ETFs tracking the same index and pick the best ticker.

    Candidates come from exactly one of an explicit comma code list or a
    name-substring ``--query`` prefilter (matched case- and space-insensitively,
    leverage/inverse excluded unless ``--include-leverage``, AUM-desc, capped
    at MAX_COMPARE_CANDIDATES with a visible note). Per-candidate detail is
    fail-open; scoring is set-relative via ``score_candidates``. Differing
    base indexes are flagged (``base_index_mismatch``) but still scored — the
    user may be comparing across indexes deliberately.

    Args:
        args: Parsed CLI arguments with codes, query, include_leverage.

    Returns:
        0 on success. 1 on selector misuse, unknown codes, no candidates, or
        universe unavailability.
    """
    if bool(args.codes) == bool(args.query):
        _print_json({"error": "exactly one of CODES or --query is required"})
        return 1
    try:
        rows, source, notes = etf_kr.get_etf_universe()
    except etf_kr.EtfDataUnavailable as e:
        _print_json({"error": str(e)})
        return 1

    if args.codes:
        wanted = [_normalize_etf_code(c) for c in args.codes.split(",") if c.strip()]
        # Dedup (first-seen order) so 360750,360750 doesn't score against itself.
        wanted = list(dict.fromkeys(wanted))
        if not wanted:
            _print_json({"error": "no valid ETF codes given (empty code list)"})
            return 1
        by_code = {r.code: r for r in rows}
        unknown = [c for c in wanted if c not in by_code]
        if unknown:
            _print_json({"error": f"unknown KR ETF code(s): {', '.join(unknown)}"})
            return 1
        candidates = [by_code[c] for c in wanted]
    else:
        q = args.query.lower().replace(" ", "")
        if not q:
            # A blank query would substring-match every ETF name and silently
            # compare the top 15 by AUM.
            _print_json({"error": "empty query text"})
            return 1
        candidates = [r for r in rows if q in r.name.lower().replace(" ", "")]
        if not args.include_leverage:
            candidates = [r for r in candidates if not r.leveraged_or_inverse]
        candidates.sort(key=lambda r: r.aum_100m_krw, reverse=True)
        if len(candidates) > MAX_COMPARE_CANDIDATES:
            notes = notes + [
                f"query matched {len(candidates)} ETFs; comparing top "
                f"{MAX_COMPARE_CANDIDATES} by AUM"
            ]
            candidates = candidates[:MAX_COMPARE_CANDIDATES]
    if not candidates:
        _print_json({"error": f"no ETFs matched query: {args.query!r}"})
        return 1

    details = {}
    for c in candidates:
        # fetch_etf_detail is fail-open by contract — never raises (stage 26);
        # a failed detail yields Nones + note, so no try/except is needed here.
        d = etf_kr.fetch_etf_detail(c.code)
        details[c.code] = d
        notes = notes + d["notes"]

    # Base-index consistency: flag (not fail) when candidates track different
    # indexes.
    groups: dict[str, list[str]] = {}
    for c in candidates:
        idx = details[c.code].get("base_index")
        if idx is not None:
            groups.setdefault(idx, []).append(c.code)
    mismatch = len(groups) > 1
    if mismatch:
        listing = "; ".join(f"{idx}: {codes}" for idx, codes in sorted(groups.items()))
        notes = notes + [f"base_index mismatch — {listing}"]

    result = score_candidates(candidates, details)
    _print_json(
        {
            "asof": datetime.now().date().isoformat(),
            "source": source,
            "count": len(candidates),
            "base_index_mismatch": mismatch,
            "best": result["best"],
            "scored": result["scored"],
            "notes": notes + result["notes"],
        }
    )
    return 0


# ---------------------------------------------------------------------------
# ISA commands (Stage 28: targets, drift-DCA allocator, decision log)
# ---------------------------------------------------------------------------


def _parse_kv_list(raw: str) -> dict[str, str]:
    """Parse a comma-separated k=v list ("a=1,b=2") into a dict.

    Args:
        raw: e.g. "overseas_equity=50,bond=50". Blank parts are skipped.

    Returns:
        Ordered dict of stripped keys → stripped string values.

    Raises:
        ValueError: on a part without "=".
    """
    out: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"expected key=value, got {part!r}")
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _find_isa_portfolio(conn):
    """Return the KR portfolio named "ISA", or None if it doesn't exist."""
    for pf in list_portfolios(conn):
        if pf.market == "KR" and pf.name == "ISA":
            return pf
    return None


def _isa_class_values(conn, pf, etf_map: dict[str, str]):
    """Value the ISA book per asset class at current prices.

    Reuses the existing portfolio valuation path (compute_positions + the KR
    provider's get_current_price) — no duplicated pricing logic.

    Args:
        conn: portfolio.db connection.
        pf: the ISA Portfolio.
        etf_map: asset class → ETF code from the active target.

    Returns:
        (value_by_class, notes) — positions whose ticker is not in the
        etf_map are EXCLUDED from class values with a visible warning; a
        missing live price falls back to avg cost with a note.
    """
    notes: list[str] = []
    code_to_class = {code: cls for cls, code in etf_map.items()}
    provider = _get_provider("KR")
    value_by_class: dict[str, float] = {}
    unmapped: list[str] = []
    for pos in compute_positions(conn, pf.id):
        # Positions may be recorded unpadded/lowercase ("69500", "193t0") —
        # normalize before the map lookup or they'd be silently excluded.
        code = _normalize_etf_code(pos.ticker)
        cls = code_to_class.get(code)
        if cls is None:
            unmapped.append(pos.ticker)
            continue
        price = provider.get_current_price(code)
        if price is None:
            price = pos.avg_price
            notes.append(f"price unavailable for {code} — using avg cost")
        value_by_class[cls] = value_by_class.get(cls, 0.0) + pos.quantity * price
    if unmapped:
        notes.append(
            "positions not in etf_map excluded from class values: "
            + ", ".join(sorted(unmapped))
        )
    return value_by_class, notes


def cmd_isa_init(args) -> int:
    """Store an approved ISA target allocation (+ class→ETF map).

    Validates weights (sum 100) and map coverage via ``save_target``, and each
    mapped code against the live ETF universe (unknown or leveraged/inverse
    codes are rejected; an unavailable universe skips validation with a
    visible note — fail open). The stored target becomes active immediately
    and a ``target_change`` decision is logged.

    Args:
        args: Parsed CLI arguments with allocation, map, note.

    Returns:
        0 on success, 1 on validation failure.
    """
    try:
        allocation = {
            cls: float(w) for cls, w in _parse_kv_list(args.allocation).items()
        }
        etf_map = {
            cls: _normalize_etf_code(code)
            for cls, code in _parse_kv_list(args.map).items()
        }
    except ValueError as e:
        _print_json({"error": str(e)})
        return 1

    notes: list[str] = []
    try:
        rows, _source, _notes = etf_kr.get_etf_universe()
        by_code = {r.code: r for r in rows}
        for cls, code in etf_map.items():
            row = by_code.get(code)
            if row is None:
                _print_json({"error": f"unknown KR ETF code for {cls}: {code}"})
                return 1
            if row.leveraged_or_inverse:
                _print_json(
                    {"error": f"leveraged/inverse ETF not allowed in ISA: {code}"}
                )
                return 1
    except etf_kr.EtfDataUnavailable:
        notes.append("universe unavailable — code validation skipped")

    conn = pf_get_connection()
    try:
        init_isa_tables(conn)
        try:
            target_id = save_target(conn, allocation, etf_map, args.note)
        except ValueError as e:
            _print_json({"error": str(e)})
            return 1
        _print_json(
            {
                "target_id": target_id,
                "allocation": allocation,
                "etf_map": etf_map,
                "note": args.note,
                "notes": notes,
            }
        )
        return 0
    finally:
        conn.close()


def _isa_context(conn):
    """Load (target, portfolio) or print the blocking error and return None.

    Returns:
        (target dict, Portfolio) on success; None after printing an error
        JSON (missing target or missing ISA portfolio).
    """
    init_isa_tables(conn)
    target = get_active_target(conn)
    if target is None:
        _print_json({"error": "no ISA target — run isa init"})
        return None
    pf = _find_isa_portfolio(conn)
    if pf is None:
        _print_json(
            {
                "error": "no ISA portfolio — run: "
                "portfolio create --market KR --name ISA, then record ISA "
                "trades with --portfolio ISA (plain --market KR goes to the "
                "first KR portfolio, e.g. Toss KR)"
            }
        )
        return None
    return target, pf


def _isa_cum_contributions(conn, pf_id: str) -> int:
    """Cumulative net contributions to the ISA book, in integer KRW.

    Sum of BUY transaction cost minus SELL proceeds over the portfolio's
    entire history (simple net-cost basis; a money-weighted return refinement
    is future work).

    Args:
        conn: portfolio.db connection.
        pf_id: portfolio id.

    Returns:
        Net contributed KRW (can be negative after large sells).
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN side = 'BUY' THEN quantity * price "
        "ELSE -quantity * price END), 0) AS cum "
        "FROM transactions WHERE portfolio_id = ?",
        (pf_id,),
    ).fetchone()
    return int(round(row["cum"]))


# Benchmark index tickers for the NAV track record. Both are fetched via the
# US provider's yfinance path — verified 2026-07-10: yfinance serves ^GSPC and
# ^KS11 (KOSPI) directly, while the KR provider mangles index symbols
# (its ticker normalization produces "0^KS11", which 404s on every source).
ISA_BENCHMARKS = {"sp500": "^GSPC", "kospi": "^KS11"}


def cmd_isa_status(args) -> int:
    """Active target + current class weights, drift, band check, track record.

    Args:
        args: (no arguments).

    Returns:
        0 on success, 1 when the target or the ISA portfolio is missing.
    """
    try:
        conn = pf_get_connection()
        try:
            ctx = _isa_context(conn)
            if ctx is None:
                return 1
            target, pf = ctx
            value_by_class, notes = _isa_class_values(conn, pf, target["etf_map"])
            total = sum(value_by_class.values())
            weights = {
                cls: round(v / total * 100.0, 3) if total > 0 else 0.0
                for cls, v in value_by_class.items()
            }
            drift = compute_drift(value_by_class, target["allocation"])
            rb = check_rebalance(value_by_class, target["allocation"])
            contributions = _isa_cum_contributions(conn, pf.id)
            # Simple since-inception return on net contributions (not
            # money-weighted — refinement is future work).
            return_pct = (
                round((total - contributions) / contributions * 100.0, 2)
                if contributions > 0
                else None
            )
            _print_json(
                {
                    "asof": datetime.now().date().isoformat(),
                    "target": target,
                    "value_by_class": value_by_class,
                    "total_value_krw": total,
                    "weights_pct": weights,
                    "drift_pp": {cls: round(d, 3) for cls, d in drift.items()},
                    "rebalance": rb,
                    "contributions_cum_krw": contributions,
                    "since_inception_return_pct": return_pct,
                    "recent_snapshots": list_nav_snapshots(conn, limit=3),
                    "notes": notes + rb["notes"],
                }
            )
            return 0
        finally:
            conn.close()
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_isa_snapshot(args) -> int:
    """Record a NAV snapshot: book value, cumulative contributions, benchmarks.

    Pure Python (no LLM). Benchmark closes (S&P 500 ^GSPC, KOSPI ^KS11) come
    from the US provider's yfinance path; a failed fetch stores null for that
    index with a visible note (fail-open).

    Args:
        args: (no arguments).

    Returns:
        0 on success, 1 when the target or the ISA portfolio is missing.
    """
    try:
        conn = pf_get_connection()
        try:
            ctx = _isa_context(conn)
            if ctx is None:
                return 1
            target, pf = ctx
            value_by_class, notes = _isa_class_values(conn, pf, target["etf_map"])
            nav = int(round(sum(value_by_class.values())))
            contributions = _isa_cum_contributions(conn, pf.id)
            us = _get_provider("US")
            benchmarks = {}
            for name, ticker in ISA_BENCHMARKS.items():
                close = us.get_current_price(ticker)
                benchmarks[name] = close
                if close is None:
                    notes.append(f"benchmark fetch failed: {name} ({ticker})")
            snapshot_id = save_nav_snapshot(conn, nav, contributions, benchmarks, notes)
            _print_json(
                {
                    "id": snapshot_id,
                    "snapped_at": datetime.now().isoformat(),
                    "nav_krw": nav,
                    "contributions_cum_krw": contributions,
                    "benchmarks": benchmarks,
                    "notes": notes,
                }
            )
            return 0
        finally:
            conn.close()
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_isa_allocate(args) -> int:
    """Allocate a monthly contribution across the ISA book (never sells).

    Runs the deterministic drift-DCA allocator (tilts clamped in code), maps
    class buys to ETFs via the active target's etf_map with estimated share
    counts, and logs a ``contribution`` decision unless ``--dry-run``.

    Args:
        args: Parsed CLI arguments with amount, tilt, dry_run.

    Returns:
        0 on success, 1 on missing target/portfolio or bad tilt syntax.
    """
    try:
        tilt = None
        if args.tilt:
            try:
                tilt = {cls: float(v) for cls, v in _parse_kv_list(args.tilt).items()}
            except ValueError as e:
                _print_json({"error": str(e)})
                return 1
        conn = pf_get_connection()
        try:
            ctx = _isa_context(conn)
            if ctx is None:
                return 1
            target, pf = ctx
            value_by_class, notes = _isa_class_values(conn, pf, target["etf_map"])
            result = allocate_contribution(
                args.amount, value_by_class, target["allocation"], tilt_pp=tilt
            )
            notes = notes + result["notes"]

            provider = _get_provider("KR")
            per_etf = []
            for cls, buy in result["buys_by_class"].items():
                code = target["etf_map"][cls]
                price = provider.get_current_price(code)
                if price is None:
                    notes.append(f"price unavailable for {code} — shares not estimated")
                    shares = None
                else:
                    shares = int(buy // price)
                per_etf.append(
                    {
                        "asset_class": cls,
                        "code": code,
                        "buy_krw": buy,
                        "est_price": price,
                        "est_shares": shares,
                    }
                )

            decision_id = None
            if not args.dry_run:
                decision_id = log_decision(
                    conn,
                    kind="contribution",
                    amount_krw=args.amount,
                    inputs={
                        "current_value_by_class": value_by_class,
                        "targets": target["allocation"],
                        "tilt": tilt,
                    },
                    proposal={"tilt": tilt} if tilt else None,
                    final={
                        "buys_by_class": result["buys_by_class"],
                        "per_etf": per_etf,
                    },
                    notes=notes,
                )
            _print_json(
                {
                    "asof": datetime.now().date().isoformat(),
                    "amount_krw": args.amount,
                    "buys_by_class": result["buys_by_class"],
                    "per_etf": per_etf,
                    "effective_targets": result["effective_targets"],
                    "notes": notes,
                    "dry_run": bool(args.dry_run),
                    "decision_id": decision_id,
                }
            )
            return 0
        finally:
            conn.close()
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_isa_rebalance(args) -> int:
    """Band check + contribution-only remedy (sell-minimizing by design).

    Reports band breaches and the minimum extra contribution that restores
    every class to within the band via the standard allocator — no sells are
    ever suggested (ISA 비과세/의무기간 rationale). Logs a ``rebalance``
    decision.

    Args:
        args: (no arguments).

    Returns:
        0 on success, 1 when the target or the ISA portfolio is missing.
    """
    try:
        conn = pf_get_connection()
        try:
            ctx = _isa_context(conn)
            if ctx is None:
                return 1
            target, pf = ctx
            value_by_class, notes = _isa_class_values(conn, pf, target["etf_map"])
            rb = check_rebalance(value_by_class, target["allocation"])
            notes = notes + rb["notes"]
            remedy = min_contribution_to_restore(value_by_class, target["allocation"])
            if remedy is None:
                notes.append(
                    "no finite contribution restores the band — review targets"
                )
            decision_id = log_decision(
                conn,
                kind="rebalance",
                amount_krw=None,
                inputs={
                    "current_value_by_class": value_by_class,
                    "targets": target["allocation"],
                },
                proposal=None,
                final={
                    "needed": rb["needed"],
                    "breaches": rb["breaches"],
                    "min_contribution_to_restore": remedy,
                },
                notes=notes,
            )
            _print_json(
                {
                    "asof": datetime.now().date().isoformat(),
                    "needed": rb["needed"],
                    "breaches": rb["breaches"],
                    "min_contribution_to_restore": remedy,
                    "notes": notes,
                    "decision_id": decision_id,
                }
            )
            return 0
        finally:
            conn.close()
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_isa_log(args) -> int:
    """Show the ISA decision history (newest first).

    Args:
        args: Parsed CLI arguments with limit.

    Returns:
        0 always (read-only).
    """
    conn = pf_get_connection()
    try:
        init_isa_tables(conn)
        _print_json({"decisions": list_decisions(conn, limit=args.limit)})
        return 0
    finally:
        conn.close()


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


def _resolve_portfolio(conn, market: str, name: str | None):
    """Resolve the target portfolio for a trade/import.

    Args:
        conn: portfolio.db connection.
        market: "US" or "KR" (case-insensitive).
        name: optional portfolio name. When given, routes to the portfolio
            with that exact name in the market (e.g. --portfolio ISA keeps ISA
            trades out of "Toss KR"). When None, keeps the historical behavior
            of the first portfolio for the market.

    Returns:
        (Portfolio, None) on success, (None, error message) on failure — the
        error lists the available names in the market when a name is unknown.
    """
    market = market.upper()
    if name is None:
        pf = get_portfolio_for_market(conn, market)
        if pf is None:
            return (
                None,
                f"No portfolio for market {market}. Run 'portfolio create' first.",
            )
        return pf, None
    candidates = [p for p in list_portfolios(conn) if p.market == market]
    for pf in candidates:
        if pf.name == name:
            return pf, None
    available = ", ".join(p.name for p in candidates) or "(none)"
    return None, (
        f"No portfolio named {name!r} for market {market} "
        f"(available: {available}). Run 'portfolio create' first."
    )


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
            pf, err = _resolve_portfolio(conn, args.market, args.portfolio)
            if pf is None:
                _print_json({"error": err})
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
            pf, err = _resolve_portfolio(conn, args.market, args.portfolio)
            if pf is None:
                _print_json({"error": err})
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


def cmd_portfolio_exit_check(args) -> int:
    """Advisory exit/trim/add/watch/hold check per holding.

    Mirrors :func:`cmd_portfolio_advice` but layers ATR chandelier trailing
    stops, R:R take-profit, and linked-prediction thesis invalidation via
    :func:`portfolio.exit_manager.compute_exit_actions`. Provider failures
    degrade gracefully (the affected ticker downgrades to WATCH).
    """
    try:
        conn = pf_get_connection()
        try:
            pf = get_portfolio_for_market(conn, args.market)
            if pf is None:
                _print_json({"error": f"No portfolio for market {args.market}"})
                return 1
            positions = compute_positions(conn, pf.id)
            provider = _get_provider(args.market)

            current_prices: dict = {}
            metrics_by_ticker: dict = {}
            atr_by_ticker: dict = {}
            fetch_errors: dict = {}
            for pos in positions:
                # Each ticker's fetch/compute is isolated: one provider failure
                # must degrade that holding to WATCH, not abort the command.
                try:
                    bars = provider.get_price_history(pos.ticker, days=300)
                    if not bars:
                        continue
                    current_prices[pos.ticker] = bars[-1].close
                    bar_dicts = [asdict(b) for b in bars]
                    metrics = compute_horizon_metrics(
                        bars=bar_dicts, ticker=pos.ticker, market=args.market.upper()
                    )
                    # ~22-bar swing high for the chandelier high-watermark (fixed
                    # lookback — see references/exit_rules.md for the caveat).
                    swing_high = max(b.high for b in bars[-22:])
                    metrics_by_ticker[pos.ticker] = {
                        "current_price": metrics.current_price,
                        "ma20": metrics.ma20,
                        "ma50": metrics.ma50,
                        "ma200": metrics.ma200,
                        "rsi14": metrics.rsi14,
                        "overextension_level": metrics.overextension_level,
                        "swing_high_22": swing_high,
                    }
                    atr = compute_atr(bar_dicts, period=14)
                    if atr is not None:
                        atr_by_ticker[pos.ticker] = atr
                except Exception as e:  # degrade this holding to WATCH
                    fetch_errors[pos.ticker] = str(e)
                    current_prices.pop(pos.ticker, None)
                    metrics_by_ticker.pop(pos.ticker, None)
                    atr_by_ticker.pop(pos.ticker, None)

            # Linked predictions, keyed by ticker. The exit_manager links to the
            # latest OPEN prediction for stop/target/R:R and the latest-overall
            # for the MISS thesis-invalidation check, so we query both
            # separately to avoid a mixed-status limit window dropping the
            # relevant OPEN thesis.
            open_predictions_by_ticker: dict = {}
            pred_conn = get_connection()
            try:
                for pos in positions:
                    latest_open = db_list_predictions(
                        pred_conn,
                        status="OPEN",
                        market=args.market.upper(),
                        ticker=pos.ticker,
                        limit=1,
                    )
                    latest_overall = db_list_predictions(
                        pred_conn,
                        market=args.market.upper(),
                        ticker=pos.ticker,
                        limit=1,
                    )
                    combined = {p.id: p for p in (*latest_open, *latest_overall)}
                    open_predictions_by_ticker[pos.ticker] = [
                        asdict(p) for p in combined.values()
                    ]
            finally:
                pred_conn.close()

            result = compute_exit_actions(
                positions,
                current_prices=current_prices,
                metrics_by_ticker=metrics_by_ticker,
                atr_by_ticker=atr_by_ticker,
                open_predictions_by_ticker=open_predictions_by_ticker,
                atr_mult=args.atr_mult,
                tp_rr=args.tp_rr,
            )
            # Annotate holdings whose data fetch failed so the WATCH downgrade
            # carries the underlying error rather than a bare "no price" note.
            if fetch_errors:
                for action in result.get("actions", []):
                    err = fetch_errors.get(action.get("ticker"))
                    if err:
                        action.setdefault("triggered_rules", []).append(
                            f"데이터 조회 실패 (data fetch error): {err}"
                        )
            _print_json({"portfolio": pf.name, "market": pf.market, **result})
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
# Watchlist commands (delayed / EOD-ish trigger alerts)
# ---------------------------------------------------------------------------


def cmd_watch_add(args) -> int:
    """Add a saved watchlist row with explicit trigger levels."""
    try:
        from scheduler.watchlist_store import get_connection as wl_get_connection
        from scheduler.watchlist_store import add_watch

        conn = wl_get_connection()
        try:
            watch_id = add_watch(
                conn,
                ticker=args.ticker,
                market=args.market,
                direction=args.direction,
                entry_low=args.entry_low,
                entry_high=args.entry_high,
                stop=args.stop,
                target=args.target,
                reentry=args.reentry,
                note=args.note,
            )
            _print_json({"added": True, "id": watch_id})
            return 0
        finally:
            conn.close()
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_watch_remove(args) -> int:
    """Remove a saved watchlist row by id."""
    try:
        from scheduler.watchlist_store import get_connection as wl_get_connection
        from scheduler.watchlist_store import remove_watch

        conn = wl_get_connection()
        try:
            removed = remove_watch(conn, args.id)
            _print_json({"removed": removed, "id": args.id})
            return 0 if removed else 1
        finally:
            conn.close()
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_watch_list(args) -> int:
    """List the unified watchlist (saved + open predictions + positions)."""
    try:
        from dataclasses import asdict as _asdict
        from scheduler.watchlist_store import load_unified_watchlist

        targets = load_unified_watchlist(market=args.market)
        _print_json({"watchlist": [_asdict(t) for t in targets]})
        return 0
    except Exception as e:
        _print_json({"error": str(e)})
        return 1


def cmd_watch_check(args) -> int:
    """Run one monitor pass and print the JSON of fired triggers."""
    try:
        from scheduler.watchlist_monitor import run_monitor

        summary = run_monitor(
            market=args.market, force=args.force, dry_run=args.dry_run
        )
        _print_json(summary)
        return 0
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

    # --- sector-rs ---
    p = sub.add_parser(
        "sector-rs",
        help="Rank sectors by relative strength + breadth + lifecycle stage",
    )
    p.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    p.add_argument(
        "--days",
        type=int,
        default=400,
        help="Calendar days of price history to fetch (default 400)",
    )
    p.add_argument(
        "--write",
        action="store_true",
        help="Atomically persist data/sector_rs_{market}.json for discovery",
    )
    p.set_defaults(func=cmd_sector_rs)

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

    p = sub.add_parser(
        "macro-news",
        help="Global macro/geopolitical headlines via GDELT (free, no API key)",
    )
    p.add_argument(
        "--timespan", default="24h", help="Look-back window, e.g. 24h, 1h, 3d"
    )
    p.add_argument("--limit", type=_positive_int, default=20)
    p.add_argument(
        "--query",
        default=DEFAULT_MACRO_QUERY,
        help="GDELT Boolean query (GDELT fallback only; RSS primary is a fixed feed set)",
    )
    p.set_defaults(func=cmd_macro_news)

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

    # --- catalyst (forward event timeline + R3 event-risk gate) ---
    catalyst = sub.add_parser(
        "catalyst",
        help="Forward catalyst timeline + R3 event-risk gate (earnings + macro)",
    )
    catalyst_sub = catalyst.add_subparsers(dest="catalyst_command", required=True)

    ct = catalyst_sub.add_parser(
        "timeline",
        help="Merged forward earnings (US) + macro timeline for tickers",
    )
    ct.add_argument("tickers", help="Comma-separated tickers (e.g. NVDA,AMD or 005930)")
    ct.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    ct.add_argument(
        "--days",
        type=_positive_int,
        default=14,
        help="Forward calendar-day window to scan (default 14)",
    )
    ct.add_argument(
        "--include-macro",
        action="store_true",
        help="Include the market-wide macro (FOMC/CPI/NFP) timeline",
    )
    ct.set_defaults(func=cmd_catalyst_timeline)

    cg = catalyst_sub.add_parser(
        "gate",
        help="Deterministic R3 gate: per-ticker earnings cap/trim + macro_trim",
    )
    cg.add_argument("tickers", help="Comma-separated tickers (e.g. NVDA,AMD or 005930)")
    cg.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    cg.set_defaults(func=cmd_catalyst_gate)

    # --- etf (KR ETF universe + metadata, stage 26) ---
    etf = sub.add_parser(
        "etf",
        help="KR-listed ETF universe + metadata (Naver source, ISA layer)",
    )
    etf_sub = etf.add_subparsers(dest="etf_command", required=True)

    el = etf_sub.add_parser(
        "list",
        help="List KR ETFs sorted by AUM (leverage/inverse excluded by default)",
    )
    el.add_argument(
        "--asset-class",
        choices=sorted(set(etf_kr.TAB_ASSET_CLASS.values())),
        help="Keep only one asset class (e.g. domestic_equity, overseas_equity)",
    )
    el.add_argument(
        "--min-aum",
        type=_positive_int,
        help="Minimum AUM in 억원 (marketSum)",
    )
    el.add_argument(
        "--include-leverage",
        action="store_true",
        help="Include leverage/inverse ETFs (excluded by default)",
    )
    el.add_argument("--limit", type=_positive_int, help="Max rows to return")
    el.set_defaults(func=cmd_etf_list)

    ei = etf_sub.add_parser(
        "info",
        help="One KR ETF: universe row + 펀드보수/기초지수 detail",
    )
    ei.add_argument("code", help="6-digit KR ETF code (e.g. 360750)")
    ei.set_defaults(func=cmd_etf_info)

    ec = etf_sub.add_parser(
        "compare",
        help="Pick the best ticker among ETFs tracking the same index",
    )
    ec.add_argument(
        "codes",
        nargs="?",
        default=None,
        help="Comma-separated ETF codes (e.g. 360750,379800) — or use --query",
    )
    ec.add_argument(
        "--query",
        help="Name substring to prefilter the universe (space-insensitive, "
        f"AUM-desc, capped at {MAX_COMPARE_CANDIDATES})",
    )
    ec.add_argument(
        "--include-leverage",
        action="store_true",
        help="Include leverage/inverse ETFs in --query matches",
    )
    ec.set_defaults(func=cmd_etf_compare)

    # --- isa (Stage 28: targets, drift-DCA allocator, decision log) ---
    isa = sub.add_parser(
        "isa",
        help="ISA book: target allocation, contribution allocator, decision log",
    )
    isa_sub = isa.add_subparsers(dest="isa_command", required=True)

    ii = isa_sub.add_parser(
        "init", help="Store an approved target allocation + class→ETF map"
    )
    ii.add_argument(
        "--allocation",
        required=True,
        help='Comma k=v weights summing to 100 (e.g. "overseas_equity=50,bond=30,gold=20")',
    )
    ii.add_argument(
        "--map",
        required=True,
        help='Comma class=ETF-code map (e.g. "overseas_equity=360750,bond=114260,gold=411060")',
    )
    ii.add_argument("--note", help="Approval note")
    ii.set_defaults(func=cmd_isa_init)

    ist = isa_sub.add_parser(
        "status", help="Current class weights, drift vs target, band check"
    )
    ist.set_defaults(func=cmd_isa_status)

    ia = isa_sub.add_parser(
        "allocate", help="Allocate a contribution (drift-DCA, never sells)"
    )
    ia.add_argument(
        "--amount", type=_positive_int, required=True, help="Contribution in KRW"
    )
    ia.add_argument(
        "--tilt",
        help='Proposed %%p tilts, clamped to ±10 (e.g. "overseas_equity=+5,bond=-5")',
    )
    ia.add_argument(
        "--dry-run", action="store_true", help="Preview without logging a decision"
    )
    ia.set_defaults(func=cmd_isa_allocate)

    ir = isa_sub.add_parser(
        "rebalance",
        help="Band breaches + minimum contribution-only remedy (no sells)",
    )
    ir.set_defaults(func=cmd_isa_rebalance)

    isn = isa_sub.add_parser(
        "snapshot",
        help="Record a NAV snapshot (book value + contributions + benchmarks)",
    )
    isn.set_defaults(func=cmd_isa_snapshot)

    il = isa_sub.add_parser("log", help="ISA decision history (newest first)")
    il.add_argument(
        "--limit", type=_positive_int, default=20, help="Max rows (default 20)"
    )
    il.set_defaults(func=cmd_isa_log)

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
    pb.add_argument(
        "--portfolio",
        default=None,
        help='Route to the named portfolio (default: first portfolio for the market, e.g. "ISA")',
    )
    pb.set_defaults(func=cmd_portfolio_buy)

    ps = pf_sub.add_parser("sell", help="Record a sell")
    ps.add_argument("ticker", help="Stock ticker")
    ps.add_argument("--qty", type=float, required=True)
    ps.add_argument("--price", type=float, required=True)
    ps.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    ps.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ps.add_argument("--note", default=None)
    ps.add_argument("--thesis-id", default=None)
    ps.add_argument(
        "--portfolio",
        default=None,
        help='Route to the named portfolio (default: first portfolio for the market, e.g. "ISA")',
    )
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
    pi.add_argument(
        "--portfolio",
        default=None,
        help='Route to the named portfolio (default: first portfolio for the market, e.g. "ISA")',
    )
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

    pec = pf_sub.add_parser(
        "exit-check",
        help="Advisory exit/trim/add check (ATR trailing stop + R:R + thesis)",
    )
    pec.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    pec.add_argument(
        "--atr-mult",
        type=float,
        default=3.0,
        help="Chandelier ATR multiplier (default 3.0)",
    )
    pec.add_argument(
        "--tp-rr",
        type=float,
        default=2.0,
        help="Take-profit R-multiple threshold (default 2.0)",
    )
    pec.set_defaults(func=cmd_portfolio_exit_check)

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

    # watch — delayed/EOD-ish trigger alerts for a saved watchlist + open
    # predictions + portfolio positions.
    watch = sub.add_parser(
        "watch", help="Watchlist trigger alerts (delayed/EOD, not real-time)"
    )
    watch_sub = watch.add_subparsers(dest="watch_command", required=True)

    wa = watch_sub.add_parser("add", help="Add a saved watch with trigger levels")
    wa.add_argument("--ticker", required=True)
    wa.add_argument("--market", required=True, choices=["US", "KR", "us", "kr"])
    wa.add_argument(
        "--direction", default="BULL", choices=["BULL", "BEAR", "bull", "bear"]
    )
    wa.add_argument("--entry-low", type=float, default=None)
    wa.add_argument("--entry-high", type=float, default=None)
    wa.add_argument("--stop", type=float, default=None)
    wa.add_argument("--target", type=float, default=None)
    wa.add_argument("--reentry", type=float, default=None)
    wa.add_argument("--note", default=None)
    wa.set_defaults(func=cmd_watch_add)

    wr = watch_sub.add_parser("remove", help="Remove a saved watch by id")
    wr.add_argument("id", type=int)
    wr.set_defaults(func=cmd_watch_remove)

    wl = watch_sub.add_parser(
        "list", help="List the unified watchlist (saved + predictions + positions)"
    )
    wl.add_argument("--market", default=None, choices=["US", "KR", "us", "kr"])
    wl.set_defaults(func=cmd_watch_list)

    wc = watch_sub.add_parser(
        "check", help="Run one monitor pass; prints fired triggers as JSON"
    )
    wc.add_argument("--market", default=None, choices=["US", "KR", "us", "kr"])
    wc.add_argument("--force", action="store_true", help="Ignore the market-hours gate")
    wc.add_argument(
        "--dry-run", action="store_true", help="Evaluate but send no Telegram"
    )
    wc.set_defaults(func=cmd_watch_check)

    return parser


def main() -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
