---
name: watchlist-monitor
description: Set and monitor price-level alerts for a saved watchlist plus open predictions and portfolio positions. Fires Korean Telegram alerts when the latest close touches an entry zone, stop, target, or re-entry level. DELAYED / EOD-ish, not real-time. Use when the user wants to track entry/exit levels, set an alert, or watch tickers. Triggers on keywords like watchlist, alerts, set alert, entry zone, 워치리스트, 관심종목, 알림.
---

# Watchlist Monitor

관심종목 + 열린 예측 + 보유 포지션의 가격 레벨을 감시하고, 트리거가 발생하면
한국어 텔레그램 알림을 보내는 스킬입니다.

## ⚠️ 지연/종가 기준 — 실시간 아님 (Delayed, NOT real-time)

이 모니터는 **실시간 틱 알림이 아닙니다.** 트리거는
`provider.get_current_price()` 기준으로 평가되는데, 이 함수는
`get_price_history(days=5)`의 **마지막 종가(last close)** 를 반환합니다. 한국
시장(PyKRX)은 EOD(end-of-day) 위주입니다. 따라서 모든 "도달(touch)"은 가장
최근 확보 가능한 종가에 대한 **지연된 판독**이며, 장중 실시간 가격이 아닙니다.

- 크론은 종가 부근 체크포인트 용도로 EOD 주기로 돌립니다 (`scheduler/crontab.example`).
- 알림 문구도 "현재가(지연/종가 기준)"으로 표기되어 실시간 체결로 오해하지
  않도록 합니다.

## 데이터 소스 (3개 병합)

`load_unified_watchlist`이 `(ticker, market)` 기준으로 중복 제거하며 우선순위는
**saved > prediction > position** 입니다.

1. **saved** — 이 스킬로 저장한 행 (`data/watchlist.db`). 명시적 entry/stop/
   target/reentry 레벨.
2. **prediction** — OPEN 예측 (`predictions.db`, 읽기 전용). entry_price /
   target_price / stop_price / direction.
3. **position** — 포트폴리오 보유 종목 (`portfolio.db`). 평단가를 진입가로,
   기본 손절은 평단 × 0.92, target은 없음 (예측이 제공하면 그쪽 우선).

> 이 스킬은 **알림 전용**입니다. 예측이나 포트폴리오 데이터를 **절대 변경하지
> 않습니다.** HIT/MISS/EXPIRED 기록은 outcome tracker만 담당합니다.

## 워크플로우 (`watch add/remove/list/check`)

```bash
# 관심종목 추가 (진입 구간 + 손절 + 목표 + 재진입)
./bin/stock-cli watch add --ticker NVDA --market US \
    --entry-low 100 --entry-high 105 --stop 95 --target 130 --reentry 110 \
    --note "베이스 돌파 대기"

# BEAR 방향도 가능 (손절/목표 미러링)
./bin/stock-cli watch add --ticker TSLA --market US --direction BEAR \
    --stop 260 --target 200

# 통합 워치리스트 조회 (saved + 예측 + 포지션)
./bin/stock-cli watch list --market US

# 저장 행 삭제
./bin/stock-cli watch remove 3

# 1회 점검 실행 — 발생한 트리거를 JSON으로 출력
./bin/stock-cli watch check --market US --dry-run --force
```

- `--dry-run` : 평가만 하고 텔레그램은 보내지 않음 (상태는 정확히 갱신).
- `--force`   : 장 시간 게이트를 무시하고 강제 실행.
- `--market`  : `US` / `KR` 한쪽만. 생략하면 양쪽 모두 (각자 장 시간 게이트 적용).

## 트리거 규칙

BULL 기본, BEAR는 미러링됩니다. 가격 P 기준:

| 트리거 | BULL | BEAR | 비고 |
|--------|------|------|------|
| ENTRY  | entry_low ≤ P ≤ entry_high | 동일 (방향 무관) | saved는 구간, 예측/포지션은 진입가 ±1% 밴드 |
| STOP   | P ≤ stop | P ≥ stop | |
| TARGET | P ≥ target | P ≤ target | |
| REENTRY| reentry 아래에 있다가 다시 위로 교차 (P ≥ reentry) | 동일 | **saved 전용** |

### 중복 제거 / 재무장 (dedup / re-arm)

- 상태가 "미충족 → 충족"으로 바뀌는 **상승 엣지(rising edge)** 에서만 1회 발화.
- 충족 상태가 지속되면 재발화하지 않음. 한 번 벗어났다가 다시 진입하면 재무장.
- **6시간 쿨다운** 백스톱: 진짜 상승 엣지라도 직전 알림으로부터 6시간 이내면 억제.
- 상태는 `state/watchlist_alerts.json`에 키 `{source}:{ticker}:{market}:{trigger}`로
  저장 (원자적 쓰기: temp + `os.replace`). 사라진 항목의 키는 정리됨.

## 장 시간 게이트 (KST)

- **KR**: 09:00–15:30, 평일.
- **US**: 약 22:30–05:00 KST, 미국 거래일 기준 (자정 넘어가는 구간 처리).
- 닫혀 있으면 깔끔하게 no-op (`--force`로 무시 가능).

## 한국어 알림 형식

트리거당 한 건의 한국어 텔레그램 메시지. 4건 초과 발생 시 한 건의 묶음
다이제스트로 전송합니다 (채팅 과다 방지).

```
📥 진입 구간 도달 | NVDA (US)
현재가(지연/종가 기준): 102.30
기준선: 100.00–105.00
```

이모지: ENTRY 📥 · REENTRY 🔁 · TARGET 🎯 · STOP ⚠️

## 코드 위치

- `scheduler/watchlist_store.py` — 저장 DB + 통합 워치리스트 병합.
- `scheduler/watchlist_monitor.py` — 트리거 평가 + dedup + 게이트 + 전송.
- `scheduler/telegram_sender.py` — `send_watch_alert` (ENTRY/REENTRY 이모지).
- `stock_cli.py` — `watch add/remove/list/check` 서브커맨드.
- `scheduler/crontab.example` — EOD 주기 크론 항목.
