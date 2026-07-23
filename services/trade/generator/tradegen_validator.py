#!/usr/bin/env python3
"""
Auction-driven trade entry validation.

This module owns only *new trade entry eligibility* for signal-originated
trades.  SignalGenerator owns signal lifecycle; TradeMonitor owns management
of an already-created trade.

Patch 5.3 rules
---------------
- Parse only ``AUCTION_SIGNAL_DOWNSTREAM_V1``.
- Automatic entry is allowed only for ACTIVE/EXPAND + STRENGTHEN.
- Defensive postures require an explicit manual confirmation.
- EXIT_BIAS/FORCE_EXIT/EXIT/should_exit_signal are hard blocks in every mode.
- Duplicate checks are strict and fail closed: DB errors propagate.
- Confidence/quality remain optional and are never replaced with 0/LOW.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from configs.trade_config import TRADE_CONFIG
from enums.enums import SignalStatus
from schemas.signal import SignalSchema
from schemas.user import UserSchema
from schemas.user_trade import UserTradeSchema
from utils.datetime_utils import business_now_naive, to_ist_naive


DOWNSTREAM_CONTRACT_VERSION = "AUCTION_SIGNAL_DOWNSTREAM_V1"

ACTIVE_ENTRY_STATUSES = {"CREATED", "READY", "SUBMITTED", "FILLED"}
TERMINAL_EXIT_STATUSES = {"FILLED", "CANCELLED"}
TERMINAL_SIGNAL_STATUSES = {
    "CLOSED",
    "INVALIDATED",
    "EXPIRED",
    "REPLACED",
    "CANCELLED",
    "BLOCKED",
}

ENTRY_STAGES = {"ACTIVE", "EXPAND"}
DEFENSIVE_STAGES = {"PROTECT", "TRANSITION", "WEAKENING"}
HARD_EXIT_STAGES = {"EXIT_BIAS", "FORCE_EXIT"}

TRADE_DECISION_ALLOW = "ALLOW"
TRADE_DECISION_WAIT = "WAIT"
TRADE_DECISION_BLOCK = "BLOCK"

MODE_AUTO = "AUTO"
MODE_MANUAL_PREVIEW = "MANUAL_PREVIEW"
MODE_MANUAL_CONFIRM = "MANUAL_CONFIRM"
# Backward-compatible import name.  Its semantics are now limited to soft
# entry-warning confirmation; it cannot bypass a hard lifecycle exit posture.
MODE_MANUAL_OVERRIDE = MODE_MANUAL_CONFIRM


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
            "entry_decision": {
                TRADE_DECISION_ALLOW: "ENTRY_ELIGIBLE",
                TRADE_DECISION_WAIT: "CONFIRMATION_REQUIRED",
                TRADE_DECISION_BLOCK: "ENTRY_BLOCKED",
            }[self.decision],
            "allowed": bool(self.allowed),
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "details": dict(self.details),
        }

    @staticmethod
    def allow(*, warnings: Optional[List[str]] = None, details: Optional[Dict[str, Any]] = None) -> "TradeDecision":
        return TradeDecision(True, TRADE_DECISION_ALLOW, True, [], warnings or [], details or {})

    @staticmethod
    def wait(reason: str, *, warnings: Optional[List[str]] = None, details: Optional[Dict[str, Any]] = None) -> "TradeDecision":
        return TradeDecision(True, TRADE_DECISION_WAIT, False, [reason], warnings or [], details or {})

    @staticmethod
    def block(reason: str, *, warnings: Optional[List[str]] = None, details: Optional[Dict[str, Any]] = None) -> "TradeDecision":
        return TradeDecision(True, TRADE_DECISION_BLOCK, False, [reason], warnings or [], details or {})


def _enum_str(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().upper()


def _as_dict(value: Any, *, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return value


def _required_text(obj: Dict[str, Any], key: str, *, path: str) -> str:
    if key not in obj:
        raise ValueError(f"{path}.{key} is required")
    value = str(obj[key] or "").strip()
    if not value:
        raise ValueError(f"{path}.{key} cannot be blank")
    return value


def _required_bool(obj: Dict[str, Any], key: str, *, path: str) -> bool:
    if key not in obj or not isinstance(obj[key], bool):
        raise ValueError(f"{path}.{key} must be a boolean")
    return obj[key]


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


def _decision_policy() -> Dict[str, Any]:
    return TRADE_CONFIG.policy.decision.model_dump(mode="python")


def _policy_bool(key: str, default: bool) -> bool:
    return bool(_decision_policy().get(key, default))


def _policy_str(key: str, default: str) -> str:
    return str(_decision_policy().get(key, default) or default).strip()


def _full_meta(signal: SignalSchema) -> Dict[str, Any]:
    return _as_dict(getattr(signal, "meta_json", None), path="signal.meta_json")


def _downstream_context(signal: SignalSchema) -> Dict[str, Any]:
    meta = _full_meta(signal)
    contract = _as_dict(meta.get("downstream_contract"), path="signal.meta_json.downstream_contract")
    version = _required_text(contract, "version", path="signal.meta_json.downstream_contract")
    if version != DOWNSTREAM_CONTRACT_VERSION:
        raise ValueError(
            "Unsupported downstream contract version: "
            f"expected={DOWNSTREAM_CONTRACT_VERSION} actual={version}"
        )

    signal_block = _as_dict(meta.get("signal"), path="signal.meta_json.signal")
    lifecycle = _as_dict(meta.get("lifecycle"), path="signal.meta_json.lifecycle")
    evidence = _as_dict(meta.get("active_signal_evidence"), path="signal.meta_json.active_signal_evidence")
    setup_levels = _as_dict(meta.get("setup_levels"), path="signal.meta_json.setup_levels")

    stage = _enum_str(getattr(signal, "stage", None))
    if not stage:
        stage = _enum_str(signal_block.get("stage"))
    if not stage:
        raise ValueError("signal stage is required")

    status = _enum_str(getattr(signal, "status", None))
    if not status:
        raise ValueError("signal status is required")

    evidence_action = _enum_str(
        evidence.get("active_evidence_action")
        if "active_evidence_action" in evidence
        else evidence.get("evidence_action")
    )
    if not evidence_action:
        raise ValueError("active_signal_evidence.active_evidence_action is required")

    lifecycle_trade_action = _enum_str(
        lifecycle.get("trade_action")
        if "trade_action" in lifecycle
        else signal_block.get("trade_action")
    )
    if not lifecycle_trade_action:
        raise ValueError("lifecycle.trade_action is required")

    should_exit = _required_bool(
        evidence,
        "should_exit_signal",
        path="signal.meta_json.active_signal_evidence",
    )

    setup_family = _required_text(setup_levels, "setup_label", path="signal.meta_json.setup_levels").upper()
    persisted_setup = _enum_str(getattr(signal, "setup", None))
    if persisted_setup != setup_family:
        raise ValueError(
            f"signal setup identity mismatch persisted={persisted_setup} setup_levels={setup_family}"
        )

    opportunity_key = _required_text(setup_levels, "opportunity_key", path="signal.meta_json.setup_levels")
    candidate_id = _required_text(setup_levels, "candidate_id", path="signal.meta_json.setup_levels")
    boundary_event_key = _required_text(
        setup_levels,
        "boundary_event_key",
        path="signal.meta_json.setup_levels",
    )

    signal_reason = str(signal_block.get("signal_reason") or getattr(signal, "status_reason", "") or "").strip()
    auction_action = _enum_str(evidence.get("auction_action"))
    auction_state = _enum_str(evidence.get("auction_state"))
    alignment = _enum_str(evidence.get("directional_alignment"))

    confidence = _optional_float(signal_block.get("confidence"))
    quality = signal_block.get("quality")
    if quality is not None:
        quality = str(quality).strip() or None

    return {
        "contract_version": version,
        "signal_stage": stage,
        "signal_status": status,
        "signal_action": _enum_str(signal_block.get("signal_action")),
        "signal_state": _enum_str(signal_block.get("signal_state")),
        "lifecycle_trade_action": lifecycle_trade_action,
        "management_posture": evidence_action,
        "lifecycle_reason": signal_reason,
        "auction_action": auction_action,
        "auction_state": auction_state,
        "directional_alignment": alignment,
        "should_exit_signal": should_exit,
        "opportunity_key": opportunity_key,
        "candidate_id": candidate_id,
        "boundary_event_key": boundary_event_key,
        "setup_family": setup_family,
        "confidence": confidence,
        "quality": quality,
    }


def _signal_status(signal: SignalSchema) -> str:
    return _enum_str(getattr(signal, "status", None))


def _symbol(signal: SignalSchema) -> str:
    return str(getattr(signal, "symbol", "") or getattr(signal, "equity_ref", "") or "").strip().upper()


def _symbol_family(signal: SignalSchema) -> str:
    return str(
        getattr(signal, "equity_ref", "")
        or getattr(signal, "underlying", "")
        or getattr(signal, "symbol", "")
        or ""
    ).strip().upper()


def _side(signal: SignalSchema) -> str:
    return _enum_str(getattr(signal, "side", None))


def _is_open_signal(signal: SignalSchema) -> bool:
    return _signal_status(signal) == SignalStatus.OPEN.value


def _is_autogen_user(user: UserSchema) -> bool:
    return int(getattr(user, "active", 0) or 0) == 1 and int(getattr(user, "logged_in", 0) or 0) == 1


def _user_autotrade_enabled(user: UserSchema) -> bool:
    return int(getattr(user, "autotrade", 0) or 0) == 1


def _signal_entry_prices(signal: SignalSchema) -> Dict[str, Any]:
    side = _side(signal)
    created = _optional_float(getattr(signal, "created_price", None))
    current_raw = getattr(signal, "last_price", None)
    if current_raw is None:
        current_raw = getattr(signal, "ltp", None)
    current = _optional_float(current_raw)

    directional_move_pct: Optional[float] = None
    if created is not None and created > 0 and current is not None:
        if side == "BUY":
            directional_move_pct = ((current - created) / created) * 100.0
        elif side == "SELL":
            directional_move_pct = ((created - current) / created) * 100.0

    return {
        "side": side,
        "created_price": created,
        "current_price": current,
        "directional_move_pct": directional_move_pct,
    }


def _signal_created_time(signal: SignalSchema):
    for attr in ("actionable_time", "qualified_time", "first_seen_time"):
        value = to_ist_naive(getattr(signal, attr, None))
        if value is not None:
            return value
    return None


def _manual_entry_warnings(signal: SignalSchema) -> tuple[List[str], Dict[str, Any]]:
    if not _policy_bool("manual_entry_warning_enabled", True):
        return [], {}

    prices = _signal_entry_prices(signal)
    created_time = _signal_created_time(signal)
    now = business_now_naive()
    age_minutes: Optional[float] = None
    if created_time is not None:
        age_minutes = max(0.0, (now - created_time).total_seconds() / 60.0)

    delay_threshold = float(_decision_policy().get("manual_entry_delay_warning_minutes", 6.0) or 6.0)
    move_threshold = float(_decision_policy().get("manual_entry_move_warning_pct", 0.50) or 0.50)
    move_pct = prices["directional_move_pct"]
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
    return [
        f"Manual entry warning: signal is {age_text} and {move_text}. "
        "A delayed manual entry may involve chasing or reduced reward-to-risk."
    ], details


def _price_entry_decision(signal: SignalSchema, *, mode: str, warnings: List[str]) -> Optional[TradeDecision]:
    if not _policy_bool("signal_entry_not_in_loss_enabled", True):
        return None
    if mode == MODE_MANUAL_CONFIRM:
        return None

    prices = _signal_entry_prices(signal)
    side = prices["side"]
    created = prices["created_price"]
    current = prices["current_price"]
    details = {
        "policy": "signal_entry_not_in_loss",
        **prices,
        "required_relation": (
            "current_price >= created_price" if side == "BUY" else
            "current_price <= created_price" if side == "SELL" else
            "valid BUY/SELL side required"
        ),
    }

    if created is None or created <= 0 or current is None or current <= 0:
        details["not_in_loss"] = False
        return TradeDecision.wait(
            _policy_str("signal_entry_wait_price_missing_code", "SIGNAL_ENTRY_WAIT_PRICE_UNAVAILABLE"),
            warnings=warnings,
            details=details,
        )

    not_in_loss = (side == "BUY" and current >= created) or (side == "SELL" and current <= created)
    details["not_in_loss"] = bool(not_in_loss)
    details["at_breakeven"] = bool(current == created)
    if not_in_loss:
        return None
    return TradeDecision.wait(
        _policy_str("signal_entry_wait_in_loss_code", "SIGNAL_ENTRY_WAIT_NOT_IN_LOSS"),
        warnings=warnings,
        details=details,
    )


def _active_trade_exists(userid: str, signal_id: str) -> bool:
    rows = UserTradeSchema.fetch_active_trades_for_signal(userid=userid, signal_id=signal_id)
    for row in rows:
        entry_status = _enum_str(getattr(row, "entry_status", None))
        exit_status = _enum_str(getattr(row, "exit_status", None))
        if entry_status in ACTIVE_ENTRY_STATUSES and exit_status not in TERMINAL_EXIT_STATUSES:
            return True
    return bool(rows)


def _active_symbol_family_trade_exists(userid: str, equity_ref: str) -> bool:
    rows = UserTradeSchema.fetch_active_trades_for_user_equity_ref(userid=userid, equity_ref=equity_ref)
    for row in rows:
        entry_status = _enum_str(getattr(row, "entry_status", None))
        exit_status = _enum_str(getattr(row, "exit_status", None))
        if entry_status in ACTIVE_ENTRY_STATUSES and exit_status not in TERMINAL_EXIT_STATUSES:
            return True
    return bool(rows)


class TradeDecisionHelper:
    """Common AUTO/manual signal-entry decision helper."""

    @staticmethod
    def evaluate(*, user: UserSchema, signal: SignalSchema, mode: str = MODE_AUTO) -> TradeDecision:
        mode = _enum_str(mode or MODE_AUTO)
        if mode not in {MODE_AUTO, MODE_MANUAL_PREVIEW, MODE_MANUAL_CONFIRM}:
            raise ValueError(f"Unsupported trade validation mode: {mode}")

        userid = str(getattr(user, "userid", "") or "").strip()
        signal_id = str(getattr(signal, "signal_id", "") or "").strip()
        context = _downstream_context(signal)
        details: Dict[str, Any] = {
            "userid": userid,
            "signal_id": signal_id,
            "symbol": _symbol(signal),
            "equity_ref": _symbol_family(signal),
            "side": _side(signal),
            "mode": mode,
            **context,
        }
        warnings: List[str] = []

        if not userid:
            return TradeDecision.block("missing_userid", details=details)
        if not signal_id:
            return TradeDecision.block("missing_signal_id", details=details)
        if mode == MODE_AUTO and not _is_autogen_user(user):
            return TradeDecision.block("user_not_autogen_eligible", details=details)
        if mode == MODE_AUTO and not _user_autotrade_enabled(user):
            return TradeDecision.block("autotrade_not_enabled", details=details)
        if not _is_open_signal(signal):
            return TradeDecision.block("signal_not_open", details=details)
        if context["signal_status"] in TERMINAL_SIGNAL_STATUSES:
            return TradeDecision.block("signal_terminal", details=details)

        # A lifecycle exit posture is never overrideable for new entry.
        hard_exit = (
            context["signal_stage"] in HARD_EXIT_STAGES
            or context["management_posture"] == "EXIT"
            or context["should_exit_signal"]
            or context["lifecycle_trade_action"] in {"EXIT_POSITION", "FORCE_EXIT"}
        )
        if hard_exit:
            return TradeDecision.block("signal_exit_posture", details=details)

        # One deployment per user/signal, including historically closed packages.
        if UserTradeSchema.has_any_trade_for_signal(userid=userid, signal_id=signal_id):
            return TradeDecision.block("signal_already_deployed", details=details)

        defensive = (
            context["signal_stage"] in DEFENSIVE_STAGES
            or context["management_posture"] == "CAUTION"
            or context["lifecycle_trade_action"] == "TIGHTEN_STOP"
        )
        entry_ready = (
            context["signal_stage"] in ENTRY_STAGES
            and context["management_posture"] == "STRENGTHEN"
            and context["lifecycle_trade_action"] not in {"EXIT_POSITION", "FORCE_EXIT"}
        )

        if defensive:
            reason = "signal_defensive_posture_requires_manual_confirmation"
            if mode == MODE_MANUAL_CONFIRM:
                warnings.append(reason)
            else:
                return TradeDecision.wait(reason, details=details)
        elif not entry_ready:
            return TradeDecision.block("signal_not_entry_eligible", details=details)

        if mode == MODE_MANUAL_PREVIEW:
            manual_warnings, manual_details = _manual_entry_warnings(signal)
            warnings.extend(manual_warnings)
            if manual_details:
                details["manual_entry_diagnostics"] = manual_details

        price_decision = _price_entry_decision(signal, mode=mode, warnings=warnings)
        if price_decision is not None:
            price_decision.details = {**details, **price_decision.details}
            return price_decision

        if _policy_bool("block_duplicate_signal_trade", True) and _active_trade_exists(userid, signal_id):
            return TradeDecision.block("active_trade_exists_for_signal", warnings=warnings, details=details)

        if _policy_bool("block_duplicate_symbol_trade", True):
            equity_ref = _symbol_family(signal)
            family_has_active = _active_symbol_family_trade_exists(userid, equity_ref)
            details["duplicate_symbol_check"] = {
                "equity_ref": equity_ref,
                "family_has_active_trade": family_has_active,
                "policy": "one_active_trade_family_per_user_symbol",
            }
            if family_has_active:
                return TradeDecision.block("active_trade_exists_for_symbol_family", warnings=warnings, details=details)

        return TradeDecision.allow(warnings=warnings, details=details)

    @staticmethod
    def evaluate_by_ids(*, userid: str, signal_id: str, mode: str = MODE_AUTO) -> TradeDecision:
        user = UserSchema.fetch_user(userid)
        if not user:
            return TradeDecision.block(
                "user_not_found",
                details={"userid": userid, "signal_id": signal_id, "mode": _enum_str(mode)},
            )
        signal = SignalSchema.fetch_by_signal_id_strict(signal_id)
        if not signal:
            return TradeDecision.block(
                "signal_not_found",
                details={"userid": userid, "signal_id": signal_id, "mode": _enum_str(mode)},
            )
        return TradeDecisionHelper.evaluate(user=user, signal=signal, mode=mode)
