# Stage 28 — ISA Targets, Drift-DCA Allocator, Decision Log

## Why

Stages 26-27 delivered the ETF data and same-index selection; the ISA book still needs
its decision core: an approved target allocation, a deterministic rule for where each
monthly contribution goes, and an audit trail. The LLM (stage 29 skill) may only
PROPOSE tilts — the allocator clamps them in code (gate philosophy), and the book is
sell-minimizing by design (비과세/의무기간).

## What

- `portfolio/isa_allocator.py` — pure allocation math (no I/O): `clamp_tilt`,
  `effective_targets`, `allocate_contribution`, `compute_drift`, `check_rebalance`,
  `min_contribution_to_restore`.
- `portfolio/isa_store.py` — `isa_targets` + `isa_decisions` tables in the existing
  `data/portfolio.db` (WAL, IF NOT EXISTS; zero changes to existing tables).
- `stock-cli isa init / status / allocate / rebalance / log` — the ISA book is a normal
  KR portfolio named "ISA"; valuation reuses `compute_positions` + the KR provider
  (no duplicated pricing logic).
- 21 new tests: 10 allocator, 8 store, 13 CLI (offline: tmp SQLite, patched
  universe/prices).

## How

**Constants (code-enforced):** `TILT_CAP_PP = 10.0` (hard ± clamp on any proposed
tilt), `REBALANCE_BAND_PP = 5.0` (|drift| band).

**Allocation algorithm** (verbatim in the module docstring): effective targets =
clamped tilt applied, floored at 0, renormalized to 100 → `total_after = current + amount`
→ per-class `deficit = max(0, total_after·eff% − current)` → contribution split
proportional to deficits (underweight-first waterfall; all-at-target → proportional to
targets) → floor to integer KRW, remainder distributed by largest fractional part then
class name ascending, so `sum(buys) == amount` exactly.

**`min_contribution_to_restore`:** the plan suggested a closed form (max over
overweight classes), but a counterexample (targets 90/5/5, holdings 0/50/50: closed
form gives 400, true answer 567 — the underweight class binds) shows it is
insufficient. Implemented instead as the documented fallback: deterministic doubling +
binary search on integer KRW against the exact band predicate (float-exact standard
allocation, no rounding), monotone in M. Pinned by exact-arithmetic tests: the 60/40
two-class case → 818,182 KRW (hand-derivable: 5.4M·100/55 − 9M), the counterexample
case → 567.

**Sell-minimizing rationale:** `allocate` never emits sells; `rebalance` reports band
breaches plus the contribution-only remedy. Every decision (contribution / rebalance /
target_change) is logged with inputs, raw proposal, and the final clamped action.

`isa init` validates weights (sum 100 ±0.01), etf_map coverage (exact class match),
and each mapped code against the live universe (unknown or leveraged/inverse →
rejected; universe down → visible "code validation skipped" note, fail-open).

## Code locations

- `portfolio/isa_allocator.py` — algorithm + constants + band search
- `portfolio/isa_store.py` — `CREATE_ISA_TABLES_SQL`, `save_target`,
  `get_active_target`, `log_decision`, `list_decisions`
- `stock_cli.py` — `cmd_isa_*`, `_parse_kv_list`, `_find_isa_portfolio`,
  `_isa_class_values`, `_isa_context`, `isa` subparser group
- `portfolio/tests/test_isa_allocator.py`, `portfolio/tests/test_isa_store.py`,
  `tests/test_isa_cli.py`

## Deviations from the plan

- `min_contribution_to_restore` is a documented deterministic binary search, not a
  closed form — the plan's own escape hatch, taken because the closed form is provably
  wrong when an underweight class binds (counterexample above, pinned as a test).

## Retrospective

Deriving the remedy math before coding caught a real spec bug (the overweight-only
closed form) at test-design time instead of in production. Reusing the portfolio
valuation path kept the CLI layer thin — the only new logic is class mapping.

## Review

