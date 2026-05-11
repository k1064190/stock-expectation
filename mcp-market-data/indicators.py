"""Technical indicator computations for horizon analysis.

Functions here are pure: they accept OHLCV bars (as dicts) and return numeric
metrics. No I/O, no provider dependencies. Reused by ``stock-cli horizon-metrics``,
the expect skill, the scheduler, and tests.

Bars input format (matches ``stock-cli price-batch`` output):
``[{"date": "YYYY-MM-DD", "open": float, "high": float, "low": float,
    "close": float, "volume": int}, ...]`` sorted oldest-first.
"""

from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Tuning constants (exposed for tests and future calibration)
# ---------------------------------------------------------------------------
# A stock that has more than doubled in a year AND is within 15% of its
# 52-week high is flagged as cycle-peak risk. Thresholds chosen to flag the
# 2026-04-14 MU case (+502% 1Y, -9.5% off ATH).
CYCLE_RISK_RETURN_1Y_THRESHOLD = 1.0  # +100% YoY
CYCLE_RISK_PCT_FROM_ATH_THRESHOLD = -0.15  # within 15% of 52W high

RSI_PERIOD = 14


@dataclass
class HorizonMetrics:
    """Multi-horizon technical snapshot for a single ticker.

    All return / percentage values are decimals (e.g. 0.032 = +3.2%). Fields
    are ``None`` when the input has insufficient bars to compute them — the
    caller should display "N/A" rather than substituting zero.

    Args:
        ticker: Stock ticker (US upper-case, KR 6-digit).
        market: "US" or "KR".
        current_price: Most recent close.
        ma20: 20-day simple moving average, or None if < 20 bars.
        ma50: 50-day simple moving average, or None if < 50 bars.
        ma200: 200-day simple moving average, or None if < 200 bars.
        rsi14: 14-period Wilder-smoothed RSI on closes, or None if < 15 bars.
        return_1w: 5-trading-day return, decimal. None if < 6 bars.
        return_1m: 21-trading-day return, decimal. None if < 22 bars.
        return_6m: 126-trading-day return, decimal. None if < 127 bars.
        return_1y: 252-trading-day return, decimal. None if < 253 bars.
        pct_from_52w_high: (current - 52W high) / 52W high, negative or zero.
            Uses all available bars up to 252 if fewer; None if no bars.
        pct_from_52w_low: (current - 52W low) / 52W low, positive or zero.
        max_drawdown_1y: Worst peak-to-trough decline over the last ~252
            bars, negative. None if < 2 bars.
        cycle_risk_flag: True when return_1y > CYCLE_RISK_RETURN_1Y_THRESHOLD
            AND pct_from_52w_high > CYCLE_RISK_PCT_FROM_ATH_THRESHOLD. Both
            components must be non-None for the flag to be True.
        vol_5d_avg: Mean trading volume over the last 5 bars. None if < 5 bars.
        vol_50d_avg: Mean trading volume over the last 50 bars. None if < 50 bars.
        vol_ratio: ``vol_5d_avg / vol_50d_avg``. None when either input is None
            or when ``vol_50d_avg`` is zero (avoid division-by-zero). The
            ``/expect`` Volume bucket awards +1.0 when this ratio > 1.3.
    """

    ticker: str
    market: str
    current_price: float
    ma20: Optional[float]
    ma50: Optional[float]
    ma200: Optional[float]
    rsi14: Optional[float]
    return_1w: Optional[float]
    return_1m: Optional[float]
    return_6m: Optional[float]
    return_1y: Optional[float]
    pct_from_52w_high: Optional[float]
    pct_from_52w_low: Optional[float]
    max_drawdown_1y: Optional[float]
    cycle_risk_flag: bool
    vol_5d_avg: Optional[float]
    vol_50d_avg: Optional[float]
    vol_ratio: Optional[float]


def compute_sma(closes: list[float], period: int) -> Optional[float]:
    """Simple moving average of the last ``period`` closes.

    Args:
        closes: List of closing prices, oldest-first.
        period: Window length.

    Returns:
        SMA of the most recent ``period`` values, or None if fewer than
        ``period`` bars are available.
    """
    if period <= 0 or len(closes) < period:
        return None
    window = closes[-period:]
    return sum(window) / period


def compute_rsi(closes: list[float], period: int = RSI_PERIOD) -> Optional[float]:
    """Wilder's Relative Strength Index.

    Uses the standard Wilder smoothing (running EMA of gains/losses with
    alpha = 1/period), seeded with the simple average of the first ``period``
    changes.

    Args:
        closes: Closing prices, oldest-first. Needs at least ``period + 1``
            bars to produce a value.
        period: RSI lookback (default 14).

    Returns:
        RSI in [0, 100], or None if fewer than ``period + 1`` bars. When
        losses are identically zero (monotonic uptrend) returns 100.0. When
        gains are identically zero returns 0.0.
    """
    if period <= 0 or len(closes) < period + 1:
        return None

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    # Seed: simple averages over the first `period` changes.
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder smoothing for the remaining changes.
    for g, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _return_over(closes: list[float], lookback: int) -> Optional[float]:
    """Decimal return between closes[-1] and closes[-1 - lookback]."""
    if lookback <= 0 or len(closes) < lookback + 1:
        return None
    past = closes[-lookback - 1]
    if past <= 0:
        return None
    return (closes[-1] - past) / past


