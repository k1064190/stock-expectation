"""Catalyst event timeline + R3 event-risk gate.

Unifies two FMP forward calendars into one normalized timeline and turns it
into a deterministic confidence gate (RULE R3) that /expect and daily-briefing
consult before issuing new BUY calls:

  - Earnings calendar (FMP /stable/earnings-calendar, US-listed only) →
    per-ticker binary-event risk near the report date. When the FMP fetch
    fails (the legacy /api/v3 endpoints now 403 for newer keys), a keyless
    yfinance per-ticker fallback supplies the next earnings dates.
  - Economic calendar (FMP /stable/economic-calendar) → market-wide macro
    shocks (FOMC / CPI / NFP). KR has no forward EPS feed on FMP, so KR
    consumes the US macro stream (transmitted via FX / SOXL) and gets no
    per-ticker earnings cap. No keyless macro fallback exists — a macro
    outage is flagged via ``macro_available=False`` + ``notes``.

Design rules (mirrors the rest of mcp-market-data):
  - FAIL-OPEN, but VISIBLY: the gate never raises and never blocks the cron.
    ``gate_unavailable=True`` only when NO source produced data; a partial
    outage keeps the gate live and is recorded in ``earnings_source`` /
    ``macro_available`` / ``notes`` so a dead feed can never be confused with
    "no imminent events".
  - The earnings + macro windows are fetched ONCE per ``evaluate_gate`` call
    (not once per ticker) to protect the 250/day FMP free-tier quota. The
    yfinance fallback is per-ticker but only runs over the candidate list.

The pure core (``trading_days_between``, ``build_timeline``, ``evaluate_gate``
with injected fetchers) is stdlib-only and fully testable offline.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Literal, Optional

import httpx

logger = logging.getLogger(__name__)

# The legacy /api/v3 calendar endpoints return 403 Forbidden for keys issued
# after FMP's 2025 plan migration; /stable is the current API surface.
FMP_BASE_URL = "https://financialmodelingprep.com/stable"

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
# FMP economic-calendar event names vary ("CPI", "Consumer Price Index",
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

# The R3 macro stream is US-only by design: FOMC/CPI/NFP transmit globally
# (via FX / index futures) so KR consumes the same stream, but a non-US CPI/NFP
# row (e.g. UK or EU) must NOT trigger macro_trim. FMP's economic-calendar
# country field uses "US"; we also accept the long form defensively.
MACRO_COUNTRIES = ("us", "united states")

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
        source: Provenance string, e.g. "fmp:earnings-calendar" or
            "yfinance:calendar" (keyless earnings fallback).
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
        gate_unavailable: True only when NO event source produced data — both
            the earnings side (FMP + yfinance fallback, US) and the macro side
            failed (FAIL-OPEN). Callers must treat caps/trims as advisory-zero
            in that case and proceed. Partial outages keep this False and are
            flagged via ``earnings_source`` / ``macro_available`` / ``notes``.
        notes: Human-readable annotations (e.g. which source failed and why).
        earnings_source: Which feed produced the per-ticker earnings verdicts:
            "fmp" (/stable/earnings-calendar), "yfinance" (keyless fallback),
            or None (earnings side unavailable — or KR, which has no forward
            EPS feed by design).
        macro_available: True when the FMP economic calendar was fetched
            successfully. False means ``macro_trim`` could NOT be computed
            (e.g. the endpoint needs a paid plan) — not that no macro event is
            imminent.
    """

    asof: str
    market: str
    by_ticker: dict[str, dict] = field(default_factory=dict)
    macro_trim: float = 0.0
    macro_events: list[dict] = field(default_factory=list)
    gate_unavailable: bool = False
    notes: list[str] = field(default_factory=list)
    earnings_source: Optional[str] = None
    macro_available: bool = False


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


