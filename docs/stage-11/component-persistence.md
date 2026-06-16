# Stage 11 · H1 — Per-pillar component persistence (harmonization substrate)

## Why
Recalibration (A) exposed that logged confidence carries **no discrimination** —
it collapses to the base rate because the system never records *why* it scored a
pick. To harmonize the three capabilities (algorithmic / news / LLM) and one day
emit an informative blended confidence, each pillar's contribution must be
captured per prediction. You can't learn a blend from data you never stored.

## What
- Nullable `components` JSON column on `predictions` (additive migration,
  mirroring `raw_confidence`). Stores e.g.
  `{"algo":7.0,"news":1.0,"llm_context":-1.5,"overextension":"NONE","regime":"RISK_ON"}`.
- `Prediction.components: Optional[dict]`; serialized on insert, parsed on read.
- `predict create --components '<json>'` (rejects malformed/non-object/NaN).
- `metrics.get_component_contribution` + `stock-cli component-contribution`:
  win-rate split per pillar (numeric → positive/zero/negative; categorical →
  by value). Empty until components-tagged rows accumulate.
- Both skills now emit `--components` on every logged prediction.
- 14 tests in `tests/test_components.py` + 3 CLI-validation tests.

## How
- `components` is one extensible JSON column rather than several typed columns —
  cheap to migrate and open to new pillars.
- The contribution readout answers "does a positive news/llm contribution
  actually raise the hit rate?" — the measurement substrate for a learned blend
  (E2-future) that can restore confidence discrimination.

## Validation
Forward-looking infrastructure: production has 0 components-tagged closed rows
today, so `component-contribution` is empty until `/expect` + daily-briefing log
new predictions. The contribution math is unit-tested on seeded data (positive
news 0.8 win vs negative 0.2). No backfill claim.

## Review loop (code-reviewer + codex + gemini)
- **Fixed (all, High) — full-swap migration data loss.** `_migrate_schema_if_needed`
  copied a hardcoded column list omitting `raw_confidence`/`components`; a drifted
  pre-v2 DB carrying them would lose them. Now copies the **intersection** of old
  and new columns. Regression test added (legacy DB w/ both columns + no
  analysis_group_id → values preserved).
- **Fixed (codex, Medium) — NaN/Infinity** accepted by `json.loads`; now rejected
  via `parse_constant` (they would serialize back as non-standard JSON and skew
  the numeric split).
- **Fixed (code-reviewer, Medium) — zero vs negative** conflation; numeric pillars
  now split positive/zero/negative.
- **Fixed (codex, Low) — `n_with_components`** counted invalid rows; now counts
  only successfully-parsed dict rows.
- **Fixed — error handling** on `cmd_component_contribution`; CLI-validation tests
  for malformed/non-object/NaN components.
- **Noted — `CHECK(json_valid(...))`** suggestion declined: SQLite-version
  dependent and the CLI already validates; would add fragility for little gain.

## Code locations
- `mcp-prediction-store/models.py` — `components` column, `_ensure_components_column`,
  intersection copy in `_migrate_schema_if_needed`, dataclass/insert/reader.
- `mcp-prediction-store/metrics.py` — `get_component_contribution`.
- `stock_cli.py` — `--components` flag + validation, `cmd_component_contribution`.
- `.claude/skills/expect/SKILL.md`, `.claude/skills/daily-briefing/SKILL.md`.
- `tests/test_components.py`, `tests/test_recalibration_cli.py`.

## Retrospective
- The review caught a latent data-loss bug in a migration path my change didn't
  touch but did *expand the blast radius of* — a reminder to audit the full-swap
  copy whenever a column is added.
- This stage ships no win-rate change by design; it is the data foundation that
  makes the next harmonization stage (learned blend) possible and honest.
