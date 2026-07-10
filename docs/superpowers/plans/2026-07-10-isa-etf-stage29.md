# ISA ETF Stage 29 — Monthly Briefing Skill, Scheduler, Track Record Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the ISA loop: a monthly NAV snapshot track record (`isa snapshot`), a `/isa-briefing` Claude Code skill that turns CLI data into a Korean contribution briefing (tilt proposals stay inside the code-enforced clamp), and a `scheduler/isa_briefing.py` monthly runner with Telegram delivery and a crontab entry.

**Architecture:** Track record is pure Python (no LLM): `isa snapshot` values the ISA book, stores NAV + cumulative contributions + benchmark closes (S&P 500 via existing US provider, KOSPI via KR provider) into a new `isa_nav` table. The scheduler mirrors `scheduler/daily_briefing.py`'s structure — REUSE its runner/telegram utilities by import wherever importable instead of copying (inspect it first; the gold-trend script does NOT exist on master, do not reference it). The skill follows the existing skill conventions (`.claude/skills/daily-briefing/SKILL.md` as the style reference): all data via `bin/stock-cli`, judgment in the LLM, the ±10%p tilt clamp and decision logging enforced by the stage-28 CLI.

**Tech Stack:** Python 3.11, sqlite3, existing providers, pytest.

## Global Constraints

- `uv run pytest -m "not network"` green before every commit.
- All CLI output JSON; fail open with VISIBLE notes; predictions.db untouched.
- Monthly contribution amount is ALWAYS an explicit argument (`--amount`); never hardcoded, never silently defaulted.
- The LLM may only PROPOSE tilts; `isa allocate` clamps and logs (stage 28). The skill must instruct the model to pass tilts via `--tilt` and treat the CLI's clamped output as final.

---

### Task 1: `isa snapshot` — NAV track record

**Files:**
- Modify: `portfolio/isa_store.py` (new table + helpers), `stock_cli.py` (`isa snapshot`, and surface recent snapshots in `isa status` output)
- Test: `portfolio/tests/test_isa_store.py`, `tests/test_isa_cli.py` (append)

