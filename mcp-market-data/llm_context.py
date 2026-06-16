"""Deterministic guardrails for the /expect LLM_CONTEXT bull/bear debate.

LLM_CONTEXT is the channel where the model encodes macro/sector/narrative
context the deterministic table can't see. It graded weak (~19% win) and is the
hardest pillar to make rigorous because it is LLM-judged. This module can't make
the *judgment* better, but it enforces that the emitted debate is well-formed and
disciplined: score in range and sign-consistent with the declared winner, a real
(non-empty) bear case always present, and — when the score is strong — at least
one cited, typed piece of evidence behind it. A sloppy or hand-wavy debate fails
the lint instead of silently moving the composite.

Pure functions — no network, no I/O. Exposed via ``stock-cli lint-llm-context``
and consumed by tests.
"""

from __future__ import annotations

import math
from typing import Optional

# Bounded, asymmetric range (bigger negative span mitigates the algorithmic
# momentum bias). Mirrors the /expect rubric.
LLM_CONTEXT_MIN = -5.0
LLM_CONTEXT_MAX = 3.0

# Evidence behind a strong score must name one of these signal types, so
# "market feels risky" cannot justify a -3.
ALLOWED_SIGNAL_TYPES = frozenset(
    {"macro", "sector", "event", "flow", "valuation", "technical", "narrative"}
)

# Above this magnitude the score must be backed by cited, typed evidence.
STRONG_SCORE_THRESHOLD = 2.0


def clamp_score(score: float) -> Optional[float]:
    """Clamp a raw score into [-5.0, +3.0]; return None for non-finite input.

    NaN/Infinity must not pass through — ``min``/``max`` would let NaN surface as
    a max-bullish 3.0, which a caller reading the score (not the lint status)
    could trust. Oversized integers (hundreds of digits) are also rejected: they
    would raise OverflowError on the float conversion.
    """
    if not _is_finite_number(score):
        return None
    return max(LLM_CONTEXT_MIN, min(LLM_CONTEXT_MAX, float(score)))


def _is_finite_number(value: object) -> bool:
    """True when ``value`` is a real, finite number (handles huge-int overflow).

    ``math.isfinite`` raises OverflowError on an int too large to convert to
    float, so a malformed debate with a giant integer score must be caught here
    rather than crashing the linter.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _nonempty_str(value: object) -> bool:
    """True when ``value`` is a non-blank string (not an arbitrary truthy object)."""
    return isinstance(value, str) and bool(value.strip())


def _point_has_claim(point: object) -> bool:
    """True when a debate point carries a real (non-blank string) claim."""
    return isinstance(point, dict) and _nonempty_str(point.get("claim"))


def _point_is_cited(point: object) -> bool:
    """True when a point has a non-blank string evidence and an allowed type.

    Only string evidence counts — ``{}`` / ``[""]`` stringify to truthy text but
    are not evidence, so they must not satisfy the citation requirement.
    """
    if not isinstance(point, dict):
        return False
    return (
        _nonempty_str(point.get("evidence"))
        and point.get("signal_type") in ALLOWED_SIGNAL_TYPES
    )


def validate_llm_context(debate: dict) -> list[str]:
    """Return a list of rigor violations in an LLM_CONTEXT debate (empty == clean).

    Expected shape::

        {"score": -3.0, "winner": "bear",
         "bull_points": [{"claim": ..., "evidence": ..., "signal_type": ...}, ...],
         "bear_points": [...]}

    Checks:
      - ``score`` is a finite number within [-5.0, +3.0].
      - ``winner`` is one of bull/bear/neutral and agrees with the score's sign.
      - A real bear case exists (``bear_points`` non-empty) — the debate must be
        adversarial, never one-sided.
      - When |score| >= 2.0, the winning side has >= 1 cited, typed point.

    Args:
        debate: The structured debate object.

    Returns:
        Human-readable issue strings; empty list when the debate is well-formed.
    """
    issues: list[str] = []
    if not isinstance(debate, dict):
        return ["debate must be a JSON object"]

    score = debate.get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        issues.append("score must be a number")
        score = None
    elif not _is_finite_number(score):
        issues.append("score must be finite (NaN/Infinity/oversized not allowed)")
        score = None
    elif not LLM_CONTEXT_MIN <= score <= LLM_CONTEXT_MAX:
        issues.append(
            f"score {score} out of range [{LLM_CONTEXT_MIN}, {LLM_CONTEXT_MAX}]"
        )

    winner = debate.get("winner")
    if winner not in ("bull", "bear", "neutral"):
        issues.append("winner must be one of bull/bear/neutral")
    elif score is not None:
        expected = "bull" if score > 0 else "bear" if score < 0 else "neutral"
        if winner != expected:
            issues.append(
                f"winner '{winner}' contradicts score {score} (expected '{expected}')"
            )

    bull = debate.get("bull_points")
    bear = debate.get("bear_points")
    if not isinstance(bull, list):
        issues.append("bull_points must be a list")
        bull = []
    if not isinstance(bear, list):
        issues.append("bear_points must be a list")
        bear = []
    if not any(_point_has_claim(p) for p in bear):
        issues.append(
            "bear_points has no point with a real claim — a genuine bear case is required"
        )

    if isinstance(score, (int, float)) and abs(score) >= STRONG_SCORE_THRESHOLD:
        winning_side = bull if score > 0 else bear if score < 0 else None
        if winning_side is not None and not any(
            _point_is_cited(p) for p in winning_side
        ):
            issues.append(
                f"|score| >= {STRONG_SCORE_THRESHOLD} requires >=1 winning-side point "
                f"with non-empty evidence and a signal_type in {sorted(ALLOWED_SIGNAL_TYPES)}"
            )

    return issues


def score_from_debate(debate: dict) -> Optional[float]:
    """Return the clamped score from a debate, or None if it has no numeric score."""
    score = debate.get("score") if isinstance(debate, dict) else None
    # clamp_score handles finiteness (incl. oversized-int overflow) and returns
    # None for anything it can't represent.
    return clamp_score(score) if _is_finite_number(score) else None
