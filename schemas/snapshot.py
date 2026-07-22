# schemas/snapshot.py

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import Text, cast, or_, and_
from sqlalchemy.exc import IntegrityError

from database.database import get_trades_db
from models.trade_models import Snapshot as SnapshotORM
from utils.json_utils import sanitize_json

logger = logging.getLogger(__name__)


STRICT_MODEL_CONFIG = ConfigDict(
    extra="forbid",
    arbitrary_types_allowed=True,
    populate_by_name=True,
)


class StrictBaseModel(BaseModel):
    model_config = STRICT_MODEL_CONFIG


# -----------------------------
# Core market facts
# -----------------------------
class BarBlock(StrictBaseModel):
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None


class PrevDayBlock(StrictBaseModel):
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None


class TodayBlock(StrictBaseModel):
    open: Optional[float] = None


class OpeningRangeBlock(StrictBaseModel):
    window: str = "09:15-09:29"
    high: Optional[float] = None
    low: Optional[float] = None
    ready: bool = False


class LevelsBlock(StrictBaseModel):
    prev_day: PrevDayBlock = Field(default_factory=PrevDayBlock)
    today: TodayBlock = Field(default_factory=TodayBlock)
    opening_range: OpeningRangeBlock = Field(default_factory=OpeningRangeBlock)


# -----------------------------
# Indicator facts
# -----------------------------
class EMABlock(StrictBaseModel):
    fast: Optional[float] = None
    mid1: Optional[float] = None
    mid2: Optional[float] = None
    slow: Optional[float] = None
    ref: Optional[float] = None


class HMABlock(StrictBaseModel):
    fast: Optional[float] = None
    mid1: Optional[float] = None
    mid2: Optional[float] = None
    slow: Optional[float] = None
    state: str = "NO_TREND"
    strength: str = "NA"


class VWAPBlock(StrictBaseModel):
    value: Optional[float] = None
    side: str = "UNKNOWN"
    distance_pct: Optional[float] = None
    distance_points: Optional[float] = None
    distance_atr: Optional[float] = None


class RSIBlock(StrictBaseModel):
    value: Optional[float] = None
    zone: str = "NA"


class ADXBlock(StrictBaseModel):
    value: Optional[float] = None
    band: str = "NA"


class ATRBlock(StrictBaseModel):
    value: Optional[float] = None
    band: str = "NA"
    pct: Optional[float] = None


class BollingerBlock(StrictBaseModel):
    upper: Optional[float] = None
    mid: Optional[float] = None
    lower: Optional[float] = None
    bb_width: Optional[float] = None
    position: Optional[float] = None
    zone: str = "UNKNOWN"


class EnvelopeBlock(StrictBaseModel):
    hma_envelope: Optional[float] = None
    ema_envelope: Optional[float] = None


class IndicatorsBlock(StrictBaseModel):
    ema: EMABlock = Field(default_factory=EMABlock)
    hma: HMABlock = Field(default_factory=HMABlock)
    vwap: VWAPBlock = Field(default_factory=VWAPBlock)
    rsi: RSIBlock = Field(default_factory=RSIBlock)
    adx: ADXBlock = Field(default_factory=ADXBlock)
    atr: ATRBlock = Field(default_factory=ATRBlock)
    bollinger: BollingerBlock = Field(default_factory=BollingerBlock)
    envelopes: EnvelopeBlock = Field(default_factory=EnvelopeBlock)


class VolumeBlock(StrictBaseModel):
    bar_volume: Optional[float] = None
    bar_rvol: Optional[float] = None
    bar_rvol_pct: Optional[float] = None
    bar_rvol_band: str = "NA"
    bar_volume_slope: Optional[float] = None
    today_cum: Optional[float] = None
    prev_day_total: Optional[float] = None
    today_vs_prev_ratio: Optional[float] = None
    periods: Dict[str, Any] = Field(default_factory=dict)


