#!/usr/bin/env python3
"""Run the once-daily stockscan selector.

This service waits until SCANNER_CONFIG.scan.run_time, scans the enabled EQ
universe using first 1-minute candle data, applies the selected active basket,
and exits. It does not loop and does not promote/demote generate_signals.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, time as dtime
from typing import Optional

# ensure project root on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from configs.scanner_config import SCANNER_CONFIG
from logconfig import setup_logging
from services.selection.stockscan import StockScanner
from utils.datetime_utils import IST
from utils.run_control import allow_run_today

SCAN = SCANNER_CONFIG.scan
RUN_TIME = dtime.fromisoformat(SCAN.run_time)
LOG_FILE = SCAN.log_file

logger: Optional[logging.Logger] = None


def wait_for_run_time() -> bool:
    """Block until the configured run time; exit if already too late."""
    while True:
        now = datetime.now(IST)
        t = now.time()

        if t >= RUN_TIME:
            logger.info("Reached stockscan run time %s; proceeding", RUN_TIME)
            return True

        run_dt = now.replace(
            hour=RUN_TIME.hour,
            minute=RUN_TIME.minute,
            second=RUN_TIME.second,
            microsecond=0,
        )
        remaining = (run_dt - now).total_seconds()
        step = 30 if remaining > 120 else 5
        logger.info(
            "Stockscan run time not reached (%s). Sleeping %d sec (remaining %.0f)...",
            RUN_TIME,
            step,
            remaining,
        )
        time.sleep(step)


def _log_scan_summary(now_ist: datetime, res: dict):
    stats = res.get("stats") or {}
    selected = res.get("selected") or []
    selected_whitelist = res.get("selected_whitelist") or []
    selected_dynamic = res.get("selected_dynamic") or []
    update_result = res.get("update_result") or {}

    logger.info(
        "Stockscan @ %s | selected=%d whitelist=%d dynamic=%d universe=%d candidates=%d missing=%d invalid=%d activated_db=%s",
        now_ist.strftime("%H:%M:%S"),
        len(selected),
        len(selected_whitelist),
        len(selected_dynamic),
        int(stats.get("universe", 0)),
        int(stats.get("candidates", 0)),
        int(stats.get("missing_candle", 0)),
        int(stats.get("invalid_candle", 0)),
        update_result.get("activated_count"),
    )

    if selected_whitelist:
        logger.info("Stockscan whitelist active: %s", ", ".join(selected_whitelist))
    if selected_dynamic:
        logger.info("Stockscan selected dynamic: %s", ", ".join(selected_dynamic))

    top_candidates = (res.get("candidates") or [])[:20]
    for row in top_candidates:
        logger.debug(
            "candidate %-14s score=%.3f dir=%s gap=%.3f day=%.3f bar=%.3f range=%.3f turnover_lakh=%.1f",
            row.get("symbol"),
            float(row.get("score") or 0.0),
            row.get("direction"),
            float(row.get("gap_pct") or 0.0),
            float(row.get("day_move_pct") or 0.0),
            float(row.get("candle_move_pct") or 0.0),
            float(row.get("candle_range_pct") or 0.0),
            float(row.get("turnover_lakh") or 0.0),
        )


def main():
    global logger
    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)

    if not allow_run_today(logger, "stockscan"):
        return

    logger.info(
        "=== Stockscan once-daily selector starting | run_time=%s | active_limit=%d | whitelist=%d ===",
        RUN_TIME,
        SCAN.daily_active_limit,
        len(SCANNER_CONFIG.universe.whitelist),
    )

    if not wait_for_run_time():
        return

    try:
        now = datetime.now(IST)
        scanner = StockScanner()
        res = scanner.generate_scan(now, apply_updates=True)
        _log_scan_summary(now, res)
    except Exception:
        logger.exception("Stockscan failed")
        raise
    finally:
        logger.info("=== Stockscan once-daily selector stopped ===")


if __name__ == "__main__":
    main()
