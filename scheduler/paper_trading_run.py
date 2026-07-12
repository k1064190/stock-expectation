"""Daily paper-trading runner (cron + replay).

Trades the logged LIVE BULL predictions in a simulated per-market book, applying
the deterministic strategy + cost model in ``paper_trading/``. Two modes:

    # Forward (cron): process today for both books.
    uv run python scheduler/paper_trading_run.py --market ALL

    # Replay: bootstrap >=1 month of NAV history from already-logged predictions.
    uv run python scheduler/paper_trading_run.py --market ALL --replay 2026-04-04..2026-06-22

The trading calendar and prices come from the same US/KR providers the outcome
tracker uses; the benchmark column tracks a passive index buy-and-hold (SPY / KODEX 200).
Pure helpers (horizon_end, day_candidates, build_price_map, trading_dates,
benchmark_nav) are unit-tested; the orchestration is exercised by the live replay.
"""

from __future__ import annotations

import argparse
import bisect
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-prediction-store"))
sys.path.insert(0, str(PROJECT_ROOT / "mcp-market-data"))
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass

from paper_trading import engine
from paper_trading import models as pt
from paper_trading.strategy import Candidate, StrategyParams

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] paper_trading: %(message)s"
)
logger = logging.getLogger("paper_trading_run")

KR_TZ = ZoneInfo("Asia/Seoul")
# Exchange-local timezones: a prediction's UTC created_at is converted to the
# market's local date so a pre-open KR signal (07:00 KST = prior UTC day) is
# dated to the session it should actually trade into, not the prior one.
MARKET_TZ = {"US": ZoneInfo("America/New_York"), "KR": ZoneInfo("Asia/Seoul")}
INITIAL_CAPITAL = {"US": 100_000.0, "KR": 100_000_000.0}
# Forward (cron) mode processes this trailing window of sessions ending at as_of,
# so a daily tick catches the last completed session even though "today" hasn't
# closed yet; already-recorded days are idempotent no-ops.
FORWARD_WINDOW_DAYS = 7
BENCHMARK_TICKER = {"US": "SPY", "KR": "069500"}  # SPY / KODEX 200
TIMEFRAME_CALENDAR_DAYS = {"1W": 7, "2W": 14, "1M": 30, "3M": 90, "6M": 180, "1Y": 365}


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------- #


def signal_local_date(created_at_iso: str, market: str) -> str:
    """The exchange-local calendar date a prediction was created on.

    ``created_at`` is stored in UTC; converting to the market timezone keeps a
    pre-open KR signal (07:00 KST, which is the prior UTC day) on its own
    trading day rather than the previous session's close (avoids a stale,
    look-behind fill).
    """
    dt = datetime.fromisoformat(created_at_iso)
    return dt.astimezone(MARKET_TZ[market]).date().isoformat()


def effective_entry_date(
    local_date: str, trading_dates_sorted: list[str]
) -> str | None:
    """First trading date on or after ``local_date`` (None if past the range).

    A signal generated pre-open, on a weekend, or on a holiday fills at the next
    available session — never an earlier one.
    """
    i = bisect.bisect_left(trading_dates_sorted, local_date)
    return trading_dates_sorted[i] if i < len(trading_dates_sorted) else None


def horizon_end(created_date: str, timeframe: str) -> str:
    """Calendar-date horizon end for a lot opened on ``created_date``."""
    d = date.fromisoformat(created_date)
    return (d + timedelta(days=TIMEFRAME_CALENDAR_DAYS.get(timeframe, 30))).isoformat()


def day_candidates(rows: list[dict]) -> list[Candidate]:
    """Build one Candidate per ticker from a day's BULL prediction rows.

    Multiple horizons per ticker collapse to the highest-confidence row, whose
    target/stop/horizon define the lot.
    """
    best: dict[str, dict] = {}
    for r in rows:
        prev = best.get(r["ticker"])
        if prev is None or r["conf"] > prev["conf"]:
            best[r["ticker"]] = r
    return [
        Candidate(
            ticker=r["ticker"],
            ref_price=r["entry_price"],  # engine resolves the actual fill from close
            confidence=r["conf"],
            prediction_id=r["id"],
            stop_price=r["stop_price"],
            target_price=r["target_price"],
            # Measure the horizon from the actual entry session (which may be
            # delayed past a weekend/holiday), not the raw signal date.
            horizon_end_date=horizon_end(
                r.get("entry_date", r["cdate"]), r["timeframe"]
            ),
        )
        for r in best.values()
    ]


def build_price_map(batch: dict) -> dict:
    """Turn provider batch output ({ticker: [OHLCV]}) into {ticker: {date: bar}}."""
    pm: dict[str, dict] = {}
    for ticker, bars in batch.items():
        pm[ticker] = {
            b.date: {"open": b.open, "high": b.high, "low": b.low, "close": b.close}
            for b in bars
        }
    return pm


