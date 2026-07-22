
#!/usr/bin/env python3
"""
generate_nifty100.py  INSERT-ONLY from instruments (old convention)

What this does
--------------
- Reads utils/ind_nifty100list.csv (same format as NSE "ind_nifty100list.csv").
- For each symbol (plus the two indices):
    * Retrieves instrument(s) from the instruments table:
        - Stocks: by tradingsymbol
        - Indices: base lookup uses "NIFTY"/"BANKNIFTY" for F&O
    * INSERTS rows into `symbols` (no updates). If a row already exists,
      we quietly skip it.
    * For indices, the EQ row is inserted with symbol=name=equity_ref:
        - "NIFTY 50" / "NIFTY BANK"
      But F&O retrieval uses "NIFTY"/"BANKNIFTY" as the underlying base.

- Inserts: EQ + front-month FUT + same-expiry OPTs (CE/PE).
"""

import csv
import logging
import os
import sys
from datetime import date
from typing import List, Optional, Dict

# Ensure project root
PROJ_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJ_ROOT)

# Project imports
from logconfig import setup_logging
from database.database import get_trades_db
from models.trade_models import Instrument as InstrumentORM
from schemas.symbol import SymbolSchema
from schemas.instrument import InstrumentSchema
from utils.datetime_utils import current_fo_expiry

logger: Optional[logging.Logger] = None
AS_OF = date.today()
CSV_PATH = os.path.join(PROJ_ROOT, "utils", "ind_nifty100list.csv")
LOG_FILE = "generate_nifty100.log"

INDEX_UNDERLYING = {
    "NIFTY 50":   "NIFTY",
    "NIFTY BANK": "BANKNIFTY",
}

DEFAULT_FLAGS = {
    "generate_candles":   False,
    "merge_candles":      False,
    "update_performance": False,
    "generate_signals":   False,
    "processed":          False,
    "active":             True,
    "enabled":            True,
}

def _norm(s: str) -> str:
    return (s or "").strip().upper()

