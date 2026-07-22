#!/usr/bin/env python3
import argparse
import logging
import os
import sys
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Optional, Set

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from logconfig import setup_logging
from config import AppConfig
from configs.snapshot_config import SNAPSHOT_CONFIG
from schemas.user import UserSchema
from schemas.symbol import SymbolSchema
from services.snapshot.snapshot_generator import SnapshotGenerator

# NEW: centralized day gate (holidays/weekends/whitelist/blackout)
from utils.run_control import allow_run_today

# ----------------------------
# CONFIG (NO hardcoded times)
# ----------------------------
# Use the typed Pydantic config directly. SnapshotServiceConfig no longer has an
# `extras` bucket, so max_workers/tick_minutes must come from service fields.
service_conf = SNAPSHOT_CONFIG.service
IST = ZoneInfo("Asia/Kolkata")

START_TIME      = dtime.fromisoformat(service_conf.window_start)            # e.g. "09:16:00"
END_TIME        = dtime.fromisoformat(service_conf.window_end)              # e.g. "15:31:00"
RETRY_INTERVAL  = int(service_conf.retry_interval_seconds)                  # logging only
LOG_FILE        = service_conf.log_file
MAX_WORKERS     = int(service_conf.max_workers)
TICK_MINUTES    = int(service_conf.tick_minutes)                            # cadence from config

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
            logger.info("Current time %s is past snapshot window end %s; exiting", t, END_TIME)
            return False

        if t >= START_TIME:
            logger.info("Reached snapshot window start %s; proceeding", START_TIME)
            return True

        start_dt = now.replace(
            hour=START_TIME.hour, minute=START_TIME.minute,
            second=START_TIME.second, microsecond=0
        )
        remaining = (start_dt - now).total_seconds()
        step = 30 if remaining > 120 else 5
        logger.debug("Window not open yet (%s). Sleeping %d sec (remaining: %.0f)...",
                     START_TIME, step, remaining)
        time.sleep(step)


def sleep_to_next_tick(minutes: int):
    """
    Sleep until the next aligned tick boundary.
    For 3-minute ticks: aligns to minute % 3 == 0 (seconds=0).
    Example: 09:16 -> next tick 09:18:00
    """
    now = datetime.now(IST)
    base = now.replace(second=0, microsecond=0)
    m = base.minute

    add = minutes - (m % minutes)
    if add == 0 and now.second == 0:
        add = minutes

    next_tick = base + timedelta(minutes=add)
    remaining = (next_tick - now).total_seconds()
    if remaining > 0:
        time.sleep(remaining)


# ----------------------------
# TOKEN HELPERS
# ----------------------------
def _safe_int_token(token) -> Optional[int]:
    try:
        if token in (None, "", "None"):
            return None
        return int(token)
    except (TypeError, ValueError):
        return None


# ----------------------------
# WORKER TASK
# ----------------------------
def _generate_one(token, symbol, api_key, access_token, now: datetime):
    """
    Child process task: generate & persist one symbol snapshot.
    Returns snapshot result or None on error; never raises.
    """
    try:
        token_int = _safe_int_token(token)
        if token_int is None:
            logging.warning("Snapshot skipped in worker: invalid token for %s (token=%r)", symbol, token)
            return None

        gen = SnapshotGenerator(
            token=token_int,
            symbol=symbol,
            api_key=api_key,
            access_token=access_token,
        )
        return gen.generate_snapshot(end_date=now)

    except Exception:
        logging.exception("Snapshot error in worker for %s", symbol)
        return None


