"""Signal-table helper used by the Auction service orchestrator.

The Auction Engine decides *what* should happen.  This service owns the
mechanics of reading active-signal context and creating, maintaining, or
closing signal rows.  It deliberately contains no evidence, setup discovery,
setup selection, or Advisor evaluation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import logging
import uuid
from typing import Any, Dict, Optional, Tuple

from enums.enums import LifecycleStage, SignalStatus
from services.auction_engine.active_context import ActiveContext, ActiveContextProvider
from services.auction_engine.contracts import AuctionEngineResult, FinalAction
from services.signals.signal_metrics import calculate_signal_metrics
from utils.datetime_utils import to_ist_naive
from utils.json_utils import sanitize_json

logger = logging.getLogger(__name__)


_REPORT_ONLY_CREATE_REASON = "WOULD_CREATE_REPORT_ONLY"
_PERSISTED_CREATE_REASON = "SIGNAL_CREATED_FROM_SELECTED_OPPORTUNITY"
_CREATE_REQUEST_REASON = "CREATE_REQUEST_FROM_SELECTED_OPPORTUNITY"
_SIGNAL_ID_NAMESPACE = uuid.UUID("0e4c829e-7220-4f40-950e-60f1d96c10a7")


def _deterministic_signal_id(lifecycle: str, opportunity_key: str) -> str:
    """Stable identity makes CREATE idempotent across a restart retry."""
    source = f"AUTOTRADES:{str(lifecycle).strip().upper()}:{opportunity_key}"
    return str(uuid.uuid5(_SIGNAL_ID_NAMESPACE, source))


def _execution_reason_codes(
    action: str,
    reason_codes: Tuple[str, ...] | list[str],
) -> Tuple[str, ...]:
    """Translate decision-only markers into persistence-accurate reasons.

    DecisionEngine remains persistence-agnostic and may emit the historical
    report marker.  Once the lifecycle service is running with writes enabled,
    the audit trail must describe what actually happened.
    """
    replacement = None
    if action == "CREATE":
        replacement = _PERSISTED_CREATE_REASON
    elif action == "CREATE_BLOCKED":
        replacement = _CREATE_REQUEST_REASON

    mapped = []
    for reason in reason_codes:
        value = str(reason)
        if replacement and value == _REPORT_ONLY_CREATE_REASON:
            value = replacement
        if value and value not in mapped:
            mapped.append(value)

    if replacement and replacement not in mapped:
        mapped.insert(0, replacement)
    return tuple(mapped)


def _value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().upper()


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _snapshot_dump(snapshot: Any) -> Dict[str, Any]:
    if isinstance(snapshot, dict):
        return dict(snapshot)
    if hasattr(snapshot, "model_dump"):
        return snapshot.model_dump(mode="python")
    return {
        key: value
        for key, value in vars(snapshot).items()
        if not key.startswith("_")
    }


def _opportunity_key(signal: Optional[Any]) -> Optional[str]:
    if signal is None or not isinstance(getattr(signal, "meta_json", None), dict):
        return None
    meta = signal.meta_json
    for path in (
        ("auction_engine", "opportunity_key"),
        ("selected_opportunity", "opportunity_key"),
    ):
        current: Any = meta
        for key in path:
            current = current.get(key) if isinstance(current, dict) else None
        if current:
            return str(current)
    value = meta.get("opportunity_key")
    return str(value) if value else None


@dataclass(frozen=True)
class SignalLifecycleResult:
    symbol: str
    snapshot_time: datetime
    requested_action: str
    applied_action: str
    signal_id: Optional[str] = None
    opportunity_key: Optional[str] = None
    signal_opportunity_key: Optional[str] = None
    persisted: bool = False
    reason_codes: Tuple[str, ...] = ()
    diagnostics: Dict[str, Any] = field(default_factory=dict)


class SignalLifecycleService:
    """Read signal context before evaluation and apply an instruction after it."""

    def __init__(
        self,
        *,
        lifecycle: str = "DEFAULT",
        write_enabled: bool = False,
        causal_replay: bool = False,
        enforce_creation_permissions: bool = True,
    ) -> None:
        self.lifecycle = str(lifecycle or "DEFAULT").strip().upper()
        self.write_enabled = bool(write_enabled)
        self.causal_replay = bool(causal_replay)
        self.enforce_creation_permissions = bool(enforce_creation_permissions)
        self._symbol_cache: Dict[str, Optional[Any]] = {}
        self._replay_provider = ActiveContextProvider(self.lifecycle) if causal_replay else None

    def reset(self) -> None:
        self._symbol_cache.clear()
        if self._replay_provider is not None:
            self._replay_provider.reset()

    def resolve_equity_ref(self, symbol: str) -> str:
        key = str(symbol or "").strip().upper()
        if key not in self._symbol_cache:
            try:
                from schemas.symbol import SymbolSchema
                self._symbol_cache[key] = SymbolSchema.fetch_symbol(key)
            except Exception:
                logger.exception("Failed to resolve symbol context | symbol=%s", key)
                self._symbol_cache[key] = None
        row = self._symbol_cache.get(key)
        return str(getattr(row, "equity_ref", None) or key).strip().upper()

    def get_active_context(
        self,
        *,
        snapshot: Any,
        equity_ref: Optional[str] = None,
    ) -> ActiveContext:
        symbol = str(_field(snapshot, "symbol") or "").strip().upper()
        ref = str(equity_ref or self.resolve_equity_ref(symbol)).strip().upper()
        raw_ts = _field(snapshot, "snapshot_time")
        ts = to_ist_naive(raw_ts) or raw_ts

        # Historical report mode needs causal as-of filtering. Live mode reads
        # the current OPEN row directly and therefore sees the prior cadence's
        # writes immediately.
        if self._replay_provider is not None and not self.write_enabled:
            return self._replay_provider.evaluate(
                equity_ref=ref,
                symbol=symbol,
                snapshot_time=ts,
            )

        try:
            from schemas.signal import SignalSchema
            signal = SignalSchema.fetch_active_signal(ref, self.lifecycle)
        except Exception as exc:
            logger.exception("Failed to read signal lifecycle context | %s", ref)
            return ActiveContext(
                equity_ref=ref,
                snapshot_time=ts,
                evaluation_status="ERROR",
                reason_codes=("ACTIVE_CONTEXT_READ_FAILED",),
                diagnostics={"error": f"{type(exc).__name__}: {exc}"},
            )

        retry_created_signal_id = None
        if signal is not None:
            first_seen = to_ist_naive(getattr(signal, "first_seen_time", None))
            last_snapshot = to_ist_naive(getattr(signal, "last_snapshot_time", None))
            # A CREATE may have committed before checkpoint/processed acknowledgement.
            # Reconstruct the pre-snapshot context only when the signal itself was
            # first created by this same snapshot. Older signals updated at this
            # timestamp remain valid active context.
            if first_seen == ts and last_snapshot == ts:
                retry_created_signal_id = str(signal.signal_id)
                signal = None

        reasons = (
            ("ACTIVE_SIGNAL_PRESENT",)
            if signal is not None
            else (
                ("SAME_SNAPSHOT_CREATE_RETRY_CONTEXT_RECONSTRUCTED",)
                if retry_created_signal_id
                else ("NO_ACTIVE_SIGNAL_AS_OF_SNAPSHOT",)
            )
        )

        return ActiveContext(
            equity_ref=ref,
            snapshot_time=ts,
            active_signal_id=str(signal.signal_id) if signal is not None else None,
            active_signal_side=_value(signal.side) if signal is not None else None,
            active_signal_setup=(
                str(signal.setup).strip().upper() if signal is not None else None
            ),
            active_signal_stage=_value(signal.stage) if signal is not None else None,
            active_signal_status=_value(signal.status) if signal is not None else None,
            active_signal_opportunity_key=_opportunity_key(signal),
            active_signal_entry_price=(
                float(signal.created_price)
                if signal is not None and signal.created_price is not None
                else None
            ),
            active_signal_last_snapshot_time=(
                to_ist_naive(signal.last_snapshot_time) if signal is not None else None
            ),
            evaluation_status="AVAILABLE",
            reason_codes=reasons,
            diagnostics={
                "lifecycle": self.lifecycle,
                "time_basis": "CURRENT_OPEN_ROWS",
                "same_snapshot_create_retry_signal_id": retry_created_signal_id,
            },
        )

    def apply_instruction(
        self,
        *,
        snapshot: Any,
        result: AuctionEngineResult,
        context_before: ActiveContext,
    ) -> SignalLifecycleResult:
        """Apply one Auction decision and maintain an existing signal.

        The method is invoked once for every successfully evaluated snapshot.
        HOLD/DEFER/BLOCK still maintain an existing signal's latest price and
        excursion metrics.  No local setup decision is recomputed here.
        """
        final = result.final_decision
        selected = final.selected_candidate
        opportunity_key = selected.opportunity_key if selected is not None else None
        requested = final.action.value
        reasons = list(final.reason_codes)

        existing_present = bool(context_before.active_signal_id)
        signal_opportunity_key = (
            opportunity_key
            if final.action is FinalAction.CREATE
            else context_before.active_signal_opportunity_key
        )
        existing = None
        if existing_present and self.write_enabled:
            from schemas.signal import SignalSchema
            existing = SignalSchema.fetch_by_signal_id(context_before.active_signal_id)
            if existing is None:
                reasons.append("ACTIVE_SIGNAL_CONTEXT_ROW_MISSING")

        defensive_guard_triggered = False
        if final.action is FinalAction.INVALIDATE:
            if existing_present:
                action = "INVALIDATE"
            else:
                action = "NO_ACTION"
                reasons.append("INVALIDATE_SKIPPED_NO_ACTIVE_SIGNAL")
        elif final.action is FinalAction.CREATE:
            # Active-signal context should already have been resolved by
            # DecisionEngine. Keep this check only as a defensive race guard
            # between the pre-evaluation read and the persistence call.
            if existing_present:
                action = "CREATE_BLOCKED"
                defensive_guard_triggered = True
                reasons.append("DEFENSIVE_CREATE_BLOCK_ACTIVE_SIGNAL_PRESENT")
                if (
                    opportunity_key
                    and context_before.active_signal_opportunity_key == opportunity_key
                ):
                    reasons.append(
                        "DEFENSIVE_CREATE_BLOCK_SAME_OPPORTUNITY_ALREADY_ACTIVE"
                    )
            else:
                allowed, permission_reasons = self._creation_permission(snapshot)
                if allowed:
                    action = "CREATE"
                else:
                    action = "CREATE_BLOCKED"
                    reasons.extend(permission_reasons)
        elif existing_present:
            # HOLD/DEFER/BLOCK still maintains current price and excursion facts
            # for an already-active signal.
            action = "UPDATE"
        else:
            action = "NO_ACTION"

        operational_reasons = (
            _execution_reason_codes(action, reasons)
            if self.write_enabled
            else tuple(dict.fromkeys(reasons))
        )

        if not self.write_enabled:
            applied = {
                "CREATE": "WOULD_CREATE",
                "CREATE_BLOCKED": "WOULD_BLOCK_CREATE",
                "UPDATE": "WOULD_UPDATE",
                "INVALIDATE": "WOULD_INVALIDATE",
                "NO_ACTION": "NO_ACTION",
            }[action]
            return SignalLifecycleResult(
                symbol=result.symbol,
                snapshot_time=result.snapshot_time,
                requested_action=requested,
                applied_action=applied,
                signal_id=context_before.active_signal_id,
                opportunity_key=opportunity_key,
                signal_opportunity_key=signal_opportunity_key,
                persisted=False,
                reason_codes=operational_reasons,
                diagnostics={
                    "write_enabled": False,
                    "active_signal_before": existing_present,
                    "active_signal_opportunity_key_before": (
                        context_before.active_signal_opportunity_key
                    ),
                    "active_signal_side_before": context_before.active_signal_side,
                    "defensive_guard_triggered": defensive_guard_triggered,
                },
            )

        idempotent_create_retry = False
        if action == "CREATE":
            if not opportunity_key:
                raise ValueError("CREATE instruction missing opportunity_key")
            deterministic_signal_id = _deterministic_signal_id(
                self.lifecycle, opportunity_key
            )
            idempotent_create_retry = bool(
                context_before.diagnostics.get(
                    "same_snapshot_create_retry_signal_id"
                )
            )
            persisted = self._create(
                snapshot,
                result,
                reason_codes=operational_reasons,
                signal_id=deterministic_signal_id,
            )
        elif action == "UPDATE":
            persisted = self._update(snapshot, result, existing)
        elif action == "INVALIDATE":
            persisted = self._invalidate(snapshot, result, existing)
        else:
            persisted = None

        return SignalLifecycleResult(
            symbol=result.symbol,
            snapshot_time=result.snapshot_time,
            requested_action=requested,
            applied_action=action,
            signal_id=(
                str(persisted.signal_id)
                if persisted is not None
                else context_before.active_signal_id
            ),
            opportunity_key=opportunity_key,
            signal_opportunity_key=signal_opportunity_key,
            persisted=persisted is not None,
            reason_codes=operational_reasons,
            diagnostics={
                "write_enabled": True,
                "active_signal_before": existing_present,
                "active_signal_side_before": context_before.active_signal_side,
                "defensive_guard_triggered": defensive_guard_triggered,
                "idempotent_create_retry": idempotent_create_retry,
            },
        )

    def _creation_permission(self, snapshot: Any) -> Tuple[bool, Tuple[str, ...]]:
        if not self.enforce_creation_permissions:
            return True, ()
        symbol = str(_field(snapshot, "symbol") or "").strip().upper()
        # The Auction runner always resolves the symbol before evaluation. Keep
        # this method defensive for direct callers as well.
        if symbol not in self._symbol_cache:
            self.resolve_equity_ref(symbol)
        row = self._symbol_cache.get(symbol)
        blockers = []
        if row is None:
            blockers.append("CREATE_BLOCKED_SYMBOL_RECORD_MISSING")
        else:
            if not bool(getattr(row, "active", True)):
                blockers.append("CREATE_BLOCKED_SYMBOL_INACTIVE")
            if not bool(getattr(row, "generate_signals", True)):
                blockers.append("CREATE_BLOCKED_SYMBOL_GENERATE_SIGNALS_DISABLED")
        if not bool(_field(snapshot, "gen_signals", False)):
            blockers.append("CREATE_BLOCKED_SNAPSHOT_GENERATE_SIGNALS_DISABLED")
        return not blockers, tuple(blockers)

    def _create(
        self,
        snapshot: Any,
        result: AuctionEngineResult,
        *,
        reason_codes: Tuple[str, ...],
        signal_id: Optional[str] = None,
    ) -> Any:
        from schemas.signal import SignalSchema
        if signal_id:
            existing = SignalSchema.fetch_by_signal_id(signal_id)
            if existing is not None:
                return existing

        payload = result.final_decision.signal_payload
        if payload is None:
            raise ValueError("CREATE instruction missing signal payload")
        candidate = result.final_decision.selected_candidate
        if candidate is None:
            raise ValueError("CREATE instruction missing selected candidate")

        snapshot_time = _field(snapshot, "snapshot_time")
        close = _field(snapshot, "close")
        ltp = _field(snapshot, "ltp")
        ltp_time = _field(snapshot, "ltp_time")
        snapshot_json = sanitize_json(_snapshot_dump(snapshot))
        criteria_json = sanitize_json({
            **payload.criteria_json,
            "auction_state": result.auction_state.current_state.value,
            "decision_reason_codes": list(reason_codes),
            "engine_decision_reason_codes": list(result.final_decision.reason_codes),
        })
        meta_json = sanitize_json({
            **payload.meta_json,
            "initiated_setup_label": payload.setup_label,
            "auction_engine": {
                "opportunity_key": candidate.opportunity_key,
                "candidate_id": candidate.candidate_id,
                "event_key": candidate.event_key,
                "config_version": result.final_decision.config_version,
                "decision_reason_codes": list(reason_codes),
                "engine_decision_reason_codes": list(
                    result.final_decision.reason_codes
                ),
            },
        })
        analytics = calculate_signal_metrics(
            existing_signal=None,
            side=payload.side.value,
            current_price=close,
            current_time=snapshot_time,
        )
        return SignalSchema.create_signal(
            equity_ref=payload.equity_ref,
            symbol=payload.symbol,
            lifecycle=self.lifecycle,
            setup=payload.setup_label,
            side=payload.side.value,
            stage=LifecycleStage.ACTIVE,
            status=SignalStatus.OPEN,
            status_reason=";".join(reason_codes),
            last_eval_time=snapshot_time,
            last_snapshot_time=snapshot_time,
            criteria_json=criteria_json,
            snapshot_json=snapshot_json,
            meta_json=meta_json,
            last_price=_decimal(close),
            ltp=_decimal(ltp if ltp is not None else close),
            ltp_time=ltp_time or snapshot_time,
            signal_id=signal_id,
            **analytics,
        )

    def _update(
        self,
        snapshot: Any,
        result: AuctionEngineResult,
        existing: Optional[Any],
    ) -> Optional[Any]:
        if existing is None:
            return None
        snapshot_time = _field(snapshot, "snapshot_time")
        close = _field(snapshot, "close")
        ltp = _field(snapshot, "ltp")
        ltp_time = _field(snapshot, "ltp_time")
        analytics = calculate_signal_metrics(
            existing_signal=existing,
            side=existing.side,
            current_price=close,
            current_time=snapshot_time,
        )
        meta = dict(existing.meta_json) if isinstance(existing.meta_json, dict) else {}
        meta["latest_auction_evaluation"] = sanitize_json({
            "snapshot_time": result.snapshot_time,
            "auction_state": result.auction_state.current_state.value,
            "final_action": result.final_decision.action.value,
            "reason_codes": list(result.final_decision.reason_codes),
            "selected_opportunity_key": (
                result.final_decision.selected_candidate.opportunity_key
                if result.final_decision.selected_candidate else None
            ),
        })
        from schemas.signal import SignalSchema
        return SignalSchema.update_signal(
            signal_id=existing.signal_id,
            stage=existing.stage,
            status=SignalStatus.OPEN,
            setup=existing.setup,
            status_reason=existing.status_reason,
            last_eval_time=snapshot_time,
            last_snapshot_time=snapshot_time,
            criteria_json=existing.criteria_json,
            snapshot_json=sanitize_json(_snapshot_dump(snapshot)),
            meta_json=sanitize_json(meta),
            last_price=_decimal(close),
            ltp=_decimal(ltp if ltp is not None else close),
            ltp_time=ltp_time or snapshot_time,
            **analytics,
        )

    def _invalidate(
        self,
        snapshot: Any,
        result: AuctionEngineResult,
        existing: Optional[Any],
    ) -> Optional[Any]:
        if existing is None:
            return None
        snapshot_time = _field(snapshot, "snapshot_time")
        close = _field(snapshot, "close")
        ltp = _field(snapshot, "ltp")
        ltp_time = _field(snapshot, "ltp_time")
        analytics = calculate_signal_metrics(
            existing_signal=existing,
            side=existing.side,
            current_price=close,
            current_time=snapshot_time,
        )
        meta = dict(existing.meta_json) if isinstance(existing.meta_json, dict) else {}
        meta["auction_invalidation"] = sanitize_json({
            "snapshot_time": result.snapshot_time,
            "reason_codes": list(result.final_decision.reason_codes),
        })
        from schemas.signal import SignalSchema
        return SignalSchema.close_signal(
            signal_id=existing.signal_id,
            status=SignalStatus.INVALIDATED,
            setup=existing.setup,
            reason=";".join(result.final_decision.reason_codes),
            ts=snapshot_time,
            last_eval_time=snapshot_time,
            last_snapshot_time=snapshot_time,
            criteria_json=existing.criteria_json,
            snapshot_json=sanitize_json(_snapshot_dump(snapshot)),
            meta_json=sanitize_json(meta),
            last_price=_decimal(close),
            ltp=_decimal(ltp if ltp is not None else close),
            ltp_time=ltp_time or snapshot_time,
            **analytics,
        )


__all__ = [
    "SignalLifecycleResult",
    "SignalLifecycleService",
    "_deterministic_signal_id",
    "_execution_reason_codes",
]