def _redact_key(msg: str) -> str:
    """Redact any ``apikey=...`` query value from an error message.

    httpx exception strings embed the full request URL — without redaction, a
    failed FMP fetch would leak the API key into ``notes`` (which reaches the
    gate's JSON output, logs, and LLM prompts).

    Args:
        msg: Raw exception string.

    Returns:
        ``msg`` with any ``apikey=`` value replaced by ``apikey=***``.
    """
    return re.sub(r"(apikey=)[^&\s'\"]+", r"\1***", msg)


def _is_macro_event(name: str) -> bool:
    """True if a macro release name matches a market-moving keyword.

    Args:
        name: Raw FMP economic-calendar event name.

    Returns:
        Whether ``name`` contains an FOMC / CPI / NFP-class keyword.
    """
    lowered = (name or "").lower()
    return any(kw in lowered for kw in MACRO_KEYWORDS)


def _is_us_macro_country(country: Optional[str]) -> bool:
    """True if a macro row's country is the US (the only stream R3 acts on).

    Args:
        country: Raw FMP economic-calendar ``country`` field ("US",
            "United States", or a non-US country like "GB"/"EU").

    Returns:
        Whether ``country`` matches a US identifier ("US"/"United States",
        case-insensitive). Non-US rows are ignored so a foreign CPI/NFP release
        cannot trigger ``macro_trim``.
    """
    return (country or "").strip().lower() in MACRO_COUNTRIES


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
    structure. Strictly-past rows (event_date before ``asof``) are dropped;
    same-day rows (event_date == ``asof``, td == 0) are kept — they still carry
    binary risk.

    Args:
        asof: As-of date, "YYYY-MM-DD".
        earnings_rows: Earnings rows (keys: symbol, date; optional time,
            companyName, source) from FMP /stable/earnings-calendar or the
            yfinance fallback. Treated as market-specific (US only in practice).
        macro_rows: FMP /stable/economic-calendar rows (keys: date, event, impact,
            country, ...). Filtered to High-impact, US-country macro keywords.
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
        # Drop only strictly-past rows. An event ON the as-of date (td == 0,
        # e.g. an earnings report TODAY) still carries binary risk and must
        # reach the WATCH cap.
        if _parse_date(event_date) < _parse_date(asof):
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
            source=row.get("source") or "fmp:earnings-calendar",
        )
        by_ticker.setdefault(symbol, []).append(ev)

    market_wide: list[CatalystEvent] = []
    for row in macro_rows:
        name = row.get("event", "")
        event_date = row.get("date")
        impact = row.get("impact")
        country = row.get("country")
        if (
            not event_date
            or impact != "High"
            or not _is_macro_event(name)
            or not _is_us_macro_country(country)
        ):
            continue
        try:
            td = trading_days_between(asof, event_date)
        except (ValueError, AttributeError):
            continue
        # Drop only strictly-past rows. A macro release ON the as-of date
        # (td == 0, e.g. an FOMC decision TODAY) still drives macro_trim.
        if _parse_date(event_date) < _parse_date(asof):
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
                source="fmp:economic-calendar",
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
    fetch_earnings_fallback: Optional[
        Callable[[list[str], str], tuple[list[dict], list[str]]]
    ] = None,
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

    FAIL-OPEN, visibly: the earnings and macro sides fail independently. A
    failed FMP earnings fetch (US) falls back to keyless yfinance per-ticker
    earnings dates; ``gate_unavailable=True`` only when NO side produced data.
    Every degradation lands in ``notes`` and in ``earnings_source`` /
    ``macro_available`` so a dead feed cannot masquerade as "no events".

    Args:
        asof: As-of date, "YYYY-MM-DD".
        tickers: Candidate tickers (case-insensitive for US; KR codes kept as-is).
        market: "US" or "KR".
        fetch_earnings: Optional injected fetcher (asof_from, asof_to, market)
            → earnings rows. Defaults to the real FMP fetcher. Injected by tests.
        fetch_macro: Optional injected fetcher (asof_from, asof_to) → macro rows.
            Defaults to the real FMP fetcher. Injected by tests.
        fetch_earnings_fallback: Optional injected fallback fetcher
            (tickers, asof) → (earnings rows, failed tickers), used when the
            primary earnings fetch fails. Failed tickers are flagged in
            ``notes``. Defaults to the real yfinance fetcher. Injected by
            tests.

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
    fetch_earnings_fallback = fetch_earnings_fallback or _fetch_earnings_fallback_yf

    # Window: from as-of through the widest threshold (+ weekend slack so a
    # business-day window still captures calendar-dated rows).
    asof_dt = _parse_date(asof)
    window_to = (asof_dt + timedelta(days=EARNINGS_TRIM_DAYS + 4)).strftime("%Y-%m-%d")

    # --- Earnings side (US only — KR has no forward EPS feed, so the fetch is
    # skipped entirely: saves a quota call and avoids implying a cap we cannot
    # compute). FMP is primary; yfinance is the keyless fallback. ``None`` here
    # means "not yet fetched" (vs ``[]`` = fetched, no events).
    earnings_rows: Optional[list[dict]] = None
    if market == "US":
        # Skip the real FMP fetcher when the key is missing — it cannot
        # succeed. Injected (test) fetchers run regardless.
        if api_key or fetch_earnings is not _fetch_earnings_window:
            try:
                earnings_rows = fetch_earnings(asof, window_to, market)
                gate.earnings_source = "fmp"
            except Exception as e:
                err = _redact_key(str(e))
                logger.warning(
                    "FMP earnings calendar fetch failed for %s: %s", market, err
                )
                gate.notes.append(
                    f"FMP earnings calendar fetch failed: {err} — trying yfinance fallback"
                )
        else:
            gate.notes.append("FMP_API_KEY not set — trying yfinance earnings fallback")
        if earnings_rows is None:
            try:
                earnings_rows, yf_failed = fetch_earnings_fallback(norm_tickers, asof)
                gate.earnings_source = "yfinance"
                # A per-ticker lookup FAILURE is not "no scheduled earnings" —
                # flag each so a partial provider outage stays visible.
                for ft in yf_failed:
                    gate.notes.append(
                        f"yfinance lookup failed for {ft} — earnings risk unknown"
                    )
            except Exception as e:
                logger.warning("yfinance earnings fallback failed: %s", e)
                gate.notes.append(
                    f"yfinance earnings fallback failed: {e} — "
                    "earnings caps unavailable (fail-open)"
                )
    if earnings_rows is None:
        earnings_rows = []

    # --- Macro side (FMP only — there is no keyless macro-calendar fallback).
    macro_rows: list[dict] = []
    if api_key or fetch_macro is not _fetch_macro_window:
        try:
            macro_rows = fetch_macro(asof, window_to)
            gate.macro_available = True
        except Exception as e:
            err = _redact_key(str(e))
            logger.warning("Macro calendar fetch failed for %s: %s", market, err)
            gate.notes.append(
                f"macro calendar fetch failed: {err} — macro trim unavailable (fail-open)"
            )
    else:
        gate.notes.append(
            "FMP_API_KEY not set — macro calendar unavailable (fail-open)"
        )

    # FAIL-OPEN: fully unavailable only when NO side produced data. A partial
    # outage keeps the gate live — the dead side is already flagged in notes.
    if market == "US":
        gate.gate_unavailable = (
            gate.earnings_source is None and not gate.macro_available
        )
    else:  # KR is macro-only by design; its earnings side is not an outage.
        gate.gate_unavailable = not gate.macro_available
    if gate.gate_unavailable:
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
# Thin fetch wrappers (network: FMP primary + yfinance earnings fallback)
# ---------------------------------------------------------------------------


