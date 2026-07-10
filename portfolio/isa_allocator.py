"""Pure ISA allocation math — deterministic drift-DCA allocator (no I/O).

The ISA book is sell-minimizing by design (비과세/의무기간): contributions are
steered toward underweight classes instead of selling overweight ones. The
LLM may only PROPOSE tilts; this module clamps them in code (gate philosophy).

Allocation algorithm (``allocate_contribution``):
  1. ``eff = effective_targets(targets, clamp_tilt(tilt))``.
  2. ``total_after = sum(current.values()) + amount``.
  3. Per class: ``deficit = max(0, total_after * eff/100 - current)``.
  4. If ``sum(deficits) == 0`` (already at/above all targets): allocate
     ``amount`` proportional to ``eff``.
  5. ``buys = amount * deficit / sum(deficits)`` (or step-4 proportions).
  6. Rounding: floor each buy to integer KRW; distribute the remaining won one
     at a time to classes ordered by largest fractional part, then class name
     ascending. ``sum(buys) == amount`` exactly.

Module constants: ``TILT_CAP_PP`` (hard ±cap on any proposed tilt) and
``REBALANCE_BAND_PP`` (|drift| band that triggers a rebalance check).
"""

from __future__ import annotations

import math

# Hard per-class cap on LLM-proposed tilts, in percentage points.
TILT_CAP_PP = 10.0
# Rebalance band: |current weight - target| beyond this many %p is a breach.
REBALANCE_BAND_PP = 5.0


def clamp_tilt(
    tilt_pp: dict[str, float], cap: float = TILT_CAP_PP
) -> tuple[dict[str, float], list[str]]:
    """Clamp each proposed tilt to ±cap percentage points.

    Args:
        tilt_pp: proposed per-class tilts in %p (e.g. {"bond": -15.0}).
        cap: hard cap in %p (default TILT_CAP_PP).

    Returns:
        (clamped tilts, notes) — one visible note per clamped class.
    """
    clamped: dict[str, float] = {}
    notes: list[str] = []
    for cls, t in tilt_pp.items():
        c = max(-cap, min(cap, t))
        if c != t:
            notes.append(
                f"tilt for {cls} clamped from {t:+g}p to {c:+g}p (cap ±{cap:g}p)"
            )
        clamped[cls] = c
    return clamped, notes


def effective_targets(
    targets_pct: dict[str, float], tilt_pp: dict[str, float] | None
) -> tuple[dict[str, float], list[str]]:
    """Apply (already-clamped) tilts to the target weights.

    Args:
        targets_pct: approved target weights summing to 100.
        tilt_pp: per-class %p shifts (or None). Classes not present in
            ``targets_pct`` are ignored with a note.

    Returns:
        (effective weights renormalized to sum 100, notes). Tilted weights are
        floored at 0 before renormalization.
    """
    notes: list[str] = []
    tilt_pp = tilt_pp or {}
    for cls in tilt_pp:
        if cls not in targets_pct:
            notes.append(f"tilt for unknown class {cls} ignored")
    eff = {cls: max(0.0, w + tilt_pp.get(cls, 0.0)) for cls, w in targets_pct.items()}
    total = sum(eff.values())
    if total <= 0:
        # Every class floored to 0 — renormalization is impossible and any
        # allocation over all-zero weights would destroy money. Fall back to
        # the original targets, visibly.
        notes.append("tilt zeroed all classes — tilt ignored")
        eff = dict(targets_pct)
        total = sum(eff.values())
    if total > 0 and total != 100.0:
        eff = {cls: w * 100.0 / total for cls, w in eff.items()}
    return eff, notes


def _round_preserving_sum(raw: dict[str, float], amount: int) -> dict[str, int]:
    """Floor each raw KRW amount and hand out the remainder deterministically.

    Args:
        raw: per-class fractional KRW amounts summing (approximately) to amount.
        amount: exact integer total the result must sum to.

    Returns:
        Integer KRW per class with ``sum == amount`` exactly. Remainder goes
        one won at a time to the largest fractional part, ties broken by class
        name ascending.
    """
    floors = {cls: math.floor(v) for cls, v in raw.items()}
    remainder = amount - sum(floors.values())
    order = sorted(raw, key=lambda cls: (-(raw[cls] - floors[cls]), cls))
    for cls in order[:remainder]:
        floors[cls] += 1
    return floors