# -----------------------------
# Time windows
# -----------------------------
class MarketWindowBlock(StrictBaseModel):
    status: str = "na"
    bars: int = 0
    minutes: Optional[int] = None
    session_elapsed_minutes: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume_sum: Optional[float] = None
    avg_volume: Optional[float] = None
    move_points: Optional[float] = None
    move_pct: Optional[float] = None
    move_atr: Optional[float] = None
    range_points: Optional[float] = None
    range_pct: Optional[float] = None
    slope_points_per_bar: Optional[float] = None
    slope_atr_per_bar: Optional[float] = None
    close_position_in_range: Optional[float] = None


class NumericIndicatorWindowBlock(StrictBaseModel):
    status: str = "na"
    bars: int = 0
    minutes: Optional[int] = None
    start_value: Optional[float] = None
    end_value: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    delta: Optional[float] = None
    slope_per_bar: Optional[float] = None
    start_state: Optional[str] = None
    end_state: Optional[str] = None
    state_changed: bool = False
    touched_upper: bool = False
    touched_lower: bool = False


class HMAIndicatorWindowBlock(StrictBaseModel):
    status: str = "na"
    bars: int = 0
    minutes: Optional[int] = None
    start_state: str = "UNKNOWN"
    end_state: str = "UNKNOWN"
    start_strength: str = "UNKNOWN"
    end_strength: str = "UNKNOWN"
    state_changed: bool = False
    strength_changed: bool = False
    flip_count: int = 0
    bars_in_current_state: int = 0


class VWAPIndicatorWindowBlock(StrictBaseModel):
    status: str = "na"
    bars: int = 0
    minutes: Optional[int] = None
    start_side: str = "UNKNOWN"
    end_side: str = "UNKNOWN"
    crossed: bool = False
    min_distance_pct: Optional[float] = None
    max_distance_pct: Optional[float] = None
    end_distance_pct: Optional[float] = None
    bars_in_current_side: int = 0


class BollingerIndicatorWindowBlock(StrictBaseModel):
    status: str = "na"
    bars: int = 0
    minutes: Optional[int] = None
    start_zone: str = "UNKNOWN"
    end_zone: str = "UNKNOWN"
    zone_changed: bool = False
    touch_upper_count: int = 0
    touch_lower_count: int = 0
    min_position: Optional[float] = None
    max_position: Optional[float] = None
    width_change_pct: Optional[float] = None


# -----------------------------
# Price action facts
# -----------------------------
class PriceActionReferenceBlock(StrictBaseModel):
    position: str = "UNKNOWN"
    distance_atr: Optional[float] = None
    distance_points: Optional[float] = None


class PriceActionWindowMoveBlock(StrictBaseModel):
    status: str = "na"
    session_elapsed_minutes: Optional[float] = None
    points: Optional[float] = None
    pct: Optional[float] = None
    atr: Optional[float] = None


class PriceActionSlopeBlock(StrictBaseModel):
    status: str = "na"
    bars_3_atr: Optional[float] = None
    bars_5_atr: Optional[float] = None
    bars_3_atr_per_bar: Optional[float] = None
    bars_5_atr_per_bar: Optional[float] = None
    previous_3_atr_per_bar: Optional[float] = None
    state: str = "UNKNOWN"


class PriceActionBlock(StrictBaseModel):
    orb: PriceActionReferenceBlock = Field(default_factory=PriceActionReferenceBlock)
    vwap: PriceActionReferenceBlock = Field(default_factory=PriceActionReferenceBlock)
    moves: Dict[str, PriceActionWindowMoveBlock] = Field(default_factory=dict)
    slope: PriceActionSlopeBlock = Field(default_factory=PriceActionSlopeBlock)


# -----------------------------
# Structure block
# -----------------------------
class StructureRangeBlock(StrictBaseModel):
    range_id: Optional[str] = None
    version: int = 0
    high: Optional[float] = None
    low: Optional[float] = None
    width_pct: Optional[float] = None
    width_atr: Optional[float] = None
    source: str = "UNKNOWN"
    range_type: str = "UNKNOWN"
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    established_at: Optional[datetime] = None
    evidence_cutoff: Optional[datetime] = None
    bars: int = 0
    provisional: bool = False
    breakout_eligible: bool = False


