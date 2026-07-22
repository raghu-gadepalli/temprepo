# File: schemas/derivatives.py
# PURPOSE: DB/IO ONLY (no computations). All analytics moved to services/derivatives_helper.py
#
# DB table: derivativeschain_v2
#   symbol (PK), snapshot_time (PK), raw (JSON NOT NULL), derived (JSON NULL)

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field
from sqlalchemy import func

from database.database import get_trades_db
from models.trade_models import DerivativesChain as DerivativesChainORM
from utils.json_utils import sanitize_json

logger = logging.getLogger(__name__)


# -----------------------------
# Raw quote shapes (light typing)
# -----------------------------
class OHLCQuote(BaseModel):
    low: Optional[float] = None
    high: Optional[float] = None
    open: Optional[float] = None
    close: Optional[float] = None

    model_config = {"extra": "allow"}


class InstrumentQuote(BaseModel):
    instrument: str
    exchange: Optional[str] = None
    quote_time: Optional[datetime] = None

    last_price: Optional[float] = None
    volume: Optional[float] = None
    oi: Optional[float] = None
    ohlc: Optional[OHLCQuote] = None

    # useful for futures/options if needed
    expiry: Optional[str] = None

    model_config = {"extra": "allow"}


# -----------------------------
# Derived / lite blocks (used in Snapshot)
# -----------------------------
class OptionPick(BaseModel):
    """Used for UI top calls/puts and for selection."""
    symbol: str
    strike: Optional[float] = None
    oi: Optional[float] = None
    ltp: Optional[float] = None

    model_config = {"extra": "allow"}


class OptionsLite(BaseModel):
    """
    Decision-ready summary.
    This is what you will copy into Snapshot.derivatives.options_lite
    """
    atm_strike: Optional[float] = None
    pcr: Optional[float] = None

    support: Optional[float] = None
    resistance: Optional[float] = None
    max_pain: Optional[float] = None

    top_calls: List[OptionPick] = Field(default_factory=list)
    top_puts: List[OptionPick] = Field(default_factory=list)

    model_config = {"extra": "allow"}


class OptionLadderLeg(BaseModel):
    """One option in the ladder; no CE/PE paired row needed."""
    symbol: str
    type: Literal["CE", "PE"]
    strike: float
    oi: Optional[float] = None
    oi_chg: Optional[float] = None
    ltp: Optional[float] = None

    model_config = {"extra": "allow"}


class OptionLadder(BaseModel):
    """
    For instrument selection at snapshot time.
    Keep it simple: a list of CE legs and PE legs around ATM.
    """
    window: int = 5
    atm_strike: Optional[float] = None
    calls: List[OptionLadderLeg] = Field(default_factory=list)
    puts: List[OptionLadderLeg] = Field(default_factory=list)

    model_config = {"extra": "allow"}


class OIWindowRow(BaseModel):
    """Paired strike row for display and totals."""
    strike: float

    ce_symbol: Optional[str] = None
    ce_oi: Optional[float] = None
    ce_oi_chg: Optional[float] = None
    ce_ltp: Optional[float] = None

    pe_symbol: Optional[str] = None
    pe_oi: Optional[float] = None
    pe_oi_chg: Optional[float] = None
    pe_ltp: Optional[float] = None

    model_config = {"extra": "allow"}


class OIWindowTotals(BaseModel):
    ce_oi: Optional[float] = None
    pe_oi: Optional[float] = None
    ce_oi_chg: Optional[float] = None
    pe_oi_chg: Optional[float] = None

    model_config = {"extra": "allow"}


class OIWindow(BaseModel):
    atm: Optional[float] = None
    window: int = 5
    rows: List[OIWindowRow] = Field(default_factory=list)
    totals: OIWindowTotals = Field(default_factory=OIWindowTotals)

    model_config = {"extra": "allow"}


class SentimentDriver(BaseModel):
    key: Optional[str] = None       # e.g. "ce_writing"
    label: Optional[str] = None     # e.g. "CE writing"
    bias: Optional[str] = None      # "bullish"/"bearish"
    share: Optional[float] = None   # 0..1

    model_config = {"extra": "allow"}


