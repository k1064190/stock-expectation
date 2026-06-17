"""Unit tests for scheduler.blended_funnel (pure blend + format)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "mcp-market-data"))

from blended_funnel import blend_streams, format_blended_for_prompt  # noqa: E402
from candidate_discovery import Candidate  # noqa: E402


def _cand(ticker, source="momentum", setup=None, reason="momentum", ret=10.0):
    return Candidate(
        ticker=ticker,
        name=ticker,
        market="US",
        market_cap=None,
        trading_value=None,
        return_5d_pct=ret,
        vol_ratio_5d=1.0,
        reason=reason,
        discovery_source=source,
        setup_type=setup,
    )


def test_blend_puts_presurge_first_and_splits_anchors():
    momentum = [
        _cand("SPY", source="anchor", reason="anchor"),
        _cand("HOT", source="momentum", ret=22.0),
    ]
    presurge = [_cand("BASE", source="presurge", setup="pullback", ret=3.0)]
    picks, anchors = blend_streams(momentum, presurge)
    assert [c.ticker for c in picks] == ["BASE", "HOT"]  # pre-surge first
    assert [c.ticker for c in anchors] == ["SPY"]


def test_blend_dedups_preferring_presurge_tag():
    momentum = [_cand("DUP", source="momentum", ret=18.0)]
    presurge = [_cand("DUP", source="presurge", setup="base_pivot", ret=4.0)]
    picks, _ = blend_streams(momentum, presurge)
    assert len(picks) == 1
    assert picks[0].discovery_source == "presurge"
    assert picks[0].setup_type == "base_pivot"


def test_sector_boost_reorders_and_tags():
    momentum = [_cand("LATE", source="momentum", ret=12.0)]
    presurge = [_cand("EARLY", source="presurge", setup="pullback", ret=3.0)]
    verdicts = {
        "EARLY": {"verdict": "FAVOR", "stage": "early"},
        "LATE": {"verdict": "AVOID", "stage": "late"},
    }
    picks, _ = blend_streams(momentum, presurge, sector_verdicts=verdicts)
    assert picks[0].ticker == "EARLY"  # FAVOR+presurge ranks above AVOID momentum
    assert picks[0].sector_verdict == "FAVOR"
    late = next(c for c in picks if c.ticker == "LATE")
    assert late.sector_verdict == "AVOID"


def test_blend_no_sector_verdicts_is_noop_order():
    momentum = [_cand("M1", ret=20.0), _cand("M2", ret=16.0)]
    presurge = [_cand("P1", source="presurge", setup="rs_leader", ret=8.0)]
    picks, _ = blend_streams(momentum, presurge)
    assert [c.ticker for c in picks] == ["P1", "M1", "M2"]


def test_format_has_labelled_sections():
    picks = [
        _cand("P1", source="presurge", setup="pullback", ret=2.0),
        _cand("M1", source="momentum", ret=19.0),
    ]
    anchors = [_cand("SPY", source="anchor", reason="anchor")]
    out = format_blended_for_prompt(picks, anchors, "US")
    assert "Pre-surge" in out and "Momentum" in out and "Anchors" in out
    assert "P1" in out and "M1" in out and "SPY" in out
    # momentum section warns it is conditional
    assert "BUY only if NOT overextended" in out
