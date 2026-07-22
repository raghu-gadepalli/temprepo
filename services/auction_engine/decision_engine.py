"""Pure local-decision layer for the Auction Engine.

Setup Manager owns stock-local opportunity selection. This layer validates the
selected candidate and converts the manager result into a signal-agnostic local
assessment. It does not apply Advisor policy, inspect active signals/trades, or
build a signal payload.
"""
from __future__ import annotations

from typing import Optional, Tuple

from configs.auction_engine_config import AuctionEngineConfig
from services.auction_engine.contracts import (
    LocalDecision,
    LocalDecisionAction,
    ManagerAction,
    ManagerDecision,
    SetupCandidate,
)


class DecisionEngine:
    """Translate Setup Manager output into a pure local Auction decision."""

    def __init__(self, config: AuctionEngineConfig) -> None:
        self.config = config

    def evaluate(
        self,
        *,
        manager: ManagerDecision,
        selected: Optional[SetupCandidate],
    ) -> LocalDecision:
        ts = manager.snapshot_time
        diagnostics = {
            "decision_scope": "LOCAL_AUCTION_ONLY",
            "signal_lifecycle_applied": False,
            "active_signal_context_applied": False,
            "advisor_context_applied": False,
            "signal_payload_created": False,
            "manager_intent": manager.action.value,
        }

        if manager.action is ManagerAction.NO_ACTION:
            if int(manager.diagnostics.get("active_watch_count") or 0) > 0:
                action = LocalDecisionAction.WATCH
                reasons: Tuple[str, ...] = (
                    "LOCAL_WATCH_NO_ELIGIBLE_OPPORTUNITY_YET",
                )
            else:
                action = LocalDecisionAction.NO_OPPORTUNITY
                reasons = ("NO_LOCAL_ELIGIBLE_OR_WATCH_OPPORTUNITY",)
            selected_for_record = None
        elif manager.action is ManagerAction.DEFER:
            action = LocalDecisionAction.DEFER
            reasons = tuple(manager.reason_codes) or ("LOCAL_DEFER_MANAGER",)
            selected_for_record = None
        elif manager.action is ManagerAction.BLOCK:
            action = LocalDecisionAction.BLOCKED
            reasons = tuple(manager.reason_codes) or ("LOCAL_BLOCK_MANAGER",)
            selected_for_record = None
        elif selected is None:
            action = LocalDecisionAction.BLOCKED
            reasons = ("MANAGER_SELECTION_MISSING_CANDIDATE",)
            selected_for_record = None
        else:
            selected_for_record = selected
            stop_geometry_valid = self._valid_stop_geometry(selected)
            diagnostics["structural_stop_geometry_valid"] = stop_geometry_valid
            if not stop_geometry_valid:
                action = LocalDecisionAction.BLOCKED
                reasons = ("INVALID_STRUCTURAL_STOP_GEOMETRY",)
            else:
                action = LocalDecisionAction.CONFIRMED
                reasons = (
                    "LOCAL_OPPORTUNITY_CONFIRMED",
                    "SETUP_MANAGER_SELECTED_LOCAL_OPPORTUNITY",
                )

        diagnostics["local_action"] = action.value
        return LocalDecision(
            symbol=manager.symbol,
            trading_day=ts.date(),
            snapshot_time=ts,
            action=action,
            selected_candidate=selected_for_record,
            manager_decision=manager,
            reason_codes=tuple(reasons),
            valid_until=(
                selected_for_record.valid_until
                if selected_for_record is not None
                else None
            ),
            diagnostics=diagnostics,
            config_version=self.config.engine.config_version,
        )

    @staticmethod
    def _valid_stop_geometry(candidate: SetupCandidate) -> bool:
        stop = candidate.stop_anchor_price
        if stop is None:
            return False
        if candidate.side.value == "BUY":
            return float(stop) < float(candidate.entry_price)
        if candidate.side.value == "SELL":
            return float(stop) > float(candidate.entry_price)
        return False


__all__ = ["DecisionEngine"]
