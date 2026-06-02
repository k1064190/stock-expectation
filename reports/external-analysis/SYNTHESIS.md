# 크로스-레포 개선안 합성 (SYNTHESIS)

생성: 2026-06-02 · 입력: `vibe-trading.md`, `autohedge.md`, `finceptterminal.md`
대상: `stock-expectation` (Python 예측 CLI + Claude 스킬 + SQLite 트랙레코드)

## 라이선스 요약 (구현 방식 제약)

| 레포 | 라이선스 | 차용 가능 범위 |
|------|----------|---------------|
| Vibe-Trading | **MIT** | **코드 차용 가능** (저작권 고지 유지). `agent/src/factors/`, `agent/backtest/validation.py`가 핵심 |
| AutoHedge | MIT | 코드 차용 가능하나 **베낄 가치 있는 코드 없음** — 프롬프트 아키텍처 *아이디어*만 |
| FinceptTerminal | **AGPL-3.0 + 상용(엄격)** | **아이디어만, 코드·프롬프트·config 일절 복사 불가** — 독립 재구현 필수 |

## 측정된 우리 결함 → 개선안 매핑

| 측정된 결함 (증거) | 직접 해결하는 개선안 |
|-------------------|---------------------|
| valuation/cycle/mean_reversion 승률 0%인데 미pruning | **S1** 유의성 검정 신호 평가, **S5** OOS 분할 |
| confidence ≈ 0.60 상수 (정보 없음) | **S3** confidence 재보정, **S7** 부트스트랩 CI |
| LIVE 39% vs INTERACTIVE 67% | **S2** 리스크/엣지 게이트, **S4** 단계형 파이프라인, **S10** 프롬프트 위생 |
| BEAR 승률 ~6% / 남발 | **S2** BEAR 엣지 바 상향 (Stage 2 store 게이트와 보완), **S6** Bull/Bear/Judge |
| 승률 50% 수렴 (랜덤과 구분 안 됨) | **S5** OOS, **S7** CI, **S8** 순열검정 |
| 단일 관점 편향 | **S6** 투자자 페르소나 렌즈 / 디베이트 |

## 마스터 우선순위 목록

### Tier 1 — 측정 결함 직격, 대부분 코드(테스트 가능), 낮은~중간 노력

- **S1. 유의성 검정 신호 평가 + 자동 prune 목록** *(Vibe, MIT 코드 차용)* — `metrics.py:get_signal_performance`의 raw 승률을 50% 귀무가설 대비 t-stat/이항검정 + `alive/weak/dead` 판정으로 교체. 학습원: `bench_runner.py:39-55` (`t_stat`,`categorise`). 닿는 곳: `mcp-prediction-store/metrics.py`, `scheduler/weekly_calibration.py`. **노력 S/M · 영향 HIGH**
- **S2. 예측 로깅 전 필수 리스크/엣지 게이트 (BEAR 바 상향)** *(AutoHedge 아이디어)* — reward:risk·엣지 점수가 임계 미만이면 WATCH/HOLD로 강등, BEAR/SELL은 더 높은 엣지 바 요구. Stage 2의 store-계층 LIVE BEAR 하드게이트와 상호보완(이쪽은 "왜/언제" 판단, store는 최종 방어선). 닿는 곳: `.claude/skills/expect/SKILL.md`, `daily-briefing/SKILL.md`. **노력 S/M · 영향 HIGH**
- **S3. calibration 곡선 기반 confidence 재보정** *(Vibe 철학 + 우리 기존 `get_calibration_report`)* — 이미 계산되나 미사용인 calibration 곡선을 단조 재보정 맵(isotonic/버킷 lookup)으로 만들어 로깅 시 raw confidence에 적용. 닿는 곳: `metrics.py`(맵 적합), 로깅 경로. **노력 M · 영향 HIGH**

### Tier 2 — 구조 개선

