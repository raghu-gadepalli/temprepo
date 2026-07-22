#!/usr/bin/env python3
"""Signal-only Auction Engine replay harness.

This replaces the legacy SignalGenerator test harness in the AutoLabs clone.
It deliberately tests only the new signal path:

    unprocessed snapshot
    -> active signal context
    -> AuctionEngine evaluation
    -> SignalLifecycleService persistence

It does NOT:
- mark snapshots processed;
- write stock_opportunities;
- write stock_engine_checkpoints;
- generate user trades;
- run TradeExecutor or TradeMonitor.

Because snapshots remain unprocessed, the same source rows can be replayed again
once the signal table is cleared.  Use ``--clear-signals`` for a clean run.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set
from zoneinfo import ZoneInfo

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG
from configs.auction_service_config import AUCTION_SERVICE_CONFIG
from database.database import get_trades_db
from logconfig import setup_logging
from models.trade_models import Signal as SignalORM
from models.trade_models import Snapshot as SnapshotORM
from schemas.snapshot import SnapshotSchema
from services.auction_engine.persistence import AuctionPersistenceCoordinator
from services.auction_engine.service_runner import AuctionServiceRunner
from services.signals.signal_lifecycle_service import SignalLifecycleService
from utils.datetime_utils import to_ist_naive
from utils.json_utils import sanitize_json

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


def _args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay unprocessed snapshots through Auction Engine and persist signals only."
    )
    parser.add_argument(
        "--date",
        required=True,
        help="Trading day in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--symbols",
        help="Optional comma-separated symbol filter.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional maximum snapshot count after filtering; 0 means all.",
    )
    parser.add_argument(
        "--clear-signals",
        action="store_true",
        help="Delete live signal rows for the selected symbols before replay.",
    )
    parser.add_argument(
        "--report-dir",
        default="reports",
        help="Directory for CSV and manifest outputs.",
    )
    parser.add_argument("--log-file")
    return parser.parse_args(argv)


def _symbol_filter(raw: Optional[str]) -> Optional[List[str]]:
    if not raw:
        return None
    values = sorted({item.strip().upper() for item in raw.split(",") if item.strip()})
    return values or None


def _write_csv(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    data = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not data:
        path.write_text("", encoding="utf-8")
        return
    fields = sorted({key for row in data for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(data)


def _load_unprocessed_snapshots(
    *,
    trading_day: date,
    symbols: Optional[List[str]],
    limit: int,
) -> List[SnapshotSchema]:
    rows = SnapshotSchema.fetch_unprocessed() or []
    wanted = set(symbols or [])
    selected: List[SnapshotSchema] = []
    for row in rows:
        ts = to_ist_naive(row.snapshot_time) or row.snapshot_time
        symbol = str(row.symbol or "").strip().upper()
        if ts.date() != trading_day:
            continue
        if wanted and symbol not in wanted:
            continue
        selected.append(row)

    selected.sort(key=lambda row: (to_ist_naive(row.snapshot_time) or row.snapshot_time, row.symbol))
    if limit > 0:
        selected = selected[:limit]
    return selected


def _snapshot_processed_counts(
    *,
    trading_day: date,
    symbols: Optional[List[str]],
) -> Dict[str, int]:
    day_start = datetime.combine(trading_day, datetime.min.time())
    day_end = datetime.combine(trading_day, datetime.max.time())
    with get_trades_db() as db:
        query = db.query(SnapshotORM).filter(
            SnapshotORM.snapshot_time >= day_start,
            SnapshotORM.snapshot_time <= day_end,
        )
        if symbols:
            query = query.filter(SnapshotORM.symbol.in_(symbols))
        total = int(query.count())
        processed = int(query.filter(SnapshotORM.processed == True).count())  # noqa: E712
    return {
        "total": total,
        "processed": processed,
        "unprocessed": total - processed,
    }


def _snapshot_counts_by_symbol(
    *,
    trading_day: date,
    symbols: Optional[List[str]],
) -> Dict[str, Dict[str, int]]:
    """Return total/processed/unprocessed counts per symbol for the test day."""
    day_start = datetime.combine(trading_day, datetime.min.time())
    day_end = datetime.combine(trading_day, datetime.max.time())
    with get_trades_db() as db:
        query = db.query(SnapshotORM.symbol, SnapshotORM.processed).filter(
            SnapshotORM.snapshot_time >= day_start,
            SnapshotORM.snapshot_time <= day_end,
        )
        if symbols:
            query = query.filter(SnapshotORM.symbol.in_(symbols))
        rows = query.order_by(SnapshotORM.symbol.asc()).all()

    counts: Dict[str, Dict[str, int]] = {}
    for raw_symbol, processed in rows:
        symbol = str(raw_symbol or "").strip().upper()
        entry = counts.setdefault(
            symbol,
            {"total": 0, "processed": 0, "unprocessed": 0},
        )
        entry["total"] += 1
        if bool(processed):
            entry["processed"] += 1
        else:
            entry["unprocessed"] += 1
    return counts


def _selected_snapshot_counts(
    snapshots: Sequence[SnapshotSchema],
) -> Dict[str, int]:
    counts: Counter[str] = Counter(
        str(snapshot.symbol or "").strip().upper() for snapshot in snapshots
    )
    return dict(sorted(counts.items()))


def _clear_signals(symbols: Optional[List[str]]) -> int:
    with get_trades_db() as db:
        query = db.query(SignalORM)
        if symbols:
            query = query.filter(SignalORM.symbol.in_(symbols))
        deleted = int(query.delete(synchronize_session=False))
        db.commit()
    return deleted


def _signal_rows(signal_ids: Set[str]) -> List[Dict[str, Any]]:
    if not signal_ids:
        return []
    with get_trades_db() as db:
        rows = (
            db.query(SignalORM)
            .filter(SignalORM.signal_id.in_(sorted(signal_ids)))
            .order_by(SignalORM.first_seen_time.asc(), SignalORM.symbol.asc())
            .all()
        )

    output: List[Dict[str, Any]] = []
    for row in rows:
        meta = row.meta_json if isinstance(row.meta_json, dict) else {}
        auction = meta.get("auction_engine") if isinstance(meta.get("auction_engine"), dict) else {}
        output.append({
            "signal_id": row.signal_id,
            "equity_ref": row.equity_ref,
            "symbol": row.symbol,
            "lifecycle": row.lifecycle,
            "setup": row.setup,
            "side": str(row.side),
            "stage": str(row.stage),
            "status": str(row.status),
            "status_reason": row.status_reason,
            "first_seen_time": row.first_seen_time,
            "last_eval_time": row.last_eval_time,
            "last_snapshot_time": row.last_snapshot_time,
            "created_price": row.created_price,
            "last_price": row.last_price,
            "ltp": row.ltp,
            "last_pnl": row.last_pnl,
            "max_price": row.max_price,
            "min_price": row.min_price,
            "max_pnl": row.max_pnl,
            "min_pnl": row.min_pnl,
            "opportunity_key": auction.get("opportunity_key"),
            "candidate_id": auction.get("candidate_id"),
            "event_key": auction.get("event_key"),
        })
    return output


def _manifest(
    *,
    trading_day: date,
    runner: AuctionServiceRunner,
    signal_rows: List[Dict[str, Any]],
    processed_before: Dict[str, int],
    processed_after: Dict[str, int],
    cleared_signals: int,
    requested_symbols: List[str],
    found_symbols: List[str],
    missing_symbols: List[str],
    selected_snapshot_counts: Dict[str, int],
    snapshot_counts_before_by_symbol: Dict[str, Dict[str, int]],
    signal_clear_scope: List[str],
) -> Dict[str, Any]:
    actions = Counter(str(row.get("applied_action") or "") for row in runner.stats.signal_rows)
    setups = Counter(str(row.get("setup") or "") for row in signal_rows)
    sides = Counter(str(row.get("side") or "") for row in signal_rows)
    statuses = Counter(str(row.get("status") or "") for row in signal_rows)
    return sanitize_json({
        "trading_day": trading_day,
        "engine_version": AUCTION_ENGINE_CONFIG.engine.engine_version,
        "config_version": AUCTION_ENGINE_CONFIG.engine.config_version,
        "service_version": AUCTION_SERVICE_CONFIG.service_version,
        "mode": "SIGNAL_ONLY_WRITE_TEST",
        "symbol_filter_mode": "EXPLICIT" if requested_symbols else "ALL_UNPROCESSED",
        "requested_symbols": requested_symbols,
        "found_symbols": found_symbols,
        "missing_symbols": missing_symbols,
        "selected_snapshot_counts": selected_snapshot_counts,
        "snapshot_counts_before_by_symbol": snapshot_counts_before_by_symbol,
        "signal_clear_scope": signal_clear_scope,
        "snapshots_seen": runner.stats.snapshots_seen,
        "snapshots_evaluated": runner.stats.snapshots_evaluated,
        "snapshots_marked_processed": runner.stats.snapshots_marked_processed,
        "processed_counts_before": processed_before,
        "processed_counts_after": processed_after,
        "processed_flags_unchanged": processed_before == processed_after,
        "manager_select_count": runner.stats.manager_select_count,
        "manager_actions": runner.stats.manager_actions,
        "final_actions": runner.stats.final_actions,
        "signal_actions": dict(sorted(actions.items())),
        "signals_touched": len(signal_rows),
        "signal_setup_counts": dict(sorted(setups.items())),
        "signal_side_counts": dict(sorted(sides.items())),
        "signal_status_counts": dict(sorted(statuses.items())),
        "signals_cleared_before_run": cleared_signals,
        "opportunities_written": runner.stats.opportunities_written,
        "checkpoints_written": runner.stats.checkpoints_written,
        "checkpoints_restored": runner.stats.checkpoints_restored,
        "errors": runner.stats.errors,
        "first_snapshot_time": runner.stats.first_snapshot_time,
        "last_snapshot_time": runner.stats.last_snapshot_time,
        "signal_write_enabled": True,
        "opportunity_write_enabled": False,
        "checkpoint_write_enabled": False,
        "restore_enabled": False,
        "mark_processed_enabled": False,
    })


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _args(argv)
    trading_day = date.fromisoformat(args.date)
    symbols = _symbol_filter(args.symbols)
    report_dir = Path(args.report_dir)
    log_file = args.log_file or str(report_dir / "test_signal_generator.log")
    setup_logging(log_file=log_file)
    global logger
    logger = logging.getLogger(__name__)

    processed_before = _snapshot_processed_counts(
        trading_day=trading_day,
        symbols=symbols,
    )
    snapshot_counts_before_by_symbol = _snapshot_counts_by_symbol(
        trading_day=trading_day,
        symbols=symbols,
    )
    snapshots = _load_unprocessed_snapshots(
        trading_day=trading_day,
        symbols=symbols,
        limit=max(0, int(args.limit)),
    )
    if not snapshots:
        raise RuntimeError(
            f"No unprocessed snapshots found for {trading_day} and symbols={symbols or 'ALL'}"
        )

    requested_symbols = list(symbols or [])
    selected_snapshot_counts = _selected_snapshot_counts(snapshots)
    found_symbols = sorted(selected_snapshot_counts)
    missing_symbols = sorted(set(requested_symbols) - set(found_symbols))
    signal_clear_scope = requested_symbols or found_symbols
    cleared_signals = (
        _clear_signals(signal_clear_scope) if args.clear_signals else 0
    )

    logger.info(
        "=== Auction signal-only replay | date=%s snapshots=%d requested=%s "
        "found=%s missing=%s clear_signals=%s clear_scope=%s "
        "mark_processed=False ===",
        trading_day,
        len(snapshots),
        requested_symbols or "ALL_UNPROCESSED",
        found_symbols,
        missing_symbols,
        bool(args.clear_signals),
        signal_clear_scope,
    )
    if missing_symbols:
        logger.warning(
            "Requested symbols have no unprocessed snapshots for the test day | %s",
            missing_symbols,
        )

    signal_service = SignalLifecycleService(
        lifecycle=AUCTION_SERVICE_CONFIG.signal_lifecycle,
        write_enabled=True,
        causal_replay=False,
    )
    persistence = AuctionPersistenceCoordinator(
        AUCTION_ENGINE_CONFIG,
        opportunity_write_enabled=False,
        checkpoint_write_enabled=False,
    )
    runner = AuctionServiceRunner(
        signal_service=signal_service,
        persistence=persistence,
        restore_enabled=False,
        mark_processed_enabled=False,
    )
    runner.start_day(trading_day)

    for index, snapshot in enumerate(snapshots, start=1):
        runner.process_snapshot(snapshot)
        if index % 500 == 0 or index == len(snapshots):
            logger.info(
                "Signal-only progress | %d/%d snapshots | selects=%d actions=%s errors=%d",
                index,
                len(snapshots),
                runner.stats.manager_select_count,
                runner.stats.signal_actions,
                runner.stats.errors,
            )

    processed_after = _snapshot_processed_counts(
        trading_day=trading_day,
        symbols=symbols,
    )
    if processed_before != processed_after:
        raise RuntimeError(
            "Signal-only replay changed snapshot processed flags: "
            f"before={processed_before} after={processed_after}"
        )

    touched_ids = {
        str(row.get("signal_id"))
        for row in runner.stats.signal_rows
        if row.get("signal_id")
    }
    persisted_signals = _signal_rows(touched_ids)
    manifest = _manifest(
        trading_day=trading_day,
        runner=runner,
        signal_rows=persisted_signals,
        processed_before=processed_before,
        processed_after=processed_after,
        cleared_signals=cleared_signals,
        requested_symbols=requested_symbols,
        found_symbols=found_symbols,
        missing_symbols=missing_symbols,
        selected_snapshot_counts=selected_snapshot_counts,
        snapshot_counts_before_by_symbol=snapshot_counts_before_by_symbol,
        signal_clear_scope=signal_clear_scope,
    )

    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    prefix = report_dir / f"auction_signal_test_{trading_day}_{stamp}"
    _write_csv(prefix.with_name(prefix.name + "_decisions.csv"), runner.stats.decision_rows)
    _write_csv(prefix.with_name(prefix.name + "_lifecycle.csv"), runner.stats.signal_rows)
    _write_csv(prefix.with_name(prefix.name + "_signals.csv"), persisted_signals)
    _write_csv(prefix.with_name(prefix.name + "_summary.csv"), [{
        **manifest,
        "manager_actions": json.dumps(manifest["manager_actions"], sort_keys=True),
        "final_actions": json.dumps(manifest["final_actions"], sort_keys=True),
        "signal_actions": json.dumps(manifest["signal_actions"], sort_keys=True),
        "processed_counts_before": json.dumps(manifest["processed_counts_before"], sort_keys=True),
        "processed_counts_after": json.dumps(manifest["processed_counts_after"], sort_keys=True),
        "requested_symbols": json.dumps(manifest["requested_symbols"]),
        "found_symbols": json.dumps(manifest["found_symbols"]),
        "missing_symbols": json.dumps(manifest["missing_symbols"]),
        "selected_snapshot_counts": json.dumps(
            manifest["selected_snapshot_counts"], sort_keys=True
        ),
        "snapshot_counts_before_by_symbol": json.dumps(
            manifest["snapshot_counts_before_by_symbol"], sort_keys=True
        ),
        "signal_clear_scope": json.dumps(manifest["signal_clear_scope"]),
        "signal_setup_counts": json.dumps(manifest["signal_setup_counts"], sort_keys=True),
        "signal_side_counts": json.dumps(manifest["signal_side_counts"], sort_keys=True),
        "signal_status_counts": json.dumps(manifest["signal_status_counts"], sort_keys=True),
    }])
    prefix.with_name(prefix.name + "_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )

    logger.info("Signal-only Auction replay complete | %s", manifest)
    logger.info("Reports: %s_*", prefix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
