"""Pure cross-opportunity arbitration for the Auction Engine.

The manager consumes the factual stock-day opportunity ledger. It never
recomputes setup eligibility, reads signal/trade state, or applies external signal/trade context. Candidate aliases are already collapsed by ``opportunity_key``; the
manager selects an actually ELIGIBLE alias, considers fresh WATCH opposition
explicitly, and derives rotation from historical local selections.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Tuple

from configs.auction_engine_config import AuctionEngineConfig
from services.auction_engine.contracts import (
    ManagerAction,
    ManagerDecision,
    SetupCandidate,
    TradeSide,
)
from services.auction_engine.opportunity_ledger import OpportunityRecord


_STRUCTURAL_WATCH_BLOCKER_TOKENS = (
    "TREND_SIDE_CONFLICT",
    "ACTIVE_TREND_FAILURE",
    "CHAOTIC_ROTATION",
    "MATURE_EXTENSION",
    "REVERSAL_TRANSITION",
    "RECENT_VALID_BALANCE_NOT_CONFIRMED",
    "STRONG_DISPLACEMENT_NOT_CONFIRMED",
    "ENTRY_TOO_FAR",
    "ROOM_",
    "COUNTERTREND_POLICY_DEFERRED",
    "INSUFFICIENT_SESSION_TIME",
)
_PENDING_WATCH_BLOCKERS = {
    "INITIATION_WAITING_FOR_IMMEDIATE_HOLD_OR_RETEST",
    "INITIATION_WAITING_FOR_OUTSIDE_RECLAIM",
    "FAILED_DIRECTIONAL_FOLLOWTHROUGH_PENDING",
    "FAILED_RENEWED_OUTSIDE_ENTRY_BLOCKED",
    "BREAKOUT_STATE_UNKNOWN",
    "FAILED_STATE_UNKNOWN",
}


class SetupManager:
    def __init__(self, config: AuctionEngineConfig) -> None:
        self.config = config

    def evaluate(
        self,
        symbol: str,
        snapshot_time: datetime,
        opportunities: Iterable[OpportunityRecord],
    ) -> ManagerDecision:
        records = tuple(opportunities)
        active = [record for record in records if record.active_eligible]
        watches = [
            record
            for record in records
            if record.active_watch and record.watch_candidates(snapshot_time)
        ]
        buys = [record for record in active if record.side is TradeSide.BUY]
        sells = [record for record in active if record.side is TradeSide.SELL]
        historical_switches, historical_sequence = self._historical_side_switches(
            records,
            snapshot_time,
        )
        diagnostics: Dict[str, object] = {
            "decision_scope": "LOCAL_AUCTION_ONLY",
            "signal_context_applied": False,
            "unique_opportunity_count": len(records),
            "active_eligible_count": len(active),
            "active_watch_count": len(watches),
            "active_buy_count": len(buys),
            "active_sell_count": len(sells),
            "active_opportunity_keys": [record.opportunity_key for record in active],
            "watch_opportunity_keys": [record.opportunity_key for record in watches],
            "historical_selected_side_sequence": historical_sequence,
            "historical_side_switches_in_lookback": historical_switches,
            "alias_double_counting_prevented": True,
        }

        if not active:
            return ManagerDecision(
                symbol=symbol,
                snapshot_time=snapshot_time,
                action=ManagerAction.NO_ACTION,
                reason_codes=("NO_ACTIVE_ELIGIBLE_OPPORTUNITY",),
                diagnostics=diagnostics,
                config_version=self.config.engine.config_version,
            )

        if buys and sells:
            ordered = sorted(
                active,
                key=lambda record: record.eligible_time or record.first_observed_time,
            )
            ids = tuple(
                candidate.candidate_id
                for record in ordered
                for candidate in record.eligible_candidates()[:1]
            )
            reason = (
                "DEFER_REPEATED_SIDE_ROTATION"
                if historical_switches
                >= self.config.decision.rotation_side_switches_to_defer
                else "DEFER_MATERIAL_OPPOSITION"
            )
            return ManagerDecision(
                symbol=symbol,
                snapshot_time=snapshot_time,
                action=ManagerAction.DEFER,
                opposing_candidate_ids=ids,
                material_opposition=True,
                reason_codes=(reason,),
                diagnostics=diagnostics,
                config_version=self.config.engine.config_version,
            )

        same_side = buys or sells
        selected_record = max(
            same_side,
            key=lambda record: (
                record.eligible_time or record.first_observed_time,
                record.first_observed_time,
                record.opportunity_key,
            ),
        )
        selected_candidate = selected_record.selected_candidate()
        if selected_candidate is None:
            return ManagerDecision(
                symbol=symbol,
                snapshot_time=snapshot_time,
                action=ManagerAction.BLOCK,
                reason_codes=("ELIGIBLE_OPPORTUNITY_HAS_NO_ELIGIBLE_ALIAS",),
                diagnostics=diagnostics,
                config_version=self.config.engine.config_version,
            )

        projected_switches = historical_switches
        if historical_sequence and historical_sequence[-1][1] != selected_record.side.value:
            projected_switches += 1
        diagnostics["recent_eligible_side_switches"] = projected_switches
        diagnostics["selected_opportunity_key"] = selected_record.opportunity_key
        diagnostics["selected_side"] = selected_record.side.value
        diagnostics["selected_candidate_eligibility"] = selected_candidate.eligibility.value
        diagnostics["same_side_distinct_opportunity_count"] = len(same_side)

        opposite_watch = [
            record for record in watches if record.side is not selected_record.side
        ]
        watch_details: List[Dict[str, object]] = []
        material_watch_candidates: List[SetupCandidate] = []
        ignored_watch_candidates: List[SetupCandidate] = []
        for record in opposite_watch:
            for candidate in record.watch_candidates(snapshot_time):
                material, reason = self._watch_materiality(candidate, snapshot_time)
                watch_details.append(
                    {
                        "opportunity_key": record.opportunity_key,
                        "candidate_id": candidate.candidate_id,
                        "side": candidate.side.value,
                        "material": material,
                        "classification_reason": reason,
                        "blockers": list(candidate.blockers),
                        "valid_until": (
                            candidate.valid_until.isoformat(sep=" ")
                            if candidate.valid_until
                            else None
                        ),
                    }
                )
                target = (
                    material_watch_candidates
                    if material
                    else ignored_watch_candidates
                )
                target.append(candidate)
        diagnostics["opposing_watch_considered"] = watch_details
        diagnostics["material_opposing_watch_count"] = len(material_watch_candidates)
        diagnostics["non_material_opposing_watch_count"] = len(
            ignored_watch_candidates
        )

        if (
            self.config.decision.unresolved_watch_opposition_enabled
            and material_watch_candidates
        ):
            ids = tuple(
                candidate.candidate_id for candidate in material_watch_candidates
            )
            return ManagerDecision(
                symbol=symbol,
                snapshot_time=snapshot_time,
                action=ManagerAction.DEFER,
                opposing_candidate_ids=ids,
                material_opposition=True,
                reason_codes=("DEFER_MATERIAL_WATCH_OPPOSITION",),
                diagnostics=diagnostics,
                config_version=self.config.engine.config_version,
            )

        if projected_switches >= self.config.decision.rotation_side_switches_to_defer:
            return ManagerDecision(
                symbol=symbol,
                snapshot_time=snapshot_time,
                action=ManagerAction.DEFER,
                opposing_candidate_ids=(selected_candidate.candidate_id,),
                material_opposition=True,
                reason_codes=("DEFER_REPEATED_SIDE_ROTATION",),
                diagnostics=diagnostics,
                config_version=self.config.engine.config_version,
            )

        support = tuple(
            candidate.candidate_id
            for record in same_side
            if record.opportunity_key != selected_record.opportunity_key
            for candidate in record.eligible_candidates()[:1]
        )
        reasons = [
            "SELECT_MOST_RECENT_ACTIVE_STRUCTURE",
            "SELECTED_ALIAS_IS_CURRENTLY_ELIGIBLE",
            "ALIASES_ALREADY_COLLAPSED_BY_OPPORTUNITY_KEY",
        ]
        if ignored_watch_candidates:
            reasons.append("NON_MATERIAL_WATCH_OPPOSITION_IGNORED")

        return ManagerDecision(
            symbol=symbol,
            snapshot_time=snapshot_time,
            action=ManagerAction.SELECT,
            selected_candidate_id=selected_candidate.candidate_id,
            same_direction_support_ids=support,
            reason_codes=tuple(reasons),
            diagnostics=diagnostics,
            config_version=self.config.engine.config_version,
        )

    def _historical_side_switches(
        self,
        records: Iterable[OpportunityRecord],
        now: datetime,
    ) -> Tuple[int, List[Tuple[str, str]]]:
        window = timedelta(minutes=self.config.decision.rotation_lookback_minutes)
        ordered = sorted(
            (
                record
                for record in records
                if record.selected_time is not None
                and record.selected_time < now
                and now - record.selected_time <= window
            ),
            key=lambda record: (record.selected_time, record.opportunity_key),
        )
        sequence = [
            (record.selected_time.isoformat(sep=" "), record.side.value)
            for record in ordered
        ]
        switches = sum(
            1
            for previous, current in zip(ordered, ordered[1:])
            if previous.side is not current.side
        )
        return switches, sequence

    @staticmethod
    def _watch_materiality(
        candidate: SetupCandidate,
        now: datetime,
    ) -> Tuple[bool, str]:
        if candidate.valid_until is not None and candidate.valid_until < now:
            return False, "STALE_OPPOSITION_IGNORED"
        blockers = set(candidate.blockers)
        if any(
            token in blocker
            for blocker in blockers
            for token in _STRUCTURAL_WATCH_BLOCKER_TOKENS
        ):
            return False, "STRUCTURALLY_BLOCKED_OPPOSITION_IGNORED"
        if blockers and blockers <= _PENDING_WATCH_BLOCKERS:
            return True, "PENDING_CONFIRMATION_MATERIAL_OPPOSITION"
        if candidate.dynamic_watch and not blockers:
            return True, "DYNAMIC_WATCH_MATERIAL_OPPOSITION"
        return False, "NON_MATERIAL_OPPOSITION"


__all__ = ["SetupManager"]
