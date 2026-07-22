#!/usr/bin/env python3
"""Focused FAILED_BREAKOUT candidate-funnel review.

Edit TEST SETTINGS and run:

    python tests/test_failed_breakout_review.py

This report is read-only. It evaluates snapshots in chronological order and
writes rows for the original FAILED_BREAKOUT event plus each subsequent
snapshot where the five-candle event watch remains active. This makes delayed
price-action confirmation visible even after structure.breakout.status has
moved away from FAILED_BREAKOUT.

RANGE_REABSORBED is intentionally not treated as a separate setup here. The
report defaults to the FAILED_BREAKOUT structure state only.

The report does not recompute StockAdvisor, does not change setup thresholds,
and does not mutate snapshots, signals, setup state, or trades. Room to VWAP,
accepted-range midpoint, and the opposite edge is recorded only as setup-quality
context; it is not signal-layer target or risk/reward logic.
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

from configs.evidence_config import EVIDENCE_CONFIG
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
# None = latest snapshot date in DB. Or set explicitly, e.g. "2026-07-10".
TEST_DATE: Optional[str] = None

OUTPUT_CSV_PATH: str = "failed_breakout_review.csv"
OUTPUT_EVENT_CSV_PATH: str = "failed_breakout_event_review.csv"

# Explicit symbols override all automatic scoping.
SYMBOL_FILTER: List[str] = []

# When no explicit filter is supplied, prefer symbols that actually have a
# FAILED_BREAKOUT setup-state row for the selected replay day. This keeps the
# diagnostic report aligned with a scoped replay instead of silently scanning
# the full snapshot table. If no setup-state rows exist, fall back to all.
AUTO_SCOPE_TO_SETUP_STATE_SYMBOLS: bool = True

# FAILED_BREAKOUT only by design. RANGE_REABSORBED is supporting structure
# context, not a separate CREATE-capable setup. These statuses identify the
# original event rows; later watch-window rows are included through candidate
# evaluation even when the current structure status has changed.
STRUCTURE_STATUSES: Tuple[str, ...] = ("FAILED_BREAKOUT",)

# Optional snapshot-time window in HH:MM or HH:MM:SS. None = full day.
START_TIME: Optional[str] = None
END_TIME: Optional[str] = None

# Event-outcome horizons requested for FAILED_BREAKOUT tuning. With 3-minute
# snapshots these are approximately 9/18/27 minutes after the event/evaluation.
HORIZON_BARS: Tuple[int, ...] = (3, 6, 9)

# Fail loudly on a malformed eligible snapshot instead of silently producing a
# partial tuning report.
FAIL_ON_ROW_ERROR: bool = True

# Optional quick-test cap after filtering. None = all eligible snapshots.
MAX_RECORDS: Optional[int] = None

PRINT_TOP_N: int = 100
PROGRESS_EVERY: int = 25
LOG_FILE: str = "test_failed_breakout_review.log"

SETUP_LABEL = "FAILED_BREAKOUT"
TERMINAL_SETUP_STATES = {"CONSUMED", "INVALIDATED", "EXPIRED", "SUPERSEDED", "DROPPED", "SIGNAL_CREATED", "COOLDOWN"}


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
        rec = db.query(SnapshotORM).order_by(SnapshotORM.snapshot_time.desc()).first()
    if rec is None or rec.snapshot_time is None:
        return None
    return rec.snapshot_time.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


_SYMBOL_SCOPE_CACHE: Dict[str, List[str]] = {}


def _symbol_filter(day_start: Optional[datetime] = None) -> List[str]:
    explicit = sorted({_upper(symbol) for symbol in SYMBOL_FILTER if _upper(symbol)})
    if explicit or not AUTO_SCOPE_TO_SETUP_STATE_SYMBOLS or day_start is None:
        return explicit

    cache_key = day_start.date().isoformat()
    if cache_key in _SYMBOL_SCOPE_CACHE:
        return list(_SYMBOL_SCOPE_CACHE[cache_key])

    with get_trades_db() as db:
        rows = (
            db.query(StockSetupStateORM.equity_ref, StockSetupStateORM.symbol)
            .filter(StockSetupStateORM.trading_day == day_start.date())
            .filter(StockSetupStateORM.setup == SETUP_LABEL)
            .all()
        )
    symbols = sorted({
        value
        for equity_ref, symbol in rows
        for value in (_upper(equity_ref), _upper(symbol))
        if value
    })
    _SYMBOL_SCOPE_CACHE[cache_key] = symbols
    return list(symbols)


def _pct(points: Optional[float], base: Optional[float]) -> Optional[float]:
    if points is None or base is None or base == 0:
        return None
    return points / base * 100.0


def _ratio(points: Optional[float], atr: Optional[float]) -> Optional[float]:
    if points is None or atr is None or atr == 0:
        return None
    return points / atr


def _directional_room(
    *,
    side: str,
    close: Optional[float],
    reference: Optional[float],
    atr: Optional[float],
) -> Dict[str, Any]:
    if close is None or reference is None:
        return {
            "points": None,
            "pct": None,
            "atr": None,
            "available": None,
        }
    if side == "BUY":
        points = reference - close
    elif side == "SELL":
        points = close - reference
    else:
        points = None
    return {
        "points": points,
        "pct": _pct(points, close),
        "atr": _ratio(points, atr),
        "available": points is not None and points > 0,
    }


def _median(values: Iterable[Any]) -> Optional[float]:
    nums = [number for number in (_num(value) for value in values) if number is not None]
    return statistics.median(nums) if nums else None


# =============================================================================
# Database loading and indexing
# =============================================================================
def _fetch_snapshot_rows(day_start: datetime) -> List[SnapshotORM]:
    start_dt = _combine_time(day_start, START_TIME) or day_start
    end_dt = _combine_time(day_start, END_TIME) or (day_start + timedelta(days=1))
    if end_dt <= start_dt:
        raise ValueError(f"Invalid review window: {START_TIME!r} -> {END_TIME!r}")

    symbols = _symbol_filter(day_start)
    with get_trades_db() as db:
        query = (
            db.query(SnapshotORM)
            .filter(SnapshotORM.snapshot_time >= start_dt)
            .filter(SnapshotORM.snapshot_time < end_dt)
        )
        if symbols:
            query = query.filter(SnapshotORM.symbol.in_(symbols))
        # Keep MySQL responsible only for filtering. A database-side ORDER BY
        # can exhaust sort_buffer_size on replay-sized snapshot tables even
        # though the selected day/symbol result set is modest.
        rows = query.all()

    rows.sort(
        key=lambda row: (
            _upper(row.symbol),
            _parse_dt(row.snapshot_time) or datetime.min,
        )
    )
    return rows


def _signal_time_values(signal: SignalORM) -> List[datetime]:
    times: List[datetime] = []
    for value in (
        signal.qualified_time,
        signal.actionable_time,
        signal.first_seen_time,
        signal.last_snapshot_time,
    ):
        parsed = _parse_dt(value)
        if parsed is not None and parsed not in times:
            times.append(parsed)
    return times


def _fetch_signal_index(day_start: datetime) -> Dict[Tuple[str, str, datetime], List[SignalORM]]:
    day_end = day_start + timedelta(days=1)
    symbols = _symbol_filter(day_start)
    with get_trades_db() as db:
        query = (
            db.query(SignalORM)
            .filter(SignalORM.setup == SETUP_LABEL)
            .filter(SignalORM.last_eval_time >= day_start)
            .filter(SignalORM.last_eval_time < day_end)
        )
        if symbols:
            query = query.filter(SignalORM.equity_ref.in_(symbols))
        # Avoid a MySQL filesort over the signals table. The SQL predicates
        # already reduce this to the requested setup/day/symbol scope, so sort
        # the small filtered result in Python instead. This prevents MySQL
        # error 1038 (Out of sort memory) without changing server settings.
        signals = query.all()

    signals.sort(
        key=lambda signal: (
            _parse_dt(signal.last_eval_time) or datetime.min,
            int(signal.id or 0),
        )
    )

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
    return sorted(matches, key=lambda signal: int(signal.id or 0))[0]


def _fetch_setup_state_event_index(day_start: datetime) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Index current and archived FAILED_BREAKOUT lifecycle outcomes by event key."""
    symbols = _symbol_filter(day_start)
    with get_trades_db() as db:
        query = (
            db.query(StockSetupStateORM)
            .filter(StockSetupStateORM.trading_day == day_start.date())
            .filter(StockSetupStateORM.setup == SETUP_LABEL)
        )
        if symbols:
            query = query.filter(StockSetupStateORM.equity_ref.in_(symbols))
        rows = query.all()

    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for state_row in rows:
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
                "persisted_transitions": sanitize_json(current_transitions),
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
                "persisted_transitions": sanitize_json(transitions),
            }
            for symbol in identity_symbols:
                index[(symbol, event_key)] = record
    return index


