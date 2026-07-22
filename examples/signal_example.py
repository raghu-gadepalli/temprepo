#!/usr/bin/env python3
"""
examples/signal_example.py

Exercise the CRUD methods on SignalSchema with logging instead of print.
"""

import logging
import os
import sys
from datetime import datetime
from decimal import Decimal

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
#  Shared logging setup 
from logconfig import setup_logging
setup_logging()
logger = logging.getLogger(__name__)

from schemas.signal import SignalSchema
from enums.enums import SymbolType, TradeType

# Use a fixed UUID for simplicity
TEST_SIGNAL_ID = "fixed-test-signal-001"
SYMBOL         = "NIFTY"
STRATEGY       = "TEST_STRAT"


def test_create_signal() -> str:
    sig = SignalSchema(
        signal_id       = TEST_SIGNAL_ID,
        symbol          = SYMBOL,
        instrument_type = SymbolType.EQ,
        strategy_name   = STRATEGY,
        trade_type      = TradeType.BUY,
        entry_time      = datetime.now(),
        entry_price     = Decimal("17500.25"),
        entry_snapshot  = {},          # dummy
        last_time       = datetime.now(),
        last_price      = 17500.25,
        last_snapshot   = {},
        exited          = False,
        processed       = False,
        active          = True,
        quantity        = 1,
        source          = "autotrades",
        message         = "",
    )
    logger.info("Creating signal")
    created = SignalSchema.create_signal(sig)
    logger.info("Created signal:\n%s", created.model_dump_json(indent=4))
    return created.signal_id


def test_fetch_signal(signal_id: str) -> None:
    logger.info("Fetching any signal with ID '%s'", signal_id)
    fetched = SignalSchema.fetch_signal(signal_id)
    if fetched:
        logger.info("Fetched signal:\n%s", fetched.model_dump_json(indent=4))
    else:
        logger.warning("No signal found with ID '%s'.", signal_id)


def test_update_signal(signal_id: str) -> None:
    update_data = {
        "last_time": datetime.now(),
        "last_price": 17505.75,
        "exited": True,
        "pnl": Decimal("5.50"),
        "processed": True,
    }
    logger.info("Updating signal '%s'", signal_id)
    updated = SignalSchema.update_signal(signal_id, update_data)
    if updated:
        logger.info("Updated signal:\n%s", updated.model_dump_json(indent=4))
    else:
        logger.warning("Update failed; signal '%s' not found.", signal_id)


def test_delete_signal(signal_id: str) -> None:
    logger.info("Deleting signal '%s'", signal_id)
    success = SignalSchema.delete_signal(signal_id)
    if success:
        logger.info("Deleted signal '%s' successfully.", signal_id)
    else:
        logger.warning("Failed to delete signal '%s'; not found.", signal_id)


if __name__ == "__main__":
    # Create (and overwrite TEST_SIGNAL_ID if desired)
    sid = test_create_signal()

    # Or just use the fixed ID:
    sid = TEST_SIGNAL_ID

    test_fetch_signal(sid)
    # Uncomment to test update and delete flows:
    # test_update_signal(sid)
    # test_fetch_signal(sid)   # to see the updated state
    # test_delete_signal(sid)
    # test_fetch_signal(sid)   # now should be inactive / not found
