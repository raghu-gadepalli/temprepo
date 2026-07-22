#!/usr/bin/env python3
import logging
import os
import sys
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from logconfig import setup_logging
from configs.monitor_config import MONITOR_CONFIG
from services.trade.monitor.trade_monitor import TradeMonitor

# centralized day gate (holidays/weekends/whitelist/blackout)
from utils.run_control import allow_run_today

conf = MONITOR_CONFIG
IST            = ZoneInfo("Asia/Kolkata")
START_TIME     = dtime.fromisoformat(conf.window_start)
END_TIME       = dtime.fromisoformat(conf.window_end)
LOG_FILE       = conf.log_file

EXTRAS         = conf
ALIGN_SEC      = int(conf.retry_interval_seconds)

logger = None


def in_window(now: datetime) -> bool:
    t = now.astimezone(IST).time()
    return START_TIME <= t < END_TIME


def wait_for_window() -> bool:
    while True:
        now = datetime.now(IST)
        t = now.time()

        if t >= END_TIME:
            logger.info("Current time %s is past monitor window end %s; exiting", t, END_TIME)
            return False

        if t >= START_TIME:
            logger.info("Reached monitor window start %s; proceeding", START_TIME)
            return True

        start_dt = now.replace(
            hour=START_TIME.hour,
            minute=START_TIME.minute,
            second=START_TIME.second,
            microsecond=0,
        )
        remaining = (start_dt - now).total_seconds()
        step = 30 if remaining > 120 else 5
        logger.info(
            "Window not open yet (%s). Sleeping %d sec (remaining: %.0f)...",
            START_TIME, step, remaining
        )
        time.sleep(step)


def sleep_until_next_boundary(now: datetime, interval_sec: int):
    """
    Align to wall clock boundaries.
    Example for 30s: hh:mm:00, hh:mm:30, hh:mm+1:00, ...
    """
    if interval_sec <= 0:
        time.sleep(1)
        return

    # compute next boundary strictly in the future
    # (if we are exactly on boundary, next is +interval)
    sec = now.second
    micro = now.microsecond

    remainder = sec % interval_sec
    add = (interval_sec - remainder) if remainder != 0 else interval_sec

    next_dt = now.replace(microsecond=0) + timedelta(seconds=add)
    sleep_s = (next_dt - now).total_seconds()

    if sleep_s < 0.001:
        sleep_s = 0.001
    time.sleep(sleep_s)


def main():
    global logger
    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)

    if not allow_run_today(logger, "monitor"):
        return

    logger.info("=== Trade Monitor Service starting ===")
    logger.info("tick_align_seconds=%s window=%s..%s", ALIGN_SEC, START_TIME, END_TIME)

    if not wait_for_window():
        return

    tm = TradeMonitor()  # instantiate ONCE

    try:
        while True:
            now = datetime.now(IST)
            if not in_window(now):
                logger.info("Reached monitor window end at %s; exiting", END_TIME)
                break

            try:
                updated = tm.monitor() or 0
                if updated > 0:
                    logger.info("Monitor tick updated=%d @ %s", updated, now.strftime("%H:%M:%S"))
                else:
                    logger.debug("Monitor tick updated=0 @ %s", now.strftime("%H:%M:%S"))
            except Exception:
                logger.exception("Error during monitor tick @ %s", now.astimezone(IST))

            # align to :00/:30 boundaries (or configured)
            sleep_until_next_boundary(datetime.now(IST), ALIGN_SEC)

    except KeyboardInterrupt:
        logger.info("Interrupted; stopping")
    finally:
        logger.info("=== Trade Monitor Service stopped ===")


if __name__ == "__main__":
    main()