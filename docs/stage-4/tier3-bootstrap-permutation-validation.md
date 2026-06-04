# Stage 4 — Tier 3 (S7 부트스트랩 CI / S8 순열검정 / S9 JSON 스키마 검증)

> 참고: `docs/stage-4/`는 프로젝트의 기존 "Stage 4 expect-rewrite"와 공유되는 append-only 로그 디렉터리입니다. 이 문서는 외부-벤치마크 이니셔티브의 Tier 3 작업입니다.

## Why
Tier 1/2에 이어 통계적 정직성과 입력 견고성을 마무리:
- 점추정 승률만으로는 "동전과 구분되는가"를 알 수 없음 → 부트스트랩 CI (S7)
- "confidence가 실제 정보를 담는가"는 이항검정으로 못 봄 → 순열검정 (S8)
- LLM/JSON 예측을 요청만 하고 검증 안 함 → 스키마 검증 (S9)

출처: Vibe-Trading(MIT) `validation.py`(bootstrap/permutation), AutoHedge("validate, don't just request JSON").

## What
- **S7 (코드)**: `get_track_record_ci` — 승률·Brier의 부트스트랩 95% CI + `prob_better_than_coin`(시드 고정 재현가능). 실데이터: 승률 0.498 **CI95 [0.44, 0.56] → 동전과 구분 불가**(prob 0.47).
- **S8 (코드)**: `permutation_test_confidence` — HIT/MISS 라벨 셔플로 "confidence가 결과와 연관되는가" 검정(승률은 셔플 불변이므로 별개). 실데이터: observed_diff +0.0124, **p=0.002 → confidence는 약하지만 유의미하게 정보 보유**.
- **S9 (코드)**: `validate_prediction_dict` — 필수필드/enum/범위/양수 검증. API-mode `log_predictions`가 Prediction 생성 전 호출해 malformed 행을 명확한 메시지로 skip.
- 노출: `stock-cli calibration`의 `robustness` 섹션, weekly_calibration `robustness` 키.

## How
- `_closed_filter`(공유 WHERE) + `_percentile_ci`. 부트스트랩/순열 모두 `random.Random(seed)`로 결정론적.
- 순열 통계 = mean(conf|HIT) − mean(conf|MISS); 단측 p = 셔플 통계가 관측 이상인 비율.
- `validate_prediction_dict`는 에러 문자열 리스트 반환(빈 리스트=유효); enum은 기존 Market/Direction/Timeframe 재사용.
- TDD: S7 3개 + S8 2개 + S9 6개 신규. 전체 `not network` 314 통과.

## Code locations
- `mcp-prediction-store/metrics.py` — TrackRecordCI, get_track_record_ci, permutation_test_confidence, _closed_filter, _percentile_ci
- `mcp-prediction-store/models.py` — validate_prediction_dict
- `mcp-prediction-store/tests/test_metrics.py`, `tests/test_models.py`
- `scheduler/daily_briefing.py` — log_predictions 검증 게이트
- `scheduler/weekly_calibration.py` — robustness 키
- `stock_cli.py` — cmd_calibration robustness 출력

## Review loop (code-reviewer-pro + codex)
- **code-reviewer-pro**: (Critical) `_percentile_ci` 인덱스 `int(p*m)`가 내측 편향 → 표준 nearest-rank `ceil(p*m)-1`로 수정. (Warning) 순열검정이 단측이라 anti-informative confidence를 못 잡음 → **양측**(abs)으로 변경 + docstring.
- **codex (gpt-5.5/high)**: `NaN`/`Infinity` 가격이 `>0` 검사를 통과(`nan<=0`=False) → `math.isfinite` 가드 추가, entry/target/stop 공통 헬퍼로 통일. 회귀 테스트 추가.
- 수정 후 전체 315 통과. 실데이터 conf_p 0.004(양측).

## Retrospective
- S7/S8가 실데이터에서 "현재 전체 트랙레코드는 동전 수준이나 confidence는 약한 신호 보유"라는 정직한 그림을 정량화 — 향후 calibration 루프의 핵심 지표.
- S9는 store CHECK에 의존하던 암묵적 검증을 명시적·메시지화해 API-mode의 조용한 실패를 제거.
