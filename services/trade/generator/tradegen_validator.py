#!/usr/bin/env python3
"""
services/trade/generator/tradegen_validator.py

Shared trade validation gate for Autotrades.

Purpose
-------
This helper answers only one question:

    Can this user create a trade from this signal now?

It is intentionally shared by:
- backend autogen trade generation
- manual signal trade creation / preview from UI

Layering
--------
Signal/lifecycle owns market-setup validity:
- exhaustion
- balanced/no-setup state
- derivatives evidence/conflicts
- compression/expansion readiness
- entry posture

Trade validation owns execution safety:
- user eligibility
- signal is still open
- deployable signal state/action
- duplicate exposure protection
- active symbol-family protection

Trade creation/planning owns:
- instrument resolution
- price/risk defaults
- persistence

Manual override
---------------
Manual UI can override soft lifecycle/deployability WAIT conditions, but not hard
safety blocks such as missing user/signal, closed signal, duplicate active trade,
or inactive/autogen-ineligible user checks in AUTO mode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from utils.datetime_utils import business_now_naive, to_ist_naive

from configs.trade_config import TRADE_CONFIG
from enums.enums import SignalStatus
from schemas.signal import SignalSchema
from schemas.user import UserSchema
from schemas.user_trade import UserTradeSchema

logger = logging.getLogger(__name__)


ACTIVE_ENTRY_STATUSES = {
    "CREATED",
    "READY",
    "SUBMITTED",
    "FILLED",
}

TERMINAL_EXIT_STATUSES = {
    "FILLED",
    "CANCELLED",
}

TERMINAL_SIGNAL_STATUSES = {
    "CLOSED",
    "INVALIDATED",
    "EXPIRED",
    "REPLACED",
    "CANCELLED",
    "BLOCKED",
}

TRADE_DECISION_ALLOW = "ALLOW"
TRADE_DECISION_WAIT = "WAIT"
TRADE_DECISION_BLOCK = "BLOCK"

MODE_AUTO = "AUTO"
MODE_MANUAL_PREVIEW = "MANUAL_PREVIEW"
MODE_MANUAL_OVERRIDE = "MANUAL_OVERRIDE"


@dataclass
class TradeDecision:
    ok: bool
    decision: str
    allowed: bool
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "decision": self.decision,
            "allowed": bool(self.allowed),
            "reasons": list(self.reasons or []),
            "warnings": list(self.warnings or []),
            "details": dict(self.details or {}),
        }

    @staticmethod
    def allow(*, warnings: Optional[List[str]] = None, details: Optional[Dict[str, Any]] = None) -> "TradeDecision":
        return TradeDecision(
            ok=True,
            decision=TRADE_DECISION_ALLOW,
            allowed=True,
            reasons=[],
            warnings=warnings or [],
            details=details or {},
        )

    @staticmethod
    def wait(reason: str, *, warnings: Optional[List[str]] = None, details: Optional[Dict[str, Any]] = None) -> "TradeDecision":
        return TradeDecision(
            ok=True,
            decision=TRADE_DECISION_WAIT,
            allowed=False,
            reasons=[reason],
            warnings=warnings or [],
            details=details or {},
        )

    @staticmethod
    def block(reason: str, *, warnings: Optional[List[str]] = None, details: Optional[Dict[str, Any]] = None) -> "TradeDecision":
        return TradeDecision(
            ok=True,
            decision=TRADE_DECISION_BLOCK,
            allowed=False,
            reasons=[reason],
            warnings=warnings or [],
            details=details or {},
        )


def _enum_str(x: Any) -> str:
    v = getattr(x, "value", x)
    return str(v or "").strip().upper()


def _as_dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _decision_policy() -> Dict[str, Any]:
    return TRADE_CONFIG.policy.decision.model_dump(mode="python")


def _policy_bool(key: str, default: bool) -> bool:
    return bool(_decision_policy().get(key, default))


def _policy_str(key: str, default: str) -> str:
    return str(_decision_policy().get(key, default) or default).strip()


def _signal_meta(signal: SignalSchema) -> Dict[str, Any]:
    meta = _as_dict(getattr(signal, "meta_json", None))
    signal_meta = meta.get("signal")
    if not isinstance(signal_meta, dict):
        raise ValueError("signal.meta_json['signal'] is required for trade validation")
    return signal_meta


def _full_meta(signal: SignalSchema) -> Dict[str, Any]:
    meta = _as_dict(getattr(signal, "meta_json", None))
    if not meta:
        raise ValueError("signal.meta_json is required for trade validation")
    return meta


def _initiated_setup(signal: SignalSchema) -> Dict[str, Any]:
    meta = _full_meta(signal)
    initiated = meta.get("initiated_setup")
    return initiated if isinstance(initiated, dict) else {}


def _initiated_entry_reason(signal: SignalSchema) -> Dict[str, Any]:
    initiated = _initiated_setup(signal)
    entry_reason = initiated.get("entry_reason")
    return entry_reason if isinstance(entry_reason, dict) else {}


def _initiated_setup_label(signal: SignalSchema) -> str:
    return _enum_str(_initiated_setup(signal).get("setup_label"))


def _originating_setup_label(signal: SignalSchema) -> str:
    """Return the immutable setup identity and fail on metadata drift."""
    persisted = _enum_str(getattr(signal, "setup", ""))
    initiated = _initiated_setup_label(signal)
    explicit = _enum_str(_full_meta(signal).get("initiated_setup_label"))
    if not persisted:
        raise ValueError("SIGNAL_ORIGINATING_SETUP_MISSING")
    mismatches = [x for x in (initiated, explicit) if x and x != persisted]
    if mismatches:
        raise ValueError(
            "SIGNAL_SETUP_IDENTITY_MISMATCH "
            f"signal_id={getattr(signal, 'signal_id', None)} "
            f"persisted={persisted} initiated={initiated} explicit={explicit}"
        )
    return persisted


def _initiated_entry_action(signal: SignalSchema) -> str:
    return _enum_str(_initiated_entry_reason(signal).get("action"))


def _signal_action(signal: SignalSchema) -> str:
    return _enum_str(_signal_meta(signal).get("signal_action"))


def _evidence_action(signal: SignalSchema) -> str:
    return _enum_str(_signal_meta(signal).get("signal_action"))


def _setup_state(signal: SignalSchema) -> str:
    return _enum_str(_signal_meta(signal).get("setup_state") or _signal_meta(signal).get("signal_state"))


def _evidence_reason(signal: SignalSchema) -> str:
    return str(_signal_meta(signal).get("signal_reason") or "").strip()


def _signal_stage(signal: SignalSchema) -> str:
    return _enum_str(getattr(signal, "stage", "") or _signal_meta(signal).get("stage"))


def _signal_confidence(signal: SignalSchema) -> float:
    try:
        return float(_signal_meta(signal).get("confidence") or 0)
    except Exception:
        return 0.0


def _signal_quality(signal: SignalSchema) -> str:
    return _enum_str(_signal_meta(signal).get("quality"))


def _num_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _signal_entry_prices(signal: SignalSchema) -> Dict[str, Any]:
    side = _side(signal)
    created_price = _num_or_none(getattr(signal, "created_price", None))
    current_price = _num_or_none(
        getattr(signal, "last_price", None)
        or getattr(signal, "ltp", None)
    )

    directional_move_pct: Optional[float] = None
    if created_price is not None and created_price > 0 and current_price is not None:
        if side == "BUY":
            directional_move_pct = ((current_price - created_price) / created_price) * 100.0
        elif side == "SELL":
            directional_move_pct = ((created_price - current_price) / created_price) * 100.0

    return {
        "side": side,
        "created_price": created_price,
        "current_price": current_price,
        "directional_move_pct": directional_move_pct,
    }


def _signal_created_time(signal: SignalSchema):
    for attr in ("actionable_time", "qualified_time", "first_seen_time"):
        value = to_ist_naive(getattr(signal, attr, None))
        if value is not None:
            return value
    return None


def _manual_entry_warnings(signal: SignalSchema) -> tuple[List[str], Dict[str, Any]]:
    """Return manual-only delay/chase diagnostics without blocking AUTO."""
    if not _policy_bool("manual_entry_warning_enabled", True):
        return [], {}

    prices = _signal_entry_prices(signal)
    created_time = _signal_created_time(signal)
    now = business_now_naive()
    age_minutes: Optional[float] = None
    if created_time is not None:
        try:
            age_minutes = max(0.0, (now - created_time).total_seconds() / 60.0)
        except Exception:
            age_minutes = None

    delay_threshold = float(_decision_policy().get("manual_entry_delay_warning_minutes", 6.0) or 6.0)
    move_threshold = float(_decision_policy().get("manual_entry_move_warning_pct", 0.50) or 0.50)
    move_pct = prices.get("directional_move_pct")

    delayed = age_minutes is not None and age_minutes >= max(0.0, delay_threshold)
    moved = move_pct is not None and move_pct >= max(0.0, move_threshold)

    details = {
        **prices,
        "signal_created_time": created_time,
        "checked_time": now,
        "age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
        "delay_warning_minutes": delay_threshold,
        "move_warning_pct": move_threshold,
        "delayed": delayed,
        "moved_in_signal_direction": moved,
    }

    if not (delayed or moved):
        return [], details

    age_text = f"{age_minutes:.1f} minutes old" if age_minutes is not None else "delayed"
    if move_pct is None:
        move_text = "current move could not be calculated"
    elif move_pct >= 0:
        move_text = f"has already moved {move_pct:.2f}% in the signal direction"
    else:
        move_text = f"is currently {abs(move_pct):.2f}% adverse to the signal"

    warning = (
        f"Manual entry warning: signal is {age_text} and {move_text}. "
        "A delayed manual entry may involve chasing or reduced reward-to-risk."
    )
    return [warning], details


def _signal_entry_price_decision(
    signal: SignalSchema,
    *,
    mode: str,
    warnings: Optional[List[str]] = None,
) -> Optional[TradeDecision]:
    """Require only that an OPEN signal is not currently in loss.

    The rule is inclusive so AUTO may deploy on the signal-creation pass when
    current price equals created price. Manual preview surfaces an adverse price
    as an explicit confirmation warning; MANUAL_OVERRIDE is the operator's
    deliberate bypass.
    """
    if not _policy_bool("signal_entry_not_in_loss_enabled", True):
        return None

    if _enum_str(mode) == MODE_MANUAL_OVERRIDE:
        return None

    prices = _signal_entry_prices(signal)
    side = prices["side"]
    created_price = prices["created_price"]
    current_price = prices["current_price"]

    details = {
        "policy": "signal_entry_not_in_loss",
        **prices,
        "required_relation": (
            "current_price >= created_price"
            if side == "BUY"
            else "current_price <= created_price"
            if side == "SELL"
            else "valid BUY/SELL side required"
        ),
    }

    if created_price is None or created_price <= 0 or current_price is None or current_price <= 0:
        details["not_in_loss"] = False
        return TradeDecision.wait(
            _policy_str(
                "signal_entry_wait_price_missing_code",
                "SIGNAL_ENTRY_WAIT_PRICE_UNAVAILABLE",
            ),
            warnings=list(warnings or []),
            details=details,
        )

    not_in_loss = (
        (side == "BUY" and current_price >= created_price)
        or (side == "SELL" and current_price <= created_price)
    )
    details["not_in_loss"] = bool(not_in_loss)
    details["at_breakeven"] = bool(current_price == created_price)

    if not_in_loss:
        return None

    return TradeDecision.wait(
        _policy_str(
            "signal_entry_wait_in_loss_code",
            "SIGNAL_ENTRY_WAIT_NOT_IN_LOSS",
        ),
        warnings=list(warnings or []),
        details=details,
    )


def _lifecycle_name(signal: SignalSchema) -> str:
    return _enum_str(getattr(signal, "lifecycle", ""))


def _signal_status(signal: SignalSchema) -> str:
    return _enum_str(getattr(signal, "status", ""))


def _symbol(signal: SignalSchema) -> str:
    return str(getattr(signal, "symbol", "") or getattr(signal, "equity_ref", "") or "").strip().upper()


def _side(signal: SignalSchema) -> str:
    return _enum_str(getattr(signal, "side", ""))


def _symbol_family(signal: SignalSchema) -> str:
    return str(
        getattr(signal, "equity_ref", "")
        or getattr(signal, "underlying", "")
        or getattr(signal, "symbol", "")
        or ""
    ).strip().upper()


def _is_open_signal(signal: SignalSchema) -> bool:
    return _signal_status(signal) == SignalStatus.OPEN.value


def _is_autogen_user(user: UserSchema) -> bool:
    return (
        int(getattr(user, "active", 0) or 0) == 1
        and int(getattr(user, "logged_in", 0) or 0) == 1
    )


def _user_autotrade_enabled(user: UserSchema) -> bool:
    return int(getattr(user, "autotrade", 0) or 0) == 1


def _fetch_active_trades_for_signal(userid: str, signal_id: str) -> List[Any]:
    if not userid or not signal_id:
        return []

    method_names = [
        "fetch_active_trades_for_signal",
        "fetch_open_trades_for_signal",
        "list_active_trades_for_signal",
        "list_open_trades_for_signal",
    ]

    for name in method_names:
        fn = getattr(UserTradeSchema, name, None)
        if not callable(fn):
            continue
        try:
            rows = fn(userid=userid, signal_id=signal_id) or []
            return list(rows)
        except TypeError:
            try:
                rows = fn(userid, signal_id) or []
                return list(rows)
            except Exception:
                logger.exception("TradeDecisionHelper: %s failed | userid=%s signal_id=%s", name, userid, signal_id)
                return []
        except Exception:
            logger.exception("TradeDecisionHelper: %s failed | userid=%s signal_id=%s", name, userid, signal_id)
            return []

    return []


def _active_trade_exists(userid: str, signal_id: str) -> bool:
    rows = _fetch_active_trades_for_signal(userid, signal_id)
    if not rows:
        return False

    for row in rows:
        entry_status = _enum_str(getattr(row, "entry_status", ""))
        exit_status = _enum_str(getattr(row, "exit_status", ""))
        if entry_status in ACTIVE_ENTRY_STATUSES and exit_status not in TERMINAL_EXIT_STATUSES:
            return True

    return bool(rows)


def _fetch_active_trades_for_symbol_family(userid: str, equity_ref: str) -> List[Any]:
    userid = str(userid or "").strip()
    equity_ref = str(equity_ref or "").strip().upper()
    if not userid or not equity_ref:
        return []

    method_names = [
        "fetch_active_trades_for_user_equity_ref",
        "fetch_open_trades_for_user_equity_ref",
        "list_active_trades_for_user_equity_ref",
        "list_open_trades_for_user_equity_ref",
    ]

    for name in method_names:
        fn = getattr(UserTradeSchema, name, None)
        if not callable(fn):
            continue
        try:
            rows = fn(userid=userid, equity_ref=equity_ref) or []
            return list(rows)
        except TypeError:
            try:
                rows = fn(userid, equity_ref) or []
                return list(rows)
            except Exception:
                logger.exception("TradeDecisionHelper: %s failed | userid=%s equity_ref=%s", name, userid, equity_ref)
                return []
        except Exception:
            logger.exception("TradeDecisionHelper: %s failed | userid=%s equity_ref=%s", name, userid, equity_ref)
            return []

    try:
        rows = UserTradeSchema.fetch_open_positions(userid=userid, symbol=equity_ref) or []
        return list(rows)
    except Exception:
        logger.exception("TradeDecisionHelper: fetch_open_positions fallback failed | userid=%s equity_ref=%s", userid, equity_ref)
        return []


def _active_symbol_family_trade_exists(userid: str, equity_ref: str) -> bool:
    rows = _fetch_active_trades_for_symbol_family(userid, equity_ref)
    if not rows:
        return False

    for row in rows:
        entry_status = _enum_str(getattr(row, "entry_status", ""))
        exit_status = _enum_str(getattr(row, "exit_status", ""))
        if entry_status in ACTIVE_ENTRY_STATUSES and exit_status not in TERMINAL_EXIT_STATUSES:
            return True

    return bool(rows)


class TradeDecisionHelper:
    """Common AUTO/MANUAL trade decision helper."""

    @staticmethod
    def evaluate(
        *,
        user: UserSchema,
        signal: SignalSchema,
        mode: str = MODE_AUTO,
    ) -> TradeDecision:
        mode = _enum_str(mode or MODE_AUTO)
        userid = str(getattr(user, "userid", "") or "").strip()
        signal_id = str(getattr(signal, "signal_id", "") or getattr(signal, "signal_id", "") or "").strip()

        warnings: List[str] = []
        details: Dict[str, Any] = {
            "userid": userid,
            "signal_id": signal_id,
            "symbol": _symbol(signal),
            "equity_ref": _symbol_family(signal),
            "lifecycle": _lifecycle_name(signal),
            "side": _side(signal),
            "stage": _signal_stage(signal),
            "status": _signal_status(signal),
            "setup_action": _evidence_action(signal),
            "setup_state": _setup_state(signal),
            "initiated_setup_label": _initiated_setup_label(signal),
            "originating_setup_label": _originating_setup_label(signal),
            "initiated_entry_action": _initiated_entry_action(signal),
            "reason": _evidence_reason(signal),
            "signal_action": _signal_action(signal),
            "confidence": _signal_confidence(signal),
            "quality": _signal_quality(signal),
            "mode": mode,
        }

        if not userid:
            return TradeDecision.block("missing_userid", details=details)

        if not signal_id:
            return TradeDecision.block("missing_signal_id", details=details)

        if mode == MODE_AUTO and not _is_autogen_user(user):
            return TradeDecision.block("user_not_autogen_eligible", details=details)

        # ``users.autotrade`` is the authoritative opt-in for automatic
        # deployment. It is not a tunable strategy policy and cannot be
        # bypassed by role, broker login or execution mode.
        if mode == MODE_AUTO and not _user_autotrade_enabled(user):
            return TradeDecision.block("autotrade_not_enabled", details=details)

        if not _is_open_signal(signal):
            return TradeDecision.block("signal_not_open", details=details)

        if _signal_status(signal) in TERMINAL_SIGNAL_STATUSES:
            return TradeDecision.block("signal_terminal", details=details)

        # Any historical trade row means this user/signal was already deployed.
        # Terminal exits do not make it eligible again; re-entry is intentionally
        # unsupported until a separate explicit policy/model is introduced.
        if UserTradeSchema.has_any_trade_for_signal(userid=userid, signal_id=signal_id):
            return TradeDecision.block("signal_already_deployed", details=details)

        # Lifecycle action used to close/replace an old opposite signal. It is
        # never a clean fresh deployment action for autogen. Manual override may
        # still create only after an explicit operator confirmation.
        if _signal_action(signal) == "INVALIDATE_OPPOSITE":
            reason = "signal_action_invalidating_opposite_wait_for_confirmation"
            if mode == MODE_MANUAL_OVERRIDE:
                warnings.append(reason)
            else:
                return TradeDecision.wait(reason, warnings=warnings, details=details)

        if mode == MODE_MANUAL_PREVIEW:
            manual_warnings, manual_details = _manual_entry_warnings(signal)
            warnings.extend(manual_warnings)
            if manual_details:
                details["manual_entry_diagnostics"] = manual_details

        price_decision = _signal_entry_price_decision(
            signal,
            mode=mode,
            warnings=warnings,
        )
        if price_decision is not None:
            price_decision.details = {**details, **dict(price_decision.details or {})}
            return price_decision

        if _policy_bool("block_duplicate_signal_trade", True):
            if _active_trade_exists(userid, signal_id):
                return TradeDecision.block(
                    "active_trade_exists_for_signal",
                    warnings=warnings,
                    details=details,
                )

        if _policy_bool("block_duplicate_symbol_trade", True):
            equity_ref = _symbol_family(signal)
            family_has_active = _active_symbol_family_trade_exists(userid, equity_ref)
            details["duplicate_symbol_check"] = {
                "equity_ref": equity_ref,
                "family_has_active_trade": family_has_active,
                "policy": "one_active_trade_family_per_user_symbol",
            }
            if family_has_active:
                return TradeDecision.block(
                    "active_trade_exists_for_symbol_family",
                    warnings=warnings,
                    details=details,
                )

        return TradeDecision.allow(warnings=warnings, details=details)

    @staticmethod
    def evaluate_by_ids(
        *,
        userid: str,
        signal_id: str,
        mode: str = MODE_AUTO,
    ) -> TradeDecision:
        user = UserSchema.fetch_user(userid)
        if not user:
            return TradeDecision.block(
                "user_not_found",
                details={"userid": userid, "signal_id": signal_id, "mode": _enum_str(mode)},
            )

        signal = SignalSchema.fetch_by_signal_id(signal_id)
        if not signal:
            return TradeDecision.block(
                "signal_not_found",
                details={"userid": userid, "signal_id": signal_id, "mode": _enum_str(mode)},
            )

        return TradeDecisionHelper.evaluate(user=user, signal=signal, mode=mode)