def _prepare_snapshot_series(
    rows: Sequence[SnapshotORM],
) -> Tuple[
    Dict[str, List[Tuple[datetime, Dict[str, Any]]]],
    Dict[Tuple[str, datetime], int],
    List[Tuple[SnapshotORM, Dict[str, Any]]],
]:
    by_symbol: Dict[str, List[Tuple[datetime, Dict[str, Any]]]] = defaultdict(list)
    loaded: List[Tuple[SnapshotORM, Dict[str, Any]]] = []

    for rec in rows:
        if not isinstance(rec.data, dict) or not rec.data:
            raise ValueError(f"Snapshot payload missing | symbol={rec.symbol} time={rec.snapshot_time}")
        snapshot = SnapshotSchema.from_db_dict(rec.data)
        data = sanitize_json(snapshot.model_dump(mode="python", by_alias=True, exclude_none=False))
        timestamp = _parse_dt(snapshot.snapshot_time)
        if timestamp is None:
            raise ValueError(f"Snapshot timestamp missing | symbol={rec.symbol}")
        symbol = _upper(snapshot.symbol or rec.symbol)
        by_symbol[symbol].append((timestamp, data))
        loaded.append((rec, data))

    position: Dict[Tuple[str, datetime], int] = {}
    for symbol, series in by_symbol.items():
        series.sort(key=lambda item: item[0])
        for index, (timestamp, _) in enumerate(series):
            position[(symbol, timestamp)] = index
    return by_symbol, position, loaded


