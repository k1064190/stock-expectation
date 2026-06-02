# Stage 1 — Tier 1 개선 (S1 신호 유의성 / S2 리스크 게이트 / S3 confidence 재보정)

## Why
측정된 핵심 결함 3종을 직격:
- valuation/cycle/mean_reversion 승률 0%인데 prune 메커니즘 없음 → **S1**
- LIVE 39% 저품질 + BEAR 6% 남발 → **S2**
- confidence ≈ 0.60 상수(정보 없음) → **S3**

출처: Vibe-Trading(MIT) 신호 IC/분류·재보정 철학, AutoHedge 리스크-퍼스트 게이트 아이디어.

## What
- **S1**: 신호별 raw 승률에 50% 귀무가설 대비 **이항검정 p-value + verdict(alive/weak/dead)** 추가. 실데이터에서 valuation/cycle/mean_reversion만 `dead`(p=0.001)로 정확히 식별, 나머지는 `weak`.
- **S2**: `/expect`·`daily-briefing` 프롬프트에 **리스크/엣지 게이트(reward:risk<1.5 등록 금지)** + **BEAR 상향 바**(≥3 signal + 매크로 확인 + RR≥2.0, LIVE는 NEUTRAL 강등) 추가.
- **S3**: calibration 곡선 → **단조 재보정 맵**(isotonic/PAVA) 적합 + 적용 함수. 실데이터 0.626→0.50으로 과신 교정. CLI `calibration` 출력에 `recalibration_map` 노출.

## How
- `_binomial_two_sided_p` (math.comb, 의존성 없음) + `_signal_verdict` → `get_signal_performance`가 p_value/verdict 반환(기본값 있는 필드 추가라 기존 consumer 호환).
- `_isotonic_nondecreasing`(PAVA) → `build_recalibration_map` / `apply_recalibration`(선형보간, 빈 맵은 identity).
- `find_worst_signals`를 verdict=="dead" 기준으로 보강(레거시 win_rate 휴리스틱 유지).
- TDD: 신규 테스트 9개(verdict 3 + recalibration 3 + weekly 1 + 기존 보강). 전체 `not network` 287 통과.

## Code locations
- `mcp-prediction-store/metrics.py:55-` (SignalPerformance + 헬퍼 + build/apply_recalibration)
- `mcp-prediction-store/tests/test_metrics.py` (S1/S3 테스트)
- `scheduler/weekly_calibration.py:99-` (find_worst_signals verdict)
- `scheduler/tests/test_weekly_calibration.py` (verdict 테스트 + _FakeSignal 필드)
- `stock_cli.py:836-` (cmd_calibration: verdict + recalibration_map 출력)
- `.claude/skills/expect/SKILL.md` Step 9 (RULE C2/C3 + 재보정), `.claude/skills/daily-briefing/SKILL.md` 품질 규칙

## Review loop (code-reviewer-pro + codex)
- **code-reviewer-pro** (Critical 1): `_binomial_two_sided_p`가 `k>n`일 때 IndexError 가능 → `n==0 or k<0 or k>n: return 1.0` 가드 추가. PAVA·보간·호환성은 정상 판정.
- **codex** (gpt-5.5/high): `+1e-12` 절대 허용오차가 큰 n에서 스케일 부정확 가능 지적 → **상대 허용오차** `observed*(1+1e-9)`로 교체(대칭 동점 유지 + 스케일 무관). Codex의 "much more likely 포함" 표현은 과장이나, 상대 허용오차가 더 견고하므로 수용.
- gemini-subagent는 이번 stage에서 생략(두 리뷰가 동일 지점에 수렴, 견고히 수정 완료).
- 수정 후 28개 테스트 통과, 실데이터 verdict 불변(valuation/cycle/mean_reversion=dead).

## Retrospective
- 저장값 의미를 안 바꾸고 필드 추가만으로 S1을 호환성 있게 확장 — 기존 4개 consumer 무수정.
- scipy 없이 exact binomial로 구현해 core 의존성 확장 회피.
- S2는 프롬프트 변경이라 자동 테스트 불가 — Stage 2의 store-계층 BEAR 하드게이트가 실질 방어선이 됨(상호보완).
