# ISA ETF Stage 27 — Scoring + `etf compare` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deterministic long-horizon ETF scoring (cost + liquidity) and a `stock-cli etf compare` command that picks the best ticker among ETFs tracking the same index.

**Architecture:** New pure-function module `mcp-market-data/etf_score.py` (data stays in `etf_kr.py`, scoring stays here — one responsibility per file). Candidates come either from an explicit code list or a name-substring `--query` prefilter over the universe; per-candidate detail (fee, base index) is fetched via the stage-26 `fetch_etf_detail` (fail-open per candidate). Scores are normalized WITHIN the candidate set (relative comparison — this is a same-index tiebreaker, not an absolute rating). Missing metadata downgrades with a visible note, never silently (spec rule).

**Tech Stack:** Python 3.11, stdlib only for scoring; existing `etf_kr` interfaces: `EtfInfo` (fields: code, name, price, nav, deviation_pct, aum_100m_krw, value_million_krw, ret_3m_pct, tab_code, asset_class, tax_type, hedged, leveraged_or_inverse), `get_etf_universe() -> (rows, source, notes)`, `fetch_etf_detail(code) -> {"fund_pay_pct", "base_index", "notes"}`.

## Global Constraints

- `uv run pytest -m "not network"` green before every commit; offline tests only (mock universe/detail).
- All CLI output JSON; fail open with VISIBLE notes; no predictions-store coupling.
- Scoring must be fully deterministic: fixed weights as module constants, explicit tie-breaks (lower fee → higher AUM → code ascending).
- Detail fetches are bounded: `--query` prefilter caps candidates at 15 (visible truncation note).

---

### Task 1: `etf_score.py` — deterministic candidate scoring

**Files:**
- Create: `mcp-market-data/etf_score.py`
- Test: `mcp-market-data/tests/test_etf_score.py`

**Interfaces:**
- Produces: `score_candidates(rows: list[EtfInfo], details: dict[str, dict]) -> dict` returning `{"scored": [per-candidate dicts sorted best-first], "best": code | None, "notes": [str]}`. Per-candidate dict keys: `code, name, fund_pay_pct, base_index, aum_100m_krw, value_million_krw, deviation_pct, ret_3m_pct, hedged, tax_type, cost_score, liquidity_score, composite, notes`.

**Scoring rules (module constants, document in docstring):**
- `cost_score` (0–100): min-max inverted within the set on `fund_pay_pct` (lowest fee → 100). All fees equal → all 100. Fee missing → `cost_score=None` + note `"<code>: fee unavailable — cost score excluded"`.
- `liquidity_score` (0–100): weighted min-max within the set — AUM 0.5, traded value 0.3, |deviation_pct| 0.2 (lower |deviation| better, missing deviation → weight redistributed to AUM/value proportionally + note).
- `composite`: mean of available subscores (`COST_WEIGHT=0.5, LIQ_WEIGHT=0.5`, renormalized if one side missing). Sort desc; ties → lower fee, then higher AUM, then code asc. `best` = first code (None if `rows` empty).
- Single-candidate set: both scores 100, note `"single candidate — scores degenerate"`.

- [ ] **Step 1: Failing tests**

```python
# mcp-market-data/tests/test_etf_score.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etf_kr import EtfInfo  # noqa: E402
from etf_score import score_candidates  # noqa: E402


def _mk(code, name, aum, value, dev, fee_in_details=True):
    return EtfInfo(code=code, name=name, price=10000.0, nav=10000.0,
                   deviation_pct=dev, aum_100m_krw=aum, value_million_krw=value,
                   ret_3m_pct=10.0, tab_code=4, asset_class="overseas_equity",
                   tax_type="other_type", hedged=False, leveraged_or_inverse=False)


ROWS = [
    _mk("AAAAA1", "TIGER 미국S&P500", aum=202101, value=2677236, dev=0.05),
    _mk("BBBBB2", "KODEX 미국S&P500", aum=100000, value=900000, dev=0.10),
    _mk("CCCCC3", "ACE 미국S&P500", aum=50000, value=400000, dev=0.30),
]
DETAILS = {
    "AAAAA1": {"fund_pay_pct": 0.07, "base_index": "S&P 500", "notes": []},
    "BBBBB2": {"fund_pay_pct": 0.0099, "base_index": "S&P 500", "notes": []},
    "CCCCC3": {"fund_pay_pct": None, "base_index": "S&P 500", "notes": ["fund fee unavailable"]},
}


def test_best_balances_cost_and_liquidity():
    out = score_candidates(ROWS, DETAILS)
    scored = {s["code"]: s for s in out["scored"]}
    # BBBBB2: cheapest fee (cost 100) + mid liquidity; AAAAA1: fee 0.07 (cost 0) + top liquidity (100)
    assert scored["BBBBB2"]["cost_score"] == 100.0
    assert scored["AAAAA1"]["liquidity_score"] == 100.0
    assert out["best"] == "BBBBB2"  # 0.5*100 + 0.5*mid > 0.5*0 + 0.5*100


def test_missing_fee_excluded_with_note():
    out = score_candidates(ROWS, DETAILS)
    c = next(s for s in out["scored"] if s["code"] == "CCCCC3")
    assert c["cost_score"] is None
    assert c["composite"] == c["liquidity_score"]  # renormalized to liquidity only
    assert any("CCCCC3" in n and "fee unavailable" in n for n in out["notes"])


def test_deterministic_tiebreak_and_empty():
    out = score_candidates([], {})
    assert out["best"] is None and out["scored"] == []
    twin_a = _mk("TWINA1", "X", aum=100, value=100, dev=0.1)
    twin_b = _mk("TWINB2", "Y", aum=100, value=100, dev=0.1)
    d = {"TWINA1": {"fund_pay_pct": 0.1, "base_index": "I", "notes": []},
         "TWINB2": {"fund_pay_pct": 0.1, "base_index": "I", "notes": []}}
    out = score_candidates([twin_a, twin_b], d)
    assert out["best"] == "TWINA1"  # identical scores → code ascending
```

