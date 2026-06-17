"""Sector-rotation relative-strength screener (pure, never-raise).

Turns a sector ETF's price structure — relative to the market benchmark — plus
its constituent breadth into a prescriptive rotation verdict
(FAVOR / ROTATING_IN / ROTATING_OUT / AVOID / NEUTRAL) and a lifecycle stage
(EARLY / MID / LATE). This is the machine-readable counterpart to the
narrative ``sector-analyst`` skill: discovery consumes the JSON it produces to
bias candidate ranking toward sectors that are rotating *in* and away from ones
that are rotating *out*.

The three scoring axes (all reuse :class:`indicators.HorizonMetrics`):

  1. **Relative strength (RS)** — ``sector.return_1m - benchmark.return_1m`` for
     the 1-month axis, plus a 3-month proxy from a 63-bar close return spread.
  2. **Breadth** — fraction of the sector's constituent basket whose latest
     close is above its MA20 *and* whose 1-month return beats the benchmark.
  3. **Stage** — EARLY / MID / LATE from RSI, distance to MA50, and the reused
     overextension classifier.

Design contract (mirrors ``regime.py``): every function is pure (no network, no
DB) and the public entry points never raise. A missing benchmark floors every
sector to a NEUTRAL verdict (we must not certify rotation against an unknown
market), and a sector with no usable ETF metrics is reported NEUTRAL too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from indicators import HorizonMetrics

# --- Stage thresholds (explicit so the verdict is auditable and tunable) ---- #
# EARLY = healthy, not-yet-extended leadership: a constructive RSI band, price
# hugging the MA50, no overextension, and positive 1m relative strength.
STAGE_RSI_EARLY_LOW = 45.0
STAGE_RSI_EARLY_HIGH = 65.0
STAGE_MA50_BAND = 0.08  # within +/-8% of MA50
# LATE = stretched/blow-off leadership that is more likely to mean-revert.
STAGE_RSI_LATE = 70.0
STAGE_MA50_LATE_ABOVE = 0.15  # price >15% above MA50

# --- Verdict thresholds ----------------------------------------------------- #
BREADTH_FAVOR = 0.5  # >=50% of the basket leading the benchmark
BREADTH_AVOID = 0.4  # <40% leading -> the sector is thinning out
BREADTH_WEAK = 0.4  # "weak breadth" cutoff for the LATE rotate-out rule

# --- Score normalization ---------------------------------------------------- #
# rs_1m is a return spread (decimal). Map [-RS_CLAMP, +RS_CLAMP] -> [0, 1] so a
# +/-10% monthly outperformance saturates the RS axis. Chosen to keep the axis
# responsive across the realistic monthly sector-vs-benchmark dispersion.
RS_CLAMP = 0.10
STAGE_SCORE = {"EARLY": 1.0, "MID": 0.6, "LATE": 0.2}
WEIGHT_RS = 0.5
WEIGHT_BREADTH = 0.3
WEIGHT_STAGE = 0.2

VALID_VERDICTS = ("FAVOR", "ROTATING_IN", "ROTATING_OUT", "AVOID", "NEUTRAL")
VALID_STAGES = ("EARLY", "MID", "LATE")

# ---------------------------------------------------------------------------
# US constituent baskets — a small static leader list per GICS sector. These
# are the liquid bellwethers used to compute breadth; not an exhaustive
# membership. KR baskets ship in data/kr_sector_map.csv instead.
# ---------------------------------------------------------------------------
US_SECTOR_ETFS: dict[str, str] = {
    "Technology": "XLK",
    "Financials": "XLF",
    "Energy": "XLE",
    "Healthcare": "XLV",
    "Industrials": "XLI",
    "Consumer Staples": "XLP",
    "Consumer Discretionary": "XLY",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Materials": "XLB",
    "Communication Services": "XLC",
}

US_SECTOR_CONSTITUENTS: dict[str, list[str]] = {
    "Technology": ["AAPL", "MSFT", "NVDA", "AVGO", "AMD", "CRM", "ORCL", "ADBE"],
    "Financials": ["JPM", "BAC", "WFC", "GS", "MS", "C", "BLK"],
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG", "MPC"],
    "Healthcare": ["UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "TMO"],
    "Industrials": ["CAT", "GE", "HON", "UNP", "BA", "RTX", "DE"],
    "Consumer Staples": ["PG", "KO", "PEP", "COST", "WMT", "MDLZ"],
    "Consumer Discretionary": ["AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX"],
    "Utilities": ["NEE", "DUK", "SO", "D", "AEP", "EXC"],
    "Real Estate": ["PLD", "AMT", "EQIX", "PSA", "O", "SPG"],
    "Materials": ["LIN", "SHW", "APD", "FCX", "NEM", "ECL"],
    "Communication Services": ["GOOGL", "META", "NFLX", "DIS", "TMUS", "VZ"],
}

# The US benchmark all sector ETFs are measured against (S&P 500 proxy).
US_BENCHMARK = "SPY"


@dataclass
class SectorVerdict:
    """Sector-rotation relative-strength verdict for one sector.

    Args:
        sector: GICS sector name (US) or the KR CSV sector label.
        benchmark: Benchmark ticker the RS was measured against (e.g. SPY,
            069500). Empty string when the benchmark was unavailable.
        rs_1m: 1-month relative strength: ``sector.return_1m -
            benchmark.return_1m`` (decimal). ``None`` when either return is
            unavailable.
        rs_3m: 3-month relative strength proxy from a 63-bar close-return
            spread. ``None`` when unavailable.
        breadth_pct: Fraction (0..1) of the constituent basket whose latest
            close is above its MA20 *and* whose 1-month return beats the
            benchmark. ``None`` when no constituent metrics were available.
        stage: Lifecycle stage — "EARLY" | "MID" | "LATE".
        verdict: Prescriptive rotation call — one of ``VALID_VERDICTS``.
        score: 0..100 composite (0.5*RS + 0.3*breadth + 0.2*stage). A
            benchmark-missing NEUTRAL floor scores 50.0.
        notes: Short human-readable rationale strings.
    """

    sector: str
    benchmark: str
    rs_1m: Optional[float]
    rs_3m: Optional[float]
    breadth_pct: Optional[float]
    stage: str
    verdict: str
    score: float
    notes: list[str] = field(default_factory=list)


def _return_63bar(closes: list[float]) -> Optional[float]:
    """63-bar (~3-month) close-to-close return as a decimal.

    Args:
        closes: Closing prices, oldest-first.

    Returns:
        ``(closes[-1] / closes[-64] - 1)``, or None if there are fewer than
        64 closes or the base price is non-positive.
    """
    if len(closes) < 64:
        return None
    past = closes[-64]
    if past <= 0:
        return None
    return closes[-1] / past - 1.0


def classify_stage(metrics: HorizonMetrics, rs_1m: Optional[float]) -> str:
    """Classify a sector's lifecycle stage from its ETF price structure.

    EARLY requires constructive, not-yet-extended leadership; LATE flags
    stretched/blow-off conditions; everything else is MID.

    Stage rules (in priority order — LATE wins over EARLY when both could
    fire, because an overextended sector is the more actionable signal):
      - LATE  = overextension ELEVATED/EXTREME OR price >15% above MA50 OR
                rsi14 > 70.
      - EARLY = rsi14 in [45, 65] AND price within +/-8% of MA50 AND
                overextension NONE AND rs_1m > 0.
      - MID   = otherwise.

    Args:
        metrics: HorizonMetrics for the sector ETF.
        rs_1m: 1-month relative strength (decimal) or None.

    Returns:
        "EARLY", "MID", or "LATE". Falls back to "MID" when the inputs needed
        to decide EARLY/LATE are missing (a conservative neutral middle).
    """
    price = metrics.current_price
    ma50 = metrics.ma50
    rsi = metrics.rsi14
    # Reuse the overextension level already computed on the HorizonMetrics
    # (RSI14 + price-vs-MA20). compute_horizon_metrics always populates it.
    overext = metrics.overextension_level or "NONE"

    # Distance from MA50 (decimal), or None when MA50 is unavailable.
    ma50_dist: Optional[float] = None
    if ma50 is not None and ma50 > 0:
        ma50_dist = price / ma50 - 1.0

    # LATE: any stretched/blow-off condition.
    if overext in ("ELEVATED", "EXTREME"):
        return "LATE"
    if ma50_dist is not None and ma50_dist > STAGE_MA50_LATE_ABOVE:
        return "LATE"
    if rsi is not None and rsi > STAGE_RSI_LATE:
        return "LATE"

    # EARLY: constructive, not-yet-extended leadership.
    early = (
        rsi is not None
        and STAGE_RSI_EARLY_LOW <= rsi <= STAGE_RSI_EARLY_HIGH
        and ma50_dist is not None
        and abs(ma50_dist) <= STAGE_MA50_BAND
        and overext == "NONE"
        and rs_1m is not None
        and rs_1m > 0
    )
    if early:
        return "EARLY"

    return "MID"


def _classify_verdict(
    rs_1m: Optional[float],
    rs_3m: Optional[float],
    breadth: Optional[float],
    stage: str,
) -> str:
    """Map the three axes to a prescriptive rotation verdict.

    Verdict rules (first match wins, ordered so the strongest signals take
    precedence):
      - FAVOR        = rs_1m > 0 AND breadth >= 0.5 AND stage in (EARLY, MID).
      - ROTATING_IN  = rs_1m > 0 AND rs_3m <= 0 AND stage == EARLY.
      - ROTATING_OUT = (rs_1m < 0 AND rs_3m > 0) OR (stage == LATE with weak
                       breadth).
      - AVOID        = rs_1m < 0 AND breadth < 0.4.
      - NEUTRAL      = otherwise (or when rs_1m is unknown).

    Args:
        rs_1m: 1-month relative strength (decimal) or None.
        rs_3m: 3-month relative strength (decimal) or None.
        breadth: Constituent breadth fraction (0..1) or None.
        stage: Lifecycle stage from :func:`classify_stage`.

    Returns:
        One of ``VALID_VERDICTS``.
    """
    # Without 1-month RS we cannot certify any directional rotation call.
    if rs_1m is None:
        return "NEUTRAL"

    b = breadth if breadth is not None else 0.0

    # FAVOR: leading the market with broad participation in a constructive stage.
    if rs_1m > 0 and b >= BREADTH_FAVOR and stage in ("EARLY", "MID"):
        return "FAVOR"

    # ROTATING_IN: fresh leadership — turning up on 1m while still flat/down on 3m.
    if rs_1m > 0 and rs_3m is not None and rs_3m <= 0 and stage == "EARLY":
        return "ROTATING_IN"

    # ROTATING_OUT: momentum rolling over, or a late-stage sector thinning out.
    if rs_1m < 0 and rs_3m is not None and rs_3m > 0:
        return "ROTATING_OUT"
    if stage == "LATE" and b < BREADTH_WEAK:
        return "ROTATING_OUT"

    # AVOID: lagging the market with poor participation.
    if rs_1m < 0 and b < BREADTH_AVOID:
        return "AVOID"

    return "NEUTRAL"


def _normalize_score(
    rs_1m: Optional[float], breadth: Optional[float], stage: str
) -> float:
    """Blend the three axes into a 0..100 composite.

    RS is clamped to ``[-RS_CLAMP, +RS_CLAMP]`` then linearly mapped to [0, 1];
    breadth is already a [0, 1] fraction; stage maps via ``STAGE_SCORE``.
    Missing RS/breadth contribute a neutral 0.5 / 0.0 respectively so the score
    degrades gracefully rather than spiking.

    Args:
        rs_1m: 1-month relative strength (decimal) or None.
        breadth: Constituent breadth fraction (0..1) or None.
        stage: Lifecycle stage.

    Returns:
        Composite score in [0, 100].
    """
    if rs_1m is None:
        rs_norm = 0.5
    else:
        clamped = max(-RS_CLAMP, min(RS_CLAMP, rs_1m))
        rs_norm = (clamped + RS_CLAMP) / (2 * RS_CLAMP)
    breadth_norm = breadth if breadth is not None else 0.0
    stage_norm = STAGE_SCORE.get(stage, 0.6)
    blended = (
        WEIGHT_RS * rs_norm + WEIGHT_BREADTH * breadth_norm + WEIGHT_STAGE * stage_norm
    )
    return round(100.0 * blended, 1)


def compute_breadth(
    constituent_metrics: list[HorizonMetrics],
    benchmark_return_1m: Optional[float],
) -> Optional[float]:
    """Fraction of the basket leading the benchmark on price *and* momentum.

    A constituent "leads" when its latest close is above its MA20 (in an
    uptrend) AND its 1-month return beats the benchmark's. Constituents whose
    MA20 or 1-month return is unavailable are skipped from the denominator so a
    short-history name can't silently drag breadth toward zero.

    Args:
        constituent_metrics: HorizonMetrics for each basket member.
        benchmark_return_1m: Benchmark's 1-month return (decimal), or None.

    Returns:
        Breadth fraction in [0, 1], or None when no constituent had both a
        usable MA20 and a usable 1-month return (or the benchmark return is
        unknown).
    """
    if benchmark_return_1m is None:
        return None
    leading = 0
    counted = 0
    for m in constituent_metrics:
        if m.ma20 is None or m.return_1m is None:
            continue
        counted += 1
        if m.current_price > m.ma20 and m.return_1m > benchmark_return_1m:
            leading += 1
    if counted == 0:
        return None
    return leading / counted


def compute_sector_verdict(
    sector: str,
    etf_metrics: Optional[HorizonMetrics],
    etf_closes: Optional[list[float]],
    benchmark_metrics: Optional[HorizonMetrics],
    benchmark_closes: Optional[list[float]],
    constituent_metrics: list[HorizonMetrics],
) -> SectorVerdict:
    """Build the full SectorVerdict for one sector (never raises).

    Computes the three axes (RS 1m/3m, breadth, stage), maps them to a verdict
    and a 0..100 score. A missing benchmark floors the result to NEUTRAL with a
    score of 50.0 (we will not certify rotation against an unknown market). A
    missing sector ETF likewise yields a NEUTRAL floor.

    Args:
        sector: Sector label.
        etf_metrics: HorizonMetrics for the sector ETF, or None when no data.
        etf_closes: The sector ETF's closes (oldest-first) for the 3m proxy, or
            None.
        benchmark_metrics: HorizonMetrics for the market benchmark, or None.
        benchmark_closes: The benchmark's closes for the 3m proxy, or None.
        constituent_metrics: HorizonMetrics for each constituent (may be empty).

    Returns:
        A populated SectorVerdict; every field is filled even on the degraded
        paths.
    """
    benchmark_ticker = benchmark_metrics.ticker if benchmark_metrics else ""

    # NEUTRAL floor #1: no benchmark to measure relative strength against.
    if benchmark_metrics is None:
        return SectorVerdict(
            sector=sector,
            benchmark="",
            rs_1m=None,
            rs_3m=None,
            breadth_pct=None,
            stage="MID",
            verdict="NEUTRAL",
            score=50.0,
            notes=["no benchmark data — floored to NEUTRAL"],
        )

    # NEUTRAL floor #2: no sector ETF metrics — nothing to score.
    if etf_metrics is None:
        return SectorVerdict(
            sector=sector,
            benchmark=benchmark_ticker,
            rs_1m=None,
            rs_3m=None,
            breadth_pct=None,
            stage="MID",
            verdict="NEUTRAL",
            score=50.0,
            notes=["no sector ETF data — floored to NEUTRAL"],
        )

    notes: list[str] = []

    # Axis 1: relative strength (1m from HorizonMetrics, 3m from 63-bar closes).
    rs_1m: Optional[float] = None
    if etf_metrics.return_1m is not None and benchmark_metrics.return_1m is not None:
        rs_1m = etf_metrics.return_1m - benchmark_metrics.return_1m
        notes.append(f"RS_1m {rs_1m:+.1%} vs {benchmark_ticker}")

    rs_3m: Optional[float] = None
    etf_3m = _return_63bar(etf_closes) if etf_closes else None
    bench_3m = _return_63bar(benchmark_closes) if benchmark_closes else None
    if etf_3m is not None and bench_3m is not None:
        rs_3m = etf_3m - bench_3m
        notes.append(f"RS_3m {rs_3m:+.1%}")

    # Axis 2: breadth across the constituent basket.
    breadth = compute_breadth(constituent_metrics, benchmark_metrics.return_1m)
    if breadth is not None:
        notes.append(f"breadth {breadth:.0%}")
    elif not constituent_metrics:
        notes.append("no constituents — breadth N/A")

    # Axis 3: lifecycle stage.
    stage = classify_stage(etf_metrics, rs_1m)

    verdict = _classify_verdict(rs_1m, rs_3m, breadth, stage)
    score = _normalize_score(rs_1m, breadth, stage)

    return SectorVerdict(
        sector=sector,
        benchmark=benchmark_ticker,
        rs_1m=rs_1m,
        rs_3m=rs_3m,
        breadth_pct=breadth,
        stage=stage,
        verdict=verdict,
        score=score,
        notes=notes,
    )


def rank_sectors(verdicts: list[SectorVerdict]) -> list[SectorVerdict]:
    """Sort verdicts by descending score (strongest rotation candidate first).

    Args:
        verdicts: SectorVerdicts in any order.

    Returns:
        A new list sorted by ``score`` descending. Ties keep input order
        (Python's sort is stable).
    """
    return sorted(verdicts, key=lambda v: v.score, reverse=True)
