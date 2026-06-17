"""Position exit-action rules.

Pure decision logic for the ``position-exit-manager`` skill. Given current
holdings plus injected technical metrics, ATR, and linked predictions, it
emits an advisory action per position:

    EXIT  — hard risk-off: thesis invalidated or trailing/structural stop hit
    TRIM  — take partial profit: R:R target reached or blow-off / stack break
    ADD   — pyramid into strength: trend intact, modest gain, no contradiction
    WATCH — signals mixed or required inputs missing (never a silent HOLD)
    HOLD  — default: trend intact, nothing triggered

The function NEVER raises. Any missing input downgrades the affected rule and
falls back to WATCH rather than guessing a HOLD. TRIM/EXIT are advisory only —
this module never mutates predictions and never records trades.

All numeric outputs are JSON-serializable. Percentages (``pnl_pct``) are
human-readable percents (e.g. 9.09 for +9.09%); overextension follows the
:mod:`indicators` convention of decimals internally but is passed in already
classified as a string level.
"""

from typing import Optional

from .models import Position

# ---------------------------------------------------------------------------
# Tuning defaults (overridable per call from the CLI flags)
# ---------------------------------------------------------------------------
DEFAULT_ATR_MULT = 3.0  # chandelier: swing_high - atr_mult * ATR
DEFAULT_TP_RR = 2.0  # take-profit at realized R-multiple >= this
ADD_RSI_LOW = 45.0
ADD_RSI_HIGH = 65.0
ADD_PNL_MAX_PCT = 20.0  # ADD only while gain is in [0, +20%]
TRIM_EXTREME_PNL_PCT = 20.0  # EXTREME-overext TRIM needs pnl > +20%


