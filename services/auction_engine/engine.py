"""Deterministic, signal-agnostic Auction Engine orchestration.

The engine owns evidence interpretation, auction state, boundary episodes,
setup candidates, the stock-day opportunity ledger, local arbitration and the
final local opportunity assessment. It does not read signal/trade state, apply
Advisor policy, create signal payloads, or perform database writes.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from services.auction_engine.contracts import (
    AuctionEngineResult,
    EvidenceSnapshot,
    ManagerAction,
)
from services.auction_engine.boundary_engine import BoundaryEpisodeEngine
from services.auction_engine.evidence import EvidenceBuilder
from services.auction_engine.state_engine import (
    AuctionStateChronologyError,
    AuctionStateEngine,
)
from services.auction_engine.setup_engine import SetupCandidateEngine
from services.auction_engine.opportunity_ledger import OpportunityLedger
from services.auction_engine.setup_manager import SetupManager
from services.auction_engine.decision_engine import DecisionEngine
from services.auction_engine.checkpoint_codec import (
    decode_checkpoint_value,
    encode_checkpoint_value,
)


@dataclass(frozen=True)
class _HistoryTrend:
    hma_order: str
    hma_spread_atr: Optional[float]


@dataclass(frozen=True)
class _HistoryEvidence:
    close: float
    bar: Any
    trend: _HistoryTrend


class AuctionEngine:
    """Chronological pure Auction Engine.

    Checkpoint methods remain temporarily for compatibility with the parallel
    branch and will be removed when snapshot-carried state is introduced.
    """

    def __init__(
        self,
        config: AuctionEngineConfig = AUCTION_ENGINE_CONFIG,
    ) -> None:
        self.config = config
        self.evidence_builder = EvidenceBuilder(config)
        self.state_engine = AuctionStateEngine(config)
        self.boundary_engine = BoundaryEpisodeEngine(config)
        self.setup_engine = SetupCandidateEngine(config)
        self.opportunity_ledger = OpportunityLedger()
        self.setup_manager = SetupManager(config)
        self.decision_engine = DecisionEngine(config)
        self._history: Dict[str, Deque[Any]] = defaultdict(
            lambda: deque(maxlen=self.config.state.history_bars)
        )
        self._last_results: Dict[str, AuctionEngineResult] = {}
        self._last_input_hashes: Dict[str, str] = {}

    def reset(self, symbol: Optional[str] = None) -> None:
        if symbol is None:
            self._history.clear()
            self._last_results.clear()
            self._last_input_hashes.clear()
            self.state_engine.reset()
            self.boundary_engine.reset()
            self.setup_engine.reset()
            self.opportunity_ledger.reset()
            return
        key = str(symbol).strip().upper()
        self._history.pop(key, None)
        self._last_results.pop(key, None)
        self._last_input_hashes.pop(key, None)
        self.state_engine.reset(key)
        self.boundary_engine.reset(key)
        self.setup_engine.reset(key)
        self.opportunity_ledger.reset(key)

    def evaluate_snapshot(
        self,
        snapshot: Any,
        *,
        equity_ref: Optional[str] = None,
    ) -> AuctionEngineResult:
        symbol, snapshot_time = _snapshot_identity(snapshot)
        input_hash = _snapshot_content_hash(snapshot)
        last_result = self._last_results.get(symbol)
        if last_result is not None:
            if (
                snapshot_time < last_result.snapshot_time
                and self.config.state.strict_chronology
            ):
                raise AuctionStateChronologyError(
                    f"Out-of-order snapshot for {symbol}: "
                    f"{snapshot_time} < {last_result.snapshot_time}"
                )
            if snapshot_time.date() != last_result.snapshot_time.date():
                self.reset(symbol)
                last_result = None
            elif snapshot_time == last_result.snapshot_time:
                if self._last_input_hashes.get(symbol) == input_hash:
                    return last_result
                if self.config.state.strict_chronology:
                    raise AuctionStateChronologyError(
                        f"Conflicting duplicate snapshot for {symbol} @ {snapshot_time}"
                    )

        history = tuple(self._history[symbol])
        evidence = self.evidence_builder.build(
            snapshot,
            history=history,
            equity_ref=equity_ref,
        )

        state_evaluation = self.state_engine.evaluate(evidence)
        boundary_evaluation = self.boundary_engine.evaluate(
            evidence,
            state_evaluation.state,
        )
        candidates = self.setup_engine.evaluate(
            evidence,
            state_evaluation.state,
            boundary_evaluation.episode,
            state_diagnostics=state_evaluation.diagnostics,
            closed_episode=boundary_evaluation.closed_episode,
        )
        ledger_records = self.opportunity_ledger.update(
            symbol,
            snapshot_time,
            candidates,
            boundary_episode=boundary_evaluation.episode,
            closed_episode=boundary_evaluation.closed_episode,
        )
        manager = self.setup_manager.evaluate(
            symbol,
            snapshot_time,
            ledger_records,
        )

        selected = None
        selected_record = None
        if manager.selected_candidate_id:
            for record in self.opportunity_ledger.active_eligible(symbol):
                candidate = record.candidates.get(manager.selected_candidate_id)
                if candidate is not None and candidate.eligibility.value == "ELIGIBLE":
                    selected = candidate
                    selected_record = record
                    break

        local_decision = self.decision_engine.evaluate(
            manager=manager,
            selected=selected,
        )

        selected_key = None
        selection_recorded = False
        if manager.action is ManagerAction.SELECT and selected is not None:
            selected_key = selected.opportunity_key
            # Selection is an Auction fact, but it must be idempotent. Without
            # signal-level consumption the same live opportunity may remain
            # selected for several snapshots. Keep the first local selection
            # time so rotation diagnostics remain causal and stable.
            if selected_record is not None and selected_record.selected_time is None:
                self.opportunity_ledger.mark_selected(
                    selected.opportunity_key,
                    snapshot_time,
                    selected.candidate_id,
                )
                selection_recorded = True

        result = AuctionEngineResult(
            symbol=symbol,
            snapshot_time=snapshot_time,
            evidence=evidence,
            auction_state=state_evaluation.state,
            boundary_episode=boundary_evaluation.episode,
            candidates=candidates,
            manager_decision=manager,
            local_decision=local_decision,
            advisor_decisions=(),
            final_decision=None,
            diagnostics={
                "phase": "PURE_ANALYTICAL_CORE",
                "decision_scope": "LOCAL_AUCTION_ONLY",
                "signal_lifecycle_applied": False,
                "active_signal_context_applied": False,
                "advisor_context_applied": False,
                "opportunity_consumption_applied": False,
                "proposed_state": state_evaluation.proposed_state.value,
                "transitioned": state_evaluation.transitioned,
                "state_flags": state_evaluation.flags,
                "state_diagnostics": state_evaluation.diagnostics,
                "boundary_transitioned": boundary_evaluation.transitioned,
                "boundary_previous_status": (
                    boundary_evaluation.previous_status.value
                    if boundary_evaluation.previous_status is not None
                    else None
                ),
                "boundary_diagnostics": boundary_evaluation.diagnostics,
                "boundary_closed_episode": (
                    boundary_evaluation.closed_episode.to_storage_dict(
                        exclude_none=False
                    )
                    if boundary_evaluation.closed_episode is not None
                    else None
                ),
                "candidate_count": len(candidates),
                "unique_opportunity_count": len(
                    {item.opportunity_key for item in candidates}
                ),
                "candidate_ids": [item.candidate_id for item in candidates],
                "opportunity_keys": [item.opportunity_key for item in candidates],
                "candidate_families": [item.family.value for item in candidates],
                "candidate_eligibilities": [
                    item.eligibility.value for item in candidates
                ],
                "ledger_records": list(
                    self.opportunity_ledger.record_dicts(symbol)
                ),
                "ledger_events": [
                    item.to_dict()
                    for item in self.opportunity_ledger.events(symbol)
                ],
                "local_selected_opportunity_key": selected_key,
                "local_selection_recorded_now": selection_recorded,
                "local_action": local_decision.action.value,
            },
        )
        self._history[symbol].append(_compact_history_evidence(evidence))
        self._last_results[symbol] = result
        self._last_input_hashes[symbol] = input_hash
        return result

    def export_incremental_state(self, symbol: str) -> Dict[str, Any]:
        """Export compact state carried by the enriched snapshot.

        Unlike the legacy restart checkpoint, this payload excludes the full
        last result and the ever-growing ledger event history. The chronological
        snapshot rows are the history; only state required by the next Auction
        evaluation is carried forward.
        """
        key = str(symbol or "").strip().upper()
        if not key:
            raise ValueError("Auction incremental-state symbol is required")
        payload = {
            "state_schema": "AUCTION_INCREMENTAL_STATE_V1",
            "engine_name": self.config.engine.engine_name,
            "engine_version": self.config.engine.engine_version,
            "config_version": self.config.engine.config_version,
            "config_hash": self.config.stable_hash(),
            "symbol": key,
            "history": list(self._history.get(key, ())),
            "state_memory": self.state_engine._memory.get(key),
            "boundary_current": self.boundary_engine._current.get(key),
            "boundary_last_time": self.boundary_engine._last_time.get(key),
            "boundary_sequences": {
                seq_key: value
                for seq_key, value in self.boundary_engine._sequences.items()
                if seq_key[0] == key
            },
            "boundary_last_terminal": self.boundary_engine._last_terminal.get(key),
            "setup_initiation": {
                item_key: value
                for item_key, value in self.setup_engine._initiation.items()
                if value.symbol == key
            },
            "setup_failed": {
                item_key: value
                for item_key, value in self.setup_engine._failed.items()
                if value.symbol == key
            },
            "setup_emitted_once": set(self.setup_engine._emitted_once),
            "setup_completed": set(self.setup_engine._completed),
            "setup_last_time": self.setup_engine._last_time.get(key),
            "ledger_records": {
                item_key: value
                for item_key, value in self.opportunity_ledger._records.items()
                if value.symbol == key
            },
            "ledger_last_day": self.opportunity_ledger._last_day.get(key),
        }
        return encode_checkpoint_value(payload)

    def restore_incremental_state(
        self,
        symbol: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Restore state produced by :meth:`export_incremental_state`."""
        key = str(symbol or "").strip().upper()
        decoded = decode_checkpoint_value(dict(payload))
        if not isinstance(decoded, dict):
            raise ValueError("Auction incremental state root must be a mapping")
        expected = {
            "state_schema": "AUCTION_INCREMENTAL_STATE_V1",
            "engine_name": self.config.engine.engine_name,
            "engine_version": self.config.engine.engine_version,
            "config_version": self.config.engine.config_version,
            "config_hash": self.config.stable_hash(),
            "symbol": key,
        }
        for field, value in expected.items():
            if decoded.get(field) != value:
                raise ValueError(
                    f"Auction snapshot-state mismatch for {field}: "
                    f"{decoded.get(field)!r} != {value!r}"
                )

        self.reset()
        history = decoded.get("history") or []
        self._history[key] = deque(
            history,
            maxlen=self.config.state.history_bars,
        )
        state_memory = decoded.get("state_memory")
        if state_memory is not None:
            self.state_engine._memory[key] = state_memory
        boundary_current = decoded.get("boundary_current")
        if boundary_current is not None:
            self.boundary_engine._current[key] = boundary_current
        boundary_last_time = decoded.get("boundary_last_time")
        if boundary_last_time is not None:
            self.boundary_engine._last_time[key] = boundary_last_time
        self.boundary_engine._sequences = dict(
            decoded.get("boundary_sequences") or {}
        )
        boundary_last_terminal = decoded.get("boundary_last_terminal")
        if boundary_last_terminal is not None:
            self.boundary_engine._last_terminal[key] = boundary_last_terminal
        self.setup_engine._initiation = dict(
            decoded.get("setup_initiation") or {}
        )
        self.setup_engine._failed = dict(decoded.get("setup_failed") or {})
        self.setup_engine._emitted_once = set(
            decoded.get("setup_emitted_once") or set()
        )
        self.setup_engine._completed = set(
            decoded.get("setup_completed") or set()
        )
        setup_last_time = decoded.get("setup_last_time")
        if setup_last_time is not None:
            self.setup_engine._last_time[key] = setup_last_time
        self.opportunity_ledger._records = dict(
            decoded.get("ledger_records") or {}
        )
        ledger_last_day = decoded.get("ledger_last_day")
        if ledger_last_day is not None:
            self.opportunity_ledger._last_day[key] = ledger_last_day

    def export_checkpoint(self, symbol: str) -> Dict[str, Any]:
        """Export complete recoverable state for one symbol.

        The live runner uses one AuctionEngine instance per symbol, so these
        component collections contain only that symbol's current-day state.
        """
        key = str(symbol or "").strip().upper()
        if not key:
            raise ValueError("Checkpoint symbol is required")
        payload = {
            "checkpoint_schema": "AUCTION_ENGINE_STATE_V1",
            "engine_name": self.config.engine.engine_name,
            "engine_version": self.config.engine.engine_version,
            "config_version": self.config.engine.config_version,
            "symbol": key,
            "history": list(self._history.get(key, ())),
            "last_result": self._last_results.get(key),
            "last_input_hash": self._last_input_hashes.get(key),
            "state_memory": dict(self.state_engine._memory),
            "boundary_current": dict(self.boundary_engine._current),
            "boundary_last_time": dict(self.boundary_engine._last_time),
            "boundary_sequences": dict(self.boundary_engine._sequences),
            "boundary_last_terminal": dict(self.boundary_engine._last_terminal),
            "setup_initiation": dict(self.setup_engine._initiation),
            "setup_failed": dict(self.setup_engine._failed),
            "setup_emitted_once": set(self.setup_engine._emitted_once),
            "setup_completed": set(self.setup_engine._completed),
            "setup_last_time": dict(self.setup_engine._last_time),
            "ledger_records": dict(self.opportunity_ledger._records),
            "ledger_events": list(self.opportunity_ledger._events),
            "ledger_last_day": dict(self.opportunity_ledger._last_day),
        }
        return encode_checkpoint_value(payload)

    def restore_checkpoint(self, symbol: str, payload: Mapping[str, Any]) -> None:
        """Restore a checkpoint previously produced by ``export_checkpoint``."""
        key = str(symbol or "").strip().upper()
        decoded = decode_checkpoint_value(dict(payload))
        if not isinstance(decoded, dict):
            raise ValueError("Auction checkpoint root must be a mapping")
        expected = {
            "checkpoint_schema": "AUCTION_ENGINE_STATE_V1",
            "engine_name": self.config.engine.engine_name,
            "engine_version": self.config.engine.engine_version,
            "config_version": self.config.engine.config_version,
            "symbol": key,
        }
        for field, value in expected.items():
            if decoded.get(field) != value:
                raise ValueError(
                    f"Auction checkpoint mismatch for {field}: "
                    f"{decoded.get(field)!r} != {value!r}"
                )

        self.reset()
        history = decoded.get("history") or []
        self._history[key] = deque(history, maxlen=self.config.state.history_bars)
        if decoded.get("last_result") is not None:
            self._last_results[key] = decoded["last_result"]
        if decoded.get("last_input_hash"):
            self._last_input_hashes[key] = decoded["last_input_hash"]

        self.state_engine._memory = dict(decoded.get("state_memory") or {})
        self.boundary_engine._current = dict(decoded.get("boundary_current") or {})
        self.boundary_engine._last_time = dict(decoded.get("boundary_last_time") or {})
        self.boundary_engine._sequences = dict(decoded.get("boundary_sequences") or {})
        self.boundary_engine._last_terminal = dict(decoded.get("boundary_last_terminal") or {})
        self.setup_engine._initiation = dict(decoded.get("setup_initiation") or {})
        self.setup_engine._failed = dict(decoded.get("setup_failed") or {})
        self.setup_engine._emitted_once = set(decoded.get("setup_emitted_once") or set())
        self.setup_engine._completed = set(decoded.get("setup_completed") or set())
        self.setup_engine._last_time = dict(decoded.get("setup_last_time") or {})
        self.opportunity_ledger._records = dict(decoded.get("ledger_records") or {})
        self.opportunity_ledger._events = list(decoded.get("ledger_events") or [])
        self.opportunity_ledger._last_day = dict(decoded.get("ledger_last_day") or {})

    def evaluate_many(
        self,
        snapshots: Iterable[Any],
        *,
        equity_refs: Optional[Mapping[str, str]] = None,
    ) -> List[AuctionEngineResult]:
        results: List[AuctionEngineResult] = []
        refs = equity_refs or {}
        for snapshot in snapshots:
            symbol, _ = _snapshot_identity(snapshot)
            results.append(
                self.evaluate_snapshot(snapshot, equity_ref=refs.get(symbol))
            )
        return results