# ----------------------------
# TICK
# ----------------------------
def tick(now: datetime, *, only_symbols: Optional[Set[str]] = None):
    """
    1) Load DATA_USER creds
    2) Fetch active EQ symbols ONLY
    3) Run ALL of them in parallel (process pool) in this tick
    """
    t0 = time.perf_counter()

    # 1) credentials
    user = UserSchema.fetch_user(AppConfig.DATA_USER)
    if not user:
        logger.error("No DATA_USER %s", AppConfig.DATA_USER)
        return

    # 2) symbols (EQ ONLY)
    symbols = SymbolSchema.fetch_symbols(active=1, type_filter="EQ")
    if not symbols:
        logger.warning("No active EQ symbols")
        return

    # Defensive: never process non-EQ even if DB has bad data
    eq_symbols = [s for s in symbols if (getattr(s, "type", "") or "").upper() == "EQ"]
    if only_symbols:
        eq_symbols = [
            s for s in eq_symbols
            if str(getattr(s, "symbol", "") or "").strip().upper() in only_symbols
        ]

    # Filter bad tokens early
    due = []
    bad_tok = 0
    for s in eq_symbols:
        if _safe_int_token(getattr(s, "token", None)) is None:
            bad_tok += 1
            logger.warning("Skipping %s due to missing/invalid token (token=%r)", s.symbol, s.token)
            continue
        due.append(s)

    logger.info(
        "Snapshot tick @ %s | EQ_total=%d due=%d bad_token=%d tick_minutes=%d max_workers=%d",
        now.astimezone(IST).strftime("%H:%M:%S"),
        len(eq_symbols), len(due), bad_tok, TICK_MINUTES, MAX_WORKERS
    )

    if not due:
        return

    ok = 0
    fail = 0

    # 3) parallelize
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as exe:
        futures = {
            exe.submit(
                _generate_one,
                sym.token, sym.symbol,
                user.apikey, user.access_token,
                now,
            ): sym.symbol
            for sym in due
        }

        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                res = fut.result()
                if res:
                    ok += 1
                else:
                    fail += 1
            except Exception:
                fail += 1
                logger.exception("Snapshot failed for %s", sym)

    elapsed = time.perf_counter() - t0
    logger.info("Tick done: ok=%d fail=%d elapsed=%.3fs (interval_hint=%ds)", ok, fail, elapsed, RETRY_INTERVAL)

    # If tick > tick_minutes, cadence can slip
    if elapsed > (TICK_MINUTES * 60):
        logger.warning(
            "Tick exceeded %d minutes (elapsed=%.3fs). You will miss cadence. "
            "Reduce symbol count, raise max_workers, or optimize generator.",
            TICK_MINUTES, elapsed
        )


# ----------------------------
# MAIN
# ----------------------------
def _parse_args():
    parser = argparse.ArgumentParser(description="Generate and persist EQ snapshots")
    parser.add_argument(
        "--symbols",
        nargs="+",
        help="Optional symbol list, for example: --symbols COFORGE",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one snapshot tick immediately instead of starting the service loop",
    )
    return parser.parse_args()


def main():
    global logger
    args = _parse_args()
    only_symbols = (
        {str(item).strip().upper() for item in args.symbols if str(item).strip()}
        if args.symbols
        else None
    )
    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)

    # NEW: global day gate via config (whitelist overrides; else holidays/weekends/blackout skip)
    if not allow_run_today(logger, "snapshot"):
        return

    logger.info(
        "=== Snapshot Service starting (EQ only; window=%s-%s; cadence=%dm; max_workers=%d; symbols=%s) ===",
        START_TIME, END_TIME, TICK_MINUTES, MAX_WORKERS,
        sorted(only_symbols) if only_symbols else "ALL",
    )

    if args.once:
        now = datetime.now(IST)
        tick(now, only_symbols=only_symbols)
        logger.info("=== One-shot snapshot run complete ===")
        return

    if not wait_for_window():
        return

    # OPTION-1: window can open at 09:16, but we align the FIRST tick to next 3-min boundary (09:18, 09:21, ...)
    sleep_to_next_tick(TICK_MINUTES)

    try:
        while True:
            now = datetime.now(IST)

            if not in_window(now):
                logger.info("Reached snapshot window end at %s; exiting", END_TIME)
                break

            tick(now, only_symbols=only_symbols)
            sleep_to_next_tick(TICK_MINUTES)

    except KeyboardInterrupt:
        logger.info("Interrupted; stopping")
    finally:
        logger.info("=== Snapshot Service stopped ===")


if __name__ == "__main__":
    main()