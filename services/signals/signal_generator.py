#!/usr/bin/env python3
"""Auction-driven signal persistence and lifecycle translation.

The snapshot pipeline has already completed Structure and Auction evaluation.
This module deliberately does not run EvidenceEvaluator, setup discovery,
setup selection, or StockAdvisor. It translates the validated Auction result
into the existing signal table, including operational lifecycle stage/status.

Rules
-----
* LOCAL_CONFIRMED creates one signal when no active signal exists.
* Later Auction evaluations update the exact Auction-linked signal.
* SignalGenerator continues to own signals.stage, signals.status, reason/history,
  metrics, audit output, and terminal persistence.
* Auction state and opportunity lifecycle are translated deterministically into
  ACTIVE/EXPAND/PROTECT/TRANSITION/WEAKENING/EXIT_BIAS/FORCE_EXIT.
* Defensive downgrades are immediate; favourable recovery is bounded to one
  operational stage per completed snapshot.
* EXPIRED and SUPERSEDED apply only when the exact originating opportunity is
  explicitly carried with that terminal lifecycle. Absence is an explicit HOLD.
* A different confirmed opportunity updates the active signal posture but does
  not replace it in the same pass.
* Opportunity identity is exact and immutable. There is no symbol/side/setup
  fallback matching.
* snapshot.close is the completed-candle price used for signal analytics.
  snapshot.ltp remains independent and is never substituted with snapshot.close.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from configs.signal_config import SIGNAL_CONFIG
from enums.enums import LifecycleStage, SignalSide, SignalStatus
from schemas.signal import SignalSchema
from schemas.snapshot import (
    AuctionDecisionProjection,
    CandidateProjection,
    OpportunityProjection,
    SnapshotSchema,
)
from schemas.symbol import SymbolSchema
from services.audit.auditlog import write_auditlog
from services.signals.signal_metrics import calculate_signal_metrics
from utils.json_utils import sanitize_json

logger = logging.getLogger(__name__)

DEFAULT_LIFECYCLE = SIGNAL_CONFIG.default_lifecycle.strip().upper()
_SIGNAL_ID_NAMESPACE = uuid.UUID("66bf75a2-909b-4c44-b5db-b9dd2eff598e")
_DOWNSTREAM_CONTRACT_VERSION = "AUCTION_SIGNAL_DOWNSTREAM_V1"

_ALLOWED_AUCTION_ACTIONS = {
    "NO_LOCAL_OPPORTUNITY",
    "LOCAL_WATCH",
    "LOCAL_CONFIRMED",
    "LOCAL_DEFER",
    "LOCAL_BLOCKED",
}

_ALLOWED_AUCTION_STATES = {
    "UNKNOWN",
    "BALANCE",
    "COMPRESSION",
    "BOUNDARY_INTERACTION",
    "FRESH_EXPANSION",
    "ORDERLY_UPTREND",
    "ORDERLY_DOWNTREND",
    "CONTROLLED_PULLBACK",
    "RECOMPRESSION",
    "REACCELERATION",
    "MATURE_EXTENSION",
    "TREND_FAILURE",
    "REVERSAL",
    "CHAOTIC_ROTATION",
}

_ALLOWED_OPPORTUNITY_LIFECYCLES = {
    "WATCH",
    "ELIGIBLE",
    "INELIGIBLE",
    "EXPIRED",
    "SUPERSEDED",
    "CONSUMED",
}


@dataclass(frozen=True)
class AuctionSignalIdentity:
    opportunity_key: str
    candidate_id: str
    boundary_event_key: str
    setup_family: str
    setup_subtype: str
    side: str
    created_snapshot_time: datetime


@dataclass(frozen=True)
class AuctionLifecycleDecision:
    signal_action: str
    stage: LifecycleStage
    status: SignalStatus
    reason_code: str
    opportunity_lifecycle: Optional[str]
    directional_alignment: str
    terminal: bool


@dataclass(frozen=True)
class AuctionSignalInstruction:
    persistence_action: str
    auction_action: str
    auction_state: str
    reason_codes: Tuple[str, ...]
    decision: AuctionDecisionProjection
    active_identity: Optional[AuctionSignalIdentity]
    current_opportunity: Optional[OpportunityProjection]
    current_candidate: Optional[CandidateProjection]
    same_opportunity: bool
    competing_confirmed_opportunity: bool
    lifecycle: AuctionLifecycleDecision


class SignalFetcher:
    """Strict reads used by the live signal path."""

    def fetch_symbol(self, symbol: str) -> Optional[SymbolSchema]:
        return SymbolSchema.fetch_symbol_strict(symbol)

    def fetch_active_signal(self, equity_ref: str, lifecycle: str) -> Optional[SignalSchema]:
        return SignalSchema.fetch_active_signal_strict(equity_ref, lifecycle)

    def fetch_signal_by_id(self, signal_id: str) -> Optional[SignalSchema]:
        return SignalSchema.fetch_by_signal_id_strict(signal_id)


class SignalPersister:
    """Reuse the current signal table and analytics semantics."""

    def create(
        self,
        *,
        snapshot: SnapshotSchema,
        equity_ref: str,
        lifecycle: str,
        instruction: AuctionSignalInstruction,
        signal_id: str,
        criteria_json: Dict[str, Any],
        meta_json: Dict[str, Any],
        analytics: Dict[str, Any],
    ) -> SignalSchema:
        identity = _require_instruction_identity(instruction)
        return SignalSchema.create_signal(
            equity_ref=equity_ref,
            symbol=snapshot.symbol,
            lifecycle=lifecycle,
            setup=identity.setup_family,
            side=SignalSide.from_string(identity.side),
            stage=instruction.lifecycle.stage,
            status=instruction.lifecycle.status,
            status_reason=_status_reason(instruction),
            last_eval_time=snapshot.snapshot_time,
            last_snapshot_time=snapshot.snapshot_time,
            criteria_json=criteria_json,
            snapshot_json=_snapshot_json(snapshot),
            meta_json=meta_json,
            last_price=_decimal_required(snapshot.close, "snapshot.close"),
            ltp=_decimal_optional(snapshot.ltp, "snapshot.ltp"),
            ltp_time=snapshot.ltp_time,
            signal_id=signal_id,
            **analytics,
        )

    def update(
        self,
        *,
        signal: SignalSchema,
        snapshot: SnapshotSchema,
        instruction: AuctionSignalInstruction,
        criteria_json: Dict[str, Any],
        meta_json: Dict[str, Any],
        analytics: Dict[str, Any],
    ) -> Optional[SignalSchema]:
        return SignalSchema.update_signal(
            signal_id=signal.signal_id,
            stage=instruction.lifecycle.stage,
            status=instruction.lifecycle.status,
            setup=signal.setup,
            status_reason=_status_reason(instruction),
            last_eval_time=snapshot.snapshot_time,
            last_snapshot_time=snapshot.snapshot_time,
            criteria_json=criteria_json,
            snapshot_json=_snapshot_json(snapshot),
            meta_json=meta_json,
            last_price=_decimal_required(snapshot.close, "snapshot.close"),
            ltp=_decimal_optional(snapshot.ltp, "snapshot.ltp"),
            ltp_time=snapshot.ltp_time,
            **analytics,
        )

    def close(
        self,
        *,
        signal: SignalSchema,
        snapshot: SnapshotSchema,
        instruction: AuctionSignalInstruction,
        criteria_json: Dict[str, Any],
        meta_json: Dict[str, Any],
        analytics: Dict[str, Any],
    ) -> Optional[SignalSchema]:
        if instruction.lifecycle.status == SignalStatus.OPEN:
            raise ValueError("SignalPersister.close requires a terminal status")
        return SignalSchema.close_signal(
            signal_id=signal.signal_id,
            stage=instruction.lifecycle.stage,
            status=instruction.lifecycle.status,
            setup=signal.setup,
            reason=_status_reason(instruction),
            ts=snapshot.snapshot_time,
            last_eval_time=snapshot.snapshot_time,
            last_snapshot_time=snapshot.snapshot_time,
            criteria_json=criteria_json,
            snapshot_json=_snapshot_json(snapshot),
            meta_json=meta_json,
            last_price=_decimal_required(snapshot.close, "snapshot.close"),
            ltp=_decimal_optional(snapshot.ltp, "snapshot.ltp"),
            ltp_time=snapshot.ltp_time,
            **analytics,
        )


class SignalAssembler:
    """Translate one validated snapshot into create/update/no-action."""

    def __init__(
        self,
        *,
        fetcher: Optional[SignalFetcher] = None,
        persister: Optional[SignalPersister] = None,
    ) -> None:
        self.lifecycle = DEFAULT_LIFECYCLE
        self.fetcher = fetcher or SignalFetcher()
        self.persister = persister or SignalPersister()

    def assemble(self, snapshot: SnapshotSchema) -> List[Tuple[str, SignalSchema]]:
        if not isinstance(snapshot, SnapshotSchema):
            raise TypeError("SignalAssembler requires a validated SnapshotSchema")
        if snapshot.auction.status != "OK":
            raise ValueError(
                f"Auction status must be OK before signal processing: {snapshot.auction.status}"
            )
        if snapshot.auction.decision is None or snapshot.auction.state is None:
            raise ValueError("Validated Auction result is missing state or decision")

        symbol = snapshot.symbol.strip().upper()
        symbol_row = self.fetcher.fetch_symbol(symbol)
        if symbol_row is None:
            logger.info("SIG_SKIP | %s @ %s | reason=SYMBOL_RECORD_MISSING", symbol, snapshot.snapshot_time)
            return []
        if not bool(symbol_row.active):
            logger.info("SIG_SKIP | %s @ %s | reason=SYMBOL_INACTIVE", symbol, snapshot.snapshot_time)
            return []
        if not bool(symbol_row.generate_signals):
            logger.info(
                "SIG_SKIP | %s @ %s | reason=SYMBOL_GENERATE_SIGNALS_DISABLED",
                symbol,
                snapshot.snapshot_time,
            )
            return []
        if not snapshot.gen_signals:
            logger.info(
                "SIG_SKIP | %s @ %s | reason=SNAPSHOT_GENERATE_SIGNALS_DISABLED",
                symbol,
                snapshot.snapshot_time,
            )
            return []

        equity_ref = str(symbol_row.equity_ref or symbol).strip().upper()
        existing_signal = self.fetcher.fetch_active_signal(equity_ref, self.lifecycle)
        instruction = _build_instruction(snapshot, existing_signal)

        if instruction.persistence_action == "NO_ACTION":
            logger.info(
                "SIG_NO_ACTION | %s @ %s | auction_action=%s lifecycle_reason=%s",
                equity_ref,
                snapshot.snapshot_time,
                instruction.auction_action,
                instruction.lifecycle.reason_code,
            )
            return []

        if instruction.persistence_action == "CREATE":
            identity = _require_instruction_identity(instruction)
            deterministic_id = _deterministic_signal_id(self.lifecycle, identity.opportunity_key)
            existing_by_id = self.fetcher.fetch_signal_by_id(deterministic_id)
            if existing_by_id is not None:
                if existing_by_id.status == SignalStatus.OPEN:
                    existing_signal = existing_by_id
                    instruction = _as_update_instruction(snapshot, instruction, existing_by_id)
                else:
                    logger.info(
                        "SIG_NO_ACTION | %s @ %s | reason=OPPORTUNITY_ALREADY_CONSUMED signal_id=%s opportunity=%s status=%s",
                        equity_ref,
                        snapshot.snapshot_time,
                        existing_by_id.signal_id,
                        identity.opportunity_key,
                        _enum_text(existing_by_id.status),
                    )
                    return []
            else:
                criteria_json = _criteria_json(snapshot, instruction)
                meta_json = _create_meta_json(snapshot, instruction, criteria_json)
                analytics = calculate_signal_metrics(
                    existing_signal=None,
                    side=identity.side,
                    current_price=snapshot.close,
                    current_time=snapshot.snapshot_time,
                )
                persisted = self.persister.create(
                    snapshot=snapshot,
                    equity_ref=equity_ref,
                    lifecycle=self.lifecycle,
                    instruction=instruction,
                    signal_id=deterministic_id,
                    criteria_json=criteria_json,
                    meta_json=meta_json,
                    analytics=analytics,
                )
                event_action = instruction.lifecycle.signal_action
                self._audit(
                    snapshot=snapshot,
                    existing_signal=None,
                    persisted_signal=persisted,
                    instruction=instruction,
                    analytics=analytics,
                    action=event_action,
                )
                logger.info(
                    "SIG_%s | %s @ %s | signal_id=%s opportunity=%s candidate=%s setup=%s side=%s stage=%s status=%s",
                    event_action,
                    equity_ref,
                    snapshot.snapshot_time,
                    persisted.signal_id,
                    identity.opportunity_key,
                    identity.candidate_id,
                    identity.setup_family,
                    identity.side,
                    instruction.lifecycle.stage.value,
                    instruction.lifecycle.status.value,
                )
                return [(event_action, persisted)]

        if instruction.persistence_action not in {"UPDATE", "CLOSE"}:
            raise ValueError(
                f"Unsupported signal persistence action: {instruction.persistence_action}"
            )
        if existing_signal is None:
            raise ValueError(
                f"{instruction.persistence_action} instruction requires an existing active signal"
            )

        active_identity = _signal_identity(existing_signal)
        criteria_json = _criteria_json(snapshot, instruction)
        meta_json = _update_meta_json(
            existing_signal=existing_signal,
            snapshot=snapshot,
            instruction=instruction,
            active_identity=active_identity,
            criteria_json=criteria_json,
        )
        analytics = calculate_signal_metrics(
            existing_signal=existing_signal,
            side=existing_signal.side,
            current_price=snapshot.close,
            current_time=snapshot.snapshot_time,
        )
        if instruction.persistence_action == "CLOSE":
            persisted = self.persister.close(
                signal=existing_signal,
                snapshot=snapshot,
                instruction=instruction,
                criteria_json=criteria_json,
                meta_json=meta_json,
                analytics=analytics,
            )
        else:
            persisted = self.persister.update(
                signal=existing_signal,
                snapshot=snapshot,
                instruction=instruction,
                criteria_json=criteria_json,
                meta_json=meta_json,
                analytics=analytics,
            )
        if persisted is None:
            raise RuntimeError(
                f"Signal {instruction.persistence_action.lower()} returned no row for "
                f"signal_id={existing_signal.signal_id}"
            )

        event_action = instruction.lifecycle.signal_action
        self._audit(
            snapshot=snapshot,
            existing_signal=existing_signal,
            persisted_signal=persisted,
            instruction=instruction,
            analytics=analytics,
            action=event_action,
        )
        logger.info(
            "SIG_%s | %s @ %s | signal_id=%s active_opportunity=%s current_opportunity=%s "
            "same_opportunity=%s auction_action=%s stage=%s status=%s lifecycle_reason=%s",
            event_action,
            equity_ref,
            snapshot.snapshot_time,
            persisted.signal_id,
            active_identity.opportunity_key,
            (
                instruction.current_opportunity.opportunity_key
                if instruction.current_opportunity is not None
                else None
            ),
            instruction.same_opportunity,
            instruction.auction_action,
            instruction.lifecycle.stage.value,
            instruction.lifecycle.status.value,
            instruction.lifecycle.reason_code,
        )
        return [(event_action, persisted)]

    def _audit(
        self,
        *,
        snapshot: SnapshotSchema,
        existing_signal: Optional[SignalSchema],
        persisted_signal: SignalSchema,
        instruction: AuctionSignalInstruction,
        analytics: Dict[str, Any],
        action: str,
    ) -> None:
        if not SIGNAL_CONFIG.audit.enabled:
            return
        previous_state = (
            _enum_text(existing_signal.stage)
            if existing_signal is not None
            else None
        )
        current_opportunity_key = (
            instruction.current_opportunity.opportunity_key
            if instruction.current_opportunity is not None
            else None
        )
        try:
            write_auditlog(
                entity_type=SIGNAL_CONFIG.audit.entity_type,
                entity_id=persisted_signal.signal_id,
                symbol=snapshot.symbol,
                evaluation_stage=SIGNAL_CONFIG.audit.evaluation_stage,
                previous_state=previous_state,
                new_state=instruction.lifecycle.stage.value,
                action=action,
                reason_code=instruction.lifecycle.reason_code,
                reason_text=_status_reason(instruction),
                confidence=None,
                ts=snapshot.snapshot_time,
                payload_json={
                    "signal_id": persisted_signal.signal_id,
                    "snapshot_time": snapshot.snapshot_time,
                    "close": snapshot.close,
                    "ltp": snapshot.ltp,
                    "auction_action": instruction.auction_action,
                    "auction_state": instruction.auction_state,
                    "active_opportunity_key": (
                        instruction.active_identity.opportunity_key
                        if instruction.active_identity is not None
                        else None
                    ),
                    "current_opportunity_key": current_opportunity_key,
                    "current_candidate_id": _current_candidate_id(instruction),
                    "same_opportunity": instruction.same_opportunity,
                    "competing_confirmed_opportunity": instruction.competing_confirmed_opportunity,
                    "signal_action": instruction.lifecycle.signal_action,
                    "signal_stage": instruction.lifecycle.stage.value,
                    "signal_status": instruction.lifecycle.status.value,
                    "signal_reason_code": instruction.lifecycle.reason_code,
                    "opportunity_lifecycle": instruction.lifecycle.opportunity_lifecycle,
                    "directional_alignment": instruction.lifecycle.directional_alignment,
                    "terminal": instruction.lifecycle.terminal,
                    "analytics": analytics,
                },
            )
        except Exception:
            logger.exception(
                "Signal audit write failed | signal_id=%s snapshot_time=%s",
                persisted_signal.signal_id,
                snapshot.snapshot_time,
            )


class SignalGenerator:
    """Public live entry point retained for scripts/gen_signals.py."""

    def __init__(self, snapshot: SnapshotSchema):
        if not isinstance(snapshot, SnapshotSchema):
            raise TypeError("SignalGenerator requires a validated SnapshotSchema")
        self.snapshot = snapshot
        self.assembler = SignalAssembler()

    def generate_signal(self) -> Optional[str]:
        # Do not swallow exceptions.  scripts/gen_signals.py must leave the
        # snapshot unprocessed when signal processing fails.
        events = self.assembler.assemble(self.snapshot)
        return events[-1][0] if events else None

    def generate(self) -> Optional[str]:
        return self.generate_signal()


# ---------------------------------------------------------------------------
# Strict Auction-to-signal translation
# ---------------------------------------------------------------------------
def _build_instruction(
    snapshot: SnapshotSchema,
    existing_signal: Optional[SignalSchema],
) -> AuctionSignalInstruction:
    decision = snapshot.auction.decision
    state = snapshot.auction.state
    if decision is None or state is None:
        raise ValueError("Auction decision/state missing")

    action = decision.action.strip().upper()
    if action not in _ALLOWED_AUCTION_ACTIONS:
        raise ValueError(f"Unsupported Auction action for signal processing: {action}")

    active_identity = _signal_identity(existing_signal) if existing_signal is not None else None

    if action == "LOCAL_CONFIRMED":
        candidate, opportunity = _confirmed_selection(snapshot, decision)
        same = bool(
            active_identity is not None
            and active_identity.opportunity_key == opportunity.opportunity_key
        )
        lifecycle = _resolve_signal_lifecycle(
            snapshot=snapshot,
            existing_signal=existing_signal,
            auction_action=action,
            active_identity=active_identity,
            current_opportunity=opportunity,
            same_opportunity=same,
            competing_confirmed_opportunity=bool(active_identity is not None and not same),
        )
        return AuctionSignalInstruction(
            persistence_action=(
                "CREATE"
                if existing_signal is None
                else "CLOSE"
                if lifecycle.terminal
                else "UPDATE"
            ),
            auction_action=action,
            auction_state=state.current,
            reason_codes=tuple(decision.reason_codes),
            decision=decision,
            active_identity=active_identity,
            current_opportunity=opportunity,
            current_candidate=candidate,
            same_opportunity=same,
            competing_confirmed_opportunity=bool(active_identity is not None and not same),
            lifecycle=lifecycle,
        )

    current_opportunity: Optional[OpportunityProjection] = None
    current_candidate: Optional[CandidateProjection] = None
    same = False
    if active_identity is not None:
        current_opportunity = _optional_opportunity(
            snapshot,
            active_identity.opportunity_key,
        )
        if current_opportunity is not None:
            current_candidate = _candidate_for_opportunity(snapshot, current_opportunity)
            same = True

    lifecycle = _resolve_signal_lifecycle(
        snapshot=snapshot,
        existing_signal=existing_signal,
        auction_action=action,
        active_identity=active_identity,
        current_opportunity=current_opportunity,
        same_opportunity=same,
        competing_confirmed_opportunity=False,
    )
    return AuctionSignalInstruction(
        persistence_action=(
            "NO_ACTION"
            if existing_signal is None
            else "CLOSE"
            if lifecycle.terminal
            else "UPDATE"
        ),
        auction_action=action,
        auction_state=state.current,
        reason_codes=tuple(decision.reason_codes),
        decision=decision,
        active_identity=active_identity,
        current_opportunity=current_opportunity,
        current_candidate=current_candidate,
        same_opportunity=same,
        competing_confirmed_opportunity=False,
        lifecycle=lifecycle,
    )



def _resolve_signal_lifecycle(
    *,
    snapshot: SnapshotSchema,
    existing_signal: Optional[SignalSchema],
    auction_action: str,
    active_identity: Optional[AuctionSignalIdentity],
    current_opportunity: Optional[OpportunityProjection],
    same_opportunity: bool,
    competing_confirmed_opportunity: bool,
) -> AuctionLifecycleDecision:
    state = snapshot.auction.state
    if state is None:
        raise ValueError("Auction state missing while resolving signal lifecycle")
    state_name = state.current.strip().upper()
    if state_name not in _ALLOWED_AUCTION_STATES:
        raise ValueError(f"Unsupported Auction state for signal lifecycle: {state_name}")

    if existing_signal is None:
        if auction_action == "LOCAL_CONFIRMED":
            opportunity_lifecycle = _opportunity_lifecycle(current_opportunity)
            if opportunity_lifecycle not in {"ELIGIBLE", "CONSUMED"}:
                raise ValueError(
                    "LOCAL_CONFIRMED can create only from an ELIGIBLE/CONSUMED opportunity"
                )
            return AuctionLifecycleDecision(
                signal_action="CREATE",
                stage=LifecycleStage.ACTIVE,
                status=SignalStatus.OPEN,
                reason_code="AUCTION_CONFIRMED_SIGNAL_CREATED",
                opportunity_lifecycle=opportunity_lifecycle,
                directional_alignment=_directional_alignment(snapshot, _current_side_for_lifecycle(current_opportunity)),
                terminal=False,
            )
        return AuctionLifecycleDecision(
            signal_action="NO_ACTION",
            stage=LifecycleStage.DISCOVERY,
            status=SignalStatus.OPEN,
            reason_code=f"NO_ACTIVE_SIGNAL_{auction_action}",
            opportunity_lifecycle=_opportunity_lifecycle(current_opportunity),
            directional_alignment="NEUTRAL",
            terminal=False,
        )

    if _enum_text(existing_signal.status) != SignalStatus.OPEN.value:
        raise ValueError(
            f"Signal lifecycle update requires OPEN status: {existing_signal.signal_id} "
            f"status={_enum_text(existing_signal.status)}"
        )
    if active_identity is None:
        raise ValueError("Existing signal is missing Auction identity")

    existing_stage = LifecycleStage.from_string(existing_signal.stage)
    active_side = active_identity.side

    if competing_confirmed_opportunity:
        if current_opportunity is None:
            raise ValueError("Competing confirmed opportunity is missing")
        opportunity_lifecycle = _opportunity_lifecycle(current_opportunity)
        current_side = _current_side_for_lifecycle(current_opportunity)
        if current_side == active_side:
            target_stage = LifecycleStage.PROTECT
            reason_code = "COMPETING_SAME_SIDE_OPPORTUNITY_PROTECT"
            alignment = "ALIGNED"
        else:
            target_stage = LifecycleStage.EXIT_BIAS
            reason_code = "COMPETING_OPPOSITE_OPPORTUNITY_EXIT_BIAS"
            alignment = "OPPOSITE"
        return _open_lifecycle_transition(
            existing_stage=existing_stage,
            target_stage=target_stage,
            reason_code=reason_code,
            opportunity_lifecycle=opportunity_lifecycle,
            directional_alignment=alignment,
        )

    if not same_opportunity or current_opportunity is None:
        return AuctionLifecycleDecision(
            signal_action="HOLD",
            stage=existing_stage,
            status=SignalStatus.OPEN,
            reason_code="ACTIVE_OPPORTUNITY_NOT_IN_CURRENT_PROJECTION_HOLD",
            opportunity_lifecycle=None,
            directional_alignment="NEUTRAL",
            terminal=False,
        )

    opportunity_lifecycle = _opportunity_lifecycle(current_opportunity)
    if opportunity_lifecycle == "EXPIRED":
        return AuctionLifecycleDecision(
            signal_action="EXPIRE",
            stage=LifecycleStage.FORCE_EXIT,
            status=SignalStatus.EXPIRED,
            reason_code="ACTIVE_AUCTION_OPPORTUNITY_EXPIRED",
            opportunity_lifecycle=opportunity_lifecycle,
            directional_alignment=_directional_alignment(snapshot, active_side),
            terminal=True,
        )
    if opportunity_lifecycle == "SUPERSEDED":
        return AuctionLifecycleDecision(
            signal_action="REPLACE",
            stage=LifecycleStage.FORCE_EXIT,
            status=SignalStatus.REPLACED,
            reason_code="ACTIVE_AUCTION_OPPORTUNITY_SUPERSEDED",
            opportunity_lifecycle=opportunity_lifecycle,
            directional_alignment=_directional_alignment(snapshot, active_side),
            terminal=True,
        )

    base_stage, alignment = _stage_from_auction_state(snapshot, active_side)

    if opportunity_lifecycle == "INELIGIBLE":
        target_stage = _more_defensive(base_stage, LifecycleStage.EXIT_BIAS)
        reason_code = "ACTIVE_AUCTION_OPPORTUNITY_INELIGIBLE"
    elif opportunity_lifecycle == "WATCH":
        target_stage = _watch_stage(base_stage)
        reason_code = f"ACTIVE_AUCTION_OPPORTUNITY_WATCH_{state_name}_{alignment}"
    elif opportunity_lifecycle in {"ELIGIBLE", "CONSUMED"}:
        if auction_action == "LOCAL_CONFIRMED":
            target_stage = base_stage
            reason_code = f"AUCTION_CONFIRMED_{state_name}_{alignment}"
        elif auction_action == "LOCAL_WATCH":
            target_stage = _watch_stage(base_stage)
            reason_code = f"AUCTION_WATCH_{state_name}_{alignment}"
        elif auction_action == "LOCAL_DEFER":
            target_stage = _more_defensive(base_stage, LifecycleStage.TRANSITION)
            reason_code = f"AUCTION_DEFER_{state_name}_{alignment}"
        elif auction_action == "LOCAL_BLOCKED":
            target_stage = _more_defensive(base_stage, LifecycleStage.WEAKENING)
            reason_code = f"AUCTION_BLOCKED_{state_name}_{alignment}"
        elif auction_action == "NO_LOCAL_OPPORTUNITY":
            target_stage = existing_stage
            reason_code = "ACTIVE_ELIGIBLE_OPPORTUNITY_NO_LOCAL_DECISION_HOLD"
        else:
            raise ValueError(f"Unsupported Auction action: {auction_action}")
    else:
        raise ValueError(
            f"Unsupported active opportunity lifecycle: {opportunity_lifecycle}"
        )

    return _open_lifecycle_transition(
        existing_stage=existing_stage,
        target_stage=target_stage,
        reason_code=reason_code,
        opportunity_lifecycle=opportunity_lifecycle,
        directional_alignment=alignment,
    )


def _open_lifecycle_transition(
    *,
    existing_stage: LifecycleStage,
    target_stage: LifecycleStage,
    reason_code: str,
    opportunity_lifecycle: Optional[str],
    directional_alignment: str,
) -> AuctionLifecycleDecision:
    resolved_stage = target_stage
    if _stage_rank(target_stage) > _stage_rank(existing_stage):
        resolved_stage = _bounded_recovery_stage(existing_stage, target_stage)

    if resolved_stage == existing_stage:
        signal_action = "HOLD"
    elif _stage_rank(resolved_stage) > _stage_rank(existing_stage):
        signal_action = "PROMOTE"
    else:
        signal_action = "DOWNGRADE"
    return AuctionLifecycleDecision(
        signal_action=signal_action,
        stage=resolved_stage,
        status=SignalStatus.OPEN,
        reason_code=reason_code,
        opportunity_lifecycle=opportunity_lifecycle,
        directional_alignment=directional_alignment,
        terminal=False,
    )


def _bounded_recovery_stage(
    existing_stage: LifecycleStage,
    requested_stage: LifecycleStage,
) -> LifecycleStage:
    # Defensive downgrades are immediate. Recoveries are deliberately bounded
    # to one operational step per completed snapshot so a single favourable
    # Auction state cannot jump an EXIT_BIAS signal straight back to ACTIVE.
    recovery_steps = {
        LifecycleStage.FORCE_EXIT: LifecycleStage.FORCE_EXIT,
        LifecycleStage.EXIT_BIAS: LifecycleStage.WEAKENING,
        LifecycleStage.WEAKENING: LifecycleStage.PROTECT,
        LifecycleStage.TRANSITION: LifecycleStage.PROTECT,
        LifecycleStage.DISCOVERY: LifecycleStage.BUILDING,
        LifecycleStage.BUILDING: LifecycleStage.ACTIVE,
        LifecycleStage.PROTECT: LifecycleStage.ACTIVE,
        LifecycleStage.ACTIVE: LifecycleStage.EXPAND,
        LifecycleStage.EXPAND: LifecycleStage.EXPAND,
        LifecycleStage.EARLY_CONTINUATION: LifecycleStage.ACTIVE,
        LifecycleStage.MATURE_CONTINUATION: LifecycleStage.PROTECT,
        LifecycleStage.PULLBACK_CONTINUATION: LifecycleStage.PROTECT,
    }
    if existing_stage not in recovery_steps:
        raise ValueError(f"No recovery step defined for lifecycle stage: {existing_stage.value}")
    next_stage = recovery_steps[existing_stage]
    return (
        requested_stage
        if _stage_rank(requested_stage) <= _stage_rank(next_stage)
        else next_stage
    )


def _stage_from_auction_state(
    snapshot: SnapshotSchema,
    active_side: str,
) -> Tuple[LifecycleStage, str]:
    state = snapshot.auction.state
    if state is None:
        raise ValueError("Auction state missing")
    state_name = state.current.strip().upper()
    alignment = _directional_alignment(snapshot, active_side)

    if state_name in {"FRESH_EXPANSION", "REACCELERATION"}:
        if alignment == "ALIGNED":
            return LifecycleStage.EXPAND, alignment
        if alignment == "OPPOSITE":
            return LifecycleStage.EXIT_BIAS, alignment
        return LifecycleStage.TRANSITION, alignment

    if state_name in {"ORDERLY_UPTREND", "ORDERLY_DOWNTREND"}:
        if alignment == "ALIGNED":
            return LifecycleStage.ACTIVE, alignment
        if alignment == "OPPOSITE":
            return LifecycleStage.EXIT_BIAS, alignment
        return LifecycleStage.TRANSITION, alignment

    if state_name == "MATURE_EXTENSION":
        if alignment == "OPPOSITE":
            return LifecycleStage.EXIT_BIAS, alignment
        return LifecycleStage.PROTECT, alignment

    if state_name == "CONTROLLED_PULLBACK":
        if alignment == "OPPOSITE":
            return LifecycleStage.WEAKENING, alignment
        return LifecycleStage.PROTECT, alignment

    if state_name == "TREND_FAILURE":
        if alignment == "ALIGNED":
            return LifecycleStage.ACTIVE, alignment
        if alignment == "OPPOSITE":
            return LifecycleStage.WEAKENING, alignment
        return LifecycleStage.TRANSITION, alignment

    if state_name == "REVERSAL":
        if alignment == "ALIGNED":
            return LifecycleStage.ACTIVE, alignment
        if alignment == "OPPOSITE":
            return LifecycleStage.EXIT_BIAS, alignment
        return LifecycleStage.WEAKENING, alignment

    if state_name in {"BALANCE", "COMPRESSION", "RECOMPRESSION"}:
        return LifecycleStage.PROTECT, "NEUTRAL"

    if state_name in {"BOUNDARY_INTERACTION", "CHAOTIC_ROTATION", "UNKNOWN"}:
        return LifecycleStage.TRANSITION, "NEUTRAL"

    raise ValueError(f"Unsupported Auction state for lifecycle stage: {state_name}")


def _directional_alignment(snapshot: SnapshotSchema, active_side: str) -> str:
    direction = _auction_direction(snapshot)
    side = active_side.strip().upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError(f"Invalid signal side for lifecycle alignment: {active_side}")
    if direction == "NEUTRAL":
        return "NEUTRAL"
    return "ALIGNED" if direction == side else "OPPOSITE"


def _auction_direction(snapshot: SnapshotSchema) -> str:
    state = snapshot.auction.state
    if state is None:
        raise ValueError("Auction state missing")
    state_name = state.current.strip().upper()
    if state_name == "ORDERLY_UPTREND":
        return "BUY"
    if state_name == "ORDERLY_DOWNTREND":
        return "SELL"
    if state_name in {
        "UNKNOWN",
        "BALANCE",
        "COMPRESSION",
        "BOUNDARY_INTERACTION",
        "RECOMPRESSION",
        "CHAOTIC_ROTATION",
    }:
        return "NEUTRAL"
    raw_side = snapshot.structure.raw.side.strip().upper()
    if raw_side not in {"BUY", "SELL", "NEUTRAL"}:
        raise ValueError(f"Invalid structure.raw.side for lifecycle alignment: {raw_side}")
    return raw_side


def _watch_stage(base_stage: LifecycleStage) -> LifecycleStage:
    if base_stage in {LifecycleStage.ACTIVE, LifecycleStage.EXPAND}:
        return LifecycleStage.PROTECT
    return base_stage


def _more_defensive(
    left: LifecycleStage,
    right: LifecycleStage,
) -> LifecycleStage:
    return left if _stage_rank(left) <= _stage_rank(right) else right


def _stage_rank(stage: LifecycleStage) -> int:
    ranks = SIGNAL_CONFIG.resolution.stage_rank
    key = stage.value
    if key not in ranks:
        raise ValueError(f"Missing signal lifecycle stage rank: {key}")
    return int(ranks[key])


def _opportunity_lifecycle(
    opportunity: Optional[OpportunityProjection],
) -> Optional[str]:
    if opportunity is None:
        return None
    lifecycle = opportunity.lifecycle.strip().upper()
    if lifecycle not in _ALLOWED_OPPORTUNITY_LIFECYCLES:
        raise ValueError(f"Unsupported Auction opportunity lifecycle: {lifecycle}")
    return lifecycle


def _current_side_for_lifecycle(opportunity: Optional[OpportunityProjection]) -> str:
    if opportunity is None:
        raise ValueError("Current opportunity is required for lifecycle side")
    side = opportunity.side.strip().upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError(f"Invalid opportunity side for lifecycle: {side}")
    return side


def _confirmed_selection(
    snapshot: SnapshotSchema,
    decision: AuctionDecisionProjection,
) -> Tuple[Optional[CandidateProjection], OpportunityProjection]:
    if decision.manager_action != "SELECT":
        raise ValueError("LOCAL_CONFIRMED requires manager_action=SELECT")
    if decision.selected_candidate_id is None:
        raise ValueError("LOCAL_CONFIRMED missing selected_candidate_id")
    if decision.selected_opportunity_key is None:
        raise ValueError("LOCAL_CONFIRMED missing selected_opportunity_key")
    if decision.family is None or decision.subtype is None or decision.side is None:
        raise ValueError("LOCAL_CONFIRMED missing family/subtype/side")
    if decision.entry_price is None:
        raise ValueError("LOCAL_CONFIRMED missing entry_price")

    opportunity = _require_unique_opportunity(snapshot, decision.selected_opportunity_key)
    candidate = _optional_candidate(snapshot, decision.selected_candidate_id)

    if opportunity.selected_candidate_id != decision.selected_candidate_id:
        raise ValueError("Selected opportunity does not point to Auction decision candidate")
    if opportunity.primary_family != decision.family:
        raise ValueError("Selected opportunity family differs from Auction decision")
    if opportunity.primary_subtype != decision.subtype:
        raise ValueError("Selected opportunity subtype differs from Auction decision")
    if opportunity.side != decision.side:
        raise ValueError("Selected opportunity side differs from Auction decision")

    # The compact public candidate list contains current observations only.  A
    # selected candidate may be omitted on later snapshots while the decision
    # and opportunity continue to carry its stable identity and geometry.
    if candidate is not None:
        if candidate.opportunity_key != opportunity.opportunity_key:
            raise ValueError("Selected candidate/opportunity identity mismatch")
        if candidate.family != decision.family:
            raise ValueError("Selected candidate family differs from Auction decision")
        if candidate.subtype != decision.subtype:
            raise ValueError("Selected candidate subtype differs from Auction decision")
        if candidate.side != decision.side:
            raise ValueError("Selected candidate side differs from Auction decision")
        if candidate.eligibility != "ELIGIBLE":
            raise ValueError(
                f"LOCAL_CONFIRMED selected non-eligible candidate: {candidate.eligibility}"
            )
        tolerance = max(1e-9, abs(decision.entry_price) * 1e-9)
        if abs(candidate.entry_price - decision.entry_price) > tolerance:
            raise ValueError("Selected candidate entry_price differs from Auction decision")
    return candidate, opportunity


def _candidate_for_opportunity(
    snapshot: SnapshotSchema,
    opportunity: OpportunityProjection,
) -> Optional[CandidateProjection]:
    candidate = _optional_candidate(snapshot, opportunity.primary_candidate_id)
    if candidate is not None and candidate.opportunity_key != opportunity.opportunity_key:
        raise ValueError("Opportunity primary candidate points to another opportunity")
    return candidate


def _optional_candidate(
    snapshot: SnapshotSchema,
    candidate_id: str,
) -> Optional[CandidateProjection]:
    matches = [item for item in snapshot.auction.candidates if item.candidate_id == candidate_id]
    if len(matches) > 1:
        raise ValueError(f"Duplicate Auction candidate identity in snapshot: {candidate_id}")
    return matches[0] if matches else None


def _require_unique_opportunity(
    snapshot: SnapshotSchema,
    opportunity_key: str,
) -> OpportunityProjection:
    matches = [
        item
        for item in snapshot.auction.opportunities
        if item.opportunity_key == opportunity_key
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected one Auction opportunity for {opportunity_key}, found {len(matches)}"
        )
    return matches[0]


def _optional_opportunity(
    snapshot: SnapshotSchema,
    opportunity_key: str,
) -> Optional[OpportunityProjection]:
    matches = [
        item
        for item in snapshot.auction.opportunities
        if item.opportunity_key == opportunity_key
    ]
    if len(matches) > 1:
        raise ValueError(
            f"Duplicate Auction opportunity identity in snapshot: {opportunity_key}"
        )
    return matches[0] if matches else None


def _signal_identity(signal: Optional[SignalSchema]) -> Optional[AuctionSignalIdentity]:
    if signal is None:
        return None
    meta = signal.meta_json
    if not isinstance(meta, dict):
        raise ValueError(
            f"Active signal {signal.signal_id} has no Auction metadata; clean transition is required"
        )
    if "auction_signal" not in meta:
        raise ValueError(
            f"Active signal {signal.signal_id} is not Auction-linked; clean transition is required"
        )
    block = meta["auction_signal"]
    if not isinstance(block, dict):
        raise ValueError("meta_json.auction_signal must be an object")

    required = (
        "opportunity_key",
        "candidate_id",
        "boundary_event_key",
        "setup_family",
        "setup_subtype",
        "side",
        "created_snapshot_time",
    )
    missing = [name for name in required if name not in block]
    if missing:
        raise ValueError(
            f"Active signal {signal.signal_id} Auction identity missing fields: {missing}"
        )
    created_time = block["created_snapshot_time"]
    if isinstance(created_time, str):
        created_time = datetime.fromisoformat(created_time)
    if not isinstance(created_time, datetime):
        raise ValueError("auction_signal.created_snapshot_time must be a datetime")

    identity = AuctionSignalIdentity(
        opportunity_key=_required_text(block["opportunity_key"], "auction_signal.opportunity_key"),
        candidate_id=_required_text(block["candidate_id"], "auction_signal.candidate_id"),
        boundary_event_key=_required_text(block["boundary_event_key"], "auction_signal.boundary_event_key"),
        setup_family=_required_text(block["setup_family"], "auction_signal.setup_family").upper(),
        setup_subtype=_required_text(block["setup_subtype"], "auction_signal.setup_subtype").upper(),
        side=_required_text(block["side"], "auction_signal.side").upper(),
        created_snapshot_time=created_time,
    )
    if identity.setup_family != signal.setup.strip().upper():
        raise ValueError(
            f"Signal setup identity mismatch: signal.setup={signal.setup} auction={identity.setup_family}"
        )
    if identity.side != _enum_text(signal.side):
        raise ValueError(
            f"Signal side identity mismatch: signal.side={_enum_text(signal.side)} auction={identity.side}"
        )
    return identity


def _require_instruction_identity(
    instruction: AuctionSignalInstruction,
) -> AuctionSignalIdentity:
    opportunity = instruction.current_opportunity
    decision = instruction.decision
    if opportunity is None:
        raise ValueError("CREATE requires current opportunity")
    if (
        decision.selected_candidate_id is None
        or decision.family is None
        or decision.subtype is None
        or decision.side is None
    ):
        raise ValueError("CREATE requires complete selected Auction identity")
    return AuctionSignalIdentity(
        opportunity_key=opportunity.opportunity_key,
        candidate_id=decision.selected_candidate_id,
        boundary_event_key=opportunity.boundary_event_key,
        setup_family=decision.family,
        setup_subtype=decision.subtype,
        side=decision.side,
        created_snapshot_time=opportunity.selected_time or opportunity.eligible_time or opportunity.last_observed_time,
    )


def _as_update_instruction(
    snapshot: SnapshotSchema,
    instruction: AuctionSignalInstruction,
    existing_signal: SignalSchema,
) -> AuctionSignalInstruction:
    active_identity = _signal_identity(existing_signal)
    if active_identity is None:
        raise ValueError("Idempotent update requires active Auction signal identity")
    current_key = (
        instruction.current_opportunity.opportunity_key
        if instruction.current_opportunity is not None
        else None
    )
    same = current_key == active_identity.opportunity_key
    competing = bool(
        instruction.auction_action == "LOCAL_CONFIRMED"
        and current_key != active_identity.opportunity_key
    )
    lifecycle = _resolve_signal_lifecycle(
        snapshot=snapshot,
        existing_signal=existing_signal,
        auction_action=instruction.auction_action,
        active_identity=active_identity,
        current_opportunity=instruction.current_opportunity,
        same_opportunity=same,
        competing_confirmed_opportunity=competing,
    )
    return AuctionSignalInstruction(
        persistence_action=("CLOSE" if lifecycle.terminal else "UPDATE"),
        auction_action=instruction.auction_action,
        auction_state=instruction.auction_state,
        reason_codes=instruction.reason_codes,
        decision=instruction.decision,
        active_identity=active_identity,
        current_opportunity=instruction.current_opportunity,
        current_candidate=instruction.current_candidate,
        same_opportunity=same,
        competing_confirmed_opportunity=competing,
        lifecycle=lifecycle,
    )


# ---------------------------------------------------------------------------
# Signal payloads
# ---------------------------------------------------------------------------
def _criteria_json(
    snapshot: SnapshotSchema,
    instruction: AuctionSignalInstruction,
) -> Dict[str, Any]:
    opportunity = instruction.current_opportunity
    decision = instruction.decision
    return sanitize_json({
        "source": "AUCTION",
        "lifecycle": DEFAULT_LIFECYCLE,
        "snapshot_time": snapshot.snapshot_time,
        "auction_action": instruction.auction_action,
        "auction_state": instruction.auction_state,
        "signal_action": instruction.lifecycle.signal_action,
        "signal_stage": instruction.lifecycle.stage.value,
        "signal_status": instruction.lifecycle.status.value,
        "signal_reason_code": instruction.lifecycle.reason_code,
        "directional_alignment": instruction.lifecycle.directional_alignment,
        "terminal": instruction.lifecycle.terminal,
        "same_opportunity": instruction.same_opportunity,
        "competing_confirmed_opportunity": instruction.competing_confirmed_opportunity,
        "decision_reason_codes": list(instruction.reason_codes),
        "selected_opportunity_key": decision.selected_opportunity_key,
        "selected_candidate_id": decision.selected_candidate_id,
        "opportunity_key": opportunity.opportunity_key if opportunity is not None else None,
        "opportunity_lifecycle": opportunity.lifecycle if opportunity is not None else None,
        "boundary_event_key": opportunity.boundary_event_key if opportunity is not None else None,
        "candidate_id": _current_candidate_id(instruction),
        "setup_family": _current_family(instruction),
        "setup_subtype": _current_subtype(instruction),
        "side": _current_side(instruction),
        "eligibility": _current_eligibility(instruction),
        "entry_price": decision.entry_price,
        "stop_anchor_price": decision.stop_anchor_price,
        "stop_anchor_type": decision.stop_anchor_type,
        "target_basis": decision.target_basis,
        "target_reference_price": decision.target_reference_price,
        "valid_until": decision.valid_until,
    })


def _create_meta_json(
    snapshot: SnapshotSchema,
    instruction: AuctionSignalInstruction,
    criteria_json: Dict[str, Any],
) -> Dict[str, Any]:
    identity = _require_instruction_identity(instruction)
    latest = _latest_auction_evaluation(snapshot, instruction)
    setup_levels = _entry_setup_levels(snapshot, instruction, identity)
    downstream = _downstream_projection(
        snapshot=snapshot,
        instruction=instruction,
        identity=identity,
        setup_levels=setup_levels,
    )
    meta = {
        "reason": _status_reason(instruction),
        "source": "AUCTION",
        "strategy": "AUCTION",
        "setup_label": identity.setup_family,
        "primary_pattern": identity.setup_subtype,
        "setup_levels": setup_levels,
        "initiated_setup_label": identity.setup_family,
        "initiated_setup": _initiated_setup_payload(
            snapshot=snapshot,
            instruction=instruction,
            identity=identity,
            setup_levels=setup_levels,
        ),
        "auction_signal": {
            "opportunity_key": identity.opportunity_key,
            "candidate_id": identity.candidate_id,
            "boundary_event_key": identity.boundary_event_key,
            "setup_family": identity.setup_family,
            "setup_subtype": identity.setup_subtype,
            "side": identity.side,
            "created_snapshot_time": snapshot.snapshot_time,
        },
        "entry_criteria_json": criteria_json,
        "current_criteria_json": criteria_json,
        "latest_auction_evaluation": latest,
        "signal_lifecycle": _signal_lifecycle_record(snapshot, instruction),
        "signal_lifecycle_history": [
            _signal_lifecycle_record(snapshot, instruction)
        ],
        "auction_posture_history": [
            _posture_history_record(snapshot, instruction)
        ],
        **downstream,
    }
    _validate_downstream_contract(meta, identity)
    return sanitize_json(meta)


def _update_meta_json(
    *,
    existing_signal: SignalSchema,
    snapshot: SnapshotSchema,
    instruction: AuctionSignalInstruction,
    active_identity: AuctionSignalIdentity,
    criteria_json: Dict[str, Any],
) -> Dict[str, Any]:
    meta = existing_signal.meta_json
    if not isinstance(meta, dict):
        raise ValueError("Auction-linked signal meta_json is missing")
    if "entry_criteria_json" not in meta:
        raise ValueError("Auction-linked signal missing immutable entry_criteria_json")
    if "auction_posture_history" not in meta:
        raise ValueError("Auction-linked signal missing auction_posture_history")
    posture_history = meta["auction_posture_history"]
    if not isinstance(posture_history, list):
        raise ValueError("auction_posture_history must be a list")

    next_meta = dict(meta)
    next_meta["reason"] = _status_reason(instruction)
    next_meta["current_criteria_json"] = criteria_json
    next_meta["latest_auction_evaluation"] = _latest_auction_evaluation(snapshot, instruction)

    setup_levels, migrated = _existing_or_migrated_setup_levels(
        meta=meta,
        active_identity=active_identity,
    )
    next_meta["setup_levels"] = setup_levels
    next_meta["strategy"] = "AUCTION"
    next_meta["setup_label"] = active_identity.setup_family
    next_meta["primary_pattern"] = active_identity.setup_subtype
    initiated_setup = _existing_or_migrated_initiated_setup(
        meta=meta,
        snapshot=snapshot,
        instruction=instruction,
        active_identity=active_identity,
        setup_levels=setup_levels,
    )
    next_meta["initiated_setup"] = initiated_setup
    next_meta["initiated_setup_label"] = active_identity.setup_family
    if migrated:
        next_meta["downstream_contract_migration"] = {
            "version": _DOWNSTREAM_CONTRACT_VERSION,
            "snapshot_time": snapshot.snapshot_time,
            "reason": "PATCH5_2_BACKFILLED_FROM_IMMUTABLE_ENTRY_CRITERIA",
        }

    posture_record = _posture_history_record(snapshot, instruction)
    previous_posture_signature = None
    if posture_history:
        previous_posture = posture_history[-1]
        if not isinstance(previous_posture, dict):
            raise ValueError("auction_posture_history entries must be objects")
        previous_posture_signature = _posture_signature(previous_posture)
    current_posture_signature = _posture_signature(posture_record)
    next_posture_history = list(posture_history)
    if previous_posture_signature != current_posture_signature:
        next_posture_history.append(posture_record)
    next_meta["auction_posture_history"] = next_posture_history[-32:]

    lifecycle_record = _signal_lifecycle_record(snapshot, instruction)
    if "signal_lifecycle_history" in meta:
        lifecycle_history = meta["signal_lifecycle_history"]
        if not isinstance(lifecycle_history, list):
            raise ValueError("signal_lifecycle_history must be a list")
        next_lifecycle_history = list(lifecycle_history)
    else:
        # Explicit one-time migration for signals created by Patch 5 before
        # lifecycle columns/history were wired back into SignalGenerator.
        next_lifecycle_history = [
            sanitize_json({
                "snapshot_time": existing_signal.last_snapshot_time,
                "signal_action": "MIGRATED",
                "stage": _enum_text(existing_signal.stage),
                "status": _enum_text(existing_signal.status),
                "reason_code": "PATCH5_PERSISTED_LIFECYCLE_BASELINE",
                "auction_action": _previous_auction_action(existing_signal),
                "auction_state": None,
                "opportunity_lifecycle": None,
                "directional_alignment": "NEUTRAL",
                "terminal": _enum_text(existing_signal.status) != SignalStatus.OPEN.value,
            })
        ]

    previous_lifecycle_signature = None
    if next_lifecycle_history:
        previous_lifecycle = next_lifecycle_history[-1]
        if not isinstance(previous_lifecycle, dict):
            raise ValueError("signal_lifecycle_history entries must be objects")
        previous_lifecycle_signature = _lifecycle_signature(previous_lifecycle)
    current_lifecycle_signature = _lifecycle_signature(lifecycle_record)
    if previous_lifecycle_signature != current_lifecycle_signature:
        next_lifecycle_history.append(lifecycle_record)
    next_meta["signal_lifecycle"] = lifecycle_record
    next_meta["signal_lifecycle_history"] = next_lifecycle_history[-32:]

    # Immutable identity must remain exact throughout updates.
    stored_identity = _signal_identity(existing_signal)
    if stored_identity != active_identity:
        raise ValueError("Active signal Auction identity changed during update")
    downstream = _downstream_projection(
        snapshot=snapshot,
        instruction=instruction,
        identity=active_identity,
        setup_levels=setup_levels,
    )
    next_meta.update(downstream)
    _validate_downstream_contract(next_meta, active_identity)
    return sanitize_json(next_meta)


def _posture_signature(record: Dict[str, Any]) -> Tuple[Any, ...]:
    required = (
        "auction_action",
        "auction_state",
        "current_opportunity_key",
        "current_candidate_id",
        "same_opportunity",
        "competing_confirmed_opportunity",
    )
    missing = [key for key in required if key not in record]
    if missing:
        raise ValueError(f"auction_posture_history entry missing fields: {missing}")
    return tuple(record[key] for key in required)


def _lifecycle_signature(record: Dict[str, Any]) -> Tuple[Any, ...]:
    required = (
        "signal_action",
        "stage",
        "status",
        "reason_code",
        "opportunity_lifecycle",
        "directional_alignment",
        "terminal",
    )
    missing = [key for key in required if key not in record]
    if missing:
        raise ValueError(f"signal_lifecycle_history entry missing fields: {missing}")
    return tuple(record[key] for key in required)


def _entry_setup_levels(
    snapshot: SnapshotSchema,
    instruction: AuctionSignalInstruction,
    identity: AuctionSignalIdentity,
) -> Dict[str, Any]:
    decision = instruction.decision
    if decision.entry_price is None:
        raise ValueError("Auction signal CREATE missing decision.entry_price")
    if decision.stop_anchor_price is None:
        raise ValueError("Auction signal CREATE missing decision.stop_anchor_price")
    if decision.stop_anchor_type is None:
        raise ValueError("Auction signal CREATE missing decision.stop_anchor_type")
    return sanitize_json({
        "contract_version": _DOWNSTREAM_CONTRACT_VERSION,
        "source": "AUCTION",
        "setup_label": identity.setup_family,
        "setup_subtype": identity.setup_subtype,
        "side": identity.side,
        "candidate_id": identity.candidate_id,
        "opportunity_key": identity.opportunity_key,
        "boundary_event_key": identity.boundary_event_key,
        "snapshot_time": snapshot.snapshot_time,
        "entry_price": decision.entry_price,
        "initial_stop_reference_price": decision.stop_anchor_price,
        "reference_price": decision.stop_anchor_price,
        "reference_source": decision.stop_anchor_type,
        "level_price": decision.stop_anchor_price,
        "level_type": decision.stop_anchor_type,
        "target_basis": decision.target_basis,
        "target_reference_price": decision.target_reference_price,
    })


def _existing_or_migrated_setup_levels(
    *,
    meta: Dict[str, Any],
    active_identity: AuctionSignalIdentity,
) -> Tuple[Dict[str, Any], bool]:
    if "setup_levels" in meta:
        setup_levels = meta["setup_levels"]
        if not isinstance(setup_levels, dict):
            raise ValueError("Auction-linked signal setup_levels must be an object")
        _validate_setup_levels_identity(setup_levels, active_identity)
        return dict(setup_levels), False

    if "entry_criteria_json" not in meta:
        raise ValueError(
            "Auction-linked signal missing setup_levels and immutable entry_criteria_json"
        )
    entry = meta["entry_criteria_json"]
    if not isinstance(entry, dict):
        raise ValueError("entry_criteria_json must be an object for downstream migration")
    required = (
        "entry_price",
        "stop_anchor_price",
        "stop_anchor_type",
        "target_basis",
        "target_reference_price",
    )
    missing = [key for key in required if key not in entry]
    if missing:
        raise ValueError(
            f"entry_criteria_json missing downstream setup-level fields: {missing}"
        )
    if entry["entry_price"] is None:
        raise ValueError("entry_criteria_json.entry_price is required for migration")
    if entry["stop_anchor_price"] is None:
        raise ValueError("entry_criteria_json.stop_anchor_price is required for migration")
    if entry["stop_anchor_type"] is None:
        raise ValueError("entry_criteria_json.stop_anchor_type is required for migration")

    setup_levels = sanitize_json({
        "contract_version": _DOWNSTREAM_CONTRACT_VERSION,
        "source": "AUCTION",
        "setup_label": active_identity.setup_family,
        "setup_subtype": active_identity.setup_subtype,
        "side": active_identity.side,
        "candidate_id": active_identity.candidate_id,
        "opportunity_key": active_identity.opportunity_key,
        "boundary_event_key": active_identity.boundary_event_key,
        "snapshot_time": active_identity.created_snapshot_time,
        "entry_price": entry["entry_price"],
        "initial_stop_reference_price": entry["stop_anchor_price"],
        "reference_price": entry["stop_anchor_price"],
        "reference_source": entry["stop_anchor_type"],
        "level_price": entry["stop_anchor_price"],
        "level_type": entry["stop_anchor_type"],
        "target_basis": entry["target_basis"],
        "target_reference_price": entry["target_reference_price"],
    })
    _validate_setup_levels_identity(setup_levels, active_identity)
    return setup_levels, True


def _validate_setup_levels_identity(
    setup_levels: Dict[str, Any],
    identity: AuctionSignalIdentity,
) -> None:
    required = (
        "contract_version",
        "source",
        "setup_label",
        "setup_subtype",
        "side",
        "candidate_id",
        "opportunity_key",
        "boundary_event_key",
        "entry_price",
        "initial_stop_reference_price",
        "reference_price",
        "reference_source",
    )
    missing = [key for key in required if key not in setup_levels]
    if missing:
        raise ValueError(f"setup_levels missing required fields: {missing}")
    expected = {
        "contract_version": _DOWNSTREAM_CONTRACT_VERSION,
        "source": "AUCTION",
        "setup_label": identity.setup_family,
        "setup_subtype": identity.setup_subtype,
        "side": identity.side,
        "candidate_id": identity.candidate_id,
        "opportunity_key": identity.opportunity_key,
        "boundary_event_key": identity.boundary_event_key,
    }
    mismatches = [
        key
        for key in expected
        if str(setup_levels[key]).strip() != str(expected[key]).strip()
    ]
    if mismatches:
        raise ValueError(f"setup_levels Auction identity mismatch: {mismatches}")
    _positive_decimal_required(
        setup_levels["entry_price"],
        "signal.meta_json.setup_levels.entry_price",
    )
    _positive_decimal_required(
        setup_levels["initial_stop_reference_price"],
        "signal.meta_json.setup_levels.initial_stop_reference_price",
    )
    _positive_decimal_required(
        setup_levels["reference_price"],
        "signal.meta_json.setup_levels.reference_price",
    )


def _initiated_setup_payload(
    *,
    snapshot: SnapshotSchema,
    instruction: AuctionSignalInstruction,
    identity: AuctionSignalIdentity,
    setup_levels: Dict[str, Any],
) -> Dict[str, Any]:
    return sanitize_json({
        "setup_label": identity.setup_family,
        "setup_subtype": identity.setup_subtype,
        "strategy": "AUCTION",
        "side": identity.side,
        "candidate_id": identity.candidate_id,
        "opportunity_key": identity.opportunity_key,
        "boundary_event_key": identity.boundary_event_key,
        "snapshot_time": snapshot.snapshot_time,
        "entry_price": setup_levels["entry_price"],
        "entry_reason": {
            "action": instruction.lifecycle.signal_action,
            "reason_code": instruction.lifecycle.reason_code,
            "auction_action": instruction.auction_action,
            "auction_state": instruction.auction_state,
        },
        "setup_levels": setup_levels,
    })


def _existing_or_migrated_initiated_setup(
    *,
    meta: Dict[str, Any],
    snapshot: SnapshotSchema,
    instruction: AuctionSignalInstruction,
    active_identity: AuctionSignalIdentity,
    setup_levels: Dict[str, Any],
) -> Dict[str, Any]:
    if "initiated_setup" in meta:
        initiated = meta["initiated_setup"]
        if not isinstance(initiated, dict):
            raise ValueError("initiated_setup must be an object")
        next_initiated = dict(initiated)
        next_initiated["setup_label"] = active_identity.setup_family
        next_initiated["setup_subtype"] = active_identity.setup_subtype
        next_initiated["strategy"] = "AUCTION"
        next_initiated["side"] = active_identity.side
        next_initiated["candidate_id"] = active_identity.candidate_id
        next_initiated["opportunity_key"] = active_identity.opportunity_key
        next_initiated["boundary_event_key"] = active_identity.boundary_event_key
        next_initiated["setup_levels"] = setup_levels
        if "entry_price" not in next_initiated:
            next_initiated["entry_price"] = setup_levels["entry_price"]
        if "entry_reason" not in next_initiated:
            next_initiated["entry_reason"] = {
                "action": "CREATE",
                "reason_code": "PATCH5_2_MIGRATED_AUCTION_ENTRY_REASON",
                "auction_action": "LOCAL_CONFIRMED",
                "auction_state": None,
            }
        return sanitize_json(next_initiated)
    return _initiated_setup_payload(
        snapshot=snapshot,
        instruction=instruction,
        identity=active_identity,
        setup_levels=setup_levels,
    )


def _downstream_projection(
    *,
    snapshot: SnapshotSchema,
    instruction: AuctionSignalInstruction,
    identity: AuctionSignalIdentity,
    setup_levels: Dict[str, Any],
) -> Dict[str, Any]:
    signal_state = _downstream_signal_state(instruction)
    evidence = _active_signal_evidence_projection(
        snapshot=snapshot,
        instruction=instruction,
        identity=identity,
    )
    primary_candidate = _downstream_candidate_projection(instruction)
    supports, warnings, conflicts = _downstream_reason_buckets(instruction)
    quality: Optional[str] = None
    confidence: Optional[float] = None

    signal_block = sanitize_json({
        "contract_version": _DOWNSTREAM_CONTRACT_VERSION,
        "source": "AUCTION",
        "stage": instruction.lifecycle.stage.value,
        "side": identity.side,
        "confidence": confidence,
        "quality": quality,
        "confidence_source": "NOT_EMITTED_BY_AUCTION",
        "quality_source": "NOT_EMITTED_BY_AUCTION",
        "signal_action": instruction.lifecycle.signal_action,
        "signal_state": signal_state,
        "setup_state": instruction.lifecycle.opportunity_lifecycle,
        "signal_reason": instruction.lifecycle.reason_code,
        "strategy": "AUCTION",
        "setup_label": identity.setup_family,
        "primary_pattern": identity.setup_subtype,
        "evaluator_state": instruction.auction_state,
        "decision": instruction.auction_action,
        "price_action_confirmed": bool(
            instruction.auction_action == "LOCAL_CONFIRMED"
            and (
                instruction.lifecycle.signal_action == "CREATE"
                or instruction.same_opportunity
            )
        ),
        "price_action_strength": None,
        "blocked_by": (
            list(instruction.reason_codes)
            if instruction.auction_action == "LOCAL_BLOCKED"
            else []
        ),
        "risk_flags": [],
        "setup_levels": setup_levels,
    })
    lifecycle_block = sanitize_json({
        "contract_version": _DOWNSTREAM_CONTRACT_VERSION,
        "source": "AUCTION",
        "stage": instruction.lifecycle.stage.value,
        "side": identity.side,
        "confidence": confidence,
        "quality": quality,
        "signal_action": instruction.lifecycle.signal_action,
        "signal_state": signal_state,
        "signal_reason": instruction.lifecycle.reason_code,
        "trade_action": _downstream_trade_action(instruction),
        "blocked_by_policy": instruction.auction_action == "LOCAL_BLOCKED",
        "blocked_by_policy_reason": (
            instruction.lifecycle.reason_code
            if instruction.auction_action == "LOCAL_BLOCKED"
            else None
        ),
        "supports": supports,
        "warnings": warnings,
        "conflicts": conflicts,
        "blocks": (
            list(instruction.reason_codes)
            if instruction.auction_action == "LOCAL_BLOCKED"
            else []
        ),
        "negative_cluster": conflicts,
        "confidence_factors": [],
        "summary": _status_reason(instruction),
        "reason": instruction.lifecycle.reason_code,
        "reasons": list(instruction.reason_codes),
    })
    setup_decision = sanitize_json({
        "phase": (
            "SIGNAL_CREATE"
            if instruction.lifecycle.signal_action == "CREATE"
            else "ACTIVE_SIGNAL_MANAGEMENT"
        ),
        "has_active_signal": True,
        "active_side": identity.side,
        "reference_side": evidence["reference_side"],
        "decision": instruction.auction_action,
        "evaluator_state": instruction.auction_state,
        "preferred_side": evidence["reference_side"],
        "primary_candidate": primary_candidate,
        "entry_ready_candidates": (
            [primary_candidate]
            if primary_candidate is not None
            and instruction.auction_action == "LOCAL_CONFIRMED"
            else []
        ),
        "active_signal_evidence": evidence,
    })
    current_evidence = sanitize_json({
        "contract_version": _DOWNSTREAM_CONTRACT_VERSION,
        "source": "AUCTION",
        "snapshot_time": snapshot.snapshot_time,
        "stage": instruction.lifecycle.stage.value,
        "side": identity.side,
        "signal_action": instruction.lifecycle.signal_action,
        "signal_state": signal_state,
        "signal_reason": instruction.lifecycle.reason_code,
        "reason": {
            "code": instruction.lifecycle.reason_code,
            "text": _status_reason(instruction),
        },
        "strategy": "AUCTION",
        "setup_label": identity.setup_family,
        "primary_pattern": identity.setup_subtype,
        "entry_permission": _downstream_entry_permission(instruction),
        "evaluator_state": instruction.auction_state,
        "decision": instruction.auction_action,
        "preferred_side": evidence["reference_side"],
        "blocked_by": signal_block["blocked_by"],
        "risk_flags": [],
        "setup_decision": setup_decision,
        "active_signal_evidence": evidence,
        "primary_candidate": primary_candidate,
        "entry_ready_candidates": setup_decision["entry_ready_candidates"],
    })
    return {
        "downstream_contract": sanitize_json({
            "version": _DOWNSTREAM_CONTRACT_VERSION,
            "source": "AUCTION_SIGNAL_GENERATOR",
            "snapshot_time": snapshot.snapshot_time,
            "setup_levels_immutable": True,
            "confidence_provided": False,
            "quality_provided": False,
        }),
        "signal": signal_block,
        "lifecycle": lifecycle_block,
        "active_signal_evidence": evidence,
        "setup_decision": setup_decision,
        "current_evidence": current_evidence,
        "evidence_reason": {
            "code": instruction.lifecycle.reason_code,
            "text": _status_reason(instruction),
        },
        "supports": supports,
        "warnings": warnings,
        "conflicts": conflicts,
    }


def _downstream_signal_state(instruction: AuctionSignalInstruction) -> str:
    if instruction.lifecycle.status != SignalStatus.OPEN:
        return instruction.lifecycle.status.value
    if instruction.lifecycle.signal_action == "CREATE":
        return "READY"
    stage = instruction.lifecycle.stage
    if stage in {LifecycleStage.ACTIVE, LifecycleStage.EXPAND}:
        return "ACCEPTED"
    if stage in {LifecycleStage.DISCOVERY, LifecycleStage.BUILDING}:
        return "TRACKING"
    return "MANAGE"


def _downstream_trade_action(instruction: AuctionSignalInstruction) -> str:
    stage = instruction.lifecycle.stage
    if instruction.lifecycle.terminal or stage == LifecycleStage.FORCE_EXIT:
        return "FORCE_EXIT"
    if stage == LifecycleStage.EXIT_BIAS:
        return "EXIT_POSITION"
    if stage in {
        LifecycleStage.WEAKENING,
        LifecycleStage.TRANSITION,
        LifecycleStage.PROTECT,
    }:
        return "TIGHTEN_STOP"
    if instruction.lifecycle.signal_action == "CREATE":
        return "CREATE_TRADE"
    return "HOLD_POSITION"


def _downstream_entry_permission(instruction: AuctionSignalInstruction) -> str:
    if instruction.lifecycle.status != SignalStatus.OPEN:
        return "BLOCK"
    if instruction.lifecycle.signal_action == "CREATE":
        return "ALLOW"
    if instruction.lifecycle.stage in {LifecycleStage.ACTIVE, LifecycleStage.EXPAND}:
        return "ALLOW"
    if instruction.lifecycle.stage in {
        LifecycleStage.PROTECT,
        LifecycleStage.TRANSITION,
        LifecycleStage.WEAKENING,
    }:
        return "CAUTION"
    return "BLOCK"


def _active_signal_evidence_projection(
    *,
    snapshot: SnapshotSchema,
    instruction: AuctionSignalInstruction,
    identity: AuctionSignalIdentity,
) -> Dict[str, Any]:
    stage = instruction.lifecycle.stage
    if instruction.lifecycle.terminal or stage in {
        LifecycleStage.EXIT_BIAS,
        LifecycleStage.FORCE_EXIT,
    }:
        evidence_action = "EXIT"
        exit_pressure = "HIGH"
        trail_mode = "EXIT_READY"
        target_expansion_allowed = False
        should_exit_signal = True
    elif stage == LifecycleStage.WEAKENING:
        evidence_action = "CAUTION"
        exit_pressure = "MEDIUM"
        trail_mode = "DEFENSIVE"
        target_expansion_allowed = False
        should_exit_signal = False
    elif stage in {LifecycleStage.TRANSITION, LifecycleStage.PROTECT}:
        evidence_action = "CAUTION"
        exit_pressure = "MEDIUM" if stage == LifecycleStage.TRANSITION else "LOW"
        trail_mode = "DEFENSIVE"
        target_expansion_allowed = False
        should_exit_signal = False
    elif stage in {LifecycleStage.ACTIVE, LifecycleStage.EXPAND}:
        evidence_action = (
            "STRENGTHEN"
            if instruction.lifecycle.directional_alignment == "ALIGNED"
            else "HOLD"
        )
        exit_pressure = "LOW"
        trail_mode = "NORMAL"
        target_expansion_allowed = bool(
            instruction.lifecycle.directional_alignment == "ALIGNED"
        )
        should_exit_signal = False
    elif stage in {LifecycleStage.DISCOVERY, LifecycleStage.BUILDING}:
        evidence_action = "HOLD"
        exit_pressure = "LOW"
        trail_mode = "NORMAL"
        target_expansion_allowed = False
        should_exit_signal = False
    else:
        raise ValueError(
            f"Unsupported stage for downstream active evidence: {stage.value}"
        )

    current_side = _current_side(instruction)
    reference_side = current_side if current_side is not None else identity.side
    primary_candidate = _downstream_candidate_projection(instruction)
    top_same = (
        primary_candidate
        if primary_candidate is not None and reference_side == identity.side
        else None
    )
    top_opposite = (
        primary_candidate
        if primary_candidate is not None and reference_side != identity.side
        else None
    )
    return sanitize_json({
        "contract_version": _DOWNSTREAM_CONTRACT_VERSION,
        "mode": "AUCTION_LIFECYCLE_ADAPTER_NO_AUTO_REVERSAL",
        "has_active_signal": True,
        "active_side": identity.side,
        "active_setup_label": identity.setup_family,
        "active_setup_subtype": identity.setup_subtype,
        "active_opportunity_key": identity.opportunity_key,
        "active_candidate_id": identity.candidate_id,
        "active_boundary_event_key": identity.boundary_event_key,
        "stage": stage.value,
        "signal_status": instruction.lifecycle.status.value,
        "reference_side": reference_side,
        "evidence_action": evidence_action,
        "active_evidence_action": evidence_action,
        "reason_code": instruction.lifecycle.reason_code,
        "reason_text": _status_reason(instruction),
        "exit_pressure": exit_pressure,
        "trail_mode": trail_mode,
        "target_expansion_allowed": target_expansion_allowed,
        "should_exit_signal": should_exit_signal,
        "same_pass_reversal_allowed": False,
        "next_pass_create_policy": "ALLOW_ONLY_IF_NO_ACTIVE_SIGNAL_AT_PASS_START",
        "support_score": None,
        "opposition_score": None,
        "same_side_candidate_count": 1 if top_same is not None else 0,
        "same_side_confirmed_count": (
            1
            if top_same is not None and instruction.auction_action == "LOCAL_CONFIRMED"
            else 0
        ),
        "same_side_entry_ready_count": (
            1
            if top_same is not None and instruction.auction_action == "LOCAL_CONFIRMED"
            else 0
        ),
        "opposite_candidate_count": 1 if top_opposite is not None else 0,
        "opposite_confirmed_count": (
            1
            if top_opposite is not None and instruction.auction_action == "LOCAL_CONFIRMED"
            else 0
        ),
        "opposite_entry_ready_count": (
            1
            if top_opposite is not None and instruction.auction_action == "LOCAL_CONFIRMED"
            else 0
        ),
        "top_same_side_candidate": top_same,
        "top_opposite_candidate": top_opposite,
        "primary_candidate": primary_candidate,
        "auction_action": instruction.auction_action,
        "auction_state": instruction.auction_state,
        "opportunity_lifecycle": instruction.lifecycle.opportunity_lifecycle,
        "directional_alignment": instruction.lifecycle.directional_alignment,
        "snapshot_time": snapshot.snapshot_time,
    })


def _downstream_candidate_projection(
    instruction: AuctionSignalInstruction,
) -> Optional[Dict[str, Any]]:
    candidate = instruction.current_candidate
    opportunity = instruction.current_opportunity
    if candidate is None and opportunity is None:
        return None
    return sanitize_json({
        "candidate_id": _current_candidate_id(instruction),
        "opportunity_key": (
            opportunity.opportunity_key if opportunity is not None else None
        ),
        "setup_label": _current_family(instruction),
        "setup_subtype": _current_subtype(instruction),
        "side": _current_side(instruction),
        "eligibility": _current_eligibility(instruction),
        "event_key": (
            opportunity.boundary_event_key if opportunity is not None else None
        ),
        "entry_price": instruction.decision.entry_price,
        "stop_anchor_price": instruction.decision.stop_anchor_price,
        "stop_anchor_type": instruction.decision.stop_anchor_type,
        "target_basis": instruction.decision.target_basis,
        "target_reference_price": instruction.decision.target_reference_price,
    })


def _downstream_reason_buckets(
    instruction: AuctionSignalInstruction,
) -> Tuple[List[str], List[str], List[str]]:
    reasons = list(instruction.reason_codes)
    if instruction.lifecycle.terminal or instruction.lifecycle.stage in {
        LifecycleStage.EXIT_BIAS,
        LifecycleStage.FORCE_EXIT,
    }:
        return [], [], reasons
    if instruction.lifecycle.stage in {
        LifecycleStage.PROTECT,
        LifecycleStage.TRANSITION,
        LifecycleStage.WEAKENING,
    }:
        return [], reasons, []
    return reasons, [], []


def _validate_downstream_contract(
    meta: Dict[str, Any],
    identity: AuctionSignalIdentity,
) -> None:
    required_blocks = (
        "downstream_contract",
        "signal",
        "lifecycle",
        "active_signal_evidence",
        "setup_decision",
        "current_evidence",
        "setup_levels",
        "initiated_setup",
    )
    missing = [key for key in required_blocks if key not in meta]
    if missing:
        raise ValueError(f"Auction signal downstream contract missing blocks: {missing}")
    for key in required_blocks:
        if not isinstance(meta[key], dict):
            raise ValueError(f"Auction signal downstream block {key} must be an object")

    setup_levels = meta["setup_levels"]
    _validate_setup_levels_identity(setup_levels, identity)
    initiated = meta["initiated_setup"]
    initiated_expected = {
        "setup_label": identity.setup_family,
        "setup_subtype": identity.setup_subtype,
        "side": identity.side,
        "candidate_id": identity.candidate_id,
        "opportunity_key": identity.opportunity_key,
        "boundary_event_key": identity.boundary_event_key,
    }
    initiated_missing = [key for key in initiated_expected if key not in initiated]
    if initiated_missing:
        raise ValueError(
            f"initiated_setup missing Auction identity fields: {initiated_missing}"
        )
    initiated_mismatch = [
        key for key in initiated_expected
        if initiated[key] != initiated_expected[key]
    ]
    if initiated_mismatch:
        raise ValueError(
            f"initiated_setup Auction identity mismatch: {initiated_mismatch}"
        )
    if "setup_levels" not in initiated or initiated["setup_levels"] != setup_levels:
        raise ValueError("initiated_setup.setup_levels must match immutable setup_levels")

    signal_block = meta["signal"]
    lifecycle_block = meta["lifecycle"]
    active_evidence = meta["active_signal_evidence"]
    setup_decision = meta["setup_decision"]
    current_evidence = meta["current_evidence"]
    for key in (
        "contract_version",
        "stage",
        "side",
        "signal_action",
        "signal_state",
        "signal_reason",
        "setup_label",
        "setup_levels",
    ):
        if key not in signal_block:
            raise ValueError(f"signal downstream block missing {key}")
    for key in (
        "contract_version",
        "stage",
        "side",
        "signal_action",
        "signal_state",
        "signal_reason",
        "trade_action",
    ):
        if key not in lifecycle_block:
            raise ValueError(f"lifecycle downstream block missing {key}")
    for key in (
        "contract_version",
        "active_evidence_action",
        "reason_code",
        "exit_pressure",
        "trail_mode",
        "target_expansion_allowed",
        "should_exit_signal",
    ):
        if key not in active_evidence:
            raise ValueError(f"active_signal_evidence missing {key}")
    if signal_block["contract_version"] != _DOWNSTREAM_CONTRACT_VERSION:
        raise ValueError("signal downstream contract version mismatch")
    if lifecycle_block["contract_version"] != _DOWNSTREAM_CONTRACT_VERSION:
        raise ValueError("lifecycle downstream contract version mismatch")
    if active_evidence["contract_version"] != _DOWNSTREAM_CONTRACT_VERSION:
        raise ValueError("active_signal_evidence contract version mismatch")
    if signal_block["side"] != identity.side:
        raise ValueError("signal downstream side does not match Auction identity")
    if signal_block["setup_label"] != identity.setup_family:
        raise ValueError("signal downstream setup does not match Auction identity")
    if signal_block["setup_levels"] != setup_levels:
        raise ValueError("signal.setup_levels must match immutable setup_levels")
    if lifecycle_block["side"] != identity.side:
        raise ValueError("lifecycle downstream side does not match Auction identity")
    for key in ("stage", "signal_action", "signal_state", "signal_reason"):
        if signal_block[key] != lifecycle_block[key]:
            raise ValueError(f"signal/lifecycle downstream mismatch for {key}")
    if active_evidence["active_side"] != identity.side:
        raise ValueError("active_signal_evidence side does not match Auction identity")
    if active_evidence["active_setup_label"] != identity.setup_family:
        raise ValueError("active_signal_evidence setup does not match Auction identity")
    if active_evidence["active_opportunity_key"] != identity.opportunity_key:
        raise ValueError("active_signal_evidence opportunity does not match Auction identity")
    if active_evidence["active_candidate_id"] != identity.candidate_id:
        raise ValueError("active_signal_evidence candidate does not match Auction identity")
    if active_evidence["active_boundary_event_key"] != identity.boundary_event_key:
        raise ValueError("active_signal_evidence boundary does not match Auction identity")
    if active_evidence["stage"] != signal_block["stage"]:
        raise ValueError("active_signal_evidence stage does not match signal block")
    if active_evidence["reason_code"] != signal_block["signal_reason"]:
        raise ValueError("active_signal_evidence reason does not match signal block")
    if setup_decision["active_signal_evidence"] != active_evidence:
        raise ValueError("setup_decision active evidence must match canonical block")
    if current_evidence["active_signal_evidence"] != active_evidence:
        raise ValueError("current_evidence active evidence must match canonical block")
    if current_evidence["setup_decision"] != setup_decision:
        raise ValueError("current_evidence setup decision must match canonical block")


def _latest_auction_evaluation(
    snapshot: SnapshotSchema,
    instruction: AuctionSignalInstruction,
) -> Dict[str, Any]:
    opportunity = instruction.current_opportunity
    return sanitize_json({
        "snapshot_time": snapshot.snapshot_time,
        "auction_action": instruction.auction_action,
        "auction_state": instruction.auction_state,
        "signal_action": instruction.lifecycle.signal_action,
        "signal_stage": instruction.lifecycle.stage.value,
        "signal_status": instruction.lifecycle.status.value,
        "signal_reason_code": instruction.lifecycle.reason_code,
        "opportunity_lifecycle": instruction.lifecycle.opportunity_lifecycle,
        "directional_alignment": instruction.lifecycle.directional_alignment,
        "terminal": instruction.lifecycle.terminal,
        "reason_codes": list(instruction.reason_codes),
        "same_opportunity": instruction.same_opportunity,
        "competing_confirmed_opportunity": instruction.competing_confirmed_opportunity,
        "current_opportunity_key": opportunity.opportunity_key if opportunity is not None else None,
        "current_opportunity_lifecycle": opportunity.lifecycle if opportunity is not None else None,
        "current_candidate_id": _current_candidate_id(instruction),
        "current_setup_family": _current_family(instruction),
        "current_setup_subtype": _current_subtype(instruction),
        "current_side": _current_side(instruction),
        "entry_price": instruction.decision.entry_price,
        "stop_anchor_price": instruction.decision.stop_anchor_price,
        "stop_anchor_type": instruction.decision.stop_anchor_type,
        "target_basis": instruction.decision.target_basis,
        "target_reference_price": instruction.decision.target_reference_price,
    })


def _signal_lifecycle_record(
    snapshot: SnapshotSchema,
    instruction: AuctionSignalInstruction,
) -> Dict[str, Any]:
    return sanitize_json({
        "snapshot_time": snapshot.snapshot_time,
        "signal_action": instruction.lifecycle.signal_action,
        "stage": instruction.lifecycle.stage.value,
        "status": instruction.lifecycle.status.value,
        "reason_code": instruction.lifecycle.reason_code,
        "auction_action": instruction.auction_action,
        "auction_state": instruction.auction_state,
        "opportunity_lifecycle": instruction.lifecycle.opportunity_lifecycle,
        "directional_alignment": instruction.lifecycle.directional_alignment,
        "terminal": instruction.lifecycle.terminal,
    })


def _posture_history_record(
    snapshot: SnapshotSchema,
    instruction: AuctionSignalInstruction,
) -> Dict[str, Any]:
    opportunity = instruction.current_opportunity
    return sanitize_json({
        "snapshot_time": snapshot.snapshot_time,
        "auction_action": instruction.auction_action,
        "auction_state": instruction.auction_state,
        "signal_action": instruction.lifecycle.signal_action,
        "signal_stage": instruction.lifecycle.stage.value,
        "signal_status": instruction.lifecycle.status.value,
        "signal_reason_code": instruction.lifecycle.reason_code,
        "opportunity_lifecycle": instruction.lifecycle.opportunity_lifecycle,
        "directional_alignment": instruction.lifecycle.directional_alignment,
        "terminal": instruction.lifecycle.terminal,
        "current_opportunity_key": opportunity.opportunity_key if opportunity is not None else None,
        "current_candidate_id": _current_candidate_id(instruction),
        "same_opportunity": instruction.same_opportunity,
        "competing_confirmed_opportunity": instruction.competing_confirmed_opportunity,
        "reason_codes": list(instruction.reason_codes),
    })


def _current_candidate_id(instruction: AuctionSignalInstruction) -> Optional[str]:
    if instruction.current_candidate is not None:
        return instruction.current_candidate.candidate_id
    if instruction.current_opportunity is not None:
        if instruction.auction_action == "LOCAL_CONFIRMED":
            return instruction.decision.selected_candidate_id
        return instruction.current_opportunity.primary_candidate_id
    return None


def _current_family(instruction: AuctionSignalInstruction) -> Optional[str]:
    if instruction.current_candidate is not None:
        return instruction.current_candidate.family
    if instruction.current_opportunity is not None:
        if instruction.auction_action == "LOCAL_CONFIRMED":
            return instruction.decision.family
        return instruction.current_opportunity.primary_family
    return None


def _current_subtype(instruction: AuctionSignalInstruction) -> Optional[str]:
    if instruction.current_candidate is not None:
        return instruction.current_candidate.subtype
    if instruction.current_opportunity is not None:
        if instruction.auction_action == "LOCAL_CONFIRMED":
            return instruction.decision.subtype
        return instruction.current_opportunity.primary_subtype
    return None


def _current_side(instruction: AuctionSignalInstruction) -> Optional[str]:
    if instruction.current_candidate is not None:
        return instruction.current_candidate.side
    if instruction.current_opportunity is not None:
        if instruction.auction_action == "LOCAL_CONFIRMED":
            return instruction.decision.side
        return instruction.current_opportunity.side
    return None


def _current_eligibility(instruction: AuctionSignalInstruction) -> Optional[str]:
    if instruction.current_candidate is not None:
        return instruction.current_candidate.eligibility
    if instruction.current_opportunity is not None:
        return instruction.current_opportunity.primary_eligibility
    return None


# ---------------------------------------------------------------------------
# Small strict helpers
# ---------------------------------------------------------------------------
def _snapshot_json(snapshot: SnapshotSchema) -> Dict[str, Any]:
    return snapshot.to_db_dict()


def _deterministic_signal_id(lifecycle: str, opportunity_key: str) -> str:
    source = f"AUTOTRADES:AUCTION_SIGNAL:{lifecycle}:{opportunity_key}"
    return str(uuid.uuid5(_SIGNAL_ID_NAMESPACE, source))


def _decimal_required(value: Any, source: str) -> Decimal:
    if value is None:
        raise ValueError(f"Missing required decimal from {source}")
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid decimal from {source}: {value!r}") from exc
    if not out.is_finite():
        raise ValueError(f"Non-finite decimal from {source}: {value!r}")
    return out


def _positive_decimal_required(value: Any, source: str) -> Decimal:
    out = _decimal_required(value, source)
    if out <= 0:
        raise ValueError(f"Expected positive decimal from {source}: {value!r}")
    return out


def _decimal_optional(value: Any, source: str) -> Optional[Decimal]:
    if value is None:
        return None
    return _decimal_required(value, source)


def _required_text(value: Any, source: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"Missing required text from {source}")
    return text


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value)).strip().upper()


def _status_reason(instruction: AuctionSignalInstruction) -> str:
    return (
        f"{instruction.lifecycle.signal_action}:"
        f"{instruction.lifecycle.stage.value}:"
        f"{instruction.lifecycle.reason_code}"
    )[:255]


def _previous_auction_action(signal: Optional[SignalSchema]) -> Optional[str]:
    if signal is None:
        return None
    meta = signal.meta_json
    if not isinstance(meta, dict) or "latest_auction_evaluation" not in meta:
        return None
    latest = meta["latest_auction_evaluation"]
    if not isinstance(latest, dict) or "auction_action" not in latest:
        return None
    return str(latest["auction_action"])


__all__ = [
    "AuctionLifecycleDecision",
    "AuctionSignalIdentity",
    "AuctionSignalInstruction",
    "SignalAssembler",
    "SignalFetcher",
    "SignalGenerator",
    "SignalPersister",
]