def _max_drawdown(closes: list[float]) -> Optional[float]:
    """Worst peak-to-trough decline over the given series (negative decimal)."""
    if len(closes) < 2:
        return None
    peak = closes[0]
    worst = 0.0
    for price in closes:
        if price > peak:
            peak = price
        if peak > 0:
            dd = (price - peak) / peak
            if dd < worst:
                worst = dd
    return worst


def _close(bar) -> float:
    """Extract close from a dict or OHLCV-like object."""
    if isinstance(bar, dict):
        return float(bar["close"])
    return float(bar.close)


def _volume(bar) -> float:
    """Extract volume from a dict or OHLCV-like object."""
    if isinstance(bar, dict):
        return float(bar["volume"])
    return float(bar.volume)


def compute_avg_volume(volumes: list[float], period: int) -> Optional[float]:
    """Arithmetic mean of the trailing ``period`` volume values.

    Mirrors :func:`compute_sma` but for volume. Returns ``None`` when there
    aren't enough bars so callers can distinguish "no data" from a real zero.

    Args:
        volumes: Trading volumes, oldest-first.
        period: Window length (must be > 0).

    Returns:
        Trailing mean, or None if fewer than ``period`` bars.
    """
    if period <= 0 or len(volumes) < period:
        return None
    window = volumes[-period:]
    return sum(window) / period


def compute_horizon_metrics(
    bars: list,
    ticker: str,
    market: str,
) -> HorizonMetrics:
    """Compute all horizon metrics for a ticker from OHLCV bars.

    Args:
        bars: OHLCV bars, oldest-first. Each element may be a dict with at
            least a ``close`` key, or an OHLCV dataclass instance.
        ticker: Stock ticker (for labelling the result).
        market: "US" or "KR".

    Returns:
        HorizonMetrics. Fields that require more bars than are available
        are set to None; ``cycle_risk_flag`` is False when required inputs
        are None.

    Raises:
        ValueError: If ``bars`` is empty.
    """
    if not bars:
        raise ValueError(f"No bars supplied for {ticker}")

    closes = [_close(b) for b in bars]
    current = closes[-1]

    ma20 = compute_sma(closes, 20)
    ma50 = compute_sma(closes, 50)
    ma200 = compute_sma(closes, 200)
    rsi14 = compute_rsi(closes, RSI_PERIOD)

    return_1w = _return_over(closes, 5)
    return_1m = _return_over(closes, 21)
    return_6m = _return_over(closes, 126)
    return_1y = _return_over(closes, 252)

    # 52-week window: last 252 bars (or all of them if fewer).
    window_1y = closes[-252:]
    pct_from_52w_high: Optional[float] = None
    pct_from_52w_low: Optional[float] = None
    if window_1y:
        hi = max(window_1y)
        lo = min(window_1y)
        if hi > 0:
            pct_from_52w_high = (current - hi) / hi
        if lo > 0:
            pct_from_52w_low = (current - lo) / lo

    max_dd_1y = _max_drawdown(window_1y)

    cycle_risk_flag = (
        return_1y is not None
        and return_1y > CYCLE_RISK_RETURN_1Y_THRESHOLD
        and pct_from_52w_high is not None
        and pct_from_52w_high > CYCLE_RISK_PCT_FROM_ATH_THRESHOLD
    )

    volumes = [_volume(b) for b in bars]
    vol_5d_avg = compute_avg_volume(volumes, 5)
    vol_50d_avg = compute_avg_volume(volumes, 50)
    if vol_5d_avg is None or vol_50d_avg is None or vol_50d_avg == 0:
        vol_ratio: Optional[float] = None
    else:
        vol_ratio = vol_5d_avg / vol_50d_avg

    return HorizonMetrics(
        ticker=ticker,
        market=market,
        current_price=current,
        ma20=ma20,
        ma50=ma50,
        ma200=ma200,
        rsi14=rsi14,
        return_1w=return_1w,
        return_1m=return_1m,
        return_6m=return_6m,
        return_1y=return_1y,
        pct_from_52w_high=pct_from_52w_high,
        pct_from_52w_low=pct_from_52w_low,
        max_drawdown_1y=max_dd_1y,
        cycle_risk_flag=cycle_risk_flag,
        vol_5d_avg=vol_5d_avg,
        vol_50d_avg=vol_50d_avg,
        vol_ratio=vol_ratio,
    )
