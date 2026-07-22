#!/usr/bin/env python3
"""Run the single-owner Auction Signal service.

Historical mode is report-only by default.  Live cutover uses the same loop as
``gen_signals.py`` but delegates interpretation to AuctionEngine and signal-row
mechanics to SignalLifecycleService.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, time as dtime
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Iterable, List, Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG
from configs.auction_service_config import AUCTION_SERVICE_CONFIG
from logconfig import setup_logging
from schemas.snapshot import SnapshotSchema
from schemas.stock_engine_checkpoint import StockEngineCheckpoint
from schemas.stock_opportunity import StockOpportunity
from services.auction_engine.persistence import AuctionPersistenceCoordinator
from services.auction_engine.service_runner import AuctionServiceRunner
from services.signals.signal_lifecycle_service import SignalLifecycleService
from utils.run_control import allow_run_today

IST = ZoneInfo(AUCTION_ENGINE_CONFIG.engine.timezone)
logger: Optional[logging.Logger] = None


def _args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Auction Engine as the signal-service owner")
    parser.add_argument("--date", help="Historical report day YYYY-MM-DD")
    parser.add_argument("--symbols", help="Comma-separated symbols")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--log-file")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--restore", action="store_true", help="Restore checkpoints in historical mode")
    parser.add_argument("--write-checkpoints", action="store_true")
    parser.add_argument("--write-opportunities", action="store_true")
    parser.add_argument("--write-signals", action="store_true")
    parser.add_argument("--mark-processed", action="store_true")
    parser.add_argument(
        "--confirm-live-cutover",
        action="store_true",
        help="Required in live mode after the legacy signal service is disabled",
    )
    parser.add_argument("--reset-day", action="store_true", help="Delete auction rows for --date")
    return parser.parse_args(argv)


def _symbol_filter(raw: Optional[str]) -> Optional[List[str]]:
    if not raw:
        return None
    return sorted({item.strip().upper() for item in raw.split(",") if item.strip()})


def _write_csv(path: Path, rows: Iterable[dict]) -> None:
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


def _runner(args: argparse.Namespace, *, historical: bool) -> AuctionServiceRunner:
    if args.write_signals and not (args.write_checkpoints and args.write_opportunities):
        raise ValueError(
            "--write-signals requires --write-checkpoints and --write-opportunities"
        )
    if args.mark_processed and not args.write_signals:
        raise ValueError(
            "--mark-processed requires --write-signals so snapshots cannot be "
            "acknowledged without signal maintenance"
        )

    signal_service = SignalLifecycleService(
        lifecycle=AUCTION_SERVICE_CONFIG.signal_lifecycle,
        write_enabled=bool(args.write_signals),
        causal_replay=historical,
    )
    persistence = AuctionPersistenceCoordinator(
        AUCTION_ENGINE_CONFIG,
        opportunity_write_enabled=bool(args.write_opportunities),
        checkpoint_write_enabled=bool(args.write_checkpoints),
    )
    return AuctionServiceRunner(
        signal_service=signal_service,
        persistence=persistence,
        restore_enabled=(bool(args.restore) if historical else True),
        mark_processed_enabled=bool(args.mark_processed),
    )


def _historical(args: argparse.Namespace) -> int:
    if args.write_signals or args.mark_processed:
        raise ValueError(
            "Historical Phase 5A validation cannot write signals or alter "
            "snapshot processed flags"
        )
    trading_day = date.fromisoformat(args.date)
    symbols = _symbol_filter(args.symbols)
    if args.reset_day:
        deleted_opportunities = StockOpportunity.delete_day(trading_day=trading_day)
        deleted_checkpoints = StockEngineCheckpoint.delete_day(
            trading_day=trading_day,
            engine_name=AUCTION_ENGINE_CONFIG.engine.engine_name,
        )
        logger.info(
            "Reset auction persistence | opportunities=%d checkpoints=%d",
            deleted_opportunities,
            deleted_checkpoints,
        )

    runner = _runner(args, historical=True)
    runner.start_day(trading_day)
    after_time = None
    after_symbol = ""
    remaining = args.limit if args.limit > 0 else None
    while True:
        batch_limit = AUCTION_SERVICE_CONFIG.batch_size
        if remaining is not None:
            batch_limit = min(batch_limit, remaining)
        rows = SnapshotSchema.fetch_day_replay_batch(
            trading_day=trading_day,
            after_time=after_time,
            after_symbol=after_symbol,
            symbols=symbols,
            limit=batch_limit,
        )
        if not rows:
            break
        runner.process_snapshots(rows)
        last = rows[-1]
        after_time = last.snapshot_time
        after_symbol = last.symbol
        if remaining is not None:
            remaining -= len(rows)
            if remaining <= 0:
                break
        if len(rows) < batch_limit:
            break

    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    prefix = Path(args.report_dir) / f"auction_service_{trading_day}_{stamp}"
    _write_csv(prefix.with_name(prefix.name + "_decisions.csv"), runner.stats.decision_rows)
    _write_csv(prefix.with_name(prefix.name + "_signal_lifecycle.csv"), runner.stats.signal_rows)
    _write_csv(prefix.with_name(prefix.name + "_checkpoints.csv"), runner.checkpoint_rows())
    summary = {
        "trading_day": trading_day.isoformat(),
        "snapshots_seen": runner.stats.snapshots_seen,
        "snapshots_evaluated": runner.stats.snapshots_evaluated,
        "snapshots_skipped_by_checkpoint": runner.stats.snapshots_skipped_by_checkpoint,
        "snapshots_marked_processed": runner.stats.snapshots_marked_processed,
        "manager_select_count": runner.stats.manager_select_count,
        "would_create_count": runner.stats.would_create_count,
        "manager_actions": runner.stats.manager_actions,
        "final_actions": runner.stats.final_actions,
        "signal_actions": runner.stats.signal_actions,
        "opportunities_written": runner.stats.opportunities_written,
        "checkpoints_written": runner.stats.checkpoints_written,
        "checkpoints_restored": runner.stats.checkpoints_restored,
        "errors": runner.stats.errors,
        "first_snapshot_time": runner.stats.first_snapshot_time,
        "last_snapshot_time": runner.stats.last_snapshot_time,
        "signal_write_enabled": bool(args.write_signals),
        "checkpoint_write_enabled": bool(args.write_checkpoints),
        "opportunity_write_enabled": bool(args.write_opportunities),
        "mark_processed_enabled": bool(args.mark_processed),
        "restore_enabled": bool(args.restore),
        "engine_version": AUCTION_ENGINE_CONFIG.engine.engine_version,
        "config_version": AUCTION_ENGINE_CONFIG.engine.config_version,
        "service_version": AUCTION_SERVICE_CONFIG.service_version,
    }
    _write_csv(prefix.with_name(prefix.name + "_summary.csv"), [{
        **summary,
        "manager_actions": json.dumps(summary["manager_actions"], sort_keys=True),
        "final_actions": json.dumps(summary["final_actions"], sort_keys=True),
        "signal_actions": json.dumps(summary["signal_actions"], sort_keys=True),
    }])
    prefix.with_name(prefix.name + "_manifest.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    logger.info("Historical Auction service complete | %s", summary)
    logger.info("Reports: %s_*", prefix)
    return 0


def _in_window(now: datetime) -> bool:
    start = dtime.fromisoformat(AUCTION_SERVICE_CONFIG.window_start)
    end = dtime.fromisoformat(AUCTION_SERVICE_CONFIG.window_end)
    return start <= now.astimezone(IST).time() < end


def _wait_for_window() -> bool:
    start = dtime.fromisoformat(AUCTION_SERVICE_CONFIG.window_start)
    end = dtime.fromisoformat(AUCTION_SERVICE_CONFIG.window_end)
    while True:
        now = datetime.now(IST)
        if now.time() >= end:
            return False
        if now.time() >= start:
            return True
        time.sleep(5 if (datetime.combine(now.date(), start, IST) - now).total_seconds() < 120 else 30)


def _live(args: argparse.Namespace) -> int:
    if not allow_run_today(logger, "auction"):
        return 0
    if not _wait_for_window():
        return 0
    if not args.confirm_live_cutover:
        raise ValueError(
            "Live Auction service requires --confirm-live-cutover after disabling gen_signals"
        )
    required = {
        "--write-checkpoints": args.write_checkpoints,
        "--write-opportunities": args.write_opportunities,
        "--write-signals": args.write_signals,
        "--mark-processed": args.mark_processed,
    }
    missing = [name for name, enabled in required.items() if not enabled]
    if missing:
        raise ValueError(
            "Live cutover requires the complete single-owner chain: " + ", ".join(missing)
        )

    runner = _runner(args, historical=False)
    runner.start_day(datetime.now(IST).date())
    try:
        while True:
            now = datetime.now(IST)
            if not _in_window(now):
                break
            rows = SnapshotSchema.fetch_unprocessed(
                limit=AUCTION_SERVICE_CONFIG.batch_size
            )
            if rows:
                runner.process_snapshots(rows)
                logger.info(
                    "Auction poll | evaluated=%d processed=%d creates=%d errors=%d",
                    runner.stats.snapshots_evaluated,
                    runner.stats.snapshots_marked_processed,
                    runner.stats.would_create_count,
                    runner.stats.errors,
                )
            time.sleep(AUCTION_SERVICE_CONFIG.retry_interval_seconds)
    except KeyboardInterrupt:
        logger.info("Auction service interrupted")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    global logger
    args = _args(argv)
    log_file = args.log_file or (
        str(Path(args.report_dir) / "gen_auction.log")
        if args.date else AUCTION_SERVICE_CONFIG.log_file
    )
    setup_logging(log_file=log_file)
    logger = logging.getLogger(__name__)
    logger.info(
        "=== Auction Signal Service | date=%s restore=%s checkpoint_write=%s "
        "opportunity_write=%s signal_write=%s mark_processed=%s ===",
        args.date,
        args.restore,
        args.write_checkpoints,
        args.write_opportunities,
        args.write_signals,
        args.mark_processed,
    )
    return _historical(args) if args.date else _live(args)


if __name__ == "__main__":
    raise SystemExit(main())
