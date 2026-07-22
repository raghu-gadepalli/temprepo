#!/usr/bin/env python3
"""Continuous-versus-checkpoint-restore determinism test.

The program performs two clean runs over the same unprocessed snapshot set:

1. one continuous full-day Auction run;
2. a split run that stops at ``--split-time``, discards all in-memory runners,
   then resumes from persisted checkpoints in a fresh runner.

The final checkpoints, opportunities, signals, processed counts, decision rows,
and lifecycle rows must match exactly. Trades are never read or written.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import logging
import os
from pathlib import Path
import sys
from typing import Optional, Sequence

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from logconfig import setup_logging
from tests.auction_persistence_replay import (
    compare_replays,
    parse_symbols,
    parse_until,
    run_replay,
    write_outcome_reports,
)

logger = logging.getLogger(__name__)


def _args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare continuous Auction replay with checkpoint restore."
    )
    parser.add_argument("--date", required=True, help="Trading day YYYY-MM-DD")
    parser.add_argument("--symbols", help="Optional comma-separated symbols")
    parser.add_argument("--split-time", default="12:00")
    parser.add_argument("--batch-size", type=int, default=0)
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--log-file")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    global logger
    args = _args(argv)
    trading_day = date.fromisoformat(args.date)
    symbols = parse_symbols(args.symbols)
    split_time = parse_until(trading_day, args.split_time)
    log_file = args.log_file or str(Path(args.report_dir) / "replay_unprocessed_multi.log")
    setup_logging(log_file=log_file)
    logger = logging.getLogger(__name__)
    batch_size = args.batch_size or None

    logger.info(
        "=== Auction restart comparison | date=%s symbols=%s split=%s ===",
        trading_day,
        symbols or "ALL",
        split_time,
    )

    continuous = run_replay(
        label="continuous",
        trading_day=trading_day,
        symbols=symbols,
        reset=True,
        batch_size=batch_size,
    )
    continuous_prefix = write_outcome_reports(
        continuous,
        report_dir=args.report_dir,
    )

    split_first = run_replay(
        label="split_part1",
        trading_day=trading_day,
        symbols=symbols,
        until_time=split_time,
        reset=True,
        batch_size=batch_size,
    )
    split_first_prefix = write_outcome_reports(
        split_first,
        report_dir=args.report_dir,
    )

    # Fresh runner/service/engine instances are created here. The only state
    # available to the second half is persisted signal/opportunity/checkpoint
    # data and the remaining unprocessed snapshots.
    split_second = run_replay(
        label="split_part2_restored",
        trading_day=trading_day,
        symbols=symbols,
        reset=False,
        batch_size=batch_size,
    )
    split_second_prefix = write_outcome_reports(
        split_second,
        report_dir=args.report_dir,
    )

    comparison = compare_replays(
        continuous=continuous,
        split_first=split_first,
        split_second=split_second,
    )
    comparison.update({
        "trading_day": trading_day.isoformat(),
        "symbols": symbols or [],
        "split_time": split_time.isoformat(sep=" ") if split_time else None,
        "continuous_reports": str(continuous_prefix),
        "split_first_reports": str(split_first_prefix),
        "split_second_reports": str(split_second_prefix),
    })
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    comparison_path = Path(args.report_dir) / (
        f"auction_restart_comparison_{trading_day}_{stamp}.json"
    )
    comparison_path.parent.mkdir(parents=True, exist_ok=True)
    comparison_path.write_text(
        json.dumps(comparison, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    logger.info("Auction restart comparison | %s", comparison["checks"])
    logger.info("Comparison report: %s", comparison_path)
    return 0 if comparison["passed"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
