import logging
import os
import sys
from datetime import datetime

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
#  Shared logging setup 
from logconfig import setup_logging
setup_logging()
logger = logging.getLogger(__name__)

#  Import SignalGenerator (fallback if package import fails) 
from services.signals.signal_generator import SignalGenerator
from schemas.snapshot import FrequencySnapshotSchema, SnapshotSchema
from schemas.signal import SignalSchema
from enums.enums import TradeType

#  Build a minimal dummy snapshot 
fs = FrequencySnapshotSchema(
    frequency="1",
    snapshot_time=datetime(2025, 4, 17, 9, 55),
    close=100.0,
    ema_10=100.5,
    ema_20=101.0,
    ema_25=101.5,
    ema_50=102.0,
    ema_cr_10_25=None,
    ema_cr_10_50=None,
    ema_cr_20_50=None,
    rsi=None,
    state=None,
    has_state_changed=True,
)
fs.compute_crossover_states()

snap = SnapshotSchema(
    symbol="SBIN",
    snapshot_time=datetime(2025, 4, 23, 9, 15),
    close=100.0,
    frequencies_snapshot={"1": fs},
    changed_states=["1"],
    buy_weight=1.0,
    sell_weight=0.0,
)

#  Persist snapshot so fetch_open_signal can find context if needed 
# SnapshotSchema.create_snapshot(snap)

#  Run the signal generator 
gen = SignalGenerator(snap)
result = gen.generate_signal()
logger.info("SignalGenerator returned: %r", result)

#  Fetch any open signal (if ENTRY was generated) 
open_sig = SignalSchema.fetch_open_signal("SBIN", "TREND-1-EQ")
if open_sig:
    logger.info("Open signal record:\n%s", open_sig.model_dump_json(indent=2))
else:
    logger.info("No open signal found.")