- [ ] **Step 2: Run** `uv run pytest mcp-market-data/tests/test_etf_score.py -q` → FAIL (module missing)
- [ ] **Step 3: Implement** `etf_score.py` per the rules above (pure functions: `_minmax(values) -> list[float|None]`, then assemble; document args/returns; no I/O in this module)
- [ ] **Step 4: Run** → 3 passed · **Step 5: Commit** `feat(etf): deterministic candidate scoring (cost + liquidity, set-relative)`

---

### Task 2: CLI `etf compare`

**Files:**
- Modify: `stock_cli.py` (extend the `etf` subparser group)
- Test: `tests/test_etf_cli.py` (append)

**Interfaces:**
- Produces: `stock-cli etf compare [CODES] [--query TEXT] [--include-leverage]`.
  - `CODES`: optional comma list (each normalized `.strip().zfill(6).upper()` — reuse the stage-26 normalization; extract it into a small helper `_normalize_etf_code` if it is currently inline).
  - `--query`: name-substring prefilter over `get_etf_universe()` rows — match on BOTH name and query lowered with spaces removed (`"s&p500"` matches `"TIGER 미국S&P500"`); leverage/inverse excluded unless `--include-leverage`; sorted by AUM desc; capped at 15 with note `"query matched N ETFs; comparing top 15 by AUM"` when truncated.
  - Exactly one of CODES / `--query` required (error JSON + rc 1 otherwise; also rc 1 when no candidates resolve).
  - Per-candidate `fetch_etf_detail` (fail-open — a candidate with failed detail still appears, fee None + note).
  - Base-index consistency: if >1 distinct non-None `base_index`, add `"base_index_mismatch": true` + note listing `index: [codes]` groups; still score (the user may be comparing across indexes deliberately).
  - Output JSON: `{asof, source, count, base_index_mismatch, best, scored, notes}` (universe notes + detail notes + scoring notes merged).

- [ ] **Step 1: Failing tests** — monkeypatch `etf_kr.get_etf_universe` (returns the 3-row set above + one leveraged row) and `etf_kr.fetch_etf_detail` (DETAILS lookup): assert (a) `etf compare AAAAA1,BBBBB2,CCCCC3` returns best BBBBB2 and mismatch false; (b) `--query "s&p 500"` finds the 3 (leveraged excluded) — note the space-insensitive match; (c) mixed-index details set mismatch true + note; (d) neither/both args → rc 1 error JSON; (e) unknown code in CODES → rc 1 with which code.
- [ ] **Step 2: Run** → FAIL · **Step 3: Implement** `cmd_etf_compare` + parser wiring · **Step 4: Run** targeted then full `uv run pytest -m "not network"` → green · **Step 5: Commit** `feat(cli): etf compare — same-index best-ticker selection`

---

### Task 3: stage docs

**Files:**
- Create: `docs/stage-27/etf-scoring-compare.md` (Why/What/How/Code locations/Retrospective)
- Modify: `docs/summary.md` (append `## Stage 27 — ETF scoring and same-index comparison` + one line), `README.md` (one `etf compare` example line under the existing etf example)

- [ ] **Step 1: Write docs** · **Step 2: Full test run green** · **Step 3: Commit** `docs(stage-27): scoring + compare stage doc + index`

## Self-Review Notes

- Spec coverage: "cost (총보수+추적오차)" — 추적오차 has no source (recorded stage-26 decision); cost uses fee only, |괴리율| covers execution quality inside liquidity. This is the documented degradation path.
- Interfaces match stage-26 exactly (tuple shape of `get_etf_universe`, detail dict keys).
- No placeholders; all tests concrete.
