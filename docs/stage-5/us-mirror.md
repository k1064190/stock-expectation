# Stage 5 — US Market Mirror of Dynamic Candidate Discovery

## Why

PR #12-#16 delivered dynamic candidate discovery + theme clustering for the
KR market. The US daily-briefing path still ran the *old* hardcoded
universe: SPY/QQQ/DIA + 7 sector ETFs (XLK/XLF/XLE/XLV/XLI/XLP/XLU), no
breakout discovery, no theme cluster. So a US-side analogue of the
5/13 KR catalyst (e.g. DDOG +41% / 5d) would be invisible to the morning
briefing.

This stage ports the Stage A/B+3 architecture 1:1 to US.

## What

### Static universe
- **New** `data/us_universe.csv` — 135 curated tickers covering S&P 500
  mega-/large-/mid-caps + ADRs (TSM/ASML/BABA/JD/PDD/NIO) + broad-market
  ETFs (SPY/QQQ/DIA/IWM/VOO/VTI) + sector ETFs (XLK/XLF/XL.../SMH/SOXX/
  ARKK). 3 columns: `ticker,name,market_segment`.

### Mirror functions in `scheduler/candidate_discovery.py`
- `ANCHORS_US = (SPY, QQQ, DIA)` — broad-market ETF analogues to
  KR's (005930, 000660, 069500).
- `_normalise_us_ticker` — 1-5 uppercase letters with optional `.`/`-`
  class separator (handles BRK.B / BRK-B, BF.B).
- `_load_static_us_universe()` + `_load_static_us_universe_names()` —
  parallel to the KR helpers, returns the same `(ticker, None, None)`
  shape so `score_and_filter` accepts both markets without changes.
- `discover_us_candidates(top_n_output, ..., provider=None)` —
  end-to-end: load CSV → filter via existing `score_and_filter` →
  merge ETF anchors → sort by `|return_5d|` → truncate. Stamps
  `Candidate.market = "US"` so `format_candidates_for_prompt` branches.
- `_fill_us_names(cands)` — pure CSV lookup (no PyKRX-style HTTP
  backstop — yfinance has no ticker-name endpoint, the CSV is
  authoritative).
- `_format_usd_cap(cap_usd)` — `$T` / `$B` / `$M` units for the
  prompt's market-cap column.

### Behaviour change in `score_and_filter`
- `days=30` → `days=35` so both providers (PyKRX `get_market_ohlcv`
  returns ~26 bars for 30 calendar days, yfinance returns ~23 trading
  days for `days=30`) sit safely above the 25-bar `_vol_ratio`
  threshold. Without this, every US ticker would degenerate to
  `vol_ratio=1.0`.

### Format branching in `format_candidates_for_prompt`
- Reads `cands[0].market` to choose Korean labels (`시총`, 조/억) vs
  English labels (`cap`, `$T`/`$B`/`$M`). Header text changes from
  "KR 후보 종목 (...)" to "US Candidates (...)".

### Wiring in `scheduler/daily_briefing.py`
- `fetch_us_market_data()` (API mode) rewritten end-to-end to call
  `discover_us_candidates` + `fetch_news_for_candidates` +
  `cluster_news` + `backfill_news_counts` + `format_themes_for_prompt`.
  Each candidate line now carries `[reason]` + `news7d=N`.
- `build_claude_code_prompt("US")` (cron mode) injects
  `{candidate_block}` + `{themes_block}` + `bin/stock-cli price-batch
  {ticker_csv}`. Replaces the static "Fetch SPY, QQQ, DIA and sector
  ETFs" instruction.

### Prompt template + SKILL doc
- `scheduler/prompts/briefing_us.md`: small addition to Sector Analysis
  step pointing the LLM at the `## Active Themes` block when populated.
- `.claude/skills/daily-briefing/SKILL.md`: US section now mirrors the
  KR note about dynamic discovery + lists only the 3 anchors + 4
  sector ETFs explicitly, rather than the prior hardcoded set.

### Tests (7 new, all mocked)
- `test_us_anchors_are_three_broad_market_etfs` — anchors are SPY/QQQ/DIA.
- `test_us_ticker_validator_accepts_standard_forms` — AAPL, BRK.B,
  BRK-B, lower-case input with whitespace, single-letter (F).
- `test_us_ticker_validator_rejects_malformed` — empty, all-digits
  (KR-style), too-long, leading digit.
- `test_us_csv_loads_anchors_and_breakouts` — the shipped CSV must
  contain SPY/QQQ/DIA + NVDA/AAPL/MSFT/AMD/MU, > 100 rows.
- `test_discover_us_anchors_when_filter_empty` — anchors-only fallback
  when CSV-load returns []. All three ETFs present, `reason="anchor"`,
  `market="US"`.
