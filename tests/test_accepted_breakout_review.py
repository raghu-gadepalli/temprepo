#!/usr/bin/env python3
"""Focused ACCEPTED_BREAKOUT signal-quality review.

Edit TEST SETTINGS and run:

    python tests/test_accepted_breakout_review.py

This report is read-only. It reads persisted ACCEPTED_BREAKOUT signals, their
entry evidence, subsequent snapshots and user trades directly from the DB. It
writes one compact row per created ACCEPTED_BREAKOUT signal so the setup can be
tuned before changing StockAdvisor or TradeManager parameters. It also measures
acceptance against the selected level itself, because structure.bars_outside may
refer to the accepted range while the selected signal reference is ORB/PDH/PDL.

The report intentionally does not recompute StockAdvisor and does not mutate
signals, snapshots, setup state or trades.
"""

from __future__ import annotations

import csv
import json
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
from models.trade_models import Snapshot as SnapshotORM
from schemas.signal import SignalSchema
from schemas.user_trade import UserTradeSchema
from utils.json_utils import sanitize_json


# =============================================================================
# TEST SETTINGS
# =============================================================================
# None = latest signal date in DB. Or set explicitly, e.g. "2026-07-10".
TEST_DATE: Optional[str] = None

OUTPUT_CSV_PATH: str = "accepted_breakout_review.csv"

# Empty list = all accepted-breakout symbols.
SYMBOL_FILTER: List[str] = []

# Optional signal-time window in HH:MM or HH:MM:SS. None = full selected date.
START_TIME: Optional[str] = None
END_TIME: Optional[str] = None

# Fixed post-signal horizons. With 3-minute snapshots, 1/2/3/5/10 bars are
# approximately 3/6/9/15/30 minutes.
HORIZON_BARS: Tuple[int, ...] = (1, 2, 3, 5, 10)

# Fail instead of silently producing a partial row when persisted entry evidence
# cannot identify the selected ACCEPTED_BREAKOUT candidate.
STRICT_ENTRY_CANDIDATE: bool = True

# Optional quick-test cap after DB filters. None = all matching signals.
MAX_RECORDS: Optional[int] = None

PRINT_TOP_N: int = 100
PROGRESS_EVERY: int = 25
LOG_FILE: str = "test_accepted_breakout_review.log"

SETUP_LABEL = "ACCEPTED_BREAKOUT"


# =============================================================================
# Generic helpers
# =============================================================================
def _value(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


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
        s = str(value).strip()
        if not s or s.lower() in ("nan", "none", "null"):
            return None
        return float(s)
    except Exception:
        return None


def _json_loads(value: Any) -> Dict[str, Any]:
    """Normalize a DB/schema JSON value into a plain dictionary.

    UserTradeSchema validates trade_management as TradeManagementSchema, so a
    fetched value is commonly a Pydantic model rather than raw JSON text.
    Convert schema models with model_dump() and use the project's canonical
    JSON sanitizer. Only raw string/bytes values need json.loads().
    """
    if value is None:
        return {}

    if isinstance(value, dict):
        parsed: Any = value
    else:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            parsed = model_dump(mode="json", exclude_none=True)
        else:
            if isinstance(value, (bytes, bytearray, memoryview)):
                value = bytes(value).decode("utf-8")
            s = str(value or "").strip()
            if not s or s.lower() in ("nan", "none", "null"):
                return {}
            parsed = json.loads(s)

    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object, got {type(parsed).__name__}")
    return sanitize_json(parsed)


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    s = str(value or "").strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return None
    return datetime.fromisoformat(s.replace("T", " ").split("+")[0]).replace(tzinfo=None)


def _signal_time(sig: Any) -> Optional[datetime]:
    # qualified_time is normally the CREATE time. last_snapshot_time can later
    # move to the invalidation/exit evaluation, so it is deliberately lower in
    # the fallback order.
    return (
        _parse_dt(_value(sig, "qualified_time"))
        or _parse_dt(_value(sig, "actionable_time"))
        or _parse_dt(_value(sig, "first_seen_time"))
        or _parse_dt(_value(sig, "last_snapshot_time"))
        or _parse_dt(_value(sig, "last_eval_time"))
    )


def _parse_time_value(value: Optional[str]) -> Optional[Tuple[int, int, int]]:
    if not value:
        return None
    parts = [int(x) for x in str(value).strip().split(":")]
    if len(parts) == 2:
        return parts[0], parts[1], 0
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    raise ValueError(f"Invalid time value: {value!r}. Use HH:MM or HH:MM:SS.")


def _combine_time(day_start: datetime, value: Optional[str]) -> Optional[datetime]:
    parsed = _parse_time_value(value)
    if not parsed:
        return None
    hh, mm, ss = parsed
    return day_start.replace(hour=hh, minute=mm, second=ss, microsecond=0)


def _parse_date_start() -> Optional[datetime]:
    if not TEST_DATE:
        return None
    return datetime.fromisoformat(str(TEST_DATE).strip()).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )


