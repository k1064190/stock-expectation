"""Deterministic market-regime classification for the hard risk-off gate.

The June 2026 drawdown exposed a regime blind spot: ``/expect`` kept issuing BULL
calls straight through a correction (140 BULL in 6/1–6/7 resolved at a 4.3% win
rate). This module turns observable index price structure into a transparent
RISK_ON / NEUTRAL / RISK_OFF verdict so the skills can hard-gate new BULL
issuance when the market is risk-off, instead of relying on an LLM to notice.

Pure functions — no network, no DB — so the logic is unit-testable. The CLI
command (``stock-cli regime``) supplies the index bars.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Optional

from indicators import HorizonMetrics

# --- Thresholds (explicit so the verdict is auditable and tunable) --------- #
# Drawdown from the 52-week high: a >10% pullback is correction territory.
DRAWDOWN_CAUTION = -0.05
DRAWDOWN_RISK_OFF = -0.10
# 1-month index return: sustained negative momentum.
RETURN_1M_CAUTION = -0.03
RETURN_1M_RISK_OFF = -0.08
# Annualized realized volatility of daily returns (20-day window). ~16% is the
# long-run equity norm; >25% is an elevated/turbulent regime.
VOL_CAUTION = 0.20
VOL_RISK_OFF = 0.30
# Risk-off score cutoffs (sum of the component points below).
SCORE_RISK_OFF = 4
SCORE_NEUTRAL = 2

VALID_LABELS = ("RISK_ON", "NEUTRAL", "RISK_OFF")


@dataclass
class RegimeVerdict:
    """Market-regime classification derived from one index's price structure.

    Args:
        market: "US" or "KR".
        index_ticker: The index/ETF proxy used (e.g. SPY, 069500).
        label: RISK_ON, NEUTRAL, or RISK_OFF.
        score: Sum of risk-off points (higher = more risk-off). 0..8.
        components: Per-signal point contributions, for transparency.
        realized_vol_annual: Annualized 20-day realized volatility, or None.
        notes: Short human-readable rationale strings.
        proxy_scores: When this verdict is an aggregate over several index
            proxies (e.g. SPY + QQQ), maps each proxy ticker to its risk-off
            score. Empty for a single-proxy verdict.
    """

    market: str
    index_ticker: str
    label: str
    score: int
    components: dict[str, int]
    realized_vol_annual: Optional[float]
    notes: list[str] = field(default_factory=list)
    proxy_scores: dict[str, int] = field(default_factory=dict)


def aggregate_regime(verdicts: list[RegimeVerdict]) -> RegimeVerdict:
    """Combine several index-proxy verdicts into the worst (most risk-off) one.

    The June 2026 drawdown was tech-led: SPY stayed calm while QQQ and the
    growth names broke down. Gating on the broad index alone would miss that, so
    the US gate evaluates both SPY and QQQ and takes the more risk-off verdict.

    Selection is by **label severity first, then score**. Score alone is not
    enough: a proxy floored to NEUTRAL for insufficient history keeps score 0,
    so a different proxy at score 1 (RISK_ON) would otherwise win and let the
    gate certify RISK_ON against a proxy that explicitly refused to. Severity
    ordering preserves that floor. Ties resolve to input order (max() is stable).

    Args:
        verdicts: One RegimeVerdict per index proxy (must be non-empty).

    Returns:
        The most risk-off verdict (by label severity, then score), annotated
        with every proxy's score in ``proxy_scores``.

    Raises:
        ValueError: If ``verdicts`` is empty.
    """
    if not verdicts:
        raise ValueError("aggregate_regime requires at least one verdict")
    severity = {"RISK_OFF": 2, "NEUTRAL": 1, "RISK_ON": 0}
    worst = max(verdicts, key=lambda v: (severity.get(v.label, 0), v.score))
    # Return a copy so the input verdicts are never mutated (they may be logged
    # or reused by the caller).
    return replace(worst, proxy_scores={v.index_ticker: v.score for v in verdicts})


def compute_realized_vol(closes: list[float], window: int = 20) -> Optional[float]:
    """Annualized realized volatility of daily log returns.

    Args:
        closes: Closing prices, oldest-first.
        window: Number of most-recent daily returns to use. Defaults to 20.

    Returns:
        Annualized volatility (decimal, e.g. 0.18 = 18%), or None if there are
        fewer than ``window + 1`` closes or the window has non-positive prices.
    """
    if len(closes) < window + 1:
        return None
    recent = closes[-(window + 1) :]
    rets = []
    for prev, cur in zip(recent, recent[1:]):
        if prev <= 0 or cur <= 0:
            return None
        rets.append(math.log(cur / prev))
    n = len(rets)
    if n < 2:
        return None
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    return math.sqrt(var) * math.sqrt(252.0)


def compute_regime(
    metrics: HorizonMetrics,
    realized_vol_annual: Optional[float],
) -> RegimeVerdict:
    """Classify the market regime from an index's HorizonMetrics + realized vol.

    Scoring (risk-off points; each component is independent and additive):
      - Trend vs MA200: price below MA200 is the strongest bearish structure (+2);
        below MA50 but above MA200 is a caution (+1).
      - Drawdown from 52-week high: ≤ -10% (+2), ≤ -5% (+1).
      - 1-month return: ≤ -8% (+2), ≤ -3% (+1).
      - Realized volatility: ≥ 30% annualized (+2), ≥ 20% (+1).

    Label: score ≥ 4 → RISK_OFF; 2–3 → NEUTRAL; ≤ 1 → RISK_ON.

    Args:
        metrics: HorizonMetrics for the index proxy.
        realized_vol_annual: Output of ``compute_realized_vol`` on the index
            closes, or None when unavailable.

    Returns:
        A RegimeVerdict with the label, total score, and per-component points.
    """
    components: dict[str, int] = {}
    notes: list[str] = []
    price = metrics.current_price

    # Trend vs moving averages.
    trend_pts = 0
    if metrics.ma200 is not None and price < metrics.ma200:
        trend_pts = 2
        notes.append("price below MA200 (long-term downtrend)")
    elif metrics.ma50 is not None and price < metrics.ma50:
        trend_pts = 1
        notes.append("price below MA50 (short-term weakness)")
    components["trend"] = trend_pts

    # Drawdown from 52-week high.
    dd_pts = 0
    dd = metrics.pct_from_52w_high
    if dd is not None:
        if dd <= DRAWDOWN_RISK_OFF:
            dd_pts = 2
            notes.append(f"{dd:.1%} from 52w high (correction)")
        elif dd <= DRAWDOWN_CAUTION:
            dd_pts = 1
            notes.append(f"{dd:.1%} from 52w high (pullback)")
    components["drawdown"] = dd_pts

    # 1-month momentum.
    mom_pts = 0
    r1m = metrics.return_1m
    if r1m is not None:
        if r1m <= RETURN_1M_RISK_OFF:
            mom_pts = 2
            notes.append(f"1M return {r1m:.1%} (sharp decline)")
        elif r1m <= RETURN_1M_CAUTION:
            mom_pts = 1
            notes.append(f"1M return {r1m:.1%} (negative momentum)")
    components["momentum"] = mom_pts

    # Realized volatility.
    vol_pts = 0
    if realized_vol_annual is not None:
        if realized_vol_annual >= VOL_RISK_OFF:
            vol_pts = 2
            notes.append(f"realized vol {realized_vol_annual:.0%} (turbulent)")
        elif realized_vol_annual >= VOL_CAUTION:
            vol_pts = 1
            notes.append(f"realized vol {realized_vol_annual:.0%} (elevated)")
    components["volatility"] = vol_pts

    score = trend_pts + dd_pts + mom_pts + vol_pts
    if score >= SCORE_RISK_OFF:
        label = "RISK_OFF"
    elif score >= SCORE_NEUTRAL:
        label = "NEUTRAL"
    else:
        label = "RISK_ON"

    # Safety bias for a hard gate: without MA200 the primary trend anchor is
    # unknown, so we must not certify RISK_ON. Floor an otherwise-RISK_ON verdict
    # to NEUTRAL (the score is left untouched for transparency).
    if metrics.ma200 is None and label == "RISK_ON":
        label = "NEUTRAL"
        notes.append("insufficient history (no MA200) — floored RISK_ON to NEUTRAL")

    return RegimeVerdict(
        market=metrics.market,
        index_ticker=metrics.ticker,
        label=label,
        score=score,
        components=components,
        realized_vol_annual=realized_vol_annual,
        notes=notes,
    )
