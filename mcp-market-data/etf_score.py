"""Deterministic ETF candidate scoring (cost + liquidity, set-relative).

Pure functions only — no I/O. Data acquisition lives in ``etf_kr``; this
module ranks a candidate set (ETFs tracking the same index) so the CLI can
pick the best ticker. Scores are min-max normalized WITHIN the candidate set:
this is a same-index tiebreaker, not an absolute rating.

Scoring rules (spec: stage 27):
  - ``cost_score`` (0-100): min-max inverted on ``fund_pay_pct`` — lowest fee
    → 100. All fees equal → all 100. Missing fee → ``None`` + visible note
    (추적오차 has no source — recorded stage-26 decision — so cost is fee-only
    and |괴리율| covers execution quality inside liquidity).
  - ``liquidity_score`` (0-100): weighted min-max — AUM 0.5, traded value 0.3,
    |deviation_pct| 0.2 (lower |deviation| better). Missing deviation → its
    weight is redistributed to AUM/value proportionally + note.
  - ``composite``: weighted mean of the available subscores
    (COST_WEIGHT/LIQ_WEIGHT, renormalized when cost is missing).
  - Deterministic ordering: composite desc, then lower fee, then higher AUM,
    then code ascending.
"""

from __future__ import annotations

from etf_kr import EtfInfo

# Composite weights (renormalized when a subscore is unavailable).
COST_WEIGHT = 0.5
LIQ_WEIGHT = 0.5
# Liquidity subweights (deviation weight redistributed when missing).
AUM_WEIGHT = 0.5
VALUE_WEIGHT = 0.3
DEVIATION_WEIGHT = 0.2


def _minmax(values: list[float | None], invert: bool = False) -> list[float | None]:
    """Min-max normalize to 0-100 within the set.

    Args:
        values: raw values; ``None`` entries pass through as ``None``.
        invert: when True, the LOWEST raw value maps to 100 (e.g. fees).

    Returns:
        One score per input. Degenerate sets (all known values equal) map to
        100.0 regardless of ``invert`` — equal candidates are equally best.
    """
    known = [v for v in values if v is not None]
    if not known:
        return [None] * len(values)
    lo, hi = min(known), max(known)
    out: list[float | None] = []
    for v in values:
        if v is None:
            out.append(None)
        elif hi == lo:
            out.append(100.0)
        else:
            score = (v - lo) / (hi - lo) * 100
            out.append(round(100 - score if invert else score, 4))
    return out


def score_candidates(rows: list[EtfInfo], details: dict[str, dict]) -> dict:
    """Score and rank a candidate set of ETFs.

    Args:
        rows: candidate universe rows (same base index, typically).
        details: per-code detail dicts from ``etf_kr.fetch_etf_detail``
            (keys: ``fund_pay_pct``, ``base_index``, ``notes``).

    Returns:
        ``{"scored": [...], "best": code | None, "notes": [str]}`` — scored is
        sorted best-first; each entry carries code/name/fee/base_index/AUM/
        value/deviation/ret_3m/hedged/tax_type plus ``cost_score``,
        ``liquidity_score``, ``composite`` and per-candidate ``notes``.
    """
    notes: list[str] = []
    if not rows:
        return {"scored": [], "best": None, "notes": notes}
    if len(rows) == 1:
        notes.append("single candidate — scores degenerate")

    fees = [details.get(r.code, {}).get("fund_pay_pct") for r in rows]
    cost_scores = _minmax(fees, invert=True)
    aum_scores = _minmax([float(r.aum_100m_krw) for r in rows])
    value_scores = _minmax([float(r.value_million_krw) for r in rows])
    dev_scores = _minmax(
        [abs(r.deviation_pct) if r.deviation_pct is not None else None for r in rows],
        invert=True,
    )

    scored = []
    for i, r in enumerate(rows):
        cand_notes: list[str] = []
        cost = cost_scores[i]
        if cost is None:
            cand_notes.append(f"{r.code}: fee unavailable — cost score excluded")
        if dev_scores[i] is None:
            liq = (AUM_WEIGHT * aum_scores[i] + VALUE_WEIGHT * value_scores[i]) / (
                AUM_WEIGHT + VALUE_WEIGHT
            )
            cand_notes.append(
                f"{r.code}: deviation unavailable — liquidity weights redistributed"
            )
        else:
            liq = (
                AUM_WEIGHT * aum_scores[i]
                + VALUE_WEIGHT * value_scores[i]
                + DEVIATION_WEIGHT * dev_scores[i]
            )
        liq = round(liq, 4)
        if cost is None:
            composite = liq
        else:
            composite = round(COST_WEIGHT * cost + LIQ_WEIGHT * liq, 4)
        detail = details.get(r.code, {})
        scored.append(
            {
                "code": r.code,
                "name": r.name,
                "fund_pay_pct": detail.get("fund_pay_pct"),
                "base_index": detail.get("base_index"),
                "aum_100m_krw": r.aum_100m_krw,
                "value_million_krw": r.value_million_krw,
                "deviation_pct": r.deviation_pct,
                "ret_3m_pct": r.ret_3m_pct,
                "hedged": r.hedged,
                "tax_type": r.tax_type,
                "cost_score": cost,
                "liquidity_score": liq,
                "composite": composite,
                "notes": cand_notes,
            }
        )
        notes.extend(cand_notes)

    # Deterministic: composite desc → lower fee → higher AUM → code asc.
    scored.sort(
        key=lambda s: (
            -s["composite"],
            s["fund_pay_pct"] if s["fund_pay_pct"] is not None else float("inf"),
            -s["aum_100m_krw"],
            s["code"],
        )
    )
    return {"scored": scored, "best": scored[0]["code"], "notes": notes}
