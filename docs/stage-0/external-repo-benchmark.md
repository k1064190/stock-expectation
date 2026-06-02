# Stage 0 — 외부 레포 벤치마킹

## Why
예측 품질이 한 달 반 만에 승률 73%→51%로 악화. 외부 오픈소스 트레이딩 시스템 3종에서
이식 가능한 개선 패턴을 찾기 위해 clone·심층 분석을 수행.

## What
- 3개 레포를 `research/external/`(gitignore)에 clone: Vibe-Trading(HKUDS), AutoHedge(Swarm Corp), FinceptTerminal(Fincept).
- 서브에이전트 3개 병렬 분석 → 레포별 보고서 `reports/external-analysis/{vibe-trading,autohedge,finceptterminal}.md`.
- 통합 합성 `reports/external-analysis/SYNTHESIS.md` — 우리 측정 결함과 매핑한 개선안 S1~S13.
- 리뷰 게이트 결과: **Tier 1(S1+S2+S3)만 Stage 1에서 구현**, 나머지는 다음 Stage로 연기.

## How
- 라이선스 확인: Vibe-Trading=MIT(코드 차용 가능), AutoHedge=MIT(아이디어 위주), FinceptTerminal=AGPL/상용(아이디어만, 코드 복사 금지).
- 적용성 기준: Python CLI + Claude 스킬 + SQLite 트랙레코드에 실제로 들어맞는 것만 채택. 웹 UI·선물/크립토 엔진·C++/Qt·ML 플랫폼은 제외.

## Code locations
- `research/external/*` (gitignore), `reports/external-analysis/*.md`, `.gitignore`(research/external 추가).

## Retrospective
- WebFetch 정찰 → clone → 병렬 서브에이전트 보고서 → 합성 흐름이 효율적. 측정된 결함(BEAR 6%, 0% 신호, confidence 상수)이 후보 우선순위를 명확히 정렬해줌.
- 연기 항목은 SYNTHESIS.md에 보존되어 다음 Stage 재진입이 쉬움.