def trading_dates(benchmark_map: dict, frm: str, to: str) -> list[str]:
    """Trading calendar = the benchmark series' dates within [frm, to]."""
    return sorted(d for d in benchmark_map if frm <= d <= to)


def benchmark_nav(
    initial_capital: float, benchmark_map: dict, on_date: str, baseline_close: float
) -> float | None:
    """Passive index buy-and-hold NAV: initial capital scaled from a fixed baseline.

    ``baseline_close`` is the benchmark close at the book's inception (recovered
    by the caller so it stays constant across runs), keeping the benchmark series
    continuous rather than re-anchoring to each run's first date.
    """
    if on_date not in benchmark_map or not baseline_close or baseline_close <= 0:
        return None
    return initial_capital * benchmark_map[on_date]["close"] / baseline_close


# --------------------------------------------------------------------------- #
# Data access + orchestration (live; smoke-tested via replay)
# --------------------------------------------------------------------------- #


def _provider(market: str):
    if market == "US":
        from providers.us import USMarketProvider

        return USMarketProvider()
    from providers.kr import KoreanMarketProvider

    return KoreanMarketProvider()


def filter_low_edge_band(rows: list[dict]) -> tuple[list[dict], int]:
    """Drop rows whose components JSON carries a truthy ``low_edge_band``.

    The tag marks the 0.60-0.70 raw-confidence band with negative realized
    edge — those predictions still log (training data), but the paper book
    does not commit capital to them. Unparseable/absent components keep the
    row (fail-open).

    Args:
        rows: Prediction row dicts with an optional ``components`` JSON string.

    Returns:
        (kept_rows, skipped_count).
    """
    kept: list[dict] = []
    skipped = 0
    for r in rows:
        raw = r.get("components")
        tagged = False
        if raw:
            try:
                comp = raw if isinstance(raw, dict) else json.loads(raw)
                tagged = bool(comp.get("low_edge_band"))
            except (json.JSONDecodeError, AttributeError, TypeError):
                tagged = False
        if tagged:
            skipped += 1
        else:
            kept.append(r)
    return kept, skipped