def _latest_signal_date_start() -> Optional[datetime]:
    latest = _parse_dt(SignalSchema.fetch_latest_signal_snapshot_time())
    return latest.replace(hour=0, minute=0, second=0, microsecond=0) if latest else None


def _nested(mapping: Any, *keys: str, default: Any = None) -> Any:
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def _same_level(a: Any, b: Any, tolerance: float = 1e-6) -> bool:
    av = _num(a)
    bv = _num(b)
    return av is not None and bv is not None and abs(av - bv) <= tolerance


# =============================================================================
# Entry-candidate extraction
# =============================================================================
def _selected_primary_candidate(meta: Dict[str, Any]) -> Dict[str, Any]:
    entry = meta.get("entry_criteria_json") or {}
    current = entry.get("current_evidence") or {}
    candidate = current.get("primary_candidate") or meta.get("initiated_setup") or {}
    return candidate if isinstance(candidate, dict) else {}


def _entry_discovered_candidates(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    entry = meta.get("entry_criteria_json") or {}
    candidates = entry.get("discovered_setups") or []
    if not candidates:
        candidates = _nested(meta, "evidence", "discovered_setups", default=[]) or []
    return [c for c in candidates if isinstance(c, dict)]


def _match_selected_candidate(meta: Dict[str, Any], side: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    selected = _selected_primary_candidate(meta)
    candidates = _entry_discovered_candidates(meta)
    matches: List[Dict[str, Any]] = []

    selected_type = str(selected.get("level_type") or "").strip().upper()
    selected_source = str(selected.get("level_source") or "").strip().upper()
    selected_price = selected.get("level_price")

    for candidate in candidates:
        if str(candidate.get("setup_label") or "").strip().upper() != SETUP_LABEL:
            continue
        if str(candidate.get("side") or "").strip().upper() != side:
            continue

        data = candidate.get("data") or {}
        setup_inputs = data.get("setup_inputs") or {}
        level_type = str(setup_inputs.get("level_type") or "").strip().upper()
        level_source = str(setup_inputs.get("level_source") or "").strip().upper()
        level_price = setup_inputs.get("level_price")

        same = False
        if selected_type and level_type == selected_type:
            same = True
        if selected_source and level_source == selected_source and _same_level(level_price, selected_price):
            same = True
        if _same_level(level_price, selected_price):
            same = True
        if same:
            matches.append(candidate)

    # Prefer the exact entry-ready candidate when duplicate level descriptions
    # exist in the evidence payload.
    for candidate in matches:
        if bool(candidate.get("price_action_confirmed")) and not bool(candidate.get("entry_blocked")):
            return candidate, candidates
    if matches:
        return matches[0], candidates

    raise RuntimeError(
        "Persisted ACCEPTED_BREAKOUT entry evidence does not contain the selected candidate | "
        f"side={side} selected_type={selected_type or 'NA'} "
        f"selected_source={selected_source or 'NA'} selected_price={selected_price}"
    )


# =============================================================================
# Selected-level acceptance and snapshot outcome calculations
# =============================================================================
def _snapshot_history_through_signal(
    symbol: str,
    signal_time: datetime,
    max_bars: int = 40,
) -> List[Tuple[datetime, Dict[str, Any]]]:
    day_start = signal_time.replace(hour=0, minute=0, second=0, microsecond=0)
    with get_trades_db() as db:
        rows = (
            db.query(SnapshotORM)
            .filter(SnapshotORM.symbol == symbol)
            .filter(SnapshotORM.snapshot_time >= day_start)
            .filter(SnapshotORM.snapshot_time <= signal_time)
            .order_by(SnapshotORM.snapshot_time.desc())
            .limit(max(1, int(max_bars)))
            .all()
        )

    history: List[Tuple[datetime, Dict[str, Any]]] = []
    for row in reversed(rows):
        payload = getattr(row, "data", None)
        snapshot_time = _parse_dt(getattr(row, "snapshot_time", None))
        if snapshot_time is None or not isinstance(payload, dict):
            continue
        history.append((snapshot_time, payload))
    return history


def _close_is_outside(*, side: str, close: Optional[float], level_price: Optional[float]) -> bool:
    if close is None or level_price is None:
        return False
    if side == "BUY":
        return close > level_price
    if side == "SELL":
        return close < level_price
    raise ValueError(f"Unsupported side: {side}")


def _body_is_outside(*, side: str, bar: Dict[str, Any], level_price: Optional[float]) -> bool:
    if level_price is None:
        return False
    open_price = _num(bar.get("open"))
    close = _num(bar.get("close"))
    if open_price is None or close is None:
        return False
    body_low = min(open_price, close)
    body_high = max(open_price, close)
    if side == "BUY":
        return body_low > level_price
    if side == "SELL":
        return body_high < level_price
    raise ValueError(f"Unsupported side: {side}")


def _consecutive_outside_metrics(
    *,
    side: str,
    level_price: Optional[float],
    history: Sequence[Tuple[datetime, Dict[str, Any]]],
) -> Dict[str, Any]:
    consecutive_closes = 0
    consecutive_bodies = 0
    first_close_time: Optional[datetime] = None
    first_body_time: Optional[datetime] = None

    for snapshot_time, payload in reversed(history):
        bar = _bar(payload)
        close = _num(bar.get("close"))
        if _close_is_outside(side=side, close=close, level_price=level_price):
            consecutive_closes += 1
            first_close_time = snapshot_time
        else:
            break

    for snapshot_time, payload in reversed(history):
        bar = _bar(payload)
        if _body_is_outside(side=side, bar=bar, level_price=level_price):
            consecutive_bodies += 1
            first_body_time = snapshot_time
        else:
            break

    signal_time = history[-1][0] if history else None
    return {
        "consecutive_closes_outside": consecutive_closes,
        "consecutive_bodies_outside": consecutive_bodies,
        "first_close_outside_time": _text(first_close_time),
        "first_body_outside_time": _text(first_body_time),
        "minutes_closes_outside": (
            (signal_time - first_close_time).total_seconds() / 60.0
            if signal_time is not None and first_close_time is not None
            else None
        ),
    }


def _structure_reference_price(payload: Dict[str, Any], side: str) -> Optional[float]:
    breakout = _nested(payload, "structure", "breakout", default={}) or {}
    accepted = _nested(payload, "structure", "accepted", "range", default={}) or {}
    if side == "BUY":
        return _num(breakout.get("reference_high")) or _num(accepted.get("high"))
    if side == "SELL":
        return _num(breakout.get("reference_low")) or _num(accepted.get("low"))
    raise ValueError(f"Unsupported side: {side}")


def _candidate_level_diagnostics(
    *,
    side: str,
    close: float,
    atr: Optional[float],
    selected_level_price: Optional[float],
    structure_reference_price: Optional[float],
    candidates: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    seen_prices: List[float] = []
    entries: List[Tuple[str, str, float, Optional[float]]] = []

    for candidate in candidates:
        if str(candidate.get("setup_label") or "").strip().upper() != SETUP_LABEL:
            continue
        if str(candidate.get("side") or "").strip().upper() != side:
            continue
        data = candidate.get("data") or {}
        inputs = data.get("setup_inputs") or {}
        price = _num(inputs.get("level_price"))
        if price is None or any(abs(price - existing) <= 1e-6 for existing in seen_prices):
            continue
        seen_prices.append(price)
        if side == "BUY":
            distance_points = close - price
        else:
            distance_points = price - close
        distance_atr = distance_points / atr if atr else None
        entries.append((
            str(inputs.get("level_type") or "UNKNOWN"),
            str(inputs.get("level_source") or "UNKNOWN"),
            price,
            distance_atr,
        ))

    valid_distances = [x[3] for x in entries if x[3] is not None]
    span_atr = (max(valid_distances) - min(valid_distances)) if valid_distances else None
    span_points = span_atr * atr if span_atr is not None and atr else None
    selected_vs_structure_points = None
    if selected_level_price is not None and structure_reference_price is not None:
        selected_vs_structure_points = abs(selected_level_price - structure_reference_price)

    return {
        "multiple_level_candidates": len(entries) >= 2,
        "candidate_level_span_points": span_points,
        "candidate_level_span_atr": span_atr,
        "candidate_level_span_pct": span_points / close * 100.0 if span_points is not None and close else None,
        "all_level_prices": ",".join(
            f"{level_type}@{price:.6f}" for level_type, _source, price, _distance in entries
        ),
        "structure_reference_price": structure_reference_price,
        "selected_is_structure_reference": _same_level(selected_level_price, structure_reference_price),
        "selected_vs_structure_reference_points": selected_vs_structure_points,
        "selected_vs_structure_reference_atr": (
            selected_vs_structure_points / atr
            if selected_vs_structure_points is not None and atr
            else None
        ),
        "selected_vs_structure_reference_pct": (
            selected_vs_structure_points / close * 100.0
            if selected_vs_structure_points is not None and close
            else None
        ),
    }


def _future_snapshot_payloads(symbol: str, signal_time: datetime, max_bars: int) -> List[Dict[str, Any]]:
    day_end = signal_time.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    with get_trades_db() as db:
        rows = (
            db.query(SnapshotORM)
            .filter(SnapshotORM.symbol == symbol)
            .filter(SnapshotORM.snapshot_time > signal_time)
            .filter(SnapshotORM.snapshot_time < day_end)
            .order_by(SnapshotORM.snapshot_time.asc())
            .limit(max(1, int(max_bars)))
            .all()
        )
    return [row.data for row in rows if isinstance(getattr(row, "data", None), dict)]


def _bar(payload: Dict[str, Any]) -> Dict[str, Any]:
    value = payload.get("bar") or {}
    return value if isinstance(value, dict) else {}


def _horizon_metrics(
    *,
    side: str,
    entry_price: float,
    level_price: Optional[float],
    payloads: Sequence[Dict[str, Any]],
    horizons: Iterable[int],
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    max_available = len(payloads)
    first_reabsorption_bar: Optional[int] = None

    for idx, payload in enumerate(payloads, start=1):
        close = _num(_bar(payload).get("close"))
        if close is None or level_price is None:
            continue
        if side == "BUY" and close < level_price:
            first_reabsorption_bar = idx
            break
        if side == "SELL" and close > level_price:
            first_reabsorption_bar = idx
            break

    out["future_bars_available"] = max_available
    out["first_reabsorption_bar"] = first_reabsorption_bar

    for horizon in horizons:
        n = max(1, int(horizon))
        window = list(payloads[:n])
        prefix = f"next_{n}bar"
        out[f"{prefix}_available"] = len(window)
        if not window:
            out[f"{prefix}_mfe_pct"] = None
            out[f"{prefix}_mae_pct"] = None
            out[f"{prefix}_close_move_pct"] = None
            out[f"{prefix}_reabsorbed"] = None
            continue

        highs = [_num(_bar(p).get("high")) for p in window]
        lows = [_num(_bar(p).get("low")) for p in window]
        closes = [_num(_bar(p).get("close")) for p in window]
        highs = [x for x in highs if x is not None]
        lows = [x for x in lows if x is not None]
        closes = [x for x in closes if x is not None]

        if side == "BUY":
            favorable_points = (max(highs) - entry_price) if highs else None
            adverse_points = (min(lows) - entry_price) if lows else None
            close_points = (closes[-1] - entry_price) if closes else None
        elif side == "SELL":
            favorable_points = (entry_price - min(lows)) if lows else None
            adverse_points = (entry_price - max(highs)) if highs else None
            close_points = (entry_price - closes[-1]) if closes else None
        else:
            raise ValueError(f"Unsupported side: {side}")

        out[f"{prefix}_mfe_pct"] = (
            favorable_points / entry_price * 100.0 if favorable_points is not None and entry_price else None
        )
        out[f"{prefix}_mae_pct"] = (
            adverse_points / entry_price * 100.0 if adverse_points is not None and entry_price else None
        )
        out[f"{prefix}_close_move_pct"] = (
            close_points / entry_price * 100.0 if close_points is not None and entry_price else None
        )
        out[f"{prefix}_reabsorbed"] = (
            first_reabsorption_bar is not None and first_reabsorption_bar <= n
        )

    return out


# =============================================================================
# Trade diagnostics (secondary; no TradeManager tuning in this report)
# =============================================================================
def _trade_summary(signal_id: str) -> Dict[str, Any]:
    rows = UserTradeSchema.fetch_for_signal_id(signal_id) or []
    total_pnl = 0.0
    future_pnl = 0.0
    future_rows = 0
    exit_reasons = set()
    max_expansion_count = 0

    for row in rows:
        pnl = _num(_value(row, "exit_pnl"))
        if pnl is None:
            pnl = _num(_value(row, "last_pnl_value"))
        pnl = pnl or 0.0
        total_pnl += pnl

        instrument_type = str(_text(_value(row, "instrument_type"))).strip().upper()
        if instrument_type == "FUT":
            future_rows += 1
            future_pnl += pnl

        reason = str(_value(row, "exit_reason") or "").strip()
        if reason:
            exit_reasons.add(reason)

        management = _json_loads(_value(row, "trade_management"))
        expansion_count = int(_num(management.get("expansion_count")) or 0)
        max_expansion_count = max(max_expansion_count, expansion_count)

    return {
        "trade_rows": len(rows),
        "trade_pnl_sum": round(total_pnl, 2),
        "future_trade_rows": future_rows,
        "future_trade_pnl": round(future_pnl, 2),
        "trade_exit_reasons": ",".join(sorted(exit_reasons)),
        "max_expansion_count": max_expansion_count,
    }


# =============================================================================
# Report formatting
# =============================================================================
def _fieldnames() -> List[str]:
    core = [
        "signal_id", "symbol", "side", "signal_time", "created_price",
        "status", "stage", "status_reason", "closed_time", "closed_price", "signal_age_minutes",
        "signal_mfe_pct", "signal_mae_pct",
        "acceptance_path", "breakout_status", "breakout_reason", "attempt_time", "accepted_time",
        "level_type", "level_source", "level_rank", "level_price", "level_tags",
        "signal_invalidation_reference_price", "signal_invalidation_reference_source",
        "signal_invalidation_reference_policy", "signal_invalidation_buffer_atr",
        "signal_invalidation_buffer_points", "signal_invalidation_distance_from_level_points",
        "level_candidate_count", "all_level_types", "all_level_prices", "multiple_level_candidates",
        "candidate_level_span_points", "candidate_level_span_atr", "candidate_level_span_pct",
        "structure_reference_price", "selected_is_structure_reference",
        "selected_vs_structure_reference_points", "selected_vs_structure_reference_atr",
        "selected_vs_structure_reference_pct",
        "bars_outside", "effective_bars_outside", "min_bars_outside",
        "selected_level_consecutive_closes_outside", "selected_level_consecutive_bodies_outside",
        "selected_level_first_close_outside_time", "selected_level_first_body_outside_time",
        "selected_level_minutes_closes_outside",
        "structure_reference_consecutive_closes_outside", "structure_reference_consecutive_bodies_outside",
        "break_distance_points", "break_distance_atr", "break_distance_pct",
        "entry_distance_points", "entry_distance_atr", "entry_distance_pct", "max_entry_distance_atr",
        "accepted_range_high", "accepted_range_low", "accepted_range_width_points",
        "accepted_range_width_atr", "accepted_range_width_pct",
        "next_external_level_available", "next_external_level_type", "next_external_level_price",
        "room_to_next_level_points", "room_to_next_level_atr", "room_to_next_level_pct",
        "price_action_strength", "single_candle_confirmed", "multi_candle_confirmed",
        "current_move_atr", "current_close_position", "move_15m_atr", "position_15m",
        "slope_3_atr", "slope_5_atr", "slope_3_atr_per_bar",
        "rsi", "bollinger_position", "bar_rvol", "bar_rvol_band",
        "hma_state", "hma_strength", "hma_aligned",
        "candidate_blocked", "candidate_blocked_by", "candidate_risk_flags",
        "future_bars_available", "first_reabsorption_bar",
    ]
    for horizon in HORIZON_BARS:
        prefix = f"next_{int(horizon)}bar"
        core.extend([
            f"{prefix}_available", f"{prefix}_mfe_pct", f"{prefix}_mae_pct",
            f"{prefix}_close_move_pct", f"{prefix}_reabsorbed",
        ])
    core.extend([
        "trade_rows", "trade_pnl_sum", "future_trade_rows", "future_trade_pnl",
        "trade_exit_reasons", "max_expansion_count", "entry_candidate_extraction_error",
    ])
    return core


def _write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    out = Path(path)
    if out.parent and str(out.parent) not in ("", "."):
        out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_fieldnames(), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _median(values: Iterable[Any]) -> Optional[float]:
    clean = [_num(v) for v in values]
    clean = [v for v in clean if v is not None]
    return statistics.median(clean) if clean else None


def _group_summary(rows: List[Dict[str, Any]], key: str) -> List[Tuple[str, int, Optional[float], Optional[float], int, int]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key) or "UNKNOWN")].append(row)

    output = []
    for name, group in groups.items():
        signal_mfe = [_num(row.get("signal_mfe_pct")) or 0.0 for row in group]
        next_3 = [_num(row.get("next_3bar_mfe_pct")) for row in group]
        next_3_clean = [v for v in next_3 if v is not None]
        low_followthrough = sum(1 for v in signal_mfe if v < 0.10)
        useful_move = sum(1 for v in signal_mfe if v >= 0.50)
        output.append((
            name,
            len(group),
            statistics.median(signal_mfe) if signal_mfe else None,
            statistics.median(next_3_clean) if next_3_clean else None,
            low_followthrough,
            useful_move,
        ))
    return sorted(output, key=lambda item: (-item[1], item[0]))


def _print_group_summary(rows: List[Dict[str, Any]], key: str) -> None:
    print(f"\nBy {key}")
    print("-" * 110)
    print(f"{'group':36s} {'count':>6s} {'med_signal_mfe':>15s} {'med_3bar_mfe':>13s} {'mfe<0.10':>10s} {'mfe>=0.50':>10s}")
    for name, count, med_signal, med_3bar, low_count, useful_count in _group_summary(rows, key):
        print(
            f"{name[:36]:36s} {count:6d} "
            f"{(med_signal if med_signal is not None else float('nan')):15.4f} "
            f"{(med_3bar if med_3bar is not None else float('nan')):13.4f} "
            f"{low_count:10d} {useful_count:10d}"
        )


# =============================================================================
# Main
# =============================================================================
def main() -> None:
    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)

    day_start = _parse_date_start() or _latest_signal_date_start()
    if day_start is None:
        raise RuntimeError("No signals found. Run replay first, or set TEST_DATE.")

    start_dt = _combine_time(day_start, START_TIME) or day_start
    end_dt = _combine_time(day_start, END_TIME) or (day_start + timedelta(days=1))
    if end_dt <= start_dt:
        raise ValueError(f"Invalid review window: START_TIME={START_TIME!r}, END_TIME={END_TIME!r}")

    signals = SignalSchema.fetch_for_advisor_review(
        start_time=start_dt,
        end_time=end_dt,
        symbols=SYMBOL_FILTER,
        setups=[SETUP_LABEL],
        limit=MAX_RECORDS,
    )

    rows: List[Dict[str, Any]] = []
    extraction_failures = 0
    max_horizon = max(int(x) for x in HORIZON_BARS)

    print(
        f"Starting ACCEPTED_BREAKOUT review | source=db | date={day_start.date()} | "
        f"signals={len(signals)} | symbols={SYMBOL_FILTER or 'all'} | "
        f"window={START_TIME or 'start'}->{END_TIME or 'end'} | output={OUTPUT_CSV_PATH}",
        flush=True,
    )

    for idx, sig in enumerate(signals, start=1):
        signal_id = str(_value(sig, "signal_id") or "").strip()
        symbol = str(_value(sig, "equity_ref") or _value(sig, "symbol") or "").strip().upper()
        side = str(_text(_value(sig, "side"))).strip().upper()
        signal_time = _signal_time(sig)
        created_price = _num(_value(sig, "created_price"))

        if not signal_id or not symbol or side not in {"BUY", "SELL"} or signal_time is None or not created_price:
            raise RuntimeError(
                "Invalid persisted ACCEPTED_BREAKOUT signal identity | "
                f"signal_id={signal_id!r} symbol={symbol!r} side={side!r} "
                f"signal_time={signal_time!r} created_price={created_price!r}"
            )

        meta = _json_loads(_value(sig, "meta_json"))
        candidate_error = ""
        try:
            candidate, all_candidates = _match_selected_candidate(meta, side)
        except Exception as exc:
            extraction_failures += 1
            if STRICT_ENTRY_CANDIDATE:
                raise
            candidate = {}
            all_candidates = _entry_discovered_candidates(meta)
            candidate_error = str(exc)
            logger.exception("Candidate extraction failed | signal_id=%s", signal_id)

        data = candidate.get("data") or {}
        setup_inputs = data.get("setup_inputs") or {}
        location = data.get("entry_location_filter") or {}
        setup_levels = data.get("setup_levels") or {}
        price_action = data.get("price_action") or {}
        next_level = setup_inputs.get("next_external_level") or {}
        hma = location.get("hma") or setup_inputs.get("hma_context") or {}

        atr = _num(location.get("atr"))
        level_price = _num(setup_inputs.get("level_price"))
        if level_price is None:
            level_price = _num(_selected_primary_candidate(meta).get("level_price"))
        close = _num(location.get("close")) or created_price

        break_points = _num(location.get("break_distance_points"))
        entry_points = _num(location.get("entry_distance_from_level_points"))
        range_points = _num(location.get("accepted_range_width_points"))
        room_points = _num(next_level.get("distance_points"))

        history = _snapshot_history_through_signal(symbol, signal_time)
        if not history:
            raise RuntimeError(
                f"No snapshot history found through signal time | symbol={symbol} signal_time={signal_time}"
            )
        entry_snapshot_time, entry_snapshot = history[-1]
        if entry_snapshot_time != signal_time:
            raise RuntimeError(
                "Signal time does not match latest available snapshot at/before CREATE | "
                f"symbol={symbol} signal_time={signal_time} snapshot_time={entry_snapshot_time}"
            )

        structure_reference_price = _structure_reference_price(entry_snapshot, side)
        selected_acceptance = _consecutive_outside_metrics(
            side=side,
            level_price=level_price,
            history=history,
        )
        structure_acceptance = _consecutive_outside_metrics(
            side=side,
            level_price=structure_reference_price,
            history=history,
        )
        level_diagnostics = _candidate_level_diagnostics(
            side=side,
            close=close,
            atr=atr,
            selected_level_price=level_price,
            structure_reference_price=structure_reference_price,
            candidates=all_candidates,
        )

        future_payloads = _future_snapshot_payloads(symbol, signal_time, max_horizon)
        horizon = _horizon_metrics(
            side=side,
            entry_price=created_price,
            level_price=level_price,
            payloads=future_payloads,
            horizons=HORIZON_BARS,
        )

        closed_time = _parse_dt(_value(sig, "closed_time"))
        age_minutes = (
            (closed_time - signal_time).total_seconds() / 60.0
            if closed_time is not None and closed_time >= signal_time
            else None
        )

        selected_level_types = []
        for item in all_candidates:
            if str(item.get("setup_label") or "").strip().upper() != SETUP_LABEL:
                continue
            item_inputs = _nested(item, "data", "setup_inputs", default={}) or {}
            label = str(item_inputs.get("level_type") or "").strip()
            if label and label not in selected_level_types:
                selected_level_types.append(label)

        row: Dict[str, Any] = {
            "signal_id": signal_id,
            "symbol": symbol,
            "side": side,
            "signal_time": signal_time.isoformat(sep=" "),
            "created_price": created_price,
            "status": str(_text(_value(sig, "status"))).strip().upper(),
            "stage": str(_text(_value(sig, "stage"))).strip().upper(),
            "status_reason": _value(sig, "status_reason"),
            "closed_time": _text(closed_time),
            "closed_price": _num(_value(sig, "closed_price")),
            "signal_age_minutes": age_minutes,
            "signal_mfe_pct": _num(_value(sig, "max_pnl")),
            "signal_mae_pct": _num(_value(sig, "min_pnl")),
            "acceptance_path": setup_inputs.get("acceptance_path") or _selected_primary_candidate(meta).get("acceptance_path"),
            "breakout_status": setup_inputs.get("breakout_status"),
            "breakout_reason": setup_inputs.get("breakout_reason"),
            "attempt_time": setup_inputs.get("attempt_time"),
            "accepted_time": setup_inputs.get("accepted_time"),
            "level_type": setup_inputs.get("level_type") or _selected_primary_candidate(meta).get("level_type"),
            "level_source": setup_inputs.get("level_source") or _selected_primary_candidate(meta).get("level_source"),
            "level_rank": setup_inputs.get("level_rank"),
            "level_price": level_price,
            "level_tags": ",".join(str(x) for x in (setup_inputs.get("level_tags") or [])),
            "signal_invalidation_reference_price": _num(setup_levels.get("signal_invalidation_reference_price")),
            "signal_invalidation_reference_source": setup_levels.get("signal_invalidation_reference_source"),
            "signal_invalidation_reference_policy": setup_levels.get("signal_invalidation_reference_policy"),
            "signal_invalidation_buffer_atr": _num(setup_levels.get("signal_invalidation_buffer_atr")),
            "signal_invalidation_buffer_points": _num(setup_levels.get("signal_invalidation_buffer_points")),
            "signal_invalidation_distance_from_level_points": (
                abs(_num(setup_levels.get("signal_invalidation_reference_price")) - level_price)
                if _num(setup_levels.get("signal_invalidation_reference_price")) is not None and level_price is not None
                else None
            ),
            "level_candidate_count": setup_inputs.get("level_candidate_count") or len(selected_level_types),
            "all_level_types": ",".join(selected_level_types),
            **level_diagnostics,
            "bars_outside": location.get("bars_outside"),
            "selected_level_consecutive_closes_outside": selected_acceptance.get("consecutive_closes_outside"),
            "selected_level_consecutive_bodies_outside": selected_acceptance.get("consecutive_bodies_outside"),
            "selected_level_first_close_outside_time": selected_acceptance.get("first_close_outside_time"),
            "selected_level_first_body_outside_time": selected_acceptance.get("first_body_outside_time"),
            "selected_level_minutes_closes_outside": selected_acceptance.get("minutes_closes_outside"),
            "structure_reference_consecutive_closes_outside": structure_acceptance.get("consecutive_closes_outside"),
            "structure_reference_consecutive_bodies_outside": structure_acceptance.get("consecutive_bodies_outside"),
            "effective_bars_outside": location.get("effective_bars_outside"),
            "min_bars_outside": location.get("min_bars_outside"),
            "break_distance_points": break_points,
            "break_distance_atr": _num(location.get("break_distance_atr")),
            "break_distance_pct": break_points / close * 100.0 if break_points is not None and close else None,
            "entry_distance_points": entry_points,
            "entry_distance_atr": _num(location.get("entry_distance_from_level_atr")),
            "entry_distance_pct": entry_points / close * 100.0 if entry_points is not None and close else None,
            "max_entry_distance_atr": _num(location.get("max_entry_distance_from_level_atr")),
            "accepted_range_high": _num(location.get("accepted_range_high")),
            "accepted_range_low": _num(location.get("accepted_range_low")),
            "accepted_range_width_points": range_points,
            "accepted_range_width_atr": _num(location.get("accepted_range_width_atr")),
            "accepted_range_width_pct": range_points / close * 100.0 if range_points is not None and close else None,
            "next_external_level_available": bool(next_level.get("available")),
            "next_external_level_type": next_level.get("level_type"),
            "next_external_level_price": _num(next_level.get("price")),
            "room_to_next_level_points": room_points,
            "room_to_next_level_atr": room_points / atr if room_points is not None and atr else None,
            "room_to_next_level_pct": room_points / close * 100.0 if room_points is not None and close else None,
            "price_action_strength": _num(candidate.get("price_action_strength")),
            "single_candle_confirmed": bool(price_action.get("single_candle_confirmed")),
            "multi_candle_confirmed": bool(price_action.get("multi_candle_confirmed")),
            "current_move_atr": _num(price_action.get("current_move_atr")),
            "current_close_position": _num(price_action.get("current_close_position")),
            "move_15m_atr": _num(price_action.get("move_15m_atr")),
            "position_15m": _num(price_action.get("position_15m")),
            "slope_3_atr": _num(price_action.get("slope_3_atr")),
            "slope_5_atr": _num(price_action.get("slope_5_atr")),
            "slope_3_atr_per_bar": _num(price_action.get("slope_3_atr_per_bar")),
            "rsi": _num(location.get("rsi")),
            "bollinger_position": _num(location.get("bollinger_position")),
            "bar_rvol": _num(location.get("bar_rvol")),
            "bar_rvol_band": location.get("bar_rvol_band"),
            "hma_state": hma.get("state"),
            "hma_strength": hma.get("strength"),
            "hma_aligned": hma.get("aligned"),
            "candidate_blocked": bool(candidate.get("entry_blocked")),
            "candidate_blocked_by": candidate.get("blocked_by"),
            "candidate_risk_flags": ",".join(str(x) for x in (candidate.get("risk_flags") or [])),
            "entry_candidate_extraction_error": candidate_error,
        }
        row.update(horizon)
        row.update(_trade_summary(signal_id))
        rows.append(row)

        if PROGRESS_EVERY and (idx % PROGRESS_EVERY == 0 or idx == len(signals)):
            print(
                f"Processed {idx}/{len(signals)} signals | rows={len(rows)} | "
                f"candidate_failures={extraction_failures}",
                flush=True,
            )

    _write_csv(OUTPUT_CSV_PATH, rows)

    print("\nACCEPTED_BREAKOUT focused review")
    print("-" * 110)
    print("source                   : db")
    print(f"date                     : {day_start.date()}")
    print(f"signals                  : {len(signals)}")
    print(f"rows                     : {len(rows)}")
    print(f"candidate_failures       : {extraction_failures}")
    print(f"output_csv               : {OUTPUT_CSV_PATH}")
    print(f"acceptance_paths         : {dict(Counter(str(r.get('acceptance_path') or 'UNKNOWN') for r in rows))}")
    print(f"level_sources            : {dict(Counter(str(r.get('level_source') or 'UNKNOWN') for r in rows))}")
    print(f"signal_statuses          : {dict(Counter(str(r.get('status') or 'UNKNOWN') for r in rows))}")
    print(f"reabsorbed_within_1_bar  : {sum(1 for r in rows if r.get('next_1bar_reabsorbed') is True)}")
    print(f"reabsorbed_within_2_bars : {sum(1 for r in rows if r.get('next_2bar_reabsorbed') is True)}")
    print(f"signal_mfe_median_pct    : {_median(r.get('signal_mfe_pct') for r in rows)}")
    print(f"next_3bar_mfe_median_pct : {_median(r.get('next_3bar_mfe_pct') for r in rows)}")

    _print_group_summary(rows, "acceptance_path")
    _print_group_summary(rows, "level_source")
    _print_group_summary(rows, "level_type")
    _print_group_summary(rows, "level_candidate_count")
    _print_group_summary(rows, "selected_is_structure_reference")
    _print_group_summary(rows, "selected_level_consecutive_closes_outside")

    print(f"\nFirst {min(PRINT_TOP_N, len(rows))} rows")
    print("-" * 130)
    for row in rows[: max(0, int(PRINT_TOP_N))]:
        print(
            f"{str(row.get('symbol') or ''):14s} {str(row.get('side') or ''):4s} "
            f"{str(row.get('signal_time') or '')[:19]:19s} "
            f"path={str(row.get('acceptance_path') or ''):25s} "
            f"level={str(row.get('level_type') or ''):24s} "
            f"levels={int(_num(row.get('level_candidate_count')) or 0):2d} "
            f"sel_bars={int(_num(row.get('selected_level_consecutive_closes_outside')) or 0):2d} "
            f"span_atr={float(row.get('candidate_level_span_atr') or 0):6.3f} "
            f"entry_atr={float(row.get('entry_distance_atr') or 0):6.3f} "
            f"room_pct={float(row.get('room_to_next_level_pct') or 0):7.4f} "
            f"mfe={float(row.get('signal_mfe_pct') or 0):7.4f} "
            f"mfe_3bar={float(row.get('next_3bar_mfe_pct') or 0):7.4f} "
            f"reabs_2={row.get('next_2bar_reabsorbed')}"
        )

    if extraction_failures:
        raise RuntimeError(
            f"ACCEPTED_BREAKOUT review completed with {extraction_failures} candidate extraction failures. "
            "Inspect the log/report before tuning."
        )


if __name__ == "__main__":
    main()
