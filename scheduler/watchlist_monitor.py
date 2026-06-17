"""Watchlist trigger monitor — DELAYED / EOD-ish, NOT real-time.

IMPORTANT — this is not an intraday alerter. It evaluates triggers against
``provider.get_current_price()``, which returns the LAST CLOSE (it calls
``get_price_history(days=5)`` and takes the last bar). KR data via PyKRX is
end-of-day oriented. So every "touch" this script reports is a delayed read of
the most recent available close, never a live tick. Schedule it on an EOD-
appropriate cadence (see scheduler/crontab.example) and read its alerts as
"the last close crossed your level", not "price is crossing right now".

It builds a unified watchlist (saved rows + OPEN predictions + portfolio
positions — see watchlist_store.py), fetches the latest price per ticker
(never-raise; a None price is skipped and counted), and fires Korean Telegram
alerts on rising-edge trigger transitions with a dedup/cooldown backstop.

This script is ALERT-ONLY. It never mutates predictions or portfolio data —
the outcome tracker remains the sole writer of HIT/MISS/EXPIRED.

Run on a trading-window cadence (KST):
    python watchlist_monitor.py --market US
    python watchlist_monitor.py            # both markets, market-hours gated
"""

import json
import logging
import os
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "mcp-market-data"))

# Auto-load .env for API keys (FMP, Telegram, etc.).
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass

from providers.us import USMarketProvider
from providers.kr import KoreanMarketProvider
from scheduler.telegram_sender import send_watch_alert
from scheduler.watchlist_store import WatchTarget, load_unified_watchlist

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("watchlist_monitor")

KR_TZ = ZoneInfo("Asia/Seoul")

# Korean regular session in KST.
KR_OPEN = time(9, 0)
KR_CLOSE = time(15, 30)
# US regular session expressed in KST. US 09:30-16:00 ET maps to roughly
# 22:30-05:00 KST during EDT (the common case); EST shifts an hour later. We
# use a slightly generous 22:30-05:00 window so the monitor stays awake across
# both DST regimes — this is an EOD-ish alerter, so a loose window is fine.
US_OPEN_KST = time(22, 30)
US_CLOSE_KST = time(5, 0)

# Entry-zone band for non-saved sources (prediction/position) that carry a
# single entry price rather than an explicit low/high zone: +/-1% around entry.
ENTRY_BAND_PCT = 0.01

# Per-key cooldown backstop: even on a fresh rising edge, never re-alert the
# same key within this window. Guards against rapid flip-flop near a boundary.
COOLDOWN = timedelta(hours=6)

STATE_PATH = PROJECT_ROOT / "state" / "watchlist_alerts.json"

# Korean labels for each trigger type, used in alert message bodies.
TRIGGER_LABELS_KR = {
    "ENTRY": "진입 구간 도달",
    "STOP": "손절가 도달",
    "TARGET": "목표가 도달",
    "REENTRY": "재진입 신호",
}


# ---------------------------------------------------------------------------
# Market-hours gate
# ---------------------------------------------------------------------------


def is_market_open(market: str, now: datetime) -> bool:
    """Return whether ``market`` is in its regular session at KST ``now``.

    Weekday-only (Mon-Fri in KST). The US window wraps past midnight, so an
    open session spans the previous KST evening into the early morning; we treat
    any time at/after the US open OR before the US close as in-session, and
    require a weekday on whichever calendar day the session belongs to.

    Args:
        market: "US" or "KR".
        now: Timezone-aware datetime; converted to KST internally.

    Returns:
        True if the market's regular session is currently open.
    """
    kst = now.astimezone(KR_TZ)
    t = kst.time()
    weekday = kst.weekday()  # Mon=0 .. Sun=6

    if market.upper() == "KR":
        if weekday >= 5:  # Sat/Sun
            return False
        return KR_OPEN <= t <= KR_CLOSE

    if market.upper() == "US":
        # US session runs evening KST → next morning KST. The evening portion
        # (>= 22:30) belongs to that weekday; the morning portion (< 05:00)
        # belongs to the prior US trading day, i.e. the previous KST weekday.
        if t >= US_OPEN_KST:
            return weekday < 5  # evening start, must be a KST weekday
        if t < US_CLOSE_KST:
            # Early morning: the session opened the previous KST day. Mon
            # morning KST (weekday 0) is Sun evening US — market closed. So
            # require the *previous* KST day to be a weekday: Tue-Sat morning.
            return 1 <= weekday <= 5
        return False

    return False