- `test_discover_us_dynamic_surfaces_breakouts` — single-mover universe
  produces `reason="momentum"` survivor.
- `test_format_us_uses_english_labels_and_dollar_cap` — output uses
  "US Candidates" / "cap=$3.50T" labels; "시총" / "₩" must NOT appear.

## How

Design decisions, mirroring or diverging from KR:

1. **Static CSV as primary source** — unlike KR where the CSV is a
   fallback for a broken PyKRX bulk endpoint, US has no equivalent
   bulk endpoint at all (yfinance, FMP free tier, Finnhub all are
   per-ticker). The CSV IS the universe by design.
2. **Anchors are ETFs, not single stocks** — SPY/QQQ/DIA give the LLM
   broad-market reference at three different concentration levels (S&P
   500, NASDAQ 100, Dow 30). KR anchors are Samsung + SK Hynix +
   KODEX 200 (two single-stock leaders + one ETF), reflecting the
   chaebol-dominated KR structure.
3. **Same filter thresholds** (15% / 2x) — US daily moves are typically
   smaller, but the dynamic mid-caps that breakout (DDOG, NVDA-style
   moves) still clear 15% on 5-day windows. If too strict in practice,
   parameters are exposed as kwargs at call sites.
4. **`days=35`** — needed for `_vol_ratio`'s 25-bar window across both
   providers. KR runs unchanged because PyKRX's calendar-window
   semantics already gave ~26 bars at days=30 (now ~30 bars at days=35,
   harmless).
5. **No PyKRX-style backstop in `_fill_us_names`** — yfinance has no
   `get_ticker_name` endpoint. The CSV is authoritative. Out-of-CSV
   tickers render as "?" in the prompt (rare; only if a caller hands
   in a custom universe).
6. **Mixed-market lists not supported** — `format_candidates_for_prompt`
   branches on `cands[0].market`. Briefing pipeline never mixes KR
   and US candidates in the same list, so this is fine.

## Code locations

- `data/us_universe.csv` (NEW, 136 lines incl header)
- `scheduler/candidate_discovery.py`:
  - `ANCHORS_US` constant (after ANCHORS_KR)
  - `STATIC_US_UNIVERSE_PATH`, `_normalise_us_ticker`,
    `_load_static_us_universe`, `_load_static_us_universe_names`
    (after their KR analogues)
  - `score_and_filter`: `days=30` → `days=35` (comment updated)
  - `discover_us_candidates` (after `discover_kr_candidates`)
  - `format_candidates_for_prompt`: market branching on `cands[0].market`
  - `_format_usd_cap`, `_fill_us_names` (after the KR equivalents)
- `scheduler/daily_briefing.py`:
  - imports: add `discover_us_candidates`
  - `fetch_us_market_data()`: full rewrite
  - `build_claude_code_prompt("US")`: candidate_block + themes_block inject
- `scheduler/prompts/briefing_us.md`: + 4-line note about Active Themes
- `.claude/skills/daily-briefing/SKILL.md`: US section rewrite
- `scheduler/tests/test_candidate_discovery.py`: + 7 US-specific tests

## Verification

```bash
# Unit (all mocked, no network)
uv run pytest scheduler/tests/test_candidate_discovery.py -v
# → 30 passed (was 23 — 7 new US tests)

# Full regression
uv run pytest -m "not network" -q
# → 260 passed

# Live US discover smoke
uv run python -c "
from scheduler.candidate_discovery import discover_us_candidates
for c in discover_us_candidates(top_n_output=15):
    print(f'{c.ticker:6s} {c.name[:28]:28s} [{c.reason:8s}] '
          f'5d={c.return_5d_pct:+6.1f}% vol={c.vol_ratio_5d:.2f}x')
"
```

Smoke output (2026-05-14, post days=35 fix):
```
SPY    SPDR S&P 500 ETF             [anchor  ] 5d=  +0.0% vol=1.00x
QQQ    Invesco QQQ Trust            [anchor  ] 5d=  +0.0% vol=1.00x
DIA    SPDR Dow Jones ETF           [anchor  ] 5d=  +0.0% vol=1.00x
DDOG   Datadog                      [momentum] 5d= +41.0% vol=1.98x  ◄◄ breakout
NET    Cloudflare                   [momentum] 5d= -23.1% vol=1.82x  ◄◄ crash
PANW   Palo Alto Networks           [momentum] 5d= +20.3% vol=1.27x
MU     Micron Technology            [momentum] 5d= +19.3% vol=1.42x
CRWD   CrowdStrike                  [momentum] 5d= +18.6% vol=1.06x
```

