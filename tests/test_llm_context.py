"""Tests for LLM_CONTEXT debate guardrails (mcp-market-data/llm_context.py)."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-market-data"))

from llm_context import (  # noqa: E402
    clamp_score,
    score_from_debate,
    validate_llm_context,
)


def _cited(signal_type="macro"):
    return {
        "claim": "KOSPI parabolic",
        "evidence": "+25% in 22 sessions",
        "signal_type": signal_type,
    }


def test_clamp_score_bounds():
    assert clamp_score(9.0) == 3.0
    assert clamp_score(-9.0) == -5.0
    assert clamp_score(-3.0) == -3.0


def test_clean_strong_bear_debate():
    debate = {
        "score": -3.0,
        "winner": "bear",
        "bull_points": [
            {"claim": "uptrend", "evidence": "MA stack", "signal_type": "technical"}
        ],
        "bear_points": [_cited("macro"), _cited("flow")],
    }
    assert validate_llm_context(debate) == []


def test_score_out_of_range_flagged():
    debate = {
        "score": 5.0,
        "winner": "bull",
        "bull_points": [_cited()],
        "bear_points": [_cited()],
    }
    assert any("out of range" in i for i in validate_llm_context(debate))


def test_winner_sign_mismatch_flagged():
    debate = {
        "score": -2.0,
        "winner": "bull",
        "bull_points": [_cited()],
        "bear_points": [_cited()],
    }
    assert any("contradicts score" in i for i in validate_llm_context(debate))


def test_missing_bear_case_flagged():
    debate = {
        "score": 1.0,
        "winner": "bull",
        "bull_points": [_cited()],
        "bear_points": [],
    }
    assert any("bear case is required" in i for i in validate_llm_context(debate))


def test_strong_score_needs_cited_evidence():
    # Strong bear score but bear points have no evidence/signal_type → flagged.
    debate = {
        "score": -3.0,
        "winner": "bear",
        "bull_points": [_cited()],
        "bear_points": [{"claim": "feels risky", "evidence": "", "signal_type": None}],
    }
    assert any(
        "requires >=1 winning-side point" in i for i in validate_llm_context(debate)
    )


def test_weak_score_does_not_require_evidence():
    # |score| < 2 → no cited-evidence requirement (but bear case still required).
    debate = {
        "score": 1.0,
        "winner": "bull",
        "bull_points": [{"claim": "mild", "evidence": "", "signal_type": None}],
        "bear_points": [{"claim": "some risk", "evidence": "", "signal_type": None}],
    }
    assert validate_llm_context(debate) == []


def test_bad_signal_type_not_accepted_as_citation():
    debate = {
        "score": 2.5,
        "winner": "bull",
        "bull_points": [{"claim": "x", "evidence": "y", "signal_type": "vibes"}],
        "bear_points": [_cited()],
    }
    assert any(
        "requires >=1 winning-side point" in i for i in validate_llm_context(debate)
    )


def test_non_dict_debate():
    assert validate_llm_context([1, 2, 3]) == ["debate must be a JSON object"]


def test_score_from_debate_clamps_and_handles_missing():
    assert score_from_debate({"score": 7.0}) == 3.0
    assert score_from_debate({}) is None
    assert score_from_debate({"score": True}) is None  # bool is not a score


def test_fake_bear_case_rejected():
    # An empty-object or claimless bear point is not a real bear case.
    debate = {
        "score": 2.0,
        "winner": "bull",
        "bull_points": [_cited()],
        "bear_points": [{}],
    }
    assert any("genuine bear case" in i for i in validate_llm_context(debate))


def test_non_string_evidence_not_a_citation():
    # evidence as {} stringifies to truthy text but must not count as a citation.
    debate = {
        "score": 3.0,
        "winner": "bull",
        "bull_points": [{"claim": "x", "evidence": {}, "signal_type": "macro"}],
        "bear_points": [{"claim": "real risk"}],
    }
    assert any(
        "requires >=1 winning-side point" in i for i in validate_llm_context(debate)
    )


def test_nan_score_flagged_and_not_clamped():
    nan = float("nan")
    assert any(
        "finite" in i
        for i in validate_llm_context(
            {
                "score": nan,
                "winner": "neutral",
                "bull_points": [],
                "bear_points": [{"claim": "r"}],
            }
        )
    )
    assert score_from_debate({"score": nan}) is None
    assert clamp_score(float("inf")) is None


def test_score_zero_neutral_ok_and_nonneutral_rejected():
    ok = {
        "score": 0.0,
        "winner": "neutral",
        "bull_points": [],
        "bear_points": [{"claim": "r"}],
    }
    assert validate_llm_context(ok) == []
    bad = {
        "score": 0.0,
        "winner": "bull",
        "bull_points": [],
        "bear_points": [{"claim": "r"}],
    }
    assert any("contradicts score" in i for i in validate_llm_context(bad))
