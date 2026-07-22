"""Build the pure Auction Engine section embedded in each market snapshot.

The immediately previous enriched snapshot is the only continuity source.
There is no Auction service checkpoint, opportunity-table persistence, Advisor
call, active-signal lookup, or signal lifecycle side effect in this module.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import time
from typing import Any, Dict, Mapping, Optional

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG
from configs.snapshot_config import SNAPSHOT_CONFIG
from services.auction_engine.engine import AuctionEngine


def enrich_snapshot_with_auction(
    snapshot_payload: Mapping[str, Any],
    *,
    previous_payload: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the Auction block for one newly assembled snapshot.

    Normal operation restores bounded incremental state from the immediately
    previous same-day snapshot and advances it with the current snapshot.
    When no compatible previous Auction block exists, the engine starts a fresh
    local sequence. This is intentional for the clean base implementation; the
    development database can be regenerated chronologically from the first bar.
    """
    symbol = str(snapshot_payload.get("symbol") or "").strip().upper()
    snapshot_time = _as_datetime(snapshot_payload.get("snapshot_time"))
    if not symbol:
        raise ValueError("Auction snapshot symbol is required")
    if snapshot_time is None:
        raise ValueError("Auction snapshot_time is required")

    cfg = SNAPSHOT_CONFIG.auction
    engine = AuctionEngine(AUCTION_ENGINE_CONFIG)
    previous_time = _payload_time(previous_payload)
    continuity_mode = "COLD_START"
    started = time.perf_counter()

    try:
        if previous_auction_continuity_usable(
            previous_payload,
            symbol=symbol,
            current_time=snapshot_time,
        ):
            previous_auction = dict(previous_payload.get("auction") or {})
            engine.restore_incremental_state(
                symbol,
                previous_auction["continuity"],
            )
            continuity_mode = "INCREMENTAL_PREVIOUS_SNAPSHOT"

        result = engine.evaluate_snapshot(
            _auction_input(snapshot_payload),
            equity_ref=symbol,
        )
        continuity = engine.export_incremental_state(symbol)
        continuity_raw = json.dumps(
            continuity,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")

        diagnostics: Dict[str, Any] = {
            "decision_scope": "LOCAL_AUCTION_ONLY",
            "signal_lifecycle_applied": False,
            "advisor_context_applied": False,
            "active_signal_context_applied": False,
            "history_count": len(engine._history.get(symbol, ())),
            "candidate_count": len(result.candidates),
            "local_action": result.local_decision.action.value,
            "manager_action": result.manager_decision.action.value,
            "selected_candidate_id": result.manager_decision.selected_candidate_id,
            "selected_opportunity_key": (
                result.local_decision.selected_candidate.opportunity_key
                if result.local_decision.selected_candidate is not None
                else None
            ),
        }
        if cfg.include_diagnostics:
            diagnostics.update(
                {
                    "proposed_state": result.diagnostics.get("proposed_state"),
                    "transitioned": result.diagnostics.get("transitioned"),
                    "boundary_transitioned": result.diagnostics.get(
                        "boundary_transitioned"
                    ),
                    "state_flags": result.diagnostics.get("state_flags") or {},
                    "state_diagnostics": (
                        result.diagnostics.get("state_diagnostics") or {}
                    ),
                    "boundary_diagnostics": (
                        result.diagnostics.get("boundary_diagnostics") or {}
                    ),
                }
            )

        return {
            "status": "OK",
            "continuity_mode": continuity_mode,
            "engine_name": AUCTION_ENGINE_CONFIG.engine.engine_name,
            "engine_version": AUCTION_ENGINE_CONFIG.engine.engine_version,
            "config_version": AUCTION_ENGINE_CONFIG.engine.config_version,
            "config_hash": AUCTION_ENGINE_CONFIG.stable_hash(),
            "previous_snapshot_time": previous_time,
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "continuity_bytes": len(continuity_raw),
            "continuity_hash": hashlib.sha256(continuity_raw).hexdigest(),
            "continuity": continuity,
            "state": result.auction_state.to_storage_dict(exclude_none=False),
            "boundary": (
                result.boundary_episode.to_storage_dict(exclude_none=False)
                if result.boundary_episode is not None
                else None
            ),
            "candidates": [
                candidate.to_storage_dict(exclude_none=False)
                for candidate in result.candidates
            ],
            "opportunities": [
                _compact_opportunity(record)
                for record in engine.opportunity_ledger.records(symbol)
            ],
            "manager_decision": result.manager_decision.to_storage_dict(
                exclude_none=False
            ),
            "local_decision": result.local_decision.to_storage_dict(
                exclude_none=False
            ),
            "diagnostics": diagnostics,
            "error": None,
        }
    except Exception as exc:
        if not cfg.fail_open:
            raise
        return {
            "status": "ERROR",
            "continuity_mode": continuity_mode,
            "engine_name": AUCTION_ENGINE_CONFIG.engine.engine_name,
            "engine_version": AUCTION_ENGINE_CONFIG.engine.engine_version,
            "config_version": AUCTION_ENGINE_CONFIG.engine.config_version,
            "config_hash": AUCTION_ENGINE_CONFIG.stable_hash(),
            "previous_snapshot_time": previous_time,
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "continuity_bytes": 0,
            "continuity_hash": None,
            "continuity": {},
            "state": {},
            "boundary": None,
            "candidates": [],
            "opportunities": [],
            "manager_decision": {},
            "local_decision": {},
            "diagnostics": {
                "decision_scope": "LOCAL_AUCTION_ONLY",
                "signal_lifecycle_applied": False,
                "advisor_context_applied": False,
                "active_signal_context_applied": False,
            },
            "error": f"{type(exc).__name__}: {exc}",
        }


def previous_auction_continuity_usable(
    payload: Optional[Mapping[str, Any]],
    *,
    symbol: str,
    current_time: datetime,
) -> bool:
    """Return whether the previous snapshot can advance this Auction sequence."""
    if not isinstance(payload, Mapping) or not payload:
        return False
    if str(payload.get("symbol") or "").strip().upper() != symbol:
        return False

    previous_time = _payload_time(payload)
    if previous_time is None:
        return False
    previous_time, comparable_current = _align_datetimes(
        previous_time,
        current_time,
    )
    if previous_time.date() != comparable_current.date():
        return False
    gap_minutes = (comparable_current - previous_time).total_seconds() / 60.0
    if gap_minutes <= 0:
        return False
    if gap_minutes > SNAPSHOT_CONFIG.auction.max_incremental_gap_minutes:
        return False

    auction = payload.get("auction")
    if not isinstance(auction, Mapping) or auction.get("status") != "OK":
        return False
    if auction.get("engine_name") != AUCTION_ENGINE_CONFIG.engine.engine_name:
        return False
    if auction.get("engine_version") != AUCTION_ENGINE_CONFIG.engine.engine_version:
        return False
    if auction.get("config_version") != AUCTION_ENGINE_CONFIG.engine.config_version:
        return False
    if auction.get("config_hash") != AUCTION_ENGINE_CONFIG.stable_hash():
        return False
    continuity = auction.get("continuity")
    return bool(
        isinstance(continuity, Mapping)
        and continuity.get("state_schema") == "AUCTION_INCREMENTAL_STATE_V2"
    )


def _auction_input(payload: Mapping[str, Any]) -> Dict[str, Any]:
    data = dict(payload)
    data.pop("auction", None)
    return data


def _compact_opportunity(record: Any) -> Dict[str, Any]:
    candidate = record.primary_candidate
    return {
        "opportunity_key": record.opportunity_key,
        "symbol": record.symbol,
        "trading_day": record.trading_day.isoformat(),
        "side": record.side.value,
        "boundary_event_key": record.boundary_event_key,
        "lifecycle_state": record.lifecycle_state,
        "first_observed_time": record.first_observed_time.isoformat(),
        "last_observed_time": record.last_observed_time.isoformat(),
        "eligible_time": (
            record.eligible_time.isoformat() if record.eligible_time else None
        ),
        "selected_time": (
            record.selected_time.isoformat() if record.selected_time else None
        ),
        "selected_candidate_id": record.selected_candidate_id,
        "primary_candidate_id": candidate.candidate_id,
        "primary_family": candidate.family.value,
        "primary_subtype": candidate.subtype,
        "primary_role": candidate.candidate_role.value,
        "primary_eligibility": candidate.eligibility.value,
        "candidate_ids": list(record.candidate_ids),
        "supporting_candidate_ids": list(record.supporting_candidate_ids),
        "reason_codes": list(record.reason_codes),
    }


def _align_datetimes(left: datetime, right: datetime) -> tuple[datetime, datetime]:
    if left.tzinfo is None and right.tzinfo is not None:
        left = left.replace(tzinfo=right.tzinfo)
    elif left.tzinfo is not None and right.tzinfo is None:
        right = right.replace(tzinfo=left.tzinfo)
    return left, right


def _payload_time(payload: Optional[Mapping[str, Any]]) -> Optional[datetime]:
    if not isinstance(payload, Mapping):
        return None
    return _as_datetime(payload.get("snapshot_time"))


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


__all__ = [
    "enrich_snapshot_with_auction",
    "previous_auction_continuity_usable",
]