**Interfaces (produces):**
- Table `isa_nav (id INTEGER PRIMARY KEY AUTOINCREMENT, snapped_at TEXT NOT NULL, nav_krw INTEGER NOT NULL, contributions_cum_krw INTEGER NOT NULL, benchmarks TEXT NOT NULL, notes TEXT)`; `benchmarks` JSON like `{"sp500": 6123.4, "kospi": 3050.1}` (close values; `null` per index when its fetch fails, with a note — fail-open).
- `save_nav_snapshot(conn, nav_krw, contributions_cum_krw, benchmarks: dict, notes: list[str]) -> int`; `list_nav_snapshots(conn, limit=24) -> list[dict]` (newest first, JSON decoded).
- CLI `isa snapshot`: values the ISA book (reuse `_isa_class_values` / positions path), computes `contributions_cum_krw` as the sum of BUY transaction amounts minus SELL amounts in the ISA portfolio (inspect the transactions table for the exact fields), fetches benchmark closes (S&P 500: `^GSPC` via the US provider's price path; KOSPI: `^KS11` or provider equivalent — check what the providers actually support and note the choice), stores, prints the row. `isa status` gains a `recent_snapshots` key (last 3) and a simple `since_inception_return_pct` = (nav - contributions)/contributions*100 when contributions > 0 (money-weighted refinement is future work — note in docstring).

- [ ] **Step 1: failing tests** — store round-trip + ordering/limit + benchmarks JSON with null; CLI snapshot happy path (patched positions/prices/benchmarks: nav and cum-contribution math asserted exactly), benchmark-fetch-failure → null + note; status shows recent_snapshots + return pct (and no division by zero on empty book).
- [ ] **Step 2: red** · **Step 3: implement** · **Step 4: green (full suite)** · **Step 5: commit** `feat(isa): NAV snapshot track record (isa snapshot + status integration)`

---

### Task 2: `scheduler/isa_briefing.py` — monthly runner

**Files:**
- Create: `scheduler/isa_briefing.py`
- Modify: `scheduler/crontab.example` (monthly entry, e.g. `37 7 1 * *` — first of month 07:37 KST, off the :00 mark; comment says adjust to the user's contribution day)
- Test: `scheduler/tests/test_isa_briefing.py`

**Behavior (produces):**
- `uv run python scheduler/isa_briefing.py --amount 1000000 [--mode claude-code|codex-cli] [--no-telegram] [--dry-run]`.
- Inspect `scheduler/daily_briefing.py` FIRST and import/reuse: its claude-code / codex-cli runner helpers, Telegram send helper, and `_macro_block()` (macro context). If a helper is module-private but cleanly importable, import it; only if genuinely unusable, extract a tiny shared helper — do NOT copy-paste bodies.
- Flow: run `isa snapshot` (records this month's NAV first), then build a prompt containing: `isa status` JSON, `isa rebalance` JSON, the macro block, the amount, and the instruction to follow `.claude/skills/isa-briefing/SKILL.md`. `--dry-run` prints the prompt and exits without snapshot/LLM/telegram. Otherwise dispatch via the chosen mode; send the model's briefing output via Telegram (reuse daily's pattern incl. its length chunking if present).
- Errors: missing target / missing ISA portfolio → clear actionable stderr message, exit 1, no LLM call.

- [ ] **Step 1: failing tests** — prompt builder contains status/rebalance/amount/skill-pointer sections (patched CLI calls); `--dry-run` performs no side effects (no snapshot row, no telegram, monkeypatched runner not called); missing-target path exits 1 before LLM.
- [ ] **Step 2: red** · **Step 3: implement** · **Step 4: green** · **Step 5: commit** `feat(scheduler): monthly ISA briefing runner + crontab entry`

---

### Task 3: `.claude/skills/isa-briefing/SKILL.md`

**Files:**
- Create: `.claude/skills/isa-briefing/SKILL.md`

**Content requirements** (follow `daily-briefing`'s SKILL.md structure/frontmatter; Korean output like the daily briefing):
- Frontmatter description + triggers: isa, ISA 브리핑, 적립, 월 적립, 리밸런싱, ETF 적립, isa briefing, monthly contribution.
- Workflow steps, all via `bin/stock-cli`: (1) `isa status` + `isa rebalance` + `isa log --limit 5`; (2) `macro-news` (risk level is context — NOTE: the stage-24 RISK_OFF switch gates stock BULL predictions, NOT ISA contributions; long-term DCA continues through risk-off by design — the skill must say this explicitly and instead treat risk-off as a reason to keep tilts at 0, not to skip the month); (3) OPTIONAL `etf compare --query ...` when considering a cheaper same-index switch for future buys (never sells); (4) decide tilt within ±10%p with 2-3 sentence Korean rationale per tilted class, defaulting to NO tilt — tilting is the exception; (5) run `isa allocate --amount <AMOUNT> [--tilt ...]` (the CLI clamps + logs); (6) quarterly (Jan/Apr/Jul/Oct runs): include the rebalance-band section with the contribution-only remedy from `isa rebalance`; (7) compose the Korean briefing: 이번 달 적립 배분표 (per-ETF amounts + estimated shares), 포트폴리오 현황 (weights vs targets, drift), 트랙레코드 (recent snapshots + since-inception return vs benchmarks), 매크로 코멘트, 결정 로그 ID.
- Hard rules section: never skip the monthly contribution based on market narrative; tilt is capped in code; never recommend selling for rebalance (contribution-only remedies; note ISA 비과세/의무기간); everything logged.

- [ ] Write the skill · sanity-check by following it manually against the CLI (offline: --help paths exist) · commit `feat(skill): /isa-briefing monthly contribution skill`

---

### Task 4: docs

**Files:**
- Create: `docs/stage-29/isa-briefing-track-record.md`
- Modify: `docs/summary.md` (`## Stage 29 — ISA monthly briefing + track record`), `README.md` (ISA section: setup + monthly flow, brief), `CLAUDE.md` (scheduler section: one isa_briefing block mirroring the daily/outcome blocks; skills list count/groups if enumerated)

- [ ] Write docs · full suite green · commit `docs(stage-29): ISA briefing stage doc + index + README/CLAUDE`

## Self-Review Notes

- Spec coverage: monthly briefing (T2/T3), quarterly rebalance section (T3 step 6), NAV vs benchmarks (T1), tilt-decision annual evaluation is DATA-READY via isa_decisions + isa_nav (the evaluation itself is future work — noted in the stage doc, consistent with the spec's "annual after-the-fact evaluation" cadence: no data exists to evaluate yet).
- The RISK_OFF interaction (stage 24 vs ISA) is explicitly resolved: gates stocks, not DCA.
- Amount is always explicit; cron line documents where to change it.