class BalanceMetricsBlock(StrictBaseModel):
    adjacent_overlap_ratio: Optional[float] = None
    directional_efficiency: Optional[float] = None
    net_displacement_fraction: Optional[float] = None
    close_occupancy_ratio: Optional[float] = None
    midpoint_drift_atr: Optional[float] = None
    upper_boundary_drift_atr: Optional[float] = None
    lower_boundary_drift_atr: Optional[float] = None
    upper_interactions: int = 0
    lower_interactions: int = 0
    quality: Optional[float] = None
    classification: str = "UNKNOWN"
    reason: Optional[str] = None


class RawStructureBlock(StrictBaseModel):
    state: str = "UNKNOWN"
    side: str = "NEUTRAL"
    range: StructureRangeBlock = Field(default_factory=StructureRangeBlock)
    metrics: BalanceMetricsBlock = Field(default_factory=BalanceMetricsBlock)
    recent_swing_high: Optional[float] = None
    recent_swing_low: Optional[float] = None
    reason: Optional[str] = None


class AcceptedStructureBlock(StrictBaseModel):
    state: str = "RANGE_ACCEPTED"
    # Read-only compatibility for already persisted pre-neutral snapshots. New
    # snapshots never serialize a direction on an accepted range.
    side: Optional[str] = Field(default=None, exclude=True)
    range: StructureRangeBlock = Field(default_factory=StructureRangeBlock)
    metrics: BalanceMetricsBlock = Field(default_factory=BalanceMetricsBlock)
    age_bars: int = 0
    frozen: bool = True
    promoted_time: Optional[datetime] = None
    quality: Optional[float] = None
    reason: Optional[str] = None


class CandidateStructureBlock(StrictBaseModel):
    active: bool = False
    status: str = "NONE"
    side: str = "NEUTRAL"
    range: StructureRangeBlock = Field(default_factory=StructureRangeBlock)
    metrics: BalanceMetricsBlock = Field(default_factory=BalanceMetricsBlock)
    bars_confirmed: int = 0
    first_seen_time: Optional[datetime] = None
    quality: Optional[float] = None
    reason: Optional[str] = None


class RecentCloseObservationBlock(StrictBaseModel):
    time: datetime
    close: float


class StructureAnchorBlock(StrictBaseModel):
    pdh: Optional[float] = None
    pdl: Optional[float] = None
    orb_high: Optional[float] = None
    orb_low: Optional[float] = None
    orb_ready: bool = False
    recent15_high: Optional[float] = None
    recent15_low: Optional[float] = None
    active_anchor: str = "UNKNOWN"


class BreakoutContextBlock(StrictBaseModel):
    swing: str = "UNKNOWN"
    orb: str = "UNKNOWN"
    pdh_pdl: str = "UNKNOWN"
    recent15: str = "UNKNOWN"


class StructureBlock(StrictBaseModel):
    raw: RawStructureBlock = Field(default_factory=RawStructureBlock)
    accepted: AcceptedStructureBlock = Field(default_factory=AcceptedStructureBlock)
    candidate: CandidateStructureBlock = Field(default_factory=CandidateStructureBlock)
    recent_closes: List[RecentCloseObservationBlock] = Field(default_factory=list)
    anchors: StructureAnchorBlock = Field(default_factory=StructureAnchorBlock)
    breakout_context: BreakoutContextBlock = Field(default_factory=BreakoutContextBlock)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    session_phase: str = "UNKNOWN"
    previous_state: Optional[str] = None
    previous_side: Optional[str] = None
    count: int = 1
    flip_count_today: int = 0
    reason: Optional[str] = None


# -----------------------------
# State and events
# -----------------------------
class StateMetricBlock(StrictBaseModel):
    confirmed_state: Optional[str] = None
    raw_state: Optional[str] = None
    previous_state: Optional[str] = None
    age_bars: int = 0
    previous_age_bars: int = 0
    candidate_state: Optional[str] = None
    candidate_age_bars: int = 0
    changed: bool = False
    flip_count_today: int = 0


class StructureStateContextBlock(StrictBaseModel):
    confirmed_state: Optional[str] = None
    # Legacy input compatibility only; range direction is no longer emitted.
    confirmed_side: Optional[str] = Field(default=None, exclude=True)
    raw_state: Optional[str] = None
    raw_side: Optional[str] = None
    previous_state: Optional[str] = None
    previous_side: Optional[str] = None
    age_bars: int = 0
    changed: bool = False
    flip_count_today: int = 0


