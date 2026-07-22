"""Signal price/performance metrics shared by lifecycle persistence paths.

This module intentionally contains no evidence, setup, or decision logic.  It
only converts one current price into the existing signal-table analytics.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional, Tuple


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().upper()


def calculate_signal_metrics(
    *,
    existing_signal: Optional[Any],
    side: Any,
    current_price: Any,
    current_time: Any,
) -> Dict[str, Any]:
    """Calculate side-adjusted signal analytics using existing table semantics.

    Raw price extremes remain actual observed prices.  P&L and MFE/MAE are
    side-adjusted so SELL signals use the low as favourable excursion and the
    high as adverse excursion.
    """
    side_s = _value(side)
    price = _decimal(current_price)
    if price is None:
        return {}

    created_price = (
        _decimal(getattr(existing_signal, "created_price", None))
        if existing_signal is not None
        else price
    ) or price
    if created_price == 0:
        return {}
    if side_s not in {"BUY", "SELL"}:
        side_s = ""

    def pnl_for_price(observed: Decimal) -> Tuple[Decimal, Decimal]:
        if side_s == "BUY":
            pnl_value = observed - created_price
        elif side_s == "SELL":
            pnl_value = created_price - observed
        else:
            pnl_value = Decimal("0")
        pnl_pct = (pnl_value / created_price) * Decimal("100")
        return pnl_pct, pnl_value

    last_pnl_raw, last_pnl_value_raw = pnl_for_price(price)

    if existing_signal is not None:
        previous_max = _decimal(getattr(existing_signal, "max_price", None)) or created_price
        previous_min = _decimal(getattr(existing_signal, "min_price", None)) or created_price
        previous_max_time = getattr(existing_signal, "max_time", None) or current_time
        previous_min_time = getattr(existing_signal, "min_time", None) or current_time
    else:
        previous_max = created_price
        previous_min = created_price
        previous_max_time = current_time
        previous_min_time = current_time

    max_price = previous_max
    min_price = previous_min
    max_time = previous_max_time
    min_time = previous_min_time
    if price > max_price:
        max_price = price
        max_time = current_time
    if price < min_price:
        min_price = price
        min_time = current_time

    if side_s == "BUY":
        favourable_price, adverse_price = max_price, min_price
    elif side_s == "SELL":
        favourable_price, adverse_price = min_price, max_price
    else:
        favourable_price = adverse_price = price

    max_pnl_raw, max_pnl_value_raw = pnl_for_price(favourable_price)
    min_pnl_raw, min_pnl_value_raw = pnl_for_price(adverse_price)

    return {
        "last_pnl": last_pnl_raw.quantize(Decimal("0.0001")),
        "last_pnl_value": last_pnl_value_raw.quantize(Decimal("0.01")),
        "max_price": max_price.quantize(Decimal("0.01")),
        "min_price": min_price.quantize(Decimal("0.01")),
        "max_time": max_time,
        "min_time": min_time,
        "max_pnl": max_pnl_raw.quantize(Decimal("0.0001")),
        "min_pnl": min_pnl_raw.quantize(Decimal("0.0001")),
        "max_pnl_value": max_pnl_value_raw.quantize(Decimal("0.01")),
        "min_pnl_value": min_pnl_value_raw.quantize(Decimal("0.01")),
    }


__all__ = ["calculate_signal_metrics"]
