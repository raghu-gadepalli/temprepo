import logging
import os
import sys
from datetime import datetime

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
#  Shared logging setup 

from schemas.derivatives import OptChainSnapshotSchema, OptionInstrument

from logconfig import setup_logging
setup_logging()
logger = logging.getLogger(__name__)


def test_optchain_crud():
    # Step 1: Define symbol and time
    symbol = "INFY"
    snapshot_time = datetime.now().replace(second=0, microsecond=0)

    # Step 2: Create a minimal sample option chain
    option_chain = {
        "1560_CE": OptionInstrument(
            strike=1560,
            type="CE",
            oi=3353600,
            ltp=18.75,
            volume=12000,
            ohlc={"open": 20.1, "high": 21.0, "low": 17.8, "close": 19.2}
        ).model_dump(),

        "1500_PE": OptionInstrument(
            strike=1500,
            type="PE",
            oi=1292800,
            ltp=14.25,
            volume=8500,
            ohlc={"open": 13.5, "high": 15.0, "low": 13.2, "close": 14.0}
        ).model_dump()
    }

    # Step 3: Build the OptChainSnapshotSchema
    record = OptChainSnapshotSchema(
        symbol=symbol,
        snapshot_time=snapshot_time,
        option_chain=option_chain
    )

    # Step 4: Create/insert into DB
    inserted = OptChainSnapshotSchema.create_optchain(record)
    print("Inserted:", inserted)

    # Step 5: Fetch by symbol + snapshot_time
    fetched = OptChainSnapshotSchema.fetch_optchain_at(symbol, snapshot_time)
    print("Fetched by timestamp:", fetched.option_chain if fetched else "None")

    # Step 6: Fetch latest
    latest = OptChainSnapshotSchema.fetch_latest_optchain(symbol)
    print("Latest snapshot time:", latest.snapshot_time if latest else "None")

    # Step 7: Delete (optional for cleanup)
    # # deleted = OptChainSnapshotSchema.delete_optchain(symbol, snapshot_time)
    # print("Deleted:", deleted)

if __name__ == "__main__":
    test_optchain_crud()
