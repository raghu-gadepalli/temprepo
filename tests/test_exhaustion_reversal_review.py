#!/usr/bin/env python3
"""Focused EXHAUSTION_REVERSAL candidate-funnel review.

Edit TEST SETTINGS and run:

    python tests/test_exhaustion_reversal_review.py

This report is read-only. It replays persisted snapshots in chronological order
and records every EXHAUSTION_REVERSAL candidate returned by SetupDiscoverer,
including direct extreme discovery, WATCH rows, delayed WATCH promotion,
price-action confirmation, location/tradability blocks, signal creation and
forward MFE/MAE.

Two CSVs are written:

* exhaustion_reversal_review.csv
    One row per candidate evaluation.
* exhaustion_reversal_event_review.csv
    One compact row per watched/direct exhaustion event.

The scalar context columns intentionally overlap with the breakout review
reports so ACCEPTED_BREAKOUT, FAILED_BREAKOUT and EXHAUSTION_REVERSAL can later
be compared one stock at a time against the same snapshots. The report never
mutates snapshots, signals, setup state or trades.
"""

from __future__ import annotations

import csv
import logging
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from database.database import get_trades_db
from logconfig import setup_logging
from models.trade_models import Signal as SignalORM
from models.trade_models import Snapshot as SnapshotORM
from models.trade_models import StockSetupState as StockSetupStateORM
from schemas.snapshot import SnapshotSchema
from services.evidence.setup_discovery_helper import SetupCandidate, SetupDiscoverer
from utils.json_utils import sanitize_json


# =============================================================================
# TEST SETTINGS
# =============================================================================
# None = latest snapshot date in DB. Or set explicitly, e.g. "2026-07-17".
TEST_DATE: str = "2026-07-20"

OUTPUT_CSV_PATH: str = "exhaustion_reversal_review.csv"
OUTPUT_EVENT_CSV_PATH: str = "exhaustion_reversal_event_review.csv"

# Empty list = all symbols. For one-stock comparison use e.g. ["ICICIPRULI"].
SYMBOL_FILTER: List[str] = []

# Optional snapshot-time window in HH:MM or HH:MM:SS. None = full selected day.
START_TIME: Optional[str] = None
END_TIME: Optional[str] = None

# With 3-minute snapshots these are approximately 9/18/27 minutes.
HORIZON_BARS: Tuple[int, ...] = (3, 6, 9)

# Fail loudly on malformed eligible rows rather than silently hiding evidence.
FAIL_ON_ROW_ERROR: bool = True

# Optional quick-test cap after filtering. None = all candidate rows.
MAX_RECORDS: Optional[int] = None

PRINT_TOP_N: int = 100
PROGRESS_EVERY: int = 25
LOG_FILE: str = "test_exhaustion_reversal_review.log"

SETUP_LABEL = "EXHAUSTION_REVERSAL"


# =============================================================================
# Generic helpers
# =============================================================================
def _upper(value: Any) -> str:
    if hasattr(value, "value"):
        value = value.value
    return str(value or "").strip().upper()


def _text(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "value"):
        value = value.value
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(sep=" ")
    return str(value)


def _num(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        if hasattr(value, "value"):
            value = value.value
        text = str(value).strip()
        if not text or text.lower() in {"none", "null", "nan"}:
            return None
        return float(text)
    except Exception:
        return None


def _int(value: Any) -> Optional[int]:
    number = _num(value)
    return int(number) if number is not None else None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=0)
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "null", "nan"}:
        return None
    try:
        return datetime.fromisoformat(text.replace("T", " ").split("+")[0]).replace(
            tzinfo=None,
            microsecond=0,
        )
    except Exception:
        return None


def _nested(mapping: Any, *keys: str, default: Any = None) -> Any:
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def _pct(points: Optional[float], base: Optional[float]) -> Optional[float]:
    if points is None or base in (None, 0):
        return None
    return points / float(base) * 100.0


def _ratio(points: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if points is None or denominator in (None, 0):
        return None
    return points / float(denominator)


def _median(values: Iterable[Any]) -> Optional[float]:
    numbers = [number for number in (_num(value) for value in values) if number is not None]
    return statistics.median(numbers) if numbers else None


def _parse_time_value(value: Optional[str]) -> Optional[Tuple[int, int, int]]:
    if not value:
        return None
    parts = [int(part) for part in str(value).strip().split(":")]
    if len(parts) == 2:
        return parts[0], parts[1], 0
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    raise ValueError(f"Invalid time value: {value!r}. Use HH:MM or HH:MM:SS.")


def _combine_time(day_start: datetime, value: Optional[str]) -> Optional[datetime]:
    parsed = _parse_time_value(value)
    if not parsed:
        return None
    hour, minute, second = parsed
    return day_start.replace(hour=hour, minute=minute, second=second, microsecond=0)


def _selected_day_start() -> Optional[datetime]:
    if TEST_DATE:
        return datetime.fromisoformat(str(TEST_DATE).strip()).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
            tzinfo=None,
        )
    with get_trades_db() as db:
        row = db.query(SnapshotORM).order_by(SnapshotORM.snapshot_time.desc()).first()
    if row is None or row.snapshot_time is None:
        return None
    return row.snapshot_time.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


def _symbol_filter() -> List[str]:
    return sorted({_upper(symbol) for symbol in SYMBOL_FILTER if _upper(symbol)})


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(item) for item in value if str(item).strip())
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(sep=" ")
    return value


