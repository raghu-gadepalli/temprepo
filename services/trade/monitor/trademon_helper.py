#!/usr/bin/env python3
"""
services/trade/monitor/trademon_helper.py

Clean Phase-1 adaptive trade-management helper.

Inputs are an existing trade, current instrument LTP, underlying/equity ATR,
and current signal metadata. Output is a compact decision/state update:
EXPAND, HOLD, PROTECT, or EXIT.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Dict, Optional

from configs.monitor_config import MONITOR_CONFIG
from enums.enums import TradePosture


def d(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")


def _cfg(name: str) -> Any:
    cfg = getattr(MONITOR_CONFIG, "trade_management", None)
    if cfg is None or not hasattr(cfg, name):
        raise AttributeError(f"MONITOR_CONFIG.trade_management.{name} is required")
    return getattr(cfg, name)


def _cfg_dec(name: str) -> Decimal:
    return d(_cfg(name))


def _json_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(d(value))
    except Exception:
        return None


def _side(v: Any) -> str:
    s = str(getattr(v, "value", v) or "BUY").upper().strip()
    return "SELL" if s == "SELL" else "BUY"


def _inst(v: Any) -> str:
    s = str(getattr(v, "value", v) or "EQ").upper().strip()
    if s in ("EQUITY", "CASH"):
        return "EQ"
    if s in ("FUTURE", "FUTURES"):
        return "FUT"
    if s in ("CALL",):
        return "CE"
    if s in ("PUT",):
        return "PE"
    return s or "EQ"


def _target_price_from_r(*, side: str, entry_price: Decimal, atr_unit: Decimal, r_multiple: Decimal) -> Optional[Decimal]:
    if entry_price <= 0 or atr_unit <= 0 or r_multiple <= 0:
        return None
    return entry_price + (atr_unit * r_multiple) if side == "BUY" else entry_price - (atr_unit * r_multiple)


def _stop_price_from_r(*, side: str, entry_price: Decimal, atr_unit: Decimal, r_multiple: Decimal) -> Optional[Decimal]:
    if entry_price <= 0 or atr_unit <= 0 or r_multiple < 0:
        return None
    return entry_price - (atr_unit * r_multiple) if side == "BUY" else entry_price + (atr_unit * r_multiple)


def _stop_price_from_profit_r(
    *,
    side: str,
    entry_price: Decimal,
    atr_unit: Decimal,
    stop_profit_r: Decimal,
) -> Optional[Decimal]:
    """Return a stop from signed profit-space R relative to executed entry.

    Positive R locks profit, zero is cost, and negative R leaves controlled
    adverse risk.  This is separate from ``_stop_price_from_r`` whose positive
    multiple always represents an adverse-side disaster stop.
    """
    if entry_price <= 0 or atr_unit <= 0:
        return None
    return (
        entry_price + (atr_unit * stop_profit_r)
        if side == "BUY"
        else entry_price - (atr_unit * stop_profit_r)
    )


def _profit_r_from_price(
    *,
    side: str,
    entry_price: Decimal,
    price: Decimal,
    atr_unit: Decimal,
) -> Optional[Decimal]:
    if entry_price <= 0 or price <= 0 or atr_unit <= 0:
        return None
    return (price - entry_price) / atr_unit if side == "BUY" else (entry_price - price) / atr_unit


def _profit_protection_cfg(name: str) -> Any:
    cfg = _cfg("profit_protection")
    if cfg is None or not hasattr(cfg, name):
        raise AttributeError(f"MONITOR_CONFIG.trade_management.profit_protection.{name} is required")
    return getattr(cfg, name)


def _group_cfg(name: str) -> Any:
    cfg = _cfg("group_management")
    if cfg is None or not hasattr(cfg, name):
        raise AttributeError(f"MONITOR_CONFIG.trade_management.group_management.{name} is required")
    return getattr(cfg, name)


def _group_cfg_dec(name: str) -> Decimal:
    return d(_group_cfg(name))


def _floor_bucket(value: Decimal, step: Decimal) -> Decimal:
    if value <= 0 or step <= 0:
        return Decimal("0")
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _stop_is_breached(*, side: str, last_price: Decimal, stop_price: Optional[Decimal]) -> bool:
    if stop_price is None or stop_price <= 0 or last_price <= 0:
        return False
    return last_price <= stop_price if side == "BUY" else last_price >= stop_price


def _profit_r(*, side: str, entry_price: Decimal, last_price: Decimal, atr_unit: Decimal) -> Decimal:
    if entry_price <= 0 or atr_unit <= 0:
        return Decimal("0")
    profit = last_price - entry_price if side == "BUY" else entry_price - last_price
    return profit / atr_unit


def _price_reached_target(*, side: str, last_price: Decimal, target_price: Decimal) -> bool:
    if last_price <= 0 or target_price <= 0:
        return False
    return last_price >= target_price if side == "BUY" else last_price <= target_price


def _lock_stop_from_target(*, side: str, previous_target: Decimal, atr_unit: Decimal) -> Optional[Decimal]:
    if previous_target <= 0 or atr_unit <= 0:
        return None
    buffer_r = _cfg_dec("target_lock_buffer_r")
    buffer = atr_unit * max(buffer_r, Decimal("0"))
    return previous_target - buffer if side == "BUY" else previous_target + buffer


def _is_better_stop(*, side: str, new_stop: Optional[Decimal], old_stop: Optional[Decimal]) -> bool:
    if new_stop is None or new_stop <= 0:
        return False
    if old_stop is None or old_stop <= 0:
        return True
    return new_stop > old_stop if side == "BUY" else new_stop < old_stop


def _is_better_target(*, side: str, new_target: Optional[Decimal], old_target: Optional[Decimal]) -> bool:
    """Return True only when the active target is improved farther in profit direction."""
    if new_target is None or new_target <= 0:
        return False
    if old_target is None or old_target <= 0:
        return True
    return new_target > old_target if side == "BUY" else new_target < old_target


def _ratchet_trade_management_levels(
    *,
    side: str,
    tm: Dict[str, Any],
    previous_tm: Dict[str, Any],
) -> Dict[str, Any]:
    """Keep adaptive levels monotonic after profit locking/target expansion.

    For BUY trades, stop and target may only move upward. For SELL trades,
    stop and target may only move downward. Expansion count is also monotonic.
    This prevents a later pullback from recomputing lower expansion levels and
    undoing profit protection.
    """
    out = dict(tm or {})
    prev_stop = d(previous_tm.get("current_stop_price") or 0)
    new_stop = d(out.get("current_stop_price") or 0)
    if prev_stop > 0 and new_stop > 0 and not _is_better_stop(side=side, new_stop=new_stop, old_stop=prev_stop):
        out["current_stop_price"] = _json_number(prev_stop)

    prev_target = d(previous_tm.get("current_target_price") or 0)
    new_target = d(out.get("current_target_price") or 0)
    if prev_target > 0 and new_target > 0 and not _is_better_target(side=side, new_target=new_target, old_target=prev_target):
        out["current_target_price"] = _json_number(prev_target)

    try:
        prev_count = int(previous_tm.get("expansion_count") or 0)
    except Exception:
        prev_count = 0
    try:
        new_count = int(out.get("expansion_count") or 0)
    except Exception:
        new_count = 0
    out["expansion_count"] = max(prev_count, new_count)
    return out


def _as_dict_payload(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if hasattr(raw, "model_dump"):
        try:
            obj = raw.model_dump(mode="python")
            return obj if isinstance(obj, dict) else {}
        except Exception:
            pass
    if hasattr(raw, "dict"):
        try:
            obj = raw.dict()
            return obj if isinstance(obj, dict) else {}
        except Exception:
            pass
    return {}


def _get_full_signal_meta(signal: Any) -> Dict[str, Any]:
    if signal is None:
        return {}
    return _as_dict_payload(getattr(signal, "meta_json", None))


def _get_signal_meta(signal: Any) -> Dict[str, Any]:
    meta = _get_full_signal_meta(signal)
    signal_meta = meta.get("signal")
    if not isinstance(signal_meta, dict):
        raise ValueError("signal.meta_json['signal'] is required for trade management")
    return signal_meta


def _active_signal_evidence(signal: Any) -> Dict[str, Any]:
    meta = _get_full_signal_meta(signal)
    active = meta.get("active_signal_evidence")
    if isinstance(active, dict):
        return active
    current = meta.get("current_evidence")
    if isinstance(current, dict) and isinstance(current.get("active_signal_evidence"), dict):
        return current.get("active_signal_evidence") or {}
    return {}


def _signal_stage(signal: Any, signal_meta: Dict[str, Any]) -> str:
    raw = signal_meta.get("stage") or getattr(signal, "stage", "")
    return str(getattr(raw, "value", raw) or "").upper().strip()


def _signal_confidence(signal_meta: Dict[str, Any]) -> Optional[Decimal]:
    val = signal_meta.get("confidence")
    if val is None:
        return None
    out = d(val)
    return out if out >= 0 else None


def _signal_quality(signal_meta: Dict[str, Any]) -> Optional[Decimal]:
    val = signal_meta.get("quality")
    if val is None:
        return None
    if isinstance(val, str):
        label = val.upper().strip()
        if label == "HIGH":
            return Decimal("80")
        if label == "MEDIUM":
            return Decimal("60")
        if label == "LOW":
            return Decimal("35")
    out = d(val)
    return out if out >= 0 else None


def extract_underlying_atr(snapshot_dict: Dict[str, Any]) -> Optional[Decimal]:
    snap = snapshot_dict or {}
    candidates = [
        (((snap.get("indicators") or {}).get("atr") or {}).get("value")),
    ]
    for value in candidates:
        val = d(value)
        if val > 0:
            return val
    return None


def instrument_atr_unit(*, instrument_type: Any, underlying_atr: Optional[Decimal]) -> Optional[Decimal]:
    atr = d(underlying_atr or 0)
    if atr <= 0:
        return None
    if _inst(instrument_type) in ("CE", "PE"):
        factor = _cfg_dec("option_atr_factor")
    else:
        factor = Decimal("1")
    unit = atr * factor
    return unit if unit > 0 else None


def initial_stop_r_for_instrument(instrument_type: Any) -> Decimal:
    """Return initial stop R by instrument, keeping option premium noise wider.

    EQ/FUT use the base stop multiple. ATM options use a wider multiple
    because premiums can retrace more than the underlying during normal
    consolidation even when the lifecycle setup remains intact.
    """
    if _inst(instrument_type) in ("CE", "PE"):
        return _cfg_dec("initial_option_stop_r_multiple")
    return _cfg_dec("initial_stop_r_multiple")


@dataclass(frozen=True)
class TradeManagementDecision:
    posture: str
    reason: str
    trade_management: Dict[str, Any]


class TradeMonHelper:
    @staticmethod
    def initialize_trade_management(
        *,
        side: Any,
        instrument_type: Any,
        entry_price: Any,
        underlying_atr: Optional[Decimal],
        asof_time: Optional[datetime] = None,
        initial_stop_price: Optional[Any] = None,
        initial_stop_source: Optional[str] = None,
        initial_stop_reason: Optional[str] = None,
        initial_target_price: Optional[Any] = None,
        initial_target_source: Optional[str] = None,
        initial_target_reason: Optional[str] = None,
        signal_setup_label: Optional[str] = None,
    ) -> Dict[str, Any]:
        side_s = _side(side)
        inst_s = _inst(instrument_type)
        entry = d(entry_price)
        atr0 = d(underlying_atr or 0)
        factor = _cfg_dec("option_atr_factor") if inst_s in ("CE", "PE") else Decimal("1")
        atr_unit = atr0 * factor if atr0 > 0 else Decimal("0")

        target_r = _cfg_dec("initial_target_r_multiple")
        stop_r = initial_stop_r_for_instrument(inst_s)

        target = _target_price_from_r(side=side_s, entry_price=entry, atr_unit=atr_unit, r_multiple=target_r)
        stop = _stop_price_from_r(side=side_s, entry_price=entry, atr_unit=atr_unit, r_multiple=stop_r)

        stop_source = str(initial_stop_source or "ATR_MULTIPLE").upper().strip()
        stop_reason = str(initial_stop_reason or "initial_stop_from_atr_multiple").strip()
        target_source = str(initial_target_source or "ATR_MULTIPLE").upper().strip()
        target_reason = str(initial_target_reason or "initial_target_from_atr_multiple").strip()

        override_stop = d(initial_stop_price) if initial_stop_price is not None else Decimal("0")
        if stop_source == "SIGNAL_SETUP_LEVEL" and override_stop <= 0:
            raise ValueError("SIGNAL_SETUP_LEVEL_STOP_MISSING_IN_TRADE_MANAGEMENT_INIT")

        if override_stop > 0:
            # Signal/setup reference level is authoritative. If the signal handoff is
            # malformed, fail loudly instead of silently falling back to ATR.
            if (side_s == "BUY" and override_stop < entry) or (side_s == "SELL" and override_stop > entry):
                stop = override_stop
                if atr_unit > 0:
                    stop_r = abs(entry - stop) / atr_unit
            elif stop_source == "SIGNAL_SETUP_LEVEL":
                raise ValueError("SIGNAL_SETUP_LEVEL_STOP_INVALID_SIDE")
            else:
                stop_source = "ATR_MULTIPLE"
                stop_reason = "initial_stop_invalid_side_using_atr_default"

        override_target = d(initial_target_price) if initial_target_price is not None else Decimal("0")
        if target_source == "SIGNAL_SETUP_TARGET" and override_target <= 0:
            raise ValueError("SIGNAL_SETUP_TARGET_MISSING_IN_TRADE_MANAGEMENT_INIT")

        if override_target > 0:
            # Signal/setup target is authoritative. If the handoff is malformed,
            # fail loudly instead of silently falling back to an ATR target.
            if (side_s == "BUY" and override_target > entry) or (side_s == "SELL" and override_target < entry):
                target = override_target
                if atr_unit > 0:
                    target_r = abs(target - entry) / atr_unit
            elif target_source == "SIGNAL_SETUP_TARGET":
                raise ValueError("SIGNAL_SETUP_TARGET_INVALID_SIDE")
            else:
                target_source = "ATR_MULTIPLE"
                target_reason = "initial_target_invalid_side_using_atr_default"

        return {
            "version": 1,
            "mode": str(_cfg("mode")),
            "price_basis": "INSTRUMENT",
            "posture": TradePosture.HOLD.value,
            "entry_price": _json_number(entry),
            "planned_entry_price": _json_number(entry),
            "atr_at_entry": _json_number(atr0) if atr0 > 0 else None,
            "instrument_atr": _json_number(atr_unit) if atr_unit > 0 else None,
            "instrument_atr_factor": _json_number(factor),
            "target_r_multiple": _json_number(target_r),
            "stop_r_multiple": _json_number(stop_r),
            "current_target_price": _json_number(target),
            "current_stop_price": _json_number(stop),
            "initial_target_price": _json_number(target),
            "initial_stop_price": _json_number(stop),
            "initial_stop_source": stop_source,
            "initial_stop_reason": stop_reason,
            "initial_target_source": target_source,
            "initial_target_reason": target_reason,
            "signal_setup_label": str(signal_setup_label or "").upper().strip() or None,
            "target_expansion_allowed": True,
            "trail_mode": "NORMAL",
            "exit_pressure": "LOW",
            "active_evidence_action": "CREATE",
            # ``risk_reduced`` is retained for compatibility with existing
            # audit/UI payloads. ``profit_protection_applied`` is authoritative.
            "risk_reduced": False,
            "profit_protection_applied": False,
            "profit_protection_trigger_mfe_r": _json_number(d(_profit_protection_cfg("trigger_mfe_r"))),
            "profit_protection_stop_profit_r": _json_number(d(_profit_protection_cfg("stop_profit_r"))),
            "current_stop_profit_r": _json_number(-stop_r),
            "group_management_enabled": bool(_group_cfg("enabled")),
            "group_role": "STANDALONE",
            "group_reference_trade_id": None,
            "group_reference_instrument": None,
            "group_reference_symbol": None,
            "group_reference_entry_price": None,
            "group_reference_atr": None,
            "group_reference_current_price": None,
            "group_mfe_r": None,
            "group_mae_r": None,
            "last_processed_group_mfe_bucket_r": None,
            "last_processed_group_mae_bucket_r": None,
            "group_stop_profit_r": _json_number(-stop_r),
            "group_target_r": _json_number(target_r),
            "group_projected_stop_price": _json_number(stop),
            "group_projected_target_price": _json_number(target),
            "group_update_source": "CREATE",
            "group_update_reason": "CREATE",
            "mae_risk_exit_required": False,
            "mae_risk_exit_observed_price": None,
            "expansion_count": 0,
            "last_target_hit_price": None,
            "last_managed_price": _json_number(entry),
            "last_updated_at": asof_time.isoformat() if hasattr(asof_time, "isoformat") else None,
            "last_update_reason": "CREATE",
        }

    @staticmethod
    def rebase_trade_management_after_fill(
        *,
        raw: Any,
        side: Any,
        instrument_type: Any,
        planned_entry_price: Any,
        executed_entry_price: Any,
        asof_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Re-initialize risk/R state from broker/virtual fill truth.

        The planned price remains an observability field only.  ATR-based
        target, stop, R, MFE and protection thresholds must use the actual fill.
        Setup-owned initial levels are preserved only when their explicit source
        says they are authoritative signal handoff levels.
        """
        old = _as_dict_payload(raw)
        requested_planned = d(planned_entry_price)
        stored_planned = d(old.get("planned_entry_price") or 0)
        # The relational entry_price may be repriced while a broker LIMIT order
        # is working.  Preserve the original trade-plan basis already stored in
        # trade_management; use the caller value only for legacy rows.
        planned = stored_planned if stored_planned > 0 else requested_planned
        executed = d(executed_entry_price)
        if executed <= 0:
            raise ValueError("TRADE_MANAGEMENT_REBASE_REQUIRES_EXECUTED_ENTRY_PRICE")

        atr0 = d(old.get("atr_at_entry") or 0)
        stop_source = str(old.get("initial_stop_source") or "ATR_MULTIPLE").upper().strip()
        target_source = str(old.get("initial_target_source") or "ATR_MULTIPLE").upper().strip()

        setup_stop = old.get("initial_stop_price") if stop_source == "SIGNAL_SETUP_LEVEL" else None
        setup_target = old.get("initial_target_price") if target_source == "SIGNAL_SETUP_TARGET" else None

        tm = TradeMonHelper.initialize_trade_management(
            side=side,
            instrument_type=instrument_type,
            entry_price=executed,
            underlying_atr=atr0 if atr0 > 0 else None,
            asof_time=asof_time,
            initial_stop_price=setup_stop,
            initial_stop_source=stop_source,
            initial_stop_reason=old.get("initial_stop_reason"),
            initial_target_price=setup_target,
            initial_target_source=target_source,
            initial_target_reason=old.get("initial_target_reason"),
            signal_setup_label=old.get("signal_setup_label"),
        )

        # Preserve only current evidence controls that may have been attached
        # between trade creation and fill. Runtime P&L/R/expansion state restarts
        # from the actual execution point.
        for key in (
            "target_expansion_allowed",
            "trail_mode",
            "exit_pressure",
            "active_evidence_action",
            "active_evidence_reason_code",
            "last_signal_stage",
            "last_signal_confidence",
            "last_signal_quality",
        ):
            if key in old:
                tm[key] = old.get(key)

        adverse_slippage = Decimal("0")
        if planned > 0:
            adverse_slippage = executed - planned if _side(side) == "BUY" else planned - executed
        atr_unit = d(tm.get("instrument_atr") or 0)

        tm.update({
            "planned_entry_price": _json_number(planned) if planned > 0 else None,
            "entry_price": _json_number(executed),
            "entry_slippage": _json_number(adverse_slippage),
            "entry_slippage_r": _json_number(adverse_slippage / atr_unit) if atr_unit > 0 else None,
            "entry_rebased_at": asof_time.isoformat() if hasattr(asof_time, "isoformat") else None,
            "entry_rebase_reason": "EXECUTED_ENTRY_PRICE",
            "last_managed_price": _json_number(executed),
            "last_update_reason": "ENTRY_REBASED_TO_EXECUTED_FILL",
        })
        return tm

    @staticmethod
    def normalize_trade_management(
        *,
        raw: Any,
        side: Any,
        instrument_type: Any,
        entry_price: Any,
        underlying_atr: Optional[Decimal],
        asof_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        tm = _as_dict_payload(raw)
        if not tm or str(tm.get("mode") or "") != str(_cfg("mode")):
            return TradeMonHelper.initialize_trade_management(
                side=side,
                instrument_type=instrument_type,
                entry_price=entry_price,
                underlying_atr=underlying_atr,
                asof_time=asof_time,
            )

        side_s = _side(side)
        entry = d(tm.get("entry_price") or entry_price)
        atr0 = d(tm.get("atr_at_entry") or underlying_atr or 0)
        atr_unit = d(tm.get("instrument_atr") or 0)
        if atr_unit <= 0:
            unit = instrument_atr_unit(instrument_type=instrument_type, underlying_atr=atr0)
            atr_unit = d(unit or 0)

        target_r = d(tm.get("target_r_multiple") or _cfg_dec("initial_target_r_multiple"))
        stop_r = abs(d(tm.get("stop_r_multiple") or initial_stop_r_for_instrument(instrument_type)))

        if tm.get("current_target_price") is None and atr_unit > 0:
            tm["current_target_price"] = _json_number(_target_price_from_r(side=side_s, entry_price=entry, atr_unit=atr_unit, r_multiple=target_r))
        if tm.get("current_stop_price") is None and atr_unit > 0:
            tm["current_stop_price"] = _json_number(_stop_price_from_r(side=side_s, entry_price=entry, atr_unit=atr_unit, r_multiple=stop_r))

        tm.setdefault("version", 1)
        tm.setdefault("price_basis", "INSTRUMENT")
        tm.setdefault("posture", TradePosture.HOLD.value)
        tm["entry_price"] = _json_number(entry)
        tm["atr_at_entry"] = _json_number(atr0) if atr0 > 0 else None
        tm["instrument_atr"] = _json_number(atr_unit) if atr_unit > 0 else None
        tm["target_r_multiple"] = _json_number(target_r)
        tm["stop_r_multiple"] = _json_number(stop_r)
        tm.setdefault("initial_stop_price", tm.get("current_stop_price"))
        tm.setdefault("initial_target_price", tm.get("current_target_price"))
        tm.setdefault("initial_stop_source", "ATR_MULTIPLE")
        tm.setdefault("initial_stop_reason", "normalized_existing_trade_management")
        tm.setdefault("initial_target_source", "ATR_MULTIPLE")
        tm.setdefault("initial_target_reason", "normalized_existing_trade_management")
        tm.setdefault("signal_setup_label", None)
        tm.setdefault("target_expansion_allowed", True)
        tm.setdefault("trail_mode", "NORMAL")
        tm.setdefault("exit_pressure", "LOW")
        tm.setdefault("active_evidence_action", None)
        protection_applied = bool(
            tm.get("profit_protection_applied", tm.get("risk_reduced", False))
        )
        tm["profit_protection_applied"] = protection_applied
        tm["risk_reduced"] = protection_applied
        tm.setdefault(
            "profit_protection_trigger_mfe_r",
            _json_number(d(_profit_protection_cfg("trigger_mfe_r"))),
        )
        tm.setdefault(
            "profit_protection_stop_profit_r",
            _json_number(d(_profit_protection_cfg("stop_profit_r"))),
        )
        current_stop = d(tm.get("current_stop_price") or 0)
        current_stop_profit_r = _profit_r_from_price(
            side=side_s,
            entry_price=entry,
            price=current_stop,
            atr_unit=atr_unit,
        )
        if current_stop_profit_r is not None:
            tm["current_stop_profit_r"] = _json_number(current_stop_profit_r)
        tm.setdefault("group_management_enabled", bool(_group_cfg("enabled")))
        tm.setdefault("group_role", "STANDALONE")
        tm.setdefault("group_reference_trade_id", None)
        tm.setdefault("group_reference_instrument", None)
        tm.setdefault("group_reference_symbol", None)
        tm.setdefault("group_reference_entry_price", None)
        tm.setdefault("group_reference_atr", None)
        tm.setdefault("group_reference_current_price", None)
        tm.setdefault("group_mfe_r", tm.get("mfe_profit_r"))
        tm.setdefault("group_mae_r", None)
        tm.setdefault("last_processed_group_mfe_bucket_r", None)
        tm.setdefault("last_processed_group_mae_bucket_r", None)
        tm.setdefault("group_stop_profit_r", tm.get("current_stop_profit_r"))
        tm.setdefault("group_target_r", tm.get("target_r_multiple"))
        tm.setdefault("group_projected_stop_price", tm.get("current_stop_price"))
        tm.setdefault("group_projected_target_price", tm.get("current_target_price"))
        tm.setdefault("group_update_source", None)
        tm.setdefault("group_update_reason", None)
        tm["mae_risk_exit_required"] = False
        tm["mae_risk_exit_observed_price"] = None
        try:
            tm["expansion_count"] = int(tm.get("expansion_count") or 0)
        except Exception:
            tm["expansion_count"] = 0
        tm.setdefault("last_target_hit_price", None)
        return tm

    @staticmethod
    def evaluate(
        *,
        trade: Any,
        signal: Any,
        side: Any,
        instrument_type: Any,
        entry_price: Any,
        last_price: Any,
        trade_management: Dict[str, Any],
        asof_time: Optional[datetime] = None,
        max_favorable_price: Any = None,
        max_adverse_price: Any = None,
        manual_trade_context: bool = False,
        group_role: str = "STANDALONE",
        group_reference_trade_id: Optional[int] = None,
        group_reference_instrument: Optional[str] = None,
        group_reference_symbol: Optional[str] = None,
    ) -> TradeManagementDecision:
        side_s = _side(side)
        last = d(last_price)
        tm = dict(trade_management or {})
        previous_tm = dict(tm)

        entry = d(tm.get("entry_price") or entry_price)
        atr_unit = d(tm.get("instrument_atr") or 0)
        target_r = d(tm.get("target_r_multiple") or _cfg_dec("initial_target_r_multiple"))
        stop_r = abs(d(tm.get("stop_r_multiple") or initial_stop_r_for_instrument(instrument_type)))

        normalized_group_role = str(group_role or "STANDALONE").upper().strip()
        group_reference_enabled = bool(_group_cfg("enabled")) and normalized_group_role == "REFERENCE"
        tm["group_management_enabled"] = bool(_group_cfg("enabled"))
        tm["group_role"] = normalized_group_role
        tm["group_reference_trade_id"] = group_reference_trade_id
        tm["group_reference_instrument"] = str(group_reference_instrument or "").upper().strip() or None
        tm["group_reference_symbol"] = str(group_reference_symbol or "").strip() or None
        tm["group_reference_entry_price"] = _json_number(entry) if group_reference_enabled else tm.get("group_reference_entry_price")
        tm["group_reference_atr"] = _json_number(atr_unit) if group_reference_enabled else tm.get("group_reference_atr")
        tm["group_reference_current_price"] = _json_number(last) if group_reference_enabled else tm.get("group_reference_current_price")
        tm["mae_risk_exit_required"] = False
        tm["mae_risk_exit_observed_price"] = None

        # Manual orders deliberately have no source Signal row.  Treat them as
        # explicit price-only standalone trades rather than fabricating empty
        # signal quality/confidence values.  Missing context for a non-manual
        # trade remains fail-loud through _get_signal_meta().
        manual_context = bool(manual_trade_context)
        if manual_context:
            signal_meta: Dict[str, Any] = {}
            active_ev: Dict[str, Any] = {}
            tm["management_context"] = "MANUAL_PRICE_ONLY"
            tm["signal_context_available"] = False
            # Manual trades can hit their stored target, but may not extend it
            # without an explicit signal lifecycle authorising continuation.
            tm["target_expansion_allowed"] = False
        else:
            signal_meta = _get_signal_meta(signal)
            active_ev = _active_signal_evidence(signal)
            tm["management_context"] = "SIGNAL_LIFECYCLE"
            tm["signal_context_available"] = True

        active_action = str(active_ev.get("active_evidence_action") or active_ev.get("evidence_action") or "").upper().strip()
        active_reason_code = str(active_ev.get("reason_code") or "").upper().strip()
        active_trail_mode = str(active_ev.get("trail_mode") or "").upper().strip()
        active_exit_pressure = str(active_ev.get("exit_pressure") or "").upper().strip()
        active_target_expansion_allowed = active_ev.get("target_expansion_allowed")
        active_should_exit = bool(active_ev.get("should_exit_signal")) or active_action == "EXIT"
        if active_action:
            tm["active_evidence_action"] = active_action
        if active_reason_code:
            tm["active_evidence_reason_code"] = active_reason_code
        if active_trail_mode:
            tm["trail_mode"] = active_trail_mode
        if active_exit_pressure:
            tm["exit_pressure"] = active_exit_pressure
        if active_target_expansion_allowed is not None:
            tm["target_expansion_allowed"] = bool(active_target_expansion_allowed)
        stage = "MANUAL" if manual_context else _signal_stage(signal, signal_meta)
        confidence = None if manual_context else _signal_confidence(signal_meta)
        quality = None if manual_context else _signal_quality(signal_meta)
        prev_conf = d(tm.get("last_signal_confidence") or confidence or 0)
        conf = d(confidence or 0)
        qual = d(quality or 0)

        posture = TradePosture.HOLD.value
        reason = "HOLD_MANUAL_PRICE_ONLY" if manual_context else "HOLD_NO_MATERIAL_CHANGE"

        if atr_unit <= 0 or entry <= 0 or last <= 0:
            posture = TradePosture.HOLD.value
            reason = "HOLD_MISSING_ATR_OR_PRICE"
        else:
            profit_r = _profit_r(side=side_s, entry_price=entry, last_price=last, atr_unit=atr_unit)
            favorable = d(max_favorable_price if max_favorable_price is not None else last)
            adverse = d(max_adverse_price if max_adverse_price is not None else last)
            mfe_profit_r = max(Decimal("0"), _profit_r(side=side_s, entry_price=entry, last_price=favorable, atr_unit=atr_unit))
            adverse_profit_r = _profit_r(side=side_s, entry_price=entry, last_price=adverse, atr_unit=atr_unit)
            mae_r = max(Decimal("0"), -adverse_profit_r)

            hard_exit_stage = (
                not manual_context
                and (stage in {"FORCE_EXIT", "EXIT_BIAS", "REVERSED", "INVALIDATED", "CLOSED"} or active_should_exit)
            )
            protect_stage = (
                not manual_context
                and (stage in {"WEAKENING", "TRANSITION", "PROTECT"} or active_action == "CAUTION")
            )
            continuation_stage = (
                not manual_context
                and stage in {"ACTIVE", "EXPAND"}
                and active_action not in {"CAUTION", "EXIT"}
            )
            expansion_allowed = False if manual_context else bool(tm.get("target_expansion_allowed", True))
            if active_action == "STRENGTHEN":
                expansion_allowed = True
                tm["target_expansion_allowed"] = True
            if active_action == "CAUTION":
                expansion_allowed = False
                tm["target_expansion_allowed"] = False

            # Price is the trigger. Signal evidence is permission.  The FUT/EQ
            # reference owns the group MFE/MAE ratchets; standalone trades retain
            # the legacy first-step behavior.
            protection_reason = None
            adverse_reason = None
            protection_enabled = bool(_profit_protection_cfg("enabled"))
            protection_trigger_r = max(d(_profit_protection_cfg("trigger_mfe_r")), Decimal("0"))
            protection_stop_profit_r = d(_profit_protection_cfg("stop_profit_r"))
            protection_applied = bool(
                tm.get("profit_protection_applied", tm.get("risk_reduced", False))
            )
            confidence_weakened = (
                prev_conf > 0 and (prev_conf - conf) >= _cfg_dec("protect_confidence_drop")
            )
            mae_weakening_confirmed = (not manual_context) and (protect_stage or confidence_weakened)
            adverse_confirmed = (
                not manual_context
                and (mae_weakening_confirmed or qual <= _cfg_dec("protect_quality_max"))
            )

            if group_reference_enabled:
                step_r = max(_group_cfg_dec("step_r"), Decimal("0"))
                tm["group_mfe_r"] = _json_number(mfe_profit_r)
                tm["group_mae_r"] = _json_number(mae_r)
                tm["group_reference_current_price"] = _json_number(last)
                tm["group_target_r"] = _json_number(target_r)

                current_stop_price = d(tm.get("current_stop_price") or 0)
                current_stop_profit_r = _profit_r_from_price(
                    side=side_s,
                    entry_price=entry,
                    price=current_stop_price,
                    atr_unit=atr_unit,
                )
                best_stop_profit_r = current_stop_profit_r
                best_stop_source = None
                best_stop_reason = None

                # First proof remains +1R -> -0.5R.  Every additional 0.5R
                # reference MFE tightens the stop by another 0.5R.
                if protection_enabled and mfe_profit_r >= protection_trigger_r:
                    mfe_bucket = protection_trigger_r
                    mfe_stop_profit_r = protection_stop_profit_r
                    if bool(_group_cfg("mfe_ratchet_enabled")) and step_r > 0:
                        completed = _floor_bucket(mfe_profit_r - protection_trigger_r, step_r)
                        mfe_bucket = protection_trigger_r + completed
                        mfe_stop_profit_r = protection_stop_profit_r + completed
                    tm["last_processed_group_mfe_bucket_r"] = _json_number(mfe_bucket)
                    if best_stop_profit_r is None or mfe_stop_profit_r > best_stop_profit_r:
                        best_stop_profit_r = mfe_stop_profit_r
                        best_stop_source = "GROUP_MFE_RATCHET"
                        best_stop_reason = (
                            f"GROUP_MFE_{mfe_profit_r:.2f}R_BUCKET_{mfe_bucket:.2f}R"
                            f"_STOP_{mfe_stop_profit_r:+.2f}R"
                        )
                    tm["profit_protection_applied"] = True
                    tm["risk_reduced"] = True

                # MAE only tightens a trade that has not demonstrated +0.5R and
                # whose signal evidence is weakening.  Select the highest reached
                # configured bucket; do not advance from repeated service polls.
                unproven_limit = max(_group_cfg_dec("unproven_mfe_max_r"), Decimal("0"))
                mae_permission = (
                    mfe_profit_r < unproven_limit
                    and (mae_weakening_confirmed or not bool(_group_cfg("mae_requires_weakening")))
                )
                if mae_permission:
                    reached_mae_bucket = Decimal("0")
                    mae_stop_profit_r = None
                    for raw_step in list(_group_cfg("mae_steps") or []):
                        threshold = d(getattr(raw_step, "mae_r", None))
                        signed_stop = d(getattr(raw_step, "stop_profit_r", None))
                        if threshold > 0 and mae_r >= threshold and threshold >= reached_mae_bucket:
                            reached_mae_bucket = threshold
                            mae_stop_profit_r = signed_stop
                    if reached_mae_bucket > 0 and mae_stop_profit_r is not None:
                        tm["last_processed_group_mae_bucket_r"] = _json_number(reached_mae_bucket)
                        if best_stop_profit_r is None or mae_stop_profit_r > best_stop_profit_r:
                            best_stop_profit_r = mae_stop_profit_r
                            best_stop_source = "GROUP_MAE_RATCHET"
                            best_stop_reason = (
                                f"GROUP_MAE_{mae_r:.2f}R_BUCKET_{reached_mae_bucket:.2f}R"
                                f"_MFE_{mfe_profit_r:.2f}R_STOP_{mae_stop_profit_r:+.2f}R"
                                f"_STAGE_{stage}_CONF_{conf}_QUALITY_{qual}"
                            )

                if best_stop_profit_r is not None and best_stop_source:
                    candidate_stop = _stop_price_from_profit_r(
                        side=side_s,
                        entry_price=entry,
                        atr_unit=atr_unit,
                        stop_profit_r=best_stop_profit_r,
                    )
                    old_stop = d(tm.get("current_stop_price") or 0)
                    if _is_better_stop(side=side_s, new_stop=candidate_stop, old_stop=old_stop):
                        tm["current_stop_price"] = _json_number(candidate_stop)
                        tm["current_stop_profit_r"] = _json_number(best_stop_profit_r)
                        tm["stop_r_multiple"] = _json_number(abs(best_stop_profit_r))
                        tm["group_stop_profit_r"] = _json_number(best_stop_profit_r)
                        tm["group_update_source"] = best_stop_source
                        tm["group_update_reason"] = best_stop_reason
                        stop_r = abs(best_stop_profit_r)
                        if best_stop_source == "GROUP_MFE_RATCHET":
                            protection_reason = best_stop_reason
                        else:
                            adverse_reason = best_stop_reason
                            if _stop_is_breached(side=side_s, last_price=last, stop_price=candidate_stop):
                                # The newly calculated MAE stop did not exist before
                                # this observation, so replay/live must exit at the
                                # observed market price rather than claim a trigger fill.
                                tm["mae_risk_exit_required"] = True
                                tm["mae_risk_exit_observed_price"] = _json_number(last)
                                adverse_reason = f"{adverse_reason};EXIT_AT_OBSERVED_PRICE_NEW_STOP_ALREADY_BREACHED"
                    elif best_stop_source == "GROUP_MFE_RATCHET":
                        protection_reason = f"{best_stop_reason};ALREADY_SATISFIED"
                    else:
                        adverse_reason = f"{best_stop_reason};ALREADY_SATISFIED"

            else:
                # Standalone compatibility path. Group-managed followers are not
                # evaluated here; the monitor projects reference levels to them.
                if protection_enabled and mfe_profit_r >= protection_trigger_r and not protection_applied:
                    new_stop = _stop_price_from_profit_r(
                        side=side_s,
                        entry_price=entry,
                        atr_unit=atr_unit,
                        stop_profit_r=protection_stop_profit_r,
                    )
                    old_stop = d(tm.get("current_stop_price") or 0)
                    if _is_better_stop(side=side_s, new_stop=new_stop, old_stop=old_stop):
                        tm["current_stop_price"] = _json_number(new_stop)
                        tm["current_stop_profit_r"] = _json_number(protection_stop_profit_r)
                        tm["stop_r_multiple"] = _json_number(abs(protection_stop_profit_r))
                        stop_r = abs(protection_stop_profit_r)
                        protection_reason = (
                            f"PROFIT_PROTECTION_AFTER_MFE_{mfe_profit_r:.2f}R"
                            f"_STOP_{protection_stop_profit_r:+.2f}R_CURRENT_{profit_r:.2f}R"
                        )
                    else:
                        protection_reason = (
                            f"PROFIT_PROTECTION_ALREADY_SATISFIED_AFTER_MFE_{mfe_profit_r:.2f}R"
                        )
                    tm["profit_protection_applied"] = True
                    tm["risk_reduced"] = True

                adverse_threshold = -abs(_cfg_dec("adverse_tighten_profit_r"))
                adverse_stop_r = _cfg_dec("adverse_tighten_stop_r_multiple")
                if profit_r <= adverse_threshold and adverse_confirmed and adverse_stop_r > 0 and adverse_stop_r < stop_r:
                    new_stop = _stop_price_from_r(
                        side=side_s,
                        entry_price=entry,
                        atr_unit=atr_unit,
                        r_multiple=adverse_stop_r,
                    )
                    old_stop = d(tm.get("current_stop_price") or 0)
                    if _is_better_stop(side=side_s, new_stop=new_stop, old_stop=old_stop):
                        stop_r = adverse_stop_r
                        tm["stop_r_multiple"] = _json_number(stop_r)
                        tm["current_stop_price"] = _json_number(new_stop)
                        adverse_reason = f"ADVERSE_TIGHTEN_{profit_r:.2f}R_STAGE_{stage}_CONF_{conf}_QUALITY_{qual}"

            if hard_exit_stage:
                posture = TradePosture.EXIT.value
                reason = f"EXIT_ACTIVE_EVIDENCE_{active_reason_code or active_action}" if active_should_exit else f"EXIT_STAGE_{stage}"
            elif expansion_allowed and continuation_stage and conf >= _cfg_dec("expand_confidence_min") and qual >= _cfg_dec("expand_quality_min") and profit_r >= _cfg_dec("expand_ready_profit_r"):
                # EXPAND is permission only. Actual target/stop changes happen
                # only when price hits the current target.
                posture = TradePosture.EXPAND.value
                reason = f"EXPAND_READY_{stage}_CONF_{conf}_QUALITY_{qual}_PROFIT_R_{profit_r:.2f}"
                if protection_reason:
                    reason = f"{reason};{protection_reason}"
            elif adverse_reason:
                posture = TradePosture.PROTECT.value
                reason = adverse_reason
            elif (
                not manual_context
                and (
                    protect_stage
                    or qual <= _cfg_dec("protect_quality_max")
                    or (prev_conf > 0 and (prev_conf - conf) >= _cfg_dec("protect_confidence_drop"))
                )
            ):
                if group_reference_enabled:
                    # Group MFE/MAE ratchets above are the only reference-stop
                    # writers. Signal weakening changes posture/permission but
                    # cannot run the older adverse-space profit-stop formula.
                    reason = (
                        f"PROTECT_GROUP_REFERENCE_PROFIT_STAGE_{stage}_CONF_{conf}_QUALITY_{qual}"
                        if profit_r > 0
                        else f"PROTECT_GROUP_REFERENCE_WEAKENING_STAGE_{stage}_CONF_{conf}_QUALITY_{qual}"
                    )
                elif profit_r > 0:
                    desired_stop_r = max(Decimal("0"), profit_r - _cfg_dec("protect_buffer_r"))
                    stepped_stop_r = stop_r + _cfg_dec("stop_tighten_r_step")
                    new_stop_r = max(stop_r, min(desired_stop_r, stepped_stop_r))
                    candidate_stop = _stop_price_from_r(side=side_s, entry_price=entry, atr_unit=atr_unit, r_multiple=new_stop_r)
                    old_stop = d(tm.get("current_stop_price") or 0)
                    if _is_better_stop(side=side_s, new_stop=candidate_stop, old_stop=old_stop):
                        tm["stop_r_multiple"] = _json_number(new_stop_r)
                        tm["current_stop_price"] = _json_number(candidate_stop)
                    reason = f"PROTECT_PROFIT_STAGE_{stage}_CONF_{conf}_QUALITY_{qual}"
                else:
                    reason = f"PROTECT_SIGNAL_WEAKENING_STAGE_{stage}_CONF_{conf}_QUALITY_{qual}"
                posture = TradePosture.PROTECT.value
            elif protection_reason:
                posture = TradePosture.HOLD.value
                reason = (
                    f"MANUAL_PRICE_ONLY;{protection_reason}"
                    if manual_context
                    else protection_reason
                )

            if protection_reason and protection_reason not in reason:
                reason = f"{reason};{protection_reason}"

        tm["posture"] = posture
        try:
            tm["mfe_profit_r"] = _json_number(mfe_profit_r)
            tm["current_profit_r"] = _json_number(profit_r)
            if max_favorable_price is not None:
                tm["max_favorable_price"] = _json_number(d(max_favorable_price))
        except Exception:
            pass
        current_stop = d(tm.get("current_stop_price") or 0)
        current_stop_profit_r = _profit_r_from_price(
            side=side_s,
            entry_price=entry,
            price=current_stop,
            atr_unit=atr_unit,
        )
        if current_stop_profit_r is not None:
            tm["current_stop_profit_r"] = _json_number(current_stop_profit_r)
            if group_reference_enabled:
                tm["group_stop_profit_r"] = _json_number(current_stop_profit_r)
        if group_reference_enabled:
            tm["group_target_r"] = tm.get("target_r_multiple")
            tm["group_projected_stop_price"] = tm.get("current_stop_price")
            tm["group_projected_target_price"] = tm.get("current_target_price")
        tm["last_managed_price"] = _json_number(last)
        if stage:
            tm["last_signal_stage"] = stage
        if confidence is not None:
            tm["last_signal_confidence"] = _json_number(confidence)
        if quality is not None:
            tm["last_signal_quality"] = _json_number(quality)
        tm["last_updated_at"] = asof_time.isoformat() if hasattr(asof_time, "isoformat") else None
        tm["last_update_reason"] = reason
        tm = _ratchet_trade_management_levels(side=side_s, tm=tm, previous_tm=previous_tm)

        return TradeManagementDecision(posture=posture, reason=reason, trade_management=tm)


    @staticmethod
    def project_group_reference_to_follower(
        *,
        reference_trade_management: Dict[str, Any],
        follower_trade_management: Dict[str, Any],
        side: Any,
        instrument_type: Any,
        entry_price: Any,
        underlying_atr: Optional[Decimal],
        reference_trade_id: Optional[int],
        reference_instrument: str,
        reference_symbol: str,
        asof_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Project one FUT/EQ reference state onto a sibling instrument.

        The reference owns the decision.  The sibling receives proportional
        stop/target prices using its frozen ATR unit, but the monitor does not
        let the follower independently trigger group target/stop exits.
        """
        ref = dict(reference_trade_management or {})
        tm = dict(follower_trade_management or {})
        side_s = _side(side)
        entry = d(tm.get("entry_price") or entry_price)
        atr0 = d(tm.get("atr_at_entry") or underlying_atr or 0)
        atr_unit = d(tm.get("instrument_atr") or 0)
        if atr_unit <= 0:
            atr_unit = d(instrument_atr_unit(instrument_type=instrument_type, underlying_atr=atr0) or 0)
        if entry <= 0 or atr_unit <= 0:
            raise ValueError("GROUP_FOLLOWER_PROJECTION_REQUIRES_ENTRY_AND_FROZEN_ATR")

        stop_profit_r = d(ref.get("group_stop_profit_r") if ref.get("group_stop_profit_r") is not None else ref.get("current_stop_profit_r"))
        target_r = d(ref.get("group_target_r") or ref.get("target_r_multiple") or 0)
        if target_r <= 0:
            raise ValueError("GROUP_FOLLOWER_PROJECTION_REQUIRES_REFERENCE_TARGET_R")

        mapped_stop = _stop_price_from_profit_r(
            side=side_s, entry_price=entry, atr_unit=atr_unit, stop_profit_r=stop_profit_r
        )
        mapped_target = _target_price_from_r(
            side=side_s, entry_price=entry, atr_unit=atr_unit, r_multiple=target_r
        )
        old_stop = d(tm.get("current_stop_price") or 0)
        if _is_better_stop(side=side_s, new_stop=mapped_stop, old_stop=old_stop):
            tm["current_stop_price"] = _json_number(mapped_stop)
            tm["current_stop_profit_r"] = _json_number(stop_profit_r)
            tm["stop_r_multiple"] = _json_number(abs(stop_profit_r))

        # Followers never decide the group target, so keep the projected target
        # exactly aligned with the reference R rather than preserving an older
        # independent option target.
        tm["current_target_price"] = _json_number(mapped_target)
        tm["target_r_multiple"] = _json_number(target_r)
        tm["posture"] = str(ref.get("posture") or TradePosture.HOLD.value)
        tm["target_expansion_allowed"] = bool(ref.get("target_expansion_allowed", True))
        tm["group_management_enabled"] = True
        tm["group_role"] = "FOLLOWER"
        tm["group_reference_trade_id"] = reference_trade_id
        tm["group_reference_instrument"] = str(reference_instrument or "").upper().strip() or None
        tm["group_reference_symbol"] = str(reference_symbol or "").strip() or None
        tm["group_reference_entry_price"] = ref.get("group_reference_entry_price") or ref.get("entry_price")
        tm["group_reference_atr"] = ref.get("group_reference_atr") or ref.get("instrument_atr")
        tm["group_reference_current_price"] = ref.get("group_reference_current_price") or ref.get("last_managed_price")
        tm["group_mfe_r"] = ref.get("group_mfe_r") or ref.get("mfe_profit_r")
        tm["group_mae_r"] = ref.get("group_mae_r")
        tm["last_processed_group_mfe_bucket_r"] = ref.get("last_processed_group_mfe_bucket_r")
        tm["last_processed_group_mae_bucket_r"] = ref.get("last_processed_group_mae_bucket_r")
        tm["group_stop_profit_r"] = _json_number(stop_profit_r)
        tm["group_target_r"] = _json_number(target_r)
        tm["group_projected_stop_price"] = _json_number(mapped_stop)
        tm["group_projected_target_price"] = _json_number(mapped_target)
        tm["profit_protection_applied"] = bool(ref.get("profit_protection_applied", False))
        tm["risk_reduced"] = bool(ref.get("risk_reduced", tm["profit_protection_applied"]))
        try:
            tm["expansion_count"] = int(ref.get("expansion_count") or 0)
        except Exception:
            tm["expansion_count"] = 0
        tm["last_target_hit_price"] = ref.get("last_target_hit_price")
        tm["group_update_source"] = "REFERENCE_PROJECTION"
        tm["group_update_reason"] = str(ref.get("group_update_reason") or ref.get("last_update_reason") or "REFERENCE_STATE")
        tm["mae_risk_exit_required"] = False
        tm["mae_risk_exit_observed_price"] = None
        tm["last_updated_at"] = asof_time.isoformat() if hasattr(asof_time, "isoformat") else None
        tm["last_update_reason"] = (
            f"FOLLOW_GROUP_REFERENCE_{reference_instrument}_{reference_trade_id}:"
            f"{tm['group_update_reason']}"
        )
        return _ratchet_trade_management_levels(side=side_s, tm=tm, previous_tm=follower_trade_management or {})


    @staticmethod
    def expand_after_target_hit(
        *,
        side: Any,
        entry_price: Any,
        last_price: Any,
        trade_management: Dict[str, Any],
        asof_time: Optional[datetime] = None,
    ) -> TradeManagementDecision:
        """Expand target after current target is actually hit.

        Lifecycle posture grants permission (EXPAND), but price is the trigger.
        If the current price has already moved beyond the next expanded target,
        keep expanding within this same monitor tick until the active target is
        ahead of the current price. Each completed target becomes the new
        profit-lock stop with the configured 0.5R buffer.
        """
        side_s = _side(side)
        tm = dict(trade_management or {})
        previous_tm = dict(tm)
        if tm.get("target_expansion_allowed") is False:
            reason = "TARGET_HIT_EXPAND_SKIPPED_TARGET_EXPANSION_DISABLED_BY_EVIDENCE"
            tm["last_update_reason"] = reason
            tm["posture"] = TradePosture.PROTECT.value
            return TradeManagementDecision(posture=TradePosture.PROTECT.value, reason=reason, trade_management=tm)
        entry = d(tm.get("entry_price") or entry_price)
        last = d(last_price)
        atr_unit = d(tm.get("instrument_atr") or 0)
        current_target = d(tm.get("current_target_price") or 0)
        target_r = d(tm.get("target_r_multiple") or _cfg_dec("initial_target_r_multiple"))

        if atr_unit <= 0 or entry <= 0 or current_target <= 0:
            reason = "TARGET_HIT_EXPAND_SKIPPED_MISSING_PRICE_OR_ATR"
            tm["last_update_reason"] = reason
            return TradeManagementDecision(posture=str(tm.get("posture") or TradePosture.HOLD.value), reason=reason, trade_management=tm)

        if not _price_reached_target(side=side_s, last_price=last, target_price=current_target):
            reason = "TARGET_HIT_EXPAND_SKIPPED_TARGET_NOT_REACHED"
            tm["last_update_reason"] = reason
            return TradeManagementDecision(posture=str(tm.get("posture") or TradePosture.HOLD.value), reason=reason, trade_management=tm)

        step = max(_cfg_dec("target_expand_r_step"), Decimal("0"))
        if step <= 0:
            reason = "TARGET_HIT_EXPAND_SKIPPED_ZERO_STEP"
            tm["last_update_reason"] = reason
            return TradeManagementDecision(posture=TradePosture.HOLD.value, reason=reason, trade_management=tm)

        old_stop = d(tm.get("current_stop_price") or 0)
        completed_target = current_target
        new_stop = _lock_stop_from_target(side=side_s, previous_target=completed_target, atr_unit=atr_unit)
        expansions_this_tick = 0

        # No business max-expansion cap is imposed. The range guard only
        # prevents an accidental infinite loop if config/input is corrupt.
        for _ in range(50):
            new_target_r = target_r + step
            new_target = _target_price_from_r(
                side=side_s,
                entry_price=entry,
                atr_unit=atr_unit,
                r_multiple=new_target_r,
            )
            if new_target is None or new_target <= 0 or new_target == current_target:
                break

            target_r = new_target_r
            current_target = new_target
            expansions_this_tick += 1

            if _price_reached_target(side=side_s, last_price=last, target_price=current_target):
                # Price has already crossed the newly-created target too.
                # Treat it as another completed milestone and continue to
                # create the next active target.
                completed_target = current_target
                candidate_stop = _lock_stop_from_target(side=side_s, previous_target=completed_target, atr_unit=atr_unit)
                if _is_better_stop(side=side_s, new_stop=candidate_stop, old_stop=new_stop):
                    new_stop = candidate_stop
                continue

            # current_target is now ahead of the latest price; keep it active.
            break

        if expansions_this_tick <= 0:
            reason = "TARGET_HIT_EXPAND_SKIPPED_NO_TARGET_IMPROVEMENT"
            tm["last_update_reason"] = reason
            return TradeManagementDecision(posture=TradePosture.HOLD.value, reason=reason, trade_management=tm)

        tm["target_r_multiple"] = _json_number(target_r)
        tm["current_target_price"] = _json_number(current_target)
        if _is_better_stop(side=side_s, new_stop=new_stop, old_stop=old_stop):
            tm["current_stop_price"] = _json_number(new_stop)
            # After target-locking the stop is no longer a simple initial-risk
            # stop. Keep stop_r_multiple as distance from entry for audit/debug.
            if entry > 0 and atr_unit > 0:
                locked_r = abs((entry - new_stop) / atr_unit) if side_s == "SELL" else abs((new_stop - entry) / atr_unit)
                tm["stop_r_multiple"] = _json_number(locked_r)

        try:
            tm["expansion_count"] = int(tm.get("expansion_count") or 0) + expansions_this_tick
        except Exception:
            tm["expansion_count"] = expansions_this_tick
        tm["last_target_hit_price"] = _json_number(completed_target)
        tm["group_target_r"] = _json_number(target_r)
        locked_stop = d(tm.get("current_stop_price") or 0)
        locked_stop_profit_r = _profit_r_from_price(
            side=side_s, entry_price=entry, price=locked_stop, atr_unit=atr_unit
        )
        if locked_stop_profit_r is not None:
            tm["current_stop_profit_r"] = _json_number(locked_stop_profit_r)
            tm["group_stop_profit_r"] = _json_number(locked_stop_profit_r)
        tm["group_projected_stop_price"] = tm.get("current_stop_price")
        tm["group_projected_target_price"] = tm.get("current_target_price")
        tm["group_update_source"] = "TARGET_EXPANSION"
        tm["last_managed_price"] = _json_number(last)
        tm["last_updated_at"] = asof_time.isoformat() if hasattr(asof_time, "isoformat") else None
        reason = (
            f"TARGET_HIT_EXPANDED_{expansions_this_tick}_STEP"
            f"_TO_{target_r}R_LOCK_STOP_AT_COMPLETED_TARGET_BUFFER"
        )
        tm["last_update_reason"] = reason
        tm["group_update_reason"] = reason
        tm["posture"] = TradePosture.EXPAND.value
        tm = _ratchet_trade_management_levels(side=side_s, tm=tm, previous_tm=previous_tm)
        return TradeManagementDecision(posture=TradePosture.EXPAND.value, reason=reason, trade_management=tm)
