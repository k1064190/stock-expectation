# Stage 26 — KR ETF Data Layer

## Why

The ISA ETF initiative (stages 26-29) needs a reliable KR-listed ETF universe with the
metadata that drives ISA screening: asset class, tax type (국내주식형 매매차익 비과세 vs
기타형 보유기간 과세), hedge/leverage flags, AUM, NAV deviation, fee, and base index.
pykrx's ETF endpoints are currently broken (KeyError '시장' — same breakage class as the
stage-3 ticker-list incident), so a new source was needed.

## What

- `mcp-market-data/etf_kr.py` — universe fetch + classification, per-ETF detail
  enrichment, and a CSV cache with visible stale fallback.
- `stock-cli etf list` — AUM-sorted universe with `--asset-class`, `--min-aum` (억원),
  `--include-leverage` (leverage/inverse excluded by default), `--limit`.
- `stock-cli etf info CODE` — universe row merged with 펀드보수(`fund_pay_pct`) and
  기초지수(`base_index`); the code is zero-padded before lookup.
- 16 module tests (15 offline + 1 network-marked live smoke), 7 in-process CLI tests.

## How

Naver Finance is the verified source (probed 2026-07-10):
`finance.naver.com/api/sise/etfItemList.nhn` returns all ~1,141 KR ETFs as **cp949**
JSON (decoded explicitly — `r.json()` raises UnicodeDecodeError), including `etfTabCode`
which maps to asset class; tabs 1-2 without leverage tokens classify as
`domestic_equity_type` for tax. `m.stock.naver.com/api/stock/{code}/integration`
supplies `fundPay`/`etfBaseIdx` via `totalInfos`. Universe fetches rewrite
`data/etf_universe_kr.csv`; on fetch failure the stale cache is served with a visible
note (macro-news fail-open philosophy); only when both fail does
`EtfDataUnavailable` propagate. Detail fetch is pure enrichment and never raises.
추적오차 is deliberately absent (no source); stage-27 scoring downgrades on missing
metadata with a visible flag.

## Code locations

- `mcp-market-data/etf_kr.py` — `EtfInfo`, `_parse_universe`, `fetch_etf_universe`,
  `_parse_detail`, `fetch_etf_detail`, `_save_cache`/`_load_cache`, `get_etf_universe`
- `stock_cli.py` — `cmd_etf_list` / `cmd_etf_info` + `etf` subparser group (next to
  `catalyst`)
- `mcp-market-data/tests/test_etf_kr.py`, `tests/test_etf_cli.py`

## Deviations from the plan

- The plan's `test_parse_universe_basic_fields` asserted an unrounded deviation within
  1e-6, contradicting the plan's own interface spec (`deviation_pct` rounded to 3
  decimals). The test was aligned to the spec's rounding.
- `stock_cli.py` imports `etf_kr` as a module (not from-imports like `events`) so the
  planned monkeypatching of `etf_kr.get_etf_universe`/`fetch_etf_detail` works at
  call time.
- CLAUDE.md has no subcommand enumeration line; one `etf list` example was added to its
  CLI examples instead.

## Review

Round 1 (internal + Gemini/antigravity + Codex bot), addressed in
`fix(etf): address review round 1 — parse fail-open, cache-write resilience,
code normalization, token coverage`:

**Fixed**
- (Gemini blocker) `float(ret_3m)` crashed on `""` (recently listed ETFs) →
  `not in (None, "")` guard, plus general per-row robustness below.
- (Codex P2 + internal critical) `_parse_universe` ran outside the fail-open
  choke point, so one malformed row crashed the CLI instead of falling back to
  the stale cache → per-row try/except in `_parse_row` (skips malformed rows
  with a visible "skipped N malformed universe rows" note that surfaces through
  `get_etf_universe` into the CLI JSON) AND parsing moved inside the
  `fetch_etf_universe` try block.
- (Codex P2) `_save_cache` failure (read-only `data/`) killed a successful live
  fetch → wrapped in try/except; live rows are returned with a
  "cache write failed" note.
- (Gemini + Codex P3) `etf info 69500` failed to find KODEX 200 →
  `cmd_etf_info` zero-pads the code before both the universe lookup and the
  detail fetch.
- (Gemini) `(합성 H)` was not detected as hedged → suffix check relaxed to
  `endswith("H)")`.
- (internal warning) Leverage tokens extended with "곱버스" and "3배"; a live
  probe over all 1,141 names then surfaced the long-short futures pairs
  (`KODEX 200롱코스닥150숏선물`), adding "숏" as well — flagged total 107.
- (internal warning) Cache round-trip test upgraded to full dataclass equality
  (`got == rows`), future-proofing `_OPTIONAL_FLOAT_FIELDS` drift.
- (internal suggestion) Unused `--refresh` flag / `refresh` param removed
  (YAGNI — live-first is the only mode).

**Fixed with correction (evidence over reviewer)**
- The internal reviewer's "skip rows whose zfilled code isn't 6 **digits**" is
  wrong against live data: the post-2024 KRX scheme issues **alphanumeric**
  short codes (`0193T0`, `0167A0`, ...) — digits-only validation dropped 274 of
  1,141 real ETFs in the live probe. Validation is 6 alphanumeric chars.

**Dismissed**
- (Gemini nit) `decode("cp949", errors="replace")`: silent name corruption is
  worse than the designed hard-fail → visible stale-cache fallback.
- (internal suggestion) typing-introspection for `_OPTIONAL_FLOAT_FIELDS`:
  over-engineering; drift is now caught by the round-trip equality test.

Round 2 (Codex bot on `27751bf`), all 5 findings verified valid and fixed in
`fix(etf): address codex round 2 — atomic cache write, blank-price skip,
cache-corruption fail-open, code case normalization`:

- (P2) `_save_cache` truncated the previous good cache before writing, so a
  mid-write failure destroyed the only fallback (and the swallowed
  cache-write OSError hid it) → atomic write: same-directory `.tmp` file +
  `os.replace`, best-effort temp cleanup on failure.
- (P2) Blank/missing `nowVal` became price 0 via `or 0`, fabricating a ~-100%
  NAV deviation in `etf list` → a blank required price now raises inside the
  per-row try, landing the row in the skipped-rows note.
- (P2) Corrupt/schema-mismatched cache rows raised raw ValueError/KeyError
  past the CLI's `except EtfDataUnavailable` → cache-row parsing wrapped in
  `EtfDataUnavailable("etf universe cache corrupt: ...")`; both-down stays a
  controlled JSON error.
- (P3) `etf info 193t0` failed case-sensitively against `0193T0` →
  `cmd_etf_info` normalizes `.strip().zfill(6).upper()`.
- (P3) `data/etf_universe_kr.csv` added to `.gitignore` (generated runtime
  snapshot, same class as the sector-RS snapshots).

Each fix carries a regression test (atomic write via a mid-write
`DictWriter.writerow` failure; blank-price skip note; corrupt-cache
`EtfDataUnavailable` + CLI error JSON; lowercase alnum code lookup).

## Retrospective

Probing the data source before writing the plan paid off — the cp949 quirk and pykrx
breakage were known up front, so every task went red→green on the first implementation
pass. Carry forward: keep fail-open caches CSV-simple and keep detail enrichment
non-fatal.
