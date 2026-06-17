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
import tempfile
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
US_TZ = ZoneInfo("America/New_York")

# Korean regular session in KST.
KR_OPEN = time(9, 0)
KR_CLOSE = time(15, 30)
# US regular session in EXCHANGE-LOCAL time (America/New_York), which makes the
# gate DST-aware automatically: 09:30-16:00 ET maps to ~22:30-05:00 KST under
# EDT and ~23:30-06:00 KST under EST. A KST-fixed window mishandled the EST
# post-close (16:00 ET = 06:00 KST), skipping the post-close EOD run.
US_OPEN_ET = time(9, 30)
US_CLOSE_ET = time(16, 0)
# Post-close EOD grace: this is a delayed/close-based alerter, so a run shortly
# after the 16:00 ET close should still fire against the final close rather than
# be gated out. Keep the session "open" for this long past the close.
US_POST_CLOSE_GRACE = timedelta(minutes=30)

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
    """Return whether ``market`` is in its regular session at ``now``.

    Weekday-only (Mon-Fri in the exchange's local calendar). Each market is
    evaluated in its EXCHANGE-LOCAL timezone so the gate is DST-aware: KR in
    Asia/Seoul, US in America/New_York. The US session therefore tracks the real
    16:00 ET close across both EDT and EST instead of a fixed KST window.

    Args:
        market: "US" or "KR".
        now: Timezone-aware datetime; converted to the exchange tz internally.

    Returns:
        True if the market's regular session is currently open (plus a short
        post-close EOD grace for the US session).
    """
    if market.upper() == "KR":
        kst = now.astimezone(KR_TZ)
        if kst.weekday() >= 5:  # Sat/Sun
            return False
        return KR_OPEN <= kst.time() <= KR_CLOSE

    if market.upper() == "US":
        et = now.astimezone(US_TZ)
        if et.weekday() >= 5:  # Sat/Sun in ET
            return False
        # RTH 09:30-16:00 ET (inclusive close), plus a post-close EOD grace so a
        # run shortly after the close still reads the final close. Compared as
        # full datetimes (not bare times) so the grace is robust even if it ever
        # spills past midnight.
        session_start = datetime.combine(et.date(), US_OPEN_ET, tzinfo=US_TZ)
        session_end = (
            datetime.combine(et.date(), US_CLOSE_ET, tzinfo=US_TZ) + US_POST_CLOSE_GRACE
        )
        return session_start <= et <= session_end

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
    """Atomically write the dedup state (unique temp file + os.replace).

    Each call allocates a UNIQUE same-directory temp file via tempfile.mkstemp
    so two overlapping monitor runs can't clobber a shared ``.tmp`` path or make
    ``os.replace`` fail. The replace is atomic on the same filesystem.

    Args:
        state: State dict to persist.
        path: Destination path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        # On any failure, don't leave the unique temp file behind.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
                # Snapshot the entry BEFORE should_fire consumes the edge, so a
                # later send FAILURE can roll the edge back and the next run
                # retries instead of being suppressed.
                prior = dict(
                    state.get(key, {"last_state": False, "last_alert_iso": None})
                )
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
                            "_state_key": key,
                            "_prior": prior,
                        }
                    )

    # Deliver BEFORE persisting the edge consumption: one focused Korean message
    # per trigger, or a single batched digest when more than 3 fire. Suppressed
    # entirely in dry-run. A failed send must NOT consume the rising edge, so we
    # roll back the edge/cooldown state for any fired trigger that wasn't
    # delivered — leaving it armed for the next run to retry.
    if not dry_run:
        delivered_keys = _deliver(summary["triggers"])
        for t in summary["triggers"]:
            if t["_state_key"] not in delivered_keys:
                state[t["_state_key"]] = t["_prior"]

    # Prune stale keys and persist. In dry-run we keep state accurate (fired
    # edges committed) so a subsequent real run doesn't double-fire; dry-run
    # only suppresses sends. After delivery, only successfully-delivered edges
    # remain consumed.
    _prune_state(state, live_prefixes)
    save_state(state, state_path)

    # The carried WatchTarget objects and internal bookkeeping keys are a
    # delivery detail; strip them so the returned summary stays JSON-serializable
    # (CLI prints it).
    for t in summary["triggers"]:
        t.pop("target", None)
        t.pop("_state_key", None)
        t.pop("_prior", None)

    logger.info(
        "Watchlist monitor done: %d checked, %d fired, %d errors (markets=%s)",
        summary["checked"],
        summary["fired"],
        summary["errors"],
        markets,
    )
    return summary


def _deliver(triggers: list[dict]) -> set[str]:
    """Send fired triggers to Telegram (never-raise); return delivered keys.

    One focused Korean message per trigger when <=3 fired; a single batched
    Korean digest when more than 3 fired (to avoid flooding the chat). Each
    send is wrapped so a Telegram failure never aborts the run.

    The returned set of ``_state_key`` strings lets the caller commit the
    rising-edge/cooldown state only for triggers that were actually delivered —
    a failed send leaves its edge un-consumed so the next run retries. A stable
    state key (not object identity) is used so matching survives any object
    churn.

    Args:
        triggers: Fired trigger dicts from run_monitor. Each carries a
            ``"target"`` WatchTarget used to enrich the individual message and a
            ``"_state_key"`` used to report delivery success.

    Returns:
        The set of ``_state_key`` values whose send succeeded. For the digest
        path the single message is all-or-nothing: every key on success, none on
        failure.
    """
    if not triggers:
        return set()

    if len(triggers) > 3:
        try:
            from scheduler.telegram_sender import send_message

            lines = [f"\U0001f514 워치리스트 알림 {len(triggers)}건 (지연/종가 기준)"]
            for t in triggers:
                label = TRIGGER_LABELS_KR.get(t["trigger"], t["trigger"])
                lines.append(
                    f"• {t['ticker']} ({t['market']}) — {label} @ {t['price']:.2f}"
                )
            ok = send_message("\n".join(lines))
        except Exception as e:
            logger.warning("Telegram digest failed: %s", e)
            return set()
        if not ok:
            logger.warning("Telegram digest send returned failure")
            return set()
        return {t["_state_key"] for t in triggers}

    delivered: set[str] = set()
    for t in triggers:
        try:
            ok = send_watch_alert(
                ticker=t["ticker"],
                market=t["market"],
                trigger_type=t["trigger"],
                price=t["price"],
                target=t["target"],
            )
        except Exception as e:
            logger.warning("Telegram watch alert failed: %s", e)
            continue
        if ok:
            delivered.add(t["_state_key"])
        else:
            logger.warning(
                "Telegram watch alert returned failure for %s (%s)",
                t["ticker"],
                t["market"],
            )
    return delivered


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
