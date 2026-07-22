"""Single-owner runtime orchestrator for Auction Engine and signal lifecycle."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import logging
from math import ceil
from statistics import median
from time import perf_counter
from typing import Any, Dict, Iterable, List, Optional, Tuple

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from configs.auction_service_config import AUCTION_SERVICE_CONFIG, AuctionServiceConfig
from services.auction_engine.contracts import AuctionEngineResult, FinalAction
from services.auction_engine.engine import AuctionEngine
from services.auction_engine.checkpoint_codec import checkpoint_state_hash
from services.signals.signal_lifecycle_service import (
    SignalLifecycleResult,
    SignalLifecycleService,
)
from utils.datetime_utils import to_ist_naive

logger = logging.getLogger(__name__)


@dataclass
class SymbolRuntime:
    engine: AuctionEngine
    restored: bool = False
    checkpoint_time: Optional[datetime] = None


@dataclass
class AuctionServiceStats:
    trading_day: date
    snapshots_seen: int = 0
    snapshots_evaluated: int = 0
    snapshots_skipped_by_checkpoint: int = 0
    snapshots_marked_processed: int = 0
    would_create_count: int = 0
    manager_select_count: int = 0
    manager_actions: Dict[str, int] = field(default_factory=dict)
    final_actions: Dict[str, int] = field(default_factory=dict)
    signal_actions: Dict[str, int] = field(default_factory=dict)
    opportunities_written: int = 0
    checkpoints_written: int = 0
    checkpoints_restored: int = 0
    first_snapshot_time: Optional[datetime] = None
    last_snapshot_time: Optional[datetime] = None
    errors: int = 0
    decision_rows: List[dict] = field(default_factory=list)
    signal_rows: List[dict] = field(default_factory=list)
    timing_rows: List[dict] = field(default_factory=list)


class AuctionServiceRunner:
    """Own the snapshot cadence and push each result to signal lifecycle."""

    def __init__(
        self,
        *,
        engine_config: AuctionEngineConfig = AUCTION_ENGINE_CONFIG,
        service_config: AuctionServiceConfig = AUCTION_SERVICE_CONFIG,
        signal_service: Optional[SignalLifecycleService] = None,
        persistence: Optional[Any] = None,
        restore_enabled: Optional[bool] = None,
        mark_processed_enabled: Optional[bool] = None,
        engine_factory: Any = AuctionEngine,
        profile_timing: bool = False,
    ) -> None:
        self.engine_config = engine_config
        self.service_config = service_config
        self.engine_factory = engine_factory
        self.profile_timing = bool(profile_timing)
        self.signal_service = signal_service or SignalLifecycleService(
            lifecycle=service_config.signal_lifecycle,
            write_enabled=service_config.signal_write_enabled,
        )
        if persistence is None:
            from services.auction_engine.persistence import AuctionPersistenceCoordinator
            persistence = AuctionPersistenceCoordinator(
                engine_config,
                opportunity_write_enabled=service_config.opportunity_write_enabled,
                checkpoint_write_enabled=service_config.checkpoint_write_enabled,
            )
        self.persistence = persistence
        if hasattr(self.persistence, "profile_timing_enabled"):
            self.persistence.profile_timing_enabled = self.profile_timing
        self.restore_enabled = (
            service_config.restore_checkpoint_when_memory_missing
            if restore_enabled is None else bool(restore_enabled)
        )
        self.mark_processed_enabled = (
            service_config.mark_snapshot_processed_enabled
            if mark_processed_enabled is None else bool(mark_processed_enabled)
        )
        self.trading_day: Optional[date] = None
        self.runtimes: Dict[str, SymbolRuntime] = {}
        self.stats: Optional[AuctionServiceStats] = None

    def start_day(self, trading_day: date) -> None:
        self.trading_day = trading_day
        self.runtimes.clear()
        self.signal_service.reset()
        self.persistence.reset()
        self.stats = AuctionServiceStats(trading_day=trading_day)

    def process_snapshots(self, snapshots: Iterable[Any]) -> List[AuctionEngineResult]:
        self._require_started()
        results: List[AuctionEngineResult] = []
        for snapshot in sorted(snapshots, key=lambda row: (_snapshot_identity(row)[1], _snapshot_identity(row)[0])):
            result = self.process_snapshot(snapshot)
            if result is not None:
                results.append(result)
        return results

    def process_snapshot(self, snapshot: Any) -> Optional[AuctionEngineResult]:
        self._require_started()
        symbol, raw_ts = _snapshot_identity(snapshot)
        ts = to_ist_naive(raw_ts) or raw_ts
        self.stats.snapshots_seen += 1
        if ts.date() != self.trading_day:
            raise ValueError(f"Snapshot day mismatch: {ts.date()} != {self.trading_day}")

        total_started = perf_counter() if self.profile_timing else 0.0
        timing = {
            "symbol": symbol,
            "snapshot_time": ts.isoformat(sep=" "),
            "runtime_source": None,
            "runtime_lookup_restore_ms": 0.0,
            "equity_ref_ms": 0.0,
            "active_signal_context_ms": 0.0,
            "auction_evaluation_ms": 0.0,
            "signal_lifecycle_ms": 0.0,
            "opportunity_projection_ms": 0.0,
            "opportunity_database_ms": 0.0,
            "checkpoint_export_ms": 0.0,
            "checkpoint_database_ms": 0.0,
            "mark_processed_ms": 0.0,
            "runner_record_ms": 0.0,
            "total_ms": 0.0,
            "manager_action": None,
            "final_action": None,
            "signal_action": None,
            "signal_persisted": False,
            "opportunities_examined": 0,
            "opportunity_events_examined": 0,
            "opportunities_written": 0,
            "checkpoints_written": 0,
            "checkpoint_state_bytes": 0,
            "snapshot_marked_processed": False,
            "skipped_by_checkpoint": False,
            "error": None,
        }

        runtime_preexisting = symbol in self.runtimes
        started = perf_counter() if self.profile_timing else 0.0
        runtime = self._runtime(symbol)
        if self.profile_timing:
            timing["runtime_lookup_restore_ms"] = _elapsed_ms(started)
            timing["runtime_source"] = (
                "MEMORY"
                if runtime_preexisting
                else ("RESTORED" if runtime.restored else "INITIALIZED")
            )

        if runtime.checkpoint_time is not None and ts <= runtime.checkpoint_time:
            # Expected only after a crash between checkpoint commit and processed
            # acknowledgement. Signal persistence already completed before the
            # checkpoint, so acknowledge the row without advancing engine state.
            self.stats.snapshots_skipped_by_checkpoint += 1
            timing["skipped_by_checkpoint"] = True
            if self.mark_processed_enabled:
                started = perf_counter() if self.profile_timing else 0.0
                self._mark_processed(snapshot)
                if self.profile_timing:
                    timing["mark_processed_ms"] = _elapsed_ms(started)
                    timing["snapshot_marked_processed"] = True
            if self.profile_timing:
                timing["total_ms"] = _elapsed_ms(total_started)
                self.stats.timing_rows.append(timing)
            return None

        try:
            started = perf_counter() if self.profile_timing else 0.0
            equity_ref = self.signal_service.resolve_equity_ref(symbol)
            if self.profile_timing:
                timing["equity_ref_ms"] = _elapsed_ms(started)

            started = perf_counter() if self.profile_timing else 0.0
            context = self.signal_service.get_active_context(
                snapshot=snapshot,
                equity_ref=equity_ref,
            )
            if self.profile_timing:
                timing["active_signal_context_ms"] = _elapsed_ms(started)

            started = perf_counter() if self.profile_timing else 0.0
            result = runtime.engine.evaluate_snapshot(
                snapshot,
                equity_ref=equity_ref,
                active_context=context,
            )
            if self.profile_timing:
                timing["auction_evaluation_ms"] = _elapsed_ms(started)

            started = perf_counter() if self.profile_timing else 0.0
            signal_result = self.signal_service.apply_instruction(
                snapshot=snapshot,
                result=result,
                context_before=context,
            )
            if self.profile_timing:
                timing["signal_lifecycle_ms"] = _elapsed_ms(started)

            writes = self.persistence.persist_after_signal(
                result=result,
                engine=runtime.engine,
                signal_result=signal_result,
            )
            runtime.checkpoint_time = ts
            self.stats.opportunities_written += writes["opportunities_written"]
            self.stats.checkpoints_written += writes["checkpoints_written"]

            if self.profile_timing:
                for key in (
                    "opportunity_projection_ms",
                    "opportunity_database_ms",
                    "checkpoint_export_ms",
                    "checkpoint_database_ms",
                    "opportunities_examined",
                    "opportunity_events_examined",
                    "opportunities_written",
                    "checkpoints_written",
                    "checkpoint_state_bytes",
                ):
                    timing[key] = writes.get(key, timing[key])

            if self.mark_processed_enabled:
                started = perf_counter() if self.profile_timing else 0.0
                self._mark_processed(snapshot)
                if self.profile_timing:
                    timing["mark_processed_ms"] = _elapsed_ms(started)
                    timing["snapshot_marked_processed"] = True

            started = perf_counter() if self.profile_timing else 0.0
            self._record(result, signal_result, context)
            if self.profile_timing:
                timing["runner_record_ms"] = _elapsed_ms(started)
                timing["manager_action"] = result.manager_decision.action.value
                timing["final_action"] = result.final_decision.action.value
                timing["signal_action"] = signal_result.applied_action
                timing["signal_persisted"] = bool(signal_result.persisted)
                timing["total_ms"] = _elapsed_ms(total_started)
                self.stats.timing_rows.append(timing)
            return result
        except Exception as exc:
            self.stats.errors += 1
            if self.profile_timing:
                timing["error"] = f"{type(exc).__name__}: {exc}"
                timing["total_ms"] = _elapsed_ms(total_started)
                self.stats.timing_rows.append(timing)
            logger.exception("Auction snapshot failed | %s @ %s", symbol, ts)
            if self.service_config.fail_fast_on_snapshot_error:
                raise
            return None

    def _runtime(self, symbol: str) -> SymbolRuntime:
        runtime = self.runtimes.get(symbol)
        if runtime is not None:
            return runtime

        engine = self.engine_factory(self.engine_config)
        runtime = SymbolRuntime(engine=engine)
        if self.restore_enabled:
            checkpoint = self.persistence.load_checkpoint(
                trading_day=self.trading_day,
                symbol=symbol,
            )
            if checkpoint is not None:
                self._validate_checkpoint(checkpoint, symbol)
                engine.restore_checkpoint(symbol, checkpoint.state_json)
                runtime.restored = True
                runtime.checkpoint_time = to_ist_naive(
                    checkpoint.last_processed_snapshot_time
                )
                self.stats.checkpoints_restored += 1
        self.runtimes[symbol] = runtime
        return runtime

    def _validate_checkpoint(self, checkpoint: Any, symbol: str) -> None:
        expected = {
            "trading_day": self.trading_day,
            "symbol": symbol,
            "engine_name": self.engine_config.engine.engine_name,
            "engine_version": self.engine_config.engine.engine_version,
            "config_version": self.engine_config.engine.config_version,
        }
        for field, value in expected.items():
            actual = getattr(checkpoint, field)
            if str(actual).upper() != str(value).upper():
                raise ValueError(
                    f"Invalid checkpoint {field} for {symbol}: {actual!r} != {value!r}"
                )
        if str(checkpoint.checkpoint_status).upper() not in {"ACTIVE", "COMPLETE"}:
            raise ValueError(
                f"Invalid checkpoint status for {symbol}: {checkpoint.checkpoint_status}"
            )

    def _mark_processed(self, snapshot: Any) -> None:
        from schemas.snapshot import SnapshotSchema
        symbol, snapshot_time = _snapshot_identity(snapshot)
        if not SnapshotSchema.mark_processed(symbol, snapshot_time):
            raise RuntimeError(
                f"Failed to mark snapshot processed: {symbol} @ {snapshot_time}"
            )
        self.stats.snapshots_marked_processed += 1

    def _record(
        self,
        result: AuctionEngineResult,
        signal_result: SignalLifecycleResult,
        context_before: Any,
    ) -> None:
        ts = to_ist_naive(result.snapshot_time) or result.snapshot_time
        self.stats.snapshots_evaluated += 1
        if self.stats.first_snapshot_time is None or ts < self.stats.first_snapshot_time:
            self.stats.first_snapshot_time = ts
        if self.stats.last_snapshot_time is None or ts > self.stats.last_snapshot_time:
            self.stats.last_snapshot_time = ts
        manager_action = result.manager_decision.action.value
        final_action = result.final_decision.action.value
        self.stats.manager_actions[manager_action] = (
            self.stats.manager_actions.get(manager_action, 0) + 1
        )
        self.stats.final_actions[final_action] = (
            self.stats.final_actions.get(final_action, 0) + 1
        )
        if manager_action == "SELECT":
            self.stats.manager_select_count += 1
        if result.final_decision.action is FinalAction.CREATE:
            self.stats.would_create_count += 1
        action = signal_result.applied_action
        self.stats.signal_actions[action] = self.stats.signal_actions.get(action, 0) + 1
        selected = result.final_decision.selected_candidate
        self.stats.decision_rows.append({
            "symbol": result.symbol,
            "snapshot_time": ts.isoformat(sep=" "),
            "auction_state": result.auction_state.current_state.value,
            "manager_intent": result.manager_decision.action.value,
            "manager_action": result.manager_decision.action.value,
            "final_signal_action": result.final_decision.action.value,
            "final_action": result.final_decision.action.value,
            "opportunity_key": selected.opportunity_key if selected else None,
            "candidate_id": selected.candidate_id if selected else None,
            "setup_family": selected.family.value if selected else None,
            "side": selected.side.value if selected else None,
            "active_context_status_before": context_before.evaluation_status,
            "active_signal_id_before": context_before.active_signal_id,
            "active_signal_side_before": context_before.active_signal_side,
            "active_signal_opportunity_key_before": (
                context_before.active_signal_opportunity_key
            ),
            "decision_reason_codes": "|".join(result.final_decision.reason_codes),
            "active_context_resolution_applied": bool(
                result.final_decision.diagnostics.get(
                    "active_context_resolution_applied"
                )
            ),
        })
        self.stats.signal_rows.append({
            "symbol": result.symbol,
            "snapshot_time": ts.isoformat(sep=" "),
            "manager_intent": result.manager_decision.action.value,
            "final_signal_action": result.final_decision.action.value,
            "requested_action": signal_result.requested_action,
            "applied_action": signal_result.applied_action,
            "signal_id": signal_result.signal_id,
            "opportunity_key": signal_result.opportunity_key,
            "signal_opportunity_key": signal_result.signal_opportunity_key,
            "persisted": signal_result.persisted,
            "active_signal_side_before": context_before.active_signal_side,
            "active_signal_opportunity_key_before": (
                context_before.active_signal_opportunity_key
            ),
            "reason_codes": "|".join(signal_result.reason_codes),
            "defensive_guard_triggered": bool(
                signal_result.diagnostics.get("defensive_guard_triggered")
            ),
            "idempotent_create_retry": bool(
                signal_result.diagnostics.get("idempotent_create_retry")
            ),
        })

    def timing_summary(self) -> List[dict]:
        """Return per-stage timing statistics for an opt-in profiling run."""
        rows = list(self.stats.timing_rows if self.stats is not None else ())
        if not rows:
            return []
        stage_fields = (
            "runtime_lookup_restore_ms",
            "equity_ref_ms",
            "active_signal_context_ms",
            "auction_evaluation_ms",
            "signal_lifecycle_ms",
            "opportunity_projection_ms",
            "opportunity_database_ms",
            "checkpoint_export_ms",
            "checkpoint_database_ms",
            "mark_processed_ms",
            "runner_record_ms",
            "total_ms",
        )
        total_wall_ms = sum(float(row.get("total_ms") or 0.0) for row in rows)
        output = []
        for stage in stage_fields:
            values = [float(row.get(stage) or 0.0) for row in rows]
            ordered = sorted(values)
            total_ms = sum(values)
            p95_index = max(0, min(len(ordered) - 1, ceil(len(ordered) * 0.95) - 1))
            output.append({
                "stage": stage.removesuffix("_ms"),
                "samples": len(values),
                "total_ms": round(total_ms, 6),
                "average_ms": round(total_ms / len(values), 6),
                "median_ms": round(float(median(values)), 6),
                "p95_ms": round(ordered[p95_index], 6),
                "max_ms": round(max(values), 6),
                "share_of_total_wall_pct": round(
                    (total_ms / total_wall_ms * 100.0) if total_wall_ms else 0.0,
                    4,
                ),
            })
        return output

    def checkpoint_rows(self) -> List[dict]:
        rows = []
        for symbol, runtime in sorted(self.runtimes.items()):
            result = runtime.engine._last_results.get(symbol)
            rows.append({
                "trading_day": self.trading_day.isoformat(),
                "symbol": symbol,
                "restored": runtime.restored,
                "last_processed_snapshot_time": (
                    runtime.checkpoint_time.isoformat(sep=" ")
                    if runtime.checkpoint_time else None
                ),
                "auction_state": (
                    result.auction_state.current_state.value if result else None
                ),
                "opportunity_count": len(runtime.engine.opportunity_ledger.records(symbol)),
                "checkpoint_state_hash": self._checkpoint_state_hash(
                    runtime.engine, symbol
                ),
            })
        return rows

    @staticmethod
    def _checkpoint_state_hash(engine: AuctionEngine, symbol: str) -> str:
        return checkpoint_state_hash(engine.export_checkpoint(symbol))

    def _require_started(self) -> None:
        if self.trading_day is None or self.stats is None:
            raise RuntimeError("Auction service day has not been started")


def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000.0


def _snapshot_identity(snapshot: Any) -> Tuple[str, datetime]:
    if isinstance(snapshot, dict):
        symbol = snapshot.get("symbol")
        snapshot_time = snapshot.get("snapshot_time")
    else:
        symbol = getattr(snapshot, "symbol", None)
        snapshot_time = getattr(snapshot, "snapshot_time", None)
    key = str(symbol or "").strip().upper()
    if isinstance(snapshot_time, str):
        snapshot_time = datetime.fromisoformat(snapshot_time)
    if not key or not isinstance(snapshot_time, datetime):
        raise ValueError("Snapshot symbol and snapshot_time are required")
    return key, snapshot_time


__all__ = ["AuctionServiceRunner", "AuctionServiceStats", "SymbolRuntime"]
