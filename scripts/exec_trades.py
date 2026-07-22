#!/usr/bin/env python3
"""
scripts/exec/trades.py  (vNext runner, multi-thread per user)

Assumptions:
- TradeExecutor.execute_user_once(userid=..., limit=...) ALWAYS does:
    1) reconciliation first (even if no actionable trades)
    2) then execution actions (place/poll entry/exit)
- Runner does NOT call reconcile explicitly.

Behaviors:
- 1 thread per user (bounded by max_workers)
- Waits for all user tasks to finish BEFORE sleeping
- No align-to-tick
- Uses EXECUTION_CONFIG directly
"""

import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime
from typing import Optional, Tuple

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from logconfig import setup_logging
from configs.execution_config import EXECUTION_CONFIG
from schemas.user import UserSchema
from services.trade.executor.trade_executor import TradeExecutor
from utils.run_control import allow_run_today
from utils.datetime_utils import IST  # ✅ use common IST everywhere

# ----------------------------
# CONFIG (direct, no picker)
# ----------------------------
conf = EXECUTION_CONFIG

START_TIME = dtime.fromisoformat(conf.window_start)
END_TIME = dtime.fromisoformat(conf.window_end)
RETRY_INTERVAL = int(conf.retry_interval_seconds)
LOG_FILE = conf.log_file

MAX_WORKERS = int(conf.max_workers)
LIMIT = int(conf.limit)           # per-user candidate limit

logger: Optional[logging.Logger] = None


# ----------------------------
# TIME HELPERS
# ----------------------------
def in_window(now: datetime) -> bool:
    t = now.astimezone(IST).time()
    return START_TIME <= t < END_TIME


def wait_for_window() -> bool:
    """Block until window opens; exit if already past END."""
    while True:
        now = datetime.now(IST)
        t = now.time()

        if t >= END_TIME:
            logger.info("Current time %s is past execution window end %s; exiting", t, END_TIME)
            return False

        if t >= START_TIME:
            logger.info("Reached execution window start %s; proceeding", START_TIME)
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


# ----------------------------
# PER-USER TASK
# ----------------------------
def _run_user(userid: str, limit: int) -> Tuple[str, int]:
    """
    Returns: (userid, acted_count)
    acted_count includes reconcile updates + execution updates, as defined by executor.
    """
    ex = TradeExecutor()
    acted = ex.execute_user_once(userid=userid, limit=limit)
    return userid, int(acted or 0)


# ----------------------------
# TICK (multi-thread)
# ----------------------------
def tick(now: datetime):
    t0 = time.perf_counter()

    # Same "tradeable user" logic as generator: active + logged_in
    users = UserSchema.fetch_tradeable_users()
    users = [
        u for u in users
        if int(getattr(u, "active", 0) or 0) == 1
        and int(getattr(u, "logged_in", 0) or 0) == 1
    ]

    if not users:
        logger.info("Exec tick @ %s | no logged-in active users", now.astimezone(IST).strftime("%H:%M:%S"))
        return

    logger.info(
        "Exec tick @ %s | users=%d max_workers=%d limit=%d",
        now.astimezone(IST).strftime("%H:%M:%S"),
        len(users),
        MAX_WORKERS,
        LIMIT,
    )

    total_acted = 0
    failed = 0

    maxw = min(MAX_WORKERS, max(1, len(users)))

    # One thread per user (bounded). Wait for all users before sleeping.
    with ThreadPoolExecutor(max_workers=maxw) as pool:
        futs = {pool.submit(_run_user, u.userid, LIMIT): u.userid for u in users}

        for fut in as_completed(futs):
            userid = futs[fut]
            try:
                uid, acted = fut.result()
                total_acted += acted
                logger.debug("User tick done userid=%s acted=%d", uid, acted)
            except Exception:
                failed += 1
                logger.exception("User tick failed userid=%s", userid)

    elapsed = time.perf_counter() - t0
    logger.info(
        "Tick done: acted=%d failed_users=%d elapsed=%.3fs",
        total_acted, failed, elapsed
    )


# ----------------------------
# MAIN
# ----------------------------
def main():
    global logger
    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)

    if not allow_run_today(logger, "execution"):
        return

    logger.info(
        "=== Trade Executor Service starting (window=%s-%s; interval=%ss; max_workers=%d; limit=%d) ===",
        START_TIME, END_TIME, RETRY_INTERVAL, MAX_WORKERS, LIMIT
    )

    if not wait_for_window():
        return

    try:
        while True:
            now = datetime.now(IST)

            if not in_window(now):
                logger.info("Reached execution window end at %s; exiting", END_TIME)
                break

            tick(now)

            # Sleep only AFTER all user threads finish
            time.sleep(RETRY_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Interrupted; stopping")
    finally:
        logger.info("=== Trade Executor Service stopped ===")


if __name__ == "__main__":
    main()