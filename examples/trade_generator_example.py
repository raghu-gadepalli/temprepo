import logging
import os
import sys
import uuid
from datetime import datetime
from decimal import Decimal

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
#  Shared logging setup 
from logconfig import setup_logging
setup_logging()
logger = logging.getLogger(__name__)

from schemas.user_trade import UserTradeSchema
from enums.enums         import SymbolType, TradeType, TradeStatus

def main():
    # 1) Business-key user ID
    userid = "AT1234"

    # 2) Generate a one-off signal_id (32-char hex, fits VARCHAR(36))
    signal_id = uuid.uuid4().hex

    # 3) Build your payload
    data = {
        "userid":                    userid,
        "signal_id":                 signal_id,
        "symbol":                    "INFY",
        "instrument_type":           SymbolType.EQ.value,
        "trade_type":                TradeType.BUY.value,
        "entry_time":                datetime.now(),
        "entry_price":               Decimal("100.50"),
        "executed_entry_price":      None,
        "entry_order_id":            "",
        "entry_order_response_json": "",
        "entry_retries":             5,
        "quantity":                  10,
        "trailing_stop_type":        "NOTRAIL",
        "trailing_stop_params":       Decimal("1.0"),
        "trailing_stop_price":       Decimal("100.50"),
        "stoploss_order_id":         None,
        "stoploss_order_response_json": None,
        "stoploss_retries":          5,
        #  New exit-order fields 
        "exit_order_id":             None,
        "exit_order_response_json":  None,
        "exit_retries":              5,
        "exit_time":                 None,
        "exit_price":                None,
        "exit_pnl":                  None,
        "executed_exit_price":       None,
        "last_time":                 datetime.now(),
        "last_price":                Decimal("100.50"),
        "last_pnl":                  Decimal("0.00"),
        "last_pnl_value":            Decimal("0.00"),
        "max_price":                 Decimal("100.50"),
        "min_price":                 Decimal("100.50"),
        "max_time":                  datetime.now(),
        "min_time":                  datetime.now(),
        "trade_status":              TradeStatus.OPEN.value,
        "processed":                 False,
        "active":                    True,
    }

    # 4) CREATE
    ut = UserTradeSchema.create_user_trade(data)
    created_id = ut.id
    logger.info(" Created UserTrade (id=%d):\n%s", created_id, ut.model_dump_json(indent=2))

    # 5) FETCH BY USERID
    uts = UserTradeSchema.fetch_user_trades_by_user(userid)
    logger.info("  Fetched trades for user %s: %d record(s)", userid, len(uts))

    # 6) UPDATE (advance to ENTRY_PLACED) by primary key
    updated = UserTradeSchema.update_user_trade_by_id(
        created_id,
        {"trade_status": TradeStatus.ENTRY_PLACED.value}
    )
    logger.info(" Updated trade status to ENTRY_PLACED (id=%d):\n%s",
                created_id, updated.model_dump_json(indent=2))

    # 7) FETCH INCOMPLETE (processed=False)
    incomplete = UserTradeSchema.fetch_unprocessed_trades()
    logger.info("  Incomplete trades count: %d", len(incomplete))

    # 8) DELETE (soft) by primary key
    deleted = UserTradeSchema.delete_user_trade_by_id(created_id)
    logger.info(" Soft-deleted trade (id=%d)? %s", created_id, deleted)

    # 9) VERIFY DELETE
    post_delete = UserTradeSchema.fetch_user_trades_by_user(userid)
    logger.info(" After deletion, active trades for %s: %d",
                userid, len(post_delete or []))

if __name__ == "__main__":
    main()