# =============================================================================
# Candidate and outcome calculations
# =============================================================================
def _failed_breakout_observations(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return every neutral exact-level failed-breakout observation.

    Dynamic-range references rank ahead of ORB/previous-day references, but
    coincident prices remain separate causal structures.
    """
    discoverer = SetupDiscoverer(
        persist_setup_state=False,
        read_persistent_setup_state=False,
    )
    discoverer._snapshot = data
    failed = [
        item
        for item in discoverer._derived_breakout_observations(data)
        if _upper(item.get("status")) == "FAILED_BREAKOUT"
    ]
    return sorted(
        failed,
        key=lambda item: (
            int(item.get("rank") or 999),
            _parse_dt(item.get("failed_time")) or datetime.min,
            str(item.get("reference_id") or ""),
        ),
    )


def _failed_breakout_observation(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Compatibility view returning the preferred failed-breakout observation."""
    observations = _failed_breakout_observations(data)
    return observations[0] if observations else None


def _failed_breakout_watches_from_data(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    discoverer = SetupDiscoverer(
        persist_setup_state=False,
        read_persistent_setup_state=False,
    )
    discoverer._snapshot = data
    return discoverer._failed_breakout_watches_from_snapshot(
        data,
        age_bars=0,
        source="REVIEW_DERIVED_FAILED_BREAKOUT_EVENT",
    )


def _failed_breakout_watch_from_data(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Compatibility view returning the preferred failed-breakout WATCH."""
    watches = _failed_breakout_watches_from_data(data)
    return watches[0] if watches else None


def _structure_status(data: Dict[str, Any]) -> str:
    return "FAILED_BREAKOUT" if _failed_breakout_observation(data) is not None else "NONE"


def _candidate_missing_reason(data: Dict[str, Any]) -> str:
    breakout = _failed_breakout_observation(data)
    if breakout is None:
        return "STATUS_NOT_IN_FAILED_BREAKOUT_DISCOVERY_STATUSES"
    side = _upper(breakout.get("side"))
    if side not in {"BUY", "SELL"}:
        return "ORIGINAL_BREAKOUT_SIDE_MISSING_OR_INVALID"
    level = _num(breakout.get("price"))
    if side == "BUY":
        fallback = _num(_nested(data, "structure", "accepted", "range", "high"))
    else:
        fallback = _num(_nested(data, "structure", "accepted", "range", "low"))
    if level is None and fallback is None:
        return "FAILED_BREAKOUT_REFERENCE_LEVEL_MISSING"
    return "FAILED_BREAKOUT_HELPER_RETURNED_NO_CANDIDATE"


def _snapshot_with_recent_context(
    data: Dict[str, Any],
    *,
    series_by_symbol: Dict[str, List[Tuple[datetime, Dict[str, Any]]]],
    position: Dict[Tuple[str, datetime], int],
) -> Dict[str, Any]:
    """Attach the previous five snapshots so review does not query DB per row."""
    symbol = _upper(data.get("symbol"))
    timestamp = _parse_dt(data.get("snapshot_time"))
    if not symbol or timestamp is None:
        return data
    current_index = position.get((symbol, timestamp))
    series = series_by_symbol.get(symbol) or []
    if current_index is None:
        return data
    start = max(0, current_index - int(EVIDENCE_CONFIG.failed_breakout.watch_event_lookback_bars))
    recent = [row for _, row in series[start:current_index]]
    enriched = dict(data)
    enriched["_recent_snapshots"] = recent
    return enriched


def _persisted_transition_at(
    persisted: Dict[str, Any],
    timestamp: datetime,
) -> Tuple[str, Optional[datetime], str]:
    """Return the latest persisted lifecycle transition visible at timestamp."""
    transitions = persisted.get("persisted_transitions")
    if not isinstance(transitions, list):
        return "", None, ""
    parsed: List[Tuple[datetime, str, str]] = []
    for item in transitions:
        if not isinstance(item, dict):
            continue
        transition_time = _parse_dt(item.get("transition_time"))
        if transition_time is None or transition_time > timestamp:
            continue
        parsed.append((
            transition_time,
            _upper(item.get("state")),
            _text(item.get("state_reason")),
        ))
    if not parsed:
        return "", None, ""
    transition_time, state, reason = sorted(parsed, key=lambda item: item[0])[-1]
    return state, transition_time, reason


def _review_watches_and_candidates(
    data: Dict[str, Any],
    *,
    active_watches: Dict[Tuple[str, str], Dict[str, Any]],
    setup_state_event_index: Dict[Tuple[str, str], Dict[str, Any]],
) -> List[Tuple[Optional[SetupCandidate], Dict[str, Any], str, str]]:
    """Replay all distinct FAILED_BREAKOUT references with immutable memory."""
    discoverer = SetupDiscoverer(
        persist_setup_state=False,
        read_persistent_setup_state=False,
    )
    discoverer._snapshot = data
    symbol = _upper(data.get("symbol"))
    timestamp = _parse_dt(data.get("snapshot_time"))
    if not symbol or timestamp is None:
        raise ValueError("FAILED_BREAKOUT review requires symbol and snapshot_time")

    for key, stored in list(active_watches.items()):
        if key[0] != symbol:
            continue
        event_time = _parse_dt(stored.get("event_time"))
        valid_minutes = _num(stored.get("valid_minutes")) or float(EVIDENCE_CONFIG.failed_breakout.watch_event_valid_minutes)
        if event_time is None or timestamp > event_time + timedelta(minutes=valid_minutes):
            active_watches.pop(key, None)

    incoming_watches = discoverer._failed_breakout_watches_from_snapshot(
        data,
        age_bars=0,
        source="REVIEW_CURRENT_FAILED_BREAKOUT_EVENT",
    )
    for incoming in incoming_watches:
        event_key = _text(incoming.get("event_key"))
        if not event_key:
            continue
        key = (symbol, event_key)
        stored = active_watches.get(key)
        if isinstance(stored, dict) and discoverer._failed_breakout_same_live_event(
            stored_watch=stored,
            incoming_watch=incoming,
        ):
            continue
        active_watches[key] = dict(incoming)

    watches = [
        dict(stored)
        for (stored_symbol, _), stored in active_watches.items()
        if stored_symbol == symbol and isinstance(stored, dict)
    ]
    watches.sort(
        key=lambda item: (
            int(_int(item.get("level_rank")) or 999),
            _parse_dt(item.get("event_time")) or datetime.min,
            _text(item.get("reference_id")),
        )
    )

    results: List[Tuple[Optional[SetupCandidate], Dict[str, Any], str, str]] = []
    for raw_watch in watches:
        watch = dict(raw_watch)
        event_time = _parse_dt(watch.get("event_time"))
        if event_time is None:
            raise ValueError("FAILED_BREAKOUT review event_time missing from frozen WATCH")
        age_minutes = max(0.0, (timestamp - event_time).total_seconds() / 60.0)
        watch["age_minutes"] = round(age_minutes, 2)
        watch["age_bars"] = max(0, int(round(age_minutes / 3.0)))
        event_key = _text(watch.get("event_key"))
        key = (symbol, event_key)
        active_watches[key] = dict(watch)

        persisted = setup_state_event_index.get((symbol, event_key), {}) if event_key else {}
        state, transition_time, reason = _persisted_transition_at(persisted, timestamp)
        if state in TERMINAL_SETUP_STATES:
            consumed_on_current_snapshot = state == "CONSUMED" and transition_time == timestamp
            if not consumed_on_current_snapshot:
                active_watches.pop(key, None)
                results.append((None, watch, f"TERMINAL_SUPPRESSED_{state}", reason))
                continue

        candidate = discoverer._failed_breakout_candidate_from_watch(
            watch,
            persist_state=False,
        )
        results.append((candidate, watch, "", ""))
    return results


def _review_watch_and_candidate(
    data: Dict[str, Any],
    *,
    active_watches: Dict[Tuple[str, str], Dict[str, Any]],
    setup_state_event_index: Dict[Tuple[str, str], Dict[str, Any]],
) -> Tuple[Optional[SetupCandidate], Optional[Dict[str, Any]], str, str]:
    """Compatibility wrapper returning the preferred review candidate."""
    results = _review_watches_and_candidates(
        data,
        active_watches=active_watches,
        setup_state_event_index=setup_state_event_index,
    )
    return results[0] if results else (None, None, "", "")


def _horizon_excursion(
    *,
    side: str,
    entry_price: Optional[float],
    future_rows: Sequence[Tuple[datetime, Dict[str, Any]]],
) -> Dict[str, Any]:
    if entry_price is None or entry_price == 0 or not future_rows:
        return {
            "mfe_pct": None,
            "mae_pct": None,
            "mfe_points": None,
            "mae_points": None,
        }

    highs = [_num(_nested(data, "bar", "high")) for _, data in future_rows]
    lows = [_num(_nested(data, "bar", "low")) for _, data in future_rows]
    valid_highs = [value for value in highs if value is not None]
    valid_lows = [value for value in lows if value is not None]
    if not valid_highs or not valid_lows:
        return {
            "mfe_pct": None,
            "mae_pct": None,
            "mfe_points": None,
            "mae_points": None,
        }

    if side == "BUY":
        mfe_points = max(valid_highs) - entry_price
        mae_points = min(valid_lows) - entry_price
    elif side == "SELL":
        mfe_points = entry_price - min(valid_lows)
        mae_points = entry_price - max(valid_highs)
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
    position: Dict[Tuple[str, datetime], int],
) -> Dict[str, Any]:
    output: Dict[str, Any] = {}
    series = series_by_symbol.get(symbol) or []
    current_index = position.get((symbol, timestamp))
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


def _candidate_row(
    *,
    rec: SnapshotORM,
    data: Dict[str, Any],
    candidate: Optional[SetupCandidate],
    review_watch: Optional[Dict[str, Any]],
    runtime_suppression_state: str,
    runtime_suppression_reason: str,
    isolated_candidate_selected: bool,
    signal_index: Dict[Tuple[str, str, datetime], List[SignalORM]],
    setup_state_event_index: Dict[Tuple[str, str], Dict[str, Any]],
    series_by_symbol: Dict[str, List[Tuple[datetime, Dict[str, Any]]]],
    position: Dict[Tuple[str, datetime], int],
) -> Dict[str, Any]:
    symbol = _upper(data.get("symbol") or rec.symbol)
    timestamp = _parse_dt(data.get("snapshot_time") or rec.snapshot_time)
    if timestamp is None:
        raise ValueError(f"Missing snapshot_time | symbol={symbol}")

    review_event_watch = dict(review_watch or _failed_breakout_watch_from_data(data) or {})
    breakout = (
        dict(review_event_watch.get("level_observation") or {})
        if isinstance(review_event_watch.get("level_observation"), dict)
        else (_failed_breakout_observation(data) or {})
    )
    current_accepted = _nested(data, "structure", "accepted", "range", default={}) or {}

    close = _num(_nested(data, "bar", "close")) or _num(data.get("close"))
    atr = _num(_nested(data, "indicators", "atr", "value"))
    vwap = _num(_nested(data, "indicators", "vwap", "value"))
    vwap_gap_pct = _num(_nested(data, "indicators", "vwap", "distance_pct"))

    if candidate is None:
        current_original_side = _upper(review_event_watch.get("breakout_side") or breakout.get("side"))
        candidate_side = "SELL" if current_original_side == "BUY" else "BUY" if current_original_side == "SELL" else ""
        setup_inputs: Dict[str, Any] = {}
        location: Dict[str, Any] = {}
        price_action: Dict[str, Any] = {}
        setup_levels: Dict[str, Any] = {}
        candidate_dict: Dict[str, Any] = {}
    else:
        candidate_dict = sanitize_json(candidate.model_dump(mode="python"))
        candidate_side = _upper(candidate.side)
        candidate_data = candidate_dict.get("data") or {}
        setup_inputs = candidate_data.get("setup_inputs") or {}
        location = candidate_data.get("entry_location_filter") or {}
        price_action = candidate_data.get("price_action") or {}
        setup_levels = candidate_data.get("setup_levels") or {}

    watch = setup_inputs.get("failed_breakout_watch") or review_event_watch or {}
    event_accepted = watch.get("accepted_range") if isinstance(watch, dict) else None
    accepted = event_accepted if isinstance(event_accepted, dict) and event_accepted else current_accepted
    accepted_low = _num(accepted.get("low"))
    accepted_high = _num(accepted.get("high"))
    accepted_mid = (
        (accepted_low + accepted_high) / 2.0
        if accepted_low is not None and accepted_high is not None and accepted_high > accepted_low
        else None
    )
    derived_range_width_points = (
        accepted_high - accepted_low
        if accepted_low is not None and accepted_high is not None and accepted_high > accepted_low
        else None
    )
    frozen_range_width_points = _num(watch.get("accepted_range_width_points"))
    frozen_range_width_atr = _num(watch.get("accepted_range_width_atr"))
    range_width_points = (
        frozen_range_width_points
        if frozen_range_width_points is not None
        else derived_range_width_points
    )
    range_width_atr = (
        frozen_range_width_atr
        if frozen_range_width_atr is not None
        else _num(location.get("accepted_range_width_atr"))
    )
    range_width_source = (
        "EVENT_FROZEN"
        if frozen_range_width_atr is not None
        else _text(location.get("accepted_range_width_source") or "CURRENT_EVALUATION")
    )
    range_width_pct = _pct(range_width_points, close)

    original_side = _upper(setup_inputs.get("breakout_side") or watch.get("breakout_side") or breakout.get("side"))
    implied_candidate_side = "SELL" if original_side == "BUY" else "BUY" if original_side == "SELL" else ""
    if not candidate_side:
        candidate_side = implied_candidate_side

    level = setup_inputs.get("failed_level") or watch.get("failed_level") or {}
    level_price = _num(setup_inputs.get("level_price")) or _num(level.get("price")) or _num(location.get("level_price"))
    level_type = setup_inputs.get("level_type") or level.get("level_type")
    level_source = setup_inputs.get("level_source") or level.get("source")

    midpoint_room = _directional_room(side=candidate_side, close=close, reference=accepted_mid, atr=atr)
    vwap_room = _directional_room(side=candidate_side, close=close, reference=vwap, atr=atr)
    opposite_edge = accepted_high if candidate_side == "BUY" else accepted_low if candidate_side == "SELL" else None
    opposite_edge_room = _directional_room(side=candidate_side, close=close, reference=opposite_edge, atr=atr)

    quality_contexts = [
        ("RANGE_MIDPOINT", accepted_mid, midpoint_room),
        ("VWAP", vwap, vwap_room),
        ("OPPOSITE_RANGE_EDGE", opposite_edge, opposite_edge_room),
    ]
    available_contexts = [item for item in quality_contexts if item[2].get("available")]
    nearest_context = min(available_contexts, key=lambda item: float(item[2]["points"])) if available_contexts else None

    confirmed = bool(candidate.price_action_confirmed) if candidate is not None else False
    blocked = bool(candidate.entry_blocked) if candidate is not None else False
    entry_ready = bool(candidate is not None and candidate.discovered and confirmed and not blocked)
    signal = _signal_for_candidate(
        signal_index,
        symbol=symbol,
        side=candidate_side,
        timestamp=timestamp,
    ) if candidate_side and isolated_candidate_selected else None

    min_range_width = float(EVIDENCE_CONFIG.failed_breakout.min_accepted_range_width_atr)
    range_width_meets_config = (
        range_width_atr is not None and range_width_atr >= min_range_width
    )

    event_key = _text(setup_inputs.get("watch_event_key") or watch.get("event_key"))
    persisted = setup_state_event_index.get((symbol, event_key), {}) if event_key else {}

    row: Dict[str, Any] = {
        "symbol": symbol,
        "snapshot_time": timestamp.isoformat(sep=" "),
        "structure_status": _upper(breakout.get("status")),
        "structure_reason": _text(breakout.get("reason")),
        "event_status": _upper(setup_inputs.get("breakout_status") or watch.get("event_status") or breakout.get("status")),
        "event_source": _text(setup_inputs.get("watch_source") or watch.get("source")),
        "watch_evaluation_source": _text(
            setup_inputs.get("watch_evaluation_source")
            or watch.get("evaluation_source")
            or watch.get("source")
        ),
        "event_key": event_key,
        "runtime_suppression_state": runtime_suppression_state,
        "runtime_suppression_reason": runtime_suppression_reason,
        "watch_age_bars": _int(setup_inputs.get("watch_age_bars") if setup_inputs else watch.get("age_bars")),
        "watch_age_minutes": _num(setup_inputs.get("watch_age_minutes") if setup_inputs else watch.get("age_minutes")),
        "watch_valid_bars": _int(setup_inputs.get("watch_valid_bars") if setup_inputs else watch.get("valid_bars")),
        "watch_valid_minutes": _num(setup_inputs.get("watch_valid_minutes") if setup_inputs else watch.get("valid_minutes")),
        "delayed_watch_evaluation": bool((_int(setup_inputs.get("watch_age_bars") if setup_inputs else watch.get("age_bars")) or 0) > 0),
        "original_breakout_side": original_side,
        "candidate_side": candidate_side,
        "attempt_time": _text(setup_inputs.get("attempt_time") or watch.get("attempt_time") or breakout.get("attempt_time")),
        "accepted_time": _text(setup_inputs.get("accepted_time") or watch.get("accepted_time") or breakout.get("accepted_time")),
        "failed_time": _text(setup_inputs.get("failed_time") or watch.get("failed_time") or breakout.get("failed_time")),
        "bars_outside": _int(setup_inputs.get("bars_outside") if setup_inputs else breakout.get("bars_outside")),
        "bars_reclaimed": _int(setup_inputs.get("bars_reclaimed") if setup_inputs else breakout.get("bars_reclaimed")),
        "close": close,
        "atr": atr,
        "rsi": _num(_nested(data, "indicators", "rsi", "value")),
        "bollinger_position": _num(_nested(data, "indicators", "bollinger", "position")),
        "position_15m": _num(_nested(data, "market_windows", "15m", "close_position_in_range")),
        "vwap": vwap,
        "vwap_gap_pct": vwap_gap_pct,
        "accepted_range_source": _text(accepted.get("source")),
        "accepted_range_id": _text(accepted.get("range_id")),
        "accepted_range_version": _int(accepted.get("version")),
        "accepted_range_low": accepted_low,
        "accepted_range_high": accepted_high,
        "accepted_range_mid": accepted_mid,
        "accepted_range_width_points": range_width_points,
        "accepted_range_width_atr": range_width_atr,
        "accepted_range_width_source": range_width_source,
        "accepted_range_measurement_atr": _num(watch.get("accepted_range_measurement_atr")),
        "accepted_range_width_pct": range_width_pct,
        "min_accepted_range_width_atr_config": min_range_width,
        "accepted_range_width_meets_config": range_width_meets_config,
        "accepted_range_width_below_config": bool(range_width_atr is not None and not range_width_meets_config),
        "candidate_present": candidate is not None,
        "candidate_missing_reason": "" if candidate is not None else _candidate_missing_reason(data),
        "candidate_discovered": bool(candidate.discovered) if candidate is not None else False,
        "price_action_raw_confirmed": bool(price_action.get("raw_confirmed")),
        "price_action_confirmed": confirmed,
        "price_action_strength": _num(price_action.get("strength")),
        "price_action_strength_min": _num(price_action.get("strength_confirm_min")),
        "single_candle_confirmed": bool(price_action.get("single_candle_confirmed")),
        "multi_candle_confirmed": bool(price_action.get("multi_candle_confirmed")),
        "current_move_atr": _num(price_action.get("current_move_atr")),
        "move_15m_atr": _num(price_action.get("move_15m_atr")),
        "current_close_position": _num(price_action.get("current_close_position")),
        "price_action_position_15m": _num(price_action.get("position_15m")),
        "entry_blocked": blocked,
        "blocked_by": _text(candidate.blocked_by) if candidate is not None else "",
        "risk_flags": ",".join(str(value) for value in (candidate.risk_flags if candidate is not None else [])),
        "entry_ready": entry_ready,
        # Multiple reference structures may be entry-ready on the same side.
        # Dynamic range ranks first; ORB/fixed references remain fallback.
        "isolated_candidate_selected": bool(isolated_candidate_selected),
        "candidate_reason_code": _text(candidate.reason_code) if candidate is not None else "",
        "candidate_reason_text": _text(candidate.reason_text) if candidate is not None else "",
        "candidate_evidence_state": _text(candidate.evidence_state) if candidate is not None else "",
        "inside_accepted_range": location.get("inside_accepted_range", setup_inputs.get("inside_accepted_range")),
        "require_inside_accepted_range": location.get("require_inside_accepted_range"),
        "min_bars_reclaimed": _int(location.get("min_bars_reclaimed") or setup_inputs.get("min_bars_reclaimed")),
        "reference_id": _text(setup_inputs.get("reference_id") or watch.get("reference_id") or level.get("reference_id")),
        "level_type": _text(level_type),
        "level_source": _text(level_source),
        "level_rank": _int(setup_inputs.get("level_rank") or watch.get("level_rank") or level.get("rank")),
        "level_price": level_price,
        "value_structure_source": _text(setup_inputs.get("value_structure_source") or watch.get("value_structure_source") or accepted.get("source")),
        "value_structure_type": _text(setup_inputs.get("value_structure_type") or watch.get("value_structure_type") or accepted.get("range_type")),
        "entry_distance_from_level_points": _num(location.get("entry_distance_from_level_points")),
        "entry_distance_from_level_atr": _num(location.get("entry_distance_from_level_atr")),
        "entry_distance_from_level_pct": _pct(_num(location.get("entry_distance_from_level_points")), close),
        "max_entry_distance_from_level_atr": _num(location.get("max_entry_distance_from_level_atr")),
        "setup_reference_price": _num(setup_levels.get("reference_price")),
        "setup_reference_source": _text(setup_levels.get("reference_source")),
        "setup_invalidation_side": _text(setup_levels.get("invalidation_side")),
        "midpoint_room_points": midpoint_room["points"],
        "midpoint_room_atr": midpoint_room["atr"],
        "midpoint_room_pct": midpoint_room["pct"],
        "midpoint_room_available": midpoint_room["available"],
        "vwap_room_points": vwap_room["points"],
        "vwap_room_atr": vwap_room["atr"],
        "vwap_room_pct": vwap_room["pct"],
        "vwap_room_available": vwap_room["available"],
        "opposite_edge_price": opposite_edge,
        "opposite_edge_room_points": opposite_edge_room["points"],
        "opposite_edge_room_atr": opposite_edge_room["atr"],
        "opposite_edge_room_pct": opposite_edge_room["pct"],
        "opposite_edge_room_available": opposite_edge_room["available"],
        "nearest_quality_context": nearest_context[0] if nearest_context else "",
        "nearest_quality_context_price": nearest_context[1] if nearest_context else None,
        "nearest_quality_context_room_points": nearest_context[2]["points"] if nearest_context else None,
        "nearest_quality_context_room_atr": nearest_context[2]["atr"] if nearest_context else None,
        "nearest_quality_context_room_pct": nearest_context[2]["pct"] if nearest_context else None,
        "entry_ready_not_created": bool(entry_ready and isolated_candidate_selected and signal is None),
        "signal_layer_non_creation_reason": (
            "LOWER_PRIORITY_STRUCTURAL_REFERENCE"
            if entry_ready and not isolated_candidate_selected
            else "FRESH_SIGNAL_WINDOW_CLOSED"
            if entry_ready
            and isolated_candidate_selected
            and signal is None
            and timestamp.time() > datetime.strptime(EVIDENCE_CONFIG.window.latest_fresh_signal_time, "%H:%M:%S").time()
            else runtime_suppression_state
            if runtime_suppression_state
            else ""
        ),
        "persisted_event_found": bool(persisted.get("persisted_event_found")),
        "persisted_final_state": _text(persisted.get("persisted_final_state")),
        "persisted_final_reason": _text(persisted.get("persisted_final_reason")),
        "persisted_signal_id": _text(persisted.get("persisted_signal_id")),
        "persisted_first_seen_time": _text(persisted.get("persisted_first_seen_time")),
        "persisted_last_seen_time": _text(persisted.get("persisted_last_seen_time")),
        "persisted_expires_at": _text(persisted.get("persisted_expires_at")),
        "persisted_transition_count": _int(persisted.get("persisted_transition_count")) or 0,
        "persisted_transitions": _text(persisted.get("persisted_transitions")),
    }
    row.update(_signal_columns(signal))
    row.update(
        _horizon_columns(
            symbol=symbol,
            timestamp=timestamp,
            side=candidate_side,
            entry_price=close,
            series_by_symbol=series_by_symbol,
            position=position,
        )
    )
    return row


# =============================================================================
# Output and summary
# =============================================================================
def _fieldnames(rows: Sequence[Dict[str, Any]]) -> List[str]:
    core = [
        "symbol", "snapshot_time", "structure_status", "structure_reason",
        "event_status", "event_source", "event_key", "runtime_suppression_state", "runtime_suppression_reason", "watch_age_bars", "watch_age_minutes",
        "watch_valid_bars", "watch_valid_minutes", "delayed_watch_evaluation",
        "original_breakout_side", "candidate_side", "attempt_time", "accepted_time", "failed_time",
        "bars_outside", "bars_reclaimed", "close", "atr",
        "candidate_present", "candidate_missing_reason", "candidate_discovered",
        "price_action_raw_confirmed", "price_action_confirmed", "price_action_strength",
        "single_candle_confirmed", "multi_candle_confirmed",
        "entry_blocked", "blocked_by", "risk_flags", "entry_ready", "isolated_candidate_selected", "entry_ready_not_created", "signal_layer_non_creation_reason",
        "candidate_reason_code", "candidate_evidence_state",
        "inside_accepted_range", "min_bars_reclaimed",
        "level_type", "level_source", "level_price",
        "entry_distance_from_level_atr", "entry_distance_from_level_pct",
        "accepted_range_id", "accepted_range_version",
        "accepted_range_low", "accepted_range_high", "accepted_range_mid",
        "accepted_range_width_atr", "accepted_range_width_pct",
        "min_accepted_range_width_atr_config", "accepted_range_width_meets_config",
        "midpoint_room_pct", "vwap_room_pct", "opposite_edge_room_pct",
        "nearest_quality_context", "nearest_quality_context_room_pct",
        "signal_created", "signal_id", "signal_status", "signal_stage", "signal_status_reason",
        "signal_mfe_pct", "signal_mae_pct",
        "persisted_event_found", "persisted_final_state", "persisted_final_reason",
        "persisted_signal_id", "persisted_transition_count",
    ]
    extras = sorted({key for row in rows for key in row} - set(core))
    return core + extras


def _write_csv(path: str, rows: Sequence[Dict[str, Any]]) -> None:
    if not path:
        return
    output = Path(path)
    if output.parent and str(output.parent) not in {"", "."}:
        output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_fieldnames(rows), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _count(rows: Sequence[Dict[str, Any]], key: str) -> Dict[str, int]:
    counter = Counter(str(row.get(key) if row.get(key) not in {None, ""} else "UNKNOWN") for row in rows)
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def _blocker_counts(rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        flags = [flag.strip() for flag in str(row.get("risk_flags") or "").split(",") if flag.strip()]
        if flags:
            counter.update(flags)
        elif row.get("blocked_by"):
            counter.update([str(row["blocked_by"])])
    return dict(counter.most_common())


def _event_summary_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse repeated watch evaluations into one record per symbol/event."""
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        event_key = str(row.get("event_key") or "").strip()
        if symbol and event_key:
            grouped[(symbol, event_key)].append(dict(row))

    events: List[Dict[str, Any]] = []
    for (symbol, event_key), evaluations in grouped.items():
        evaluations.sort(
            key=lambda row: (
                _int(row.get("watch_age_bars")) if _int(row.get("watch_age_bars")) is not None else 999,
                str(row.get("snapshot_time") or ""),
            )
        )
        origin = dict(evaluations[0])
        blockers: List[str] = []
        for row in evaluations:
            for flag in str(row.get("risk_flags") or "").split(","):
                flag = flag.strip()
                if flag and flag not in blockers:
                    blockers.append(flag)
            blocked_by = str(row.get("blocked_by") or "").strip()
            if blocked_by and blocked_by not in blockers:
                blockers.append(blocked_by)

        confirmed_rows = [row for row in evaluations if bool(row.get("price_action_confirmed"))]
        blocked_confirmed = [row for row in confirmed_rows if bool(row.get("entry_blocked"))]
        first_primary_blocker = ""
        if blocked_confirmed:
            first_primary_blocker = str(blocked_confirmed[0].get("blocked_by") or "")
        elif blockers:
            first_primary_blocker = blockers[0]

        signal_rows = [row for row in evaluations if bool(row.get("signal_created"))]
        selected_rows = [row for row in evaluations if bool(row.get("isolated_candidate_selected"))]
        persisted_rows = [row for row in evaluations if bool(row.get("persisted_event_found"))]
        event_time = (
            _parse_dt(origin.get("failed_time"))
            or _parse_dt(origin.get("first_evaluation_time"))
            or _parse_dt(origin.get("snapshot_time"))
        )
        signal_times = [
            parsed
            for parsed in (_parse_dt(row.get("snapshot_time")) for row in signal_rows)
            if parsed is not None
        ]
        first_signal_time = min(signal_times) if signal_times else None
        signal_delay_minutes = (
            max(0.0, (first_signal_time - event_time).total_seconds() / 60.0)
            if first_signal_time is not None and event_time is not None
            else None
        )
        latest = evaluations[-1]
        signal_row = signal_rows[0] if signal_rows else None
        selected_creation_row = next(
            (
                row
                for row in selected_rows
                if bool(row.get("entry_ready"))
            ),
            None,
        )
        initial_blocker = str(evaluations[0].get("blocked_by") or "")
        creation_blocker = (
            str(selected_creation_row.get("blocked_by") or "")
            if selected_creation_row is not None
            else ""
        )

        origin.update({
            "symbol": symbol,
            "event_key": event_key,
            "evaluation_rows": len(evaluations),
            "first_evaluation_time": evaluations[0].get("snapshot_time"),
            "last_evaluation_time": latest.get("snapshot_time"),
            "max_watch_age_bars": max((_int(row.get("watch_age_bars")) or 0) for row in evaluations),
            "ever_price_action_confirmed": bool(confirmed_rows),
            "ever_entry_ready": any(bool(row.get("entry_ready")) for row in evaluations),
            "ever_entry_blocked": any(bool(row.get("entry_blocked")) for row in evaluations),
            "event_primary_blocker": first_primary_blocker,
            "initial_primary_blocker": initial_blocker,
            "last_blocker_before_selection": next(
                (
                    str(row.get("blocked_by") or "")
                    for row in reversed(evaluations)
                    if str(row.get("blocked_by") or "")
                    and not bool(row.get("isolated_candidate_selected"))
                ),
                "",
            ),
            "blocker_at_selection": creation_blocker,
            "event_all_blockers": ",".join(blockers),
            "signal_created": bool(signal_rows),
            "signal_id": signal_row.get("signal_id") if signal_row else origin.get("signal_id"),
            "signal_created_time": first_signal_time.isoformat(sep=" ") if first_signal_time else "",
            "signal_delay_minutes": round(signal_delay_minutes, 2) if signal_delay_minutes is not None else None,
            "signal_delay_bars": round(signal_delay_minutes / 3.0, 2) if signal_delay_minutes is not None else None,
            "signal_created_after_event": bool(signal_delay_minutes is not None and signal_delay_minutes > 0),
            "signal_created_price": signal_row.get("signal_created_price") if signal_row else None,
            "signal_status": signal_row.get("signal_status") if signal_row else "",
            "signal_stage": signal_row.get("signal_stage") if signal_row else "",
            "signal_status_reason": signal_row.get("signal_status_reason") if signal_row else "",
            "signal_mfe_pct": signal_row.get("signal_mfe_pct") if signal_row else None,
            "signal_mae_pct": signal_row.get("signal_mae_pct") if signal_row else None,
            "persisted_event_found": bool(persisted_rows),
            "persisted_final_state": (
                persisted_rows[-1].get("persisted_final_state") if persisted_rows else origin.get("persisted_final_state")
            ),
            "persisted_final_reason": (
                persisted_rows[-1].get("persisted_final_reason") if persisted_rows else origin.get("persisted_final_reason")
            ),
            "persisted_transition_count": max(
                (_int(row.get("persisted_transition_count")) or 0) for row in evaluations
            ),
        })
        events.append(origin)
    events.sort(
        key=lambda row: (
            str(row.get("snapshot_time") or ""),
            str(row.get("symbol") or ""),
            int(_int(row.get("level_rank")) or 999),
            str(row.get("reference_id") or ""),
        )
    )
    return events


def _print_summary(
    rows: Sequence[Dict[str, Any]],
    event_rows: Sequence[Dict[str, Any]],
    day_start: datetime,
) -> None:
    ready = [row for row in rows if row.get("entry_ready")]
    blocked = [row for row in rows if row.get("entry_blocked")]
    unconfirmed = [row for row in rows if row.get("candidate_present") and not row.get("price_action_confirmed")]
    created = [row for row in rows if row.get("signal_created")]
    ready_events = [row for row in event_rows if row.get("ever_entry_ready")]
    blocked_events = [row for row in event_rows if row.get("ever_entry_blocked")]
    created_events = [row for row in event_rows if row.get("signal_created")]

    print("\nFAILED_BREAKOUT review summary")
    print("-" * 116)
    print(f"date                       : {day_start.date()}")
    delayed_rows = [row for row in rows if row.get("delayed_watch_evaluation")]

    print(f"event_statuses             : {STRUCTURE_STATUSES}")
    print(f"symbols_filter             : {_symbol_filter(day_start) or '(all)'}")
    print(f"time_window                : {START_TIME or '(start)'} -> {END_TIME or '(end)'}")
    print(f"unique_failed_events       : {len(event_rows)}")
    print(f"watch_evaluation_rows      : {len(rows)}")
    print(f"delayed_watch_rows         : {len(delayed_rows)}")
    print(f"candidate_present          : {sum(bool(row.get('candidate_present')) for row in rows)}")
    print(f"price_action_confirmed     : {sum(bool(row.get('price_action_confirmed')) for row in rows)}")
    print(f"unconfirmed_candidates     : {len(unconfirmed)}")
    print(f"blocked_candidates         : {len(blocked)}")
    print(f"entry_ready                : {len(ready)}")
    print(f"signals_created_events     : {len(created_events)}")
    print(f"signals_created_after_event: {sum(bool(row.get('signal_created_after_event')) for row in event_rows)}")
    print(f"entry_ready_not_created    : {sum(bool(row.get('entry_ready_not_created')) for row in rows)}")
    print(f"side_counts_events         : {_count(event_rows, 'candidate_side')}")
    print(f"primary_blockers_events    : {_count(blocked_events, 'event_primary_blocker')}")
    print(f"range_width_events         : {_count(event_rows, 'accepted_range_width_meets_config')}")
    print(f"persisted_final_states     : {_count(event_rows, 'persisted_final_state')}")
    for bars in HORIZON_BARS:
        print(f"median_h{bars}_mfe_pct_all      : {_median(row.get(f'h{bars}_mfe_pct') for row in event_rows)}")
        print(f"median_h{bars}_mae_pct_all      : {_median(row.get(f'h{bars}_mae_pct') for row in event_rows)}")
        print(f"median_h{bars}_mfe_pct_ready    : {_median(row.get(f'h{bars}_mfe_pct') for row in ready_events)}")
        print(f"median_h{bars}_mfe_pct_blocked  : {_median(row.get(f'h{bars}_mfe_pct') for row in blocked_events)}")
    print(f"evaluation_output          : {OUTPUT_CSV_PATH or '(disabled)'}")
    print(f"event_output               : {OUTPUT_EVENT_CSV_PATH or '(disabled)'}")

    print(f"\nFirst {min(PRINT_TOP_N, len(rows))} rows")
    print("-" * 116)
    for row in rows[: max(0, int(PRINT_TOP_N))]:
        print(
            f"{str(row.get('snapshot_time') or '')[:19]:19s} "
            f"{str(row.get('symbol') or ''):14s} "
            f"{str(row.get('original_breakout_side') or ''):4s}->{str(row.get('candidate_side') or ''):4s} "
            f"age={str(row.get('watch_age_bars') if row.get('watch_age_bars') is not None else ''):>2s} "
            f"PA={str(bool(row.get('price_action_confirmed'))):5s} "
            f"blocked={str(bool(row.get('entry_blocked'))):5s} "
            f"ready={str(bool(row.get('entry_ready'))):5s} "
            f"created={str(bool(row.get('signal_created'))):5s} "
            f"h9_mfe={str(row.get('h9_mfe_pct') if row.get('h9_mfe_pct') is not None else ''):>9s} "
            f"reason={str(row.get('blocked_by') or row.get('candidate_missing_reason') or row.get('candidate_reason_code') or '')}"
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
    series_by_symbol, position, loaded = _prepare_snapshot_series(snapshot_rows)
    signal_index = _fetch_signal_index(day_start)
    setup_state_event_index = _fetch_setup_state_event_index(day_start)
    allowed_statuses = {_upper(status) for status in STRUCTURE_STATUSES}

    start_line = (
        f"Starting FAILED_BREAKOUT review | source=db | date={day_start.date()} | "
        f"snapshots={len(loaded)} | symbols={_symbol_filter(day_start) or 'all'} | "
        f"window={START_TIME or 'start'}->{END_TIME or 'end'} | output={OUTPUT_CSV_PATH or 'disabled'}"
    )
    print(start_line, flush=True)
    logger.info(start_line)

    rows: List[Dict[str, Any]] = []
    active_watches: Dict[Tuple[str, str], Dict[str, Any]] = {}
    processed = 0
    for rec, data in loaded:
        processed += 1
        try:
            evaluation_data = _snapshot_with_recent_context(
                data,
                series_by_symbol=series_by_symbol,
                position=position,
            )
            review_results = _review_watches_and_candidates(
                evaluation_data,
                active_watches=active_watches,
                setup_state_event_index=setup_state_event_index,
            )
            if not review_results:
                continue

            selected_event_keys: set[str] = set()
            for side in ("BUY", "SELL"):
                ready = [
                    (candidate, watch)
                    for candidate, watch, suppression_state, _ in review_results
                    if candidate is not None
                    and not suppression_state
                    and _upper(candidate.side) == side
                    and bool(candidate.price_action_confirmed)
                    and not bool(candidate.entry_blocked)
                ]
                if not ready:
                    continue
                ready.sort(
                    key=lambda pair: (
                        int(_int(((pair[0].data or {}).get("setup_inputs") or {}).get("level_rank")) or 999),
                        _num(((pair[0].data or {}).get("entry_location_filter") or {}).get("entry_distance_from_level_atr"))
                        if _num(((pair[0].data or {}).get("entry_location_filter") or {}).get("entry_distance_from_level_atr")) is not None
                        else 999.0,
                        -float(pair[0].price_action_strength or 0.0),
                    )
                )
                selected_event_keys.add(_text(ready[0][1].get("event_key")))

            for candidate, review_watch, suppression_state, suppression_reason in review_results:
                event_key = _text(review_watch.get("event_key"))
                rows.append(
                    _candidate_row(
                        rec=rec,
                        data=data,
                        candidate=candidate,
                        review_watch=review_watch,
                        runtime_suppression_state=suppression_state,
                        runtime_suppression_reason=suppression_reason,
                        isolated_candidate_selected=bool(event_key and event_key in selected_event_keys),
                        signal_index=signal_index,
                        setup_state_event_index=setup_state_event_index,
                        series_by_symbol=series_by_symbol,
                        position=position,
                    )
                )
                if MAX_RECORDS and int(MAX_RECORDS) > 0 and len(rows) >= int(MAX_RECORDS):
                    break
            if MAX_RECORDS and int(MAX_RECORDS) > 0 and len(rows) >= int(MAX_RECORDS):
                break
        except Exception:
            logger.exception("FAILED_BREAKOUT review failed | symbol=%s time=%s", rec.symbol, rec.snapshot_time)
            if FAIL_ON_ROW_ERROR:
                raise

        if PROGRESS_EVERY and (processed % int(PROGRESS_EVERY) == 0 or processed == len(loaded)):
            line = f"Processed {processed}/{len(loaded)} snapshots | output_rows={len(rows)}"
            print(line, flush=True)
            logger.info(line)

    rows.sort(
        key=lambda row: (
            str(row.get("snapshot_time") or ""),
            str(row.get("symbol") or ""),
            int(_int(row.get("level_rank")) or 999),
            str(row.get("reference_id") or ""),
        )
    )
    event_rows = _event_summary_rows(rows)
    _write_csv(OUTPUT_CSV_PATH, rows)
    _write_csv(OUTPUT_EVENT_CSV_PATH, event_rows)
    _print_summary(rows, event_rows, day_start)


if __name__ == "__main__":
    main()
