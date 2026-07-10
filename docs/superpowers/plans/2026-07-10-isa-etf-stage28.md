# ISA ETF Stage 28 — Targets, Drift-DCA Allocator, Decision Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The ISA decision core: an approved target allocation (`isa_targets`), a deterministic drift-correcting monthly-contribution allocator with a hard ±10%p tilt clamp, band-based rebalance checks (±5%p), and a decision log (`isa_decisions`) — exposed as `stock-cli isa init/status/allocate/rebalance/log`.

**Architecture:** Pure allocation math lives in `portfolio/isa_allocator.py` (no I/O). Persistence lives in `portfolio/isa_store.py` (two new tables in the existing `data/portfolio.db`, following `portfolio/db.py` conventions — WAL, CREATE TABLE IF NOT EXISTS). The ISA book itself is a normal KR portfolio named "ISA" (existing tables, zero schema change there); positions/prices come from the existing portfolio evaluator path. The LLM (stage 29 skill) may only PROPOSE tilts — the allocator clamps them in code (gate philosophy). No predictions.db coupling.

**Tech Stack:** Python 3.11, sqlite3 stdlib (match `portfolio/db.py`), pytest.

## Global Constraints

- `uv run pytest -m "not network"` green before every commit; DB tests use tmp-path SQLite files (match existing `portfolio/tests` conventions).
- All CLI output JSON. Fail open with VISIBLE notes. Amounts are integer KRW.
- Determinism: same inputs → identical output, including rounding (rules below). Module constants: `TILT_CAP_PP = 10.0`, `REBALANCE_BAND_PP = 5.0`.
- Sell-minimizing by design: `allocate` never emits sells; `rebalance` reports breaches and contribution-only remedies (ISA 비과세/의무기간 rationale).

---

### Task 1: `portfolio/isa_allocator.py` — pure allocation math

**Files:**
- Create: `portfolio/isa_allocator.py`
- Test: `portfolio/tests/test_isa_allocator.py`

**Interfaces (produces):**
- `clamp_tilt(tilt_pp: dict[str, float], cap: float = TILT_CAP_PP) -> tuple[dict[str, float], list[str]]` — per-class clamp to ±cap; note per clamped class.
- `effective_targets(targets_pct: dict[str, float], tilt_pp: dict[str, float] | None) -> tuple[dict[str, float], list[str]]` — apply clamped tilt, floor at 0, renormalize to sum 100 (unknown tilt classes → note + ignored).
- `allocate_contribution(amount_krw: int, current_value_by_class: dict[str, float], targets_pct: dict[str, float], tilt_pp: dict[str, float] | None = None) -> dict` returning `{"buys_by_class": dict[str,int], "effective_targets": dict[str,float], "notes": [str]}`.
- `compute_drift(current_value_by_class, targets_pct) -> dict[str, float]` — current weight minus target, in %p (empty book → all -target).
- `check_rebalance(current_value_by_class, targets_pct, band_pp: float = REBALANCE_BAND_PP) -> dict` — `{"needed": bool, "breaches": [{"asset_class", "drift_pp"}]}` (|drift| > band; empty book → needed=False + note "no positions").

**Allocation algorithm (document verbatim in the module docstring):**
1. `eff = effective_targets(targets, clamp_tilt(tilt))`.
2. `total_after = sum(current.values()) + amount`.
3. Per class: `deficit = max(0, total_after * eff/100 - current)`.
4. If `sum(deficits) == 0` (already at/above all targets): allocate `amount` proportional to `eff`.
5. `buys = amount * deficit / sum(deficits)` (or step-4 proportions).
6. Rounding: floor each buy to integer KRW; distribute the remaining won one at a time to classes ordered by largest fractional part, then class name ascending. `sum(buys) == amount` exactly.

- [ ] **Step 1: Failing tests**