class StateContextBlock(StrictBaseModel):
    hma: StateMetricBlock = Field(default_factory=StateMetricBlock)
    hma_strength: StateMetricBlock = Field(default_factory=StateMetricBlock)
    vwap: StateMetricBlock = Field(default_factory=StateMetricBlock)
    rsi: StateMetricBlock = Field(default_factory=StateMetricBlock)
    adx: StateMetricBlock = Field(default_factory=StateMetricBlock)
    atr: StateMetricBlock = Field(default_factory=StateMetricBlock)
    bollinger: StateMetricBlock = Field(default_factory=StateMetricBlock)
    volume: StateMetricBlock = Field(default_factory=StateMetricBlock)
    structure: StructureStateContextBlock = Field(default_factory=StructureStateContextBlock)


class EventBlock(StrictBaseModel):
    k: str
    from_: Optional[str] = None
    to: Optional[str] = None


class AuctionSnapshotBlock(StrictBaseModel):
    """Pure Auction Engine result embedded in the market snapshot.

    ``continuity`` is the bounded incremental state read by the next snapshot.
    It is not a separate checkpoint and has no persistence outside the snapshot
    JSON itself. Signal/Advisor lifecycle data is intentionally excluded.
    """

    status: str = "NOT_RUN"
    continuity_mode: str = "COLD_START"
    engine_name: Optional[str] = None
    engine_version: Optional[str] = None
    config_version: Optional[str] = None
    config_hash: Optional[str] = None
    previous_snapshot_time: Optional[datetime] = None
    elapsed_ms: Optional[float] = None
    continuity_bytes: int = 0
    continuity_hash: Optional[str] = None
    continuity: Dict[str, Any] = Field(default_factory=dict)
    state: Dict[str, Any] = Field(default_factory=dict)
    boundary: Optional[Dict[str, Any]] = None
    candidates: List[Dict[str, Any]] = Field(default_factory=list)
    opportunities: List[Dict[str, Any]] = Field(default_factory=list)
    manager_decision: Dict[str, Any] = Field(default_factory=dict)
    local_decision: Dict[str, Any] = Field(default_factory=dict)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


