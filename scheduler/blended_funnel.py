"""Blended candidate funnel for the daily briefing.

The legacy briefing fed the LLM ONLY momentum names (``|5d return| >= 15%`` OR
``vol >= 2x``), sorted biggest-mover-first, and restricted picks to that list —
so it could only ever recommend stocks that had already surged. The as-of
backtest (WT-A.3) confirmed parabolic chases are the worst cohort, while a
modest-momentum / pre-surge stream is competitive (and never parabolic by
construction). Per the chosen design this funnel is **additive, not a
replacement**: it blends three labelled streams so the LLM sees diverse,
not-yet-extended candidates alongside a capped momentum slice and macro anchors.

  * pre-surge  — base/pullback/RS/pre-earnings names not yet extended (the new
                 diverse stream the funnel was missing).
  * momentum   — the legacy 5d-surge / volume-spike names, KEPT but capped and
                 explicitly flagged as "BUY only if not overextended" (the
                 store-level gate enforces that at log time).
  * anchors    — broad-market ETFs as a macro reference, NOT pick slots.

The hard enforcement of the parabolic cap lives in
``models._check_overextension_gate`` (store layer); this module shapes what the
LLM *sees* and tags each candidate's ``discovery_source``/``setup_type`` so
cohorts stay measurable in the weekly calibration.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(Path(__file__).resolve().parent), str(PROJECT_ROOT / "mcp-market-data")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from candidate_discovery import (  # noqa: E402
    Candidate,
    discover_kr_candidates,
    discover_us_candidates,
)
from pre_surge_discovery import discover_presurge_candidates  # noqa: E402

# Bounded sector-rotation sort multipliers (consumed when WT-C's sector verdicts
# are available; absent → all 1.0, i.e. a no-op).
_SECTOR_MULTIPLIER = {
    "FAVOR": 1.3,
    "ROTATING_IN": 1.15,
    "NEUTRAL": 1.0,
    "ROTATING_OUT": 0.8,
    "AVOID": 0.6,
}


def blend_streams(
    momentum: list[Candidate],
    presurge: list[Candidate],
    sector_verdicts: Optional[dict] = None,
) -> tuple[list[Candidate], list[Candidate]]:
    """Merge the momentum and pre-surge streams into one de-biased, tagged list.

    De-biasing: the pre-surge (not-yet-extended) stream is presented FIRST so the
    LLM no longer sees only biggest-movers-first; the momentum stream follows,
    capped by the caller. Anchors are split out as macro reference. A ticker
    appearing in both streams is kept once, preferring the pre-surge entry (it
    carries the richer ``setup_type``).

    Args:
        momentum: Output of ``discover_{us,kr}_candidates`` (momentum + anchors).
        presurge: Output of ``discover_presurge_candidates``.
        sector_verdicts: Optional ``{ticker: {"verdict": str, "stage": str}}``
            from WT-C; when present, candidates are re-ordered by a bounded
            sector multiplier and tagged with ``sector_verdict``/``sector_stage``.
            None → no sector influence (no-op).

    Returns:
        ``(picks, anchors)`` — ``picks`` is the blended, de-biased candidate list
        (pre-surge first, then non-duplicate momentum); ``anchors`` are the
        macro-reference ETFs (not counted as pick slots).
    """
    anchors = [c for c in momentum if c.reason == "anchor"]
    mom = [c for c in momentum if c.reason != "anchor"]

    seen: set[str] = set()
    ordered: list[Candidate] = []
    for c in [*presurge, *mom]:
        if c.ticker in seen:
            continue
        seen.add(c.ticker)
        ordered.append(c)

    if sector_verdicts:
        for c in ordered:
            sv = sector_verdicts.get(c.ticker)
            if sv:
                c.sector_verdict = sv.get("verdict")
                c.sector_stage = sv.get("stage")

        def _sort_key(c: Candidate) -> float:
            mult = _SECTOR_MULTIPLIER.get(c.sector_verdict or "NEUTRAL", 1.0)
            # Pre-surge ranked by setup quality is already first; preserve stream
            # order within ties by giving pre-surge a small base bump.
            base = 1.0 if c.discovery_source == "presurge" else 0.5
            return base * mult

        ordered.sort(key=_sort_key, reverse=True)

    return ordered, anchors


def format_blended_for_prompt(
    picks: list[Candidate], anchors: list[Candidate], market: str
) -> str:
    """Render the blended candidate block with labelled sub-sections.

    Args:
        picks: Blended candidate list from :func:`blend_streams`.
        anchors: Macro-reference anchors.
        market: ``"US"`` or ``"KR"`` (selects header language).

    Returns:
        A markdown block the briefing prompt injects. Pre-surge and momentum are
        separated so the LLM treats them differently (the momentum section is
        explicitly "BUY only if not overextended").
    """
    kr = market.upper() == "KR"
    pre = [c for c in picks if c.discovery_source == "presurge"]
    mom = [c for c in picks if c.discovery_source != "presurge"]

    def _line(c: Candidate) -> str:
        tag = c.setup_type or c.discovery_source
        name = c.name or "?"
        extras = []
        if c.sector_verdict:
            extras.append(f"sector={c.sector_verdict}")
        extra = f" {' '.join(extras)}" if extras else ""
        return (
            f"  - {c.ticker} {name} [{tag}]: "
            f"5d={c.return_5d_pct:+.1f}% vol_ratio={c.vol_ratio_5d:.2f}x{extra}"
        )

    lines: list[str] = []
    header = (
        "## 후보 종목 (블렌드 — pre-surge + 모멘텀)"
        if kr
        else "## Candidates (blended — pre-surge + momentum)"
    )
    lines.append(header)

    lines.append(
        "### 곧 움직일 후보 (pre-surge, 아직 과열 아님 — 우선 검토)"
        if kr
        else "### Pre-surge (not yet extended — review first)"
    )
    lines.extend([_line(c) for c in pre] or ["  (none)"])

    lines.append(
        "### 강한 모멘텀 (이미 상승 — 과열 아닐 때만 BUY, 아니면 WATCH)"
        if kr
        else "### Momentum (already moved — BUY only if NOT overextended, else WATCH)"
    )
    lines.extend([_line(c) for c in mom] or ["  (none)"])

    if anchors:
        lines.append(
            "### 앵커 (시장 참고용 — 추천 슬롯 아님)"
            if kr
            else "### Anchors (macro reference — NOT pick slots)"
        )
        lines.extend([_line(c) for c in anchors])

    return "\n".join(lines)


def assemble_blended_candidates(
    market: str,
    provider=None,
    momentum_n: int = 8,
    presurge_n: int = 12,
    min_score: float = 0.5,
    days: int = 400,
    sector_verdicts: Optional[dict] = None,
) -> tuple[list[Candidate], list[Candidate]]:
    """Discover both streams and blend them (the cron entry point).

    Args:
        market: ``"US"`` or ``"KR"``.
        provider: Injectable market-data provider (defaults to the market's).
        momentum_n: Momentum candidates to request (kept small — capped slice).
        presurge_n: Pre-surge candidates to request (the wider diverse stream).
        min_score: Pre-surge minimum setup score.
        days: History window for the pre-surge stream.
        sector_verdicts: Optional WT-C sector verdicts for boosting.

    Returns:
        ``(picks, anchors)`` from :func:`blend_streams`. Never raises — each
        underlying discovery already collapses to a safe fallback on failure.
    """
    market = market.upper()
    if market == "KR":
        momentum = discover_kr_candidates(top_n_output=momentum_n, provider=provider)
    else:
        momentum = discover_us_candidates(top_n_output=momentum_n, provider=provider)
    presurge = discover_presurge_candidates(
        market,
        provider=provider,
        top_n_output=presurge_n,
        min_score=min_score,
        days=days,
    )
    return blend_streams(momentum, presurge, sector_verdicts=sector_verdicts)
