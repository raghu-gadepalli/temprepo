#!/usr/bin/env python3
import logging
import os
import sys
from datetime import datetime
from kiteconnect.exceptions import TokenException

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
#  Shared logging setup 
from logconfig import setup_logging
setup_logging()
logger = logging.getLogger(__name__)
logging.getLogger("services.zerodha.kiteconnect_service").setLevel(logging.ERROR)

from services.snapshot.snapshot_generator import SnapshotGenerator

#  Configuration 
TOKEN        = 3329                    # instrument token
SYMBOL       = "ABB"                    # trading symbol
API_KEY      = "bv185n0541aaoish"        # your Kite API key
ACCESS_TOKEN = "JL937N6CTsP34GT4Tp2bcc9Fv8Jw50Uj"  # your Kite access token

def main():
    # parse an example end timestamp (must include offset)
    try:
        end_date = datetime.fromisoformat("2025-08-04T10:15:00+05:30")
    except ValueError as e:
        logger.error("Invalid date format: %s", e)
        sys.exit(1)

    gen = SnapshotGenerator(TOKEN, SYMBOL, API_KEY, ACCESS_TOKEN)

    try:
        snap = gen.generate_snapshot(
            end_date         = end_date,
            persist_candle   = True,
            persist_snapshot = True,
        )
    except TokenException:
        # <-- NO exc_info, so no stack
        logger.critical("Invalid API Key or access token. Please verify your credentials.")
        sys.exit(1)

    if not snap:
        logger.warning("No snapshot returned. Check token/symbol or data range.")
        sys.exit(0)

    # success: print the snapshot JSON
    print(snap.model_dump_json(indent=2))

if __name__ == "__main__":
    main()
