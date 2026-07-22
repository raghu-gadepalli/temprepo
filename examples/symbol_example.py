#!/usr/bin/env python3
import logging
import os
import sys
from decimal import Decimal
from typing import Optional

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Shared logging setup
from logconfig import setup_logging
setup_logging()
logger = logging.getLogger(__name__)

from schemas.symbol import SymbolSchema


def test_create_symbol():
    test_symbol_data = {
        "symbol":            "TESTEQ",
        "token":             "TST123",
        "name":              "Test Equity",
        "type":              "EQ",                # one of EQ/FUT/CE/PE
        "price":             Decimal("100.00"),
        "exchange":          "NSE",
        "segment":           "NSE",
        "signal_profile":          "DEFAULT",
        "lotsize":           1,
        "expiry":            None,
        "strike_price":      None,
        "tick_size":         None,
        "equity_ref":        "TESTEQ",
        "last_time":         None,
        "last_snapshot":     None,
        # dynamic gates
        "generate_candles":    True,
        "merge_candles":       True,
        "update_performance":  True,
        "generate_signals":    True,
        "processed":           False,
        # long-lived flags
        "active":              True,
        "enabled":             True,  # policy gate on
    }
    logger.info("Creating symbol...")
    created = SymbolSchema.create_symbol(test_symbol_data)
    logger.info("Created Symbol: %s", created)
    return created


def test_fetch_symbol(symbol_str: str):
    logger.info("Fetching symbol '%s'...", symbol_str)
    sym = SymbolSchema.fetch_symbol(symbol_str)
    logger.info("Fetched Symbol: %s", sym)
    return sym


def test_fetch_symbols(active: Optional[int] = 1):
    logger.info("Fetching all symbols with active=%s (policy gate enabled only)...", active)
    syms = SymbolSchema.fetch_symbols(active=active)
    logger.info("Fetched Symbols count: %s", len(syms) if syms else 0)
    return syms


def test_update_symbol(symbol_str: str, new_name: str = "Updated Test Equity"):
    update_data = {
        "name":       new_name,
        "equity_ref": "TESTEQ",
    }
    logger.info("Updating symbol '%s' (name -> %r)...", symbol_str, new_name)
    updated = SymbolSchema.update_symbol(symbol_str, update_data)
    logger.info("Updated Symbol: %s", updated)
    return updated


def test_delete_symbol(symbol_str: str):
    logger.info("Soft deleting symbol '%s' (active -> False)...", symbol_str)
    result = SymbolSchema.delete_symbol(symbol_str)
    logger.info("Delete result: %s", result)
    return result


def test_disable_policy_gate(symbol_str: str):
    """
    Disable policy gate and show:
      - fetch_symbol returns None
      - update_symbol (without 'enabled' in payload) is skipped
    """
    logger.info("Disabling policy gate for '%s' (enabled -> False)...", symbol_str)
    _ = SymbolSchema.update_symbol(symbol_str, {"enabled": False})

    # Attempt an update that should be skipped because the symbol is disabled
    attempted_name = "Should Not Update"
    logger.info("Attempting update while disabled (name -> %r); should be skipped.", attempted_name)
    res = SymbolSchema.update_symbol(symbol_str, {"name": attempted_name})

    # res is returned from update_symbol even if skipped; verify name unchanged if we have it
    if res:
        if getattr(res, "name", None) == attempted_name:
            logger.warning("Unexpected: name changed while disabled.")
        else:
            logger.info("OK: update skipped while disabled (name remains %r).", getattr(res, "name", None))

    # Now fetch via the gated method (should be None)
    sym = SymbolSchema.fetch_symbol(symbol_str)
    logger.info("Fetch after disabling enabled flag (should be None): %s", sym)
    return sym


def test_reenable(symbol_str: str):
    logger.info("Re-enabling policy gate for '%s' (enabled -> True)...", symbol_str)
    _ = SymbolSchema.update_symbol(symbol_str, {"enabled": True})
    sym = SymbolSchema.fetch_symbol(symbol_str)
    logger.info("Fetched after re-enable: %s", sym)
    return sym


if __name__ == "__main__":
    SYMBOL = "TESTEQ"

    # 1) create
    created = test_create_symbol()

    # 2) fetch it
    fetched = test_fetch_symbol(SYMBOL)

    # 3) fetch all active (enabled-only implicit)
    _ = test_fetch_symbols(active=1)

    # 4) update
    updated = test_update_symbol(SYMBOL, "Updated Test Equity")

    # 5) soft-delete (active -> False, still enabled=True)
    _ = test_delete_symbol(SYMBOL)

    # 6) verify deactivated but still fetchable (enabled=True gate)
    post = test_fetch_symbol(SYMBOL)
    if not post or post.active is False:
        logger.info("Symbol correctly deactivated (active=False, enabled unaffected).")
    else:
        logger.warning("Symbol still active: %s", post)

    # 7) demonstrate policy gate (disable, then try updating & fetching)
    _ = test_disable_policy_gate(SYMBOL)

    # 8) re-enable and verify visible again; then update to prove writes work
    _ = test_reenable(SYMBOL)
    _ = test_update_symbol(SYMBOL, "Updated After Reenable")

    logger.info("Example run complete.")
