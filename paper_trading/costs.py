"""Transaction cost model for the paper-trading engine.

Deliberately simple but non-zero so simulated returns are not optimistically
frictionless: a fixed slippage moves each fill against the trader, a commission
rate applies per side, and Korean sells pay the 0.18% securities transaction tax
(증권거래세). Constants are module-level so a future config can override them.
"""

from __future__ import annotations

# Slippage applied to every fill (5 bps): buys fill higher, sells fill lower.
SLIPPAGE_RATE = 0.0005

# Commission per side, as a fraction of gross notional.
US_COMMISSION_RATE = 0.0  # most US brokers are commission-free
KR_COMMISSION_RATE = 0.00015  # ~0.015% typical Korean retail brokerage

# Korean securities transaction tax, charged on the sell side only.
KR_SELL_TAX_RATE = 0.0018  # 0.18%


def _commission_rate(market: str) -> float:
    return KR_COMMISSION_RATE if market == "KR" else US_COMMISSION_RATE


def _sell_tax_rate(market: str) -> float:
    return KR_SELL_TAX_RATE if market == "KR" else 0.0


def simulate_buy(market: str, qty: float, raw_price: float) -> dict:
    """Simulate a buy fill including slippage and commission.

    Args:
        market: "US" or "KR" (selects the commission rate).
        qty: Number of shares.
        raw_price: The reference price before slippage (e.g. session open).

    Returns:
        dict with keys: fill_price, gross, commission, slippage, cash_out.
        ``cash_out`` is the positive amount of cash leaving the account.
    """
    fill_price = raw_price * (1 + SLIPPAGE_RATE)
    gross = qty * fill_price
    commission = gross * _commission_rate(market)
    slippage = qty * (fill_price - raw_price)
    cash_out = gross + commission
    return {
        "fill_price": fill_price,
        "gross": gross,
        "commission": commission,
        "slippage": slippage,
        "cash_out": cash_out,
    }


def simulate_sell(market: str, qty: float, raw_price: float) -> dict:
    """Simulate a sell fill including slippage, commission, and KR transaction tax.

    Args:
        market: "US" or "KR" (selects commission + transaction-tax rates).
        qty: Number of shares.
        raw_price: The reference price before slippage (e.g. session open).

    Returns:
        dict with keys: fill_price, gross, commission, tax, slippage, cash_in.
        ``cash_in`` is the positive amount of cash entering the account.
    """
    fill_price = raw_price * (1 - SLIPPAGE_RATE)
    gross = qty * fill_price
    commission = gross * _commission_rate(market)
    tax = gross * _sell_tax_rate(market)
    slippage = qty * (raw_price - fill_price)
    cash_in = gross - commission - tax
    return {
        "fill_price": fill_price,
        "gross": gross,
        "commission": commission,
        "tax": tax,
        "slippage": slippage,
        "cash_in": cash_in,
    }
