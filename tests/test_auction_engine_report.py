#!/usr/bin/env python3
"""Read-only Phase-3B auction-state, boundary and setup-candidate report.

This program reads chronological snapshots from the configured ``backtest``
database, runs the Common Evidence Ledger, Auction State Engine, Unified
Boundary Engine and observation-only Setup Candidate Engine, and writes state,
episode and candidate timelines. It does not update processed flags, call the
Advisor/Setup Manager, create signals, write setup state or invoke TradeManager.

Examples
--------

    python tests/test_auction_engine_report.py --date 2026-07-20
    python tests/test_auction_engine_report.py --date 2026-07-20 --symbols TORNTPHARM,HAVELLS
    python tests/test_auction_engine_report.py --start-date 2026-07-16 --end-date 2026-07-20
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# Allow direct execution from tests/ while keeping project-root imports.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG  # noqa: E402
from services.auction_engine.engine import AuctionEngine  # noqa: E402


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate read-only auction-state, boundary and setup-candidate reports")
    parser.add_argument("--date", help="Single trading date YYYY-MM-DD")
    parser.add_argument("--start-date", help="First trading date YYYY-MM-DD")
    parser.add_argument("--end-date", help="Last trading date YYYY-MM-DD (inclusive)")
    parser.add_argument("--symbols", help="Comma-separated symbol filter")
    parser.add_argument("--limit", type=int, default=0, help="Optional maximum snapshot rows")
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "reports" / "auction_engine"),
        help="Report output directory",
    )
    parser.add_argument(
        "--transitions-only",
        action="store_true",
        help="Write only state-transition rows to the timeline CSV",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Record malformed rows and continue instead of stopping",
    )
    return parser.parse_args(argv)


def resolve_dates(args: argparse.Namespace) -> Tuple[date, date]:
    if args.date:
        day = date.fromisoformat(args.date)
        return day, day
    if not args.start_date:
        raise SystemExit("Provide --date or --start-date")
    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date) if args.end_date else start
    if end < start:
        raise SystemExit("--end-date cannot precede --start-date")
    return start, end


SNAPSHOT_FETCH_BATCH_SIZE = 250


def _decode_snapshot_data(value: Any) -> Dict[str, Any]:
    """Decode snapshot JSON without invoking SQLAlchemy's ORM JSON processor.

    Report replays may read several thousand large snapshot payloads.  Selecting
    the ORM entity makes SQLAlchemy eagerly deserialize every JSON document
    inside ``Query.all()``, which can look hung and causes a large transient
    memory spike.  The report instead selects the JSON column as text and
    decodes one row at a time.
    """
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode("utf-8")
    if isinstance(value, str):
        decoded = json.loads(value)
        if decoded is None:
            return {}
        if not isinstance(decoded, Mapping):
            raise ValueError(f"Snapshot data JSON must decode to an object, got {type(decoded).__name__}")
        return dict(decoded)
    raise TypeError(f"Unsupported snapshot data type: {type(value)!r}")


def load_snapshot_rows(
    start_day: date,
    end_day: date,
    *,
    symbols: Sequence[str] = (),
    limit: int = 0,
) -> List[Dict[str, Any]]:
    # Database imports stay inside this function so importing this report never
    # opens a connection during ordinary unit-test discovery.
    from sqlalchemy import Text, cast
    from database.database import get_trades_db
    from models.trade_models import Snapshot as SnapshotORM

    start_dt = datetime.combine(start_day, time.min)
    end_exclusive = datetime.combine(end_day + timedelta(days=1), time.min)
    rows: List[Dict[str, Any]] = []

    with get_trades_db() as db:
        # Use a narrow projection.  Casting JSON to text bypasses SQLAlchemy's
        # eager JSON result processor; ``yield_per`` keeps the DB result
        # streaming in small batches rather than materialising all rows first.
        query = (
            db.query(
                SnapshotORM.symbol.label("symbol"),
                SnapshotORM.snapshot_time.label("snapshot_time"),
                SnapshotORM.ltp.label("ltp"),
                cast(SnapshotORM.data, Text).label("data_json"),
            )
            .filter(SnapshotORM.snapshot_time >= start_dt)
            .filter(SnapshotORM.snapshot_time < end_exclusive)
        )
        if symbols:
            query = query.filter(SnapshotORM.symbol.in_(list(symbols)))
        query = query.order_by(SnapshotORM.snapshot_time.asc(), SnapshotORM.symbol.asc())
        if limit > 0:
            query = query.limit(limit)

        records = query.execution_options(stream_results=True).yield_per(SNAPSHOT_FETCH_BATCH_SIZE)
        for record in records:
            try:
                payload = _decode_snapshot_data(record.data_json)
            except Exception as exc:
                raise ValueError(
                    f"Unable to decode snapshot JSON for {record.symbol} @ {record.snapshot_time}: {exc}"
                ) from exc
            payload.setdefault("symbol", record.symbol)
            payload.setdefault("snapshot_time", record.snapshot_time)
            if payload.get("close") is None and record.ltp is not None:
                payload["close"] = float(record.ltp)
            rows.append(payload)

    return rows


def result_row(result: Any, run_id: str) -> Dict[str, Any]:
    evidence = result.evidence
    state = result.auction_state
    boundary = evidence.boundary
    episode = result.boundary_episode
    boundary_diag = result.diagnostics.get("boundary_diagnostics", {}) or {}
    episode_diag = episode.diagnostics if episode is not None else {}
    closed_episode = result.diagnostics.get("boundary_closed_episode") or {}
    closed_episode_diag = closed_episode.get("diagnostics") or {}
    closed_frozen_range = closed_episode.get("frozen_range") or {}
    last_terminal = boundary_diag.get("last_terminal") or {}
    channels = {channel.name: channel.score for channel in state.confidence_channels}
    source_structure = evidence.raw_facts.get("source_structure", {}) if evidence.raw_facts else {}
    state_flags = result.diagnostics.get("state_flags", {}) or {}
    return {
        "run_id": run_id,
        "symbol": result.symbol,
        "snapshot_time": result.snapshot_time.isoformat(sep=" "),
        "close": evidence.close,
        "atr": evidence.atr,
        "data_quality": evidence.data_quality.status.value,
        "data_coverage": evidence.data_quality.coverage,
        "previous_state": state.previous_state.value,
        "proposed_state": result.diagnostics.get("proposed_state"),
        "current_state": state.current_state.value,
        "transitioned": bool(result.diagnostics.get("transitioned")),
        "entered_at": state.entered_at.isoformat(sep=" "),
        "transition_time": state.transition_time.isoformat(sep=" "),
        "state_age_bars": _nested(result.diagnostics, "state_diagnostics", "state_age_bars"),
        "pending_state": _nested(result.diagnostics, "state_diagnostics", "pending_state"),
        "pending_bars": _nested(result.diagnostics, "state_diagnostics", "pending_bars"),
        "pending_required_bars": _nested(result.diagnostics, "state_diagnostics", "pending_required_bars"),
        "transition_policy_reason": _nested(result.diagnostics, "state_diagnostics", "transition_policy_reason"),
        "trend_direction": evidence.trend.direction.value,
        "trend_candidate_side": _nested(result.diagnostics, "state_diagnostics", "trend_candidate_side"),
        "trend_candidate_bars": _nested(result.diagnostics, "state_diagnostics", "trend_candidate_bars"),
        "trend_candidate_ready": _nested(result.diagnostics, "state_diagnostics", "trend_candidate_ready"),
        "trend_neutral_candidate_bars": _nested(result.diagnostics, "state_diagnostics", "trend_neutral_candidate_bars"),
        "trend_neutralisation_ready": state_flags.get("trend_neutralisation_ready"),
        "established_trend_side": _nested(result.diagnostics, "state_diagnostics", "established_trend_side"),
        "trend_onset_time": _nested(result.diagnostics, "state_diagnostics", "trend_onset_time"),
        "trend_anchor_price": _nested(result.diagnostics, "state_diagnostics", "trend_anchor_price"),
        "trend_extreme_price": _nested(result.diagnostics, "state_diagnostics", "trend_extreme_price"),
        "trend_protection_level": _nested(result.diagnostics, "state_diagnostics", "trend_protection_level"),
        "trend_protection_side": _nested(result.diagnostics, "state_diagnostics", "trend_protection_side"),
        "trend_protection_source": _nested(result.diagnostics, "state_diagnostics", "trend_protection_source"),
        "trend_protection_time": _nested(result.diagnostics, "state_diagnostics", "trend_protection_time"),
        "trend_protection_version": _nested(result.diagnostics, "state_diagnostics", "trend_protection_version"),
        "trend_protection_episode_key": _nested(result.diagnostics, "state_diagnostics", "trend_protection_episode_key"),
        "leg_anchor_time": _nested(result.diagnostics, "state_diagnostics", "leg_anchor_time"),
        "leg_anchor_price": _nested(result.diagnostics, "state_diagnostics", "leg_anchor_price"),
        "leg_extreme_price": _nested(result.diagnostics, "state_diagnostics", "leg_extreme_price"),
        "leg_age_bars": _nested(result.diagnostics, "state_diagnostics", "leg_age_bars"),
        "leg_no_progress_bars": _nested(result.diagnostics, "state_diagnostics", "leg_no_progress_bars"),
        "leg_maturity_consumed": _nested(result.diagnostics, "state_diagnostics", "leg_maturity_consumed"),
        "leg_maturity_onset_time": _nested(result.diagnostics, "state_diagnostics", "leg_maturity_onset_time"),
        "leg_maturity_extreme_price": _nested(result.diagnostics, "state_diagnostics", "leg_maturity_extreme_price"),
        "trend_efficiency": evidence.trend.directional_efficiency,
        "trend_retained_structure": evidence.trend.retained_structure,
        "hma_order": evidence.trend.hma_order,
        "hma_spread_atr": evidence.trend.hma_spread_atr,
        "vwap_side": evidence.trend.vwap_side,
        "vwap_distance_atr": evidence.trend.vwap_distance_atr,
        "bar_direction": evidence.bar.direction.value,
        "bar_move_atr": evidence.bar.move_atr,
        "bar_body_fraction": evidence.bar.body_fraction,
        "bar_close_position": evidence.bar.close_position,
        "pa_followthrough": evidence.price_action.followthrough,
        "pa_rejection": evidence.price_action.rejection,
        "compression": evidence.compression.compressed,
        "compression_observed": state_flags.get("compression_observed"),
        "compression_ready": state_flags.get("compression_ready"),
        "compression_candidate_bars": _nested(result.diagnostics, "state_diagnostics", "compression_candidate_bars"),
        "compression_episode_key": _nested(result.diagnostics, "state_diagnostics", "compression_episode_key"),
        "compression_onset_time": _nested(result.diagnostics, "state_diagnostics", "compression_onset_time"),
        "compression_box_low": _nested(result.diagnostics, "state_diagnostics", "compression_box_low"),
        "compression_box_high": _nested(result.diagnostics, "state_diagnostics", "compression_box_high"),
        "compression_range_width_atr": evidence.compression.range_width_atr,
        "compression_contraction_ratio": evidence.compression.contraction_ratio,
        # The observed boundary is the latest source snapshot. The episode
        # boundary is the immutable side/price owned by the active event. Keep
        # them separate so a new observation can never appear to mutate an old
        # episode in the report.
        "observed_boundary_id": boundary.boundary_id if boundary else None,
        "observed_boundary_side": boundary.boundary_side.value if boundary else None,
        "observed_boundary_source": boundary.boundary_source if boundary else None,
        "observed_boundary_price": boundary.boundary_price if boundary else None,
        "observed_boundary_offset_atr": boundary.current_offset_atr if boundary else None,
        "observed_range_id": boundary.range_id if boundary else None,
        "observed_range_version": boundary.range_version if boundary else None,
        "observed_range_low": boundary.range_low if boundary else None,
        "observed_range_high": boundary.range_high if boundary else None,
        "episode_boundary_id": episode.boundary_id if episode else None,
        "episode_boundary_side": episode.boundary_side.value if episode else None,
        "episode_boundary_source": episode.boundary_source if episode else None,
        "episode_boundary_price": episode.boundary_price if episode else None,
        # Legacy names now mean episode-owned values, never observed values.
        "boundary_id": episode.boundary_id if episode else None,
        "boundary_side": episode.boundary_side.value if episode else None,
        "boundary_source": episode.boundary_source if episode else None,
        "boundary_price": episode.boundary_price if episode else None,
        "boundary_offset_atr": episode.current_offset_atr if episode else None,
        "boundary_episode_present": episode is not None,
        "boundary_transitioned": bool(result.diagnostics.get("boundary_transitioned")),
        "boundary_previous_status": result.diagnostics.get("boundary_previous_status"),
        "boundary_episode_status": episode.status.value if episode else None,
        "boundary_resolution": episode.resolution.value if episode else None,
        "boundary_resolution_basis": episode_diag.get("failure_resolution_basis") if episode else None,
        "boundary_event_key": episode.event_key if episode else None,
        "boundary_structural_key": episode.structural_key if episode else None,
        "boundary_attempt_id": episode.attempt_id if episode else None,
        "boundary_episode_sequence": episode.episode_sequence if episode else None,
        "boundary_event_time": episode.event_time.isoformat(sep=" ") if episode else None,
        "boundary_first_seen_time": episode.first_seen_time.isoformat(sep=" ") if episode else None,
        "boundary_attempt_time": episode.attempt_time.isoformat(sep=" ") if episode and episode.attempt_time else None,
        "boundary_first_outside_close_time": episode.first_outside_close_time.isoformat(sep=" ") if episode and episode.first_outside_close_time else None,
        "boundary_first_reentry_time": episode.first_reentry_time.isoformat(sep=" ") if episode and episode.first_reentry_time else None,
        "boundary_accepted_time": episode.accepted_time.isoformat(sep=" ") if episode and episode.accepted_time else None,
        "boundary_failed_time": episode.failed_time.isoformat(sep=" ") if episode and episode.failed_time else None,
        "boundary_resolution_time": episode_diag.get("resolution_time") if episode else None,
        "boundary_terminal_time": episode_diag.get("terminal_time") if episode else None,
        "boundary_archive_time": episode_diag.get("archive_time") if episode else None,
        "boundary_archive_reason": episode_diag.get("archive_reason") if episode else None,
        "boundary_post_terminal_protection_bars": episode_diag.get("post_terminal_protection_bars") if episode else None,
        "boundary_terminal": episode.terminal if episode else None,
        "boundary_terminal_reason": episode.terminal_reason if episode else None,
        "boundary_superseded": episode.superseded if episode else None,
        "boundary_superseded_by": episode.superseded_by if episode else None,
        "frozen_range_id": episode.frozen_range.range_id if episode else None,
        "frozen_range_version": episode.frozen_range.range_version if episode else None,
        "frozen_range_source": episode.frozen_range.source if episode else None,
        "frozen_range_low": episode.frozen_range.low if episode else None,
        "frozen_range_high": episode.frozen_range.high if episode else None,
        "frozen_range_start_time": episode.frozen_range.start_time.isoformat(sep=" ") if episode else None,
        "frozen_range_end_time": episode.frozen_range.end_time.isoformat(sep=" ") if episode and episode.frozen_range.end_time else None,
        "frozen_range_frozen_at": episode.frozen_range.frozen_at.isoformat(sep=" ") if episode else None,
        "frozen_range_basis": episode.frozen_range.basis if episode else None,
        "range_frozen": episode_diag.get("range_frozen") if episode else None,
        "episode_current_offset_atr": episode.current_offset_atr if episode else None,
        "episode_max_outside_excursion_atr": episode.max_outside_excursion_atr if episode else None,
        "episode_max_close_outside_atr": episode.max_close_outside_atr if episode else None,
        "episode_total_outside_closes": episode.total_outside_closes if episode else None,
        "episode_consecutive_outside_closes": episode.consecutive_outside_closes if episode else None,
        "episode_consecutive_acceptance_closes": episode_diag.get("consecutive_acceptance_closes") if episode else None,
        "episode_consecutive_inside_closes": episode.consecutive_inside_closes if episode else None,
        "episode_reentry_depth_atr": episode.reentry_depth_atr if episode else None,
        "episode_failure_followthrough_atr": episode_diag.get("failure_followthrough_atr") if episode else None,
        "episode_retest_detected": episode.retest_detected if episode else None,
        "episode_reset_inside_closes": episode.reset_inside_closes if episode else None,
        "boundary_transition_reason": boundary_diag.get("transition_reason"),
        "boundary_reason_codes": "|".join(episode.reason_codes) if episode else "",
        "boundary_acceptance_evidence": "|".join(fact.code for fact in episode.acceptance_evidence) if episode else "",
        "boundary_failure_evidence": "|".join(fact.code for fact in episode.failure_evidence) if episode else "",
        "boundary_handoff_occurred": boundary_diag.get("handoff_occurred"),
        "boundary_handoff_reason": boundary_diag.get("handoff_reason"),
        "closed_boundary_event_key": closed_episode.get("event_key"),
        "closed_boundary_structural_key": closed_episode.get("structural_key"),
        "closed_boundary_attempt_id": closed_episode.get("attempt_id"),
        "closed_boundary_episode_sequence": closed_episode.get("episode_sequence"),
        "closed_boundary_episode_status": closed_episode.get("status"),
        "closed_boundary_resolution": closed_episode.get("resolution"),
        "closed_boundary_resolution_basis": closed_episode_diag.get("failure_resolution_basis"),
        "closed_boundary_terminal": closed_episode.get("terminal"),
        "closed_boundary_terminal_reason": closed_episode.get("terminal_reason"),
        "closed_boundary_superseded": closed_episode.get("superseded"),
        "closed_boundary_superseded_by": closed_episode.get("superseded_by"),
        "closed_boundary_event_time": closed_episode.get("event_time"),
        "closed_boundary_first_seen_time": closed_episode.get("first_seen_time"),
        "closed_boundary_attempt_time": closed_episode.get("attempt_time"),
        "closed_boundary_first_outside_close_time": closed_episode.get("first_outside_close_time"),
        "closed_boundary_first_reentry_time": closed_episode.get("first_reentry_time"),
        "closed_boundary_accepted_time": closed_episode.get("accepted_time"),
        "closed_boundary_failed_time": closed_episode.get("failed_time"),
        "closed_boundary_resolution_time": closed_episode_diag.get("resolution_time"),
        "closed_boundary_terminal_time": closed_episode_diag.get("terminal_time"),
        "closed_boundary_archive_time": closed_episode_diag.get("archive_time"),
        "closed_boundary_archive_reason": closed_episode_diag.get("archive_reason"),
        "closed_boundary_post_terminal_protection_bars": closed_episode_diag.get("post_terminal_protection_bars"),
        "closed_episode_boundary_id": closed_episode.get("boundary_id"),
        "closed_episode_boundary_side": closed_episode.get("boundary_side"),
        "closed_episode_boundary_source": closed_episode.get("boundary_source"),
        "closed_episode_boundary_price": closed_episode.get("boundary_price"),
        "closed_frozen_range_id": closed_frozen_range.get("range_id"),
        "closed_frozen_range_version": closed_frozen_range.get("range_version"),
        "closed_frozen_range_source": closed_frozen_range.get("source"),
        "closed_frozen_range_low": closed_frozen_range.get("low"),
        "closed_frozen_range_high": closed_frozen_range.get("high"),
        "closed_frozen_range_start_time": closed_frozen_range.get("start_time"),
        "closed_frozen_range_end_time": closed_frozen_range.get("end_time"),
        "closed_frozen_range_frozen_at": closed_frozen_range.get("frozen_at"),
        "closed_frozen_range_basis": closed_frozen_range.get("basis"),
        "closed_episode_max_outside_excursion_atr": closed_episode.get("max_outside_excursion_atr"),
        "closed_episode_max_close_outside_atr": closed_episode.get("max_close_outside_atr"),
        "closed_episode_reentry_depth_atr": closed_episode.get("reentry_depth_atr"),
        "closed_episode_failure_followthrough_atr": closed_episode_diag.get("failure_followthrough_atr"),
        "closed_episode_retest_detected": closed_episode.get("retest_detected"),
        "closed_boundary_episode_json": (
            json.dumps(closed_episode, sort_keys=True, default=str)
            if closed_episode else ""
        ),
        "last_terminal_event_key": last_terminal.get("event_key"),
        "last_terminal_status": last_terminal.get("status"),
        "last_terminal_resolution": last_terminal.get("resolution"),
        "last_terminal_resolution_basis": last_terminal.get("resolution_basis"),
        "last_terminal_reason": last_terminal.get("terminal_reason"),
        "last_terminal_time": last_terminal.get("terminal_time"),
        "last_terminal_resolution_time": last_terminal.get("resolution_time"),
        "last_terminal_archive_reason": last_terminal.get("archive_reason"),
        "last_terminal_archive_time": last_terminal.get("archive_time"),
        "last_terminal_post_protection_bars": last_terminal.get("post_terminal_protection_bars"),
        "extension": evidence.extension.extended,
        "mature_extension": evidence.extension.mature,
        "stock_day_extension": evidence.extension.extended,
        "stock_day_mature_extension": evidence.extension.mature,
        "current_leg_mature": state_flags.get("current_leg_mature"),
        "current_leg_distance_atr": state_flags.get("current_leg_distance_atr"),
        "current_leg_current_distance_atr": state_flags.get("current_leg_current_distance_atr"),
        "current_leg_retracement_atr": state_flags.get("current_leg_retracement_atr"),
        "current_leg_retracement_fraction": state_flags.get("current_leg_retracement_fraction"),
        "current_leg_progress_or_rejection": state_flags.get("current_leg_progress_or_rejection"),
        "move_from_anchor_atr": evidence.extension.move_from_anchor_atr,
        "move_from_day_anchor_atr": evidence.extension.move_from_anchor_atr,
        "progress_decay": evidence.extension.progress_decay,
        "pullback_episode_key": _nested(result.diagnostics, "state_diagnostics", "pullback_episode_key"),
        "pullback_onset_time": _nested(result.diagnostics, "state_diagnostics", "pullback_onset_time"),
        "pullback_age_bars": _nested(result.diagnostics, "state_diagnostics", "pullback_age_bars"),
        "pullback_depth_atr": _nested(result.diagnostics, "state_diagnostics", "pullback_depth_atr"),
        "recompression_episode_key": _nested(result.diagnostics, "state_diagnostics", "recompression_episode_key"),
        "recompression_onset_time": _nested(result.diagnostics, "state_diagnostics", "recompression_onset_time"),
        "recompression_age_bars": _nested(result.diagnostics, "state_diagnostics", "recompression_age_bars"),
        "reacceleration_episode_key": _nested(result.diagnostics, "state_diagnostics", "reacceleration_episode_key"),
        "reacceleration_onset_time": _nested(result.diagnostics, "state_diagnostics", "reacceleration_onset_time"),
        "reacceleration_age_bars": _nested(result.diagnostics, "state_diagnostics", "reacceleration_age_bars"),
        "failure_episode_key": _nested(result.diagnostics, "state_diagnostics", "failure_episode_key"),
        "failure_watch_onset": _nested(result.diagnostics, "state_diagnostics", "failure_watch_onset"),
        "failure_watch_bars": _nested(result.diagnostics, "state_diagnostics", "failure_watch_bars"),
        "failure_side": _nested(result.diagnostics, "state_diagnostics", "failure_side"),
        "failure_level": _nested(result.diagnostics, "state_diagnostics", "failure_level"),
        "failure_level_source": _nested(result.diagnostics, "state_diagnostics", "failure_level_source"),
        "failure_level_time": _nested(result.diagnostics, "state_diagnostics", "failure_level_time"),
        "failure_level_version": _nested(result.diagnostics, "state_diagnostics", "failure_level_version"),
        "failure_level_episode_key": _nested(result.diagnostics, "state_diagnostics", "failure_level_episode_key"),
        "close_distance_beyond_level_atr": _nested(result.diagnostics, "state_diagnostics", "failure_close_distance_beyond_level_atr"),
        "failure_level_breach_bars": _nested(result.diagnostics, "state_diagnostics", "failure_level_breach_bars"),
        "failure_structure_loss_bars": _nested(result.diagnostics, "state_diagnostics", "failure_structure_loss_bars"),
        "local_structure_weakening_bars": _nested(result.diagnostics, "state_diagnostics", "local_structure_weakening_bars"),
        "structure_loss_distance_to_protection_atr": _nested(result.diagnostics, "state_diagnostics", "structure_loss_distance_to_protection_atr"),
        "structure_loss_near_protection": _nested(result.diagnostics, "state_diagnostics", "structure_loss_near_protection"),
        "structure_loss_directional_corroboration": _nested(result.diagnostics, "state_diagnostics", "structure_loss_directional_corroboration"),
        "structure_loss_value_migration_corroboration": _nested(result.diagnostics, "state_diagnostics", "structure_loss_value_migration_corroboration"),
        "structure_loss_confirmation_blockers": "|".join(_nested(result.diagnostics, "state_diagnostics", "structure_loss_confirmation_blockers") or []),
        "failure_confirmation_reason": _nested(result.diagnostics, "state_diagnostics", "failure_confirmation_reason"),
        "last_failure_confirmation_reason": _nested(result.diagnostics, "state_diagnostics", "last_failure_confirmation_reason"),
        "last_failure_confirmation_time": _nested(result.diagnostics, "state_diagnostics", "last_failure_confirmation_time"),
        "failure_watch_reason_codes": "|".join(_nested(result.diagnostics, "state_diagnostics", "failure_watch_reason_codes") or []),
        "failure_watch_reset_reason": _nested(result.diagnostics, "state_diagnostics", "failure_watch_reset_reason"),
        "failure_watch_expired": _nested(result.diagnostics, "state_diagnostics", "failure_watch_expired"),
        "trend_failure_age_bars": _nested(result.diagnostics, "state_diagnostics", "trend_failure_age_bars"),
        "trend_failure_expired": _nested(result.diagnostics, "state_diagnostics", "trend_failure_expired"),
        "failure_terminal_key": _nested(result.diagnostics, "state_diagnostics", "last_failure_terminal_key"),
        "failure_terminal_reason": _nested(result.diagnostics, "state_diagnostics", "last_failure_terminal_reason"),
        "failure_terminal_time": _nested(result.diagnostics, "state_diagnostics", "last_failure_terminal_time"),
        "trend_recovery_bars": _nested(result.diagnostics, "state_diagnostics", "trend_recovery_bars"),
        "reversal_confirmation_bars": _nested(result.diagnostics, "state_diagnostics", "reversal_confirmation_bars"),
        "reversal_side": _nested(result.diagnostics, "state_diagnostics", "reversal_side"),
        "reversal_onset_time": _nested(result.diagnostics, "state_diagnostics", "reversal_onset_time"),
        "source_raw_state": source_structure.get("raw_state"),
        "source_raw_side": source_structure.get("raw_side"),
        "source_accepted_range_id": source_structure.get("accepted_range_id"),
        "confidence_data_quality": channels.get("data_quality"),
        "confidence_balance_compression": channels.get("balance_compression"),
        "confidence_fresh_expansion": channels.get("fresh_expansion"),
        "confidence_trend": channels.get("trend"),
        "confidence_extension_maturity": channels.get("extension_maturity"),
        "confidence_chaotic_rotation": channels.get("chaotic_rotation"),
        "chaos_local_hma_flips": (state_flags.get("local_flip_counts") or {}).get("hma"),
        "chaos_local_vwap_flips": (state_flags.get("local_flip_counts") or {}).get("vwap"),
        "chaos_local_structure_flips": (state_flags.get("local_flip_counts") or {}).get("structure"),
        "chaos_local_bar_flips": state_flags.get("bar_flip_count"),
        "chaos_independent_flip_channels": state_flags.get("independent_flip_channels"),
        "chaos_cumulative_day_flip_count": state_flags.get("cumulative_day_flip_count"),
        "compression_reason_codes": "|".join(evidence.compression.reason_codes),
        "extension_supporting_facts": "|".join(fact.code for fact in evidence.extension.supporting_facts),
        "extension_contradicting_facts": "|".join(fact.code for fact in evidence.extension.contradicting_facts),
        "supporting_evidence": "|".join(fact.code for fact in state.supporting_evidence),
        "contradicting_evidence": "|".join(fact.code for fact in state.contradicting_evidence),
        "state_reason_codes": "|".join(state.reason_codes),
        "evidence_reason_codes": "|".join(evidence.reason_codes),
        "candidate_count": len(result.candidates),
        "candidate_ids": "|".join(item.candidate_id for item in result.candidates),
        "candidate_families": "|".join(item.family.value for item in result.candidates),
        "candidate_subtypes": "|".join(item.subtype for item in result.candidates),
        "candidate_sides": "|".join(item.side.value for item in result.candidates),
        "candidate_eligibilities": "|".join(item.eligibility.value for item in result.candidates),
        "eligible_candidate_count": sum(1 for item in result.candidates if item.eligibility.value == "ELIGIBLE"),
        "watch_candidate_count": sum(1 for item in result.candidates if item.eligibility.value == "WATCH"),
        "ineligible_candidate_count": sum(1 for item in result.candidates if item.eligibility.value in {"INELIGIBLE", "EXPIRED"}),
        "local_action": result.local_decision.action.value,
        "report_only": True,
        "config_version": evidence.config_version,
    }


def candidate_observation_rows(result: Any, run_id: str) -> List[Dict[str, Any]]:
    """Flatten SetupCandidate contracts into one row per candidate observation."""

    rows: List[Dict[str, Any]] = []
    episode = result.boundary_episode
    for candidate in result.candidates:
        channels = {item.name: item.score for item in candidate.confidence_channels}
        rows.append({
            "run_id": run_id,
            "symbol": candidate.symbol,
            "snapshot_time": candidate.snapshot_time.isoformat(sep=" "),
            "candidate_id": candidate.candidate_id,
            "candidate_time": candidate.candidate_time.isoformat(sep=" "),
            "event_key": candidate.event_key,
            "event_time": candidate.event_time.isoformat(sep=" "),
            "family": candidate.family.value,
            "subtype": candidate.subtype,
            "candidate_role": candidate.candidate_role.value,
            "side": candidate.side.value,
            "opportunity_key": candidate.opportunity_key,
            "boundary_thesis_key": candidate.boundary_thesis_key,
            "support_group_key": candidate.support_group_key,
            "eligibility": candidate.eligibility.value,
            "terminal": candidate.terminal,
            "valid_until": candidate.valid_until.isoformat(sep=" ") if candidate.valid_until else None,
            "entry_price": candidate.entry_price,
            "entry_atr": result.evidence.atr,
            "stop_anchor_price": candidate.stop_anchor_price,
            "stop_anchor_type": candidate.stop_anchor_type,
            "target_basis": candidate.target_basis,
            "target_reference_price": candidate.target_reference_price,
            "room_points": candidate.room_points,
            "room_atr": candidate.room_atr,
            "room_pct": candidate.room_pct,
            "reward_model": candidate.diagnostics.get("reward_model"),
            "assumed_target_hard_gate": candidate.diagnostics.get("assumed_target_hard_gate"),
            "distance_from_boundary_atr": candidate.diagnostics.get("distance_from_boundary_atr"),
            "nearest_actual_barrier_type": candidate.diagnostics.get("nearest_actual_barrier_type"),
            "nearest_actual_barrier_price": candidate.diagnostics.get("nearest_actual_barrier_price"),
            "nearest_actual_barrier_distance_atr": candidate.diagnostics.get("nearest_actual_barrier_distance_atr"),
            "nearest_actual_barrier_distance_pct": candidate.diagnostics.get("nearest_actual_barrier_distance_pct"),
            "measured_move_reference_price": candidate.diagnostics.get("measured_move_reference_price"),
            "measured_move_reference_is_diagnostic_only": candidate.diagnostics.get("measured_move_reference_is_diagnostic_only"),
            "measured_move_distance_from_entry_atr": candidate.diagnostics.get("measured_move_distance_from_entry_atr"),
            "measured_move_distance_from_entry_pct": candidate.diagnostics.get("measured_move_distance_from_entry_pct"),
            "failed_opposite_range_edge_price": candidate.diagnostics.get("failed_opposite_range_edge_price"),
            "failed_opposite_range_edge_room_atr": candidate.diagnostics.get("failed_opposite_range_edge_room_atr"),
            "failed_opposite_range_edge_room_pct": candidate.diagnostics.get("failed_opposite_range_edge_room_pct"),
            "failed_range_progress_fraction": candidate.diagnostics.get("failed_range_progress_fraction"),
            "failed_midpoint_price": candidate.diagnostics.get("failed_midpoint_price"),
            "failed_midpoint_distance_atr": candidate.diagnostics.get("failed_midpoint_distance_atr"),
            "failed_vwap_price": candidate.diagnostics.get("failed_vwap_price"),
            "failed_vwap_distance_atr": candidate.diagnostics.get("failed_vwap_distance_atr"),
            "entry_distance_atr": candidate.entry_distance_atr,
            "freshness_minutes": candidate.freshness_minutes,
            "first_move_consumed": candidate.first_move_consumed,
            "auction_state": candidate.auction_state.value,
            "blockers": "|".join(candidate.blockers),
            "reason_codes": "|".join(candidate.reason_codes),
            "supporting_evidence": "|".join(item.code for item in candidate.supporting_evidence),
            "opposing_evidence": "|".join(item.code for item in candidate.opposing_evidence),
            "confidence_structural_validity": channels.get("structural_validity"),
            "confidence_opportunity": channels.get("opportunity"),
            "confidence_freshness": channels.get("freshness"),
            # Legacy boundary columns now represent the immutable source event.
            "boundary_status": candidate.source_boundary_status.value,
            "boundary_resolution": candidate.source_boundary_resolution.value,
            "boundary_resolution_basis": candidate.source_boundary_resolution_basis,
            "source_boundary_event_key": candidate.source_boundary_event_key,
            "source_boundary_status": candidate.source_boundary_status.value,
            "source_boundary_resolution": candidate.source_boundary_resolution.value,
            "source_boundary_resolution_basis": candidate.source_boundary_resolution_basis,
            "source_boundary_id": candidate.source_boundary_id,
            "source_boundary_side": candidate.source_boundary_side.value,
            "source_boundary_source": candidate.source_boundary_source,
            "source_boundary_price": candidate.source_boundary_price,
            "source_frozen_range_id": candidate.source_frozen_range_id,
            "source_frozen_range_version": candidate.source_frozen_range_version,
            "source_frozen_range_low": candidate.source_frozen_range_low,
            "source_frozen_range_high": candidate.source_frozen_range_high,
            "current_boundary_event_key": episode.event_key if episode else None,
            "current_boundary_status": episode.status.value if episode else None,
            "current_boundary_resolution": episode.resolution.value if episode else None,
            "current_boundary_side": episode.boundary_side.value if episode else None,
            "current_boundary_price": episode.boundary_price if episode else None,
            "candidate_stage": candidate.diagnostics.get("candidate_stage"),
            "resolution_basis": candidate.diagnostics.get("resolution_basis"),
            "followthrough_confirmed": candidate.diagnostics.get("followthrough_confirmed"),
            "same_side_established_trend": candidate.diagnostics.get("same_side_established_trend"),
            "pause_context": candidate.diagnostics.get("pause_context"),
            "candidate_diagnostics_json": json.dumps(candidate.diagnostics, sort_keys=True, default=str),
            "local_action": result.local_decision.action.value,
            "advisor_evaluated": False,
            "setup_manager_selected": bool(
                result.local_decision.selected_candidate is not None
                and result.local_decision.selected_candidate.candidate_id
                == candidate.candidate_id
            ),
            "signal_created": False,
            "config_version": candidate.config_version,
        })
    return rows


def build_candidate_lifecycle(
    candidate_rows: Sequence[Mapping[str, Any]],
    snapshots: Sequence[Mapping[str, Any]],
    horizons: Sequence[int],
) -> List[Dict[str, Any]]:
    """Collapse candidate observations and append hindsight-only outcomes.

    Outcome columns are computed after the causal replay. They are never fed
    back into EvidenceSnapshot, auction state, boundary or candidate policy.
    """

    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in candidate_rows:
        grouped[str(row["candidate_id"])].append(row)

    by_symbol: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for snapshot in snapshots:
        by_symbol[str(snapshot.get("symbol") or "").upper()].append(snapshot)
    for values in by_symbol.values():
        values.sort(key=lambda item: _as_datetime(item.get("snapshot_time")) or datetime.min)

    rank = {"ELIGIBLE": 5, "WATCH": 4, "INELIGIBLE": 3, "EXPIRED": 2, "SUPERSEDED": 1, "CONSUMED": 0}
    lifecycle: List[Dict[str, Any]] = []
    for candidate_id, values in grouped.items():
        ordered = sorted(values, key=lambda item: str(item.get("snapshot_time") or ""))
        first = ordered[0]
        last = ordered[-1]
        progression: List[str] = []
        for row in ordered:
            status = str(row.get("eligibility") or "")
            if status and (not progression or progression[-1] != status):
                progression.append(status)
        eligible_rows = [row for row in ordered if row.get("eligibility") == "ELIGIBLE"]
        selected = eligible_rows[0] if eligible_rows else first
        best = max((str(row.get("eligibility") or "") for row in ordered), key=lambda item: rank.get(item, -1))
        all_blockers = sorted({code for row in ordered for code in str(row.get("blockers") or "").split("|") if code})
        summary = {
            "run_id": first.get("run_id"),
            "candidate_id": candidate_id,
            "symbol": first.get("symbol"),
            "family": first.get("family"),
            "subtype": first.get("subtype"),
            "candidate_role": first.get("candidate_role"),
            "side": first.get("side"),
            "opportunity_key": first.get("opportunity_key"),
            "boundary_thesis_key": first.get("boundary_thesis_key"),
            "support_group_key": first.get("support_group_key"),
            "event_key": first.get("event_key"),
            "event_time": first.get("event_time"),
            "candidate_time": first.get("candidate_time"),
            "first_observation_time": first.get("snapshot_time"),
            "last_observation_time": last.get("snapshot_time"),
            "observation_rows": len(ordered),
            "eligibility_progression": ">".join(progression),
            "first_eligibility": first.get("eligibility"),
            "best_eligibility": best,
            "final_eligibility": last.get("eligibility"),
            "eligible_time": eligible_rows[0].get("snapshot_time") if eligible_rows else None,
            "terminal": bool(last.get("terminal")),
            "entry_time_for_outcomes": selected.get("snapshot_time"),
            "entry_price_for_outcomes": selected.get("entry_price"),
            "entry_atr": selected.get("entry_atr"),
            "entry_auction_state": selected.get("auction_state"),
            "stop_anchor_price": selected.get("stop_anchor_price"),
            "target_basis": selected.get("target_basis"),
            "target_reference_price": selected.get("target_reference_price"),
            "room_atr": selected.get("room_atr"),
            "room_pct": selected.get("room_pct"),
            "entry_distance_atr": selected.get("entry_distance_atr"),
            "freshness_minutes": selected.get("freshness_minutes"),
            "first_move_consumed": selected.get("first_move_consumed"),
            "first_blockers": first.get("blockers"),
            "final_blockers": last.get("blockers"),
            "all_blockers": "|".join(all_blockers),
            "resolution_basis": selected.get("resolution_basis") or selected.get("boundary_resolution_basis"),
            "source_boundary_status": first.get("source_boundary_status"),
            "source_boundary_resolution": first.get("source_boundary_resolution"),
            "source_boundary_resolution_basis": first.get("source_boundary_resolution_basis"),
            "source_boundary_side": first.get("source_boundary_side"),
            "source_boundary_price": first.get("source_boundary_price"),
            "source_frozen_range_id": first.get("source_frozen_range_id"),
            "source_frozen_range_version": first.get("source_frozen_range_version"),
            "config_version": first.get("config_version"),
        }
        summary.update(_candidate_outcomes(summary, by_symbol.get(str(first.get("symbol") or "").upper(), []), horizons))
        lifecycle.append(summary)
    return sorted(lifecycle, key=lambda row: (str(row.get("symbol")), str(row.get("candidate_time")), str(row.get("family"))))


def build_opportunity_lifecycle(
    candidate_lifecycle_rows: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Collapse candidate aliases into one operational opportunity group.

    This is diagnostic grouping only.  It does not select a signal or change
    local candidate eligibility; it prevents accepted/continuation/initiation
    aliases from being counted as independent same-direction evidence.
    """

    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in candidate_lifecycle_rows:
        key = str(row.get("opportunity_key") or "")
        if key:
            grouped[key].append(row)

    rank = {
        "ELIGIBLE": 5,
        "WATCH": 4,
        "INELIGIBLE": 3,
        "EXPIRED": 2,
        "SUPERSEDED": 1,
        "CONSUMED": 0,
    }
    role_priority = {
        "EARLY_INITIATION": 1,
        "ACCEPTED_RESOLUTION_ENTRY": 2,
        "FAILED_RESOLUTION_ENTRY": 2,
        "CONTINUATION_INTERPRETATION": 3,
    }
    rows: List[Dict[str, Any]] = []
    for opportunity_key, values in grouped.items():
        ordered = sorted(
            values,
            key=lambda item: (
                str(item.get("candidate_time") or ""),
                role_priority.get(str(item.get("candidate_role") or ""), 99),
                str(item.get("candidate_id") or ""),
            ),
        )
        eligible = [item for item in ordered if item.get("best_eligibility") == "ELIGIBLE"]
        representative_pool = eligible or ordered
        representative = min(
            representative_pool,
            key=lambda item: (
                role_priority.get(str(item.get("candidate_role") or ""), 99),
                str(item.get("eligible_time") or item.get("candidate_time") or ""),
            ),
        )
        best = max(
            (str(item.get("best_eligibility") or "") for item in ordered),
            key=lambda item: rank.get(item, -1),
        )
        rows.append({
            "run_id": representative.get("run_id"),
            "opportunity_key": opportunity_key,
            "boundary_thesis_key": representative.get("boundary_thesis_key"),
            "support_group_key": representative.get("support_group_key"),
            "symbol": representative.get("symbol"),
            "side": representative.get("side"),
            "event_key": representative.get("event_key"),
            "event_time": representative.get("event_time"),
            "first_candidate_time": ordered[0].get("candidate_time"),
            "last_candidate_time": ordered[-1].get("candidate_time"),
            "candidate_record_count": len(ordered),
            "candidate_ids": "|".join(str(item.get("candidate_id") or "") for item in ordered),
            "families": "|".join(dict.fromkeys(str(item.get("family") or "") for item in ordered)),
            "subtypes": "|".join(dict.fromkeys(str(item.get("subtype") or "") for item in ordered)),
            "candidate_roles": "|".join(dict.fromkeys(str(item.get("candidate_role") or "") for item in ordered)),
            "best_eligibility": best,
            "eligible_candidate_count": len(eligible),
            "representative_candidate_id": representative.get("candidate_id"),
            "representative_candidate_role": representative.get("candidate_role"),
            "representative_family": representative.get("family"),
            "representative_subtype": representative.get("subtype"),
            "representative_eligible_time": representative.get("eligible_time"),
            "alias_double_counting_prevented": len(ordered) > 1,
            "source_boundary_status": representative.get("source_boundary_status"),
            "source_boundary_resolution": representative.get("source_boundary_resolution"),
            "source_boundary_resolution_basis": representative.get("source_boundary_resolution_basis"),
            "config_version": representative.get("config_version"),
        })
    return sorted(rows, key=lambda row: (str(row.get("symbol")), str(row.get("first_candidate_time")), str(row.get("opportunity_key"))))