def allocate_contribution(
    amount_krw: int,
    current_value_by_class: dict[str, float],
    targets_pct: dict[str, float],
    tilt_pp: dict[str, float] | None = None,
) -> dict:
    """Allocate a contribution toward underweight classes (never sells).

    Args:
        amount_krw: contribution in integer KRW.
        current_value_by_class: current market value per asset class (KRW).
        targets_pct: approved target weights summing to 100.
        tilt_pp: optional proposed tilts in %p (clamped to ±TILT_CAP_PP).

    Returns:
        ``{"buys_by_class": {class: int KRW}, "effective_targets": {...},
        "notes": [str]}`` with ``sum(buys) == amount_krw`` exactly.

    Rounding worked example: amount 100 over eff {"a": 66.7, "b": 33.3} on an
    empty book → raw {a: 66.7, b: 33.3} → floors {a: 66, b: 33}, remainder 1 →
    fractional parts a 0.7 > b 0.3 → the extra won goes to a → {a: 67, b: 33}.
    On a fractional-part tie the class name ascending wins the won.

    Raises:
        ValueError: if the rounded buys do not sum to ``amount_krw`` (money
        code — fail loud beats fail wrong; cannot happen given the
        effective-targets zeroed-tilt fallback).
    """
    notes: list[str] = []
    clamped, clamp_notes = clamp_tilt(tilt_pp or {})
    notes.extend(clamp_notes)
    eff, eff_notes = effective_targets(targets_pct, clamped)
    notes.extend(eff_notes)

    total_after = sum(current_value_by_class.values()) + amount_krw
    deficits = {
        cls: max(0.0, total_after * w / 100.0 - current_value_by_class.get(cls, 0.0))
        for cls, w in eff.items()
    }
    deficit_sum = sum(deficits.values())
    if deficit_sum > 0:
        raw = {cls: amount_krw * d / deficit_sum for cls, d in deficits.items()}
    else:
        # Already at/above every target: keep contributing proportionally.
        notes.append("all classes at/above target — allocating proportional to targets")
        raw = {cls: amount_krw * w / 100.0 for cls, w in eff.items()}
    buys = _round_preserving_sum(raw, amount_krw)
    if sum(buys.values()) != amount_krw:
        raise ValueError(
            f"allocation invariant violated: buys sum to {sum(buys.values())}, "
            f"expected {amount_krw} (eff={eff}, raw={raw})"
        )
    return {"buys_by_class": buys, "effective_targets": eff, "notes": notes}


def compute_drift(
    current_value_by_class: dict[str, float], targets_pct: dict[str, float]
) -> dict[str, float]:
    """Current weight minus target weight per class, in percentage points.

    Args:
        current_value_by_class: current market value per class (KRW).
        targets_pct: target weights summing to 100.

    Returns:
        Drift in %p for the union of target and held classes (unheld target
        class → -target; held non-target class → its full weight). Empty book
        → all -target.
    """
    total = sum(current_value_by_class.values())
    classes = set(targets_pct) | set(current_value_by_class)
    drift: dict[str, float] = {}
    for cls in classes:
        weight = (
            current_value_by_class.get(cls, 0.0) / total * 100.0 if total > 0 else 0.0
        )
        drift[cls] = weight - targets_pct.get(cls, 0.0)
    return drift


