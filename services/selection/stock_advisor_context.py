from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from typing import Any, Dict, Iterable, List, Optional, Sequence

from database.database import get_trades_db
from models.trade_models import Signal as SignalORM
from models.trade_models import StockSetupState as StockSetupStateORM
from models.trade_models import UserTrade as UserTradeORM
from schemas.snapshot import SnapshotSchema
from utils.datetime_utils import to_ist_naive

logger = logging.getLogger(__name__)


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _get(obj: Any, *path: str, default: Any = None) -> Any:
    cur = obj
    for key in path:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            cur = getattr(cur, key, None)
    return default if cur is None else cur


def _norm_upper(value: Any, default: str = "") -> str:
    raw = default if value is None else value
    return str(raw).strip().upper()


def _snapshot_ts(snapshot: SnapshotSchema) -> Optional[datetime]:
    """Return snapshot timestamp normalized to naive IST for DB-safe comparisons."""
    return to_ist_naive(getattr(snapshot, "snapshot_time", None))


def _trading_day(ts: datetime) -> date:
    normalized = to_ist_naive(ts)
    return (normalized or ts).date()


def _start_of_day(ts: datetime) -> datetime:
    normalized = to_ist_naive(ts) or ts
    return datetime.combine(normalized.date(), time.min)


def _market_window(snapshot: SnapshotSchema, key: str) -> Any:
    windows = getattr(snapshot, "market_windows", None) or {}
    return windows.get(key) or windows.get(key.lower()) or windows.get(key.upper())


def _day_range_pct(snapshot: SnapshotSchema) -> Optional[float]:
    sod = _market_window(snapshot, "sod")
    return _safe_float(_get(sod, "range_pct"))


def _recent_range_pct(snapshot: SnapshotSchema) -> Optional[float]:
    for key in ("15m", "30m", "60m", "current"):
        w = _market_window(snapshot, key)
        value = _safe_float(_get(w, "range_pct"))
        if value is not None:
            return value
    return None


def _vwap_side(snapshot: SnapshotSchema) -> str:
    side = _norm_upper(_get(snapshot, "indicators", "vwap", "side"), default="UNKNOWN")
    if side in {"ABOVE", "BUY", "BULLISH", "UP"}:
        return "ABOVE"
    if side in {"BELOW", "SELL", "BEARISH", "DOWN"}:
        return "BELOW"
    return side or "UNKNOWN"


def _hma_state(snapshot: SnapshotSchema) -> str:
    return _norm_upper(_get(snapshot, "indicators", "hma", "state"), default="UNKNOWN")


def _structure_state(snapshot: SnapshotSchema) -> str:
    return _norm_upper(_get(snapshot, "structure", "accepted", "state"), default="UNKNOWN")


def _atr_value(snapshot: SnapshotSchema) -> Optional[float]:
    return _safe_float(_get(snapshot, "indicators", "atr", "value"))


def _close(snapshot: SnapshotSchema) -> Optional[float]:
    return _safe_float(getattr(snapshot, "close", None)) or _safe_float(_get(snapshot, "bar", "close"))


def _range_position(snapshot: SnapshotSchema) -> Optional[float]:
    sod = _market_window(snapshot, "sod")
    return _safe_float(_get(sod, "close_position_in_range"))


def _move_atr(snapshot: SnapshotSchema, window: str) -> Optional[float]:
    return _safe_float(_get(_market_window(snapshot, window), "move_atr"))


def _count_side_flips(values: Sequence[str]) -> int:
    flips = 0
    prev = ""
    for raw in values:
        value = _norm_upper(raw, default="UNKNOWN")
        if value in {"", "UNKNOWN", "NEUTRAL", "NA", "NONE"}:
            continue
        if prev and value != prev:
            flips += 1
        prev = value
    return flips


def _pct_change(start: Optional[float], end: Optional[float]) -> Optional[float]:
    if start is None or end is None or start == 0:
        return None
    return (end - start) / abs(start) * 100.0


