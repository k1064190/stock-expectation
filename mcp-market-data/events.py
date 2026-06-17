"""Catalyst event timeline + R3 event-risk gate.

Unifies two FMP forward calendars into one normalized timeline and turns it
into a deterministic confidence gate (RULE R3) that /expect and daily-briefing
consult before issuing new BUY calls:

  - Earnings calendar (FMP /earning_calendar, US-listed only) → per-ticker
    binary-event risk near the report date.
  - Economic calendar (FMP /economic_calendar) → market-wide macro shocks
    (FOMC / CPI / NFP). KR has no forward EPS feed on FMP, so KR consumes the
    US macro stream (transmitted via FX / SOXL) and gets no per-ticker earnings
    cap.

Design rules (mirrors the rest of mcp-market-data):
  - FAIL-OPEN: a missing FMP key or any fetch error yields a *neutral* gate
    flagged ``gate_unavailable=True``. We never invent caps and never let an
    event-calendar outage break the cron.
  - The earnings + macro windows are fetched ONCE per ``evaluate_gate`` call
    (not once per ticker) to protect the 250/day FMP free-tier quota.

The pure core (``trading_days_between``, ``build_timeline``, ``evaluate_gate``
with injected fetchers) is stdlib-only and fully testable offline.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Literal, Optional

import httpx

logger = logging.getLogger(__name__)

FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"

# --- R3 thresholds (trading days) ------------------------------------------
# A binary earnings event within this window caps the label at WATCH: too close
# to the report to hold a directional thesis through it.
EARNINGS_WATCH_DAYS = 2
# Beyond WATCH but still inside this window: trim confidence rather than cap.
EARNINGS_TRIM_DAYS = 5
# A high-impact macro event (FOMC/CPI/NFP) within this window trims confidence
# on *every* pick in the market.
MACRO_TRIM_DAYS = 2

# Confidence trim magnitudes (subtracted from raw confidence elsewhere).
EARNINGS_CONFIDENCE_TRIM = 0.05
MACRO_CONFIDENCE_TRIM = 0.05

# Substrings (case-insensitive) that mark a macro release as market-moving.
# FMP /economic_calendar event names vary ("CPI", "Consumer Price Index",
# "Nonfarm Payrolls", "Fed Interest Rate Decision", "FOMC ...") so we match on
# keyword fragments rather than exact strings.
MACRO_KEYWORDS = (
    "fomc",
    "fed interest rate",
    "federal funds",
    "interest rate decision",
    "cpi",
    "consumer price index",
    "inflation rate",
    "nonfarm",
    "non-farm",
    "nfp",
    "unemployment rate",
)

Market = Literal["US", "KR", "GLOBAL"]
EventKind = Literal["earnings", "macro"]
Timing = Optional[Literal["BMO", "AMC", "TAS"]]
Impact = Literal["High", "Medium", "Low"]
CapLabel = Optional[Literal["WATCH"]]


@dataclass
class CatalystEvent:
    """A single forward-dated catalyst on the unified timeline.

    Attributes:
        ticker: Stock symbol for earnings events; None for market-wide macro.
        market: "US", "KR", or "GLOBAL" (macro is GLOBAL — it transmits across
            markets via FX / index futures).
        kind: "earnings" or "macro".
        name: Human-readable label (company name or release name).
        event_date: Scheduled date, "YYYY-MM-DD".
        timing: "BMO"/"AMC"/"TAS" for earnings; None for macro.
        impact: "High"/"Medium"/"Low". Earnings are treated as "High" (binary).
        trading_days_until: Business-day (Mon-Fri) distance from the as-of date;
            0 means the event is today.
        source: Provenance string, e.g. "fmp:earning_calendar".
    """

    ticker: Optional[str]
    market: Market
    kind: EventKind
    name: str
    event_date: str
    timing: Timing
    impact: Impact
    trading_days_until: int
    source: str


@dataclass
class EventGate:
    """Deterministic R3 gate verdict for a set of candidate tickers.

    Per-ticker caps/trims live in ``by_ticker``; the market-wide macro trim
    applies on top of every pick in that market.

    Attributes:
        asof: As-of date the gate was computed for, "YYYY-MM-DD".
        market: Market the gate covers ("US" or "KR").
        by_ticker: Maps each requested ticker to its event-risk verdict:
            {cap_label: "WATCH"|None, confidence_trim: float,
             next_earnings_date: str|None, trading_days_until: int|None}.
            For KR, cap_label is always None (no per-ticker earnings feed).
        macro_trim: Confidence trim (0.0 or MACRO_CONFIDENCE_TRIM) applied to
            every pick in the market because of an imminent macro release.
        macro_events: The High-impact macro events that drove ``macro_trim``.
        gate_unavailable: True when the gate is neutral because the FMP key was
            missing or a fetch failed (FAIL-OPEN). Callers must treat caps/trims
            as advisory-zero in that case and proceed.
        notes: Human-readable annotations (e.g. why the gate is unavailable).
    """

    asof: str
    market: str
    by_ticker: dict[str, dict] = field(default_factory=dict)
    macro_trim: float = 0.0
    macro_events: list[dict] = field(default_factory=list)
    gate_unavailable: bool = False
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure core (stdlib only — no network)
# ---------------------------------------------------------------------------


def _parse_date(value: str) -> date:
    """Parse a "YYYY-MM-DD" (or "YYYY-MM-DD HH:MM:SS") string to a date.

    Args:
        value: Date string; only the leading date portion is used.

    Returns:
        A ``datetime.date``.

    Raises:
        ValueError: If the leading token is not an ISO date.
    """
    return datetime.strptime(value.split(" ")[0], "%Y-%m-%d").date()


def trading_days_between(asof: str, event_date: str) -> int:
    """Count business days (Mon-Fri) from ``asof`` up to ``event_date``.

    Weekend-aware, holiday-agnostic. Same-day → 0; the count is the number of
    Mon-Fri boundaries crossed, so a Friday as-of date to the following Monday
    is 1 (only Monday is a business day strictly after Friday). A 7-calendar-day
    span that contains one weekend resolves to 5 business days.

    Caveat: market holidays are NOT excluded — pure stdlib, no holiday calendar.
    This slightly *overcounts* the true distance around holidays, which only ever
    makes the R3 gate marginally less aggressive (an event looks ~1 day farther
    away than it is). That is the safe direction for a fail-open risk gate.

    Args:
        asof: As-of date, "YYYY-MM-DD".
        event_date: Event date, "YYYY-MM-DD".

    Returns:
        Business-day count >= 0. Returns 0 when the event is on or before the
        as-of date (past events carry no forward risk).
    """
    start = _parse_date(asof)
    end = _parse_date(event_date)
    if end <= start:
        return 0
    count = 0
    cursor = start
    while cursor < end:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:  # Mon=0 .. Fri=4
            count += 1
    return count


def _is_macro_event(name: str) -> bool:
    """True if a macro release name matches a market-moving keyword.

    Args:
        name: Raw FMP economic-calendar event name.

    Returns:
        Whether ``name`` contains an FOMC / CPI / NFP-class keyword.
    """
    lowered = (name or "").lower()
    return any(kw in lowered for kw in MACRO_KEYWORDS)


def _normalize_timing(time_value: Optional[str]) -> Timing:
    """Normalize an FMP earnings ``time`` value to BMO/AMC/TAS.

    Args:
        time_value: Raw time value ("bmo", "amc", "dmh", "" ...).

    Returns:
        "BMO", "AMC", or "TAS" (time-as-scheduled / unknown).
    """
    if not time_value:
        return "TAS"
    lowered = time_value.lower()
    if lowered in ("bmo", "pre-market", "before market open"):
        return "BMO"
    if lowered in ("amc", "after-market", "after market close"):
        return "AMC"
    return "TAS"


def build_timeline(
    asof: str,
    earnings_rows: list[dict],
    macro_rows: list[dict],
    market: str,
) -> dict:
    """Merge raw earnings + macro rows into a normalized, sorted timeline.

    Pure: takes already-fetched rows (or stubbed ones) and produces the merged
    structure. Past-dated rows (event_date on/before ``asof``) are dropped.

    Args:
        asof: As-of date, "YYYY-MM-DD".
        earnings_rows: FMP /earning_calendar rows (keys: symbol, date, time,
            companyName). Treated as market-specific (US only in practice).
        macro_rows: FMP /economic_calendar rows (keys: date, event, impact,
            country, ...). Filtered to High-impact macro keywords.
        market: "US" or "KR" — stamped onto earnings events.

    Returns:
        {"by_ticker": {SYMBOL: [CatalystEvent...]}, "market_wide": [CatalystEvent...]}
        with each list sorted by (trading_days_until, event_date). Macro events
        are GLOBAL; earnings events carry the requested market.
    """
    market = market.upper()
    by_ticker: dict[str, list[CatalystEvent]] = {}

    for row in earnings_rows:
        symbol = row.get("symbol")
        event_date = row.get("date")
        if not symbol or not event_date:
            continue
        try:
            td = trading_days_between(asof, event_date)
        except (ValueError, AttributeError):
            continue
        if _parse_date(event_date) <= _parse_date(asof):
            continue
        ev = CatalystEvent(
            ticker=symbol,
            market=market,
            kind="earnings",
            name=row.get("companyName") or symbol,
            event_date=event_date,
            timing=_normalize_timing(row.get("time")),
            impact="High",  # an earnings report is a binary event by nature
            trading_days_until=td,
            source="fmp:earning_calendar",
        )
        by_ticker.setdefault(symbol, []).append(ev)

    market_wide: list[CatalystEvent] = []
    for row in macro_rows:
        name = row.get("event", "")
        event_date = row.get("date")
        impact = row.get("impact")
        if not event_date or impact != "High" or not _is_macro_event(name):
            continue
        try:
            td = trading_days_between(asof, event_date)
        except (ValueError, AttributeError):
            continue
        if _parse_date(event_date) <= _parse_date(asof):
            continue
        market_wide.append(
            CatalystEvent(
                ticker=None,
                market="GLOBAL",
                kind="macro",
                name=name,
                event_date=event_date,
                timing=None,
                impact="High",
                trading_days_until=td,
                source="fmp:economic_calendar",
            )
        )

    sort_key = lambda e: (e.trading_days_until, e.event_date)  # noqa: E731
    for events in by_ticker.values():
        events.sort(key=sort_key)
    market_wide.sort(key=sort_key)

    return {"by_ticker": by_ticker, "market_wide": market_wide}


def _evaluate_earnings(events: list[CatalystEvent]) -> dict:
    """Apply the per-ticker earnings half of RULE R3 to one ticker's events.

    Args:
        events: That ticker's forward earnings events (sorted nearest-first).

    Returns:
        {cap_label, confidence_trim, next_earnings_date, trading_days_until}.
    """
    verdict = {
        "cap_label": None,
        "confidence_trim": 0.0,
        "next_earnings_date": None,
        "trading_days_until": None,
    }
    if not events:
        return verdict
    nearest = events[0]
    td = nearest.trading_days_until
    verdict["next_earnings_date"] = nearest.event_date
    verdict["trading_days_until"] = td
    if td <= EARNINGS_WATCH_DAYS:
        verdict["cap_label"] = "WATCH"
    elif td <= EARNINGS_TRIM_DAYS:
        verdict["confidence_trim"] = EARNINGS_CONFIDENCE_TRIM
    # td > EARNINGS_TRIM_DAYS → none
    return verdict


def evaluate_gate(
    asof: str,
    tickers: list[str],
    market: str,
    fetch_earnings: Optional[Callable[[str, str, str], list[dict]]] = None,
    fetch_macro: Optional[Callable[[str, str], list[dict]]] = None,
) -> EventGate:
    """Compute the deterministic R3 event-risk gate for ``tickers``.

    Fetches the earnings + macro windows ONCE, builds the timeline, then derives:
      - per-ticker earnings cap/trim (US only — KR has no forward EPS feed);
      - a market-wide macro trim (US + KR) when a High-impact FOMC/CPI/NFP
        release lands within MACRO_TRIM_DAYS.

    R3 stacking contract (consumed by /expect Step 7):
      - A WATCH cap (here, from an imminent earnings report) wins the label,
        same as a R1/R2 WATCH cap.
      - Confidence trims take the *min* across applicable trims elsewhere; here
        each trim is independent (per-ticker earnings vs market macro) and both
        may apply to the same pick.
      - R3 never *raises* the BUY bar — it only caps the label or trims
        confidence.

    FAIL-OPEN: a missing FMP key or any fetch exception returns a neutral gate
    with ``gate_unavailable=True`` and empty caps/trims.

    Args:
        asof: As-of date, "YYYY-MM-DD".
        tickers: Candidate tickers (case-insensitive for US; KR codes kept as-is).
        market: "US" or "KR".
        fetch_earnings: Optional injected fetcher (asof_from, asof_to, market)
            → earnings rows. Defaults to the real FMP fetcher. Injected by tests.
        fetch_macro: Optional injected fetcher (asof_from, asof_to) → macro rows.
            Defaults to the real FMP fetcher. Injected by tests.

    Returns:
        An ``EventGate``.
    """
    market = market.upper()
    gate = EventGate(asof=asof, market=market)

    # Normalize tickers for lookup. US symbols are upper-cased to match the FMP
    # earnings feed; KR codes are 6-digit strings left untouched.
    norm_tickers = [t.upper() if market == "US" else t for t in tickers]

    api_key = os.environ.get("FMP_API_KEY", "")
    fetch_earnings = fetch_earnings or _fetch_earnings_window
    fetch_macro = fetch_macro or _fetch_macro_window

    # FAIL-OPEN guard 1: no key and no injected fetchers → neutral gate.
    if not api_key and fetch_earnings is _fetch_earnings_window:
        gate.gate_unavailable = True
        gate.notes.append("FMP_API_KEY not set — event gate unavailable (fail-open)")
        gate.by_ticker = {t: _neutral_ticker_verdict() for t in norm_tickers}
        return gate

    # Window: from as-of through the widest threshold (+ weekend slack so a
    # business-day window still captures calendar-dated rows).
    asof_dt = _parse_date(asof)
    window_to = (asof_dt + timedelta(days=EARNINGS_TRIM_DAYS + 4)).strftime("%Y-%m-%d")

    try:
        # KR has no FMP forward EPS feed → skip the earnings fetch entirely
        # (saves a quota call and avoids implying a cap we cannot compute).
        earnings_rows = (
            fetch_earnings(asof, window_to, market) if market == "US" else []
        )
        macro_rows = fetch_macro(asof, window_to)
    except Exception as e:  # FAIL-OPEN guard 2: any fetch error → neutral gate.
        logger.warning("Event gate fetch failed for %s: %s", market, e)
        gate.gate_unavailable = True
        gate.notes.append(f"event calendar fetch failed: {e} (fail-open)")
        gate.by_ticker = {t: _neutral_ticker_verdict() for t in norm_tickers}
        return gate

    timeline = build_timeline(asof, earnings_rows, macro_rows, market)

    # Per-ticker earnings verdict (US only). KR tickers get neutral verdicts.
    for t in norm_tickers:
        if market == "US":
            gate.by_ticker[t] = _evaluate_earnings(timeline["by_ticker"].get(t, []))
        else:
            gate.by_ticker[t] = _neutral_ticker_verdict()

    # Market-wide macro trim (US + KR). KR consumes the US macro stream.
    imminent_macro = [
        e for e in timeline["market_wide"] if e.trading_days_until <= MACRO_TRIM_DAYS
    ]
    if imminent_macro:
        gate.macro_trim = MACRO_CONFIDENCE_TRIM
        gate.macro_events = [
            {
                "name": e.name,
                "event_date": e.event_date,
                "trading_days_until": e.trading_days_until,
                "impact": e.impact,
            }
            for e in imminent_macro
        ]

    return gate


def _neutral_ticker_verdict() -> dict:
    """A per-ticker verdict with no cap and no trim (KR, or fail-open).

    Returns:
        {cap_label: None, confidence_trim: 0.0, next_earnings_date: None,
         trading_days_until: None}.
    """
    return {
        "cap_label": None,
        "confidence_trim": 0.0,
        "next_earnings_date": None,
        "trading_days_until": None,
    }


# ---------------------------------------------------------------------------
# Thin FMP fetch wrappers (network)
# ---------------------------------------------------------------------------


def _fetch_earnings_window(asof_from: str, asof_to: str, market: str) -> list[dict]:
    """Fetch US earnings rows from FMP /earning_calendar for a date window.

    Args:
        asof_from: Window start, "YYYY-MM-DD".
        asof_to: Window end, "YYYY-MM-DD".
        market: Market tag (only "US" is supported by this endpoint).

    Returns:
        Raw earnings rows (possibly empty). Raises on HTTP/transport error so
        ``evaluate_gate``'s fail-open guard can catch it.
    """
    api_key = os.environ.get("FMP_API_KEY", "")
    resp = httpx.get(
        f"{FMP_BASE_URL}/earning_calendar",
        params={"from": asof_from, "to": asof_to, "apikey": api_key},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []
    return data


def _fetch_macro_window(asof_from: str, asof_to: str) -> list[dict]:
    """Fetch macro rows from FMP /economic_calendar for a date window.

    Args:
        asof_from: Window start, "YYYY-MM-DD".
        asof_to: Window end, "YYYY-MM-DD".

    Returns:
        Raw economic-calendar rows (possibly empty). Raises on HTTP/transport
        error so ``evaluate_gate``'s fail-open guard can catch it.
    """
    api_key = os.environ.get("FMP_API_KEY", "")
    resp = httpx.get(
        f"{FMP_BASE_URL}/economic_calendar",
        params={"from": asof_from, "to": asof_to, "apikey": api_key},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []
    return data