def _read_local_csv(path: str) -> List[str]:
    out: List[str] = []
    with open(path, "r", newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            sym = _norm(row.get("Symbol") or row.get("SYMBOL") or "")
            if sym:
                out.append(sym)
    # de-dup & sort for stability
    return sorted(set(out))

def _resolve_eq_instrument(eq_display: str) -> Optional[InstrumentSchema]:
    key = _norm(eq_display)
    if key in INDEX_UNDERLYING:
        # Try to find an instrument row for the display (helpful if present)
        with get_trades_db() as db:
            rec = (
                db.query(InstrumentORM)
                  .filter((InstrumentORM.tradingsymbol == key) | (InstrumentORM.name == key))
                  .order_by(InstrumentORM.id.asc())
                  .first()
            )
        return InstrumentSchema.model_validate(rec) if rec else None
    # Stocks: helper by tradingsymbol
    return InstrumentSchema.fetch_instrument(eq_display)

def _underlying_base(eq_display: str, equity_inst: Optional[InstrumentSchema]) -> Optional[str]:
    if _norm(eq_display) in INDEX_UNDERLYING:
        return INDEX_UNDERLYING[_norm(eq_display)]
    if equity_inst and getattr(equity_inst, "name", None):
        return _norm(equity_inst.name)
    return None

def _pick_front_fut(underlying: str) -> Optional[InstrumentSchema]:
    # Preferred helper
    try:
        fut = InstrumentSchema.fetch_closest_future_for_equity(underlying, AS_OF)
        if fut:
            return fut
    except Exception:
        pass

    # Fallback direct query
    cutoff = current_fo_expiry()
    FUT_TYPES = ("FUT", "FUTIDX", "FUTSTK")
    with get_trades_db() as db:
        row = (
            db.query(InstrumentORM)
              .filter(InstrumentORM.name == _norm(underlying))
              .filter(InstrumentORM.instrument_type.in_(FUT_TYPES))
              .filter(InstrumentORM.expiry >= cutoff)
              .order_by(InstrumentORM.expiry.asc())
              .first()
        )
    return InstrumentSchema.model_validate(row) if row else None

def _same_expiry_opts(underlying: str, expiry: date):
    with get_trades_db() as db:
        rows = (
            db.query(InstrumentORM)
              .filter(InstrumentORM.name == _norm(underlying))
              .filter(InstrumentORM.instrument_type.in_(("CE", "PE")))
              .filter(InstrumentORM.expiry == expiry)
              .all()
        )
    return [InstrumentSchema.model_validate(r) for r in rows]

# -------------------------- INSERT HELPERS ----------------------------
def _insert(payload: Dict) -> bool:
    """
    Insert-only. If row exists (duplicate key), quietly skip.
    Returns True if inserted, False if skipped.
    """
    try:
        created = SymbolSchema.create_symbol(payload)
        if created:
            return True
        # If the helper returns falsy without raising, treat as skipped
        return False
    except Exception as e:
        # Duplicate or other constraint -> skip quietly; log at debug
        msg = str(e).lower()
        if "duplicate" in msg or "unique" in msg or "integrity" in msg:
            logger.debug("Skip existing: %s", payload.get("symbol"))
            return False
        logger.exception("Create failed for %s", payload.get("symbol"))
        return False

def _insert_eq(eq_display: str, equity_inst: Optional[InstrumentSchema]) -> None:
    display = _norm(eq_display)
    eq_is_index = display in INDEX_UNDERLYING

    symbol_val = display
    name_val   = display if eq_is_index else getattr(equity_inst, "name", display)

    payload = {
        "symbol":        symbol_val,
        "token":         getattr(equity_inst, "instrument_token", None),
        "name":          name_val,
        "type":          "EQ",
        "price":         getattr(equity_inst, "last_price", None),
        "exchange":      getattr(equity_inst, "exchange", "NSE"),
        "segment":       getattr(equity_inst, "segment", "NSE"),
        "signal_profile":      "MOMENTUM",
        "lotsize":       (getattr(equity_inst, "lot_size", 1) or 1),
        "expiry":        None,
        "strike_price":  None,
        "tick_size":     getattr(equity_inst, "tick_size", None),
        "equity_ref":    display,   # original display (e.g., "NIFTY 50")
        "last_time":     None,
        "last_snapshot": None,
        **DEFAULT_FLAGS,
    }
    if _insert(payload):
        logger.info("EQ    %-12s | token=%s | name=%s",
                    symbol_val,
                    str(getattr(equity_inst, "instrument_token", None)),
                    name_val)

def _insert_from_inst(inst: InstrumentSchema, equity_ref_display: str) -> None:
    itype = _norm(inst.instrument_type)
    profile = "MOMENTUM"
    payload = {
        "symbol":        inst.tradingsymbol,
        "token":         inst.instrument_token,
        "name":          inst.name,
        "type":          inst.instrument_type,
        "price":         getattr(inst, "last_price", None),
        "exchange":      inst.exchange,
        "segment":       inst.segment,
        "signal_profile":      profile,
        "lotsize":       getattr(inst, "lot_size", 1) or 1,
        "expiry":        inst.expiry,
        "strike_price":  getattr(inst, "strike", None),
        "tick_size":     getattr(inst, "tick_size", None),
        "equity_ref":    _norm(equity_ref_display),
        "last_time":     None,
        "last_snapshot": None,
        **DEFAULT_FLAGS,
    }
    if _insert(payload):
        if itype == "FUT":
            logger.info("  FUT  %-16s | token=%s | expiry=%s",
                        inst.tradingsymbol, inst.instrument_token, str(inst.expiry))
        elif itype in ("CE", "PE"):
            logger.info("  %-3s  %-16s | token=%s | expiry=%s | strike=%s",
                        itype, inst.tradingsymbol, inst.instrument_token,
                        str(inst.expiry), str(getattr(inst, "strike", None)))

# ------------------------------ FLOW ---------------------------------
def _process(eq_display: str) -> None:
    eq_inst = _resolve_eq_instrument(eq_display)
    _insert_eq(eq_display, eq_inst)

    base = _underlying_base(eq_display, eq_inst)
    if not base:
        return

    fut = _pick_front_fut(base)
    if not fut:
        return

    # Insert FUT + same-expiry options with equity_ref = original display
    _insert_from_inst(fut, eq_display)
    for inst in _same_expiry_opts(base, fut.expiry):
        _insert_from_inst(inst, eq_display)

def main():
    setup_logging(log_file=LOG_FILE)
    global logger
    logger = logging.getLogger(__name__)

    if not os.path.exists(CSV_PATH):
        logger.error("CSV not found: %s", CSV_PATH)
        sys.exit(2)

    logger.info("Starting generate_nifty100 (AS_OF=%s, CSV=%s)", AS_OF.isoformat(), CSV_PATH)

    # Process indices first
    for idx in ("NIFTY 50", "NIFTY BANK"):
        _process(idx)

    # Then CSV equities
    for s in _read_local_csv(CSV_PATH):
        _process(s)

    logger.info("Finished generate_nifty100")

if __name__ == "__main__":
    main()
