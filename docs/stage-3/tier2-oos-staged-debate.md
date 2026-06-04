# Stage 3 — Tier 2 (S5 OOS 신호 decay / S4 단계형 / S6 Bull·Bear·Judge)

## Why
Stage 1에서 연기한 Tier 2 구현. 측정 결함의 다음 층위를 공략:
- 정적 전체-기간 승률은 **최근 쇠퇴**를 숨김 → OOS 분할 필요 (S5)
- 단일패스 프롬프트 + 한쪽 관점 편향 → 단계형 + 적대적 디베이트 (S4/S6)

출처: Vibe-Trading(MIT) strict 팩터 게이트(train/test 분할), AutoHedge(Director→Quant→Risk), FinceptTerminal(Bull/Bear/Judge — 아이디어만).

## What
- **S5 (코드)**: `get_signal_decay` — closed 예측을 `outcome_date`로 train/test 분할, 각 구간을 50% 귀무가설로 채점 후 `confirmed_alive`/`train_only`/`reversed`/`noise` 라벨. 실데이터에서 `fundamental`·`news`가 train alive→test weak = **train_only(쇠퇴)**로 식별. CLI `calibration`에 `signal_decay`, weekly_calibration에 `decaying_signals` 노출.
- **S4 (프롬프트)**: `/expect` Step 5b에 단계 역할 명시 — Quant(결정론적 ALGO/NEWS) → Director/Judge(LLM_CONTEXT 디베이트) → Risk(Step 9 C2/C3 게이트).
- **S6 (프롬프트)**: `LLM_CONTEXT_SCORE`를 **Director thesis(선커밋) → Bull → Bear → Judge** 산출물로 재정의(새 override 채널 없음, -5..+3 범위·composite 불변). `stock-research`에도 경량 Bull/Bear/Judge 단계(3b) 추가.

## How
- `SignalDecay` 데이터클래스 + `get_signal_decay(min_count, oos_fraction=0.3)` + `_decay_label`. `_signal_verdict`(S1) 재사용. 전역 카운트 분할(시간순 1-oos_fraction 경계).
- `find_decaying_signals`(weekly_calibration)가 train_only/reversed만 surface. `compute_window`에 배선.
- S4/S6은 기존 결정론적 점수 표를 건드리지 않고 narrative 채널(Step 5b)의 추론 품질만 강화 — 적대적 bear case를 강제하고 thesis를 선커밋.
- TDD: S5 신규 테스트 4개(decay 3 + find_decaying 1). 전체 `not network` 301 통과.

## Code locations
- `mcp-prediction-store/metrics.py` — SignalDecay, get_signal_decay, _decay_label
- `mcp-prediction-store/tests/test_metrics.py` — decay 테스트
- `scheduler/weekly_calibration.py` — find_decaying_signals + compute_window 배선
- `scheduler/tests/test_weekly_calibration.py` — _FakeDecay + 테스트
- `stock_cli.py` — cmd_calibration signal_decay 출력
- `.claude/skills/expect/SKILL.md` Step 5b — Bull/Bear/Judge + 역할 명시
- `.claude/skills/stock-research/SKILL.md` Step 3b — 경량 디베이트

## Review loop (code-reviewer-pro + codex)
- **code-reviewer-pro (Critical)**: 같은 `outcome_date` 군집 시 날짜기반 경계가 train을 비워 동작 신호도 noise 오분류 → **인덱스 기반 분할**(`i >= split_idx`)로 변경. 회귀 테스트 추가.
- **codex (gpt-5.5/high)**: (1) test 표본 0인데 `train_only`로 false decay 보고(실데이터 `fundamental`) → `insufficient_oos` 가드 추가. (2) 동률 날짜 정렬 비결정성 → `ORDER BY outcome_date, created_at, id`로 안정화.
- 수정 후 실데이터 라벨 정상화(news만 train_only, 최근 표본 없는 신호는 insufficient_oos). 전체 303 통과.

## Retrospective
- S5는 S1의 `_signal_verdict`를 재사용해 작게 확장 — train/test 분할만 추가.
- S4/S6를 새 override 없이 기존 LLM_CONTEXT 채널 강화로 구현해 결정론적 composite 설계를 보존(surgical).
- 프롬프트 변경(S4/S6)은 자동 테스트 불가 — 디자인 일관성에 의존.