- **S4. /expect를 Director→Quant→Risk 단계형으로 분해** *(AutoHedge 아이디어)* — 단일 프롬프트를 명명된 단계로(thesis 확정 후 점수화 → 리스크). 학습원: `prompts.py:6-118,141-191`. 닿는 곳: `expect/SKILL.md`, 스케줄러 프롬프트. **노력 M · 영향 HIGH**
- **S5. 신호 평가에 OOS train/test 분할** *(Vibe, MIT)* — closed 예측을 `outcome_date`로 과거/최근 분할, 최근 구간에서 엣지 지속을 요구(`train_only` vs `confirmed_alive`). 학습원: `bench_runner_strict.py:469-493,242-293`. 닿는 곳: `metrics.py`, `weekly_calibration.py`. **노력 M · 영향 HIGH**
- **S6. Bull→Bear→Judge 디베이트 / 투자자 페르소나 렌즈** *(Fincept, 아이디어만 재구현)* — 같은 데이터로 최강 매수론·매도론 생성 후 judge가 구조화 결정. 또는 Buffett/Graham/Lynch/Marks 렌즈(가중 루브릭+고정 스키마)로 2-4관점 비교. 닿는 곳: `expect/SKILL.md`, `stock-research/SKILL.md`. **노력 S-M(디베이트)/M(페르소나) · 영향 HIGH**

### Tier 3 — 통계 위생 & 데이터 확장

- **S7. 승률/Brier 부트스트랩 CI** *(Vibe `validation.py:97-143`, MIT)* — "승률 0.54 [0.47,0.61], 0.50과 구분 안 됨" 식 보고. 닿는 곳: `metrics.py`. **노력 S · 영향 MED-HIGH**
- **S8. 트랙레코드 순열검정** *(Vibe `validation.py:26-79`, MIT)* — HIT 라벨 셔플로 "랜덤보다 유의하게 나은가" 검정. 닿는 곳: `metrics.py`, `weekly_calibration.py`. **노력 S · 영향 MED**
- **S9. 최종 예측 JSON 스키마 검증** *(AutoHedge 교훈: 요청만 말고 검증하라)* — 단계형 출력이 `Prediction` 필드(+`edge_score`/`risk_reward`) JSON으로 끝나고 CRUD 전 검증. 닿는 곳: `expect/SKILL.md`, 스케줄러 API모드 파서. **노력 S · 영향 MED-HIGH**
- **S10. 프롬프트 위생: 날짜 앵커 + 단계별 번호 체크리스트** *(AutoHedge `workers.py:19-24,38/50/62`)* — LIVE 스케줄러의 stale-date 추론 방지. 닿는 곳: `scheduler/daily_briefing.py`, 스킬 프롬프트. **노력 XS · 영향 LOW-MED**
- **S11. DBnomics 키-프리 매크로 커넥터** *(Fincept 아이디어, 독립 구현)* — FRED/WorldBank/IMF/OECD/ECB/BLS를 키 없이 단일 REST로. 닿는 곳: `mcp-market-data/providers/` + `stock-cli macro`. **노력 M · 영향 MED-HIGH**
- **S12. 주간 calibration 리포트 run-card** *(Vibe `run_card.py`, MIT)* — DB 해시/행수/윈도/sha256 감사 카드. 닿는 곳: `weekly_calibration.py`. **노력 S · 영향 LOW-MED**
- **S13. SEC EDGAR 무료 펀더멘털 백스톱(US)** *(Fincept 아이디어)* — FMP 무료한도 소진 시 폴백. 닿는 곳: US provider. **노력 M · 영향 MED**

## 권장 Stage 1 번들

**핵심 권장(Tier 1 = S1+S2+S3):** 셋 다 측정된 결함을 직격하고, S1/S3는 순수 코드라 TDD로 검증 가능하며, S2는 Stage 2의 BEAR store 게이트와 자연스럽게 묶임. 가장 높은 레버리지/노력 비율.

이후 여력에 따라 Tier 2(S4 단계형, S6 디베이트)와 Tier 3 통계(S5/S7/S8)를 추가 권장. S11 DBnomics·S13 EDGAR는 데이터 확장이라 별도 트랙으로 분리 권장(예측 품질과 직접 결합도 낮음).

## NOT 포팅 (명시적 제외)

Vibe: Alpha Zoo 팩터, swarm 런타임, MCP 서버, 선물/크립토 엔진, React 프론트 / `compute_ic_series`는 cross-sectional 패널 전제라 우리 sparse 단일종목 스키마에 직접 적용 불가(방법론만 차용). AutoHedge: Solana/Jupiter 실행, 크립토 봇, Swarms 의존성. Fincept: 전부 코드 복사 금지 — Qt/C++ UI, DataHub, 지정학/해상 에이전트, 유료 데이터, Qlib+RDAgent ML 스택.
