"""Tests for the pure ISA allocation math (portfolio.isa_allocator).

Exact-arithmetic fixtures throughout — the allocator must be fully
deterministic including rounding.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portfolio.isa_allocator import (  # noqa: E402
    allocate_contribution,
    check_rebalance,
    clamp_tilt,
    compute_drift,
)

TARGETS = {"overseas_equity": 50.0, "bond": 50.0}
CURRENT = {"overseas_equity": 5_400_000.0, "bond": 3_600_000.0}  # 60/40


def test_underweight_first_waterfall():
    out = allocate_contribution(1_000_000, CURRENT, TARGETS)
    # total_after 10M → targets 5M/5M → deficits: overseas 0, bond 1.4M → all to bond
    assert out["buys_by_class"] == {"overseas_equity": 0, "bond": 1_000_000}


def test_tilt_shifts_within_cap():
    out = allocate_contribution(
        1_000_000, CURRENT, TARGETS, tilt_pp={"overseas_equity": 10.0, "bond": -10.0}
    )
    # eff 60/40 → targets_after 6M/4M → deficits 0.6M/0.4M
    assert out["buys_by_class"] == {"overseas_equity": 600_000, "bond": 400_000}


def test_tilt_clamped_at_cap_with_note():
    clamped, notes = clamp_tilt({"overseas_equity": 15.0})
    assert clamped == {"overseas_equity": 10.0}
    assert any("clamped" in n for n in notes)
    out = allocate_contribution(
        1_000_000, CURRENT, TARGETS, tilt_pp={"overseas_equity": 15.0, "bond": -15.0}
    )
    assert out["buys_by_class"] == {"overseas_equity": 600_000, "bond": 400_000}
    assert any("clamped" in n for n in out["notes"])


def test_empty_book_allocates_to_targets():
    out = allocate_contribution(1_000_000, {}, TARGETS)
    assert out["buys_by_class"] == {"overseas_equity": 500_000, "bond": 500_000}


def test_rounding_exact_sum():
    out = allocate_contribution(1_000, {}, {"a": 33.4, "b": 33.3, "c": 33.3})
    assert sum(out["buys_by_class"].values()) == 1_000
    # floor 334/333/333 → exact, no remainder; then a skewed case:
    out2 = allocate_contribution(100, {}, {"a": 66.7, "b": 33.3})
    assert sum(out2["buys_by_class"].values()) == 100


def test_over_target_book_falls_back_to_proportional():
    # both classes already above post-contribution targets is impossible;
    # construct sum(deficit)==0 via zero amount edge → proportional path
    out = allocate_contribution(
        100, {"a": 1_000_000.0, "b": 0.0}, {"a": 0.0, "b": 100.0}
    )
    assert out["buys_by_class"]["b"] == 100


def test_drift_and_band():
    drift = compute_drift(CURRENT, TARGETS)
    assert round(drift["overseas_equity"], 1) == 10.0
    assert round(drift["bond"], 1) == -10.0
    rb = check_rebalance(CURRENT, TARGETS)
    assert rb["needed"] is True
    assert {b["asset_class"] for b in rb["breaches"]} == {"overseas_equity", "bond"}
    rb2 = check_rebalance(
        {"overseas_equity": 5_100_000.0, "bond": 4_900_000.0}, TARGETS
    )
    assert rb2["needed"] is False and rb2["breaches"] == []