```python
# portfolio/tests/test_isa_allocator.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portfolio.isa_allocator import (  # noqa: E402
    allocate_contribution, check_rebalance, clamp_tilt, compute_drift,
)

TARGETS = {"overseas_equity": 50.0, "bond": 50.0}
CURRENT = {"overseas_equity": 5_400_000.0, "bond": 3_600_000.0}  # 60/40


def test_underweight_first_waterfall():
    out = allocate_contribution(1_000_000, CURRENT, TARGETS)
    # total_after 10M → targets 5M/5M → deficits: overseas 0, bond 1.4M → all to bond
    assert out["buys_by_class"] == {"overseas_equity": 0, "bond": 1_000_000}


def test_tilt_shifts_within_cap():
    out = allocate_contribution(1_000_000, CURRENT, TARGETS,
                                tilt_pp={"overseas_equity": 10.0, "bond": -10.0})
    # eff 60/40 → targets_after 6M/4M → deficits 0.6M/0.4M
    assert out["buys_by_class"] == {"overseas_equity": 600_000, "bond": 400_000}


def test_tilt_clamped_at_cap_with_note():
    clamped, notes = clamp_tilt({"overseas_equity": 15.0})
    assert clamped == {"overseas_equity": 10.0}
    assert any("clamped" in n for n in notes)
    out = allocate_contribution(1_000_000, CURRENT, TARGETS,
                                tilt_pp={"overseas_equity": 15.0, "bond": -15.0})
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
    out = allocate_contribution(100, {"a": 1_000_000.0, "b": 0.0}, {"a": 0.0, "b": 100.0})
    assert out["buys_by_class"]["b"] == 100


def test_drift_and_band():
    drift = compute_drift(CURRENT, TARGETS)
    assert round(drift["overseas_equity"], 1) == 10.0
    assert round(drift["bond"], 1) == -10.0
    rb = check_rebalance(CURRENT, TARGETS)
    assert rb["needed"] is True
    assert {b["asset_class"] for b in rb["breaches"]} == {"overseas_equity", "bond"}
    rb2 = check_rebalance({"overseas_equity": 5_100_000.0, "bond": 4_900_000.0}, TARGETS)
    assert rb2["needed"] is False and rb2["breaches"] == []
```

- [ ] **Step 2: Run** `uv run pytest portfolio/tests/test_isa_allocator.py -q` → FAIL (module missing)
- [ ] **Step 3: Implement** per the documented algorithm (pure functions, full docstrings with arg/return types)
- [ ] **Step 4: Run** → 7 passed · **Step 5: Commit** `feat(isa): deterministic drift-DCA allocator with tilt clamp and band check`

---

### Task 2: `portfolio/isa_store.py` — targets + decisions persistence

**Files:**
- Create: `portfolio/isa_store.py`
- Test: `portfolio/tests/test_isa_store.py`

**Interfaces (produces):**
- `init_isa_tables(conn)` — `CREATE TABLE IF NOT EXISTS isa_targets (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, allocation TEXT NOT NULL, etf_map TEXT NOT NULL, note TEXT)` and `isa_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('contribution','rebalance','target_change')), amount_krw INTEGER, inputs TEXT NOT NULL, proposal TEXT, final TEXT NOT NULL, notes TEXT)`. `allocation`/`etf_map`/`inputs`/`proposal`/`final` are JSON strings.
- `save_target(conn, allocation: dict[str, float], etf_map: dict[str, str], note: str | None) -> int` — validates: weights sum to 100 (±0.01), every allocation class has an etf_map entry, no extra map classes. Raises ValueError otherwise. Also logs a `target_change` decision row.
- `get_active_target(conn) -> dict | None` — latest row, JSON-decoded.
- `log_decision(conn, kind, amount_krw, inputs: dict, proposal: dict | None, final: dict, notes: list[str]) -> int`
- `list_decisions(conn, limit: int = 20) -> list[dict]` — newest first, JSON-decoded.
Follow `portfolio/db.py` conventions (connection factory, WAL) — reuse its `get_connection` if importable; do NOT alter existing tables.

- [ ] **Step 1: Failing tests** — tmp SQLite: save_target round-trip + validation errors (sum≠100, missing map class); get_active_target returns latest; log/list decisions round-trip with JSON fields decoded; kind CHECK enforced (bad kind → sqlite3.IntegrityError).
- [ ] **Step 2: Run** → FAIL · **Step 3: Implement** · **Step 4: Run** → green · **Step 5: Commit** `feat(isa): targets + decision-log persistence (portfolio.db)`