def _compact_history_evidence(evidence: EvidenceSnapshot) -> _HistoryEvidence:
    """Keep only the three historical facts read by EvidenceBuilder."""
    return _HistoryEvidence(
        close=evidence.close,
        bar=evidence.bar,
        trend=_HistoryTrend(
            hma_order=evidence.trend.hma_order,
            hma_spread_atr=evidence.trend.hma_spread_atr,
        ),
    )


def _snapshot_identity(snapshot: Any) -> tuple[str, datetime]:
    if isinstance(snapshot, Mapping):
        symbol = snapshot.get("symbol")
        snapshot_time = snapshot.get("snapshot_time")
    elif hasattr(snapshot, "model_dump"):
        data = snapshot.model_dump(mode="python", include={"symbol", "snapshot_time"})
        symbol = data.get("symbol")
        snapshot_time = data.get("snapshot_time")
    else:
        data = getattr(snapshot, "data", None)
        symbol = getattr(snapshot, "symbol", None)
        snapshot_time = getattr(snapshot, "snapshot_time", None)
        if isinstance(data, Mapping):
            symbol = symbol or data.get("symbol")
            snapshot_time = snapshot_time or data.get("snapshot_time")

    key = str(symbol or "").strip().upper()
    if not key:
        raise ValueError("Snapshot symbol is required")
    if isinstance(snapshot_time, str):
        snapshot_time = datetime.fromisoformat(snapshot_time)
    if not isinstance(snapshot_time, datetime):
        raise ValueError(f"Snapshot timestamp is required for {key}")
    return key, snapshot_time


def _snapshot_content_hash(snapshot: Any) -> str:
    if isinstance(snapshot, Mapping):
        data = dict(snapshot)
    elif hasattr(snapshot, "model_dump"):
        data = snapshot.model_dump(mode="json")
    else:
        raw = getattr(snapshot, "data", None)
        if isinstance(raw, Mapping):
            data = dict(raw)
            data.setdefault("symbol", getattr(snapshot, "symbol", None))
            data.setdefault("snapshot_time", getattr(snapshot, "snapshot_time", None))
        else:
            data = {k: v for k, v in vars(snapshot).items() if not k.startswith("_")}
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = ["AuctionEngine"]