def _fetch_earnings_window(asof_from: str, asof_to: str, market: str) -> list[dict]:
    """Fetch US earnings rows from FMP /stable/earnings-calendar for a window.

    The stable rows carry ``symbol`` + ``date`` like the legacy v3 endpoint but
    no ``time``/``companyName`` fields — timing normalizes to "TAS" and the
    name falls back to the symbol downstream.

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
        f"{FMP_BASE_URL}/earnings-calendar",
        params={"from": asof_from, "to": asof_to, "apikey": api_key},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []
    return data


def _fetch_macro_window(asof_from: str, asof_to: str) -> list[dict]:
    """Fetch macro rows from FMP /stable/economic-calendar for a date window.

    NOTE: this endpoint returns 402 on FMP plans without economic-calendar
    access — ``evaluate_gate`` then flags ``macro_available=False`` instead of
    silently zeroing the macro trim.

    Args:
        asof_from: Window start, "YYYY-MM-DD".
        asof_to: Window end, "YYYY-MM-DD".

    Returns:
        Raw economic-calendar rows (possibly empty). Raises on HTTP/transport
        error so ``evaluate_gate``'s fail-open guard can catch it.
    """
    api_key = os.environ.get("FMP_API_KEY", "")
    resp = httpx.get(
        f"{FMP_BASE_URL}/economic-calendar",
        params={"from": asof_from, "to": asof_to, "apikey": api_key},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []
    return data


def _fetch_earnings_fallback_yf(
    tickers: list[str], asof: str
) -> tuple[list[dict], list[str]]:
    """Keyless earnings-date fallback via yfinance ``Ticker.calendar``.

    Used when the FMP earnings-calendar fetch fails (e.g. the key lacks access
    and returns 403). Looks up the next scheduled earnings date per candidate
    ticker — one yfinance call per ticker, over the gate's candidate list only.

    Dates strictly before ``asof`` are skipped BEFORE selecting the nearest
    date: yfinance sometimes reports the last, already-past earnings date (or
    an estimated multi-date window whose lower bound is past), and an
    unfiltered ``min(dates)`` would let it shadow a still-imminent future
    date. A same-day date (== ``asof``) is kept — it still carries binary risk
    (td == 0 → WATCH cap), matching the FMP feed.

    Args:
        tickers: US candidate tickers to look up.
        asof: As-of date, "YYYY-MM-DD" — dates before this are ignored.

    Returns:
        ``(rows, failed)`` where ``rows`` are FMP-shaped earnings rows
        ``{"symbol", "date", "time": None, "source": "yfinance:calendar"}``
        and ``failed`` lists tickers whose lookup ERRORED (earnings risk
        unknown — distinct from "no scheduled earnings", which just omits the
        ticker from ``rows``). Callers must surface ``failed`` so a partial
        provider outage stays visible.

    Raises:
        RuntimeError: When every per-ticker lookup errored (total outage), so
            ``evaluate_gate`` marks the earnings side unavailable instead of
            mistaking the empty result for "no imminent earnings".
    """
    import yfinance as yf

    asof_d = _parse_date(asof)
    rows: list[dict] = []
    failed: list[str] = []
    for t in tickers:
        try:
            cal = yf.Ticker(t).calendar or {}
            dates = [
                d.date() if isinstance(d, datetime) else d
                for d in (cal.get("Earnings Date") or [])
            ]
            upcoming = [d for d in dates if d >= asof_d]
            if not upcoming:
                continue
            nearest = min(upcoming)
            rows.append(
                {
                    "symbol": t,
                    "date": nearest.isoformat(),
                    "time": None,
                    "source": "yfinance:calendar",
                }
            )
        except Exception as e:  # per-ticker best-effort — record, don't abort
            failed.append(t)
            logger.warning("yfinance earnings lookup failed for %s: %s", t, e)
    if tickers and len(failed) == len(tickers):
        raise RuntimeError(
            f"yfinance earnings fallback failed for all {len(tickers)} tickers"
        )
    return rows, failed
