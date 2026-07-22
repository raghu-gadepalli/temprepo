import os, sys
import logging
from datetime import datetime
from typing import Optional

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
#  Shared logging setup 
from logconfig import setup_logging
setup_logging()
# get a modulescoped logger
logger = logging.getLogger(__name__)

from schemas.candle import CandleSchema

def test_create_candle():
    test_candle_data = {
        "symbol": "AXISBANK",
        "frequency": 1,            # 1-minute candle
        "candle_time": datetime.now().replace(second=0, microsecond=0),
        "open": 405.50,
        "high": 407.00,
        "low": 405.00,
        "close": 406.00,
        "volume": 15000.00,
        "oi": 2000.00,
        "active": True
    }
    logger.info("Creating candle")
    created = CandleSchema.create_candle(test_candle_data)
    logger.info("Created Candle: %r", created)
    return created

def test_fetch_candle(candle_str: str, frequency: int, candle_time: datetime):
    logger.info(
        "Fetching single candle %s @ freq %d and time %s",
        candle_str, frequency, candle_time
    )
    c = CandleSchema.fetch_candle(candle_str, frequency, candle_time)
    logger.info("Fetched Candle: %r", c)
    return c

def test_fetch_candles(active: Optional[bool] = True):
    logger.info("Fetching all candles (active=%s)", active)
    lst = CandleSchema.fetch_candles(active=active)
    logger.info("Fetched %d candles", len(lst) if lst else 0)
    return lst

def test_update_candle(candle_str: str, frequency: int, candle_time: datetime):
    update_data = {"close": 407.00, "volume": 15500.00}
    logger.info(
        "Updating candle %s @ freq %d, time %s with %r",
        candle_str, frequency, candle_time, update_data
    )
    c = CandleSchema.fetch_candle(candle_str, frequency, candle_time)
    if not c:
        logger.warning("No candle found to update for %s/%d @ %s", candle_str, frequency, candle_time)
        return None
    updated = CandleSchema.update_candle(c.id, update_data)
    logger.info("Updated Candle: %r", updated)
    return updated

def test_delete_candle(candle_str: str, frequency: int, candle_time: datetime):
    logger.info(
        "Softdeleting candle %s @ freq %d and time %s",
        candle_str, frequency, candle_time
    )
    c = CandleSchema.fetch_candle(candle_str, frequency, candle_time)
    if not c:
        logger.warning("No candle found to delete for %s/%d @ %s", candle_str, frequency, candle_time)
        return None
    msg = CandleSchema.delete_candle(c.id)
    logger.info("Delete result: %s", msg)
    return msg

if __name__ == "__main__":
    # 1. Create
    created = test_create_candle()
    # 2. Fetch single (customize args if needed)
    # test_fetch_candle("AXISBANK", 1, created.candle_time)
    # 3. Fetch all
    # test_fetch_candles(active=True)
    # 4. Update
    # test_update_candle("AXISBANK", 1, created.candle_time)
    # 5. Delete
    # test_delete_candle("AXISBANK", 1, created.candle_time)