# =============================================================================
# Database loading and indexing
# =============================================================================
def _fetch_snapshot_rows(day_start: datetime) -> List[SnapshotORM]:
    start_dt = _combine_time(day_start, START_TIME) or day_start
    end_dt = _combine_time(day_start, END_TIME) or (day_start + timedelta(days=1))
    if end_dt <= start_dt:
        raise ValueError(f"Invalid review window: {START_TIME!r} -> {END_TIME!r}")

    symbols = _symbol_filter()
    with get_trades_db() as db:
        query = (
            db.query(SnapshotORM)
            .filter(SnapshotORM.snapshot_time >= start_dt)
            .filter(SnapshotORM.snapshot_time < end_dt)
        )
        if symbols:
            query = query.filter(SnapshotORM.symbol.in_(symbols))
        return query.order_by(SnapshotORM.symbol.asc(), SnapshotORM.snapshot_time.asc()).all()


def _prepare_snapshot_series(
    rows: Sequence[SnapshotORM],
) -> Tuple[
    Dict[str, List[Tuple[datetime, Dict[str, Any]]]],
    Dict[Tuple[str, datetime], int],
    List[Tuple[SnapshotORM, Dict[str, Any]]],
]:
    by_symbol: Dict[str, List[Tuple[datetime, Dict[str, Any]]]] = defaultdict(list)
    loaded: List[Tuple[SnapshotORM, Dict[str, Any]]] = []

    for record in rows:
        if not isinstance(record.data, dict) or not record.data:
            raise ValueError(f"Snapshot payload missing | symbol={record.symbol} time={record.snapshot_time}")
        snapshot = SnapshotSchema.from_db_dict(record.data)
        data = sanitize_json(snapshot.model_dump(mode="python", by_alias=True, exclude_none=False))
        timestamp = _parse_dt(snapshot.snapshot_time)
        if timestamp is None:
            raise ValueError(f"Snapshot timestamp missing | symbol={record.symbol}")
        symbol = _upper(snapshot.symbol or record.symbol)
        by_symbol[symbol].append((timestamp, data))
        loaded.append((record, data))

    positions: Dict[Tuple[str, datetime], int] = {}
    for symbol, series in by_symbol.items():
        series.sort(key=lambda item: item[0])
        for index, (timestamp, _) in enumerate(series):
            positions[(symbol, timestamp)] = index
    return by_symbol, positions, loaded


def _signal_time_values(signal: SignalORM) -> List[datetime]:
    values: List[datetime] = []
    for raw in (
        signal.qualified_time,
        signal.actionable_time,
        signal.first_seen_time,
        signal.last_snapshot_time,
        signal.last_eval_time,
    ):
        parsed = _parse_dt(raw)
        if parsed is not None and parsed not in values:
            values.append(parsed)
    return values


def _fetch_signal_index(day_start: datetime) -> Dict[Tuple[str, str, datetime], List[SignalORM]]:
    day_end = day_start + timedelta(days=1)
    symbols = _symbol_filter()
    with get_trades_db() as db:
        query = (
            db.query(SignalORM)
            .filter(SignalORM.setup == SETUP_LABEL)
            .filter(SignalORM.last_eval_time >= day_start)
            .filter(SignalORM.last_eval_time < day_end)
        )
        if symbols:
            query = query.filter(SignalORM.equity_ref.in_(symbols))
        signals = query.order_by(SignalORM.last_eval_time.asc()).all()

    index: Dict[Tuple[str, str, datetime], List[SignalORM]] = defaultdict(list)
    for signal in signals:
        side = _upper(signal.side)
        identity_symbols = {_upper(signal.equity_ref), _upper(signal.symbol)} - {""}
        for timestamp in _signal_time_values(signal):
            for symbol in identity_symbols:
                index[(symbol, side, timestamp)].append(signal)
    return index


def _signal_for_candidate(
    signal_index: Dict[Tuple[str, str, datetime], List[SignalORM]],
    *,
    symbol: str,
    side: str,
    timestamp: datetime,
) -> Optional[SignalORM]:
    matches = signal_index.get((_upper(symbol), _upper(side), _parse_dt(timestamp))) or []
    if not matches:
        return None
    return sorted(matches, key=lambda item: int(item.id or 0))[0]