Round 1 (internal: fully clean LGTM, suggestions only; Gemini/antigravity: real
findings; Codex: pending — to be reconciled if it replies), addressed in
`fix(isa): guard zeroed-tilt renormalization, weight-range validation, band
float edge (review round 1)`:

**Fixed (Gemini)**
- (BLOCKER) Tilts flooring EVERY class to 0 (e.g. ten 10% classes each tilted
  -10pp) made renormalization impossible: `eff` stayed all-zero, the
  proportional path produced all-zero raws, and the remainder loop distributed
  at most one won per class — violating `sum(buys) == amount` (money
  destroyed). Fixed at the source: `effective_targets` falls back to the
  ORIGINAL targets with a visible "tilt zeroed all classes — tilt ignored"
  note when the post-floor total is 0. `allocate_contribution` additionally
  asserts the invariant and raises ValueError with context if ever violated —
  money code fails loud, not wrong.
- Band-edge float noise: drift 5.0000000001 (prints as 5.0) was a spurious
  breach → `check_rebalance` compares `abs(round(d, 3)) > band_pp`. (The
  remedy search keeps the stricter exact predicate; exact-within-band implies
  rounded-within-band, so the remedy always satisfies the check.)
- `save_target` accepted negative weights that sum to 100 (A=110/B=-10) →
  every weight validated within [0, 100], ValueError lists offending classes;
  mirrored in a CLI init error-path test.

**Applied (internal suggestion)**
- Worked rounding example (100 over 66.7/33.3 → 67/33, tie → name ascending)
  added to the `allocate_contribution` docstring.

New tests: zeroed-tilt fallback (note + money conserved + eff == originals),
band-edge non-breach, store negative-weight rejection, CLI negative-weight rc 1.

Round 2 (Codex bot — reviewed the pre-fix commit e35c545), reconciled +
addressed in `fix(isa): address codex review — portfolio routing, dup-code
rejection, ticker normalization, integer-exact restore search`:

**Already fixed in round 1 (reconciliation)**
- Negative weights persisting through `save_target` → fixed in a878227
  (weight-range validation).
- Zeroed-tilt renormalization destroying money → fixed in a878227
  (original-targets fallback + sum invariant).

**Fixed (new findings)**
- (P2, real user setup) `portfolio buy/sell/import --market KR` always wrote
  to the FIRST KR portfolio ("Toss KR" for the actual user), so following our
  own guidance would leave the ISA book empty. Added optional
  `--portfolio NAME` routing to buy/sell/import (default keeps the historical
  first-portfolio behavior — zero change without the flag; unknown name →
  error listing available portfolios). `isa status` guidance now says to
  trade with `--portfolio ISA`.
- (P2) `save_target` rejects duplicate ETF codes across classes — the reverse
  code→class map must be bijective or a class silently vanishes from class
  values/drift/buys.
- (P2) Held tickers recorded unpadded/lowercase ("69500") were silently
  excluded by `_isa_class_values`' exact map lookup → tickers normalized with
  `_normalize_etf_code` before class lookup and price fetch.
- (P3) Empty book: `isa rebalance` reported `min_contribution_to_restore: 1`
  next to "no positions" → the search returns 0 for an empty/zero book
  (consistent with `check_rebalance` needed=False).
- (P3, math) The restore-search predicate modeled fractional KRW; the real
  integer allocator can route the marginal won elsewhere (Codex boundary
  example: current a=0/b=1, targets 6/94, band 5 → model said 1, integer
  allocation leaves -6pp; true answer 9). The predicate now runs the REAL
  `allocate_contribution` (integer path). Integer rounding can break strict
  monotonicity by a few won, so bisection's invariant only guarantees the
  returned M satisfies the band; a bounded forward scan re-verifies before
  returning. Pinned regression: the boundary example returns 9 AND the
  returned M is proven through the real allocator + `check_rebalance`.
  The round-1 pinned values (818,182 / 567) are unchanged under the integer
  predicate.