# -----------------------------
# Snapshot schema
# -----------------------------
class SnapshotSchema(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    symbol: str
    snapshot_time: datetime
    tf: str = "3m"

    close: float
    bar: BarBlock = Field(default_factory=BarBlock)

    ltp: Optional[float] = None
    ltp_time: Optional[datetime] = None
    gen_signals: bool = False

    levels: LevelsBlock = Field(default_factory=LevelsBlock)
    indicators: IndicatorsBlock = Field(default_factory=IndicatorsBlock)
    volume: VolumeBlock = Field(default_factory=VolumeBlock)
    market_windows: Dict[str, MarketWindowBlock] = Field(default_factory=dict)
    indicator_windows: Dict[str, Dict[str, Dict[str, Any]]] = Field(default_factory=dict)
    price_action: PriceActionBlock = Field(default_factory=PriceActionBlock)
    structure: StructureBlock = Field(default_factory=StructureBlock)
    state_context: StateContextBlock = Field(default_factory=StateContextBlock)
    # Internal continuity cache used by snapshot generation to avoid replaying
    # the whole session on every 3m tick. Consumers should use structure and
    # state_context; this is only generation state.
    state_memory: Dict[str, Any] = Field(default_factory=dict)
    events: List[EventBlock] = Field(default_factory=list)
    derivatives: Dict[str, Any] = Field(default_factory=dict)
    auction: AuctionSnapshotBlock = Field(default_factory=AuctionSnapshotBlock)

    def to_db_dict(self) -> Dict[str, Any]:
        raw = self.model_dump(
            mode="python",
            by_alias=True,
            exclude={"ltp", "ltp_time"},
        )
        return sanitize_json(raw)

    @staticmethod
    def from_db_dict(dump: Dict[str, Any]) -> "SnapshotSchema":
        if not dump:
            raise ValueError("Empty snapshot dump")

        # Older rows may still contain the retired schema_version metadata.
        # Ignore it while loading; newly generated snapshots no longer write it.
        if "schema_version" in dump:
            dump = dict(dump)
            dump.pop("schema_version", None)

        ts = dump.get("snapshot_time")
        if isinstance(ts, str):
            try:
                dump = dict(dump)
                dump["snapshot_time"] = datetime.fromisoformat(ts)
            except Exception:
                pass

        ltp_ts = dump.get("ltp_time")
        if isinstance(ltp_ts, str):
            try:
                dump = dict(dump)
                dump["ltp_time"] = datetime.fromisoformat(ltp_ts)
            except Exception:
                pass

        return SnapshotSchema.model_validate(dump)

    @staticmethod
    def create_snapshot(snapshot: "SnapshotSchema") -> "SnapshotSchema":
        """Create or update a snapshot row idempotently.

        Replay/backfill runs often regenerate the same (symbol, snapshot_time).
        Do not rely only on a database IntegrityError because older deployed
        tables may not have the intended composite primary/unique key.  Check
        first, update the existing row when present, and only insert when no row
        exists.
        """
        raw = snapshot.to_db_dict()
        ltp_val = snapshot.ltp if snapshot.ltp is not None else snapshot.close
        ltp_time_val = snapshot.ltp_time if snapshot.ltp_time is not None else snapshot.snapshot_time

        with get_trades_db() as db:
            existing = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol == snapshot.symbol)
                .filter(SnapshotORM.snapshot_time == snapshot.snapshot_time)
                .first()
            )

            if existing:
                logger.debug(
                    "Snapshot[%s @ %s] exists; updating current schema payload",
                    snapshot.symbol,
                    snapshot.snapshot_time,
                )
                existing.ltp = ltp_val
                existing.ltp_time = ltp_time_val
                existing.data = raw
                existing.processed = False
                db.commit()
                return snapshot

            orm = SnapshotORM(
                symbol=snapshot.symbol,
                snapshot_time=snapshot.snapshot_time,
                ltp=ltp_val,
                ltp_time=ltp_time_val,
                data=raw,
                processed=False,
            )
            db.add(orm)

            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                logger.warning(
                    "Snapshot[%s @ %s] hit unique constraint during insert; updating existing row",
                    snapshot.symbol,
                    snapshot.snapshot_time,
                )
                existing = (
                    db.query(SnapshotORM)
                    .filter(SnapshotORM.symbol == snapshot.symbol)
                    .filter(SnapshotORM.snapshot_time == snapshot.snapshot_time)
                    .first()
                )
                if existing:
                    existing.ltp = ltp_val
                    existing.ltp_time = ltp_time_val
                    existing.data = raw
                    existing.processed = False
                    db.commit()

        return snapshot

    @staticmethod
    def fetch_snapshot(symbol: str, snapshot_time: datetime) -> Optional["SnapshotSchema"]:
        # Use first() instead of one_or_none() so older tables that already
        # contain duplicate rows do not break replay. create_snapshot() is now
        # idempotent and truncating/cleaning snapshots removes the duplicates,
        # but fetch should remain defensive.
        with get_trades_db() as db:
            rec = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol == symbol)
                .filter(SnapshotORM.snapshot_time == snapshot_time)
                .order_by(SnapshotORM.snapshot_time.desc())
                .first()
            )
        return SnapshotSchema.from_db_dict(rec.data) if rec and rec.data else None

    @staticmethod
    def update_snapshot(
        symbol: str,
        snapshot_time: datetime,
        update_data: Dict[str, Any],
    ) -> Optional["SnapshotSchema"]:
        with get_trades_db() as db:
            rec = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol == symbol)
                .filter(SnapshotORM.snapshot_time == snapshot_time)
                .first()
            )
            if not rec:
                return None

            existing = rec.data or {}
            existing.update(update_data or {})
            rec.data = sanitize_json(existing)
            db.commit()
            db.refresh(rec)
            data = rec.data

        return SnapshotSchema.from_db_dict(data) if data else None

    @staticmethod
    def delete_snapshot(symbol: str, snapshot_time: datetime) -> bool:
        with get_trades_db() as db:
            rows = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol == symbol)
                .filter(SnapshotORM.snapshot_time == snapshot_time)
                .all()
            )
            if not rows:
                return False

            for rec in rows:
                db.delete(rec)
            db.commit()

        return True

    @staticmethod
    def fetch_latest_for_symbol(symbol: str) -> Optional["SnapshotSchema"]:
        with get_trades_db() as db:
            rec = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol == symbol)
                .order_by(SnapshotORM.snapshot_time.desc())
                .first()
            )
        return SnapshotSchema.from_db_dict(rec.data) if rec and rec.data else None

    @staticmethod
    def fetch_latest_for_symbol_asof(symbol: str, asof_time: datetime) -> Optional["SnapshotSchema"]:
        with get_trades_db() as db:
            rec = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol == symbol)
                .filter(SnapshotORM.snapshot_time <= asof_time)
                .order_by(SnapshotORM.snapshot_time.desc())
                .first()
            )
        return SnapshotSchema.from_db_dict(rec.data) if rec and rec.data else None

    @staticmethod
    def fetch_latest_today_payload_before_time(
        symbol: str,
        ts: datetime,
    ) -> Optional[Dict[str, Any]]:
        """Return the raw previous snapshot payload for same-day continuity.

        This intentionally returns the JSON payload instead of validating through
        SnapshotSchema so older rows that do not yet contain state_memory can be
        inspected and rejected by the caller without raising.
        """
        if ts.tzinfo is None:
            start_of_day = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start_of_day = ts.astimezone(ts.tzinfo).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

        with get_trades_db() as db:
            rec = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol == symbol)
                .filter(SnapshotORM.snapshot_time >= start_of_day)
                .filter(SnapshotORM.snapshot_time < ts)
                .order_by(SnapshotORM.snapshot_time.desc())
                .first()
            )

        if not rec or not rec.data:
            return None

        return rec.data if isinstance(rec.data, dict) else None

    @staticmethod
    def fetch_latest_ltp(symbol: str) -> Optional[Dict[str, Any]]:
        with get_trades_db() as db:
            rec = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol == symbol)
                .order_by(SnapshotORM.snapshot_time.desc())
                .first()
            )

            if not rec:
                return None

            ltp_val = None
            try:
                if getattr(rec, "ltp", None) is not None:
                    ltp_val = float(rec.ltp)
            except Exception:
                ltp_val = None

            return {
                "symbol": rec.symbol,
                "snapshot_time": rec.snapshot_time,
                "ltp": ltp_val,
                "ltp_time": getattr(rec, "ltp_time", None),
            }

    @staticmethod
    def fetch_symbols_for_day(trading_day: date) -> List[str]:
        day_start = datetime.combine(trading_day, dtime.min)
        day_end = day_start + timedelta(days=1)
        with get_trades_db() as db:
            rows = (
                db.query(SnapshotORM.symbol)
                .filter(SnapshotORM.snapshot_time >= day_start)
                .filter(SnapshotORM.snapshot_time < day_end)
                .distinct()
                .order_by(SnapshotORM.symbol.asc())
                .all()
            )
        return [str(symbol).strip().upper() for (symbol,) in rows if str(symbol).strip()]

    @staticmethod
    def fetch_day_replay_batch(
        *,
        trading_day: date,
        after_time: Optional[datetime] = None,
        after_symbol: str = "",
        symbols: Optional[List[str]] = None,
        limit: int = 500,
    ) -> List["SnapshotSchema"]:
        """Load one chronological historical batch without using processed."""
        day_start = datetime.combine(trading_day, dtime.min)
        day_end = day_start + timedelta(days=1)
        with get_trades_db() as db:
            query = (
                db.query(
                    SnapshotORM.symbol.label("symbol"),
                    SnapshotORM.snapshot_time.label("snapshot_time"),
                    SnapshotORM.ltp.label("ltp"),
                    SnapshotORM.ltp_time.label("ltp_time"),
                    cast(SnapshotORM.data, Text).label("data_text"),
                )
                .filter(SnapshotORM.snapshot_time >= day_start)
                .filter(SnapshotORM.snapshot_time < day_end)
            )
            if symbols:
                query = query.filter(SnapshotORM.symbol.in_([
                    str(symbol).strip().upper() for symbol in symbols
                ]))
            if after_time is not None:
                query = query.filter(or_(
                    SnapshotORM.snapshot_time > after_time,
                    and_(
                        SnapshotORM.snapshot_time == after_time,
                        SnapshotORM.symbol > str(after_symbol or ""),
                    ),
                ))
            rows = (
                query.order_by(
                    SnapshotORM.snapshot_time.asc(),
                    SnapshotORM.symbol.asc(),
                )
                .limit(max(1, int(limit)))
                .all()
            )

        snapshots: List[SnapshotSchema] = []
        for row in rows:
            try:
                raw = row.data_text
                payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
                payload.setdefault("symbol", str(row.symbol).strip().upper())
                payload.setdefault("snapshot_time", row.snapshot_time)
                if row.ltp is not None:
                    payload["ltp"] = float(row.ltp)
                if row.ltp_time is not None:
                    payload["ltp_time"] = row.ltp_time
                snapshots.append(SnapshotSchema.from_db_dict(payload))
            except Exception as exc:
                raise ValueError(
                    f"Invalid snapshot payload for {row.symbol} @ {row.snapshot_time}"
                ) from exc
        return snapshots

    @staticmethod
    def fetch_unprocessed_day_batch(
        *,
        trading_day: date,
        after_time: Optional[datetime] = None,
        after_symbol: str = "",
        symbols: Optional[List[str]] = None,
        until_time: Optional[datetime] = None,
        limit: int = 500,
    ) -> List["SnapshotSchema"]:
        """Load one chronological batch of unprocessed snapshots for a day.

        This is the production/restart replay queue contract: processed rows are
        skipped, ordering is timestamp then symbol, and the cursor remains stable
        while the caller acknowledges rows after successful Auction processing.
        """
        day_start = datetime.combine(trading_day, dtime.min)
        day_end = day_start + timedelta(days=1)
        with get_trades_db() as db:
            query = (
                db.query(
                    SnapshotORM.symbol.label("symbol"),
                    SnapshotORM.snapshot_time.label("snapshot_time"),
                    SnapshotORM.ltp.label("ltp"),
                    SnapshotORM.ltp_time.label("ltp_time"),
                    cast(SnapshotORM.data, Text).label("data_text"),
                )
                .filter(SnapshotORM.processed == False)  # noqa: E712
                .filter(SnapshotORM.snapshot_time >= day_start)
                .filter(SnapshotORM.snapshot_time < day_end)
            )
            if symbols:
                query = query.filter(SnapshotORM.symbol.in_([
                    str(symbol).strip().upper() for symbol in symbols
                ]))
            if until_time is not None:
                query = query.filter(SnapshotORM.snapshot_time <= until_time)
            if after_time is not None:
                query = query.filter(or_(
                    SnapshotORM.snapshot_time > after_time,
                    and_(
                        SnapshotORM.snapshot_time == after_time,
                        SnapshotORM.symbol > str(after_symbol or ""),
                    ),
                ))
            rows = (
                query.order_by(
                    SnapshotORM.snapshot_time.asc(),
                    SnapshotORM.symbol.asc(),
                )
                .limit(max(1, int(limit)))
                .all()
            )

        snapshots: List[SnapshotSchema] = []
        for row in rows:
            try:
                raw = row.data_text
                payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
                payload.setdefault("symbol", str(row.symbol).strip().upper())
                payload.setdefault("snapshot_time", row.snapshot_time)
                if row.ltp is not None:
                    payload["ltp"] = float(row.ltp)
                if row.ltp_time is not None:
                    payload["ltp_time"] = row.ltp_time
                snapshots.append(SnapshotSchema.from_db_dict(payload))
            except Exception as exc:
                raise ValueError(
                    f"Invalid unprocessed snapshot payload for {row.symbol} @ {row.snapshot_time}"
                ) from exc
        return snapshots

    @staticmethod
    def fetch_unprocessed(limit: Optional[int] = None) -> List["SnapshotSchema"]:
        with get_trades_db() as db:
            query = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.processed == False)
                .order_by(
                    SnapshotORM.snapshot_time.asc(),
                    SnapshotORM.symbol.asc(),
                )
            )
            if limit is not None:
                query = query.limit(max(1, int(limit)))
            rows = query.all()

        out: List[SnapshotSchema] = []

        for r in rows:
            try:
                if r.data:
                    out.append(SnapshotSchema.from_db_dict(r.data))
            except Exception:
                logger.exception("Failed to load snapshot row: %s", getattr(r, "id", None))

        return out

    @staticmethod
    def fetch_unprocessed_keys_asof(
        snapshot_time: datetime,
        symbols: Optional[List[str]] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> List[Tuple[str, datetime]]:
        """Return unprocessed snapshot keys available by as-of time in replay order.

        Snapshot replay must be chronological across symbols.  Ordering by
        symbol first causes one symbol to advance through the day while another
        symbol is still at the same earlier timestamp, which is not how the
        live pipeline sees snapshots.
        """
        with get_trades_db() as db:
            q = db.query(SnapshotORM.symbol, SnapshotORM.snapshot_time).filter(
                SnapshotORM.processed == False,  # noqa: E712
                SnapshotORM.snapshot_time <= snapshot_time,
            )
            if symbols:
                q = q.filter(SnapshotORM.symbol.in_(symbols))
            if start_time is not None:
                q = q.filter(SnapshotORM.snapshot_time >= start_time)
            if end_time is not None:
                q = q.filter(SnapshotORM.snapshot_time <= end_time)
            q = q.order_by(SnapshotORM.snapshot_time.asc(), SnapshotORM.symbol.asc())
            if limit is not None:
                q = q.limit(max(0, int(limit)))
            rows = q.all()

        return [(str(symbol), snapshot_time) for symbol, snapshot_time in rows]

    @staticmethod
    def mark_processed(symbol: str, snapshot_time: datetime) -> bool:
        try:
            with get_trades_db() as db:
                rows = (
                    db.query(SnapshotORM)
                    .filter(SnapshotORM.symbol == symbol)
                    .filter(SnapshotORM.snapshot_time == snapshot_time)
                    .all()
                )
                if not rows:
                    return False

                for rec in rows:
                    rec.processed = True
                db.commit()

            return True
        except Exception:
            logger.exception("Error marking snapshot processed")
            return False

    @staticmethod
    def fetch_recent_today_for_symbol_before_time(
        symbol: str,
        ts: datetime,
        limit: int = 10,
        ascending: bool = True,
    ) -> List["SnapshotSchema"]:
        start_of_day = ts.replace(hour=0, minute=0, second=0, microsecond=0)

        with get_trades_db() as db:
            rows = (
                db.query(SnapshotORM)
                .filter(SnapshotORM.symbol == symbol)
                .filter(SnapshotORM.snapshot_time >= start_of_day)
                .filter(SnapshotORM.snapshot_time <= ts)
                .order_by(SnapshotORM.snapshot_time.desc())
                .limit(int(limit))
                .all()
            )

        if ascending:
            rows = list(reversed(rows))

        out: List[SnapshotSchema] = []

        for r in rows:
            try:
                if r.data:
                    out.append(SnapshotSchema.from_db_dict(r.data))
            except Exception:
                logger.exception("Failed to load snapshot row: %s", getattr(r, "id", None))

        return out

    @staticmethod
    def update_latest_ltp_if_newer(
        symbol: str,
        ltp: float,
        ltp_time: datetime,
    ) -> bool:
        try:
            with get_trades_db() as db:
                rec = (
                    db.query(SnapshotORM)
                    .filter(SnapshotORM.symbol == symbol)
                    .order_by(SnapshotORM.snapshot_time.desc())
                    .first()
                )

                if not rec:
                    return False

                if ltp_time <= rec.snapshot_time:
                    return False

                rec.ltp = ltp
                rec.ltp_time = ltp_time
                db.commit()

                return True
        except Exception:
            logger.exception("Error updating latest ltp for %s", symbol)
            return False
