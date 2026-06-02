# Stage 2 — BEAR 부호 / store 게이트·중복 / 백필 (기존 3수정)

## Why
920건 전수 분석에서 드러난 구조적 버그·데이터 오염:
- `outcome_return` 부호 버그(BEAR HIT 음수, MISS 양수) → 모든 수익률 지표 오염
- LIVE BEAR 승률 0%인데 계속 생성 / 같은 종목 하루 18행 중복 / `analysis_group_id` 미사용

## What
- **2.1 BEAR 부호**: `outcome_return`을 포지션 수익률로 재정의(BEAR=−price_change). BULL/NEUTRAL 불변.
- **2.2 store 게이트**: `insert_prediction`이 (a) LIVE+BEAR 거부, (b) 같은 6키 OPEN 중복 거부. 부분 UNIQUE 인덱스로 동시성 백스톱.
- **2.3 group_id**: daily-briefing 프롬프트에 종목별 `--analysis-group-id` 지침 추가(expect와 동일).
- **2.4 백필**: 1회성 마이그레이션으로 과거 BEAR 부호 반전 + 동일-6키 중복 제거. 백업 후 실행.

## How
- `evaluate_prediction`: `signed = -pct_change if BEAR else pct_change`.
- 중복 키 = `(ticker, market, direction, timeframe, source, date(created_at))` — live 가드·마이그레이션·UNIQUE 인덱스 **3곳 모두 동일 키**(entry_price 제외: 가격 드리프트로 중복이 새는 것 방지).
- 마이그레이션: `BEGIN`/`rollback` 원자적 트랜잭션, `_migrations` 마커로 재실행 차단, SQLite `Connection.backup()`로 WAL 포함 일관 백업.
- TDD: outcome_tracker BEAR 테스트 3개 부호 갱신, test_models 게이트/중복 6개, test_migrate 5개. 전체 `not network` 293 통과.

## 실행 결과 (실DB)
- 920 → **732행**(중복 188개 제거), BEAR 부호 반전 38건. BEAR HIT −8.2→**+8.2**, MISS +18.45→**−20.95**. 잔여 동일-키 중복 0 검증. 백업: `data/predictions.db.bak.20260602_052332_pre_bear_dedup`.

## Code locations
- `scheduler/outcome_tracker.py:140-` (부호), `scheduler/tests/test_outcome_tracker.py`
- `mcp-prediction-store/models.py` insert_prediction 가드 + CREATE_INDEX_STMTS(부분 UNIQUE) + outcome_return docstring
- `mcp-prediction-store/tests/test_models.py`
- `scripts/migrate_bear_returns_dedup.py`, `scripts/tests/test_migrate.py`
- `.claude/skills/daily-briefing/SKILL.md` (group_id)

## Review loop (code-reviewer-pro + codex)
- **code-reviewer-pro (Critical)**: 마이그레이션 dedup 키(7필드)가 live 가드(6필드)와 불일치 → 마이그레이션을 6필드로 정렬(entry_price 제외). 이미 7필드로 실행했던 DB는 백업에서 복원 후 6필드로 재실행. **(Warning)** 원자성 → BEGIN/rollback 추가.
- **codex (gpt-5.5/high)**: (1) SELECT-후-INSERT 가드 비원자적(레이스) → 부분 UNIQUE 인덱스 백스톱 추가. (2) `shutil.copy2`가 WAL 누락 가능 → `Connection.backup()`로 교체.
- 모든 지적 수정 후 35개(models+migrate) 및 전체 293 통과.
- **codex PR 리뷰(#27)**: (P1) 마이그레이션 6키 전체-상태 dedup이 합법적 closed→reopened를 삭제 → **2단계 dedup**(OPEN은 6키, resolved는 정확-중복만)으로 분리, DB 복원 후 재실행(920→779). (P3) 부분 UNIQUE 인덱스가 미정리 OPEN 중복 DB에서 연결 brick → self-heal. **Round 2**: (P1') 레거시 스키마 마이그레이션이 빈 테이블에 인덱스 생성 후 OPEN 중복 행 INSERT→UNIQUE 실패로 brick → 인덱스 생성을 `_ensure_open_dedup_index`로 분리해 레거시 복사 **이후** 실행. (P2a) resolved dedup 키에서 `outcome_date` 제외(중복이 해소 시각만 달라 누락되던 spam 제거). (P2b) API-mode `log_predictions`가 LIVE BEAR를 명시적 skip(조용한 누락 방지). 재실행 920→770. 전체 297 통과.

## Retrospective
- 키 불일치를 리뷰가 잡아준 게 핵심 — 3곳(가드/마이그레이션/인덱스)이 단일 키로 수렴해야 dedup이 일관됨.
- 백필이 비가역이라 백업→복원→재실행 사이클로 키 변경을 안전하게 반영.
- BEAR LIVE 게이트(2.2)와 프롬프트 BEAR 바(Stage 1 S2)가 이중 방어선으로 맞물림.