Live cron end-to-end (`uv run python scheduler/daily_briefing.py --market US`):
- Total runtime: 5m 28s (02:00:16 → 02:05:44 KST)
- Telegram delivered ✓
- 5 new LIVE predictions logged: MU (1W+1M BULL), PANW (1W+1M BULL),
  DIA (1W BULL) — confidence 0.60-0.62 per the calibration cap (LLM
  noted 0.70-0.80 bucket was overconfident in track record)
- LLM narrative correctly identified DDOG/MU as parabolic, NET as
  self-inflicted (layoff news), PANW/CRWD as clean breakouts

## Review loop (per CLAUDE.md mandate)

세 reviewer 병렬 — `code-reviewer-pro` agent, `codex review`, `gemini`.

### code-reviewer-pro

- **Critical**: 0건.
- **[Warning] Empty-list US 헤더가 KR 한글** — 명시적 US 분기는 추가하지 않음
  (US 경로는 anchors 3개를 항상 강제 포함하므로 `cands == []` 분기에 도달
  불가). 대신 코드 주석으로 *왜 한국어 fallback이 안전한지* 명시. 미래 변경
  시 defensive 추가 가능.
- **[Suggestion 1] `score_and_filter` market 파라미터화** — APPLIED. 새
  `market: str = "KR"` 인자, `discover_us_candidates` 가 `market="US"` 명시
  전달. Post-hoc `c.market = "US"` 스탬프 루프 제거. 더 깔끔하고 미래 호출자
  (새 screener 등) 가 stamp 잊을 risk 제거.
- **[Suggestion 2] Ticker regex 더 엄격** — APPLIED. `^[A-Z][A-Z0-9.\-]{0,5}$`
  → `^[A-Z]{1,5}(?:[.\-][A-Z])?$`. 디지트 완전 거부 (US ticker는 디지트
  없음), class separator 후 letter 한 자만 허용. "A1-B" 같은 typo 거부.
- **[Suggestion 3] 유지보수 안내 docstring** — APPLIED. "Maintenance:
  refresh quarterly..." 라인 추가.
- **[Suggestion 4] Mixed-market 검증** — DEFERRED. cron 파이프라인이 시장
  분리 호출하므로 실용성 낮음.
- **[Suggestion 5] days=35 코멘트 reword** — APPLIED (이미 우리 docstring
  에 "harmless for KR" 명시되어 있음).
- **[Suggestion 6] `_format_usd_cap` 경계 테스트** — APPLIED. 5개 boundary
  assert 추가 (T/B/M/bare).

### codex

- **[P2] US scanner batch failure → anchor fallback 무력화** — APPLIED.
  `score_and_filter` 의 `provider.get_price_history_batch` 호출을 `try/except`
  로 wrap. 실패 시 빈 survivor 반환 → `discover_us_candidates` 의 anchor
  강제 포함 로직이 정상 동작. 함수 docstring 도 "never raises" 보장 명시.
- **[P2] API-mode US 2차 batch fetch guard** — APPLIED. `fetch_us_market_data`
  의 두 번째 `get_price_history_batch` 호출도 try/except 로 wrap. 실패 시
  `bars_by_ticker = {}` 로 fallback, 후보+테마 context는 계속 prompt 에 흐름.

### gemini

- **No blockers**. 4 검증 영역 모두 GREEN.
- **[Minor] 정규식 pre-compile** — APPLIED. `_US_TICKER_RE = re.compile(...)`
  모듈 레벨로 이동, 함수당 매번 컴파일 제거 (hot path: ~135 ticker × cron).
- **[Minor] BLE001 catch 의 specific error 타입** — APPLIED.
  `logger.error("failed to read %s: %s: %s", path, type(exc).__name__, exc)`
  형식으로 변경 — encoding vs permission vs malformed 구분 가능.

### Outcome

- 적용: 7건 (codex 2 P2 + code-reviewer 4 + gemini 2)
- 보류: 2건 (code-reviewer Suggestion 4 mixed-market validation, Warning
  US-empty header)
- 시장 분리 호출이 cron 의 정형 패턴이라 보류 정당.

## Retrospective

- **CSV maintenance**: 135 tickers manually curated. Like the KR CSV,
  expect quarterly refresh as S&P 500 composition changes. The Stage 3
  stale-ticker logging in `score_and_filter` automatically surfaces any
  delisted/merged tickers in the future.
- **Active Themes noise carried over**: US run produces clusters like
  `closer look stock` / `inflation playbook dividend` from the same
  general-market article echoed across SPY/QQQ/DIA. Same root cause as
  KR's "사상 최고치 마감" sliding-window clusters — same article in
  multiple ticker buckets, ngram window slides. Worth a Stage 6
  follow-up (headline dedup before cluster counting), not blocking.
- **Days=35 affects KR too**: harmless (KR previously had ~26 bars at
  days=30, now ~30 bars at days=35). No behaviour change for KR
  filter outputs.