class OptionsSentimentWindow(BaseModel):
    """
    One window’s sentiment result.
    Stored under derived.option_sentiment_windows[window_key]
    """
    status: Literal["ok", "na", "error"] = "ok"
    window: str  # "5m"/"15m"/"60m"/"sod"
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None

    atm: Optional[float] = None
    indication: Optional[Literal["bullish", "bearish", "neutral"]] = None
    strength: Optional[float] = None  # 0..1

    pcr_now: Optional[float] = None
    pcr_delta: Optional[float] = None

    driver: Optional[SentimentDriver] = None
    components: Optional[Dict[str, float]] = None  # raw buckets

    # allow extra fields like "reason", "n_points" during migration/debug
    model_config = {"extra": "allow"}


class FutureSentimentWindow(BaseModel):
    """
    Futures build-up classification per window.
    """
    status: Literal["ok", "na", "error"] = "ok"
    window: str  # "5m"/"15m"/"60m"/"sod"
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None

    label: Optional[Literal[
        "LONG_BUILDUP",
        "SHORT_BUILDUP",
        "SHORT_COVERING",
        "LONG_UNWINDING",
        "NEUTRAL"
    ]] = None

    fut_ltp_now: Optional[float] = None
    fut_ltp_delta: Optional[float] = None
    fut_oi_now: Optional[float] = None
    fut_oi_delta: Optional[float] = None

    strength: Optional[float] = None  # optional

    model_config = {"extra": "allow"}


class DerivativesDerived(BaseModel):
    """
    Everything derived; safe to evolve without breaking raw storage.
    """
    options_lite: Optional[OptionsLite] = None
    option_ladder: Optional[OptionLadder] = None

    # Per-window blocks (keys: "sod","60m","15m","5m")
    oi_windows: Dict[str, OIWindow] = Field(default_factory=dict)
    option_sentiment_windows: Dict[str, OptionsSentimentWindow] = Field(default_factory=dict)
    future_sentiment_windows: Dict[str, FutureSentimentWindow] = Field(default_factory=dict)

    model_config = {"extra": "allow"}


# -----------------------------
# Top-level Derivatives record (DB JSON)
# -----------------------------
class DerivativesChainRaw(BaseModel):
    """
    Raw chain blob (quote snapshot) stored in DB column `raw`.
    """
    spot_price: Optional[float] = None
    future: Optional[InstrumentQuote] = None

    # options keyed by "1120_CE"/"1120_PE" OR whatever key you use today
    options: Dict[str, InstrumentQuote] = Field(default_factory=dict)

    model_config = {"extra": "allow"}