def _markets_to_check(market_arg: str | None, now: datetime, force: bool) -> list[str]:
    """Resolve which markets to evaluate this run.

    Args:
        market_arg: "US"/"KR" to restrict to one market, or None for both.
        now: Current timezone-aware time.
        force: When True, skip the market-hours gate entirely.

    Returns:
        List of market codes whose session is open (or all requested when
        ``force`` is set).
    """
    requested = [market_arg.upper()] if market_arg else ["US", "KR"]
    if force:
        return requested
    return [m for m in requested if is_market_open(m, now)]


# ---------------------------------------------------------------------------
# Trigger evaluation
# ---------------------------------------------------------------------------


def evaluate_triggers(target: WatchTarget, price: float) -> list[str]:
    """Return the set of trigger types currently SATISFIED at ``price``.

    This reports the instantaneous triggered state, not edges — the caller's
    dedup layer converts these into rising-edge alerts. Trigger rules (BULL
    default; BEAR mirrored):

    - ENTRY: inside the entry zone. Saved rows use [entry_low, entry_high];
      prediction/position rows use a +/-1% band around their single entry.
    - STOP: BULL price <= stop; BEAR price >= stop.
    - TARGET: BULL price >= target; BEAR price <= target.
    - REENTRY: price >= reentry (saved rows only). The "crosses back above
      after being below" edge is enforced by the dedup state machine, which
      only fires on a transition into the satisfied state.

    Args:
        target: The normalized watch target.
        price: Latest close price.

    Returns:
        List of satisfied trigger-type strings (subset of ENTRY/STOP/TARGET/
        REENTRY), in a stable order.
    """
    satisfied: list[str] = []
    is_bear = target.direction == "BEAR"

    # ENTRY zone.
    if target.entry_low is not None and target.entry_high is not None:
        low, high = target.entry_low, target.entry_high
        if low == high:
            # Single-point entry (prediction/position) → +/-1% band.
            low = high * (1 - ENTRY_BAND_PCT)
            high = high * (1 + ENTRY_BAND_PCT)
        if low <= price <= high:
            satisfied.append("ENTRY")

    # STOP.
    if target.stop is not None:
        if is_bear:
            if price >= target.stop:
                satisfied.append("STOP")
        else:
            if price <= target.stop:
                satisfied.append("STOP")

    # TARGET.
    if target.target is not None:
        if is_bear:
            if price <= target.target:
                satisfied.append("TARGET")
        else:
            if price >= target.target:
                satisfied.append("TARGET")

    # REENTRY (saved-only; reentry is None for other sources). Crossing-up edge
    # is handled by the dedup state machine.
    if target.reentry is not None and price >= target.reentry:
        satisfied.append("REENTRY")

    return satisfied


# ---------------------------------------------------------------------------
# Dedup / re-arm state
# ---------------------------------------------------------------------------


def load_state(path: Path = STATE_PATH) -> dict:
    """Load the alert dedup state, returning {} on any read failure.

    Args:
        path: State file path.

    Returns:
        Parsed state dict keyed by ``{source}:{ticker}:{market}:{trigger}``.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    """Atomically write the dedup state (temp file + os.replace).

    Args:
        state: State dict to persist.
        path: Destination path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _state_key(target: WatchTarget, trigger_type: str) -> str:
    """Build the per-trigger dedup key."""
    return f"{target.source}:{target.ticker}:{target.market}:{trigger_type}"


