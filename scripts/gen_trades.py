#!/usr/bin/env python3
import logging
import time
import sys
import os
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

# ensure project root is on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from logconfig import setup_logging
from configs.trade_config import TRADE_CONFIG
from services.trade.generator.trade_generator import TradeGenerator

# centralized day gate (holidays/weekends/whitelist/blackout)
from utils.run_control import allow_run_today

# CONFIG
conf = TRADE_CONFIG
IST: ZoneInfo        = ZoneInfo("Asia/Kolkata")
START_TIME: dtime    = dtime.fromisoformat(conf.window_start)
END_TIME: dtime      = dtime.fromisoformat(conf.window_end)
RETRY_INTERVAL: int  = int(conf.retry_interval_seconds)
LOG_FILE: str        = conf.log_file

logger: logging.Logger = None  # set in main()
tg: TradeGenerator = None       # set in main()

def in_window(now: datetime) -> bool:
    t = now.astimezone(IST).time()
    return START_TIME <= t < END_TIME

def wait_for_window() -> bool:
    while True:
        now = datetime.now(IST)
        t = now.time()

        if t >= END_TIME:
            logger.info("Current time %s is past trade window end %s; exiting", t, END_TIME)
            return False

        if t >= START_TIME:
            logger.info("Reached trade window start %s; proceeding", START_TIME)
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

def tick(now: datetime):
    logger.debug("Trade tick @ %s", now.astimezone(IST))
    try:
        created = tg.generate_user_trades() or []

        labels = []
        for t in created:
            inst = getattr(getattr(t, "instrument_type", None), "value", None) or str(getattr(t, "instrument_type", "NA"))
            sym  = getattr(t, "symbol", "NA")
            labels.append(f"{inst}:{sym}")

        logger.info(
            "Trade tick: created=%d @ %s %s",
            len(created),
            now.astimezone(IST),
            f"sample={labels[:12]}" if labels else ""
        )
    except Exception:
        logger.exception("Error generating trades @ %s", now.astimezone(IST))
        
def main():
    global logger, tg
    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)
    tg = TradeGenerator()  # instantiate once

    if not allow_run_today(logger, "trade"):
        return

    logger.info("=== Trade Service starting ===")

    if not wait_for_window():
        return

    try:
        while True:
            now = datetime.now(IST)
            if not in_window(now):
                logger.info("Reached trade window end at %s; exiting", END_TIME)
                break

            tick(now)
            time.sleep(RETRY_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Interrupted; stopping")
    finally:
        logger.info("=== Trade Service stopped ===")

if __name__ == "__main__":
    main()