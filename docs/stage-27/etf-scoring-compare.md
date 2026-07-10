# Stage 27 — ETF Scoring + Same-Index Comparison

## Why

Stage 26 delivered the KR ETF universe + metadata; the ISA book still needs a
deterministic answer to "which of the N ETFs tracking the same index should I buy?"
— cost and liquidity decide that, not narrative.

## What

- `mcp-market-data/etf_score.py` — pure-function, set-relative candidate scoring
  (`score_candidates`), stdlib only, no I/O.
- `stock-cli etf compare CODES | --query TEXT [--include-leverage]` — resolves
  candidates (explicit codes or a space-insensitive name prefilter, AUM-desc, capped
  at 15 with a visible note), fetches per-candidate fee/base-index fail-open, flags
  `base_index_mismatch`, and emits `{asof, source, count, base_index_mismatch, best,
  scored, notes}`.
- `_normalize_etf_code` helper extracted in `stock_cli.py` (shared by `etf info` and
  `etf compare`).
- 5 module tests + 5 CLI tests, all offline.

## How

Scoring is min-max normalized WITHIN the candidate set (same-index tiebreaker, not an
absolute rating): `cost_score` inverts fees (lowest → 100; missing fee → None + visible
note, composite renormalizes to liquidity only); `liquidity_score` weights AUM 0.5,
traded value 0.3, |괴리율| 0.2 (missing deviation redistributes its weight to AUM/value);
`composite` = 0.5/0.5 mean of available subscores. Ties break deterministically: lower
fee → higher AUM → code ascending. Degenerate sets (all equal / single candidate) score
100 with a note.

**추적오차 degradation note (spec rule, stage-26 decision):** tracking error has no
available source, so "cost" is fee-only and execution quality is covered by |괴리율|
inside liquidity — a documented downgrade with visible notes, never silent.

Live verification (2026-07-10, no key): `etf compare --query "미국 S&P500"` → 50
matches truncated to top 15 by AUM with note, best = 360750 (TIGER 미국S&P500,
fee 0.007%, composite 91.8), `base_index_mismatch=true` correctly flagging the
futures/covered-call/dividend-aristocrats variants against plain S&P 500 trackers.

## Code locations

- `mcp-market-data/etf_score.py` — `COST_WEIGHT`/`LIQ_WEIGHT`/`AUM_WEIGHT`/
  `VALUE_WEIGHT`/`DEVIATION_WEIGHT`, `_minmax`, `score_candidates`
- `stock_cli.py` — `_normalize_etf_code`, `cmd_etf_compare`,
  `MAX_COMPARE_CANDIDATES`, `compare` subparser under the `etf` group
- `mcp-market-data/tests/test_etf_score.py`, `tests/test_etf_cli.py` (compare section)

## Retrospective

Keeping scoring pure (data in `etf_kr`, math here) made the tests trivial and the live
run correct on the first try. The base-index mismatch note proved immediately useful —
the S&P 500 query surfaces futures/covered-call variants that a naive fee sort would
have crowned.

## Review

Round 1 (internal + Gemini/antigravity; Codex unresponsive — re-triggered, to be
reconciled if it replies), addressed in `fix(etf): address review round 1 —
degenerate-set note, input validation, dedup, error-path tests`:

**Refuted (evidence)**
- (Gemini "blocker") "sort tie-break `-s["aum_100m_krw"]` can TypeError on a
  string value": false — every constructor path coerces to int
  (`etf_kr._parse_row` → `int(it.get("marketSum") or 0)`; `etf_kr._load_cache`
  → `_INT_FIELDS` `int(d[k])`), the scored dict copies that int verbatim, and
  the `float()` at the `_minmax` call is normalization-math input, not a
  defensive cast. No str path exists.
- (internal warning) "missing try/except around `fetch_etf_detail` breaks
  per-candidate fail-open": false — `fetch_etf_detail` catches ALL exceptions
  internally and returns Nones + note (stage-26 contract, documented in its
  docstring). Added a one-line contract comment at the call site instead of a
  redundant try/except.

**Fixed**
- (Gemini) Multi-candidate sets equal in EVERY dimension now emit
  "candidates indistinguishable on cost/liquidity — scores degenerate"
  (the scoring-rules doc promised a degenerate-set note).
- (Gemini) `etf compare " "` now errors with "no valid ETF codes given
  (empty code list)" instead of the misleading "no ETFs matched query: None".
- (Gemini nit) Positional code lists are deduplicated first-seen-order, so
  `360750,360750` no longer scores against itself.
- (Gemini) Test added: `--query` + `--include-leverage` includes leveraged rows.
- (Gemini) Test added: query truncation at MAX_COMPARE_CANDIDATES=15 + the
  visible truncation note (AUM-desc keeps the largest).
- (internal) Test added: `--query "nonexistent"` → rc 1 with "no ETFs matched".

Round 2 (Codex bot), 1 finding, verified valid and fixed in
`fix(etf): reject blank query text (codex round 2)`:

- (P2) `etf compare --query "   "` passed the truthy selector check but
  normalized to `""`, substring-matching every ETF name and silently comparing
  the top 15 by AUM → the stripped/normalized query is validated; blank →
  rc 1 `"empty query text"` (mirrors the round-1 empty-code-list error style).
  Regression test asserts rc 1 and no comparison output.