def should_fire(state: dict, key: str, satisfied: bool, now: datetime) -> bool:
    """Decide whether a rising-edge alert should fire, mutating ``state``.

    Implements the rising-edge + cooldown contract:

    - Fire only when the trigger transitions from NOT-satisfied to satisfied
      (rising edge). A trigger that stays satisfied across runs alerts once.
    - Once it transitions out (no longer satisfied) and back in, it re-arms and
      may alert again.
    - A 6h per-key cooldown is a backstop: even a genuine fresh rising edge is
      suppressed if the last alert for this key was within COOLDOWN.

    ``state[key]`` stores ``{"last_state": bool, "last_alert_iso": str|None}``.

    Args:
        state: Mutable dedup state dict (updated in place).
        key: The per-trigger key.
        satisfied: Whether the trigger is satisfied this run.
        now: Current timezone-aware time.

    Returns:
        True if an alert should be sent for this key now.
    """
    entry = state.get(key, {"last_state": False, "last_alert_iso": None})
    prev_state = bool(entry.get("last_state", False))

    fire = False
    if satisfied and not prev_state:
        # Rising edge — subject to cooldown backstop.
        last_iso = entry.get("last_alert_iso")
        if last_iso:
            try:
                last = datetime.fromisoformat(last_iso)
                if now - last < COOLDOWN:
                    fire = False
                else:
                    fire = True
            except ValueError:
                fire = True
        else:
            fire = True

    entry["last_state"] = satisfied
    if fire:
        entry["last_alert_iso"] = now.isoformat()
    state[key] = entry
    return fire


def _prune_state(state: dict, live_prefixes: set[str]) -> None:
    """Drop state keys whose ``{source}:{ticker}:{market}`` no longer exists.

    Keeps the state file from growing unbounded as watches/predictions/
    positions are removed.

    Args:
        state: Mutable dedup state dict (updated in place).
        live_prefixes: Set of ``{source}:{ticker}:{market}`` prefixes still
            present in the current unified watchlist.
    """
    for key in list(state.keys()):
        # key == "{source}:{ticker}:{market}:{trigger}" → prefix is first 3.
        parts = key.rsplit(":", 1)
        if len(parts) != 2 or parts[0] not in live_prefixes:
            del state[key]


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _get_provider(market: str):
    """Return the market data provider for a market code."""
    return USMarketProvider() if market.upper() == "US" else KoreanMarketProvider()


