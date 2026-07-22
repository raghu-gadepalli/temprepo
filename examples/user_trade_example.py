import logging
import os
import sys
from datetime import datetime
from decimal import Decimal

# allow imports from project root
sys.path.insert(0,os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Shared logging setup
from logconfig import setup_logging
setup_logging()
logger = logging.getLogger(__name__)

from schemas.user_trade import UserTradeSchema
from enums.enums         import SymbolType, TradeType, TradeStatus

def main():
    userid    = "AT1234"
    signal_id = "SIG1234"

    # 1) Create
    data = {
        "userid":                       userid,
        "signal_id":                    signal_id,
        "symbol":                       "INFY",
        "instrument_type":              SymbolType.EQ.value,
        "trade_type":                   TradeType.BUY.value,
        "source":                       "EXAMPLE",
        "message":                      None,
        "entry_snapshot":               {},        # arbitrary payload
        "last_snapshot":                None,
        "exited":                       False,

        "entry_time":                   datetime.now(),
        "entry_price":                  Decimal("100.50"),
        "executed_entry_price":         None,
        "entry_order_id":               "",
        "entry_order_response_json":    "",
        "entry_retries":                0,

        "quantity":                     10,


        "trailing_stop_type":           "NOTRAIL",
        "trailing_stop_params":         "",        # now a VARCHAR, not JSON
        "trailing_stop_price":          Decimal("100.50"),


        "stoploss_order_id":            None,
        "stoploss_order_response_json": None,
        "stoploss_retries":             0,


        "exit_time":                    None,
        "exit_price":                   None,
        "exit_pnl":                     None,
        "executed_exit_price":          None,
        "exit_order_id":                None,
        "exit_order_response_json":     None,
        "exit_retries":                 0,

        "last_time":                    datetime.now(),
        "last_price":                   Decimal("100.50"),
        "last_pnl":                     Decimal("0.00"),
        "last_pnl_value":               Decimal("0.00"),

        "max_price":                    Decimal("100.50"),
        "min_price":                    Decimal("100.50"),
        "max_time":                     datetime.now(),
        "min_time":                     datetime.now(),

        "trade_status":                 TradeStatus.OPEN.value,
        "processed":                    False,
        "active":                       True,
    }

    # Create & log
    ut = UserTradeSchema.create_user_trade(data)
    logger.info("Created UserTrade with id=%s", ut.id)

    # Fetch by business key (userid)
    uts = UserTradeSchema.fetch_user_trades_by_user(userid)
    logger.info("Fetched %d trades for user %s", len(uts), userid)

    # Update by PK to ENTRY_PLACED
    updated = UserTradeSchema.update_user_trade_by_id(
        ut.id,
        {"trade_status": TradeStatus.ENTRY_PLACED.value}
    )
    logger.info("Updated trade: %s", updated.model_dump_json(indent=2))

    # Fetch all incomplete trades
    incomplete = UserTradeSchema.fetch_unprocessed_trades()
    logger.info("Incomplete trades count: %d", len(incomplete))

    # Delete (soft-delete) by primary key
    deleted = UserTradeSchema.delete_user_trade_by_id(ut.id)
    logger.info("Deleted trade id=%s: %s", ut.id, deleted)

if __name__ == "__main__":
    main()
