#!/usr/bin/env python3
# services/utils/generate_symbols.py

import logging
import os
import sys
from datetime import date

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
#  Shared logging setup 
from logconfig import setup_logging
setup_logging()
logger = logging.getLogger(__name__)

from schemas.instrument import InstrumentSchema
from schemas.symbol     import SymbolSchema

# map humanfriendly names  Kite root symbols for future lookup
ROOT_MAP = {
    "NIFTY 50":   "NIFTY",
    "NIFTY BANK": "BANKNIFTY",
}

# your static watchlist of equities (human names)
WATCHLIST = [
    "NIFTY 50",
    "NIFTY BANK",
    "SBIN",
    "ICICIBANK",
    "HDFCBANK",
    "AXISBANK",
    "KOTAKBANK",
    "INDUSINDBK",
    "TCS",
    "INFY",
    "TECHM",
    "RELIANCE",
    "TATAMOTORS",
    "MARUTI",
    "SUNPHARMA",
]


def upsert_symbol(inst: InstrumentSchema, equity_ref: str = None):
    """
    Given an InstrumentSchema `inst`, upsert a row in symbols.
    If `equity_ref` is provided, store it for derivatives.
    """
    data = {
        "symbol":           inst.tradingsymbol,
        "token":            inst.instrument_token,
        "name":             inst.name,
        "type":             inst.instrument_type,
        "exchange":         inst.exchange,
        "signal_profile":         "DEFAULT",
        "generate_candles":     1,
        "merge_candles":        1,
        "update_performance":   1,
        "generate_signals":     1,
        "lotsize":          inst.lot_size or 0,
        "expiry":           inst.expiry,
        "processed":        0,
        "active":           1,
        "equity_ref":       equity_ref,
    }

    existing = SymbolSchema.fetch_symbol(data["symbol"])
    if existing:
        update_data = {"active": 1}
        if equity_ref is not None:
            update_data["equity_ref"] = equity_ref
        sym = SymbolSchema.update_symbol(data["symbol"], update_data)
        logger.info("Re-activated symbol: %s", sym.symbol)
    else:
        sym = SymbolSchema.create_symbol(data)
        logger.info("Created symbol:    %s", sym.symbol)
    return sym


def main():
    today = date.today()
    logger.info("Generating symbols for watchlist as of %s\n", today)

    for human in WATCHLIST:
        logger.info("Processing equity '%s'", human)

        # 1) Equity lookup by the humanfriendly name
        inst_eq = InstrumentSchema.fetch_instrument(human)
        if not inst_eq:
            logger.warning(" Equity instrument '%s' not found, skipping.", human)
            logger.info("")
            continue

        upsert_symbol(inst_eq)

        # 2) Future lookup uses the ROOT_MAP key (e.g. "NIFTY" / "BANKNIFTY")
        root = ROOT_MAP.get(human, human)
        fut = InstrumentSchema.fetch_closest_future_for_equity(root, as_of=today)
        if not fut:
            logger.warning(" No front-month FUT for '%s' (used '%s'), skipping FUT.", human, root)
            logger.info("")
            continue

        logger.info("  Found FUT: %s (expiry %s)", fut.tradingsymbol, fut.expiry)
        upsert_symbol(fut, equity_ref=human)
        logger.info("")

    logger.info(" Watchlist symbol generation complete.")


if __name__ == "__main__":
    main()