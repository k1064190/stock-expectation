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


def test_min_contribution_two_class_exact():
    from portfolio.isa_allocator import min_contribution_to_restore

    # 60/40 book, band 5: overweight side binds.
    # weight_o(M) = 5.4M*100/(9M+M') ≤ 55 → M' ≥ 5,400,000*100/55 - 9,000,000
    # = 818,181.81... → minimal integer 818,182.
    m = min_contribution_to_restore(CURRENT, TARGETS)
    assert m == 818_182


def test_min_contribution_underweight_binds():
    from portfolio.isa_allocator import min_contribution_to_restore

    # Counterexample to the pure overweight closed form: the underweight class
    # a (target 90, held 0) binds. All contributions flow to a (only deficit
    # class while M ≤ 900): weight_a(M) = M/(100+M)*100 ≥ 85 → M ≥ 566.67
    # → 567. The overweight-only formula would give 400.
    targets = {"a": 90.0, "b": 5.0, "c": 5.0}
    current = {"a": 0.0, "b": 50.0, "c": 50.0}
    m = min_contribution_to_restore(current, targets)
    assert m == 567


def test_min_contribution_zero_when_within_band():
    from portfolio.isa_allocator import min_contribution_to_restore

    assert (
        min_contribution_to_restore(
            {"overseas_equity": 5_100_000.0, "bond": 4_900_000.0}, TARGETS
        )
        == 0
    )


def test_zeroed_tilt_falls_back_to_targets_with_note():
    """Tilts that floor EVERY class to 0 must be ignored (visible note) —
    otherwise renormalization is impossible and allocated money is destroyed."""
    targets = {f"c{i}": 10.0 for i in range(10)}
    tilt = {f"c{i}": -10.0 for i in range(10)}
    out = allocate_contribution(1_000_000, {}, targets, tilt_pp=tilt)
    assert sum(out["buys_by_class"].values()) == 1_000_000  # money conserved
    assert out["effective_targets"] == targets  # fell back to originals
    assert any("tilt ignored" in n for n in out["notes"])


def test_band_edge_float_noise_not_a_breach():
    """Drift of 5.0000000001 prints as 5.0 — must not be a spurious breach."""
    current = {"overseas_equity": 5_500_000.00011, "bond": 4_499_999.99989}
    rb = check_rebalance(current, TARGETS)
    assert rb["needed"] is False and rb["breaches"] == []


def test_min_contribution_empty_book_is_zero():
    """No positions → nothing to restore (check_rebalance says needed=False);
    the search must not report a bogus 1-KRW remedy."""
    from portfolio.isa_allocator import min_contribution_to_restore

    assert min_contribution_to_restore({}, TARGETS) == 0
    assert min_contribution_to_restore({"overseas_equity": 0.0}, TARGETS) == 0


def test_min_contribution_integer_rounding_boundary():
    """Codex boundary example: current a=0/b=1, targets 6/94, band 5. The
    fractional-KRW model says M=1 restores the band, but the REAL integer
    allocator gives that won to b (larger fractional part) leaving a at
    -6pp. The returned M must restore the band through the real allocator."""
    from portfolio.isa_allocator import min_contribution_to_restore

    current = {"a": 0.0, "b": 1.0}
    targets = {"a": 6.0, "b": 94.0}
    m = min_contribution_to_restore(current, targets)
    assert m == 9  # minimal M under the integer allocator (M=1..8 all fail)
    # And prove it end-to-end through the real allocator:
    buys = allocate_contribution(m, current, targets)["buys_by_class"]
    after = {cls: current.get(cls, 0.0) + buys.get(cls, 0) for cls in targets}
    assert check_rebalance(after, targets)["needed"] is False