def run_monitor(
    market: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
    state_path: Path = STATE_PATH,
    watchlist_db_path: Path | None = None,
    predictions_db_path: Path | None = None,
    portfolio_db_path: Path | None = None,
) -> dict:
    """Run one monitoring pass over the unified watchlist.

    Never raises on provider or Telegram failures — both are caught, counted,
    and the loop continues (mirrors outcome_tracker's never-raise ethos). This
    function NEVER mutates predictions or portfolio data; it is alert-only.

    Args:
        market: "US"/"KR" to restrict to one market, or None for both.
        force: Skip the market-hours gate (still respects --market).
        dry_run: Evaluate and update state but send no Telegram messages.
        now: Override for the current time (tests). Defaults to KST now.
        state_path: Dedup state file path (tests).
        watchlist_db_path: Override for the saved watchlist DB (tests).
        predictions_db_path: Override for predictions.db (tests).
        portfolio_db_path: Override for portfolio.db (tests).

    Returns:
        Summary dict: ``{"checked", "fired", "errors", "skipped",
        "markets", "triggers": [...]}`` where each triggers entry describes a
        fired alert.
    """
    now = now or datetime.now(KR_TZ)
    markets = _markets_to_check(market, now, force)

    summary: dict = {
        "checked": 0,
        "fired": 0,
        "errors": 0,
        "skipped": 0,
        "markets": markets,
        "triggers": [],
    }

    if not markets:
        logger.info("No markets open (KST %s) — nothing to do.", now.isoformat())
        return summary

    state = load_state(state_path)
    live_prefixes: set[str] = set()

    for mkt in markets:
        targets = load_unified_watchlist(
            market=mkt,
            watchlist_db_path=watchlist_db_path,
            predictions_db_path=predictions_db_path,
            portfolio_db_path=portfolio_db_path,
        )
        provider = _get_provider(mkt)

        for target in targets:
            live_prefixes.add(f"{target.source}:{target.ticker}:{target.market}")
            try:
                price = provider.get_current_price(target.ticker)
            except Exception as e:  # never-raise: count and continue
                logger.warning(
                    "Price fetch raised for %s (%s): %s", target.ticker, mkt, e
                )
                summary["errors"] += 1
                continue

            if price is None:
                logger.warning("No price for %s (%s) — skipping", target.ticker, mkt)
                summary["errors"] += 1
                continue

            summary["checked"] += 1
            satisfied = set(evaluate_triggers(target, price))

            # Evaluate every possible trigger type for this target so a
            # transition OUT of a satisfied state is recorded even when nothing
            # fires (that's what re-arms the rising edge). Sends are deferred
            # until after the loop so we can pick individual-vs-digest delivery
            # from the final fired count.
            for trigger_type in ("ENTRY", "STOP", "TARGET", "REENTRY"):
                key = _state_key(target, trigger_type)
                is_sat = trigger_type in satisfied
                if should_fire(state, key, is_sat, now):
                    summary["fired"] += 1
                    summary["triggers"].append(
                        {
                            "ticker": target.ticker,
                            "market": target.market,
                            "trigger": trigger_type,
                            "price": price,
                            "source": target.source,
                            "direction": target.direction,
                            "target": target,
                        }
                    )

    # Prune stale keys and persist (even in dry-run we keep state accurate so a
    # subsequent real run doesn't double-fire; dry-run only suppresses sends).
    _prune_state(state, live_prefixes)
    save_state(state, state_path)

    # Deliver: one focused Korean message per trigger, or a single batched
    # digest when more than 3 fire. Suppressed entirely in dry-run.
    if not dry_run:
        _deliver(summary["triggers"])

    # The carried WatchTarget objects are an internal delivery detail; strip
    # them so the returned summary stays JSON-serializable (CLI prints it).
    for t in summary["triggers"]:
        t.pop("target", None)

    logger.info(
        "Watchlist monitor done: %d checked, %d fired, %d errors (markets=%s)",
        summary["checked"],
        summary["fired"],
        summary["errors"],
        markets,
    )
    return summary


def _deliver(triggers: list[dict]) -> None:
    """Send fired triggers to Telegram (never-raise).

    One focused Korean message per trigger when <=3 fired; a single batched
    Korean digest when more than 3 fired (to avoid flooding the chat). Each
    send is wrapped so a Telegram failure never aborts the run.

    Args:
        triggers: Fired trigger dicts from run_monitor. Each carries a
            ``"target"`` WatchTarget used to enrich the individual message.
    """
    if not triggers:
        return

    if len(triggers) > 3:
        from scheduler.telegram_sender import send_message

        lines = [f"\U0001f514 워치리스트 알림 {len(triggers)}건 (지연/종가 기준)"]
        for t in triggers:
            label = TRIGGER_LABELS_KR.get(t["trigger"], t["trigger"])
            lines.append(
                f"• {t['ticker']} ({t['market']}) — {label} @ {t['price']:.2f}"
            )
        try:
            send_message("\n".join(lines))
        except Exception as e:
            logger.warning("Telegram digest failed: %s", e)
        return

    for t in triggers:
        try:
            send_watch_alert(
                ticker=t["ticker"],
                market=t["market"],
                trigger_type=t["trigger"],
                price=t["price"],
                target=t["target"],
            )
        except Exception as e:
            logger.warning("Telegram watch alert failed: %s", e)


def _build_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="watchlist_monitor",
        description="Delayed/EOD watchlist trigger monitor (alert-only).",
    )
    parser.add_argument("--market", default=None, choices=["US", "KR", "us", "kr"])
    parser.add_argument(
        "--force", action="store_true", help="Ignore the market-hours gate"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Evaluate but send no Telegram"
    )
    return parser


if __name__ == "__main__":
    args = _build_arg_parser().parse_args()
    result = run_monitor(market=args.market, force=args.force, dry_run=args.dry_run)
    logger.info(json.dumps(result, ensure_ascii=False))