@dataclass(frozen=True)
class StockAdvisorDayContext:
    """Day-so-far behaviour used by StockAdvisor.

    All values must be computed only from rows at or before the current snapshot
    timestamp.  This context is intentionally stock/day level; setup-specific
    confirmation stays in EvidenceEvaluator.
    """

    symbol: str
    snapshot_time: str
    snapshot_count: int = 0

    vwap_cross_count: int = 0
    vwap_above_ratio: float = 0.0
    vwap_below_ratio: float = 0.0
    vwap_context: str = "UNKNOWN"

    hma_flip_count: int = 0
    structure_flip_count: int = 0
    context_flip_count: int = 0
    chop_context: str = "UNKNOWN"

    atr_start: Optional[float] = None
    atr_current: Optional[float] = None
    atr_change_pct: Optional[float] = None
    atr_context: str = "UNKNOWN"

    day_range_start_pct: Optional[float] = None
    day_range_current_pct: Optional[float] = None
    day_range_recent_growth_pct: Optional[float] = None
    recent_range_pct: Optional[float] = None
    range_context: str = "UNKNOWN"

    trend_context: str = "UNKNOWN"
    preferred_direction: str = "NEUTRAL"
    avoid_direction: str = "NEUTRAL"

    prior_setup_states: int = 0
    prior_terminal_setup_states: int = 0
    prior_failed_setup_states: int = 0
    prior_expired_setup_states: int = 0
    prior_same_side_failed: Dict[str, int] = field(default_factory=dict)

    prior_signals: int = 0
    prior_no_mfe_signals: int = 0
    prior_fast_invalidations: int = 0
    prior_trade_rows: int = 0
    prior_trade_loss_rows: int = 0
    attempt_context: str = "NO_PRIOR_ATTEMPTS"

    reason_codes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class StockAdvisorContextBuilder:
    """Build day-so-far context for StockAdvisor.

    The builder caches same-day snapshots per symbol so replay/backtest does not
    query all rows repeatedly.  It may load the whole day's rows into memory, but
    every metric filters to snapshot_time <= current snapshot_time to avoid
    future leakage.
    """

    def __init__(self, *, snapshot_limit: int = 500) -> None:
        self.snapshot_limit = max(1, int(snapshot_limit or 500))
        self._snapshot_cache: Dict[tuple[str, date], List[SnapshotSchema]] = {}

    def build(
        self,
        snapshot: SnapshotSchema,
        *,
        recent_snapshots: Optional[Iterable[SnapshotSchema]] = None,
    ) -> StockAdvisorDayContext:
        symbol = _norm_upper(getattr(snapshot, "symbol", ""))
        ts = _snapshot_ts(snapshot)
        if not symbol or ts is None:
            return StockAdvisorDayContext(symbol=symbol, snapshot_time=str(getattr(snapshot, "snapshot_time", "")))

        snaps = self._day_snapshots(symbol=symbol, ts=ts, supplied=recent_snapshots)
        if not any(_snapshot_ts(s) == ts for s in snaps):
            snaps.append(snapshot)
            snaps.sort(key=lambda s: _snapshot_ts(s) or datetime.min)
        states = self._setup_state_rows(symbol=symbol, ts=ts)
        signals = self._signal_rows(symbol=symbol, ts=ts)
        signal_ids = [str(getattr(row, "signal_id", "") or "").strip() for row in signals]
        trades = self._trade_rows(symbol=symbol, ts=ts, signal_ids=signal_ids)

        return self._from_rows(symbol=symbol, ts=ts, snapshots=snaps, states=states, signals=signals, trades=trades)

    def _day_snapshots(
        self,
        *,
        symbol: str,
        ts: datetime,
        supplied: Optional[Iterable[SnapshotSchema]],
    ) -> List[SnapshotSchema]:
        if supplied is not None:
            rows = [s for s in supplied if isinstance(s, SnapshotSchema)]
        else:
            key = (symbol, ts.date())
            if key not in self._snapshot_cache:
                try:
                    self._snapshot_cache[key] = SnapshotSchema.fetch_recent_today_for_symbol_before_time(
                        symbol,
                        ts.replace(hour=23, minute=59, second=59, microsecond=999999),
                        limit=self.snapshot_limit,
                        ascending=True,
                    )
                except Exception:
                    logger.exception("StockAdvisor failed loading day snapshots | symbol=%s time=%s", symbol, ts)
                    self._snapshot_cache[key] = []
            rows = self._snapshot_cache.get(key, [])

        out: List[SnapshotSchema] = []
        for snap in rows:
            snap_ts = _snapshot_ts(snap)
            if snap_ts is not None and snap_ts <= ts:
                out.append(snap)
        out.sort(key=lambda s: _snapshot_ts(s) or datetime.min)
        return out

    def _setup_state_rows(self, *, symbol: str, ts: datetime) -> List[Any]:
        try:
            with get_trades_db() as db:
                rows = (
                    db.query(StockSetupStateORM)
                    .filter(StockSetupStateORM.trading_day == _trading_day(ts))
                    .filter(StockSetupStateORM.equity_ref == symbol)
                    .all()
                )
            out = []
            for row in rows:
                last_seen = getattr(row, "last_seen_time", None)
                first_seen = getattr(row, "first_seen_time", None)
                event_ts = to_ist_naive(last_seen) or to_ist_naive(first_seen)
                if event_ts is None or event_ts <= ts:
                    out.append(row)
            return out
        except Exception:
            logger.exception("StockAdvisor failed loading setup-state rows | symbol=%s time=%s", symbol, ts)
            return []

    def _signal_rows(self, *, symbol: str, ts: datetime) -> List[Any]:
        try:
            with get_trades_db() as db:
                return (
                    db.query(SignalORM)
                    .filter(SignalORM.equity_ref == symbol)
                    .filter(SignalORM.last_eval_time <= ts)
                    .all()
                )
        except Exception:
            logger.exception("StockAdvisor failed loading signal rows | symbol=%s time=%s", symbol, ts)
            return []

    def _trade_rows(self, *, symbol: str, ts: datetime, signal_ids: List[str]) -> List[Any]:
        try:
            with get_trades_db() as db:
                q = db.query(UserTradeORM).filter(UserTradeORM.equity_ref == symbol).filter(UserTradeORM.entry_time <= ts)
                if signal_ids:
                    q = q.filter(UserTradeORM.signal_id.in_(signal_ids))
                return q.all()
        except Exception:
            logger.exception("StockAdvisor failed loading trade rows | symbol=%s time=%s", symbol, ts)
            return []

    def _from_rows(self, *, symbol: str, ts: datetime, snapshots: List[SnapshotSchema], states: List[Any], signals: List[Any], trades: List[Any]) -> StockAdvisorDayContext:
        if not snapshots:
            snapshots = []

        vwap_sides = [_vwap_side(s) for s in snapshots]
        hma_states = [_hma_state(s) for s in snapshots]
        structure_states = [_structure_state(s) for s in snapshots]
        atr_values = [x for x in (_atr_value(s) for s in snapshots) if x is not None]
        day_ranges = [x for x in (_day_range_pct(s) for s in snapshots) if x is not None]
        current = snapshots[-1] if snapshots else None

        vwap_cross_count = _count_side_flips(vwap_sides)
        above_count = sum(1 for x in vwap_sides if x == "ABOVE")
        below_count = sum(1 for x in vwap_sides if x == "BELOW")
        side_count = max(1, above_count + below_count)
        vwap_above_ratio = round(above_count / side_count, 4)
        vwap_below_ratio = round(below_count / side_count, 4)

        hma_flip_count = _count_side_flips(hma_states)
        structure_flip_count = _count_side_flips(structure_states)
        context_flip_count = vwap_cross_count + hma_flip_count + structure_flip_count

        atr_start = atr_values[0] if atr_values else None
        atr_current = atr_values[-1] if atr_values else None
        atr_change_pct = _pct_change(atr_start, atr_current)

        day_start = day_ranges[0] if day_ranges else None
        day_current = day_ranges[-1] if day_ranges else None
        lookback_range = day_ranges[-10] if len(day_ranges) >= 10 else day_start
        day_recent_growth = None if lookback_range is None or day_current is None else day_current - lookback_range
        recent_range = _recent_range_pct(current) if current else None

        move30 = _move_atr(current, "30m") if current else None
        move60 = _move_atr(current, "60m") if current else None
        range_pos = _range_position(current) if current else None
        vwap_context = self._vwap_context(vwap_cross_count, vwap_above_ratio, vwap_below_ratio)
        atr_context = self._atr_context(atr_change_pct=atr_change_pct, recent_range=recent_range, day_recent_growth=day_recent_growth)
        range_context = self._range_context(day_current=day_current, recent_range=recent_range, day_recent_growth=day_recent_growth, range_pos=range_pos)
        trend_context, preferred, avoid = self._trend_context(move30=move30, move60=move60, range_pos=range_pos, vwap_context=vwap_context)
        chop_context = self._chop_context(vwap_cross_count=vwap_cross_count, context_flip_count=context_flip_count, recent_range=recent_range)

        terminal_states = {"CONSUMED", "INVALIDATED", "EXPIRED", "COOLDOWN", "CANCELLED", "FAILED"}
        failed_states = {"INVALIDATED", "EXPIRED", "FAILED", "CANCELLED"}
        prior_terminal = 0
        prior_failed = 0
        prior_expired = 0
        same_side_failed: Dict[str, int] = {"BUY": 0, "SELL": 0}
        for row in states:
            state = _norm_upper(getattr(row, "state", ""))
            side = _norm_upper(getattr(row, "side", ""))
            if state in terminal_states:
                prior_terminal += 1
            if state in failed_states:
                prior_failed += 1
                if side in same_side_failed:
                    same_side_failed[side] += 1
            if state == "EXPIRED":
                prior_expired += 1

        no_mfe = 0
        fast_invalid = 0
        for row in signals:
            try:
                max_pnl = _safe_float(getattr(row, "max_pnl", None)) or 0.0
                status = _norm_upper(getattr(row, "status", ""))
                stage = _norm_upper(getattr(row, "stage", ""))
                if max_pnl <= 0.10:
                    no_mfe += 1
                if status in {"INVALIDATED", "CLOSED"} and stage in {"FORCE_EXIT", "INVALIDATED"} and max_pnl <= 0.15:
                    fast_invalid += 1
            except Exception:
                continue

        loss_trades = 0
        for row in trades:
            pnl = _safe_float(getattr(row, "pnl", None))
            if pnl is not None and pnl < 0:
                loss_trades += 1

        attempt_context = self._attempt_context(prior_failed=prior_failed, no_mfe=no_mfe, fast_invalid=fast_invalid, loss_trades=loss_trades)
        reason_codes = [
            vwap_context,
            atr_context,
            range_context,
            trend_context,
            chop_context,
            attempt_context,
        ]
        reason_codes = [x for x in reason_codes if x and x != "UNKNOWN"]

        return StockAdvisorDayContext(
            symbol=symbol,
            snapshot_time=ts.isoformat(sep=" "),
            snapshot_count=len(snapshots),
            vwap_cross_count=vwap_cross_count,
            vwap_above_ratio=vwap_above_ratio,
            vwap_below_ratio=vwap_below_ratio,
            vwap_context=vwap_context,
            hma_flip_count=hma_flip_count,
            structure_flip_count=structure_flip_count,
            context_flip_count=context_flip_count,
            chop_context=chop_context,
            atr_start=atr_start,
            atr_current=atr_current,
            atr_change_pct=round(atr_change_pct, 4) if atr_change_pct is not None else None,
            atr_context=atr_context,
            day_range_start_pct=day_start,
            day_range_current_pct=day_current,
            day_range_recent_growth_pct=round(day_recent_growth, 4) if day_recent_growth is not None else None,
            recent_range_pct=recent_range,
            range_context=range_context,
            trend_context=trend_context,
            preferred_direction=preferred,
            avoid_direction=avoid,
            prior_setup_states=len(states),
            prior_terminal_setup_states=prior_terminal,
            prior_failed_setup_states=prior_failed,
            prior_expired_setup_states=prior_expired,
            prior_same_side_failed=same_side_failed,
            prior_signals=len(signals),
            prior_no_mfe_signals=no_mfe,
            prior_fast_invalidations=fast_invalid,
            prior_trade_rows=len(trades),
            prior_trade_loss_rows=loss_trades,
            attempt_context=attempt_context,
            reason_codes=reason_codes,
        )

    @staticmethod
    def _vwap_context(crosses: int, above_ratio: float, below_ratio: float) -> str:
        if crosses >= 6:
            return "VWAP_CHOP_REPEATED_CROSSES"
        if above_ratio >= 0.70:
            return "VWAP_ACCEPTED_ABOVE"
        if below_ratio >= 0.70:
            return "VWAP_ACCEPTED_BELOW"
        if crosses >= 3:
            return "VWAP_MIXED"
        return "VWAP_UNKNOWN"

    @staticmethod
    def _atr_context(*, atr_change_pct: Optional[float], recent_range: Optional[float], day_recent_growth: Optional[float]) -> str:
        if atr_change_pct is None:
            return "ATR_UNKNOWN"
        if atr_change_pct <= -20.0:
            if (recent_range or 0.0) <= 0.35:
                return "ATR_CONTRACTING_AFTER_SPIKE"
            return "ATR_CONTRACTING_FROM_OPEN"
        if atr_change_pct >= 20.0:
            if (day_recent_growth or 0.0) <= 0.05:
                return "ATR_EXPANDING_WITH_CHOP"
            return "ATR_EXPANDING"
        return "ATR_STABLE"

    @staticmethod
    def _range_context(*, day_current: Optional[float], recent_range: Optional[float], day_recent_growth: Optional[float], range_pos: Optional[float]) -> str:
        day_current = day_current or 0.0
        recent_range = recent_range or 0.0
        day_recent_growth = day_recent_growth or 0.0
        if range_pos is not None and 0.35 <= range_pos <= 0.65 and recent_range <= 0.30:
            return "RANGE_MIDDLE_NO_EDGE"
        if day_current >= 0.90 and recent_range <= 0.25 and day_recent_growth <= 0.05:
            return "POST_SPIKE_COMPRESSION"
        if day_recent_growth <= 0.03 and recent_range <= 0.35:
            return "RANGE_EXPANSION_STALLED"
        if day_recent_growth >= 0.10:
            return "RANGE_EXPANSION_CONTINUING"
        return "RANGE_STABLE"

    @staticmethod
    def _trend_context(*, move30: Optional[float], move60: Optional[float], range_pos: Optional[float], vwap_context: str) -> tuple[str, str, str]:
        move30 = move30 or 0.0
        move60 = move60 or 0.0
        if move30 >= 1.0 and move60 >= 1.5 and (range_pos is None or range_pos >= 0.65) and vwap_context == "VWAP_ACCEPTED_ABOVE":
            return "PERSISTENT_UPTREND", "BUY", "SELL"
        if move30 <= -1.0 and move60 <= -1.5 and (range_pos is None or range_pos <= 0.35) and vwap_context == "VWAP_ACCEPTED_BELOW":
            return "PERSISTENT_DOWNTREND", "SELL", "BUY"
        if move30 > 0.5 and vwap_context == "VWAP_ACCEPTED_ABOVE":
            return "UPTREND_DEVELOPING", "BUY", "SELL"
        if move30 < -0.5 and vwap_context == "VWAP_ACCEPTED_BELOW":
            return "DOWNTREND_DEVELOPING", "SELL", "BUY"
        return "NO_PERSISTENT_TREND", "NEUTRAL", "NEUTRAL"

    @staticmethod
    def _chop_context(*, vwap_cross_count: int, context_flip_count: int, recent_range: Optional[float]) -> str:
        recent_range = recent_range or 0.0
        if vwap_cross_count >= 6 or context_flip_count >= 10:
            return "HIGH_CONTEXT_FLIP_CHOP"
        if vwap_cross_count >= 3 or context_flip_count >= 6:
            return "MODERATE_CONTEXT_FLIP_CHOP"
        if recent_range <= 0.18:
            return "LOW_RECENT_RANGE"
        return "LOW_CHOP"

    @staticmethod
    def _attempt_context(*, prior_failed: int, no_mfe: int, fast_invalid: int, loss_trades: int) -> str:
        if prior_failed >= 3 or no_mfe >= 3 or fast_invalid >= 2:
            return "MULTIPLE_FAILED_ATTEMPTS"
        if no_mfe >= 1 or fast_invalid >= 1 or loss_trades >= 1:
            return "PRIOR_ATTEMPT_WEAK_FOLLOW_THROUGH"
        return "NO_RECENT_FAILURE"