class DerivativesChainSchema(BaseModel):
    """
    DB-backed derivatives chain record per symbol per minute.
    IO-only:
      - create_raw()
      - update_derived()
      - fetch_*(...)
    Computations live in services/derivatives_helper.py
    """
    symbol: str
    snapshot_time: datetime

    raw: Dict[str, Any]
    derived: Optional[Dict[str, Any]] = None

    model_config = {"from_attributes": True, "extra": "allow"}

    # -----------------------------
    # CREATE / UPSERT (RAW ONLY)
    # -----------------------------
    @staticmethod
    def create_raw(symbol: str, snapshot_time: datetime, raw: Dict[str, Any]) -> bool:
        """
        Persist RAW into derivativeschain_v2 (derived stays NULL unless already present).
        Uses merge() so it is idempotent by PK.
        """
        try:
            clean_raw = sanitize_json(raw if isinstance(raw, dict) else {})
            orm = DerivativesChainORM(
                symbol=symbol,
                snapshot_time=snapshot_time,
                raw=clean_raw,
            )
            with get_trades_db() as db:
                db.merge(orm)
                db.commit()

            logger.info("Inserted DerivativesChainV2 RAW for %s @ %s", symbol, snapshot_time)
            return True
        except Exception:
            logger.exception("Failed to create RAW DerivativesChainV2[%s @ %s]", symbol, snapshot_time)
            return False

    @staticmethod
    def create(record: "DerivativesChainSchema") -> bool:
        """
        Backward-compatible convenience: writes raw and derived (if provided).
        """
        try:
            clean_raw = sanitize_json(record.raw if isinstance(record.raw, dict) else {})
            clean_derived = sanitize_json(record.derived) if isinstance(record.derived, dict) else record.derived

            orm = DerivativesChainORM(
                symbol=record.symbol,
                snapshot_time=record.snapshot_time,
                raw=clean_raw,
                derived=clean_derived if isinstance(clean_derived, dict) else None,
            )
            with get_trades_db() as db:
                db.merge(orm)
                db.commit()

            logger.info("Inserted DerivativesChainV2 for %s @ %s", record.symbol, record.snapshot_time)
            return True
        except Exception:
            logger.exception("Failed to create DerivativesChainV2[%s @ %s]", record.symbol, record.snapshot_time)
            return False

    # -----------------------------
    # UPDATE DERIVED (PATCH or REPLACE)
    # -----------------------------
    @staticmethod
    def update_derived(
        symbol: str,
        snapshot_time: datetime,
        patch: Dict[str, Any],
        merge: bool = True,
    ) -> bool:
        """
        Update derived JSON for (symbol, snapshot_time).

        - merge=True  -> dict merge at top-level (base.update(patch))
        - merge=False -> replace entire derived with `patch`
        """
        try:
            if not isinstance(patch, dict):
                patch = {}

            with get_trades_db() as db:
                rec = (
                    db.query(DerivativesChainORM)
                      .filter(
                          DerivativesChainORM.symbol == symbol,
                          DerivativesChainORM.snapshot_time == snapshot_time
                      )
                      .one_or_none()
                )
                if not rec:
                    return False

                if not merge:
                    rec.derived = sanitize_json(patch)
                else:
                    base = rec.derived or {}
                    if not isinstance(base, dict):
                        base = {}
                    base.update(patch)
                    rec.derived = sanitize_json(base)

                db.commit()
            return True
        except Exception:
            logger.exception("Failed to update derived DerivativesChainV2[%s @ %s]", symbol, snapshot_time)
            return False

    # -----------------------------
    # FETCH HELPERS
    # -----------------------------
    @staticmethod
    def fetch_latest(symbol: str) -> Optional["DerivativesChainSchema"]:
        """Return the most recent derivatives snapshot for this symbol, or None."""
        try:
            with get_trades_db() as db:
                rec = (
                    db.query(DerivativesChainORM)
                      .filter(DerivativesChainORM.symbol == symbol)
                      .order_by(DerivativesChainORM.snapshot_time.desc())
                      .first()
                )
            return DerivativesChainSchema.model_validate(rec) if rec else None
        except Exception:
            logger.exception("Failed to fetch latest DerivativesChainV2 for %s", symbol)
            return None

    @staticmethod
    def fetch_at(symbol: str, snapshot_time: datetime) -> Optional["DerivativesChainSchema"]:
        """Return the derivatives snapshot for this symbol exactly at snapshot_time, or None."""
        try:
            with get_trades_db() as db:
                rec = (
                    db.query(DerivativesChainORM)
                      .filter(
                          DerivativesChainORM.symbol == symbol,
                          DerivativesChainORM.snapshot_time == snapshot_time
                      )
                      .one_or_none()
                )
            return DerivativesChainSchema.model_validate(rec) if rec else None
        except Exception:
            logger.exception("Failed to fetch DerivativesChainV2[%s @ %s]", symbol, snapshot_time)
            return None

    @staticmethod
    def fetch_first_of_day(symbol: str, ts: datetime) -> Optional["DerivativesChainSchema"]:
        """Return the very first snapshot for `symbol` on the same date as `ts`, or None."""
        day = ts.date()
        try:
            with get_trades_db() as db:
                rec = (
                    db.query(DerivativesChainORM)
                      .filter(
                          DerivativesChainORM.symbol == symbol,
                          func.date(DerivativesChainORM.snapshot_time) == day
                      )
                      .order_by(DerivativesChainORM.snapshot_time.asc())
                      .first()
                )
            return DerivativesChainSchema.model_validate(rec) if rec else None
        except Exception:
            logger.exception("Failed to fetch first-of-day DerivativesChainV2 for %s @ %s", symbol, day)
            return None

    @staticmethod
    def fetch_latest_today_for_symbol_before_time(
        symbol: str,
        ts: datetime
    ) -> Optional["DerivativesChainSchema"]:
        """
        Return the most recent row for `symbol` on ts.date(), where snapshot_time <= ts.
        """
        day = ts.date()
        try:
            with get_trades_db() as db:
                rec = (
                    db.query(DerivativesChainORM)
                      .filter(
                          DerivativesChainORM.symbol == symbol,
                          func.date(DerivativesChainORM.snapshot_time) == day,
                          DerivativesChainORM.snapshot_time <= ts
                      )
                      .order_by(DerivativesChainORM.snapshot_time.desc())
                      .first()
                )
            return DerivativesChainSchema.model_validate(rec) if rec else None
        except Exception:
            logger.exception("Failed to fetch latest-of-day DerivativesChainV2 for %s @ %s", symbol, day)
            return None

    @staticmethod
    def fetch_recent_today_for_symbol_before_time(
        symbol: str,
        t: datetime,
        limit: int = 5,
        ascending: bool = True,
    ) -> List["DerivativesChainSchema"]:
        """
        Return up to `limit` rows for `symbol` on t.date(), with snapshot_time <= t.
        Results are chronological (oldest -> newest) when ascending=True.
        """
        day = t.date()
        try:
            with get_trades_db() as db:
                recs = (
                    db.query(DerivativesChainORM)
                      .filter(
                          DerivativesChainORM.symbol == symbol,
                          func.date(DerivativesChainORM.snapshot_time) == day,
                          DerivativesChainORM.snapshot_time <= t,
                      )
                      .order_by(DerivativesChainORM.snapshot_time.desc())
                      .limit(int(limit))
                      .all()
                )
            if ascending:
                recs = list(reversed(recs))
            return [DerivativesChainSchema.model_validate(r) for r in recs]
        except Exception:
            logger.exception("Failed fetch_recent_today(V2, limit=%s) for %s @ %s", limit, symbol, day)
            return []

    @staticmethod
    def fetch_range_for_symbol(
        symbol: str,
        start_ts: datetime,
        end_ts: datetime,
        ascending: bool = True,
    ) -> List["DerivativesChainSchema"]:
        """
        Return rows for symbol in [start_ts, end_ts] (inclusive), ordered by time.
        """
        try:
            with get_trades_db() as db:
                q = (
                    db.query(DerivativesChainORM)
                    .filter(
                        DerivativesChainORM.symbol == symbol,
                        DerivativesChainORM.snapshot_time >= start_ts,
                        DerivativesChainORM.snapshot_time <= end_ts,
                    )
                    .order_by(DerivativesChainORM.snapshot_time.asc() if ascending
                                else DerivativesChainORM.snapshot_time.desc())
                )
                recs = q.all()
            return [DerivativesChainSchema.model_validate(r) for r in recs]
        except Exception:
            logger.exception("Failed fetch_range_for_symbol for %s [%s..%s]", symbol, start_ts, end_ts)
            return []

    # -----------------------------
    # COMPAT: old method name
    # -----------------------------
    @staticmethod
    def update_chain_fields(symbol: str, snapshot_time: datetime, patch: Dict[str, Any]) -> bool:
        """
        Compatibility shim:
        Old code called update_chain_fields() to patch into derivatives_chain.
        In V2, treat it as a derived-patch (top-level merge).
        """
        return DerivativesChainSchema.update_derived(symbol, snapshot_time, patch, merge=True)
