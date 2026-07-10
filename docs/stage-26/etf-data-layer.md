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
  `--include-leverage` (leverage/inverse excluded by default), `--refresh`, `--limit`.
- `stock-cli etf info CODE` — universe row merged with 펀드보수(`fund_pay_pct`) and
  기초지수(`base_index`).
- 9 tests (8 offline + 1 network-marked live smoke) for the module, 6 in-process CLI tests.

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

## Retrospective

Probing the data source before writing the plan paid off — the cp949 quirk and pykrx
breakage were known up front, so every task went red→green on the first implementation
pass. Carry forward: keep fail-open caches CSV-simple and keep detail enrichment
non-fatal.
