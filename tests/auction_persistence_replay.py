"""Shared persistence/restart replay support for the AutoLabs clone.

This module exercises the final single-owner chain only:

    unprocessed snapshot
    -> active signal context
    -> AuctionEngine
    -> SignalLifecycleService
    -> stock_opportunities
    -> stock_engine_checkpoints
    -> snapshots.processed = 1

Trades are deliberately outside this layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from hashlib import sha256
import csv
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG
from configs.auction_service_config import AUCTION_SERVICE_CONFIG
from database.database import get_trades_db
from models.trade_models import Signal as SignalORM
from models.trade_models import Snapshot as SnapshotORM
from schemas.snapshot import SnapshotSchema
from schemas.stock_engine_checkpoint import StockEngineCheckpoint
from schemas.stock_opportunity import StockOpportunity
from services.auction_engine.checkpoint_codec import checkpoint_state_hash
from services.auction_engine.persistence import AuctionPersistenceCoordinator
from services.auction_engine.service_runner import AuctionServiceRunner
from services.signals.signal_lifecycle_service import SignalLifecycleService
from utils.datetime_utils import to_ist_naive
from utils.json_utils import sanitize_json

logger = logging.getLogger(__name__)


@dataclass
class ReplayOutcome:
    label: str
    trading_day: date
    symbols: Optional[List[str]]
    until_time: Optional[datetime]
    runner: AuctionServiceRunner
    processed_counts: Dict[str, Any]
    database_state: Dict[str, Any]
    profile_timing: bool = False

    @property
    def manifest(self) -> Dict[str, Any]:
        stats = self.runner.stats
        return {
            "label": self.label,
            "trading_day": self.trading_day.isoformat(),
            "symbols": list(self.symbols or []),
            "until_time": self.until_time.isoformat(sep=" ") if self.until_time else None,
            "snapshots_seen": stats.snapshots_seen,
            "snapshots_evaluated": stats.snapshots_evaluated,
            "snapshots_skipped_by_checkpoint": stats.snapshots_skipped_by_checkpoint,
            "snapshots_marked_processed": stats.snapshots_marked_processed,
            "manager_select_count": stats.manager_select_count,
            "would_create_count": stats.would_create_count,
            "manager_actions": stats.manager_actions,
            "final_actions": stats.final_actions,
            "signal_actions": stats.signal_actions,
            "opportunities_written": stats.opportunities_written,
            "checkpoints_written": stats.checkpoints_written,
            "checkpoints_restored": stats.checkpoints_restored,
            "errors": stats.errors,
            "first_snapshot_time": _json_value(stats.first_snapshot_time),
            "last_snapshot_time": _json_value(stats.last_snapshot_time),
            "processed_counts": self.processed_counts,
            "checkpoint_hashes": self.database_state["checkpoint_hashes"],
            "opportunity_count": len(self.database_state["opportunities"]),
            "signal_count": len(self.database_state["signals"]),
            "database_state_hash": self.database_state["database_state_hash"],
            "engine_version": AUCTION_ENGINE_CONFIG.engine.engine_version,
            "config_version": AUCTION_ENGINE_CONFIG.engine.config_version,
            "service_version": AUCTION_SERVICE_CONFIG.service_version,
            "signal_write_enabled": True,
            "opportunity_write_enabled": True,
            "checkpoint_write_enabled": True,
            "restore_enabled": True,
            "mark_processed_enabled": True,
            "profile_timing": bool(self.profile_timing),
            "timing_summary": self.runner.timing_summary() if self.profile_timing else [],
        }


def parse_symbols(raw: Optional[str]) -> Optional[List[str]]:
    if not raw:
        return None
    values = sorted({item.strip().upper() for item in raw.split(",") if item.strip()})
    return values or None


def parse_until(trading_day: date, raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    value = str(raw).strip()
    if "T" in value or " " in value:
        parsed = datetime.fromisoformat(value)
        normalized = to_ist_naive(parsed) or parsed
        if normalized.date() != trading_day:
            raise ValueError("--until must belong to --date")
        return normalized
    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise ValueError("--until must use HH:MM, HH:MM:SS, or ISO datetime")
    hh, mm = int(parts[0]), int(parts[1])
    ss = int(parts[2]) if len(parts) == 3 else 0
    return datetime.combine(trading_day, dtime(hh, mm, ss))


def reset_scope(*, trading_day: date, symbols: Optional[List[str]]) -> Dict[str, int]:
    """Reset only Auction/signal replay state; trades are never touched."""
    day_start = datetime.combine(trading_day, dtime.min)
    day_end = datetime.combine(trading_day, dtime.max)
    with get_trades_db() as db:
        snapshot_query = db.query(SnapshotORM).filter(
            SnapshotORM.snapshot_time >= day_start,
            SnapshotORM.snapshot_time <= day_end,
        )
        signal_query = db.query(SignalORM)
        if symbols:
            snapshot_query = snapshot_query.filter(SnapshotORM.symbol.in_(symbols))
            signal_query = signal_query.filter(SignalORM.symbol.in_(symbols))
        snapshots_reset = int(snapshot_query.update(
            {SnapshotORM.processed: False},
            synchronize_session=False,
        ))
        signals_deleted = int(signal_query.delete(synchronize_session=False))
        db.commit()

    opportunities_deleted = StockOpportunity.delete_day(
        trading_day=trading_day,
        symbols=symbols,
    )
    checkpoints_deleted = StockEngineCheckpoint.delete_day(
        trading_day=trading_day,
        engine_name=AUCTION_ENGINE_CONFIG.engine.engine_name,
        symbols=symbols,
    )
    result = {
        "snapshots_reset": snapshots_reset,
        "signals_deleted": signals_deleted,
        "opportunities_deleted": opportunities_deleted,
        "checkpoints_deleted": checkpoints_deleted,
    }
    logger.info("Auction replay reset | %s", result)
    return result


def run_replay(
    *,
    label: str,
    trading_day: date,
    symbols: Optional[List[str]],
    until_time: Optional[datetime] = None,
    reset: bool = False,
    batch_size: Optional[int] = None,
    profile_timing: bool = False,
) -> ReplayOutcome:
    if reset:
        reset_scope(trading_day=trading_day, symbols=symbols)

    signal_service = SignalLifecycleService(
        lifecycle=AUCTION_SERVICE_CONFIG.signal_lifecycle,
        write_enabled=True,
        causal_replay=False,
    )
    persistence = AuctionPersistenceCoordinator(
        AUCTION_ENGINE_CONFIG,
        opportunity_write_enabled=True,
        checkpoint_write_enabled=True,
        profile_timing_enabled=profile_timing,
    )
    runner = AuctionServiceRunner(
        signal_service=signal_service,
        persistence=persistence,
        restore_enabled=True,
        mark_processed_enabled=True,
        profile_timing=profile_timing,
    )
    runner.start_day(trading_day)

    after_time: Optional[datetime] = None
    after_symbol = ""
    size = int(batch_size or AUCTION_SERVICE_CONFIG.batch_size)
    while True:
        rows = SnapshotSchema.fetch_unprocessed_day_batch(
            trading_day=trading_day,
            after_time=after_time,
            after_symbol=after_symbol,
            symbols=symbols,
            until_time=until_time,
            limit=size,
        )
        if not rows:
            break
        runner.process_snapshots(rows)
        last = rows[-1]
        after_time = to_ist_naive(last.snapshot_time) or last.snapshot_time
        after_symbol = str(last.symbol).strip().upper()
        logger.info(
            "%s progress | evaluated=%d processed=%d opportunities_written=%d "
            "checkpoints_written=%d restored=%d errors=%d",
            label,
            runner.stats.snapshots_evaluated,
            runner.stats.snapshots_marked_processed,
            runner.stats.opportunities_written,
            runner.stats.checkpoints_written,
            runner.stats.checkpoints_restored,
            runner.stats.errors,
        )
        if len(rows) < size:
            break

    processed_counts = snapshot_counts(trading_day=trading_day, symbols=symbols)
    state = capture_database_state(trading_day=trading_day, symbols=symbols)
    return ReplayOutcome(
        label=label,
        trading_day=trading_day,
        symbols=symbols,
        until_time=until_time,
        runner=runner,
        processed_counts=processed_counts,
        database_state=state,
        profile_timing=bool(profile_timing),
    )


def snapshot_counts(*, trading_day: date, symbols: Optional[List[str]]) -> Dict[str, Any]:
    day_start = datetime.combine(trading_day, dtime.min)
    day_end = datetime.combine(trading_day, dtime.max)
    with get_trades_db() as db:
        query = db.query(SnapshotORM.symbol, SnapshotORM.processed).filter(
            SnapshotORM.snapshot_time >= day_start,
            SnapshotORM.snapshot_time <= day_end,
        )
        if symbols:
            query = query.filter(SnapshotORM.symbol.in_(symbols))
        rows = query.order_by(SnapshotORM.symbol.asc()).all()

    by_symbol: Dict[str, Dict[str, int]] = {}
    for raw_symbol, processed in rows:
        symbol = str(raw_symbol or "").strip().upper()
        item = by_symbol.setdefault(symbol, {"total": 0, "processed": 0, "unprocessed": 0})
        item["total"] += 1
        item["processed" if bool(processed) else "unprocessed"] += 1
    total = sum(item["total"] for item in by_symbol.values())
    processed = sum(item["processed"] for item in by_symbol.values())
    return {
        "total": total,
        "processed": processed,
        "unprocessed": total - processed,
        "by_symbol": by_symbol,
    }


def capture_database_state(*, trading_day: date, symbols: Optional[List[str]]) -> Dict[str, Any]:
    opportunities = [
        item.model_dump(mode="json")
        for item in StockOpportunity.fetch_day(
            trading_day=trading_day,
            symbols=symbols,
        )
    ]
    opportunities.sort(key=lambda row: row["opportunity_key"])

    checkpoints = StockEngineCheckpoint.fetch_day(
        trading_day=trading_day,
        engine_name=AUCTION_ENGINE_CONFIG.engine.engine_name,
        symbols=symbols,
    )
    checkpoint_rows: List[Dict[str, Any]] = []
    checkpoint_hashes: Dict[str, str] = {}
    for item in checkpoints:
        digest = checkpoint_state_hash(item.state_json)
        checkpoint_hashes[item.symbol] = digest
        checkpoint_rows.append({
            "trading_day": item.trading_day.isoformat(),
            "symbol": item.symbol,
            "engine_name": item.engine_name,
            "engine_version": item.engine_version,
            "config_version": item.config_version,
            "last_processed_snapshot_time": _json_value(item.last_processed_snapshot_time),
            "last_snapshot_hash": item.last_snapshot_hash,
            "checkpoint_status": item.checkpoint_status,
            "checkpoint_version": item.checkpoint_version,
            "checkpoint_state_hash": digest,
            "diagnostics_json": sanitize_json(item.diagnostics_json),
        })
    checkpoint_rows.sort(key=lambda row: row["symbol"])

    signals = _signal_projection(trading_day=trading_day, symbols=symbols)
    processed = snapshot_counts(trading_day=trading_day, symbols=symbols)
    stable = {
        "opportunities": opportunities,
        "checkpoint_hashes": dict(sorted(checkpoint_hashes.items())),
        "signals": signals,
        "processed_counts": processed,
    }
    return {
        **stable,
        "checkpoints": checkpoint_rows,
        "database_state_hash": _stable_hash(stable),
    }


def _signal_projection(*, trading_day: date, symbols: Optional[List[str]]) -> List[Dict[str, Any]]:
    day_start = datetime.combine(trading_day, dtime.min)
    day_end = datetime.combine(trading_day, dtime.max)
    with get_trades_db() as db:
        query = db.query(SignalORM).filter(
            SignalORM.first_seen_time >= day_start,
            SignalORM.first_seen_time <= day_end,
        )
        if symbols:
            query = query.filter(SignalORM.symbol.in_(symbols))
        # Signal rows also carry large JSON payloads. Keep sorting narrow to
        # avoid MySQL error 1038 (Out of sort memory), then hydrate by id and
        # restore the deterministic order in Python.
        ordered_ids = [
            row_id
            for (row_id,) in (
                query.with_entities(SignalORM.id)
                .order_by(
                    SignalORM.symbol.asc(),
                    SignalORM.first_seen_time.asc(),
                    SignalORM.id.asc(),
                )
                .all()
            )
        ]
        if not ordered_ids:
            rows = []
        else:
            fetched = db.query(SignalORM).filter(SignalORM.id.in_(ordered_ids)).all()
            by_id = {int(row.id): row for row in fetched}
            rows = [by_id[row_id] for row_id in ordered_ids if row_id in by_id]

    stable_columns = (
        "signal_id", "equity_ref", "symbol", "lifecycle", "setup", "side",
        "stage", "status", "status_reason", "first_seen_time", "created_price",
        "last_eval_time", "last_snapshot_time", "stage_changed_time",
        "status_changed_time", "qualified_time", "actionable_time", "closed_time",
        "closed_price", "last_price", "ltp", "ltp_time", "last_pnl",
        "last_pnl_value", "max_price", "min_price", "max_time", "min_time",
        "max_pnl", "min_pnl", "max_pnl_value", "min_pnl_value",
        "criteria_json", "snapshot_json", "meta_json",
    )
    output = []
    for row in rows:
        output.append({
            name: _json_value(getattr(row, name, None))
            for name in stable_columns
        })
    return output


def write_outcome_reports(outcome: ReplayOutcome, *, report_dir: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = Path(report_dir) / f"auction_persistence_{outcome.label}_{outcome.trading_day}_{stamp}"
    prefix.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(prefix.with_name(prefix.name + "_decisions.csv"), outcome.runner.stats.decision_rows)
    _write_csv(prefix.with_name(prefix.name + "_signal_lifecycle.csv"), outcome.runner.stats.signal_rows)
    _write_csv(prefix.with_name(prefix.name + "_opportunities.csv"), outcome.database_state["opportunities"])
    _write_csv(prefix.with_name(prefix.name + "_checkpoints.csv"), outcome.database_state["checkpoints"])
    _write_csv(prefix.with_name(prefix.name + "_signals.csv"), outcome.database_state["signals"])
    if outcome.profile_timing:
        _write_csv(
            prefix.with_name(prefix.name + "_timing_snapshots.csv"),
            outcome.runner.stats.timing_rows,
        )
        _write_csv(
            prefix.with_name(prefix.name + "_timing_summary.csv"),
            outcome.runner.timing_summary(),
        )
    manifest = outcome.manifest
    prefix.with_name(prefix.name + "_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_csv(prefix.with_name(prefix.name + "_summary.csv"), [{
        key: json.dumps(value, sort_keys=True, default=str)
        if isinstance(value, (dict, list)) else value
        for key, value in manifest.items()
    }])
    return prefix


def combine_rows(first: Sequence[Dict[str, Any]], second: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return list(first) + list(second)


def compare_replays(
    *,
    continuous: ReplayOutcome,
    split_first: ReplayOutcome,
    split_second: ReplayOutcome,
) -> Dict[str, Any]:
    split_decisions = combine_rows(
        split_first.runner.stats.decision_rows,
        split_second.runner.stats.decision_rows,
    )
    split_lifecycle = combine_rows(
        split_first.runner.stats.signal_rows,
        split_second.runner.stats.signal_rows,
    )
    checks = {
        "database_state_hash_match": (
            continuous.database_state["database_state_hash"]
            == split_second.database_state["database_state_hash"]
        ),
        "checkpoint_hashes_match": (
            continuous.database_state["checkpoint_hashes"]
            == split_second.database_state["checkpoint_hashes"]
        ),
        "opportunities_match": (
            continuous.database_state["opportunities"]
            == split_second.database_state["opportunities"]
        ),
        "signals_match": (
            continuous.database_state["signals"]
            == split_second.database_state["signals"]
        ),
        "processed_counts_match": (
            continuous.processed_counts == split_second.processed_counts
        ),
        "decision_rows_match": (
            continuous.runner.stats.decision_rows == split_decisions
        ),
        "signal_lifecycle_rows_match": (
            continuous.runner.stats.signal_rows == split_lifecycle
        ),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "continuous_database_state_hash": continuous.database_state["database_state_hash"],
        "split_database_state_hash": split_second.database_state["database_state_hash"],
        "continuous_checkpoint_hashes": continuous.database_state["checkpoint_hashes"],
        "split_checkpoint_hashes": split_second.database_state["checkpoint_hashes"],
        "continuous_counts": continuous.manifest,
        "split_first_counts": split_first.manifest,
        "split_second_counts": split_second.manifest,
    }


def _write_csv(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    data = [sanitize_json(row) for row in rows]
    if not data:
        path.write_text("", encoding="utf-8")
        return
    fields = sorted({key for row in data for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in data:
            writer.writerow({
                key: json.dumps(value, sort_keys=True, default=str)
                if isinstance(value, (dict, list)) else value
                for key, value in row.items()
            })


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        normalized = to_ist_naive(value) or value
        return normalized.isoformat(sep=" ")
    return sanitize_json(value)


def _stable_hash(value: Any) -> str:
    raw = json.dumps(
        sanitize_json(value),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256(raw).hexdigest()


__all__ = [
    "ReplayOutcome",
    "capture_database_state",
    "compare_replays",
    "parse_symbols",
    "parse_until",
    "reset_scope",
    "run_replay",
    "write_outcome_reports",
]