def _fetch_bull_predictions(market: str, frm: str, to: str) -> list[dict]:
    """LIVE BULL predictions created in [frm, to] for a market (read-only)."""
    import models as pred_models

    # Widen the UTC window by a day each side so signals near the UTC/local-date
    # boundary are caught; precise filtering happens via local date + effective
    # entry date in run_range.
    frm_pad = (date.fromisoformat(frm) - timedelta(days=2)).isoformat()
    to_pad = (date.fromisoformat(to) + timedelta(days=1)).isoformat()
    conn = pred_models.get_connection()
    try:
        rows = conn.execute(
            "SELECT ticker, COALESCE(raw_confidence, confidence) AS conf, timeframe, "
            "created_at, entry_price, target_price, stop_price, id, components "
            "FROM predictions "
            "WHERE source='LIVE' AND direction='BULL' AND market=? "
            "AND status != 'CANCELLED' "  # retracted signals never reach the paper book
            "AND date(created_at) BETWEEN ? AND ?",
            (market, frm_pad, to_pad),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _fetch_price_map(provider, tickers: list[str], lookback_days: int) -> dict:
    batch = provider.get_price_history_batch(tickers, days=lookback_days)
    return build_price_map(batch)


def run_range(market: str, frm: str, to: str, params: StrategyParams) -> dict:
    """Run the paper-trading book for ``market`` across [frm, to] (inclusive).

    Used for both replay (a wide range) and forward cron (frm == to == today).
    Returns a small summary dict.
    """
    rows = _fetch_bull_predictions(market, frm, to)
    rows, skipped_low_edge = filter_low_edge_band(rows)
    if skipped_low_edge:
        logger.info(
            "%s: skipped %d low_edge_band prediction(s) (0.60-0.70 raw-conf "
            "band, negative realized edge)",
            market,
            skipped_low_edge,
        )
    for r in rows:
        r["cdate"] = signal_local_date(
            r["created_at"], market
        )  # exchange-local signal date

    conn = pt.get_connection()
    try:
        account = pt.seed_account(conn, market, INITIAL_CAPITAL[market])
        conn.commit()  # persist the seed even if the loop below runs zero days
        # Include already-held tickers so multi-day lots (whose ticker may no
        # longer appear in the prediction window) still get fresh bars for
        # exits and marking.
        held_now = {p.ticker for p in pt.get_open_positions(conn, account.id)}

        tickers = sorted(
            {r["ticker"] for r in rows} | held_now | {BENCHMARK_TICKER[market]}
        )
        # Providers treat ``days`` as a lookback from today, so span today back to
        # ``frm`` (not just the range length) — otherwise a past replay fetches
        # recent bars instead of the requested window.
        today = datetime.now(KR_TZ).date()
        lookback = (today - date.fromisoformat(frm)).days + 7
        provider = _provider(market)
        logger.info(
            "[%s] fetching prices for %d tickers (%d-day lookback)",
            market,
            len(tickers),
            lookback,
        )
        price_map = _fetch_price_map(provider, tickers, lookback)

        bmap = price_map.get(BENCHMARK_TICKER[market], {})
        dates = trading_dates(bmap, frm, to)
        if not dates:
            # No benchmark calendar — fall back to the union of all tickers' dates.
            all_dates = {d for t in price_map for d in price_map[t]}
            dates = sorted(d for d in all_dates if frm <= d <= to)
        first_bench = next((d for d in dates if d in bmap), None)
        # Anchor the benchmark to the book's inception so SPY/KODEX scaling stays
        # continuous across a later forward-window run (whose `dates` start mid-book).
        # Recover the inception close from any existing NAV row in this run's price
        # window; for a fresh book use this range's first benchmark date.
        baseline_close = None
        for r in pt.get_nav_history(conn, account.id):
            if r.benchmark_nav and r.benchmark_nav > 0 and r.date in bmap:
                baseline_close = (
                    bmap[r.date]["close"] * account.initial_capital / r.benchmark_nav
                )
                break
        if baseline_close is None and first_bench:
            baseline_close = bmap[first_bench]["close"]

        # Map each signal to the first trading session on/after its local date;
        # the lot's horizon is measured from that actual entry session. Drop
        # signals whose local date precedes the window (their session was earlier).
        preds_by_day: dict[str, list[dict]] = {}
        for r in rows:
            if r["cdate"] < frm:
                continue
            eff = effective_entry_date(r["cdate"], dates)
            if eff is not None:
                r["entry_date"] = eff
                preds_by_day.setdefault(eff, []).append(r)

        last_close: dict[str, float] = (
            {}
        )  # most recent close per ticker (carry-forward marks)
        for d in dates:
            for t in price_map:
                if d in price_map[t]:
                    last_close[t] = price_map[t][d]["close"]
            cands = day_candidates(preds_by_day.get(d, []))
            held = {p.ticker for p in pt.get_open_positions(conn, account.id)}
            needed = held | {c.ticker for c in cands}
            prices = {
                t: price_map[t][d]
                for t in needed
                if t in price_map and d in price_map[t]
            }
            bnav = (
                benchmark_nav(account.initial_capital, bmap, d, baseline_close)
                if baseline_close
                else None
            )
            engine.run_day(
                conn,
                account,
                d,
                prices,
                cands,
                params,
                benchmark_nav=bnav,
                marks=last_close,
            )
            account = pt.get_account(conn, market)  # refresh cash for next day's sizing
        latest = pt.get_latest_nav(conn, account.id)
    finally:
        conn.close()

    summary = {
        "market": market,
        "from": frm,
        "to": to,
        "days_run": len(dates),
        "final_nav": latest.nav if latest else None,
        "cumulative_return": latest.cumulative_return if latest else None,
        "benchmark_nav": latest.benchmark_nav if latest else None,
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-trading runner")
    parser.add_argument("--market", choices=["US", "KR", "ALL"], default="ALL")
    parser.add_argument("--as-of", help="Single date YYYY-MM-DD (default: today KST).")
    parser.add_argument(
        "--replay", help="Range FROM..TO (e.g. 2026-04-04..2026-06-22)."
    )
    args = parser.parse_args()

    params = StrategyParams()
    markets = ["US", "KR"] if args.market == "ALL" else [args.market]

    if args.replay:
        frm, to = args.replay.split("..")
    else:
        # Forward (cron) mode processes a short trailing WINDOW ending at as_of,
        # not just as_of itself: at 06:30 KST "today" hasn't closed (and the last
        # US session is the prior NY date), so a single-day run would find no
        # session. The window catches the last completed session(s); run_day is
        # idempotent per (account, date), so already-recorded days are no-ops.
        as_of = args.as_of or datetime.now(KR_TZ).strftime("%Y-%m-%d")
        frm = (
            date.fromisoformat(as_of) - timedelta(days=FORWARD_WINDOW_DAYS)
        ).isoformat()
        to = as_of

    for market in markets:
        summary = run_range(market, frm, to, params)
        ret = summary["cumulative_return"]
        bnav = summary["benchmark_nav"]
        logger.info(
            "[%s] %s..%s  days=%d  NAV=%.2f  cum_return=%s  benchmark_NAV=%s",
            market,
            frm,
            to,
            summary["days_run"],
            summary["final_nav"] or 0.0,
            f"{ret:+.2%}" if ret is not None else "n/a",
            f"{bnav:,.0f}" if bnav is not None else "n/a",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