def _fetch_setup_state_event_index(day_start: datetime) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Index current and archived EXHAUSTION_REVERSAL events by symbol/event key."""
    symbols = _symbol_filter()
    with get_trades_db() as db:
        query = (
            db.query(StockSetupStateORM)
            .filter(StockSetupStateORM.trading_day == day_start.date())
            .filter(StockSetupStateORM.setup == SETUP_LABEL)
        )
        if symbols:
            query = query.filter(StockSetupStateORM.equity_ref.in_(symbols))
        state_rows = query.all()

    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for state_row in state_rows:
        payload = state_row.state_json if isinstance(state_row.state_json, dict) else {}
        identity_symbols = {_upper(state_row.equity_ref), _upper(state_row.symbol)} - {""}
        watch = payload.get("watch") if isinstance(payload.get("watch"), dict) else {}
        current_key = str(payload.get("event_key") or watch.get("event_key") or "").strip()
        raw_transitions = payload.get("transition_history") if isinstance(payload.get("transition_history"), list) else []
        current_transitions = [
            item
            for item in raw_transitions
            if isinstance(item, dict)
            and (not item.get("event_key") or str(item.get("event_key")) == current_key)
        ]
        if current_key:
            record = {
                "persisted_event_found": True,
                "persisted_final_state": _upper(state_row.state),
                "persisted_final_reason": _text(state_row.state_reason),
                "persisted_signal_id": _text(state_row.signal_id),
                "persisted_first_seen_time": _text(state_row.first_seen_time),
                "persisted_last_seen_time": _text(state_row.last_seen_time),
                "persisted_expires_at": _text(state_row.expires_at),
                "persisted_transition_count": len(current_transitions),
            }
            for symbol in identity_symbols:
                index[(symbol, current_key)] = record

        history = payload.get("event_history") if isinstance(payload.get("event_history"), list) else []
        for archived in history:
            if not isinstance(archived, dict):
                continue
            event_key = str(archived.get("event_key") or "").strip()
            if not event_key:
                continue
            transitions = archived.get("transitions") if isinstance(archived.get("transitions"), list) else []
            record = {
                "persisted_event_found": True,
                "persisted_final_state": _upper(archived.get("final_state")),
                "persisted_final_reason": _text(archived.get("final_reason")),
                "persisted_signal_id": _text(archived.get("signal_id")),
                "persisted_first_seen_time": _text(archived.get("first_seen_time")),
                "persisted_last_seen_time": _text(archived.get("last_seen_time")),
                "persisted_expires_at": _text(archived.get("expires_at")),
                "persisted_transition_count": len(transitions),
            }
            for symbol in identity_symbols:
                index[(symbol, event_key)] = record
    return index


# =============================================================================
# Snapshot context and candidate evaluation
# =============================================================================
def _snapshot_with_recent_context(
    data: Dict[str, Any],
    *,
    series_by_symbol: Dict[str, List[Tuple[datetime, Dict[str, Any]]]],
    positions: Dict[Tuple[str, datetime], int],
) -> Dict[str, Any]:
    """Attach recent snapshots so WATCH reconstruction never queries DB per row."""
    symbol = _upper(data.get("symbol"))
    timestamp = _parse_dt(data.get("snapshot_time"))
    if not symbol or timestamp is None:
        return data
    current_index = positions.get((symbol, timestamp))
    series = series_by_symbol.get(symbol) or []
    if current_index is None:
        return data

    # Five completed candles are the current configured WATCH window. Keep a few
    # extra rows so future tuning can inspect reset/cooling context without
    # changing the report plumbing.
    recent = [row for _, row in series[max(0, current_index - 20) : current_index]]
    enriched = dict(data)
    enriched["_recent_snapshots"] = recent
    return enriched


def _evaluate_candidates(data: Dict[str, Any]) -> List[SetupCandidate]:
    discoverer = SetupDiscoverer(
        persist_setup_state=False,
        read_persistent_setup_state=False,
    )
    discoverer._snapshot = data
    return list(discoverer._discover_exhaustion_reversal())


def _candidate_origin(candidate: SetupCandidate) -> str:
    data = candidate.data if isinstance(candidate.data, dict) else {}
    promotion = data.get("watch_promotion") if isinstance(data.get("watch_promotion"), dict) else {}
    if promotion:
        if _bool(_nested(data, "price_action", "watch_relative_confirmed")):
            return "WATCH_RELATIVE_PROMOTION"
        return "WATCH_PROMOTION"
    watched = _nested(data, "setup_inputs", "watched_extreme", default={}) or {}
    if watched:
        return "DIRECT_WITH_PRIOR_WATCH"
    return "DIRECT_DISCOVERY"


def _watched_extreme(candidate: SetupCandidate) -> Dict[str, Any]:
    data = candidate.data if isinstance(candidate.data, dict) else {}
    watched = _nested(data, "setup_inputs", "watched_extreme", default={}) or {}
    if isinstance(watched, dict) and watched:
        return watched
    watched = _nested(data, "watch_promotion", "watched_extreme", default={}) or {}
    return watched if isinstance(watched, dict) else {}


def _event_identity(
    *,
    candidate: SetupCandidate,
    symbol: str,
    timestamp: datetime,
) -> Tuple[str, datetime, str]:
    watched = _watched_extreme(candidate)
    event_time = _parse_dt(watched.get("event_time") or watched.get("snapshot_time"))
    event_key = str(watched.get("event_key") or "").strip()
    event_source = str(watched.get("source") or "").strip()

    if event_time is None:
        event_time = timestamp
    if not event_key:
        data = candidate.data if isinstance(candidate.data, dict) else {}
        reference_price = _num(_nested(data, "setup_levels", "reference_price"))
        pieces = [SETUP_LABEL, _upper(candidate.side), event_time.isoformat()]
        if reference_price is not None:
            pieces.append(str(reference_price))
        event_key = "|".join(pieces)
    if not event_source:
        event_source = "DIRECT_CURRENT_EXTREME"
    return event_key, event_time, event_source


def _horizon_excursion(
    *,
    side: str,
    entry_price: Optional[float],
    future_rows: Sequence[Tuple[datetime, Dict[str, Any]]],
) -> Dict[str, Any]:
    if entry_price in (None, 0) or not future_rows:
        return {"mfe_pct": None, "mae_pct": None, "mfe_points": None, "mae_points": None}

    highs = [_num(_nested(data, "bar", "high")) for _, data in future_rows]
    lows = [_num(_nested(data, "bar", "low")) for _, data in future_rows]
    valid_highs = [value for value in highs if value is not None]
    valid_lows = [value for value in lows if value is not None]
    if not valid_highs or not valid_lows:
        return {"mfe_pct": None, "mae_pct": None, "mfe_points": None, "mae_points": None}

    if side == "BUY":
        mfe_points = max(valid_highs) - float(entry_price)
        mae_points = min(valid_lows) - float(entry_price)
    elif side == "SELL":
        mfe_points = float(entry_price) - min(valid_lows)
        mae_points = float(entry_price) - max(valid_highs)
    else:
        mfe_points = None
        mae_points = None

    return {
        "mfe_pct": _pct(mfe_points, entry_price),
        "mae_pct": _pct(mae_points, entry_price),
        "mfe_points": mfe_points,
        "mae_points": mae_points,
    }


def _horizon_columns(
    *,
    symbol: str,
    timestamp: datetime,
    side: str,
    entry_price: Optional[float],
    series_by_symbol: Dict[str, List[Tuple[datetime, Dict[str, Any]]]],
    positions: Dict[Tuple[str, datetime], int],
) -> Dict[str, Any]:
    output: Dict[str, Any] = {}
    series = series_by_symbol.get(symbol) or []
    current_index = positions.get((symbol, timestamp))
    if current_index is None:
        return output

    for bars in HORIZON_BARS:
        future = series[current_index + 1 : current_index + 1 + int(bars)]
        excursion = _horizon_excursion(side=side, entry_price=entry_price, future_rows=future)
        output[f"h{bars}_bars_available"] = len(future)
        output[f"h{bars}_mfe_pct"] = excursion["mfe_pct"]
        output[f"h{bars}_mae_pct"] = excursion["mae_pct"]
        output[f"h{bars}_mfe_points"] = excursion["mfe_points"]
        output[f"h{bars}_mae_points"] = excursion["mae_points"]
    return output


def _signal_columns(signal: Optional[SignalORM]) -> Dict[str, Any]:
    if signal is None:
        return {
            "signal_created": False,
            "signal_id": "",
            "signal_status": "",
            "signal_stage": "",
            "signal_status_reason": "",
            "signal_created_price": None,
            "signal_mfe_pct": None,
            "signal_mae_pct": None,
            "signal_closed_time": "",
        }
    return {
        "signal_created": True,
        "signal_id": signal.signal_id,
        "signal_status": _text(signal.status),
        "signal_stage": _text(signal.stage),
        "signal_status_reason": _text(signal.status_reason),
        "signal_created_price": _num(signal.created_price),
        "signal_mfe_pct": _num(signal.max_pnl),
        "signal_mae_pct": _num(signal.min_pnl),
        "signal_closed_time": _text(signal.closed_time),
    }


def _range_position(close: Optional[float], low: Optional[float], high: Optional[float]) -> Optional[float]:
    if close is None or low is None or high is None or high <= low:
        return None
    return (close - low) / (high - low)


def _candidate_row(
    *,
    record: SnapshotORM,
    data: Dict[str, Any],
    candidate: SetupCandidate,
    signal_index: Dict[Tuple[str, str, datetime], List[SignalORM]],
    state_index: Dict[Tuple[str, str], Dict[str, Any]],
    series_by_symbol: Dict[str, List[Tuple[datetime, Dict[str, Any]]]],
    positions: Dict[Tuple[str, datetime], int],
) -> Dict[str, Any]:
    symbol = _upper(data.get("symbol") or record.symbol)
    timestamp = _parse_dt(data.get("snapshot_time") or record.snapshot_time)
    if timestamp is None:
        raise ValueError(f"Missing snapshot_time | symbol={symbol}")

    side = _upper(candidate.side)
    candidate_data = candidate.data if isinstance(candidate.data, dict) else {}
    setup_inputs = candidate_data.get("setup_inputs") if isinstance(candidate_data.get("setup_inputs"), dict) else {}
    price_action = candidate_data.get("price_action") if isinstance(candidate_data.get("price_action"), dict) else {}
    location = candidate_data.get("entry_location_filter") if isinstance(candidate_data.get("entry_location_filter"), dict) else {}
    setup_levels = candidate_data.get("setup_levels") if isinstance(candidate_data.get("setup_levels"), dict) else {}
    promotion = candidate_data.get("watch_promotion") if isinstance(candidate_data.get("watch_promotion"), dict) else {}
    watched = _watched_extreme(candidate)

    event_key, event_time, event_source = _event_identity(
        candidate=candidate,
        symbol=symbol,
        timestamp=timestamp,
    )

    close = _num(_nested(data, "bar", "close")) or _num(data.get("close"))
    open_price = _num(_nested(data, "bar", "open"))
    high = _num(_nested(data, "bar", "high"))
    low = _num(_nested(data, "bar", "low"))
    atr = _num(_nested(data, "indicators", "atr", "value"))
    vwap = _num(_nested(data, "indicators", "vwap", "value"))
    vwap_gap_pct = _num(_nested(data, "indicators", "vwap", "distance_pct"))

    accepted_low = _num(_nested(data, "structure", "accepted", "range", "low"))
    accepted_high = _num(_nested(data, "structure", "accepted", "range", "high"))
    range_position = _range_position(close, accepted_low, accepted_high)

    watch_reference = _num(watched.get("low" if side == "BUY" else "high"))
    signal_reference = _num(setup_levels.get("reference_price"))
    reference_price = signal_reference if signal_reference is not None else watch_reference
    move_from_reference_points = None
    if close is not None and reference_price is not None:
        move_from_reference_points = close - reference_price if side == "BUY" else reference_price - close

    first_move_filter = setup_inputs.get("first_move_consumed_filter") if isinstance(setup_inputs.get("first_move_consumed_filter"), dict) else {}
    if not first_move_filter:
        first_move_filter = location.get("first_move_consumed_filter") if isinstance(location.get("first_move_consumed_filter"), dict) else {}
    directional_vwap = location.get("directional_vwap_room_filter") if isinstance(location.get("directional_vwap_room_filter"), dict) else {}
    pa_quality = location.get("price_action_quality_filter") if isinstance(location.get("price_action_quality_filter"), dict) else {}
    promotion_filter = promotion.get("promotion_filter") if isinstance(promotion.get("promotion_filter"), dict) else {}
    watch_relative = promotion.get("watch_relative_confirmation") if isinstance(promotion.get("watch_relative_confirmation"), dict) else {}
    strong_window = setup_inputs.get("watch_promotion_strong_window_confirmation") if isinstance(setup_inputs.get("watch_promotion_strong_window_confirmation"), dict) else {}

    signal = _signal_for_candidate(
        signal_index,
        symbol=symbol,
        side=side,
        timestamp=timestamp,
    )

    persisted = state_index.get((symbol, event_key), {})
    risk_flags = list(candidate.risk_flags or [])
    row: Dict[str, Any] = {
        # Identity and lifecycle
        "snapshot_time": timestamp,
        "symbol": symbol,
        "setup": SETUP_LABEL,
        "candidate_side": side,
        "candidate_origin": _candidate_origin(candidate),
        "event_key": event_key,
        "event_time": event_time,
        "event_source": event_source,
        "watch_age_bars": _int(watched.get("age_bars")),
        "watch_age_minutes": _num(watched.get("age_minutes")),
        "candidate_present": True,
        "candidate_discovered": bool(candidate.discovered),
        "price_action_confirmed": bool(candidate.price_action_confirmed),
        "price_action_strength": _num(candidate.price_action_strength),
        "entry_blocked": bool(candidate.entry_blocked),
        "entry_ready": bool(candidate.price_action_confirmed and not candidate.entry_blocked),
        "entry_ready_not_created": bool(candidate.price_action_confirmed and not candidate.entry_blocked and signal is None),
        "blocked_by": _text(candidate.blocked_by),
        "candidate_reason_code": _text(candidate.reason_code),
        "candidate_reason_text": _text(candidate.reason_text),
        "evidence_state": _text(candidate.evidence_state),
        "risk_flags": ",".join(dict.fromkeys(str(flag) for flag in risk_flags if str(flag).strip())),

        # Current bar and common indicators
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "atr": atr,
        "atr_pct": _num(_nested(data, "indicators", "atr", "pct")),
        "rsi": _num(_nested(data, "indicators", "rsi", "value")),
        "rsi_zone": _text(_nested(data, "indicators", "rsi", "zone")),
        "bollinger_position": _num(_nested(data, "indicators", "bollinger", "position")),
        "bollinger_zone": _text(_nested(data, "indicators", "bollinger", "zone")),
        "bollinger_width": _num(_nested(data, "indicators", "bollinger", "bb_width")),
        "vwap": vwap,
        "vwap_side": _text(_nested(data, "indicators", "vwap", "side")),
        "vwap_gap_pct": vwap_gap_pct,
        "vwap_gap_atr": _num(_nested(data, "indicators", "vwap", "distance_atr")),
        "adx": _num(_nested(data, "indicators", "adx", "value")),
        "adx_band": _text(_nested(data, "indicators", "adx", "band")),
        "hma_state": _text(_nested(data, "indicators", "hma", "state")),
        "hma_strength": _text(_nested(data, "indicators", "hma", "strength")),
        "bar_rvol": _num(_nested(data, "volume", "bar_rvol")),
        "bar_rvol_band": _text(_nested(data, "volume", "bar_rvol_band")),

        # Market-path context for common evidence / Advisor comparison
        "sod_position": _num(_nested(data, "market_windows", "sod", "close_position_in_range")),
        "sod_move_atr": _num(_nested(data, "market_windows", "sod", "move_atr")),
        "sod_move_pct": _num(_nested(data, "market_windows", "sod", "move_pct")),
        "position_15m": _num(_nested(data, "market_windows", "15m", "close_position_in_range")),
        "move_15m_atr": _num(_nested(data, "market_windows", "15m", "move_atr")),
        "position_30m": _num(_nested(data, "market_windows", "30m", "close_position_in_range")),
        "move_30m_atr": _num(_nested(data, "market_windows", "30m", "move_atr")),
        "position_60m": _num(_nested(data, "market_windows", "60m", "close_position_in_range")),
        "move_60m_atr": _num(_nested(data, "market_windows", "60m", "move_atr")),
        "price_slope_state": _text(_nested(data, "price_action", "slope", "state")),
        "price_slope_3_atr_per_bar": _num(_nested(data, "price_action", "slope", "bars_3_atr_per_bar")),
        "price_slope_5_atr_per_bar": _num(_nested(data, "price_action", "slope", "bars_5_atr_per_bar")),
        "hma_flip_count_today": _int(_nested(data, "state_context", "hma", "flip_count_today")),
        "hma_state_age_bars": _int(_nested(data, "state_context", "hma", "age_bars")),
        "structure_flip_count_today": _int(_nested(data, "state_context", "structure", "flip_count_today")),
        "structure_side_age_bars": _int(_nested(data, "state_context", "structure", "age_bars")),
        "vwap_side_age_bars": _int(_nested(data, "state_context", "vwap", "age_bars")),
        "option_sentiment_15m": _text(_nested(data, "derivatives", "option_sentiment_windows", "15m", "indication")),
        "future_sentiment_15m": _text(_nested(data, "derivatives", "future_sentiment_windows", "15m", "label")),

        # Current structural context, useful when comparing setup conflicts
        "structure_raw_state": _text(_nested(data, "structure", "raw", "state")),
        "structure_raw_side": _text(_nested(data, "structure", "raw", "side")),
        "structure_breakout_status": _text(_nested(data, "structure", "breakout", "status")),
        "structure_breakout_side": _text(_nested(data, "structure", "breakout", "side")),
        "accepted_range_id": _text(_nested(data, "structure", "accepted", "range", "range_id")),
        "accepted_range_version": _int(_nested(data, "structure", "accepted", "range", "version")),
        "accepted_range_source": _text(_nested(data, "structure", "accepted", "range", "source")),
        "accepted_range_low": accepted_low,
        "accepted_range_high": accepted_high,
        "accepted_range_width_atr": _num(_nested(data, "structure", "accepted", "range", "width_atr")),
        "position_in_accepted_range": range_position,
        "orb_high": _num(_nested(data, "levels", "opening_range", "high")),
        "orb_low": _num(_nested(data, "levels", "opening_range", "low")),
        "pdh": _num(_nested(data, "levels", "prev_day", "high")),
        "pdl": _num(_nested(data, "levels", "prev_day", "low")),

        # Exhaustion discovery context
        "discovery_rsi": _num(setup_inputs.get("rsi")),
        "discovery_bollinger_position": _num(setup_inputs.get("bollinger_position")),
        "discovery_bollinger_zone": _text(setup_inputs.get("bollinger_zone")),
        "discovery_sod_position": _num(setup_inputs.get("sod_position")),
        "discovery_position_30m": _num(setup_inputs.get("position_30m")),
        "discovery_sod_move_atr": _num(setup_inputs.get("sod_move_atr")),
        "discovery_move_30m_atr": _num(setup_inputs.get("move_30m_atr")),
        "watch_snapshot_time": _text(watched.get("snapshot_time")),
        "watch_open": _num(watched.get("open")),
        "watch_high": _num(watched.get("high")),
        "watch_low": _num(watched.get("low")),
        "watch_close": _num(watched.get("close")),
        "watch_rsi": _num(watched.get("rsi")),
        "watch_bollinger_position": _num(watched.get("bollinger_position")),
        "watch_bollinger_zone": _text(watched.get("bollinger_zone")),
        "watch_reference_price": watch_reference,
        "signal_reference_price": signal_reference,
        "move_from_reference_points": move_from_reference_points,
        "move_from_reference_atr": _ratio(move_from_reference_points, atr),
        "move_from_reference_pct": _pct(move_from_reference_points, reference_price),

        # Price action and promotion quality
        "pa_raw_confirmed": _bool(price_action.get("raw_confirmed")),
        "pa_single_candle_confirmed": _bool(price_action.get("single_candle_confirmed")),
        "pa_multi_candle_confirmed": _bool(price_action.get("multi_candle_confirmed")),
        "pa_watch_relative_confirmed": _bool(price_action.get("watch_relative_confirmed")),
        "pa_confirmation_mode": _text(price_action.get("confirmation_mode")),
        "pa_current_close_position": _num(price_action.get("current_close_position")),
        "pa_current_move_atr": _num(price_action.get("current_move_atr")),
        "pa_move_15m_atr": _num(price_action.get("move_15m_atr")),
        "pa_position_15m": _num(price_action.get("position_15m")),
        "promotion_code": _text(promotion.get("code") or setup_inputs.get("watch_promotion_code")),
        "promotion_filter_passes": _bool(promotion_filter.get("passes")),
        "promotion_filter_code": _text(promotion_filter.get("code") or promotion_filter.get("reason")),
        "watch_relative_passes": _bool(watch_relative.get("passes")),
        "watch_relative_strength": _num(watch_relative.get("strength")),
        "strong_window_passes": _bool(strong_window.get("passes")),
        "strong_window_move_atr": _num(strong_window.get("move_from_watch_extreme_atr")),

        # Location / first-move-consumed evidence
        "directional_vwap_room_blocked": _bool(directional_vwap.get("blocked")),
        "directional_vwap_room_code": _text(directional_vwap.get("code") or directional_vwap.get("reason")),
        "directional_vwap_min_room_pct": _num(directional_vwap.get("min_directional_vwap_room_pct")),
        "price_action_quality_blocked": bool(pa_quality),
        "price_action_quality_code": _text(pa_quality.get("code")),
        "first_move_filter_present": bool(first_move_filter),
        "first_move_filter_passes": _bool(first_move_filter.get("passes", True)) if first_move_filter else True,
        "first_move_filter_code": _text(first_move_filter.get("code") or first_move_filter.get("reason")),
        "first_move_consumed": bool(first_move_filter) and not _bool(first_move_filter.get("passes", True)),
        "first_move_move_atr": _num(
            first_move_filter.get("move_from_watch_extreme_atr")
            or first_move_filter.get("move_from_reference_atr")
        ),
        "first_move_vwap_consumed": _bool(first_move_filter.get("vwap_consumed")),

        # Persisted lifecycle
        "persisted_event_found": bool(persisted),
        "persisted_final_state": _text(persisted.get("persisted_final_state")),
        "persisted_final_reason": _text(persisted.get("persisted_final_reason")),
        "persisted_signal_id": _text(persisted.get("persisted_signal_id")),
        "persisted_first_seen_time": _text(persisted.get("persisted_first_seen_time")),
        "persisted_last_seen_time": _text(persisted.get("persisted_last_seen_time")),
        "persisted_expires_at": _text(persisted.get("persisted_expires_at")),
        "persisted_transition_count": _int(persisted.get("persisted_transition_count")) or 0,
    }

    row.update(_signal_columns(signal))
    row.update(
        _horizon_columns(
            symbol=symbol,
            timestamp=timestamp,
            side=side,
            entry_price=close,
            series_by_symbol=series_by_symbol,
            positions=positions,
        )
    )
    return row


# =============================================================================
# Event summary and output
# =============================================================================
def _event_summary_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get("symbol") or ""), str(row.get("event_key") or ""))].append(dict(row))

    events: List[Dict[str, Any]] = []
    for (symbol, event_key), evaluations in grouped.items():
        evaluations.sort(key=lambda item: str(item.get("snapshot_time") or ""))
        origin = dict(evaluations[0])
        latest = evaluations[-1]
        ready_rows = [item for item in evaluations if _bool(item.get("entry_ready"))]
        blocked_rows = [item for item in evaluations if _bool(item.get("entry_blocked"))]
        confirmed_rows = [item for item in evaluations if _bool(item.get("price_action_confirmed"))]
        signal_rows = [item for item in evaluations if _bool(item.get("signal_created"))]
        promotion_rows = [item for item in evaluations if "PROMOTION" in str(item.get("candidate_origin") or "")]

        blockers: List[str] = []
        for item in evaluations:
            for flag in str(item.get("risk_flags") or "").split(","):
                flag = flag.strip()
                if flag and flag not in blockers:
                    blockers.append(flag)
            blocked_by = str(item.get("blocked_by") or "").strip()
            if blocked_by and blocked_by not in blockers:
                blockers.append(blocked_by)

        primary_blocker = ""
        if blocked_rows:
            primary_blocker = str(blocked_rows[0].get("blocked_by") or "")
        elif blockers:
            primary_blocker = blockers[0]

        first_ready = ready_rows[0] if ready_rows else None
        signal_row = signal_rows[0] if signal_rows else None
        origin.update({
            "symbol": symbol,
            "event_key": event_key,
            "evaluation_rows": len(evaluations),
            "first_evaluation_time": evaluations[0].get("snapshot_time"),
            "last_evaluation_time": latest.get("snapshot_time"),
            "max_watch_age_bars": max((_int(item.get("watch_age_bars")) or 0) for item in evaluations),
            "ever_price_action_confirmed": bool(confirmed_rows),
            "ever_entry_blocked": bool(blocked_rows),
            "ever_entry_ready": bool(ready_rows),
            "ever_watch_promoted": bool(promotion_rows),
            "first_ready_time": first_ready.get("snapshot_time") if first_ready else "",
            "first_ready_origin": first_ready.get("candidate_origin") if first_ready else "",
            "event_primary_blocker": primary_blocker,
            "event_all_blockers": ",".join(blockers),
            "signal_created": bool(signal_rows),
            "signal_id": signal_row.get("signal_id") if signal_row else origin.get("signal_id"),
            "signal_created_time": signal_row.get("snapshot_time") if signal_row else "",
            "signal_created_after_watch": bool(signal_row and _parse_dt(signal_row.get("snapshot_time")) != _parse_dt(origin.get("event_time"))),
            "persisted_event_found": any(_bool(item.get("persisted_event_found")) for item in evaluations),
            "persisted_final_state": next(
                (item.get("persisted_final_state") for item in reversed(evaluations) if item.get("persisted_final_state")),
                origin.get("persisted_final_state"),
            ),
            "persisted_final_reason": next(
                (item.get("persisted_final_reason") for item in reversed(evaluations) if item.get("persisted_final_reason")),
                origin.get("persisted_final_reason"),
            ),
            "persisted_transition_count": max((_int(item.get("persisted_transition_count")) or 0) for item in evaluations),
        })

        # Keep both event-origin outcomes and the first tradable evaluation
        # outcomes. This makes delayed WATCH promotions comparable to direct
        # entries without losing what happened immediately after the extreme.
        for bars in HORIZON_BARS:
            origin[f"event_origin_h{bars}_mfe_pct"] = evaluations[0].get(f"h{bars}_mfe_pct")
            origin[f"event_origin_h{bars}_mae_pct"] = evaluations[0].get(f"h{bars}_mae_pct")
            origin[f"first_ready_h{bars}_mfe_pct"] = first_ready.get(f"h{bars}_mfe_pct") if first_ready else None
            origin[f"first_ready_h{bars}_mae_pct"] = first_ready.get(f"h{bars}_mae_pct") if first_ready else None
        events.append(origin)

    events.sort(key=lambda item: (str(item.get("event_time") or ""), str(item.get("symbol") or "")))
    return events


def _fieldnames(rows: Sequence[Dict[str, Any]]) -> List[str]:
    fields: List[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fields:
                fields.append(key)
    return fields


def _write_csv(path: str, rows: Sequence[Dict[str, Any]]) -> None:
    if not path:
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = _fieldnames(rows)
    with output.open("w", newline="", encoding="utf-8") as handle:
        if not fields:
            handle.write("")
            return
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(value) for key, value in row.items()})


def _count(rows: Sequence[Dict[str, Any]], key: str) -> Dict[str, int]:
    counter = Counter(str(row.get(key) or "") for row in rows)
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def _print_summary(rows: Sequence[Dict[str, Any]], events: Sequence[Dict[str, Any]], day_start: datetime) -> None:
    ready = [row for row in rows if _bool(row.get("entry_ready"))]
    blocked = [row for row in rows if _bool(row.get("entry_blocked"))]
    created_events = [row for row in events if _bool(row.get("signal_created"))]
    ready_events = [row for row in events if _bool(row.get("ever_entry_ready"))]
    blocked_events = [row for row in events if _bool(row.get("ever_entry_blocked"))]

    print("\nEXHAUSTION_REVERSAL review summary")
    print("-" * 120)
    print(f"date                       : {day_start.date()}")
    print(f"symbols_filter             : {_symbol_filter() or '(all)'}")
    print(f"time_window                : {START_TIME or '(start)'} -> {END_TIME or '(end)'}")
    print(f"candidate_evaluation_rows  : {len(rows)}")
    print(f"unique_exhaustion_events   : {len(events)}")
    print(f"price_action_confirmed     : {sum(_bool(row.get('price_action_confirmed')) for row in rows)}")
    print(f"blocked_candidates         : {len(blocked)}")
    print(f"entry_ready_rows           : {len(ready)}")
    print(f"entry_ready_events         : {len(ready_events)}")
    print(f"signals_created_events     : {len(created_events)}")
    print(f"signals_created_after_watch: {sum(_bool(row.get('signal_created_after_watch')) for row in events)}")
    print(f"side_counts_events         : {_count(events, 'candidate_side')}")
    print(f"origin_counts_events       : {_count(events, 'candidate_origin')}")
    print(f"primary_blockers_events    : {_count(blocked_events, 'event_primary_blocker')}")
    print(f"persisted_final_states     : {_count(events, 'persisted_final_state')}")
    for bars in HORIZON_BARS:
        print(f"median_origin_h{bars}_mfe_all   : {_median(row.get(f'event_origin_h{bars}_mfe_pct') for row in events)}")
        print(f"median_ready_h{bars}_mfe_ready  : {_median(row.get(f'first_ready_h{bars}_mfe_pct') for row in ready_events)}")
        print(f"median_origin_h{bars}_mfe_block : {_median(row.get(f'event_origin_h{bars}_mfe_pct') for row in blocked_events)}")
    print(f"evaluation_output          : {OUTPUT_CSV_PATH or '(disabled)'}")
    print(f"event_output               : {OUTPUT_EVENT_CSV_PATH or '(disabled)'}")

    print(f"\nFirst {min(PRINT_TOP_N, len(rows))} rows")
    print("-" * 120)
    for row in rows[: max(0, int(PRINT_TOP_N))]:
        print(
            f"{str(row.get('snapshot_time') or '')[:19]:19s} "
            f"{str(row.get('symbol') or ''):14s} "
            f"{str(row.get('candidate_side') or ''):4s} "
            f"origin={str(row.get('candidate_origin') or ''):25s} "
            f"age={str(row.get('watch_age_bars') if row.get('watch_age_bars') is not None else ''):>2s} "
            f"PA={str(_bool(row.get('price_action_confirmed'))):5s} "
            f"blocked={str(_bool(row.get('entry_blocked'))):5s} "
            f"ready={str(_bool(row.get('entry_ready'))):5s} "
            f"created={str(_bool(row.get('signal_created'))):5s} "
            f"h9_mfe={str(row.get('h9_mfe_pct') if row.get('h9_mfe_pct') is not None else ''):>9s} "
            f"reason={str(row.get('blocked_by') or row.get('candidate_reason_code') or '')}"
        )


# =============================================================================
# Main
# =============================================================================
def main() -> None:
    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)

    day_start = _selected_day_start()
    if day_start is None:
        raise RuntimeError("No snapshot date found. Set TEST_DATE or load snapshots first.")

    snapshot_rows = _fetch_snapshot_rows(day_start)
    series_by_symbol, positions, loaded = _prepare_snapshot_series(snapshot_rows)
    signal_index = _fetch_signal_index(day_start)
    state_index = _fetch_setup_state_event_index(day_start)

    # The setup helper uses class-level in-memory WATCH state in live execution.
    # Start the diagnostic from a clean memory and replay snapshots by symbol/time.
    SetupDiscoverer._exhaustion_watch_memory.clear()

    start_line = (
        f"Starting EXHAUSTION_REVERSAL review | source=db | date={day_start.date()} | "
        f"snapshots={len(loaded)} | symbols={_symbol_filter() or 'all'} | "
        f"window={START_TIME or 'start'}->{END_TIME or 'end'} | output={OUTPUT_CSV_PATH or 'disabled'}"
    )
    print(start_line, flush=True)
    logger.info(start_line)

    rows: List[Dict[str, Any]] = []
    processed = 0
    for record, data in loaded:
        processed += 1
        try:
            evaluation_data = _snapshot_with_recent_context(
                data,
                series_by_symbol=series_by_symbol,
                positions=positions,
            )
            candidates = _evaluate_candidates(evaluation_data)
            for candidate in candidates:
                rows.append(
                    _candidate_row(
                        record=record,
                        data=data,
                        candidate=candidate,
                        signal_index=signal_index,
                        state_index=state_index,
                        series_by_symbol=series_by_symbol,
                        positions=positions,
                    )
                )
                if MAX_RECORDS and int(MAX_RECORDS) > 0 and len(rows) >= int(MAX_RECORDS):
                    break
            if MAX_RECORDS and int(MAX_RECORDS) > 0 and len(rows) >= int(MAX_RECORDS):
                break
        except Exception:
            logger.exception("EXHAUSTION_REVERSAL review failed | symbol=%s time=%s", record.symbol, record.snapshot_time)
            if FAIL_ON_ROW_ERROR:
                raise

        if PROGRESS_EVERY and (processed % int(PROGRESS_EVERY) == 0 or processed == len(loaded)):
            line = f"Processed {processed}/{len(loaded)} snapshots | output_rows={len(rows)}"
            print(line, flush=True)
            logger.info(line)

    rows.sort(key=lambda item: (str(item.get("snapshot_time") or ""), str(item.get("symbol") or ""), str(item.get("candidate_side") or "")))
    event_rows = _event_summary_rows(rows)
    _write_csv(OUTPUT_CSV_PATH, rows)
    _write_csv(OUTPUT_EVENT_CSV_PATH, event_rows)
    _print_summary(rows, event_rows, day_start)


if __name__ == "__main__":
    main()
