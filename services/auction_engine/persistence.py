"""Persistence boundary for Auction opportunities and restart checkpoints."""
from __future__ import annotations

from hashlib import sha256
import json
from time import perf_counter
from typing import Any, Dict, Iterable, Optional

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from schemas.stock_engine_checkpoint import StockEngineCheckpoint
from schemas.stock_opportunity import StockOpportunity
from services.auction_engine.contracts import AuctionEngineResult
from services.signals.signal_lifecycle_service import SignalLifecycleResult
from utils.datetime_utils import to_ist_naive
from utils.json_utils import sanitize_json


class AuctionPersistenceCoordinator:
    def __init__(
        self,
        config: AuctionEngineConfig = AUCTION_ENGINE_CONFIG,
        *,
        opportunity_write_enabled: bool = False,
        checkpoint_write_enabled: bool = False,
        profile_timing_enabled: bool = False,
    ) -> None:
        self.config = config
        self.opportunity_write_enabled = bool(opportunity_write_enabled)
        self.checkpoint_write_enabled = bool(checkpoint_write_enabled)
        self.profile_timing_enabled = bool(profile_timing_enabled)
        self._record_hashes: Dict[str, str] = {}

    def reset(self) -> None:
        self._record_hashes.clear()

    def load_checkpoint(self, *, trading_day, symbol: str) -> Optional[StockEngineCheckpoint]:
        return StockEngineCheckpoint.fetch_one(
            trading_day=trading_day,
            symbol=symbol,
            engine_name=self.config.engine.engine_name,
        )

    def persist_after_signal(
        self,
        *,
        result: AuctionEngineResult,
        engine: Any,
        signal_result: SignalLifecycleResult,
    ) -> Dict[str, Any]:
        """Persist opportunity projection then the complete engine checkpoint.

        The caller marks ``snapshots.processed`` only after this method returns.
        Timing diagnostics are collected only when ``profile_timing_enabled`` is
        true; the persistence order and database behavior are unchanged.
        """
        opportunity_writes = 0
        opportunities_examined = 0
        opportunity_events_examined = 0
        opportunity_projection_ms = 0.0
        opportunity_database_ms = 0.0
        if self.opportunity_write_enabled:
            opportunity_result = self._persist_opportunities(
                result=result,
                engine=engine,
                signal_result=signal_result,
            )
            opportunity_writes = int(opportunity_result["writes"])
            opportunities_examined = int(opportunity_result["records_examined"])
            opportunity_events_examined = int(opportunity_result["events_examined"])
            opportunity_projection_ms = float(opportunity_result["projection_ms"])
            opportunity_database_ms = float(opportunity_result["database_ms"])

        checkpoint_writes = 0
        checkpoint_export_ms = 0.0
        checkpoint_database_ms = 0.0
        checkpoint_state_bytes = 0
        if self.checkpoint_write_enabled:
            started = perf_counter() if self.profile_timing_enabled else 0.0
            payload = self._checkpoint_payload(
                result=result,
                engine=engine,
                signal_result=signal_result,
            )
            if self.profile_timing_enabled:
                checkpoint_export_ms = _elapsed_ms(started)
                checkpoint_state_bytes = len(
                    json.dumps(
                        sanitize_json(payload.state_json),
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode("utf-8")
                )
                started = perf_counter()
            StockEngineCheckpoint.upsert(payload)
            if self.profile_timing_enabled:
                checkpoint_database_ms = _elapsed_ms(started)
            checkpoint_writes = 1

        return {
            "opportunities_written": opportunity_writes,
            "checkpoints_written": checkpoint_writes,
            "opportunities_examined": opportunities_examined,
            "opportunity_events_examined": opportunity_events_examined,
            "opportunity_projection_ms": opportunity_projection_ms,
            "opportunity_database_ms": opportunity_database_ms,
            "checkpoint_export_ms": checkpoint_export_ms,
            "checkpoint_database_ms": checkpoint_database_ms,
            "checkpoint_state_bytes": checkpoint_state_bytes,
        }

    def _persist_opportunities(
        self,
        *,
        result: AuctionEngineResult,
        engine: Any,
        signal_result: SignalLifecycleResult,
    ) -> Dict[str, Any]:
        writes = 0
        records_examined = 0
        events_examined = 0
        projection_ms = 0.0
        database_ms = 0.0

        started = perf_counter() if self.profile_timing_enabled else 0.0
        events_by_key: Dict[str, list] = {}
        events = list(engine.opportunity_ledger.events(result.symbol))
        events_examined = len(events)
        for event in events:
            events_by_key.setdefault(event.opportunity_key, []).append(event)
        if self.profile_timing_enabled:
            projection_ms += _elapsed_ms(started)

        for record in engine.opportunity_ledger.records(result.symbol):
            records_examined += 1
            started = perf_counter() if self.profile_timing_enabled else 0.0
            linked_signal_id = (
                signal_result.signal_id
                if signal_result.signal_opportunity_key == record.opportunity_key
                else None
            )
            payload = self._opportunity_payload(
                record,
                events_by_key.get(record.opportunity_key, ()),
                signal_id=linked_signal_id,
            )
            digest = _stable_hash(payload.model_dump(mode="json"))
            if self.profile_timing_enabled:
                projection_ms += _elapsed_ms(started)
            if self._record_hashes.get(record.opportunity_key) == digest:
                continue
            started = perf_counter() if self.profile_timing_enabled else 0.0
            StockOpportunity.upsert(payload)
            if self.profile_timing_enabled:
                database_ms += _elapsed_ms(started)
            self._record_hashes[record.opportunity_key] = digest
            writes += 1
        return {
            "writes": writes,
            "records_examined": records_examined,
            "events_examined": events_examined,
            "projection_ms": projection_ms,
            "database_ms": database_ms,
        }

    def _opportunity_payload(
        self,
        record: Any,
        event_history: Iterable[Any],
        *,
        signal_id: Optional[str],
    ) -> StockOpportunity:
        candidate = record.primary_candidate
        times = [
            value for value in (
                record.last_observed_time,
                record.selected_time,
                record.consumed_time,
                record.terminal_time,
            ) if value is not None
        ]
        last_time = max(times)
        return StockOpportunity(
            trading_day=record.trading_day,
            symbol=record.symbol,
            opportunity_key=record.opportunity_key,
            boundary_event_key=record.boundary_event_key,
            range_id=candidate.source_frozen_range_id,
            side=record.side.value,
            primary_setup_family=candidate.family.value,
            primary_setup_subtype=candidate.subtype,
            primary_candidate_id=candidate.candidate_id,
            primary_candidate_role=candidate.candidate_role.value,
            lifecycle_state=record.lifecycle_state,
            attempt_time=candidate.event_time,
            first_observed_time=record.first_observed_time,
            last_observed_time=record.last_observed_time,
            eligible_time=record.eligible_time,
            terminal_time=record.terminal_time,
            selected_time=record.selected_time,
            consumed_time=record.consumed_time,
            entry_anchor_price=candidate.entry_price,
            boundary_price=candidate.source_boundary_price,
            stop_anchor_price=candidate.stop_anchor_price,
            target_basis=candidate.target_basis,
            target_reference_price=candidate.target_reference_price,
            source_auction_state=candidate.auction_state.value,
            established_trend_side=str(
                candidate.diagnostics.get("established_trend_side") or "UNKNOWN"
            ).upper(),
            candidate_interpretations_json=record.candidate_interpretations(),
            event_history_json=[event.to_dict() for event in event_history],
            reason_codes_json=list(record.reason_codes),
            diagnostics_json={
                "boundary_status": record.boundary_status.value,
                "boundary_resolution": record.boundary_resolution.value,
                "boundary_terminal": bool(record.boundary_terminal),
                "boundary_terminal_reason": record.boundary_terminal_reason,
                "candidate_ids": list(record.candidate_ids),
                "supporting_candidate_ids": list(record.supporting_candidate_ids),
                "selected_candidate_id": record.selected_candidate_id,
                "decision_count": int(record.decision_count),
            },
            config_version=candidate.config_version,
            signal_id=signal_id,
            created_at=record.first_observed_time,
            updated_at=last_time,
        )

    def _checkpoint_payload(
        self,
        *,
        result: AuctionEngineResult,
        engine: Any,
        signal_result: SignalLifecycleResult,
    ) -> StockEngineCheckpoint:
        ts = to_ist_naive(result.snapshot_time)
        if ts is None:
            raise ValueError("Auction checkpoint requires snapshot_time")
        state_json = engine.export_checkpoint(result.symbol)
        return StockEngineCheckpoint(
            trading_day=ts.date(),
            symbol=result.symbol,
            engine_name=self.config.engine.engine_name,
            engine_version=self.config.engine.engine_version,
            config_version=self.config.engine.config_version,
            last_processed_snapshot_time=ts,
            last_snapshot_hash=engine._last_input_hashes.get(result.symbol),
            checkpoint_status="ACTIVE",
            checkpoint_version=1,
            state_json=sanitize_json(state_json),
            diagnostics_json=sanitize_json({
                "signal_requested_action": signal_result.requested_action,
                "signal_applied_action": signal_result.applied_action,
                "signal_id": signal_result.signal_id,
                "signal_persisted": signal_result.persisted,
                "selected_opportunity_key": signal_result.opportunity_key,
                "signal_opportunity_key": signal_result.signal_opportunity_key,
                "final_decision": result.final_decision.action.value,
                "auction_state": result.auction_state.current_state.value,
            }),
            created_at=ts,
            updated_at=ts,
        )


def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000.0


def _stable_hash(value: Any) -> str:
    raw = json.dumps(
        sanitize_json(value),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256(raw).hexdigest()


__all__ = ["AuctionPersistenceCoordinator"]
