#!/usr/bin/env python3
"""Single-owner Auction persistence replay.

This replaces the legacy signal/trade replay in the AutoLabs clone. It processes
only unprocessed snapshots and marks each row processed after signal,
opportunity, and checkpoint persistence succeeds. Trades are not part of this
layer.

Examples:

Clean full run for selected symbols::

    python tests/replay_unprocessed.py --date 2026-07-20 \
        --symbols COFORGE,POLYCAB --reset

First half of a restart test::

    python tests/replay_unprocessed.py --date 2026-07-20 \
        --symbols COFORGE,POLYCAB --reset --until 12:00

Resume in a new process::

    python tests/replay_unprocessed.py --date 2026-07-20 \
        --symbols COFORGE,POLYCAB
"""
from __future__ import annotations

import argparse
from datetime import date
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
    parse_symbols,
    parse_until,
    run_replay,
    write_outcome_reports,
)

logger = logging.getLogger(__name__)


def _args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay unprocessed snapshots through Auction signal persistence."
    )
    parser.add_argument("--date", required=True, help="Trading day YYYY-MM-DD")
    parser.add_argument("--symbols", help="Optional comma-separated symbols")
    parser.add_argument(
        "--until",
        help="Optional inclusive stop time: HH:MM, HH:MM:SS, or ISO datetime",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Reset processed flags, selected-symbol signals, opportunities, and "
            "checkpoints before starting. Trades are untouched."
        ),
    )
    parser.add_argument("--batch-size", type=int, default=0)
    parser.add_argument("--label", default="run")
    parser.add_argument(
        "--profile-timing",
        action="store_true",
        help=(
            "Collect per-snapshot stage timings without changing processing or "
            "persistence behavior."
        ),
    )
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--log-file")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    global logger
    args = _args(argv)
    trading_day = date.fromisoformat(args.date)
    symbols = parse_symbols(args.symbols)
    until_time = parse_until(trading_day, args.until)
    log_file = args.log_file or str(Path(args.report_dir) / "replay_unprocessed.log")
    setup_logging(log_file=log_file)
    logger = logging.getLogger(__name__)

    logger.info(
        "=== Auction persistence replay | date=%s symbols=%s until=%s reset=%s ===",
        trading_day,
        symbols or "ALL",
        until_time,
        bool(args.reset),
    )
    if args.profile_timing and symbols and len(symbols) != 1:
        logger.warning(
            "Timing profile is clearest with one symbol; requested symbols=%s",
            symbols,
        )
    outcome = run_replay(
        label=str(args.label).strip() or "run",
        trading_day=trading_day,
        symbols=symbols,
        until_time=until_time,
        reset=bool(args.reset),
        batch_size=(args.batch_size or None),
        profile_timing=bool(args.profile_timing),
    )
    prefix = write_outcome_reports(outcome, report_dir=args.report_dir)
    logger.info("Auction persistence replay complete | %s", outcome.manifest)
    if args.profile_timing:
        ranked = sorted(
            (row for row in outcome.runner.timing_summary() if row["stage"] != "total"),
            key=lambda row: row["total_ms"],
            reverse=True,
        )
        logger.info("Timing profile dominant stages | %s", ranked[:6])
    logger.info("Reports: %s_*", prefix)

    if outcome.runner.stats.errors:
        return 2
    if outcome.processed_counts["unprocessed"] and until_time is None:
        logger.error(
            "Full replay ended with unprocessed snapshots | %s",
            outcome.processed_counts,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