def _num(value) -> Optional[float]:
    """Coerce a value to float, or None if missing/uncoercible.

    Args:
        value: Any candidate numeric (float, int, str, or None).

    Returns:
        float(value) on success, else None. Never raises.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _latest_open_prediction(preds: Optional[list]) -> Optional[dict]:
    """Pick the most recent OPEN prediction by ``created_at``.

    Args:
        preds: List of prediction dicts (keys: id, direction, status,
            target_price, stop_price, created_at), or None.

    Returns:
        The OPEN prediction with the lexicographically-largest ISO
        ``created_at``, or None when there are no OPEN predictions. (ISO-8601
        timestamps sort correctly as strings.)
    """
    if not preds:
        return None
    open_preds = [p for p in preds if str(p.get("status", "")).upper() == "OPEN"]
    if not open_preds:
        return None
    return max(open_preds, key=lambda p: str(p.get("created_at", "")))


def _latest_prediction(preds: Optional[list]) -> Optional[dict]:
    """Pick the most recent prediction by ``created_at``, any status.

    Used only for the thesis-invalidation EXIT check: if the most recent
    prediction for a ticker resolved as MISS, the thesis is dead even though
    a MISS is no longer an OPEN linkage.

    Args:
        preds: List of prediction dicts, or None.

    Returns:
        The latest prediction by ISO ``created_at``, or None.
    """
    if not preds:
        return None
    return max(preds, key=lambda p: str(p.get("created_at", "")))


def _chandelier_stop(
    swing_high: Optional[float], atr: Optional[float], atr_mult: float
) -> Optional[float]:
    """ATR chandelier trailing stop = swing_high - atr_mult * ATR.

    Args:
        swing_high: High-watermark over the fixed ~22-bar lookback.
        atr: Average True Range.
        atr_mult: ATR multiplier (default 3.0).

    Returns:
        The trailing-stop price, or None if either input is missing.
    """
    if swing_high is None or atr is None:
        return None
    return swing_high - atr_mult * atr


def _evaluate_position(
    pos: Position,
    price: Optional[float],
    metrics: dict,
    atr: Optional[float],
    pred: Optional[dict],
    latest_pred: Optional[dict],
    atr_mult: float,
    tp_rr: float,
) -> dict:
    """Run the five-rule precedence ladder for one position.

    Args:
        pos: The holding.
        price: Current market price, or None when unavailable.
        metrics: Per-ticker metric dict (current_price, ma20/50/200, rsi14,
            overextension_level, swing_high_22). May be empty.
        atr: Average True Range for the ticker, or None.
        pred: Latest OPEN linked prediction dict, or None. This is the
            ``linked_prediction`` surfaced in the result.
        latest_pred: Latest prediction overall (any status), used only for the
            thesis-invalidation EXIT (status==MISS). May equal ``pred``.
        atr_mult: Chandelier ATR multiplier.
        tp_rr: Take-profit R-multiple threshold.

    Returns:
        The per-position action dict (see module docstring for fields). The
        ``action`` is one of EXIT / TRIM / ADD / WATCH / HOLD; ``triggered_rules``
        lists the human-readable reasons that fired.
    """
    ma20 = _num(metrics.get("ma20"))
    ma50 = _num(metrics.get("ma50"))
    ma200 = _num(metrics.get("ma200"))
    rsi14 = _num(metrics.get("rsi14"))
    swing_high = _num(metrics.get("swing_high_22"))
    overext = str(metrics.get("overextension_level", "NONE")).upper()

    trailing_stop = _chandelier_stop(swing_high, atr, atr_mult)

    pnl_pct = (
        ((price - pos.avg_price) / pos.avg_price * 100.0)
        if (price is not None and pos.avg_price)
        else None
    )

    # MA stack status (used by several rules and surfaced in the result).
    ma_status: dict = {}
    if ma20 is not None:
        ma_status["ma20"] = ma20
    if ma50 is not None:
        ma_status["ma50"] = ma50
    if ma200 is not None:
        ma_status["ma200"] = ma200
        if price is not None:
            ma_status["above_ma200"] = price > ma200
    stack_intact = (
        ma20 is not None
        and ma50 is not None
        and ma200 is not None
        and ma20 > ma50 > ma200
    )
    stack_broke = ma20 is not None and ma50 is not None and ma20 < ma50

    # Linked-prediction summary (advisory; never mutated).
    linked = None
    pred_stop = None
    if pred is not None:
        pred_stop = _num(pred.get("stop_price"))
        pred_target = _num(pred.get("target_price"))
        rr_progress = None
        if (
            price is not None
            and pred_stop is not None
            and pred_target is not None
            and (pred_target - pred_stop) != 0
        ):
            rr_progress = (price - pred_stop) / (pred_target - pred_stop)
        linked = {
            "id": pred.get("id"),
            "direction": pred.get("direction"),
            "status": pred.get("status"),
            "target": pred_target,
            "stop": pred_stop,
            "rr_progress": round(rr_progress, 3) if rr_progress is not None else None,
        }

    # Effective stop for R:R = max(linked stop, ATR trailing stop).
    effective_stop = None
    for candidate in (pred_stop, trailing_stop):
        if candidate is not None:
            effective_stop = (
                candidate if effective_stop is None else max(effective_stop, candidate)
            )

    triggered: list[str] = []

    # ---- helpers for the no-data fallback -------------------------------- #
    # We have enough to evaluate the trend rules only when we have a price AND
    # at least the MA200 level (the structural backbone). Otherwise the trend
    # path is undecidable and we must WATCH, not silently HOLD.
    have_trend_inputs = price is not None and ma200 is not None

    def _result(action: str) -> dict:
        return {
            "ticker": pos.ticker,
            "qty": pos.quantity,
            "avg_price": pos.avg_price,
            "current_price": price,
            "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            "action": action,
            "triggered_rules": triggered,
            "atr": atr,
            "trailing_stop": (
                round(trailing_stop, 4) if trailing_stop is not None else None
            ),
            "ma_status": ma_status,
            "overextension_level": overext,
            "linked_prediction": linked,
            "reason_ko_hint": _reason_ko_hint(action, triggered),
        }

    # No price at all → can't evaluate anything.
    if price is None:
        triggered.append("가격 데이터 없음 (no current price)")
        return _result("WATCH")

    # ---- RULE 1: EXIT (hardest risk-off) -------------------------------- #
    if pred_stop is not None and price < pred_stop:
        triggered.append(f"price {price} < linked stop {pred_stop} (EXIT)")
    if latest_pred is not None and str(latest_pred.get("status", "")).upper() == "MISS":
        triggered.append("linked prediction status==MISS (thesis invalidated)")
    if ma200 is not None and price < ma200:
        triggered.append(f"close {price} below MA200 {round(ma200, 2)}")
    if trailing_stop is not None and price < trailing_stop:
        triggered.append(
            f"price {price} < chandelier trailing stop {round(trailing_stop, 2)}"
        )
    if triggered:
        return _result("EXIT")

    # ---- RULE 2: TRIM (take partial profit) ----------------------------- #
    realized_r = None
    if effective_stop is not None and (pos.avg_price - effective_stop) > 0:
        realized_r = (price - pos.avg_price) / (pos.avg_price - effective_stop)
    if realized_r is not None and realized_r >= tp_rr:
        triggered.append(
            f"realized R:R {round(realized_r, 2)} >= take-profit {tp_rr} (TRIM)"
        )
    if overext == "EXTREME" and pnl_pct is not None and pnl_pct > TRIM_EXTREME_PNL_PCT:
        triggered.append(
            f"overextension EXTREME with +{round(pnl_pct, 1)}% gain (blow-off TRIM)"
        )
    if stack_broke and pnl_pct is not None and pnl_pct > 0:
        triggered.append("MA-stack break (MA20<MA50) while net profitable (TRIM)")
    if triggered:
        return _result("TRIM")

    # Beyond this point the trend rules require structural inputs.
    if not have_trend_inputs:
        triggered.append("필수 지표 부족 (insufficient metrics for trend rules)")
        return _result("WATCH")

    # ---- RULE 3: ADD (pyramid into strength) ---------------------------- #
    bull_ok = pred is None or str(pred.get("direction", "")).upper() == "BULL"
    bear_contradicts = pred is not None and str(pred.get("direction", "")).upper() == (
        "BEAR"
    )
    add_ready = (
        overext == "NONE"
        and rsi14 is not None
        and ADD_RSI_LOW <= rsi14 <= ADD_RSI_HIGH
        and stack_intact
        and pnl_pct is not None
        and 0.0 <= pnl_pct <= ADD_PNL_MAX_PCT
        and bull_ok
        and not bear_contradicts
    )
    if add_ready:
        triggered.append("trend intact, modest gain, no contradiction (ADD)")
        return _result("ADD")

    # ---- RULE 5: HOLD (trend intact, nothing triggered) ----------------- #
    # If the trend backbone is intact (above MA200, no trailing breach) we
    # default to HOLD. Otherwise the picture is mixed → WATCH.
    if price > ma200 and (trailing_stop is None or price >= trailing_stop):
        triggered.append("trend intact, no exit/trim/add trigger (HOLD)")
        return _result("HOLD")

    triggered.append("mixed signals (WATCH)")
    return _result("WATCH")


def _reason_ko_hint(action: str, triggered: list) -> str:
    """Korean-first one-line hint for the action.

    Args:
        action: The chosen action enum.
        triggered: The list of triggered-rule strings (for context only).

    Returns:
        A short Korean phrase the skill can expand on. Not user-final copy —
        the skill is responsible for the full Korean narrative.
    """
    hints = {
        "EXIT": "손절/청산 검토 — 추세 또는 손절선 이탈",
        "TRIM": "익절(부분 매도) 검토 — 목표 R:R 도달 또는 과열",
        "ADD": "추가 매수 검토 — 추세 양호, 적정 수익 구간",
        "WATCH": "관망 — 신호 혼재 또는 데이터 부족",
        "HOLD": "보유 유지 — 추세 유효, 트리거 없음",
    }
    return hints.get(action, "관망")


def compute_exit_actions(
    positions: list[Position],
    current_prices: dict[str, float],
    metrics_by_ticker: dict[str, dict],
    atr_by_ticker: dict[str, float],
    open_predictions_by_ticker: dict[str, list],
    atr_mult: float = DEFAULT_ATR_MULT,
    tp_rr: float = DEFAULT_TP_RR,
) -> dict:
    """Compute advisory exit/trim/add/watch/hold actions for positions.

    Pure and total: never raises. Each position is evaluated independently
    against a fixed precedence ladder (EXIT > TRIM > ADD > WATCH > HOLD).
    Missing inputs downgrade the affected rule and fall back to WATCH.

    The chandelier high-watermark approximation: ``swing_high_22`` is the max
    high over a fixed ~22-bar lookback (about one trading month), NOT a true
    since-entry high-watermark (which would need per-position entry dates and
    full bar history). This is documented in references/exit_rules.md.

    Args:
        positions: Current holdings.
        current_prices: Map ticker -> latest price.
        metrics_by_ticker: Map ticker -> metric dict with keys current_price,
            ma20, ma50, ma200, rsi14, overextension_level, swing_high_22.
            Missing keys/tickers are tolerated.
        atr_by_ticker: Map ticker -> ATR float.
        open_predictions_by_ticker: Map ticker -> list of prediction dicts
            (keys id, direction, status, target_price, stop_price, created_at).
            Only OPEN predictions are linked; the latest by created_at wins.
        atr_mult: Chandelier ATR multiplier (default 3.0).
        tp_rr: Take-profit R-multiple threshold (default 2.0).

    Returns:
        Dict ``{"actions": [<per-position dict>, ...]}`` — one entry per
        position, JSON-serializable. See module docstring for entry fields.
    """
    actions = []
    for pos in positions:
        try:
            price = _num(current_prices.get(pos.ticker))
            metrics = metrics_by_ticker.get(pos.ticker) or {}
            atr = _num(atr_by_ticker.get(pos.ticker))
            ticker_preds = open_predictions_by_ticker.get(pos.ticker)
            pred = _latest_open_prediction(ticker_preds)
            latest_pred = _latest_prediction(ticker_preds)
            actions.append(
                _evaluate_position(
                    pos, price, metrics, atr, pred, latest_pred, atr_mult, tp_rr
                )
            )
        except Exception as e:  # never-raise contract: degrade to WATCH
            actions.append(
                {
                    "ticker": pos.ticker,
                    "qty": pos.quantity,
                    "avg_price": pos.avg_price,
                    "current_price": None,
                    "pnl_pct": None,
                    "action": "WATCH",
                    "triggered_rules": [f"evaluation error: {e}"],
                    "atr": None,
                    "trailing_stop": None,
                    "ma_status": {},
                    "overextension_level": "NONE",
                    "linked_prediction": None,
                    "reason_ko_hint": "관망 — 평가 오류",
                }
            )
    return {"actions": actions}
