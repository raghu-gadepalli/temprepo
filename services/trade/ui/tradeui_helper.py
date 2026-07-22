#!/usr/bin/env python3
"""
services/trade/ui/tradeui_helper.py

Dashboard/UI formatting helpers for trades.

This module is intentionally presentation-only. It must not create, monitor,
execute, or mutate trades. Keep trading decisions inside generator/monitor
helpers and execution decisions inside the executor.
"""

from __future__ import annotations

from typing import Any


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except Exception:
        return default


def safe_str(value: Any, default: str = "") -> str:
    try:
        return str(value) if value is not None else default
    except Exception:
        return default
