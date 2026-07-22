import logging
import os
import sys
from datetime import datetime

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Shared logging setup
from logconfig import setup_logging
setup_logging()
logger = logging.getLogger(__name__)

from schemas.snapshot import SnapshotSchema, FrequencySnapshotSchema
from enums.enums import TrendType
from services.snapshot_helper import derive_hma_state_strength, derive_snapshot_values

# Define a static timestamp for all tests
STATIC_TIME_STR = "2025-04-23T09:29:00"
STATIC_TIME     = datetime.fromisoformat(STATIC_TIME_STR)


def test_create_snapshot() -> SnapshotSchema:
    """
    1) Build a dummy FrequencySnapshotSchema with HMA values
    2) Compute hma_trend, state, hma_strength
    3) Derive buy/sell weight, envelope_dvg, state, strength
    4) Create + persist a SnapshotSchema
    """
    ts = STATIC_TIME

    # --- 1) dummy FrequencySnapshotSchema ---
    dummy_freq_snapshot = FrequencySnapshotSchema(
        frequency="1",
        snapshot_time=ts,
        close=100.0,
        # Dummy HMA values for the bar
        hmafast=100.5,
        hmamid1=100.0,
        hmamid2=99.5,
        hmaslow=99.0,
    )

    # --- 2) HMA-based trend & strength ---
    rec = {
        "hmafast":     dummy_freq_snapshot.hmafast,
        "hmamid1":  dummy_freq_snapshot.hmamid1,
        "hmamid2": dummy_freq_snapshot.hmamid2,
        "hmaslow": dummy_freq_snapshot.hmaslow,
    }
    hma_state, hma_strength = derive_hma_state_strength(rec)
    dummy_freq_snapshot.hma_trend    = hma_state
    dummy_freq_snapshot.state         = hma_state
    dummy_freq_snapshot.hma_strength = hma_strength
    dummy_freq_snapshot.has_state_changed = True  # simulate a change

    # --- 3) top-level snapshot values ---
    buy_weight, sell_weight, envelope_dvg, state, strength = derive_snapshot_values(dummy_freq_snapshot)

    # --- 4) build & persist snapshot ---
    snapshot = SnapshotSchema(
        symbol="AXISBANK",
        snapshot_time=ts,
        close=100.0,
        frequencies_snapshot={"1": dummy_freq_snapshot},
        changed_states=[f"1.hma_trend:NO_TREND->{hma_state.value}"],
        buy_weight=buy_weight,
        sell_weight=sell_weight,
        envelope_dvg=envelope_dvg,
        state=state,
        strength=strength,
    )

    logger.info("-> Creating snapshot in DB at %s ...", STATIC_TIME_STR)
    created = SnapshotSchema.create_snapshot(snapshot)
    logger.info("[SUCCESS] Created Snapshot:\n%s", created.model_dump_json(indent=4))
    return created


def test_fetch_snapshot() -> None:
    """
    Fetch the snapshot for the static timestamp.
    """
    symbol = "AXISBANK"
    ts = STATIC_TIME

    logger.info("-> Fetching snapshot for '%s' at '%s' ...", symbol, STATIC_TIME_STR)
    snap = SnapshotSchema.fetch_snapshot(symbol, ts)
    if snap:
        logger.info("[SUCCESS] Fetched Snapshot:\n%s", snap.model_dump_json(indent=4))
    else:
        logger.info("[ERROR] No snapshot found for those parameters.")


def test_update_snapshot() -> None:
    """
    Update top-level fields of the snapshot.
    """
    symbol = "AXISBANK"
    ts = STATIC_TIME

    update_data = {"close": 101.0, "buy_weight": 7.5}
    logger.info("-> Updating snapshot for '%s' at '%s' with %s ...", symbol, STATIC_TIME_STR, update_data)
    updated = SnapshotSchema.update_snapshot(symbol, ts, update_data)
    if updated:
        logger.info("[SUCCESS] Updated Snapshot:\n%s", updated.model_dump_json(indent=4))
    else:
        logger.info("[ERROR] No snapshot found to update.")


def test_delete_snapshot() -> None:
    """
    Delete the snapshot.
    """
    symbol = "AXISBANK"
    ts = STATIC_TIME

    logger.info("-> Deleting snapshot for '%s' at '%s' ...", symbol, STATIC_TIME_STR)
    ok = SnapshotSchema.delete_snapshot(symbol, ts)
    if ok:
        logger.info("[SUCCESS] Deleted Snapshot.")
    else:
        logger.info("[ERROR] No snapshot found to delete.")


if __name__ == "__main__":
    # 1) Create
    test_create_snapshot()
    # 2) Fetch
    test_fetch_snapshot()
    # 3) Update
    test_update_snapshot()
    # 4) Fetch again
    test_fetch_snapshot()
    # 5) Delete
    test_delete_snapshot()
    # 6) Final fetch (should not find it)
    test_fetch_snapshot()