def check_rebalance(
    current_value_by_class: dict[str, float],
    targets_pct: dict[str, float],
    band_pp: float = REBALANCE_BAND_PP,
) -> dict:
    """Report which classes drift beyond the rebalance band.

    Args:
        current_value_by_class: current market value per class (KRW).
        targets_pct: target weights summing to 100.
        band_pp: breach threshold in %p (default REBALANCE_BAND_PP).

    Returns:
        ``{"needed": bool, "breaches": [{"asset_class", "drift_pp"}],
        "notes": [str]}`` — breaches sorted by |drift| descending. Empty book
        → needed=False with a "no positions" note (nothing to rebalance yet).
    """
    if not current_value_by_class or sum(current_value_by_class.values()) <= 0:
        return {"needed": False, "breaches": [], "notes": ["no positions"]}
    drift = compute_drift(current_value_by_class, targets_pct)
    # Compare on the rounded value: float noise at the band edge (e.g.
    # 5.0000000001, which prints as 5.0) must not be a spurious breach.
    breaches = [
        {"asset_class": cls, "drift_pp": round(d, 3)}
        for cls, d in drift.items()
        if abs(round(d, 3)) > band_pp
    ]
    breaches.sort(key=lambda b: -abs(b["drift_pp"]))
    return {"needed": bool(breaches), "breaches": breaches, "notes": []}


# Search ceiling for min_contribution_to_restore (≈ 1.15e18 KRW). If even this
# cannot restore the band the situation is structurally unreachable.
_MIN_CONTRIBUTION_SEARCH_CAP = 2**60


def min_contribution_to_restore(
    current_value_by_class: dict[str, float],
    targets_pct: dict[str, float],
    band_pp: float = REBALANCE_BAND_PP,
) -> int | None:
    """Minimum extra contribution that brings every |drift| within the band.

    Uses the REAL integer allocation (``allocate_contribution``, including
    KRW rounding) as the band predicate — a fractional-KRW model can claim
    M=1 restores the band while the rounded allocation gives that won to a
    different class (e.g. current a=0/b=1, targets 6/94: the model says 1,
    the integer allocator leaves a at -6pp; the true answer is 9).

    A pure closed form over overweight classes is also insufficient — a
    heavily underweight class can bind instead (targets 90/5/5 with holdings
    0/50/50) — so this is a deterministic doubling + binary search on integer
    KRW. Larger contributions move the book toward the effective targets, but
    integer rounding can break strict monotonicity by a few won, so the
    bisection invariant only guarantees the returned M satisfies the band
    (it may exceed the true minimum by a won-scale amount); a bounded forward
    scan re-verifies the result before returning.

    Args:
        current_value_by_class: current market value per class (KRW).
        targets_pct: target weights summing to 100.
        band_pp: allowed |drift| in %p (default REBALANCE_BAND_PP).

    Returns:
        Integer KRW M whose real allocation brings all |drift| ≤ band; 0 when
        already within band or the book is empty (nothing to restore — matches
        ``check_rebalance``'s needed=False); None when no M up to the search
        cap works (structurally unreachable, e.g. a 0% band).
    """
    if not current_value_by_class or sum(current_value_by_class.values()) <= 0:
        return 0

    def ok(m: int) -> bool:
        if m == 0:
            after = dict(current_value_by_class)
        else:
            buys = allocate_contribution(m, current_value_by_class, targets_pct)[
                "buys_by_class"
            ]
            after = {
                cls: current_value_by_class.get(cls, 0.0) + buys.get(cls, 0)
                for cls in set(current_value_by_class) | set(buys)
            }
        drift = compute_drift(after, targets_pct)
        return all(abs(d) <= band_pp for d in drift.values())

    if ok(0):
        return 0
    hi = 1
    while not ok(hi):
        hi *= 2
        if hi > _MIN_CONTRIBUTION_SEARCH_CAP:
            return None
    lo = hi // 2  # ok(lo) is False (or lo == 0, also False)
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if ok(mid):
            hi = mid
        else:
            lo = mid
    # Bisection keeps ok(hi) True throughout, but re-verify defensively and
    # scan forward a few won in case a future predicate change reintroduces
    # non-monotone edges — the returned M must actually satisfy the band.
    for m in range(hi, hi + len(targets_pct) + 2):
        if ok(m):
            return m
    return None