def _candidate_outcomes(
    candidate: Mapping[str, Any],
    snapshots: Sequence[Mapping[str, Any]],
    horizons: Sequence[int],
) -> Dict[str, Any]:
    entry_time = _as_datetime(candidate.get("entry_time_for_outcomes"))
    try:
        entry_price = float(candidate.get("entry_price_for_outcomes"))
    except (TypeError, ValueError):
        return {}
    side = str(candidate.get("side") or "").upper()
    if entry_time is None or side not in {"BUY", "SELL"}:
        return {}
    # Candidate entry is the completed signal-close price. Outcome highs/lows
    # therefore start with the next completed bar; using the candidate bar would
    # leak pre-entry intrabar movement into MFE/MAE.
    future = [item for item in snapshots if (_as_datetime(item.get("snapshot_time")) or datetime.min) > entry_time]
    if not future:
        return {}

    def bar_values(item: Mapping[str, Any]) -> Tuple[float, float, float, datetime]:
        bar = item.get("bar") if isinstance(item.get("bar"), Mapping) else {}
        close = float(bar.get("close") if bar.get("close") is not None else item.get("close"))
        high = float(bar.get("high") if bar.get("high") is not None else close)
        low = float(bar.get("low") if bar.get("low") is not None else close)
        ts = _as_datetime(item.get("snapshot_time")) or entry_time
        return high, low, close, ts

    def metrics(window: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        favorable: List[Tuple[float, datetime]] = []
        adverse: List[Tuple[float, datetime]] = []
        for item in window:
            high, low, _, ts = bar_values(item)
            if side == "BUY":
                favorable.append((max(0.0, high - entry_price), ts))
                adverse.append((max(0.0, entry_price - low), ts))
            else:
                favorable.append((max(0.0, entry_price - low), ts))
                adverse.append((max(0.0, high - entry_price), ts))
        mfe_points, mfe_time = max(favorable, key=lambda item: item[0])
        mae_points, mae_time = max(adverse, key=lambda item: item[0])
        return {
            "mfe_points": mfe_points,
            "mae_points": mae_points,
            "mfe_pct": mfe_points / entry_price,
            "mae_pct": mae_points / entry_price,
            "time_to_mfe_minutes": round((mfe_time - entry_time).total_seconds() / 60.0, 4),
            "time_to_mae_minutes": round((mae_time - entry_time).total_seconds() / 60.0, 4),
        }

    out: Dict[str, Any] = {
        "outcome_entry_basis": "CANDIDATE_CLOSE_NEXT_BAR_PATH",
        "outcome_first_bar_time": (_as_datetime(future[0].get("snapshot_time")) or entry_time).isoformat(sep=" "),
    }
    for horizon in sorted({int(x) for x in horizons if int(x) > 0}):
        values = metrics(future[:horizon])
        for key, value in values.items():
            out[f"{key}_{horizon}bars"] = value
    full = metrics(future)
    for key, value in full.items():
        out[f"{key}_full_session"] = value
    _, _, eod_close, eod_time = bar_values(future[-1])
    signed_points = eod_close - entry_price if side == "BUY" else entry_price - eod_close
    out["eod_close"] = eod_close
    out["eod_time"] = eod_time.isoformat(sep=" ")
    out["eod_move_points"] = signed_points
    out["eod_move_pct"] = signed_points / entry_price
    return out


def _closed_episode_projection(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a synthetic lifecycle row for a same-snapshot closed episode."""

    event_key = row.get("closed_boundary_event_key")
    if not event_key or event_key == row.get("boundary_event_key"):
        return None
    projected = dict(row)
    mapping = {
        "boundary_event_key": "closed_boundary_event_key",
        "boundary_structural_key": "closed_boundary_structural_key",
        "boundary_attempt_id": "closed_boundary_attempt_id",
        "boundary_episode_sequence": "closed_boundary_episode_sequence",
        "boundary_episode_status": "closed_boundary_episode_status",
        "boundary_resolution": "closed_boundary_resolution",
        "boundary_resolution_basis": "closed_boundary_resolution_basis",
        "boundary_terminal": "closed_boundary_terminal",
        "boundary_terminal_reason": "closed_boundary_terminal_reason",
        "boundary_superseded": "closed_boundary_superseded",
        "boundary_superseded_by": "closed_boundary_superseded_by",
        "boundary_event_time": "closed_boundary_event_time",
        "boundary_first_seen_time": "closed_boundary_first_seen_time",
        "boundary_attempt_time": "closed_boundary_attempt_time",
        "boundary_first_outside_close_time": "closed_boundary_first_outside_close_time",
        "boundary_first_reentry_time": "closed_boundary_first_reentry_time",
        "boundary_accepted_time": "closed_boundary_accepted_time",
        "boundary_failed_time": "closed_boundary_failed_time",
        "boundary_resolution_time": "closed_boundary_resolution_time",
        "boundary_terminal_time": "closed_boundary_terminal_time",
        "boundary_archive_time": "closed_boundary_archive_time",
        "boundary_archive_reason": "closed_boundary_archive_reason",
        "boundary_post_terminal_protection_bars": "closed_boundary_post_terminal_protection_bars",
        "episode_boundary_id": "closed_episode_boundary_id",
        "episode_boundary_side": "closed_episode_boundary_side",
        "episode_boundary_source": "closed_episode_boundary_source",
        "episode_boundary_price": "closed_episode_boundary_price",
        "boundary_id": "closed_episode_boundary_id",
        "boundary_side": "closed_episode_boundary_side",
        "boundary_source": "closed_episode_boundary_source",
        "boundary_price": "closed_episode_boundary_price",
        "frozen_range_id": "closed_frozen_range_id",
        "frozen_range_version": "closed_frozen_range_version",
        "frozen_range_source": "closed_frozen_range_source",
        "frozen_range_low": "closed_frozen_range_low",
        "frozen_range_high": "closed_frozen_range_high",
        "frozen_range_start_time": "closed_frozen_range_start_time",
        "frozen_range_end_time": "closed_frozen_range_end_time",
        "frozen_range_frozen_at": "closed_frozen_range_frozen_at",
        "frozen_range_basis": "closed_frozen_range_basis",
        "episode_max_outside_excursion_atr": "closed_episode_max_outside_excursion_atr",
        "episode_max_close_outside_atr": "closed_episode_max_close_outside_atr",
        "episode_reentry_depth_atr": "closed_episode_reentry_depth_atr",
        "episode_failure_followthrough_atr": "closed_episode_failure_followthrough_atr",
        "episode_retest_detected": "closed_episode_retest_detected",
    }
    for target, source in mapping.items():
        projected[target] = row.get(source)
    projected["boundary_transitioned"] = True
    projected["boundary_transition_reason"] = row.get("boundary_handoff_reason")
    projected["boundary_episode_present"] = True
    projected["lifecycle_row_role"] = "CLOSED_ON_HANDOFF"
    return projected


def _as_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def build_episode_summary(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse the snapshot timeline into one row per immutable event key.

    Same-snapshot supersession/edge-switch handoffs contain both an active new
    episode and a closed old episode. The closed contract is projected into a
    synthetic lifecycle row before grouping, preserving the old terminal event
    without sacrificing the replacement's true attempt timestamp.
    """

    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        key = row.get("boundary_event_key")
        if key:
            grouped[str(key)].append(row)
        closed = _closed_episode_projection(row)
        if closed is not None:
            grouped[str(closed["boundary_event_key"])].append(closed)

    episodes: List[Dict[str, Any]] = []
    for event_key, event_rows in grouped.items():
        ordered = sorted(event_rows, key=lambda item: str(item.get("snapshot_time") or ""))
        first = ordered[0]
        last = ordered[-1]
        statuses: List[str] = []
        for row in ordered:
            status = str(row.get("boundary_episode_status") or "")
            if status and (not statuses or statuses[-1] != status):
                statuses.append(status)
        terminal_rows = [row for row in ordered if row.get("boundary_terminal")]
        terminal = terminal_rows[0] if terminal_rows else last

        attempt_time = next(
            (row.get("boundary_attempt_time") for row in ordered if row.get("boundary_attempt_time")),
            None,
        )
        accepted_time = next(
            (row.get("boundary_accepted_time") for row in ordered if row.get("boundary_accepted_time")),
            None,
        )
        failed_time = next(
            (row.get("boundary_failed_time") for row in ordered if row.get("boundary_failed_time")),
            None,
        )
        # Resolution timing belongs only to ACCEPTED/FAILED outcomes.  An
        # unresolved SUPERSEDED/EXPIRED/STALE episode has terminal timing but no
        # accepted/failed resolution timing.
        resolution_time = accepted_time or failed_time
        terminal_time = next(
            (row.get("boundary_terminal_time") for row in reversed(ordered) if row.get("boundary_terminal_time")),
            None,
        )
        attempt_dt = _as_datetime(attempt_time)
        resolution_dt = _as_datetime(resolution_time)
        terminal_dt = _as_datetime(terminal_time)
        minutes_to_resolution = (
            round((resolution_dt - attempt_dt).total_seconds() / 60.0, 4)
            if attempt_dt is not None and resolution_dt is not None
            else None
        )
        bars_to_resolution = None
        if attempt_dt is not None and resolution_dt is not None:
            attempt_index = next(
                (i for i, row in enumerate(ordered) if (_as_datetime(row.get("snapshot_time")) or datetime.min) >= attempt_dt),
                None,
            )
            resolution_index = next(
                (i for i, row in enumerate(ordered) if (_as_datetime(row.get("snapshot_time")) or datetime.min) >= resolution_dt),
                None,
            )
            if attempt_index is not None and resolution_index is not None:
                bars_to_resolution = max(0, resolution_index - attempt_index)

        minutes_to_terminal = (
            round((terminal_dt - attempt_dt).total_seconds() / 60.0, 4)
            if attempt_dt is not None and terminal_dt is not None
            else None
        )
        bars_to_terminal = None
        if attempt_dt is not None and terminal_dt is not None:
            attempt_index = next(
                (i for i, row in enumerate(ordered) if (_as_datetime(row.get("snapshot_time")) or datetime.min) >= attempt_dt),
                None,
            )
            terminal_index = next(
                (i for i, row in enumerate(ordered) if (_as_datetime(row.get("snapshot_time")) or datetime.min) >= terminal_dt),
                None,
            )
            if attempt_index is not None and terminal_index is not None:
                bars_to_terminal = max(0, terminal_index - attempt_index)

        archive_time = next(
            (row.get("boundary_archive_time") for row in reversed(ordered) if row.get("boundary_archive_time")),
            None,
        )
        archive_reason = next(
            (row.get("boundary_archive_reason") for row in reversed(ordered) if row.get("boundary_archive_reason")),
            None,
        )
        post_terminal_bars = max(
            (int(float(row.get("boundary_post_terminal_protection_bars") or 0)) for row in ordered),
            default=0,
        )

        episodes.append({
            "run_id": first.get("run_id"),
            "symbol": first.get("symbol"),
            "boundary_event_key": event_key,
            "boundary_structural_key": first.get("boundary_structural_key"),
            "boundary_attempt_id": first.get("boundary_attempt_id"),
            "boundary_episode_sequence": first.get("boundary_episode_sequence"),
            "first_snapshot_time": first.get("snapshot_time"),
            "last_snapshot_time": last.get("snapshot_time"),
            "snapshot_rows": len(ordered),
            "status_progression": ">".join(statuses),
            "final_status": terminal.get("boundary_episode_status"),
            "resolution": terminal.get("boundary_resolution"),
            "resolution_basis": next(
                (row.get("boundary_resolution_basis") for row in ordered if row.get("boundary_resolution_basis")),
                None,
            ),
            "terminal": bool(terminal.get("boundary_terminal")),
            "terminal_reason": terminal.get("boundary_terminal_reason"),
            "event_time": first.get("boundary_event_time"),
            "attempt_time": attempt_time,
            "first_outside_close_time": next((row.get("boundary_first_outside_close_time") for row in ordered if row.get("boundary_first_outside_close_time")), None),
            "first_reentry_time": next((row.get("boundary_first_reentry_time") for row in ordered if row.get("boundary_first_reentry_time")), None),
            "accepted_time": accepted_time,
            "failed_time": failed_time,
            "resolution_time": resolution_time,
            "bars_to_resolution": bars_to_resolution,
            "minutes_to_resolution": minutes_to_resolution,
            "terminal_time": terminal_time,
            "bars_to_terminal": bars_to_terminal,
            "minutes_to_terminal": minutes_to_terminal,
            "archive_time": archive_time,
            "archive_reason": archive_reason,
            "post_terminal_protection_bars": post_terminal_bars,
            "episode_boundary_id": first.get("episode_boundary_id"),
            "episode_boundary_side": first.get("episode_boundary_side"),
            "episode_boundary_price": first.get("episode_boundary_price"),
            "episode_boundary_source": first.get("episode_boundary_source"),
            "frozen_range_id": terminal.get("frozen_range_id") or first.get("frozen_range_id"),
            "frozen_range_version": terminal.get("frozen_range_version") or first.get("frozen_range_version"),
            "frozen_range_low": terminal.get("frozen_range_low") or first.get("frozen_range_low"),
            "frozen_range_high": terminal.get("frozen_range_high") or first.get("frozen_range_high"),
            "frozen_range_frozen_at": terminal.get("frozen_range_frozen_at") or first.get("frozen_range_frozen_at"),
            "max_outside_excursion_atr": max(
                (float(row["episode_max_outside_excursion_atr"]) for row in ordered if row.get("episode_max_outside_excursion_atr") not in (None, "")),
                default=0.0,
            ),
            "max_close_outside_atr": max(
                (float(row["episode_max_close_outside_atr"]) for row in ordered if row.get("episode_max_close_outside_atr") not in (None, "")),
                default=0.0,
            ),
            "max_reentry_depth_atr": max(
                (float(row["episode_reentry_depth_atr"]) for row in ordered if row.get("episode_reentry_depth_atr") not in (None, "")),
                default=0.0,
            ),
            "max_failure_followthrough_atr": max(
                (float(row["episode_failure_followthrough_atr"]) for row in ordered if row.get("episode_failure_followthrough_atr") not in (None, "")),
                default=0.0,
            ),
            "retest_detected": any(bool(row.get("episode_retest_detected")) for row in ordered),
            "auction_state_at_first_seen": first.get("current_state"),
            "auction_state_at_attempt": next((row.get("current_state") for row in ordered if row.get("boundary_attempt_time")), None),
            "auction_state_at_terminal": terminal.get("current_state") if terminal.get("boundary_terminal") else None,
            "config_version": first.get("config_version"),
        })
    return sorted(episodes, key=lambda row: (str(row.get("symbol")), str(row.get("first_snapshot_time"))))


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: List[str] = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_summary(
    rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]] = (),
    candidate_lifecycle_rows: Sequence[Mapping[str, Any]] = (),
) -> List[Dict[str, Any]]:
    state_counts: Dict[str, Counter] = defaultdict(Counter)
    transitions: Counter = Counter()
    boundary_counts: Dict[str, Counter] = defaultdict(Counter)
    boundary_transitions: Counter = Counter()
    boundary_resolution_keys: Dict[str, Dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    symbol_rows: Counter = Counter()

    # State counts remain one row per source snapshot. Boundary lifecycle counts
    # additionally include the synthetic closed event emitted during a
    # same-snapshot handoff.
    for row in rows:
        symbol = str(row["symbol"])
        state = str(row["current_state"])
        symbol_rows[symbol] += 1
        state_counts[symbol][state] += 1
        if row.get("transitioned"):
            transitions[(symbol, str(row["previous_state"]), state)] += 1

    boundary_rows: List[Mapping[str, Any]] = list(rows)
    for row in rows:
        closed = _closed_episode_projection(row)
        if closed is not None:
            boundary_rows.append(closed)

    for row in boundary_rows:
        symbol = str(row["symbol"])
        status = row.get("boundary_episode_status")
        if status:
            boundary_counts[symbol][str(status)] += 1
        if row.get("boundary_transitioned") and status:
            boundary_transitions[(
                symbol,
                str(row.get("boundary_previous_status") or "NONE"),
                str(status),
            )] += 1
        resolution = str(row.get("boundary_resolution") or "")
        event_key = str(row.get("boundary_event_key") or "")
        if row.get("boundary_terminal") and resolution and resolution != "UNRESOLVED" and event_key:
            boundary_resolution_keys[symbol][resolution].add(event_key)

    boundary_resolutions: Dict[str, Counter] = defaultdict(Counter)
    for symbol, by_resolution in boundary_resolution_keys.items():
        for resolution, keys in by_resolution.items():
            boundary_resolutions[symbol][resolution] = len(keys)

    summary: List[Dict[str, Any]] = []

    def add_scope(scope: str, total: int, symbols: Sequence[str]) -> None:
        combined_states = Counter()
        combined_boundaries = Counter()
        combined_resolutions = Counter()
        for symbol in symbols:
            combined_states.update(state_counts[symbol])
            combined_boundaries.update(boundary_counts[symbol])
            combined_resolutions.update(boundary_resolutions[symbol])

        for state, count in sorted(combined_states.items()):
            summary.append({
                "symbol": scope, "section": "STATE_COUNT", "state": state,
                "boundary_status": "", "resolution": "", "count": count,
                "total_symbol_snapshots": total,
                "pct_of_symbol_snapshots": round((count / total) * 100.0, 4) if total else 0.0,
                "from_state": "", "to_state": "",
            })
        for status, count in sorted(combined_boundaries.items()):
            summary.append({
                "symbol": scope, "section": "BOUNDARY_STATUS_COUNT", "state": "",
                "boundary_status": status, "resolution": "", "count": count,
                "total_symbol_snapshots": total,
                "pct_of_symbol_snapshots": round((count / total) * 100.0, 4) if total else 0.0,
                "from_state": "", "to_state": "",
            })
        for resolution, count in sorted(combined_resolutions.items()):
            summary.append({
                "symbol": scope, "section": "BOUNDARY_TERMINAL_RESOLUTION", "state": "",
                "boundary_status": "", "resolution": resolution, "count": count,
                "total_symbol_snapshots": total, "pct_of_symbol_snapshots": "",
                "from_state": "", "to_state": "",
            })

    symbols = sorted(symbol_rows)
    add_scope("__ALL__", len(rows), symbols)
    aggregate_state_transitions = Counter(
        (str(row["previous_state"]), str(row["current_state"]))
        for row in rows if row.get("transitioned")
    )
    for (from_state, to_state), count in sorted(aggregate_state_transitions.items()):
        summary.append({
            "symbol": "__ALL__", "section": "TRANSITION_COUNT", "state": "",
            "boundary_status": "", "resolution": "", "count": count,
            "total_symbol_snapshots": len(rows), "pct_of_symbol_snapshots": "",
            "from_state": from_state, "to_state": to_state,
        })
    aggregate_boundary_transitions = Counter(
        (str(row.get("boundary_previous_status") or "NONE"), str(row.get("boundary_episode_status")))
        for row in boundary_rows if row.get("boundary_transitioned") and row.get("boundary_episode_status")
    )
    for (from_state, to_state), count in sorted(aggregate_boundary_transitions.items()):
        summary.append({
            "symbol": "__ALL__", "section": "BOUNDARY_TRANSITION_COUNT", "state": "",
            "boundary_status": "", "resolution": "", "count": count,
            "total_symbol_snapshots": len(rows), "pct_of_symbol_snapshots": "",
            "from_state": from_state, "to_state": to_state,
        })

    for symbol in symbols:
        total = symbol_rows[symbol]
        add_scope(symbol, total, [symbol])
        for (transition_symbol, from_state, to_state), count in sorted(transitions.items()):
            if transition_symbol == symbol:
                summary.append({
                    "symbol": symbol, "section": "TRANSITION_COUNT", "state": "",
                    "boundary_status": "", "resolution": "", "count": count,
                    "total_symbol_snapshots": total, "pct_of_symbol_snapshots": "",
                    "from_state": from_state, "to_state": to_state,
                })
        for (transition_symbol, from_state, to_state), count in sorted(boundary_transitions.items()):
            if transition_symbol == symbol:
                summary.append({
                    "symbol": symbol, "section": "BOUNDARY_TRANSITION_COUNT", "state": "",
                    "boundary_status": "", "resolution": "", "count": count,
                    "total_symbol_snapshots": total, "pct_of_symbol_snapshots": "",
                    "from_state": from_state, "to_state": to_state,
                })

    # Candidate counts are observation counts here; the separate lifecycle file
    # provides one row per stable candidate identity.
    candidate_counter: Counter = Counter()
    candidate_unique: Dict[Tuple[str, str, str, str], set[str]] = defaultdict(set)
    for row in candidate_rows:
        symbol = str(row.get("symbol") or "")
        family = str(row.get("family") or "")
        subtype = str(row.get("subtype") or "")
        eligibility = str(row.get("eligibility") or "")
        candidate_counter[(symbol, family, subtype, eligibility)] += 1
        candidate_unique[(symbol, family, subtype, eligibility)].add(str(row.get("candidate_id") or ""))
    all_candidate_keys = sorted(candidate_counter)
    for symbol, family, subtype, eligibility in all_candidate_keys:
        count = candidate_counter[(symbol, family, subtype, eligibility)]
        unique_count = len(candidate_unique[(symbol, family, subtype, eligibility)])
        summary.append({
            "symbol": symbol,
            "section": "CANDIDATE_OBSERVATION_COUNT",
            "state": "",
            "boundary_status": "",
            "resolution": "",
            "candidate_family": family,
            "candidate_subtype": subtype,
            "candidate_eligibility": eligibility,
            "count": count,
            "unique_candidate_count": unique_count,
            "total_symbol_snapshots": symbol_rows.get(symbol, 0),
            "pct_of_symbol_snapshots": round((count / symbol_rows[symbol]) * 100.0, 4) if symbol_rows.get(symbol) else 0.0,
            "from_state": "",
            "to_state": "",
        })
    aggregate_candidates: Counter = Counter()
    aggregate_unique: Dict[Tuple[str, str, str], set[str]] = defaultdict(set)
    for row in candidate_rows:
        key = (str(row.get("family") or ""), str(row.get("subtype") or ""), str(row.get("eligibility") or ""))
        aggregate_candidates[key] += 1
        aggregate_unique[key].add(str(row.get("candidate_id") or ""))
    for (family, subtype, eligibility), count in sorted(aggregate_candidates.items()):
        summary.append({
            "symbol": "__ALL__",
            "section": "CANDIDATE_OBSERVATION_COUNT",
            "state": "",
            "boundary_status": "",
            "resolution": "",
            "candidate_family": family,
            "candidate_subtype": subtype,
            "candidate_eligibility": eligibility,
            "count": count,
            "unique_candidate_count": len(aggregate_unique[(family, subtype, eligibility)]),
            "total_symbol_snapshots": len(rows),
            "pct_of_symbol_snapshots": round((count / len(rows)) * 100.0, 4) if rows else 0.0,
            "from_state": "",
            "to_state": "",
        })

    # Operational opportunity counts de-duplicate accepted/continuation aliases
    # and the early initiation that belongs to the same boundary attempt/side.
    rank = {
        "ELIGIBLE": 5,
        "WATCH": 4,
        "INELIGIBLE": 3,
        "EXPIRED": 2,
        "SUPERSEDED": 1,
        "CONSUMED": 0,
    }
    opportunity_groups: Dict[Tuple[str, str], List[Mapping[str, Any]]] = defaultdict(list)
    for row in candidate_lifecycle_rows:
        symbol = str(row.get("symbol") or "")
        opportunity_key = str(row.get("opportunity_key") or "")
        if opportunity_key:
            opportunity_groups[(symbol, opportunity_key)].append(row)

    opportunity_counts: Counter = Counter()
    opportunity_candidate_records: Counter = Counter()
    for (symbol, _opportunity_key), values in opportunity_groups.items():
        best = max(
            (str(item.get("best_eligibility") or "") for item in values),
            key=lambda item: rank.get(item, -1),
        )
        opportunity_counts[(symbol, best)] += 1
        opportunity_candidate_records[(symbol, best)] += len(values)

    for (symbol, eligibility), count in sorted(opportunity_counts.items()):
        summary.append({
            "symbol": symbol,
            "section": "OPERATIONAL_OPPORTUNITY_COUNT",
            "state": "",
            "boundary_status": "",
            "resolution": "",
            "candidate_family": "MULTI_INTERPRETATION_GROUP",
            "candidate_subtype": "",
            "candidate_eligibility": eligibility,
            "count": count,
            "unique_candidate_count": opportunity_candidate_records[(symbol, eligibility)],
            "unique_opportunity_count": count,
            "candidate_record_count": opportunity_candidate_records[(symbol, eligibility)],
            "total_symbol_snapshots": symbol_rows.get(symbol, 0),
            "pct_of_symbol_snapshots": "",
            "from_state": "",
            "to_state": "",
        })

    aggregate_opportunities: Counter = Counter()
    aggregate_records: Counter = Counter()
    for (symbol, eligibility), count in opportunity_counts.items():
        aggregate_opportunities[eligibility] += count
        aggregate_records[eligibility] += opportunity_candidate_records[(symbol, eligibility)]
    for eligibility, count in sorted(aggregate_opportunities.items()):
        summary.append({
            "symbol": "__ALL__",
            "section": "OPERATIONAL_OPPORTUNITY_COUNT",
            "state": "",
            "boundary_status": "",
            "resolution": "",
            "candidate_family": "MULTI_INTERPRETATION_GROUP",
            "candidate_subtype": "",
            "candidate_eligibility": eligibility,
            "count": count,
            "unique_candidate_count": aggregate_records[eligibility],
            "unique_opportunity_count": count,
            "candidate_record_count": aggregate_records[eligibility],
            "total_symbol_snapshots": len(rows),
            "pct_of_symbol_snapshots": "",
            "from_state": "",
            "to_state": "",
        })
    return summary


def git_value(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=PROJECT_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip() or "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def database_name() -> str:
    try:
        from config import AppConfig
        from sqlalchemy.engine import make_url

        return str(make_url(AppConfig.SQLALCHEMY_BINDS["trades"]).database or "UNKNOWN")
    except Exception:
        return "UNKNOWN"


def run_report(args: argparse.Namespace) -> Dict[str, Path]:
    start_day, end_day = resolve_dates(args)
    symbols = tuple(sorted({x.strip().upper() for x in (args.symbols or "").split(",") if x.strip()}))
    started_at = datetime.now()
    config_hash = AUCTION_ENGINE_CONFIG.stable_hash()
    run_id = f"CANDIDATE3B1-{start_day.isoformat()}-{end_day.isoformat()}-{started_at:%Y%m%dT%H%M%S}-{config_hash[:8]}"
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"auction_engine_candidates_{start_day.isoformat()}_{end_day.isoformat()}_{started_at:%Y%m%d_%H%M%S}"

    snapshots = load_snapshot_rows(
        start_day,
        end_day,
        symbols=symbols,
        limit=max(0, int(args.limit or 0)),
    )
    engine = AuctionEngine(AUCTION_ENGINE_CONFIG)
    timeline_rows: List[Dict[str, Any]] = []
    candidate_rows: List[Dict[str, Any]] = []
    error_rows: List[Dict[str, Any]] = []
    symbol_set = set()

    for index, snapshot in enumerate(snapshots, start=1):
        try:
            result = engine.evaluate_snapshot(snapshot)
            row = result_row(result, run_id)
            observations = candidate_observation_rows(result, run_id)
            candidate_rows.extend(observations)
            symbol_set.add(result.symbol)
            if (
                not args.transitions_only
                or row["transitioned"]
                or row.get("boundary_transitioned")
                or bool(observations)
            ):
                timeline_rows.append(row)
        except Exception as exc:
            error_rows.append({
                "row_number": index,
                "symbol": snapshot.get("symbol"),
                "snapshot_time": snapshot.get("snapshot_time"),
                "error_type": type(exc).__name__,
                "error": str(exc),
            })
            if not args.continue_on_error and AUCTION_ENGINE_CONFIG.engine.strict_evaluation:
                raise

    episode_rows = build_episode_summary(timeline_rows)
    candidate_lifecycle_rows = build_candidate_lifecycle(
        candidate_rows,
        snapshots,
        AUCTION_ENGINE_CONFIG.diagnostics.outcome_horizons_bars,
    )
    opportunity_rows = build_opportunity_lifecycle(candidate_lifecycle_rows)
    completed_at = datetime.now()
    manifest = {
        "run_id": run_id,
        "run_type": "PHASE3B2_BREAKOUT_OPPORTUNITY_MODEL_REPORT",
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "start_day": start_day.isoformat(),
        "end_day": end_day.isoformat(),
        "git_commit": git_value("rev-parse", "HEAD"),
        "git_tag": git_value("describe", "--tags", "--exact-match"),
        "config_version": AUCTION_ENGINE_CONFIG.engine.config_version,
        "config_hash": config_hash,
        "resolved_config": AUCTION_ENGINE_CONFIG.resolved_dict(),
        "database_name": database_name(),
        "requested_symbols": list(symbols),
        "symbol_count": len(symbol_set),
        "snapshot_count_loaded": len(snapshots),
        "timeline_row_count": len(timeline_rows),
        "boundary_episode_count": len(episode_rows),
        "boundary_terminal_episode_count": sum(1 for row in episode_rows if row.get("terminal")),
        "boundary_accepted_episode_count": sum(1 for row in episode_rows if row.get("resolution") == "ACCEPTED"),
        "boundary_failed_episode_count": sum(1 for row in episode_rows if row.get("resolution") == "FAILED"),
        "candidate_observation_count": len(candidate_rows),
        "candidate_count": len(candidate_lifecycle_rows),
        "candidate_eligible_count": sum(1 for row in candidate_lifecycle_rows if row.get("best_eligibility") == "ELIGIBLE"),
        "candidate_watch_only_count": sum(1 for row in candidate_lifecycle_rows if row.get("best_eligibility") == "WATCH"),
        "candidate_ineligible_or_expired_count": sum(1 for row in candidate_lifecycle_rows if row.get("best_eligibility") in {"INELIGIBLE", "EXPIRED"}),
        "candidate_family_counts": dict(Counter(str(row.get("family") or "") for row in candidate_lifecycle_rows)),
        "candidate_subtype_counts": dict(Counter(str(row.get("subtype") or "") for row in candidate_lifecycle_rows)),
        "candidate_role_counts": dict(Counter(str(row.get("candidate_role") or "") for row in candidate_lifecycle_rows)),
        "operational_opportunity_count": len(opportunity_rows),
        "operational_opportunity_eligible_count": sum(
            1 for row in opportunity_rows if row.get("best_eligibility") == "ELIGIBLE"
        ),
        "operational_opportunity_watch_only_count": sum(
            1 for row in opportunity_rows if row.get("best_eligibility") == "WATCH"
        ),
        "operational_opportunity_ineligible_or_expired_count": sum(
            1 for row in opportunity_rows
            if row.get("best_eligibility") in {"INELIGIBLE", "EXPIRED"}
        ),
        "candidate_alias_records_deduplicated": max(
            0, len(candidate_lifecycle_rows) - len(opportunity_rows)
        ),
        "error_count": len(error_rows),
        "transitions_only": bool(args.transitions_only),
        "signals_created": 0,
        "setup_state_rows_written": 0,
        "processed_flags_changed": 0,
        "notes": [
            "Patch 1 pure analytical Auction Engine core",
            "Auction Engine owns evidence, state, boundary, current setup candidates, opportunity ledger and local arbitration",
            "Local actions are NO_LOCAL_OPPORTUNITY, LOCAL_WATCH, LOCAL_CONFIRMED, LOCAL_DEFER and LOCAL_BLOCKED",
            "Active signal/trade context is not read or applied",
            "Advisor context is not evaluated inside the Auction Engine",
            "No signal payload is created and no opportunity is consumed by signal context",
            "Accepted/initiation/continuation reward is open-ended; measured moves are diagnostics only",
            "Failed-auction room is measured only to the opposite frozen-range edge",
            "Candidate source-boundary facts and opportunity identities remain immutable",
            "Candidate MFE/MAE/EOD columns are hindsight-only post-processing and never engine inputs",
            "No signals, setup-state writes or processed-flag changes",
            "Existing signal and trade pipeline is not called",
        ],
    }

    paths = {
        "timeline": output_dir / f"{stem}.csv",
        "episodes": output_dir / f"{stem}_episodes.csv",
        "candidates": output_dir / f"{stem}_candidates.csv",
        "candidate_lifecycle": output_dir / f"{stem}_candidate_lifecycle.csv",
        "opportunities": output_dir / f"{stem}_opportunities.csv",
        "summary": output_dir / f"{stem}_summary.csv",
        "manifest": output_dir / f"{stem}_manifest.json",
        "errors": output_dir / f"{stem}_errors.csv",
    }
    write_csv(paths["timeline"], timeline_rows)
    write_csv(paths["episodes"], episode_rows)
    write_csv(paths["candidates"], candidate_rows)
    write_csv(paths["candidate_lifecycle"], candidate_lifecycle_rows)
    write_csv(paths["opportunities"], opportunity_rows)
    write_csv(
        paths["summary"],
        build_summary(timeline_rows, candidate_rows, candidate_lifecycle_rows),
    )
    write_csv(paths["errors"], error_rows)
    paths["manifest"].write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return paths


def _nested(data: Mapping[str, Any], *parts: str) -> Any:
    current: Any = data
    for part in parts:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    paths = run_report(args)
    print("Auction-engine pure-core report complete")
    for label, path in paths.items():
        print(f"  {label}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
