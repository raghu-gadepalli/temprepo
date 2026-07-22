#!/usr/bin/env python3
import logging
import os
import sys
from datetime import date
from typing import Optional

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
#  Shared logging setup 
from logconfig import setup_logging
setup_logging()
logger = logging.getLogger(__name__)

from schemas.instrument import InstrumentSchema
from schemas.symbol import SymbolSchema

# Default values for testing
DEFAULT_AS_OF_DATE = date(2025, 4, 25)
DEFAULT_FUT_PRICE = 1430.10


def test_fetch_all_instruments() -> None:
    instruments = InstrumentSchema.fetch_instruments()
    if not instruments:
        logger.warning("No instruments found.")
        return
    for inst in instruments[:5]:
        logger.info("Instrument: %s", inst.model_dump_json(indent=2))
    logger.info("and %d total", len(instruments))


def test_fetch_instrument(tradingsymbol: str) -> Optional[InstrumentSchema]:
    logger.info("Fetching instrument '%s'", tradingsymbol)
    inst = InstrumentSchema.fetch_instrument(tradingsymbol)
    if inst:
        logger.info(" Found: %s", inst.model_dump_json(indent=2))
    else:
        logger.warning(" Instrument '%s' not found", tradingsymbol)
    return inst


def test_fetch_closest_future(equity_symbol: str) -> Optional[InstrumentSchema]:
    logger.info("Fetching front-month FUT for '%s' as of %s", equity_symbol, DEFAULT_AS_OF_DATE)
    fut = InstrumentSchema.fetch_closest_future_for_equity(equity_symbol, DEFAULT_AS_OF_DATE)
    if fut:
        logger.info(" Future: %s", fut.model_dump_json(indent=2))
    else:
        logger.warning(" No future found for '%s'", equity_symbol)
    return fut


def test_fetch_closest_option(fut: InstrumentSchema) -> None:
    strike = fut.last_price or DEFAULT_FUT_PRICE
    as_of = DEFAULT_AS_OF_DATE

    logger.info("Fetching closest CE for '%s' at strike  %s as of %s",
                fut.tradingsymbol, strike, as_of)
    ce = InstrumentSchema.fetch_closest_option_for_future(
        fut.tradingsymbol,
        strike,
        as_of,
        is_buy=True,
    )
    if ce:
        logger.info(" CE: %s", ce.model_dump_json(indent=2))
    else:
        logger.warning(" No CE found.")

    logger.info("Fetching closest PE for '%s' at strike  %s as of %s",
                fut.tradingsymbol, strike, as_of)
    pe = InstrumentSchema.fetch_closest_option_for_future(
        fut.tradingsymbol,
        strike,
        as_of,
        is_buy=False,
    )
    if pe:
        logger.info(" PE: %s", pe.model_dump_json(indent=2))
    else:
        logger.warning(" No PE found.")


def test_create_symbol_from_instrument(tradingsymbol: str) -> None:
    logger.info("Upserting symbol from instrument '%s'", tradingsymbol)
    inst = InstrumentSchema.fetch_instrument(tradingsymbol)
    if not inst:
        logger.warning(" Instrument '%s' not found", tradingsymbol)
        return

    # Create a Symbol ORM from the instrument, then upsert via our schema
    sym_orm = InstrumentSchema.create_symbol_from_instrument(inst)
    payload = {
        "symbol":            sym_orm.symbol,
        "token":             sym_orm.token,
        "name":              sym_orm.name,
        "type":              sym_orm.type,
        "price":             getattr(sym_orm, "last_price", None),
        "exchange":          sym_orm.exchange,
        "signal_profile":          sym_orm.signal_profile,
        "generate_candles":  sym_orm.generate_candles,
        "merge_candles":     sym_orm.merge_candles,
        "update_performance":sym_orm.update_performance,
        "generate_signals":  sym_orm.generate_signals,
        "lotsize":           sym_orm.lotsize,
        "expiry":            sym_orm.expiry,
        "processed":         sym_orm.processed,
        "active":            sym_orm.active,
        "equity_ref":        sym_orm.equity_ref,
    }
    sym = SymbolSchema.create_symbol(payload)
    if sym:
        logger.info(" Upserted symbol: %s", sym.model_dump_json(indent=2))
    else:
        logger.error(" Failed to upsert symbol '%s'", payload["symbol"])


if __name__ == "__main__":
    # You can uncomment any test you want to run:
    # test_fetch_all_instruments()
    # test_fetch_instrument("INFY")

    # For this demo:
    inst = test_fetch_instrument("INFY25MAYFUT")
    if inst:
        fut = test_fetch_closest_future("INFY")
        if fut:
            test_fetch_closest_option(fut)
            test_create_symbol_from_instrument(fut.tradingsymbol)

    logger.info(" Instrument example completed.")