---

### Task 3: CLI — `isa init / status / allocate / rebalance / log`

**Files:**
- Modify: `stock_cli.py` (new `isa` subparser group)
- Test: `tests/test_isa_cli.py` (in-process, tmp DB via monkeypatched connection factory; monkeypatched universe/prices)

**Behavior (produces):**
- `isa init --allocation "overseas_equity=50,bond=30,gold=20" --map "overseas_equity=360750,bond=114260,gold=411060" [--note TEXT]` — parses k=v lists; validates via `save_target`; ALSO validates each mapped code against `get_etf_universe()` (unknown code → error rc 1; leveraged/inverse code → error rc 1; universe unavailable → proceed with visible note "universe unavailable — code validation skipped"). Output: stored target JSON.
- `isa status` — active target + current ISA positions valued at current prices → weights by asset class, `compute_drift`, `check_rebalance`. Positions: the KR portfolio named "ISA" via the existing portfolio positions/valuation path (inspect `portfolio/evaluator.py` and `cmd_portfolio_positions` and reuse — do not duplicate pricing logic). No ISA portfolio yet → helpful error telling the user to `portfolio create --market KR --name ISA`. No target → rc 1 "no ISA target — run isa init".
- `isa allocate --amount N [--tilt "overseas_equity=+5,bond=-5"] [--dry-run]` — maps positions to asset classes via the active target's etf_map (position tickers NOT in the map → grouped as note + excluded from class values with visible warning), runs `allocate_contribution`, converts class buys → per-ETF KRW via etf_map + estimated shares (floor(amount/price), price from the existing KR price path; price unavailable → shares null + note). Logs a `contribution` decision (unless `--dry-run`). Output: per-ETF table + notes + decision id.
- `isa rebalance` — `check_rebalance` + for each breach a contribution-only remedy: the minimum extra contribution M such that allocating M by the standard algorithm brings all |drift| ≤ band (compute by closed form: M = max over overweight classes of (current_c*100/target_c_pct adjusted) — implement as documented helper `min_contribution_to_restore(current, targets, band)` in isa_allocator with its own unit test; if no finite M exists for a 0%-target class with holdings, say so in notes). Logs a `rebalance` decision. 
- `isa log [--limit N]` — decision history.

- [ ] **Step 1: Failing tests** — concrete asserts: init happy path + sum≠100 rc 1 + unknown/leveraged code rc 1 + universe-down note path; status drift math vs fixture positions; allocate underweight-first result + tilt clamp note + unmapped-ticker warning + dry-run does not log while normal run does (list_decisions count); rebalance breach + remedy value + logged; log limit.
- [ ] **Step 2: Run** → FAIL · **Step 3: Implement** (add `min_contribution_to_restore` to isa_allocator with unit test in Task 1's file) · **Step 4: Run** targeted + full suite green · **Step 5: Commit** `feat(cli): isa init/status/allocate/rebalance/log`

---

### Task 4: stage docs

**Files:**
- Create: `docs/stage-28/isa-targets-allocator.md` (Why/What/How incl. the allocation algorithm and the clamp/band constants/Code locations/Retrospective)
- Modify: `docs/summary.md` (`## Stage 28 — ISA targets, drift-DCA allocator, decision log`), `README.md` (short `isa` CLI block under portfolio section)

- [ ] Write docs · full test run green · Commit `docs(stage-28): ISA allocator stage doc + index`

## Self-Review Notes

- Spec coverage: targets propose→approve flow is conversational (skill/stage 29); the CLI stores what's given — spec's "Doctor Cho approves/edits → stored" is satisfied by `isa init` being run only after approval. Tilt clamp ±10 and band ±5 are code-enforced constants. Sell-minimizing: allocate never sells; rebalance reports remedies.
- Interfaces used by stage 29: `isa status/allocate/rebalance/log` JSON shapes; keep keys stable.
- No placeholders; allocator tests use exact-arithmetic fixtures.
