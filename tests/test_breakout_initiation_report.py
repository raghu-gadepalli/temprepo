#!/usr/bin/env python3
"""EARLY BREAKOUT_INITIATION signal report (read-only, offline).

Edit TEST SETTINGS and run:

    python tests/test_breakout_initiation_report.py

Purpose
-------
Test the original AutoTrades price-action objective directly: enter near the
beginning of a potentially meaningful trend when price first departs from a
high-quality dynamic range, rather than waiting for fully confirmed acceptance.

This report is intentionally separate from the frozen BREAKOUT_RESOLUTION
report. It uses the same immutable snapshots and the same stable dynamic-range
episode identity, but generates only EARLY_BREAKOUT signals.

Candidate entry paths
---------------------
* STRONG_DISPLACEMENT_HOLD: one strong directional close outside the current
  dynamic boundary, followed within two candles by a hold outside or successful
  boundary retest.
* TWO_MEANINGFUL_CLOSES: two consecutive completed closes materially outside
  the dynamic boundary with confirming price action and participation.

Design rules
------------
* Only INTRADAY_BALANCE dynamic boundaries own signal-producing episodes.
* FAILED breakout evidence is still tracked, but it does not create signals.
* HMA alignment is exported as context and is not an entry gate.
* The range must be mature, breakout-eligible, and sufficiently high quality.
* Entry freshness and structural room remain strict.
* The existing fully confirmed acceptance policy is evaluated in observation
  mode after the early entry. The report records whether and when it later
  confirmed, how many minutes the early entry led it, and whether price was
  reabsorbed before confirmation.
* Signal quality is measured with fixed 3/5/9-candle and full-session MFE/MAE.
* The stock-level Advisor remains observation-only and never changes a signal.

Outputs
-------
* breakout_initiation_report.csv
* test_breakout_initiation_report.log

The program does not write signals, trades, setup state, audit rows, or snapshots.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from database.database import get_trades_db
from logconfig import setup_logging
from models.trade_models import Snapshot as SnapshotORM
from models.trade_models import Symbol as SymbolORM
from services.evidence.evidence_score_helper import BUY, SELL, opposite_side, upper
from utils.json_utils import sanitize_json


# =============================================================================
# TEST SETTINGS -- edit here; no command-line arguments are required.
# =============================================================================
TEST_DATE: str = "2026-07-20"

OUTPUT_CSV_PATH: str = "breakout_initiation_report.csv"
LOG_FILE: str = "test_breakout_initiation_report.log"

# Empty list = all matching symbols in the selected snapshot day.
SYMBOL_FILTER: List[str] = []

# Match the normal replay universe. Add another type only if that type is
# intentionally present in the replay universe.
SYMBOL_TYPE_FILTER: Tuple[str, ...] = ("EQ",)

# Optional development cap. None = all matching symbols.
MAX_SYMBOLS: Optional[int] = None

# Boundary universe switch.
# True  = only current INTRADAY_BALANCE dynamic-range high/low boundaries can
#         create BREAKOUT_RESOLUTION episodes and signals.
# False = dynamic boundaries plus ORB and previous-day high/low boundaries.
DYNAMIC_BOUNDARIES_ONLY: bool = True

# FAILED evidence remains part of the episode lifecycle but does not emit trades.
GENERATE_FAILED_SIGNALS: bool = False

# Hypothetical stock-level Advisor. This is observation-only: it never removes,
# delays, or changes a BREAKOUT_RESOLUTION signal. It uses only snapshots at or
# before the signal time, so the exported advice contains no end-of-day hindsight.
ENABLE_HYPOTHETICAL_STOCK_ADVISOR: bool = True
ADVISOR_RECENT_BARS: int = 5
ADVISOR_EFFICIENCY_BARS: int = 10
ADVISOR_UPTREND_SCORE: float = 25.0
ADVISOR_STRONG_UPTREND_SCORE: float = 55.0
ADVISOR_DOWNTREND_SCORE: float = -25.0
ADVISOR_STRONG_DOWNTREND_SCORE: float = -55.0
ADVISOR_COUNTERTREND_EXCEPTION_SCORE: float = 88.0
ADVISOR_COUNTERTREND_EXCEPTION_FOLLOWTHROUGH: float = 70.0
ADVISOR_COUNTERTREND_EXCEPTION_REENTRY_ATR: float = 0.50

# Failed-breakout replacement for the old 2.50 ATR total-width condition.
# The entry must retain at least the larger of these two amounts to the opposite
# edge of the frozen accepted range. These are experimental report defaults and
# are intentionally explicit here for later comparison/tuning.
FAILURE_MIN_OPPOSITE_EDGE_ROOM_ATR: float = 1.00
FAILURE_MIN_OPPOSITE_EDGE_ROOM_PCT: float = 0.50

# Current FAILED_BREAKOUT event memory: five 3-minute candles / ~15.5 minutes.
FAILURE_WATCH_VALID_MINUTES: float = 15.5

# Objective comparison horizon. Entry is at the signal snapshot close; exit is
# the close after this many subsequent snapshots, or the final snapshot when the
# day ends sooner. This is intentionally not the production TradeManager.
COMPARISON_EXIT_AFTER_BARS: int = 9
MFE_MAE_HORIZONS: Tuple[int, ...] = (3, 5, 9)

# Keep an unresolved boundary episode available while its observation remains
# active. Failed-resolution confirmation uses FAILURE_WATCH_VALID_MINUTES above.
EPISODE_IDLE_EXPIRY_MINUTES: float = 30.0

# Existing signal invalidation policy (same defaults as evidence config).
INVALIDATION_BUFFER_ATR: float = 0.20
INVALIDATION_STRONG_SINGLE_ATR: float = 0.50
INVALIDATION_REQUIRED_CLOSES: int = 2

# A same-boundary episode may restart only after price has returned materially
# into its frozen value for the existing invalidation close count. This avoids
# introducing a separate reset threshold during the architecture comparison.
EPISODE_RESET_REQUIRED_CLOSES: int = INVALIDATION_REQUIRED_CLOSES

# When both BUY and SELL resolutions are ready at the same snapshot, require
# this score gap; otherwise treat the stock as unresolved and do not enter.
SIDE_CONFLICT_MIN_SCORE_GAP: float = 15.0

# Candidate replacement policy version. Keep this in every exported row so
# multi-day comparisons never mix definitions silently. The suffix records the
# active boundary universe selected by DYNAMIC_BOUNDARIES_ONLY.
POLICY_VERSION: str = "BI_V1_EARLY_DYNAMIC_STOCK_ADVISOR"

# Neutral boundary observation. These values are copied into this report and
# intentionally no longer read from the live ACCEPTED/FAILED setup config.
BOUNDARY_ATTEMPT_BUFFER_ATR: float = 0.15
BOUNDARY_ACCEPTANCE_BUFFER_ATR: float = 0.25
BOUNDARY_MICRO_ATTEMPT_BUFFER_ATR: float = 0.25
BOUNDARY_MICRO_ACCEPTANCE_BUFFER_ATR: float = 0.35
BOUNDARY_GENUINE_OUTSIDE_EXCURSION_ATR: float = 0.25
BOUNDARY_STRONG_WICK_EXCURSION_ATR: float = 0.50

# EARLY BREAKOUT_INITIATION policy. These thresholds are intentionally copied
# into this report so the experiment can evolve independently of live setups.
EARLY_MIN_RANGE_QUALITY: float = 55.0
EARLY_MIN_RANGE_BARS: int = 4
EARLY_MIN_RANGE_AGE_MINUTES: float = 9.0
EARLY_MIN_CLOSE_OUTSIDE_ATR: float = 0.15
EARLY_STRONG_CLOSE_OUTSIDE_ATR: float = 0.20
EARLY_STRONG_DISPLACEMENT_MOVE_ATR: float = 0.50
EARLY_STRONG_BODY_FRACTION: float = 0.55
EARLY_STRONG_BAR_RVOL: float = 1.00
EARLY_STRONG_BUY_CLOSE_POSITION: float = 0.72
EARLY_STRONG_SELL_CLOSE_POSITION: float = 0.28
EARLY_HOLD_MIN_OUTSIDE_ATR: float = 0.05
EARLY_HOLD_MAX_MINUTES: float = 6.5
EARLY_RETEST_TOLERANCE_ATR: float = 0.12
EARLY_TWO_CLOSE_REQUIRED: int = 2
EARLY_MAX_ENTRY_DISTANCE_ATR: float = 1.00
EARLY_STRONG_MAX_ENTRY_DISTANCE_ATR: float = 1.25
EARLY_MIN_BAR_RVOL: float = 0.80
EARLY_WEAK_PARTICIPATION_BANDS: Tuple[str, ...] = ("WEAK", "LOW", "VERY_LOW")
EARLY_BLOCK_IF_FULLY_CONFIRMED: bool = True

# ACCEPTED resolution policy.
ACCEPTED_FIXED_REQUIRED_CLOSES: int = 2
ACCEPTED_DYNAMIC_REQUIRED_CLOSES: int = 3
ACCEPTED_OTHER_REQUIRED_CLOSES: int = 3
ACCEPTED_MAX_ENTRY_DISTANCE_ATR: float = 1.50
ACCEPTED_STRICT_MAX_ENTRY_DISTANCE_ATR: float = 2.50
ACCEPTED_MIN_STRUCTURAL_ROOM_ATR: float = 1.25
ACCEPTED_MIN_STRUCTURAL_ROOM_PCT: float = 0.50
ACCEPTED_DYNAMIC_MIN_AGE_MINUTES: float = 12.0
ACCEPTED_MIN_BAR_RVOL: float = 0.80
ACCEPTED_WEAK_PARTICIPATION_BANDS: Tuple[str, ...] = ("WEAK", "LOW", "VERY_LOW")
ACCEPTED_STRICT_CANDLE_MOVE_ATR: float = 1.50
ACCEPTED_STRICT_BAR_RVOL: float = 2.00
ACCEPTED_STRICT_BODY_FRACTION: float = 0.65
ACCEPTED_STRICT_BREAK_DISTANCE_ATR: float = 0.40
ACCEPTED_STRICT_BUY_CLOSE_POSITION: float = 0.80
ACCEPTED_STRICT_SELL_CLOSE_POSITION: float = 0.20
ACCEPTED_TERMINAL_MOVE_15M_ATR: float = 2.00
ACCEPTED_TERMINAL_VWAP_DISTANCE_ATR: float = 1.50
ACCEPTED_TERMINAL_BUY_SOD_POSITION: float = 0.85
ACCEPTED_TERMINAL_SELL_SOD_POSITION: float = 0.15
ACCEPTED_TERMINAL_COMPONENTS_TO_BLOCK: int = 2
ACCEPTED_BLOCK_BUY_RSI: float = 78.0
ACCEPTED_BLOCK_SELL_RSI: float = 22.0
ACCEPTED_BLOCK_BUY_BB: float = 1.20
ACCEPTED_BLOCK_SELL_BB: float = -0.20

# FAILED resolution policy. A failure must prove a genuine outside auction,
# meaningful re-entry, persistence inside, and directional follow-through.
FAILURE_MEANINGFUL_REENTRY_ATR: float = 0.20
FAILURE_REQUIRED_INSIDE_CLOSES: int = 2
FAILURE_STRONG_RECLAIM_MOVE_ATR: float = 0.50
FAILURE_FOLLOWTHROUGH_BUFFER_ATR: float = 0.05
FAILURE_MAX_ENTRY_DISTANCE_ATR: float = 1.00
FAILURE_BLOCK_BUY_BB: float = 0.90
FAILURE_BLOCK_SELL_BB: float = 0.10
FAILURE_BLOCK_BUY_RSI: float = 62.0
FAILURE_BLOCK_SELL_RSI: float = 38.0
FAILURE_BLOCK_BUY_RSI_BB: float = 0.85
FAILURE_BLOCK_SELL_RSI_BB: float = 0.15
FAILURE_FRESH_TREND_MOVE_15M_ATR: float = 0.60
FAILURE_FRESH_TREND_BUY_POSITION_MAX: float = 0.30
FAILURE_FRESH_TREND_SELL_POSITION_MIN: float = 0.70
FAILURE_FRESH_TREND_ADX_MIN: float = 25.0

# Common price-action definition copied into this candidate engine.
PA_STRENGTH_CONFIRM_MIN: float = 63.0
PA_BUY_CLOSE_POSITION_MIN: float = 0.62
PA_SELL_CLOSE_POSITION_MAX: float = 0.38
PA_MIN_SINGLE_CANDLE_MOVE_ATR: float = 0.05
PA_MULTI_BUY_MOVE_15M_ATR: float = 0.10
PA_MULTI_SELL_MOVE_15M_ATR: float = -0.10
PA_MULTI_BUY_POSITION_MIN: float = 0.55
PA_MULTI_SELL_POSITION_MAX: float = 0.45

ENTRY_WINDOW_START: str = "09:30:00"
ENTRY_WINDOW_END: str = "15:00:00"

# Strict mode stops on an unexpected snapshot contract error instead of writing
# a silently partial comparison.
STRICT_EVALUATION: bool = True

PRINT_ROWS: int = 100
PROGRESS_EVERY_SYMBOLS: int = 10

SETUP_LABEL = "BREAKOUT_INITIATION"
RESOLUTION_EARLY = "EARLY_BREAKOUT"
RESOLUTION_ACCEPTED = "ACCEPTED"
RESOLUTION_FAILED = "FAILED"

logger = logging.getLogger("tests.test_breakout_initiation_report")


# =============================================================================
# Data classes
# =============================================================================
@dataclass
class BoundaryEpisode:
    episode_key: str
    structural_key: str
    episode_sequence: int
    symbol: str
    breakout_side: str
    failure_side: str
    first_seen_time: datetime
    last_seen_time: datetime
    attempt_time: Optional[datetime]
    accepted_time: Optional[datetime]
    failed_time: Optional[datetime]
    failure_expires_at: Optional[datetime]
    reference_id: str
    level_type: str
    level_source: str
    level_price: float
    rank: int
    frozen_range: Dict[str, Any]
    observation: Dict[str, Any]
    current_offset_atr: float = 0.0
    max_outside_excursion_atr: float = 0.0
    max_close_outside_atr: float = 0.0
    total_outside_closes: int = 0
    consecutive_outside_closes: int = 0
    consecutive_acceptance_closes: int = 0
    consecutive_early_outside_closes: int = 0
    strong_displacement_time: Optional[datetime] = None
    strong_displacement_price: Optional[float] = None
    strong_displacement_move_atr: Optional[float] = None
    strong_displacement_body_fraction: Optional[float] = None
    strong_displacement_bar_rvol: Optional[float] = None
    strong_displacement_close_position: Optional[float] = None
    early_hold_time: Optional[datetime] = None
    early_retest_time: Optional[datetime] = None
    confirmed_policy_ready_time: Optional[datetime] = None
    confirmed_policy_ready_price: Optional[float] = None
    first_outside_close_time: Optional[datetime] = None
    last_outside_time: Optional[datetime] = None
    first_reentry_time: Optional[datetime] = None
    reentry_depth_atr: float = 0.0
    consecutive_inside_closes: int = 0
    reclaim_snapshot_time: Optional[datetime] = None
    reclaim_open: Optional[float] = None
    reclaim_high: Optional[float] = None
    reclaim_low: Optional[float] = None
    reclaim_close: Optional[float] = None
    reclaim_move_atr: Optional[float] = None
    hold_after_reclaim_count: int = 0
    followthrough_confirmed: bool = False
    followthrough_time: Optional[datetime] = None
    followthrough_strength: float = 0.0
    failure_reaccepted_outside: bool = False
    last_close: Optional[float] = None
    last_high: Optional[float] = None
    last_low: Optional[float] = None
    state: str = "UNRESOLVED"
    acceptance_strength: float = 0.0
    failure_strength: float = 0.0
    emitted_resolutions: set[Tuple[str, str]] = field(default_factory=set)
    reset_inside_closes: int = 0
    reset_started_at: Optional[datetime] = None
    episode_reset_time: Optional[datetime] = None
    episode_reset_reason: Optional[str] = None
    terminal: bool = False
    terminal_reason: Optional[str] = None


@dataclass
class ReadyCandidate:
    symbol: str
    resolution: str
    side: str
    signal_time: datetime
    entry_price: float
    entry_atr: float
    episode_key: str
    reference_id: str
    level_type: str
    level_source: str
    level_price: float
    rank: int
    score: float
    price_action_strength: float
    entry_distance_atr: Optional[float]
    remaining_room_points: Optional[float]
    remaining_room_atr: Optional[float]
    remaining_room_pct: Optional[float]
    accepted_range_low: Optional[float]
    accepted_range_high: Optional[float]
    episode_state: str
    frozen_range_basis: str
    reasons: List[str] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalyticalSignal:
    symbol: str
    resolution: str
    side: str
    signal_time: datetime
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str
    pnl_points: float
    pnl_pct: float
    eod_pnl_points: float
    eod_pnl_pct: float
    mfe_3bar_pct: Optional[float]
    mae_3bar_pct: Optional[float]
    mfe_5bar_pct: Optional[float]
    mae_5bar_pct: Optional[float]
    mfe_9bar_pct: Optional[float]
    mae_9bar_pct: Optional[float]
    full_mfe_pct: Optional[float]
    full_mae_pct: Optional[float]
    structural_key: str
    episode_sequence: int
    episode_reset_time: Optional[datetime]
    episode_reset_reason: Optional[str]
    episode_key: str
    episode_state: str
    reference_id: str
    level_type: str
    level_source: str
    reference_price: float
    entry_atr: float
    accepted_range_low: Optional[float]
    accepted_range_high: Optional[float]
    frozen_range_id: Optional[str]
    frozen_range_version: Optional[int]
    frozen_range_basis: str
    acceptance_strength: float
    failure_strength: float
    policy_version: str
    entry_path: str
    range_quality: Optional[float]
    range_width_atr: Optional[float]
    range_bars: int
    range_age_minutes: Optional[float]
    hma_aligned_at_entry: bool
    strong_displacement_time: Optional[datetime]
    early_hold_time: Optional[datetime]
    early_retest_time: Optional[datetime]
    structural_acceptance_time: Optional[datetime]
    confirmed_acceptance_time: Optional[datetime]
    confirmed_acceptance_price: Optional[float]
    lead_to_structural_acceptance_minutes: Optional[float]
    lead_to_confirmed_acceptance_minutes: Optional[float]
    eventually_structurally_accepted: bool
    eventually_fully_confirmed: bool
    reabsorbed_before_full_confirmation: bool
    post_entry_reentry_time: Optional[datetime]
    final_episode_state: str
    final_terminal_reason: Optional[str]
    max_outside_excursion_atr: float
    total_outside_closes: int
    consecutive_acceptance_closes: int
    first_reentry_time: Optional[datetime]
    reentry_depth_atr: float
    consecutive_inside_closes: int
    hold_after_reclaim_count: int
    followthrough_confirmed: bool
    followthrough_time: Optional[datetime]
    followthrough_strength: float
    advisor_enabled: bool
    advisor_regime: str
    advisor_decision: str
    advisor_alignment: str
    advisor_confidence: float
    advisor_score: float
    advisor_day_return_pct: float
    advisor_range_position: float
    advisor_vwap_distance_atr: Optional[float]
    advisor_move_15m_atr: Optional[float]
    advisor_recent_move_atr: float
    advisor_trend_efficiency: float
    advisor_reason: str


# =============================================================================
# Generic helpers
# =============================================================================
def _as_float(value: Any) -> Optional[float]:
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


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "null", "nan"}:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        try:
            return datetime.fromisoformat(text.replace("T", " ").split("+")[0]).replace(tzinfo=None)
        except Exception:
            return None


def _nested(mapping: Any, *keys: str, default: Any = None) -> Any:
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def _snapshot_dt(snapshot: Dict[str, Any]) -> datetime:
    dt = _parse_dt(snapshot.get("snapshot_time"))
    if dt is None:
        raise ValueError(f"Snapshot has no valid snapshot_time: {snapshot.get('snapshot_time')!r}")
    return dt


def _snapshot_close(snapshot: Dict[str, Any]) -> float:
    close = _as_float(_nested(snapshot, "bar", "close"))
    if close is None:
        close = _as_float(snapshot.get("close"))
    if close is None:
        raise ValueError("Snapshot has no valid close")
    return close


def _snapshot_high(snapshot: Dict[str, Any]) -> float:
    high = _as_float(_nested(snapshot, "bar", "high"))
    return float(high if high is not None else _snapshot_close(snapshot))


def _snapshot_low(snapshot: Dict[str, Any]) -> float:
    low = _as_float(_nested(snapshot, "bar", "low"))
    return float(low if low is not None else _snapshot_close(snapshot))


def _snapshot_atr(snapshot: Dict[str, Any]) -> float:
    atr = _as_float(_nested(snapshot, "indicators", "atr", "value"))
    if atr is None or atr <= 0:
        raise ValueError("Snapshot has no positive ATR")
    return atr


def _time_from_config(value: str) -> time:
    return datetime.strptime(value, "%H:%M:%S").time()


def _within_entry_window(ts: datetime) -> bool:
    return _time_from_config(ENTRY_WINDOW_START) <= ts.time() <= _time_from_config(ENTRY_WINDOW_END)


def _close_position(low: float, high: float, close: float) -> float:
    if high <= low:
        return 0.5
    return max(0.0, min(1.0, (close - low) / (high - low)))


def _bar_open(snapshot: Dict[str, Any]) -> float:
    value = _as_float(_nested(snapshot, "bar", "open"))
    return float(value if value is not None else _snapshot_close(snapshot))


def _optional_numeric(snapshot: Dict[str, Any], path: str) -> Optional[float]:
    value: Any = snapshot
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return _as_float(value)


def _optional_path(snapshot: Dict[str, Any], path: str) -> Any:
    value: Any = snapshot
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _accepted_range_context(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return _range_from_snapshot(snapshot)


def _structural_level_candidates(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return unique neutral boundaries without assigning setup outcomes."""
    anchors = _optional_path(snapshot, "structure.anchors") or {}
    levels = _optional_path(snapshot, "levels") or {}
    opening = levels.get("opening_range") if isinstance(levels, dict) else {}
    prev_day = levels.get("prev_day") if isinstance(levels, dict) else {}
    accepted = _accepted_range_context(snapshot)
    out: List[Dict[str, Any]] = []

    def add(
        *,
        reference_id: str,
        level_type: str,
        price: Any,
        side: str,
        source: str,
        rank: int,
        aliases: Optional[List[str]] = None,
        range_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        px = _as_float(price)
        if px is None or side not in {BUY, SELL}:
            return
        alias_values = [level_type, *(aliases or [])]
        for existing in out:
            if existing["side"] != side or abs(float(existing["price"]) - px) > 1e-6:
                continue
            merged = list(existing.get("aliases") or [])
            for alias in alias_values:
                if alias and alias not in merged:
                    merged.append(alias)
            existing["aliases"] = merged
            sources = list(existing.get("sources") or [])
            if source not in sources:
                sources.append(source)
            existing["sources"] = sources
            if rank < int(existing.get("rank") or 999):
                existing.update({
                    "reference_id": reference_id,
                    "level_type": level_type,
                    "source": source,
                    "rank": rank,
                })
            if range_context and not existing.get("range_context"):
                existing["range_context"] = dict(range_context)
            return
        out.append({
            "reference_id": reference_id,
            "level_type": level_type,
            "price": float(px),
            "side": side,
            "source": source,
            "sources": [source],
            "rank": int(rank),
            "aliases": list(dict.fromkeys(alias_values)),
            "range_context": dict(range_context or {}),
        })

    orb_ready = bool(
        (anchors.get("orb_ready") if isinstance(anchors, dict) else False)
        or (opening.get("ready") if isinstance(opening, dict) else False)
    )
    orb_high = (
        anchors.get("orb_high") if isinstance(anchors, dict) else None
    ) or (opening.get("high") if isinstance(opening, dict) else None)
    orb_low = (
        anchors.get("orb_low") if isinstance(anchors, dict) else None
    ) or (opening.get("low") if isinstance(opening, dict) else None)
    pdh = (
        anchors.get("pdh") if isinstance(anchors, dict) else None
    ) or (prev_day.get("high") if isinstance(prev_day, dict) else None)
    pdl = (
        anchors.get("pdl") if isinstance(anchors, dict) else None
    ) or (prev_day.get("low") if isinstance(prev_day, dict) else None)

    if not DYNAMIC_BOUNDARIES_ONLY:
        if orb_ready:
            add(reference_id="ORB_HIGH", level_type="ORB_HIGH", price=orb_high, side=BUY, source="ORB", rank=20)
            add(reference_id="ORB_LOW", level_type="ORB_LOW", price=orb_low, side=SELL, source="ORB", rank=20)
        add(reference_id="PDH", level_type="PREVIOUS_DAY_HIGH", price=pdh, side=BUY, source="PREVIOUS_DAY", rank=30)
        add(reference_id="PDL", level_type="PREVIOUS_DAY_LOW", price=pdl, side=SELL, source="PREVIOUS_DAY", rank=30)

    if bool(accepted.get("breakout_eligible")) and _usable_range(accepted):
        source = upper(accepted.get("source") or "ACCEPTED_RANGE")
        if DYNAMIC_BOUNDARIES_ONLY and source != "INTRADAY_BALANCE":
            return out
        range_id = str(accepted.get("range_id") or "ACTIVE_RANGE")
        version = _as_int(accepted.get("version"), 0)
        context = {
            "range_id": accepted.get("range_id"),
            "version": version,
            "source": accepted.get("source"),
            "range_type": accepted.get("range_type"),
            "width_atr": accepted.get("width_atr"),
            "quality": accepted.get("quality"),
            "established_at": accepted.get("established_at"),
            "breakout_eligible": True,
        }
        if source == "ORB":
            high_type, low_type = "ORB_HIGH", "ORB_LOW"
            high_id, low_id = "ORB_HIGH", "ORB_LOW"
        elif source == "INTRADAY_BALANCE":
            high_type, low_type = "DYNAMIC_RANGE_HIGH", "DYNAMIC_RANGE_LOW"
            high_id, low_id = f"{range_id}:V{version}:HIGH", f"{range_id}:V{version}:LOW"
        else:
            high_type, low_type = "ACCEPTED_RANGE_HIGH", "ACCEPTED_RANGE_LOW"
            high_id, low_id = f"{range_id}:V{version}:HIGH", f"{range_id}:V{version}:LOW"
        add(
            reference_id=high_id,
            level_type=high_type,
            price=accepted.get("high"),
            side=BUY,
            source="STRUCTURE_ACCEPTED_RANGE",
            rank=10,
            aliases=["ACCEPTED_RANGE_HIGH"],
            range_context=context,
        )
        add(
            reference_id=low_id,
            level_type=low_type,
            price=accepted.get("low"),
            side=SELL,
            source="STRUCTURE_ACCEPTED_RANGE",
            rank=10,
            aliases=["ACCEPTED_RANGE_LOW"],
            range_context=context,
        )
    return sorted(out, key=lambda item: (int(item.get("rank") or 999), str(item.get("reference_id") or "")))


def _level_offsets(snapshot: Dict[str, Any], side: str, level: float) -> Dict[str, float]:
    atr = _snapshot_atr(snapshot)
    close = _snapshot_close(snapshot)
    high = _snapshot_high(snapshot)
    low = _snapshot_low(snapshot)
    if side == BUY:
        close_offset = (close - level) / atr
        extreme_offset = (high - level) / atr
    elif side == SELL:
        close_offset = (level - close) / atr
        extreme_offset = (level - low) / atr
    else:
        raise ValueError(f"Unsupported boundary side: {side}")
    return {"close_offset_atr": float(close_offset), "extreme_offset_atr": float(extreme_offset)}


def _range_type_for_observation(observation: Dict[str, Any]) -> str:
    context = observation.get("range_context") if isinstance(observation.get("range_context"), dict) else {}
    return upper(context.get("range_type") or "")


def _attempt_buffer(observation: Dict[str, Any]) -> float:
    return BOUNDARY_MICRO_ATTEMPT_BUFFER_ATR if _range_type_for_observation(observation) == "MICRO_COMPRESSION" else BOUNDARY_ATTEMPT_BUFFER_ATR


def _acceptance_buffer(observation: Dict[str, Any]) -> float:
    return BOUNDARY_MICRO_ACCEPTANCE_BUFFER_ATR if _range_type_for_observation(observation) == "MICRO_COMPRESSION" else BOUNDARY_ACCEPTANCE_BUFFER_ATR


def _required_acceptance_closes(episode: BoundaryEpisode) -> int:
    level_type = upper(episode.level_type)
    if level_type in {"ORB_HIGH", "ORB_LOW", "PREVIOUS_DAY_HIGH", "PREVIOUS_DAY_LOW"}:
        return ACCEPTED_FIXED_REQUIRED_CLOSES
    if level_type in {"DYNAMIC_RANGE_HIGH", "DYNAMIC_RANGE_LOW", "ACCEPTED_RANGE_HIGH", "ACCEPTED_RANGE_LOW"}:
        return ACCEPTED_DYNAMIC_REQUIRED_CLOSES
    return ACCEPTED_OTHER_REQUIRED_CLOSES


def _price_action_confirmation(snapshot: Dict[str, Any], side: str) -> Dict[str, Any]:
    open_price = _bar_open(snapshot)
    high = _snapshot_high(snapshot)
    low = _snapshot_low(snapshot)
    close = _snapshot_close(snapshot)
    current_move_atr = _optional_numeric(snapshot, "market_windows.current.move_atr")
    if current_move_atr is None:
        atr = _snapshot_atr(snapshot)
        current_move_atr = (close - open_price) / atr
    move_15m = _optional_numeric(snapshot, "market_windows.15m.move_atr") or 0.0
    pos_15m = _optional_numeric(snapshot, "market_windows.15m.close_position_in_range")
    pos_15m = 0.5 if pos_15m is None else pos_15m
    current_pos = _close_position(low, high, close)

    if side == BUY:
        single = close > open_price and current_pos >= PA_BUY_CLOSE_POSITION_MIN and current_move_atr >= PA_MIN_SINGLE_CANDLE_MOVE_ATR
        multi = move_15m >= PA_MULTI_BUY_MOVE_15M_ATR and pos_15m >= PA_MULTI_BUY_POSITION_MIN
        close_component = current_pos * 20.0
    elif side == SELL:
        single = close < open_price and current_pos <= PA_SELL_CLOSE_POSITION_MAX and current_move_atr <= -PA_MIN_SINGLE_CANDLE_MOVE_ATR
        multi = move_15m <= PA_MULTI_SELL_MOVE_15M_ATR and pos_15m <= PA_MULTI_SELL_POSITION_MAX
        close_component = (1.0 - current_pos) * 20.0
    else:
        raise ValueError(f"Unsupported price-action side: {side}")
    strength = max(0.0, min(100.0, (45.0 if single else 0.0) + (35.0 if multi else 0.0) + close_component))
    return {
        "confirmed": bool((single or multi) and strength >= PA_STRENGTH_CONFIRM_MIN),
        "single_candle_confirmed": bool(single),
        "multi_candle_confirmed": bool(multi),
        "strength": round(strength, 2),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "current_close_position": current_pos,
        "current_move_atr": current_move_atr,
        "move_15m_atr": move_15m,
        "position_15m": pos_15m,
    }


def _hma_alignment(snapshot: Dict[str, Any], side: str) -> Dict[str, Any]:
    current_state = upper(_optional_path(snapshot, "indicators.hma.state") or "")
    current_strength = upper(_optional_path(snapshot, "indicators.hma.strength") or "")
    state_15m = upper(_optional_path(snapshot, "indicator_windows.hma.15m.end_state") or "")
    strength_15m = upper(_optional_path(snapshot, "indicator_windows.hma.15m.end_strength") or "")
    buy_states = {"BUY", "STRONG_BUY", "MEDIUM_BUY", "UP_ACCELERATING", "UP_SLOWING", "TURNING_UP"}
    sell_states = {"SELL", "STRONG_SELL", "MEDIUM_SELL", "DOWN_ACCELERATING", "DOWN_SLOWING", "TURNING_DOWN"}
    wanted = buy_states if side == BUY else sell_states
    current_ok = current_state in wanted or current_strength in wanted
    window_ok = not state_15m or state_15m in wanted or strength_15m in wanted
    return {
        "aligned": bool(current_ok and window_ok),
        "current_state": current_state,
        "current_strength": current_strength,
        "state_15m": state_15m,
        "strength_15m": strength_15m,
    }


def _strict_displacement_context(snapshot: Dict[str, Any], side: str, level: float) -> Dict[str, Any]:
    atr = _snapshot_atr(snapshot)
    open_price = _bar_open(snapshot)
    high = _snapshot_high(snapshot)
    low = _snapshot_low(snapshot)
    close = _snapshot_close(snapshot)
    body = abs(close - open_price)
    candle_range = max(high - low, 1e-9)
    body_fraction = body / candle_range
    move_atr = abs(close - open_price) / atr
    bar_rvol = _optional_numeric(snapshot, "volume.bar_rvol")
    close_pos = _close_position(low, high, close)
    break_distance = ((close - level) / atr) if side == BUY else ((level - close) / atr)
    directional = close > open_price if side == BUY else close < open_price
    close_location = close_pos >= ACCEPTED_STRICT_BUY_CLOSE_POSITION if side == BUY else close_pos <= ACCEPTED_STRICT_SELL_CLOSE_POSITION
    qualified = bool(
        directional
        and move_atr >= ACCEPTED_STRICT_CANDLE_MOVE_ATR
        and bar_rvol is not None
        and bar_rvol >= ACCEPTED_STRICT_BAR_RVOL
        and body_fraction >= ACCEPTED_STRICT_BODY_FRACTION
        and close_location
        and break_distance >= ACCEPTED_STRICT_BREAK_DISTANCE_ATR
    )
    return {
        "qualified": qualified,
        "move_atr": move_atr,
        "bar_rvol": bar_rvol,
        "body_fraction": body_fraction,
        "close_position": close_pos,
        "break_distance_atr": break_distance,
    }


def _dynamic_range_age_context(episode: BoundaryEpisode, ts: datetime) -> Dict[str, Any]:
    if not _is_dynamic_level(episode.observation):
        return {"passes": True, "age_minutes": None}
    established = _parse_dt(episode.frozen_range.get("established_at")) or _parse_dt(episode.frozen_range.get("end_time"))
    if established is None:
        return {"passes": False, "age_minutes": None}
    age = max(0.0, (ts - established).total_seconds() / 60.0)
    return {"passes": age >= ACCEPTED_DYNAMIC_MIN_AGE_MINUTES, "age_minutes": age}


def _structural_room_context(
    snapshot: Dict[str, Any],
    episode: BoundaryEpisode,
    levels: Sequence[Dict[str, Any]],
    side: str,
    close: float,
    atr: float,
) -> Dict[str, Any]:
    barriers: List[Tuple[float, str]] = []
    for level in levels:
        if str(level.get("reference_id") or "") == episode.reference_id:
            continue
        px = _as_float(level.get("price"))
        if px is None:
            continue
        if side == BUY and px > close:
            barriers.append((px - close, str(level.get("reference_id") or level.get("level_type") or "LEVEL")))
        elif side == SELL and px < close:
            barriers.append((close - px, str(level.get("reference_id") or level.get("level_type") or "LEVEL")))
    anchors = _optional_path(snapshot, "structure.anchors") or {}
    extra = [
        ("RECENT15_HIGH", anchors.get("recent15_high"), BUY),
        ("RECENT15_LOW", anchors.get("recent15_low"), SELL),
    ]
    for label, value, barrier_side in extra:
        px = _as_float(value)
        if px is None or barrier_side != side:
            continue
        if side == BUY and px > close:
            barriers.append((px - close, label))
        elif side == SELL and px < close:
            barriers.append((close - px, label))
    required = max(ACCEPTED_MIN_STRUCTURAL_ROOM_ATR * atr, ACCEPTED_MIN_STRUCTURAL_ROOM_PCT * close / 100.0)
    if not barriers:
        return {"passes": True, "distance_points": None, "distance_atr": None, "distance_pct": None, "barrier": None}
    distance, label = min(barriers, key=lambda item: item[0])
    return {
        "passes": distance >= required,
        "distance_points": distance,
        "distance_atr": distance / atr,
        "distance_pct": distance / close * 100.0,
        "barrier": label,
        "required_points": required,
    }


def _terminal_extension_context(snapshot: Dict[str, Any], side: str) -> Dict[str, Any]:
    move_15m = _optional_numeric(snapshot, "market_windows.15m.move_atr") or 0.0
    vwap_distance = _optional_numeric(snapshot, "indicators.vwap.distance_atr")
    sod_position = _optional_numeric(snapshot, "market_windows.sod.close_position_in_range")
    components: List[str] = []
    if side == BUY:
        if move_15m >= ACCEPTED_TERMINAL_MOVE_15M_ATR:
            components.append("MOVE_15M")
        if vwap_distance is not None and vwap_distance >= ACCEPTED_TERMINAL_VWAP_DISTANCE_ATR:
            components.append("VWAP_EXTENSION")
        if sod_position is not None and sod_position >= ACCEPTED_TERMINAL_BUY_SOD_POSITION:
            components.append("SOD_LOCATION")
    else:
        if move_15m <= -ACCEPTED_TERMINAL_MOVE_15M_ATR:
            components.append("MOVE_15M")
        if vwap_distance is not None and vwap_distance <= -ACCEPTED_TERMINAL_VWAP_DISTANCE_ATR:
            components.append("VWAP_EXTENSION")
        if sod_position is not None and sod_position <= ACCEPTED_TERMINAL_SELL_SOD_POSITION:
            components.append("SOD_LOCATION")
    return {"blocked": len(components) >= ACCEPTED_TERMINAL_COMPONENTS_TO_BLOCK, "components": components}


def _fresh_trend_against_failure(snapshot: Dict[str, Any], side: str) -> Dict[str, Any]:
    hma = _hma_alignment(snapshot, opposite_side(side))
    move_15m = _optional_numeric(snapshot, "market_windows.15m.move_atr") or 0.0
    position_15m = _optional_numeric(snapshot, "market_windows.15m.close_position_in_range")
    position_15m = 0.5 if position_15m is None else position_15m
    adx = _optional_numeric(snapshot, "indicators.adx.value") or 0.0
    rsi = _optional_numeric(snapshot, "indicators.rsi.value")
    bb = _optional_numeric(snapshot, "indicators.bollinger.position")
    if side == BUY:
        directional = move_15m <= -FAILURE_FRESH_TREND_MOVE_15M_ATR and position_15m <= FAILURE_FRESH_TREND_BUY_POSITION_MAX
        exhausted = bool((rsi is not None and rsi <= 32.0) or (bb is not None and bb <= 0.10))
    else:
        directional = move_15m >= FAILURE_FRESH_TREND_MOVE_15M_ATR and position_15m >= FAILURE_FRESH_TREND_SELL_POSITION_MIN
        exhausted = bool((rsi is not None and rsi >= 68.0) or (bb is not None and bb >= 0.90))
    blocked = bool(hma.get("aligned") and directional and adx >= FAILURE_FRESH_TREND_ADX_MIN and not exhausted)
    return {
        "blocked": blocked,
        "opposing_hma_aligned": hma.get("aligned"),
        "move_15m_atr": move_15m,
        "position_15m": position_15m,
        "adx": adx,
        "exhausted_exception": exhausted,
    }


def _normalize_snapshot_payload(row: SnapshotORM) -> Dict[str, Any]:
    raw = row.data
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError(f"Snapshot payload is not an object: {row.symbol} @ {row.snapshot_time}")
    payload = sanitize_json(raw)
    payload.setdefault("symbol", row.symbol)
    payload.setdefault("snapshot_time", row.snapshot_time.isoformat())
    if payload.get("close") is None:
        payload["close"] = _nested(payload, "bar", "close")
    return payload


def _structural_key(symbol: str, observation: Dict[str, Any]) -> str:
    """Stable structural identity for one boundary, independent of attempt time."""
    range_context = (
        observation.get("range_context")
        if isinstance(observation.get("range_context"), dict)
        else {}
    )
    # Dynamic identity must retain its owning range version. Fixed ORB/PD levels
    # must not split merely because the current accepted-range context changed.
    if _is_dynamic_level(observation):
        range_id = str(range_context.get("range_id") or "")
        range_version = _as_int(range_context.get("version"), 0)
    else:
        range_id = ""
        range_version = 0
    level = _as_float(observation.get("price"))
    return "|".join([
        symbol,
        str(observation.get("reference_id") or ""),
        upper(observation.get("side") or ""),
        f"{float(level):.8f}" if level is not None else "",
        range_id,
        str(range_version),
    ])


def _episode_key(
    structural_key: str,
    frozen_range: Dict[str, Any],
    sequence: int,
) -> str:
    frozen_id, frozen_version = _range_identity(frozen_range)
    return "|".join([
        structural_key,
        f"FROZEN={frozen_id}:{frozen_version}",
        f"SEQ={sequence}",
    ])


def _episodes_for_structural_key(
    episodes: Dict[str, "BoundaryEpisode"],
    structural_key: str,
) -> List["BoundaryEpisode"]:
    return sorted(
        [episode for episode in episodes.values() if episode.structural_key == structural_key],
        key=lambda episode: episode.episode_sequence,
    )


def _latest_episode_for_structural_key(
    episodes: Dict[str, "BoundaryEpisode"],
    structural_key: str,
) -> Optional["BoundaryEpisode"]:
    matching = _episodes_for_structural_key(episodes, structural_key)
    return matching[-1] if matching else None



def _range_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    raw = _nested(snapshot, "structure", "accepted", "range", default={}) or {}
    accepted = _nested(snapshot, "structure", "accepted", default={}) or {}
    if not isinstance(raw, dict):
        raw = {}
    if not isinstance(accepted, dict):
        accepted = {}
    return {
        "range_id": raw.get("range_id"),
        "version": _as_int(raw.get("version"), 0),
        "high": _as_float(raw.get("high")),
        "low": _as_float(raw.get("low")),
        "source": raw.get("source"),
        "range_type": raw.get("range_type"),
        "width_pct": _as_float(raw.get("width_pct")),
        "width_atr": _as_float(raw.get("width_atr")),
        "start_time": raw.get("start_time"),
        "end_time": raw.get("end_time"),
        "established_at": raw.get("established_at"),
        "evidence_cutoff": raw.get("evidence_cutoff"),
        "bars": _as_int(raw.get("bars"), 0),
        "provisional": bool(raw.get("provisional", False)),
        "breakout_eligible": bool(raw.get("breakout_eligible", False)),
        "quality": _as_float(accepted.get("quality")),
    }


def _opening_range_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    raw = _nested(snapshot, "levels", "opening_range", default={}) or {}
    if not isinstance(raw, dict):
        raw = {}
    high = _as_float(raw.get("high"))
    low = _as_float(raw.get("low"))
    return {
        "range_id": "ORB",
        "version": 1,
        "high": high,
        "low": low,
        "source": "ORB",
        "range_type": "OPENING_RANGE",
        "breakout_eligible": bool(raw.get("ready")) and high is not None and low is not None,
    }


def _usable_range(value: Dict[str, Any]) -> bool:
    low = _as_float(value.get("low"))
    high = _as_float(value.get("high"))
    return bool(low is not None and high is not None and high > low)


def _range_identity(value: Dict[str, Any]) -> Tuple[str, int]:
    return str(value.get("range_id") or ""), _as_int(value.get("version"), 0)


def _latest_snapshot_at_or_before(
    history: Sequence[Dict[str, Any]],
    target: Optional[datetime],
) -> Optional[Dict[str, Any]]:
    if not history:
        return None
    if target is None:
        return history[-1]
    eligible = [row for row in history if _snapshot_dt(row) <= target]
    return eligible[-1] if eligible else history[0]


def _freeze_range_for_observation(
    *,
    observation: Dict[str, Any],
    history: Sequence[Dict[str, Any]],
    current_snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    """Freeze the range that owned the boundary episode.

    Dynamic references are matched by range_id/version from historical snapshots.
    ORB references use the completed opening range. Other fixed references freeze
    the accepted value that existed at the original attempt, with current value as
    a clearly labelled final fallback.
    """
    anchor_time = (
        _parse_dt(observation.get("attempt_time"))
        or _parse_dt(observation.get("accepted_time"))
        or _parse_dt(observation.get("failed_time"))
        or _snapshot_dt(current_snapshot)
    )
    level = _as_float(observation.get("price"))
    side = upper(observation.get("side") or "")
    range_context = observation.get("range_context") if isinstance(observation.get("range_context"), dict) else {}
    expected_id = str(range_context.get("range_id") or "")
    expected_version = _as_int(range_context.get("version"), 0)

    # Exact dynamic-range match, nearest to the episode anchor.
    if _is_dynamic_level(observation) and expected_id:
        matches: List[Tuple[float, Dict[str, Any], datetime]] = []
        for row in history:
            candidate = _range_from_snapshot(row)
            if not _usable_range(candidate):
                continue
            cid, cver = _range_identity(candidate)
            if cid != expected_id or (expected_version and cver != expected_version):
                continue
            boundary = candidate.get("high") if side == BUY else candidate.get("low")
            if level is not None and boundary is not None and abs(float(boundary) - level) > 1e-5:
                continue
            row_time = _snapshot_dt(row)
            distance = abs((row_time - anchor_time).total_seconds())
            matches.append((distance, candidate, row_time))
        if matches:
            _, frozen, frozen_at = sorted(matches, key=lambda item: item[0])[0]
            frozen = dict(frozen)
            frozen["frozen_at"] = frozen_at.isoformat()
            frozen["freeze_basis"] = "MATCHED_DYNAMIC_RANGE_ID_VERSION_AT_EPISODE"
            return frozen

    anchor_snapshot = _latest_snapshot_at_or_before(history, anchor_time) or current_snapshot

    if upper(observation.get("source") or "") == "ORB" or upper(observation.get("level_type") or "").startswith("ORB_"):
        orb = _opening_range_from_snapshot(anchor_snapshot)
        if not _usable_range(orb):
            orb = _opening_range_from_snapshot(current_snapshot)
        if _usable_range(orb):
            orb["frozen_at"] = _snapshot_dt(anchor_snapshot).isoformat()
            orb["freeze_basis"] = "OPENING_RANGE_AT_EPISODE"
            return orb

    accepted = _range_from_snapshot(anchor_snapshot)
    if _usable_range(accepted):
        accepted["frozen_at"] = _snapshot_dt(anchor_snapshot).isoformat()
        accepted["freeze_basis"] = "ACCEPTED_RANGE_AT_EPISODE_ANCHOR"
        return accepted

    accepted = _range_from_snapshot(current_snapshot)
    accepted["frozen_at"] = _snapshot_dt(current_snapshot).isoformat()
    accepted["freeze_basis"] = "CURRENT_ACCEPTED_RANGE_FALLBACK"
    return accepted


def _materially_back_inside_frozen_value(
    episode: BoundaryEpisode,
    snapshot: Dict[str, Any],
) -> bool:
    """Return True only after price is clearly back inside the episode value."""
    low = _as_float(episode.frozen_range.get("low"))
    high = _as_float(episode.frozen_range.get("high"))
    if low is None or high is None or high <= low:
        return False

    close = _snapshot_close(snapshot)
    atr = _snapshot_atr(snapshot)
    buffer_points = max(0.0, INVALIDATION_BUFFER_ATR * atr)
    if not (low <= close <= high):
        return False

    if episode.breakout_side == BUY:
        return close <= episode.level_price - buffer_points
    return close >= episode.level_price + buffer_points


def _can_restart_terminal_episode(episode: BoundaryEpisode) -> bool:
    return bool(
        episode.terminal
        and episode.terminal_reason == "EPISODE_RESET_INTO_FROZEN_VALUE"
        and episode.episode_reset_time is not None
    )


def _record_blockers(
    *,
    counter: Counter,
    seen: set[Tuple[str, str, str]],
    episode_key: str,
    resolution: str,
    reasons: Iterable[str],
) -> None:
    for reason in reasons:
        code = str(reason or "").strip()
        if not code:
            continue
        key = (episode_key, resolution, code)
        if key in seen:
            continue
        seen.add(key)
        counter.update([code])


def _is_fixed_level(observation: Dict[str, Any]) -> bool:
    level_type = upper(observation.get("level_type") or "")
    source = upper(observation.get("source") or "")
    return bool(
        level_type in {
            "ORB_HIGH",
            "ORB_LOW",
            "PREVIOUS_DAY_HIGH",
            "PREVIOUS_DAY_LOW",
        }
        or source in {"ORB", "PREVIOUS_DAY"}
    )


def _is_dynamic_level(observation: Dict[str, Any]) -> bool:
    level_type = upper(observation.get("level_type") or "")
    return level_type in {
        "DYNAMIC_RANGE_HIGH",
        "DYNAMIC_RANGE_LOW",
        "ACCEPTED_RANGE_HIGH",
        "ACCEPTED_RANGE_LOW",
    } or int(observation.get("rank") or 999) == 10


def _stale_fixed_level_reason(
    observation: Dict[str, Any],
    accepted: Dict[str, Any],
) -> Optional[str]:
    """Apply one common stale fixed-level rule to acceptance and failure.

    BUY breakout-side means a high boundary (ORB_HIGH/PDH). It is stale when the
    low of current accepted dynamic value is above the fixed level.
    SELL breakout-side means a low boundary (ORB_LOW/PDL). It is stale when the
    high of current accepted dynamic value is below the fixed level.
    """
    if not _is_fixed_level(observation):
        return None
    if not bool(accepted.get("breakout_eligible")):
        return None
    if upper(accepted.get("source") or "") != "INTRADAY_BALANCE":
        return None

    level = _as_float(observation.get("price"))
    dynamic_low = _as_float(accepted.get("low"))
    dynamic_high = _as_float(accepted.get("high"))
    side = upper(observation.get("side") or "")
    if level is None:
        return None
    if side == BUY and dynamic_low is not None and dynamic_low > level:
        return "STALE_FIXED_LEVEL_DYNAMIC_VALUE_MIGRATED_ABOVE"
    if side == SELL and dynamic_high is not None and dynamic_high < level:
        return "STALE_FIXED_LEVEL_DYNAMIC_VALUE_MIGRATED_BELOW"
    return None


def _room_to_failure_opposite_edge(
    *,
    side: str,
    close: float,
    atr: float,
    accepted: Dict[str, Any],
) -> Dict[str, Any]:
    low = _as_float(accepted.get("low"))
    high = _as_float(accepted.get("high"))
    if low is None or high is None or high <= low:
        return {
            "available": False,
            "passes": False,
            "reason": "FAILED_BREAKOUT_ACCEPTED_RANGE_NOT_USABLE",
            "low": low,
            "high": high,
        }

    if side == BUY:
        room_points = high - close
    elif side == SELL:
        room_points = close - low
    else:
        raise ValueError(f"Unsupported failure candidate side: {side}")

    room_atr = room_points / atr if atr > 0 else None
    room_pct = (room_points / close) * 100.0 if close > 0 else None
    required_points = max(
        FAILURE_MIN_OPPOSITE_EDGE_ROOM_ATR * atr,
        (FAILURE_MIN_OPPOSITE_EDGE_ROOM_PCT / 100.0) * close,
    )
    passes = bool(room_points >= required_points)
    return {
        "available": True,
        "passes": passes,
        "reason": None if passes else "FAILED_BREAKOUT_INSUFFICIENT_OPPOSITE_EDGE_ROOM",
        "low": low,
        "high": high,
        "room_points": room_points,
        "room_atr": room_atr,
        "room_pct": room_pct,
        "required_points": required_points,
        "min_room_atr": FAILURE_MIN_OPPOSITE_EDGE_ROOM_ATR,
        "min_room_pct": FAILURE_MIN_OPPOSITE_EDGE_ROOM_PCT,
    }


def _pnl(side: str, entry_price: float, exit_price: float) -> Tuple[float, float]:
    points = exit_price - entry_price if side == BUY else entry_price - exit_price
    pct = (points / entry_price) * 100.0 if entry_price else 0.0
    return float(points), float(pct)


# =============================================================================
# DB loading
# =============================================================================
def _load_snapshots() -> Dict[str, List[Dict[str, Any]]]:
    day_start = datetime.fromisoformat(TEST_DATE).replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    wanted_symbols = {upper(x) for x in SYMBOL_FILTER if str(x).strip()}
    wanted_types = {upper(x) for x in SYMBOL_TYPE_FILTER if str(x).strip()}

    with get_trades_db() as db:
        query = (
            db.query(SnapshotORM, SymbolORM.type)
            .join(SymbolORM, SymbolORM.symbol == SnapshotORM.symbol)
            .filter(SnapshotORM.snapshot_time >= day_start)
            .filter(SnapshotORM.snapshot_time < day_end)
            .order_by(SnapshotORM.symbol.asc(), SnapshotORM.snapshot_time.asc())
        )
        if wanted_types:
            query = query.filter(SymbolORM.type.in_(sorted(wanted_types)))
        if wanted_symbols:
            query = query.filter(SnapshotORM.symbol.in_(sorted(wanted_symbols)))
        rows = query.all()

    by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    seen: set[Tuple[str, datetime]] = set()
    for orm_row, _symbol_type in rows:
        symbol = upper(orm_row.symbol)
        key = (symbol, orm_row.snapshot_time.replace(tzinfo=None))
        if key in seen:
            continue
        seen.add(key)
        payload = _normalize_snapshot_payload(orm_row)
        by_symbol[symbol].append(payload)

    symbols = sorted(by_symbol)
    if MAX_SYMBOLS is not None:
        symbols = symbols[: max(0, int(MAX_SYMBOLS))]
    return {symbol: by_symbol[symbol] for symbol in symbols}


# =============================================================================
# Candidate evaluation
# =============================================================================

def _early_displacement_context(
    snapshot: Dict[str, Any],
    side: str,
    level: float,
) -> Dict[str, Any]:
    atr = _snapshot_atr(snapshot)
    open_price = _bar_open(snapshot)
    high = _snapshot_high(snapshot)
    low = _snapshot_low(snapshot)
    close = _snapshot_close(snapshot)
    candle_range = max(high - low, 1e-9)
    body_fraction = abs(close - open_price) / candle_range
    move_atr = abs(close - open_price) / atr
    close_position = _close_position(low, high, close)
    bar_rvol = _optional_numeric(snapshot, "volume.bar_rvol")
    close_outside_atr = ((close - level) / atr) if side == BUY else ((level - close) / atr)
    directional = close > open_price if side == BUY else close < open_price
    location_ok = (
        close_position >= EARLY_STRONG_BUY_CLOSE_POSITION
        if side == BUY
        else close_position <= EARLY_STRONG_SELL_CLOSE_POSITION
    )
    qualified = bool(
        directional
        and close_outside_atr >= EARLY_STRONG_CLOSE_OUTSIDE_ATR
        and move_atr >= EARLY_STRONG_DISPLACEMENT_MOVE_ATR
        and body_fraction >= EARLY_STRONG_BODY_FRACTION
        and bar_rvol is not None
        and bar_rvol >= EARLY_STRONG_BAR_RVOL
        and location_ok
    )
    return {
        "qualified": qualified,
        "move_atr": move_atr,
        "body_fraction": body_fraction,
        "bar_rvol": bar_rvol,
        "close_position": close_position,
        "close_outside_atr": close_outside_atr,
        "close": close,
    }


def _early_range_quality_context(
    episode: BoundaryEpisode,
    ts: datetime,
) -> Dict[str, Any]:
    quality = _as_float(episode.frozen_range.get("quality"))
    width_atr = _as_float(episode.frozen_range.get("width_atr"))
    bars = _as_int(episode.frozen_range.get("bars"), 0)
    established = (
        _parse_dt(episode.frozen_range.get("established_at"))
        or _parse_dt(episode.frozen_range.get("end_time"))
    )
    age_minutes = None
    if established is not None:
        age_minutes = max(0.0, (ts - established).total_seconds() / 60.0)
    reasons: List[str] = []
    if quality is not None and quality < EARLY_MIN_RANGE_QUALITY:
        reasons.append("EARLY_BREAKOUT_RANGE_QUALITY_TOO_LOW")
    if bars > 0 and bars < EARLY_MIN_RANGE_BARS:
        reasons.append("EARLY_BREAKOUT_RANGE_BARS_TOO_FEW")
    if age_minutes is None or age_minutes < EARLY_MIN_RANGE_AGE_MINUTES:
        reasons.append("EARLY_BREAKOUT_RANGE_TOO_NEW")
    if not bool(episode.frozen_range.get("breakout_eligible", False)):
        reasons.append("EARLY_BREAKOUT_RANGE_NOT_BREAKOUT_ELIGIBLE")
    return {
        "passes": not reasons,
        "reasons": reasons,
        "quality": quality,
        "width_atr": width_atr,
        "bars": bars,
        "age_minutes": age_minutes,
    }


def _contextual_room_levels(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return ORB/PD references as room context, never as episode owners."""
    anchors = _optional_path(snapshot, "structure.anchors") or {}
    levels = _optional_path(snapshot, "levels") or {}
    opening = levels.get("opening_range") if isinstance(levels, dict) else {}
    prev_day = levels.get("prev_day") if isinstance(levels, dict) else {}
    orb_ready = bool(
        (anchors.get("orb_ready") if isinstance(anchors, dict) else False)
        or (opening.get("ready") if isinstance(opening, dict) else False)
    )
    values = [
        ("ORB_HIGH_CONTEXT", "ORB_HIGH", (anchors.get("orb_high") if isinstance(anchors, dict) else None) or (opening.get("high") if isinstance(opening, dict) else None), BUY, orb_ready),
        ("ORB_LOW_CONTEXT", "ORB_LOW", (anchors.get("orb_low") if isinstance(anchors, dict) else None) or (opening.get("low") if isinstance(opening, dict) else None), SELL, orb_ready),
        ("PDH_CONTEXT", "PREVIOUS_DAY_HIGH", (anchors.get("pdh") if isinstance(anchors, dict) else None) or (prev_day.get("high") if isinstance(prev_day, dict) else None), BUY, True),
        ("PDL_CONTEXT", "PREVIOUS_DAY_LOW", (anchors.get("pdl") if isinstance(anchors, dict) else None) or (prev_day.get("low") if isinstance(prev_day, dict) else None), SELL, True),
    ]
    out: List[Dict[str, Any]] = []
    for reference_id, level_type, price, side, enabled in values:
        px = _as_float(price)
        if enabled and px is not None:
            out.append({
                "reference_id": reference_id,
                "level_type": level_type,
                "price": px,
                "side": side,
                "source": "CONTEXT_ONLY",
                "rank": 90,
            })
    return out


def _early_breakout_candidate(
    *,
    snapshot: Dict[str, Any],
    episode: BoundaryEpisode,
    current_accepted: Dict[str, Any],
    levels: Sequence[Dict[str, Any]],
    blocker_counts: Counter,
    blocker_seen: set[Tuple[str, str, str]],
) -> Optional[ReadyCandidate]:
    reasons: List[str] = []
    ts = _snapshot_dt(snapshot)
    side = episode.breakout_side
    close = _snapshot_close(snapshot)
    atr = _snapshot_atr(snapshot)
    level = episode.level_price

    if not _is_dynamic_level(episode.observation):
        reasons.append("EARLY_BREAKOUT_NON_DYNAMIC_BOUNDARY")
    stale_reason = _stale_fixed_level_reason(episode.observation, current_accepted)
    if stale_reason:
        reasons.append(stale_reason)

    range_quality = _early_range_quality_context(episode, ts)
    reasons.extend(range_quality["reasons"])

    # Early entry is a one-time opportunity. Do not let a previously observed
    # hold or two-close sequence become a delayed signal many candles later.
    strong_path = bool(
        episode.strong_displacement_time is not None
        and (episode.early_hold_time == ts or episode.early_retest_time == ts)
    )
    two_close_path = bool(
        episode.consecutive_early_outside_closes == EARLY_TWO_CLOSE_REQUIRED
    )
    if not strong_path and not two_close_path:
        reasons.append("EARLY_BREAKOUT_INITIATION_SEQUENCE_NOT_CONFIRMED")

    if EARLY_BLOCK_IF_FULLY_CONFIRMED and episode.confirmed_policy_ready_time is not None:
        reasons.append("EARLY_BREAKOUT_ALREADY_FULLY_CONFIRMED")

    pa = _price_action_confirmation(snapshot, side)
    if not bool(pa.get("confirmed")):
        reasons.append("EARLY_BREAKOUT_PRICE_ACTION_NOT_CONFIRMED")

    distance_atr = abs(close - level) / atr
    max_distance = (
        EARLY_STRONG_MAX_ENTRY_DISTANCE_ATR
        if strong_path
        else EARLY_MAX_ENTRY_DISTANCE_ATR
    )
    if distance_atr > max_distance:
        reasons.append(f"EARLY_BREAKOUT_ENTRY_DISTANCE_GT_{max_distance:.2f}_ATR")

    room_levels = [*levels, *_contextual_room_levels(snapshot)]
    structural_room = _structural_room_context(snapshot, episode, room_levels, side, close, atr)
    if not bool(structural_room.get("passes")):
        reasons.append("EARLY_BREAKOUT_INSUFFICIENT_STRUCTURAL_ROOM")

    terminal = _terminal_extension_context(snapshot, side)
    if bool(terminal.get("blocked")):
        reasons.append(f"EARLY_BREAKOUT_{side}_TERMINAL_EXTENSION_FIRST_MOVE_CONSUMED")

    bar_rvol = _optional_numeric(snapshot, "volume.bar_rvol")
    bar_rvol_band = upper(_optional_path(snapshot, "volume.bar_rvol_band") or "NA")
    if bar_rvol is not None and bar_rvol < EARLY_MIN_BAR_RVOL:
        reasons.append("EARLY_BREAKOUT_BAR_RVOL_TOO_LOW")
    if bar_rvol_band in set(EARLY_WEAK_PARTICIPATION_BANDS):
        reasons.append(f"EARLY_BREAKOUT_WEAK_PARTICIPATION_{bar_rvol_band}")

    # HMA is contextual only. It must never delay an early price-action entry.
    hma = _hma_alignment(snapshot, side)
    displacement = _early_displacement_context(snapshot, side, level)
    entry_path = (
        "STRONG_DISPLACEMENT_RETEST"
        if strong_path and episode.early_retest_time is not None
        else "STRONG_DISPLACEMENT_HOLD"
        if strong_path
        else "TWO_MEANINGFUL_CLOSES"
    )

    episode.acceptance_strength = min(
        100.0,
        max(
            0.0,
            35.0
            + min(20.0, episode.consecutive_early_outside_closes * 8.0)
            + min(20.0, episode.max_close_outside_atr * 12.0)
            + float(pa.get("strength") or 0.0) * 0.20
            + (10.0 if strong_path else 0.0),
        ),
    )
    episode.state = "EARLY_BREAKOUT_BUILDING" if reasons else "EARLY_BREAKOUT_ENTRY_READY"
    if reasons:
        _record_blockers(
            counter=blocker_counts,
            seen=blocker_seen,
            episode_key=episode.episode_key,
            resolution=RESOLUTION_EARLY,
            reasons=reasons,
        )
        return None

    pa_strength = float(pa.get("strength") or 0.0)
    score = (
        64.0
        + (10.0 if strong_path else 5.0)
        + min(10.0, episode.max_close_outside_atr * 8.0)
        + min(10.0, pa_strength * 0.10)
        + (4.0 if bool(hma.get("aligned")) else 0.0)
        - min(12.0, distance_atr * 6.0)
    )
    return ReadyCandidate(
        symbol=episode.symbol,
        resolution=RESOLUTION_EARLY,
        side=side,
        signal_time=ts,
        entry_price=close,
        entry_atr=atr,
        episode_key=episode.episode_key,
        reference_id=episode.reference_id,
        level_type=episode.level_type,
        level_source=episode.level_source,
        level_price=level,
        rank=episode.rank,
        score=float(score),
        price_action_strength=pa_strength,
        entry_distance_atr=distance_atr,
        remaining_room_points=_as_float(structural_room.get("distance_points")),
        remaining_room_atr=_as_float(structural_room.get("distance_atr")),
        remaining_room_pct=_as_float(structural_room.get("distance_pct")),
        accepted_range_low=_as_float(episode.frozen_range.get("low")),
        accepted_range_high=_as_float(episode.frozen_range.get("high")),
        episode_state=episode.state,
        frozen_range_basis=str(episode.frozen_range.get("freeze_basis") or "UNKNOWN"),
        context={
            "entry_path": entry_path,
            "range_quality": range_quality,
            "hma": hma,
            "price_action": pa,
            "displacement": displacement,
            "structural_room": structural_room,
            "terminal_extension": terminal,
        },
    )


def _observe_confirmed_acceptance(
    *,
    snapshot: Dict[str, Any],
    episode: BoundaryEpisode,
    current_accepted: Dict[str, Any],
    levels: Sequence[Dict[str, Any]],
) -> None:
    if episode.confirmed_policy_ready_time is not None or episode.terminal:
        return
    old_state = episode.state
    dummy_counter: Counter = Counter()
    dummy_seen: set[Tuple[str, str, str]] = set()
    candidate = _acceptance_candidate(
        snapshot=snapshot,
        episode=episode,
        current_accepted=current_accepted,
        levels=levels,
        blocker_counts=dummy_counter,
        blocker_seen=dummy_seen,
    )
    episode.state = old_state
    if candidate is not None:
        episode.confirmed_policy_ready_time = _snapshot_dt(snapshot)
        episode.confirmed_policy_ready_price = _snapshot_close(snapshot)


def _acceptance_candidate(
    *,
    snapshot: Dict[str, Any],
    episode: BoundaryEpisode,
    current_accepted: Dict[str, Any],
    levels: Sequence[Dict[str, Any]],
    blocker_counts: Counter,
    blocker_seen: set[Tuple[str, str, str]],
) -> Optional[ReadyCandidate]:
    reasons: List[str] = []
    stale_reason = _stale_fixed_level_reason(episode.observation, current_accepted)
    if stale_reason:
        reasons.append(stale_reason)

    side = episode.breakout_side
    close = _snapshot_close(snapshot)
    atr = _snapshot_atr(snapshot)
    level = episode.level_price
    required_closes = _required_acceptance_closes(episode)
    if episode.consecutive_acceptance_closes < required_closes:
        reasons.append(f"ACCEPTED_BREAKOUT_BARS_OUTSIDE_LT_{required_closes}")
    if episode.current_offset_atr < _acceptance_buffer(episode.observation):
        reasons.append("ACCEPTED_BREAKOUT_CLOSE_NOT_OUTSIDE_ACCEPTANCE_BUFFER")

    pa = _price_action_confirmation(snapshot, side)
    if not bool(pa.get("confirmed")):
        reasons.append("ACCEPTED_BREAKOUT_PRICE_ACTION_NOT_CONFIRMED")

    strict = _strict_displacement_context(snapshot, side, level)
    max_distance = ACCEPTED_STRICT_MAX_ENTRY_DISTANCE_ATR if strict.get("qualified") else ACCEPTED_MAX_ENTRY_DISTANCE_ATR
    distance_atr = abs(close - level) / atr
    if distance_atr > max_distance:
        reasons.append(f"ACCEPTED_BREAKOUT_ENTRY_DISTANCE_GT_{max_distance:.2f}_ATR")

    dynamic_age = _dynamic_range_age_context(episode, _snapshot_dt(snapshot))
    if not bool(dynamic_age.get("passes")):
        reasons.append("ACCEPTED_BREAKOUT_DYNAMIC_RANGE_TOO_NEW")

    structural_room = _structural_room_context(snapshot, episode, levels, side, close, atr)
    if not bool(structural_room.get("passes")) and not bool(strict.get("qualified")):
        reasons.append("ACCEPTED_BREAKOUT_INSUFFICIENT_STRUCTURAL_ROOM")

    terminal = _terminal_extension_context(snapshot, side)
    if bool(terminal.get("blocked")):
        reasons.append(
            f"ACCEPTED_BREAKOUT_{side}_TERMINAL_EXTENSION_FIRST_MOVE_CONSUMED"
        )

    hma = _hma_alignment(snapshot, side)
    if not bool(hma.get("aligned")):
        reasons.append("ACCEPTED_BREAKOUT_HMA_NOT_ALIGNED")

    bar_rvol = _optional_numeric(snapshot, "volume.bar_rvol")
    bar_rvol_band = upper(_optional_path(snapshot, "volume.bar_rvol_band") or "NA")
    if bar_rvol is not None and bar_rvol < ACCEPTED_MIN_BAR_RVOL:
        reasons.append("ACCEPTED_BREAKOUT_BAR_RVOL_TOO_LOW")
    if bar_rvol_band in set(ACCEPTED_WEAK_PARTICIPATION_BANDS):
        reasons.append(f"ACCEPTED_BREAKOUT_WEAK_PARTICIPATION_{bar_rvol_band}")

    rsi = _optional_numeric(snapshot, "indicators.rsi.value")
    bb_pos = _optional_numeric(snapshot, "indicators.bollinger.position")
    if side == BUY and rsi is not None and bb_pos is not None and rsi >= ACCEPTED_BLOCK_BUY_RSI and bb_pos >= ACCEPTED_BLOCK_BUY_BB:
        reasons.append("ACCEPTED_BREAKOUT_BUY_SEVERE_UPPER_EXHAUSTION")
    if side == SELL and rsi is not None and bb_pos is not None and rsi <= ACCEPTED_BLOCK_SELL_RSI and bb_pos <= ACCEPTED_BLOCK_SELL_BB:
        reasons.append("ACCEPTED_BREAKOUT_SELL_SEVERE_LOWER_EXHAUSTION")

    pa_strength = float(pa.get("strength") or 0.0)
    episode.acceptance_strength = min(
        100.0,
        max(
            0.0,
            25.0
            + min(30.0, episode.consecutive_acceptance_closes * 10.0)
            + min(20.0, episode.max_close_outside_atr * 10.0)
            + pa_strength * 0.25,
        ),
    )
    episode.state = "ACCEPTANCE_BUILDING" if reasons else "ACCEPTED_ENTRY_READY"
    if reasons:
        _record_blockers(
            counter=blocker_counts,
            seen=blocker_seen,
            episode_key=episode.episode_key,
            resolution=RESOLUTION_ACCEPTED,
            reasons=reasons,
        )
        return None

    score = (
        58.0
        + min(18.0, episode.consecutive_acceptance_closes * 4.0)
        + min(14.0, pa_strength * 0.14)
        + (6.0 if episode.rank == 10 else 0.0)
        + (4.0 if strict.get("qualified") else 0.0)
        - min(12.0, distance_atr * 5.0)
    )
    return ReadyCandidate(
        symbol=episode.symbol,
        resolution=RESOLUTION_ACCEPTED,
        side=side,
        signal_time=_snapshot_dt(snapshot),
        entry_price=close,
        entry_atr=atr,
        episode_key=episode.episode_key,
        reference_id=episode.reference_id,
        level_type=episode.level_type,
        level_source=episode.level_source,
        level_price=level,
        rank=episode.rank,
        score=float(score),
        price_action_strength=pa_strength,
        entry_distance_atr=distance_atr,
        remaining_room_points=_as_float(structural_room.get("distance_points")),
        remaining_room_atr=_as_float(structural_room.get("distance_atr")),
        remaining_room_pct=_as_float(structural_room.get("distance_pct")),
        accepted_range_low=_as_float(episode.frozen_range.get("low")),
        accepted_range_high=_as_float(episode.frozen_range.get("high")),
        episode_state=episode.state,
        frozen_range_basis=str(episode.frozen_range.get("freeze_basis") or "UNKNOWN"),
        context={
            "required_acceptance_closes": required_closes,
            "strict_displacement": strict,
            "terminal_extension": terminal,
            "structural_room": structural_room,
            "dynamic_range_age": dynamic_age,
            "hma": hma,
            "price_action": pa,
        },
    )


def _failure_candidate_from_episode(
    *,
    snapshot: Dict[str, Any],
    episode: BoundaryEpisode,
    current_accepted: Dict[str, Any],
    blocker_counts: Counter,
    blocker_seen: set[Tuple[str, str, str]],
) -> Optional[ReadyCandidate]:
    ts = _snapshot_dt(snapshot)
    if episode.failed_time is None or episode.failure_expires_at is None or ts > episode.failure_expires_at:
        return None

    reasons: List[str] = []
    stale_reason = _stale_fixed_level_reason(episode.observation, current_accepted)
    if stale_reason:
        reasons.append(stale_reason)

    genuine_attempt = bool(
        episode.max_outside_excursion_atr >= BOUNDARY_GENUINE_OUTSIDE_EXCURSION_ATR
        and (
            episode.total_outside_closes >= 1
            or episode.max_outside_excursion_atr >= BOUNDARY_STRONG_WICK_EXCURSION_ATR
        )
    )
    if not genuine_attempt:
        reasons.append("FAILED_BREAKOUT_GENUINE_OUTSIDE_ATTEMPT_NOT_PROVEN")
    if episode.reentry_depth_atr < FAILURE_MEANINGFUL_REENTRY_ATR:
        reasons.append("FAILED_BREAKOUT_MEANINGFUL_REENTRY_NOT_PROVEN")
    if episode.consecutive_inside_closes < FAILURE_REQUIRED_INSIDE_CLOSES:
        reasons.append(f"FAILED_BREAKOUT_INSIDE_HOLD_LT_{FAILURE_REQUIRED_INSIDE_CLOSES}")
    if not episode.followthrough_confirmed:
        reasons.append("FAILED_BREAKOUT_DIRECTIONAL_FOLLOWTHROUGH_NOT_CONFIRMED")
    if episode.failure_reaccepted_outside:
        reasons.append("FAILED_BREAKOUT_REACCEPTED_OUTSIDE_FAILED_LEVEL")

    close = _snapshot_close(snapshot)
    atr = _snapshot_atr(snapshot)
    low = _as_float(episode.frozen_range.get("low"))
    high = _as_float(episode.frozen_range.get("high"))
    inside_range = bool(low is not None and high is not None and high > low and low <= close <= high)
    if not inside_range:
        reasons.append("FAILED_BREAKOUT_NOT_INSIDE_FROZEN_ACCEPTED_RANGE")

    side = episode.failure_side
    pa = _price_action_confirmation(snapshot, side)
    if not bool(pa.get("confirmed")):
        reasons.append("FAILED_BREAKOUT_PRICE_ACTION_NOT_CONFIRMED")

    entry_distance_atr = abs(close - episode.level_price) / atr
    if entry_distance_atr > FAILURE_MAX_ENTRY_DISTANCE_ATR:
        reasons.append(f"FAILED_BREAKOUT_ENTRY_DISTANCE_GT_{FAILURE_MAX_ENTRY_DISTANCE_ATR:.2f}_ATR")

    room = _room_to_failure_opposite_edge(side=side, close=close, atr=atr, accepted=episode.frozen_range)
    if not bool(room.get("passes")):
        reasons.append(str(room.get("reason") or "FAILED_BREAKOUT_INSUFFICIENT_OPPOSITE_EDGE_ROOM"))

    trend_guard = _fresh_trend_against_failure(snapshot, side)
    if bool(trend_guard.get("blocked")):
        reasons.append("FAILED_BREAKOUT_FRESH_ACCELERATING_TREND_AGAINST_ENTRY")

    rsi = _optional_numeric(snapshot, "indicators.rsi.value")
    bb_pos = _optional_numeric(snapshot, "indicators.bollinger.position")
    if rsi is None or bb_pos is None:
        reasons.append("FAILED_BREAKOUT_RSI_OR_BOLLINGER_MISSING")
    elif side == BUY:
        if bb_pos >= FAILURE_BLOCK_BUY_BB:
            reasons.append("FAILED_BREAKOUT_BUY_UPPER_BOLLINGER_STRETCH")
        if rsi >= FAILURE_BLOCK_BUY_RSI and bb_pos >= FAILURE_BLOCK_BUY_RSI_BB:
            reasons.append("FAILED_BREAKOUT_BUY_HIGH_RSI_UPPER_BOLLINGER")
    else:
        if bb_pos <= FAILURE_BLOCK_SELL_BB:
            reasons.append("FAILED_BREAKOUT_SELL_LOWER_BOLLINGER_STRETCH")
        if rsi <= FAILURE_BLOCK_SELL_RSI and bb_pos <= FAILURE_BLOCK_SELL_RSI_BB:
            reasons.append("FAILED_BREAKOUT_SELL_LOW_RSI_LOWER_BOLLINGER")

    pa_strength = float(pa.get("strength") or 0.0)
    episode.failure_strength = min(
        100.0,
        max(
            0.0,
            20.0
            + min(20.0, episode.max_outside_excursion_atr * 12.0)
            + min(20.0, episode.reentry_depth_atr * 20.0)
            + min(15.0, episode.consecutive_inside_closes * 5.0)
            + min(15.0, episode.followthrough_strength * 0.15)
            + pa_strength * 0.10,
        ),
    )
    episode.state = "FAILURE_BUILDING" if reasons else "FAILED_ENTRY_READY"
    if reasons:
        _record_blockers(
            counter=blocker_counts,
            seen=blocker_seen,
            episode_key=episode.episode_key,
            resolution=RESOLUTION_FAILED,
            reasons=reasons,
        )
        return None

    age_minutes = max(0.0, (ts - episode.failed_time).total_seconds() / 60.0)
    score = (
        62.0
        + min(12.0, episode.reentry_depth_atr * 10.0)
        + min(12.0, episode.followthrough_strength * 0.12)
        + min(10.0, pa_strength * 0.10)
        + (5.0 if episode.rank == 10 else 0.0)
        - min(10.0, entry_distance_atr * 5.0)
        - min(5.0, age_minutes / 4.0)
    )
    return ReadyCandidate(
        symbol=episode.symbol,
        resolution=RESOLUTION_FAILED,
        side=side,
        signal_time=ts,
        entry_price=close,
        entry_atr=atr,
        episode_key=episode.episode_key,
        reference_id=episode.reference_id,
        level_type=episode.level_type,
        level_source=episode.level_source,
        level_price=episode.level_price,
        rank=episode.rank,
        score=float(score),
        price_action_strength=pa_strength,
        entry_distance_atr=entry_distance_atr,
        remaining_room_points=_as_float(room.get("room_points")),
        remaining_room_atr=_as_float(room.get("room_atr")),
        remaining_room_pct=_as_float(room.get("room_pct")),
        accepted_range_low=low,
        accepted_range_high=high,
        episode_state=episode.state,
        frozen_range_basis=str(episode.frozen_range.get("freeze_basis") or "UNKNOWN"),
        context={
            "genuine_attempt": genuine_attempt,
            "failure_event_time": episode.failed_time,
            "failure_watch_age_minutes": age_minutes,
            "failure_room": room,
            "price_action": pa,
            "trend_guard": trend_guard,
            "reentry_depth_atr": episode.reentry_depth_atr,
            "inside_closes": episode.consecutive_inside_closes,
            "followthrough_strength": episode.followthrough_strength,
        },
    )


def _update_followthrough(episode: BoundaryEpisode, snapshot: Dict[str, Any]) -> None:
    if episode.reclaim_snapshot_time is None or _snapshot_dt(snapshot) <= episode.reclaim_snapshot_time:
        return
    if episode.reclaim_close is None or episode.reclaim_high is None or episode.reclaim_low is None:
        return
    atr = _snapshot_atr(snapshot)
    close = _snapshot_close(snapshot)
    high = _snapshot_high(snapshot)
    low = _snapshot_low(snapshot)
    previous_close = episode.last_close
    previous_high = episode.last_high
    previous_low = episode.last_low
    close_pos = _close_position(low, high, close)
    buffer_points = FAILURE_FOLLOWTHROUGH_BUFFER_ATR * atr

    if episode.failure_side == BUY:
        break_reclaim = close >= episode.reclaim_high + buffer_points
        progressing = bool(
            previous_close is not None
            and previous_high is not None
            and close > previous_close
            and high >= previous_high
            and close_pos >= 0.55
        )
        favorable_points = close - episode.reclaim_close
    else:
        break_reclaim = close <= episode.reclaim_low - buffer_points
        progressing = bool(
            previous_close is not None
            and previous_low is not None
            and close < previous_close
            and low <= previous_low
            and close_pos <= 0.45
        )
        favorable_points = episode.reclaim_close - close

    if break_reclaim or progressing:
        episode.followthrough_confirmed = True
        episode.followthrough_time = _snapshot_dt(snapshot)
        episode.followthrough_strength = min(
            100.0,
            max(0.0, 55.0 + max(0.0, favorable_points / atr) * 25.0 + (10.0 if break_reclaim else 0.0)),
        )


def _update_boundary_episodes(
    *,
    symbol: str,
    snapshot: Dict[str, Any],
    snapshot_history: Sequence[Dict[str, Any]],
    levels: Sequence[Dict[str, Any]],
    episodes: Dict[str, BoundaryEpisode],
    current_accepted: Dict[str, Any],
    blocker_counts: Counter,
    blocker_seen: set[Tuple[str, str, str]],
) -> None:
    ts = _snapshot_dt(snapshot)
    atr = _snapshot_atr(snapshot)
    close = _snapshot_close(snapshot)
    high = _snapshot_high(snapshot)
    low = _snapshot_low(snapshot)
    open_price = _bar_open(snapshot)
    current_structural_keys: set[str] = set()

    for level_observation in levels:
        side = upper(level_observation.get("side") or "")
        level_price = _as_float(level_observation.get("price"))
        if side not in {BUY, SELL} or level_price is None:
            continue
        structural_key = _structural_key(symbol, level_observation)
        current_structural_keys.add(structural_key)
        episode = _latest_episode_for_structural_key(episodes, structural_key)
        offsets = _level_offsets(snapshot, side, level_price)
        close_offset = offsets["close_offset_atr"]
        extreme_offset = offsets["extreme_offset_atr"]
        attempt_now = bool(extreme_offset >= _attempt_buffer(level_observation) or close_offset > 0.0)

        if episode is None or (episode.terminal and _can_restart_terminal_episode(episode)):
            if not attempt_now:
                continue
            prior_episode = episode
            frozen = _freeze_range_for_observation(
                observation=level_observation,
                history=snapshot_history,
                current_snapshot=snapshot,
            )
            sequence = 1 if prior_episode is None else prior_episode.episode_sequence + 1
            key = _episode_key(structural_key, frozen, sequence)
            episode = BoundaryEpisode(
                episode_key=key,
                structural_key=structural_key,
                episode_sequence=sequence,
                symbol=symbol,
                breakout_side=side,
                failure_side=opposite_side(side),
                first_seen_time=ts,
                last_seen_time=ts,
                attempt_time=ts,
                accepted_time=None,
                failed_time=None,
                failure_expires_at=None,
                reference_id=str(level_observation.get("reference_id") or ""),
                level_type=str(level_observation.get("level_type") or ""),
                level_source=str(level_observation.get("source") or ""),
                level_price=float(level_price),
                rank=_as_int(level_observation.get("rank"), 999),
                frozen_range=frozen,
                observation=dict(level_observation),
                episode_reset_time=(prior_episode.episode_reset_time if prior_episode else None),
                episode_reset_reason=("RETURNED_MATERIALLY_INTO_FROZEN_VALUE" if prior_episode else None),
            )
            episodes[key] = episode
        elif episode.terminal:
            continue

        stale_reason = _stale_fixed_level_reason(level_observation, current_accepted)
        if stale_reason:
            episode.terminal = True
            episode.terminal_reason = stale_reason
            _record_blockers(
                counter=blocker_counts,
                seen=blocker_seen,
                episode_key=episode.episode_key,
                resolution="EPISODE",
                reasons=[stale_reason],
            )
            continue

        episode.last_seen_time = ts
        episode.observation = dict(level_observation)
        episode.current_offset_atr = close_offset
        episode.max_outside_excursion_atr = max(episode.max_outside_excursion_atr, extreme_offset)
        episode.max_close_outside_atr = max(episode.max_close_outside_atr, close_offset)

        if close_offset > 0.0:
            episode.total_outside_closes += 1
            episode.consecutive_outside_closes += 1
            episode.first_outside_close_time = episode.first_outside_close_time or ts
            episode.last_outside_time = ts
        else:
            episode.consecutive_outside_closes = 0

        if close_offset >= EARLY_MIN_CLOSE_OUTSIDE_ATR:
            episode.consecutive_early_outside_closes += 1
        else:
            episode.consecutive_early_outside_closes = 0

        displacement = _early_displacement_context(snapshot, side, level_price)
        if episode.strong_displacement_time is None and bool(displacement.get("qualified")):
            episode.strong_displacement_time = ts
            episode.strong_displacement_price = close
            episode.strong_displacement_move_atr = _as_float(displacement.get("move_atr"))
            episode.strong_displacement_body_fraction = _as_float(displacement.get("body_fraction"))
            episode.strong_displacement_bar_rvol = _as_float(displacement.get("bar_rvol"))
            episode.strong_displacement_close_position = _as_float(displacement.get("close_position"))
        elif episode.strong_displacement_time is not None and ts > episode.strong_displacement_time:
            age_minutes = (ts - episode.strong_displacement_time).total_seconds() / 60.0
            if age_minutes <= EARLY_HOLD_MAX_MINUTES:
                hold_outside = close_offset >= EARLY_HOLD_MIN_OUTSIDE_ATR
                if side == BUY:
                    retest_touch = low <= level_price + EARLY_RETEST_TOLERANCE_ATR * atr
                else:
                    retest_touch = high >= level_price - EARLY_RETEST_TOLERANCE_ATR * atr
                if hold_outside:
                    episode.early_hold_time = episode.early_hold_time or ts
                    if retest_touch:
                        episode.early_retest_time = episode.early_retest_time or ts

        if close_offset >= _acceptance_buffer(level_observation):
            episode.consecutive_acceptance_closes += 1
            required = _required_acceptance_closes(episode)
            if episode.accepted_time is None and episode.consecutive_acceptance_closes >= required:
                episode.accepted_time = ts
            if episode.failed_time is not None:
                episode.failure_reaccepted_outside = True
            episode.state = "ACCEPTANCE_BUILDING"
        else:
            episode.consecutive_acceptance_closes = 0

        genuine_attempt = bool(
            episode.max_outside_excursion_atr >= BOUNDARY_GENUINE_OUTSIDE_EXCURSION_ATR
            and (
                episode.total_outside_closes >= 1
                or episode.max_outside_excursion_atr >= BOUNDARY_STRONG_WICK_EXCURSION_ATR
            )
        )
        meaningful_reentry = bool(close_offset <= -FAILURE_MEANINGFUL_REENTRY_ATR and genuine_attempt)
        if meaningful_reentry:
            if episode.first_reentry_time is None:
                episode.first_reentry_time = ts
                episode.failed_time = ts
                episode.failure_expires_at = ts + timedelta(minutes=FAILURE_WATCH_VALID_MINUTES)
                episode.reclaim_snapshot_time = ts
                episode.reclaim_open = open_price
                episode.reclaim_high = high
                episode.reclaim_low = low
                episode.reclaim_close = close
                episode.reclaim_move_atr = abs(close - open_price) / atr
                episode.consecutive_inside_closes = 1
                episode.hold_after_reclaim_count = 0
                episode.failure_reaccepted_outside = False
            else:
                episode.consecutive_inside_closes += 1
                episode.hold_after_reclaim_count = max(0, episode.consecutive_inside_closes - 1)
            episode.reentry_depth_atr = max(episode.reentry_depth_atr, abs(close_offset))
            episode.state = "FAILURE_BUILDING"
            _update_followthrough(episode, snapshot)
        elif close_offset < 0.0:
            episode.consecutive_inside_closes = 0
            episode.hold_after_reclaim_count = 0
        elif episode.failed_time is not None and close_offset >= _acceptance_buffer(level_observation):
            episode.consecutive_inside_closes = 0
            episode.hold_after_reclaim_count = 0

        if episode.state == "UNRESOLVED" and attempt_now:
            episode.state = "OUTSIDE_ATTEMPT"

        episode.last_close = close
        episode.last_high = high
        episode.last_low = low

    current_range_id, current_range_version = _range_identity(current_accepted)
    for episode in list(episodes.values()):
        if episode.terminal:
            continue
        frozen_id, frozen_version = _range_identity(episode.frozen_range)
        if (
            episode.rank == 10
            and frozen_id
            and current_range_id
            and (frozen_id, frozen_version) != (current_range_id, current_range_version)
            and episode.structural_key not in current_structural_keys
        ):
            episode.terminal = True
            episode.terminal_reason = "SUPERSEDED_BY_NEW_DYNAMIC_RANGE"
            continue

        watch_live = bool(episode.failure_expires_at and ts <= episode.failure_expires_at)
        resolution_emitted = bool(episode.emitted_resolutions)
        watch_expired = bool(episode.failure_expires_at and ts > episode.failure_expires_at)
        reset_eligible = resolution_emitted or watch_expired
        if reset_eligible and not watch_live and _materially_back_inside_frozen_value(episode, snapshot):
            episode.reset_inside_closes += 1
            episode.reset_started_at = episode.reset_started_at or ts
            if episode.reset_inside_closes >= EPISODE_RESET_REQUIRED_CLOSES:
                episode.terminal = True
                episode.terminal_reason = "EPISODE_RESET_INTO_FROZEN_VALUE"
                episode.episode_reset_time = ts
                episode.episode_reset_reason = "RETURNED_MATERIALLY_INTO_FROZEN_VALUE"
                continue
        elif not watch_live:
            episode.reset_inside_closes = 0
            episode.reset_started_at = None

        idle_minutes = max(0.0, (ts - episode.last_seen_time).total_seconds() / 60.0)
        if idle_minutes > EPISODE_IDLE_EXPIRY_MINUTES and not watch_live:
            episode.state = "DORMANT_WAITING_FOR_RESET"


def _select_candidate(candidates: Sequence[ReadyCandidate]) -> Tuple[Optional[ReadyCandidate], Optional[str]]:
    if not candidates:
        return None, None

    by_side: Dict[str, List[ReadyCandidate]] = defaultdict(list)
    for candidate in candidates:
        by_side[candidate.side].append(candidate)

    top_by_side: Dict[str, ReadyCandidate] = {}
    for side, side_candidates in by_side.items():
        # Current dynamic boundary wins before score; then prefer higher score and
        # closer entry. This mirrors current level arbitration while retaining one
        # stock-level resolution choice.
        top_by_side[side] = sorted(
            side_candidates,
            key=lambda c: (
                c.rank,
                -c.score,
                float(c.entry_distance_atr or 999.0),
                c.signal_time,
            ),
        )[0]

    if len(top_by_side) == 1:
        return next(iter(top_by_side.values())), None

    buy = top_by_side.get(BUY)
    sell = top_by_side.get(SELL)
    if buy is None:
        return sell, None
    if sell is None:
        return buy, None

    gap = abs(buy.score - sell.score)
    if gap < SIDE_CONFLICT_MIN_SCORE_GAP:
        return None, (
            f"UNRESOLVED_SIDE_CONFLICT_BUY_{buy.score:.1f}_SELL_{sell.score:.1f}_"
            f"GAP_{gap:.1f}_LT_{SIDE_CONFLICT_MIN_SCORE_GAP:.1f}"
        )
    return (buy if buy.score > sell.score else sell), None


# =============================================================================
# Analytical signal-quality lifecycle
# =============================================================================
def _excursion_pct(
    *,
    side: str,
    entry_price: float,
    future_snapshots: Sequence[Dict[str, Any]],
) -> Tuple[Optional[float], Optional[float]]:
    if not future_snapshots or entry_price <= 0:
        return None, None
    highs = [_snapshot_high(row) for row in future_snapshots]
    lows = [_snapshot_low(row) for row in future_snapshots]
    if side == BUY:
        mfe_points = max(highs) - entry_price
        mae_points = min(lows) - entry_price
    else:
        mfe_points = entry_price - min(lows)
        mae_points = entry_price - max(highs)
    return (mfe_points / entry_price) * 100.0, (mae_points / entry_price) * 100.0



def _signed_component(value: float, mild: float, strong: float, mild_points: float, strong_points: float) -> float:
    """Return a symmetric directional score component."""
    if value >= strong:
        return strong_points
    if value >= mild:
        return mild_points
    if value <= -strong:
        return -strong_points
    if value <= -mild:
        return -mild_points
    return 0.0


def _hypothetical_stock_advisor(
    *,
    candidate: ReadyCandidate,
    episode: BoundaryEpisode,
    history: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Advise from the stock's own intraday movement without future leakage.

    The Advisor is deliberately separate from setup validity. It classifies the
    stock's regime using only snapshots available through the signal snapshot,
    then states whether the candidate is aligned, neutral, or countertrend.
    It never changes signal creation while ADVISOR_APPLY_AS_FILTER is False.
    """
    if not ENABLE_HYPOTHETICAL_STOCK_ADVISOR or not history:
        return {
            "enabled": False,
            "regime": "NOT_EVALUATED",
            "decision": "NOT_EVALUATED",
            "alignment": "NOT_EVALUATED",
            "confidence": 0.0,
            "score": 0.0,
            "day_return_pct": 0.0,
            "range_position": 0.5,
            "vwap_distance_atr": None,
            "move_15m_atr": None,
            "recent_move_atr": 0.0,
            "trend_efficiency": 0.0,
            "reason": "Hypothetical stock Advisor disabled",
        }

    current = history[-1]
    close = _snapshot_close(current)
    atr = max(_snapshot_atr(current), 1e-9)
    session_open = _bar_open(history[0])
    highs = [_snapshot_high(item) for item in history]
    lows = [_snapshot_low(item) for item in history]
    session_high = max(highs) if highs else close
    session_low = min(lows) if lows else close
    day_return_pct = ((close - session_open) / session_open) * 100.0 if session_open else 0.0
    range_position = _close_position(session_low, session_high, close)

    vwap_distance_atr = _optional_numeric(current, "indicators.vwap.distance_atr")
    move_15m_atr = _optional_numeric(current, "market_windows.15m.move_atr")

    recent = list(history[-max(2, ADVISOR_RECENT_BARS + 1):])
    recent_closes = [_snapshot_close(item) for item in recent]
    recent_move_atr = (
        (recent_closes[-1] - recent_closes[0]) / atr
        if len(recent_closes) >= 2
        else 0.0
    )

    efficiency_history = list(history[-max(2, ADVISOR_EFFICIENCY_BARS + 1):])
    efficiency_closes = [_snapshot_close(item) for item in efficiency_history]
    if len(efficiency_closes) >= 2:
        net_move = efficiency_closes[-1] - efficiency_closes[0]
        travelled = sum(abs(b - a) for a, b in zip(efficiency_closes, efficiency_closes[1:]))
        trend_efficiency = (net_move / travelled) if travelled > 1e-9 else 0.0
    else:
        trend_efficiency = 0.0

    score = 0.0
    score += _signed_component(day_return_pct, 0.30, 1.00, 12.0, 25.0)
    range_bias = (range_position - 0.5) * 2.0
    score += _signed_component(range_bias, 0.20, 0.50, 10.0, 20.0)
    if vwap_distance_atr is not None:
        score += _signed_component(vwap_distance_atr, 0.25, 0.75, 10.0, 20.0)
    if move_15m_atr is not None:
        score += _signed_component(move_15m_atr, 0.40, 1.00, 8.0, 15.0)
    score += _signed_component(recent_move_atr, 0.40, 1.00, 8.0, 12.0)
    score += _signed_component(trend_efficiency, 0.35, 0.60, 7.0, 10.0)
    score = max(-100.0, min(100.0, score))

    if score >= ADVISOR_STRONG_UPTREND_SCORE:
        regime = "STRONG_UPTREND"
    elif score >= ADVISOR_UPTREND_SCORE:
        regime = "UPTREND"
    elif score <= ADVISOR_STRONG_DOWNTREND_SCORE:
        regime = "STRONG_DOWNTREND"
    elif score <= ADVISOR_DOWNTREND_SCORE:
        regime = "DOWNTREND"
    else:
        regime = "BALANCED"

    regime_side = BUY if score >= ADVISOR_UPTREND_SCORE else SELL if score <= ADVISOR_DOWNTREND_SCORE else None
    if regime_side is None:
        alignment = "NEUTRAL"
        decision = "ALLOW"
        decision_reason = "Stock movement is balanced; setup decides"
    elif candidate.side == regime_side:
        alignment = "ALIGNED"
        decision = "ALLOW"
        decision_reason = f"{candidate.side} aligns with {regime}"
    else:
        alignment = "COUNTERTREND"
        exceptional_failure = bool(
            candidate.resolution == RESOLUTION_FAILED
            and candidate.score >= ADVISOR_COUNTERTREND_EXCEPTION_SCORE
            and episode.followthrough_strength >= ADVISOR_COUNTERTREND_EXCEPTION_FOLLOWTHROUGH
            and episode.reentry_depth_atr >= ADVISOR_COUNTERTREND_EXCEPTION_REENTRY_ATR
        )
        if exceptional_failure:
            decision = "COUNTERTREND_EXCEPTION"
            decision_reason = "Countertrend FAILED resolution has exceptional reclaim and follow-through evidence"
        else:
            decision = "DEFER"
            decision_reason = f"{candidate.side} opposes {regime}"

    confidence = min(100.0, abs(score))
    reason_parts = [
        decision_reason,
        f"score={score:.1f}",
        f"day={day_return_pct:+.2f}%",
        f"range_pos={range_position:.2f}",
        f"recent={recent_move_atr:+.2f}ATR",
        f"efficiency={trend_efficiency:+.2f}",
    ]
    if vwap_distance_atr is not None:
        reason_parts.append(f"vwap={vwap_distance_atr:+.2f}ATR")
    if move_15m_atr is not None:
        reason_parts.append(f"move15={move_15m_atr:+.2f}ATR")

    return {
        "enabled": True,
        "regime": regime,
        "decision": decision,
        "alignment": alignment,
        "confidence": confidence,
        "score": score,
        "day_return_pct": day_return_pct,
        "range_position": range_position,
        "vwap_distance_atr": vwap_distance_atr,
        "move_15m_atr": move_15m_atr,
        "recent_move_atr": recent_move_atr,
        "trend_efficiency": trend_efficiency,
        "reason": "; ".join(reason_parts),
    }


def _build_analytical_signal(
    *,
    candidate: ReadyCandidate,
    episode: BoundaryEpisode,
    snapshots: Sequence[Dict[str, Any]],
    entry_index: int,
) -> AnalyticalSignal:
    subsequent = list(snapshots[entry_index + 1 :])
    if subsequent:
        exit_offset = min(COMPARISON_EXIT_AFTER_BARS, len(subsequent)) - 1
        exit_snapshot = subsequent[exit_offset]
        exit_reason = (
            f"FIXED_{COMPARISON_EXIT_AFTER_BARS}_BAR_COMPARISON"
            if len(subsequent) >= COMPARISON_EXIT_AFTER_BARS
            else "END_OF_DAY_BEFORE_COMPARISON_HORIZON"
        )
    else:
        exit_snapshot = snapshots[entry_index]
        exit_reason = "END_OF_DAY_AT_ENTRY"

    exit_price = _snapshot_close(exit_snapshot)
    pnl_points, pnl_pct = _pnl(candidate.side, candidate.entry_price, exit_price)
    eod_snapshot = snapshots[-1]
    eod_points, eod_pct = _pnl(candidate.side, candidate.entry_price, _snapshot_close(eod_snapshot))

    metrics: Dict[int, Tuple[Optional[float], Optional[float]]] = {}
    for horizon in MFE_MAE_HORIZONS:
        metrics[horizon] = _excursion_pct(
            side=candidate.side,
            entry_price=candidate.entry_price,
            future_snapshots=subsequent[:horizon],
        )
    full_mfe, full_mae = _excursion_pct(
        side=candidate.side,
        entry_price=candidate.entry_price,
        future_snapshots=subsequent,
    )
    advisor = _hypothetical_stock_advisor(
        candidate=candidate,
        episode=episode,
        history=snapshots[: entry_index + 1],
    )

    return AnalyticalSignal(
        symbol=candidate.symbol,
        resolution=candidate.resolution,
        side=candidate.side,
        signal_time=candidate.signal_time,
        entry_time=candidate.signal_time,
        entry_price=candidate.entry_price,
        exit_time=_snapshot_dt(exit_snapshot),
        exit_price=exit_price,
        exit_reason=exit_reason,
        pnl_points=pnl_points,
        pnl_pct=pnl_pct,
        eod_pnl_points=eod_points,
        eod_pnl_pct=eod_pct,
        mfe_3bar_pct=metrics.get(3, (None, None))[0],
        mae_3bar_pct=metrics.get(3, (None, None))[1],
        mfe_5bar_pct=metrics.get(5, (None, None))[0],
        mae_5bar_pct=metrics.get(5, (None, None))[1],
        mfe_9bar_pct=metrics.get(9, (None, None))[0],
        mae_9bar_pct=metrics.get(9, (None, None))[1],
        full_mfe_pct=full_mfe,
        full_mae_pct=full_mae,
        structural_key=episode.structural_key,
        episode_sequence=episode.episode_sequence,
        episode_reset_time=episode.episode_reset_time,
        episode_reset_reason=episode.episode_reset_reason,
        episode_key=candidate.episode_key,
        episode_state=candidate.episode_state,
        reference_id=candidate.reference_id,
        level_type=candidate.level_type,
        level_source=candidate.level_source,
        reference_price=candidate.level_price,
        entry_atr=candidate.entry_atr,
        accepted_range_low=candidate.accepted_range_low,
        accepted_range_high=candidate.accepted_range_high,
        frozen_range_id=episode.frozen_range.get("range_id"),
        frozen_range_version=_as_int(episode.frozen_range.get("version"), 0),
        frozen_range_basis=candidate.frozen_range_basis,
        acceptance_strength=episode.acceptance_strength,
        failure_strength=episode.failure_strength,
        policy_version=POLICY_VERSION,
        entry_path=str(candidate.context.get("entry_path") or "UNKNOWN"),
        range_quality=_as_float((candidate.context.get("range_quality") or {}).get("quality")),
        range_width_atr=_as_float((candidate.context.get("range_quality") or {}).get("width_atr")),
        range_bars=_as_int((candidate.context.get("range_quality") or {}).get("bars"), 0),
        range_age_minutes=_as_float((candidate.context.get("range_quality") or {}).get("age_minutes")),
        hma_aligned_at_entry=bool((candidate.context.get("hma") or {}).get("aligned")),
        strong_displacement_time=episode.strong_displacement_time,
        early_hold_time=episode.early_hold_time,
        early_retest_time=episode.early_retest_time,
        structural_acceptance_time=None,
        confirmed_acceptance_time=None,
        confirmed_acceptance_price=None,
        lead_to_structural_acceptance_minutes=None,
        lead_to_confirmed_acceptance_minutes=None,
        eventually_structurally_accepted=False,
        eventually_fully_confirmed=False,
        reabsorbed_before_full_confirmation=False,
        post_entry_reentry_time=None,
        final_episode_state=episode.state,
        final_terminal_reason=None,
        max_outside_excursion_atr=episode.max_outside_excursion_atr,
        total_outside_closes=episode.total_outside_closes,
        consecutive_acceptance_closes=episode.consecutive_acceptance_closes,
        first_reentry_time=episode.first_reentry_time,
        reentry_depth_atr=episode.reentry_depth_atr,
        consecutive_inside_closes=episode.consecutive_inside_closes,
        hold_after_reclaim_count=episode.hold_after_reclaim_count,
        followthrough_confirmed=episode.followthrough_confirmed,
        followthrough_time=episode.followthrough_time,
        followthrough_strength=episode.followthrough_strength,
        advisor_enabled=bool(advisor["enabled"]),
        advisor_regime=str(advisor["regime"]),
        advisor_decision=str(advisor["decision"]),
        advisor_alignment=str(advisor["alignment"]),
        advisor_confidence=float(advisor["confidence"]),
        advisor_score=float(advisor["score"]),
        advisor_day_return_pct=float(advisor["day_return_pct"]),
        advisor_range_position=float(advisor["range_position"]),
        advisor_vwap_distance_atr=_as_float(advisor["vwap_distance_atr"]),
        advisor_move_15m_atr=_as_float(advisor["move_15m_atr"]),
        advisor_recent_move_atr=float(advisor["recent_move_atr"]),
        advisor_trend_efficiency=float(advisor["trend_efficiency"]),
        advisor_reason=str(advisor["reason"]),
    )


# =============================================================================
# Main symbol evaluation
# =============================================================================
def _evaluate_symbol(
    symbol: str,
    snapshots: Sequence[Dict[str, Any]],
    blocker_counts: Counter,
    conflict_counts: Counter,
) -> List[AnalyticalSignal]:
    episodes: Dict[str, BoundaryEpisode] = {}
    consumed_resolution_keys: set[Tuple[str, str, str]] = set()
    blocker_seen: set[Tuple[str, str, str]] = set()
    signals: List[AnalyticalSignal] = []

    for index, snapshot in enumerate(snapshots):
        ts = _snapshot_dt(snapshot)
        levels = _structural_level_candidates(snapshot)
        current_accepted = _accepted_range_context(snapshot)

        _update_boundary_episodes(
            symbol=symbol,
            snapshot=snapshot,
            snapshot_history=snapshots[: index + 1],
            levels=levels,
            episodes=episodes,
            current_accepted=current_accepted,
            blocker_counts=blocker_counts,
            blocker_seen=blocker_seen,
        )

        # Observe the current fully confirmed acceptance policy independently.
        # This never creates a report signal; it only provides the later
        # confirmation time used to compare early versus conservative entry.
        for episode in episodes.values():
            if episode.terminal or episode.last_seen_time != ts:
                continue
            _observe_confirmed_acceptance(
                snapshot=snapshot,
                episode=episode,
                current_accepted=current_accepted,
                levels=levels,
            )

        ready: List[ReadyCandidate] = []
        if _within_entry_window(ts):
            for episode in episodes.values():
                if episode.terminal or episode.last_seen_time != ts:
                    continue

                early_key = (episode.episode_key, RESOLUTION_EARLY, episode.breakout_side)
                if early_key not in consumed_resolution_keys:
                    candidate = _early_breakout_candidate(
                        snapshot=snapshot,
                        episode=episode,
                        current_accepted=current_accepted,
                        levels=levels,
                        blocker_counts=blocker_counts,
                        blocker_seen=blocker_seen,
                    )
                    if candidate is not None:
                        ready.append(candidate)

                # Failure evidence continues to evolve inside the episode, but
                # this report intentionally does not emit FAILED trades.
                if GENERATE_FAILED_SIGNALS:
                    failed_key = (episode.episode_key, RESOLUTION_FAILED, episode.failure_side)
                    if failed_key not in consumed_resolution_keys:
                        candidate = _failure_candidate_from_episode(
                            snapshot=snapshot,
                            episode=episode,
                            current_accepted=current_accepted,
                            blocker_counts=blocker_counts,
                            blocker_seen=blocker_seen,
                        )
                        if candidate is not None:
                            ready.append(candidate)

        selected, conflict_reason = _select_candidate(ready)
        if conflict_reason:
            conflict_counts.update([conflict_reason])
        if selected is None:
            continue

        resolution_key = (selected.episode_key, selected.resolution, selected.side)
        if resolution_key in consumed_resolution_keys:
            raise AssertionError(f"Consumed candidate reached arbitration: {resolution_key}")
        episode = episodes[selected.episode_key]
        signals.append(
            _build_analytical_signal(
                candidate=selected,
                episode=episode,
                snapshots=snapshots,
                entry_index=index,
            )
        )
        consumed_resolution_keys.add(resolution_key)
        episode.emitted_resolutions.add((selected.resolution, selected.side))
        episode.state = "EARLY_BREAKOUT_SIGNAL_EMITTED"

    # Finalize post-entry lifecycle outcomes after the full day has been seen.
    for row in signals:
        episode = episodes.get(row.episode_key)
        if episode is None:
            continue
        row.structural_acceptance_time = episode.accepted_time
        row.confirmed_acceptance_time = episode.confirmed_policy_ready_time
        row.confirmed_acceptance_price = episode.confirmed_policy_ready_price
        row.eventually_structurally_accepted = bool(
            episode.accepted_time is not None and episode.accepted_time >= row.entry_time
        )
        row.eventually_fully_confirmed = bool(
            episode.confirmed_policy_ready_time is not None
            and episode.confirmed_policy_ready_time >= row.entry_time
        )
        if row.eventually_structurally_accepted and episode.accepted_time is not None:
            row.lead_to_structural_acceptance_minutes = max(
                0.0, (episode.accepted_time - row.entry_time).total_seconds() / 60.0
            )
        if row.eventually_fully_confirmed and episode.confirmed_policy_ready_time is not None:
            row.lead_to_confirmed_acceptance_minutes = max(
                0.0,
                (episode.confirmed_policy_ready_time - row.entry_time).total_seconds() / 60.0,
            )
        post_entry_reentry = (
            episode.first_reentry_time
            if episode.first_reentry_time is not None
            and episode.first_reentry_time > row.entry_time
            else None
        )
        row.post_entry_reentry_time = post_entry_reentry
        row.reabsorbed_before_full_confirmation = bool(
            post_entry_reentry is not None
            and (
                episode.confirmed_policy_ready_time is None
                or post_entry_reentry < episode.confirmed_policy_ready_time
            )
        )
        row.final_episode_state = episode.state
        row.final_terminal_reason = episode.terminal_reason

    return signals


# =============================================================================
# Reporting
# =============================================================================
def _write_csv(rows: Sequence[AnalyticalSignal]) -> Path:
    path = Path(OUTPUT_CSV_PATH)
    fieldnames = [
        "symbol",
        "resolution",
        "side",
        "signal_time",
        "entry_time",
        "entry_price",
        "exit_time",
        "exit_price",
        "exit_reason",
        "pnl_points",
        "pnl_pct",
        "eod_pnl_points",
        "eod_pnl_pct",
        "mfe_3bar_pct",
        "mae_3bar_pct",
        "mfe_5bar_pct",
        "mae_5bar_pct",
        "mfe_9bar_pct",
        "mae_9bar_pct",
        "full_mfe_pct",
        "full_mae_pct",
        "structural_key",
        "episode_sequence",
        "episode_reset_time",
        "episode_reset_reason",
        "episode_state",
        "reference_type",
        "reference_source",
        "reference_price",
        "entry_atr",
        "accepted_range_low",
        "accepted_range_high",
        "frozen_range_id",
        "frozen_range_version",
        "frozen_range_basis",
        "acceptance_strength",
        "failure_strength",
        "policy_version",
        "entry_path",
        "range_quality",
        "range_width_atr",
        "range_bars",
        "range_age_minutes",
        "hma_aligned_at_entry",
        "strong_displacement_time",
        "early_hold_time",
        "early_retest_time",
        "structural_acceptance_time",
        "confirmed_acceptance_time",
        "confirmed_acceptance_price",
        "lead_to_structural_acceptance_minutes",
        "lead_to_confirmed_acceptance_minutes",
        "eventually_structurally_accepted",
        "eventually_fully_confirmed",
        "reabsorbed_before_full_confirmation",
        "post_entry_reentry_time",
        "final_episode_state",
        "final_terminal_reason",
        "max_outside_excursion_atr",
        "total_outside_closes",
        "consecutive_acceptance_closes",
        "first_reentry_time",
        "reentry_depth_atr",
        "consecutive_inside_closes",
        "hold_after_reclaim_count",
        "followthrough_confirmed",
        "followthrough_time",
        "followthrough_strength",
        "advisor_enabled",
        "advisor_regime",
        "advisor_decision",
        "advisor_alignment",
        "advisor_confidence",
        "advisor_score",
        "advisor_day_return_pct",
        "advisor_range_position",
        "advisor_vwap_distance_atr",
        "advisor_move_15m_atr",
        "advisor_recent_move_atr",
        "advisor_trend_efficiency",
        "advisor_reason",
        "episode_key",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "symbol": row.symbol,
                "resolution": row.resolution,
                "side": row.side,
                "signal_time": row.signal_time.isoformat(sep=" "),
                "entry_time": row.entry_time.isoformat(sep=" "),
                "entry_price": round(row.entry_price, 6),
                "exit_time": row.exit_time.isoformat(sep=" "),
                "exit_price": round(row.exit_price, 6),
                "exit_reason": row.exit_reason,
                "pnl_points": round(row.pnl_points, 6),
                "pnl_pct": round(row.pnl_pct, 6),
                "eod_pnl_points": round(row.eod_pnl_points, 6),
                "eod_pnl_pct": round(row.eod_pnl_pct, 6),
                "mfe_3bar_pct": "" if row.mfe_3bar_pct is None else round(row.mfe_3bar_pct, 6),
                "mae_3bar_pct": "" if row.mae_3bar_pct is None else round(row.mae_3bar_pct, 6),
                "mfe_5bar_pct": "" if row.mfe_5bar_pct is None else round(row.mfe_5bar_pct, 6),
                "mae_5bar_pct": "" if row.mae_5bar_pct is None else round(row.mae_5bar_pct, 6),
                "mfe_9bar_pct": "" if row.mfe_9bar_pct is None else round(row.mfe_9bar_pct, 6),
                "mae_9bar_pct": "" if row.mae_9bar_pct is None else round(row.mae_9bar_pct, 6),
                "full_mfe_pct": "" if row.full_mfe_pct is None else round(row.full_mfe_pct, 6),
                "full_mae_pct": "" if row.full_mae_pct is None else round(row.full_mae_pct, 6),
                "structural_key": row.structural_key,
                "episode_sequence": row.episode_sequence,
                "episode_reset_time": (
                    "" if row.episode_reset_time is None else row.episode_reset_time.isoformat(sep=" ")
                ),
                "episode_reset_reason": row.episode_reset_reason or "",
                "episode_state": row.episode_state,
                "reference_type": row.level_type,
                "reference_source": row.level_source,
                "reference_price": round(row.reference_price, 6),
                "entry_atr": round(row.entry_atr, 6),
                "accepted_range_low": "" if row.accepted_range_low is None else round(row.accepted_range_low, 6),
                "accepted_range_high": "" if row.accepted_range_high is None else round(row.accepted_range_high, 6),
                "frozen_range_id": row.frozen_range_id or "",
                "frozen_range_version": row.frozen_range_version,
                "frozen_range_basis": row.frozen_range_basis,
                "acceptance_strength": round(row.acceptance_strength, 4),
                "failure_strength": round(row.failure_strength, 4),
                "policy_version": row.policy_version,
                "entry_path": row.entry_path,
                "range_quality": "" if row.range_quality is None else round(row.range_quality, 6),
                "range_width_atr": "" if row.range_width_atr is None else round(row.range_width_atr, 6),
                "range_bars": row.range_bars,
                "range_age_minutes": "" if row.range_age_minutes is None else round(row.range_age_minutes, 6),
                "hma_aligned_at_entry": row.hma_aligned_at_entry,
                "strong_displacement_time": "" if row.strong_displacement_time is None else row.strong_displacement_time.isoformat(sep=" "),
                "early_hold_time": "" if row.early_hold_time is None else row.early_hold_time.isoformat(sep=" "),
                "early_retest_time": "" if row.early_retest_time is None else row.early_retest_time.isoformat(sep=" "),
                "structural_acceptance_time": "" if row.structural_acceptance_time is None else row.structural_acceptance_time.isoformat(sep=" "),
                "confirmed_acceptance_time": "" if row.confirmed_acceptance_time is None else row.confirmed_acceptance_time.isoformat(sep=" "),
                "confirmed_acceptance_price": "" if row.confirmed_acceptance_price is None else round(row.confirmed_acceptance_price, 6),
                "lead_to_structural_acceptance_minutes": "" if row.lead_to_structural_acceptance_minutes is None else round(row.lead_to_structural_acceptance_minutes, 6),
                "lead_to_confirmed_acceptance_minutes": "" if row.lead_to_confirmed_acceptance_minutes is None else round(row.lead_to_confirmed_acceptance_minutes, 6),
                "eventually_structurally_accepted": row.eventually_structurally_accepted,
                "eventually_fully_confirmed": row.eventually_fully_confirmed,
                "reabsorbed_before_full_confirmation": row.reabsorbed_before_full_confirmation,
                "post_entry_reentry_time": "" if row.post_entry_reentry_time is None else row.post_entry_reentry_time.isoformat(sep=" "),
                "final_episode_state": row.final_episode_state,
                "final_terminal_reason": row.final_terminal_reason or "",
                "max_outside_excursion_atr": round(row.max_outside_excursion_atr, 6),
                "total_outside_closes": row.total_outside_closes,
                "consecutive_acceptance_closes": row.consecutive_acceptance_closes,
                "first_reentry_time": (
                    "" if row.first_reentry_time is None else row.first_reentry_time.isoformat(sep=" ")
                ),
                "reentry_depth_atr": round(row.reentry_depth_atr, 6),
                "consecutive_inside_closes": row.consecutive_inside_closes,
                "hold_after_reclaim_count": row.hold_after_reclaim_count,
                "followthrough_confirmed": row.followthrough_confirmed,
                "followthrough_time": (
                    "" if row.followthrough_time is None else row.followthrough_time.isoformat(sep=" ")
                ),
                "followthrough_strength": round(row.followthrough_strength, 4),
                "advisor_enabled": row.advisor_enabled,
                "advisor_regime": row.advisor_regime,
                "advisor_decision": row.advisor_decision,
                "advisor_alignment": row.advisor_alignment,
                "advisor_confidence": round(row.advisor_confidence, 4),
                "advisor_score": round(row.advisor_score, 4),
                "advisor_day_return_pct": round(row.advisor_day_return_pct, 6),
                "advisor_range_position": round(row.advisor_range_position, 6),
                "advisor_vwap_distance_atr": (
                    "" if row.advisor_vwap_distance_atr is None else round(row.advisor_vwap_distance_atr, 6)
                ),
                "advisor_move_15m_atr": (
                    "" if row.advisor_move_15m_atr is None else round(row.advisor_move_15m_atr, 6)
                ),
                "advisor_recent_move_atr": round(row.advisor_recent_move_atr, 6),
                "advisor_trend_efficiency": round(row.advisor_trend_efficiency, 6),
                "advisor_reason": row.advisor_reason,
                "episode_key": row.episode_key,
            })
    return path


def _print_summary(
    rows: Sequence[AnalyticalSignal],
    blocker_counts: Counter,
    conflict_counts: Counter,
    symbol_count: int,
    snapshot_count: int,
) -> None:
    by_resolution = Counter(row.resolution for row in rows)
    by_side = Counter(row.side for row in rows)
    winners = sum(1 for row in rows if row.pnl_points > 0)
    losers = sum(1 for row in rows if row.pnl_points < 0)
    flat = len(rows) - winners - losers
    total_pct = sum(row.pnl_pct for row in rows)
    avg_pct = total_pct / len(rows) if rows else 0.0

    print("\nBREAKOUT_INITIATION REPORT SUMMARY")
    print(f"Policy: {POLICY_VERSION}")
    print(f"Date: {TEST_DATE}")
    print(f"Symbols: {symbol_count}")
    print(f"Snapshots: {snapshot_count}")
    print(f"Signals: {len(rows)}")
    print(f"Resolution: {dict(by_resolution)}")
    print(f"Side: {dict(by_side)}")
    print(f"Winners / Losers / Flat: {winners} / {losers} / {flat}")
    print(f"Average fixed comparison PnL %: {avg_pct:.4f}")
    print(f"Total unweighted fixed comparison PnL %: {total_pct:.4f}")

    if ENABLE_HYPOTHETICAL_STOCK_ADVISOR:
        by_advice = Counter(row.advisor_decision for row in rows)
        by_regime = Counter(row.advisor_regime for row in rows)
        advised_rows = [row for row in rows if row.advisor_decision in {"ALLOW", "COUNTERTREND_EXCEPTION"}]
        advised_avg = (sum(row.pnl_pct for row in advised_rows) / len(advised_rows)) if advised_rows else 0.0
        advised_winners = sum(1 for row in advised_rows if row.pnl_points > 0)
        print(f"Advisor decisions: {dict(by_advice)}")
        print(f"Advisor regimes: {dict(by_regime)}")
        print(
            "Hypothetical advisor-approved fixed comparison: "
            f"{len(advised_rows)} signals, {advised_winners} winners, avg={advised_avg:.4f}%"
        )

    if blocker_counts:
        print("\nTop unique episode blockers:")
        for reason, count in blocker_counts.most_common(20):
            print(f"  {count:5d}  {reason}")
    if conflict_counts:
        print("\nSide conflicts:")
        for reason, count in conflict_counts.most_common(10):
            print(f"  {count:5d}  {reason}")

    if rows:
        print("\nSignals:")
        for row in rows[:PRINT_ROWS]:
            print(
                f"  {row.symbol:14s} {row.resolution:14s} {row.side:4s} "
                f"{row.entry_time.strftime('%H:%M')} -> {row.exit_time.strftime('%H:%M')} "
                f"P9={row.pnl_pct:+.3f}% MFE={float(row.full_mfe_pct or 0):+.3f}% "
                f"MAE={float(row.full_mae_pct or 0):+.3f}% "
                f"{row.level_type}@{row.reference_price:.2f} "
                f"PATH={row.entry_path} CONF={row.eventually_fully_confirmed} "
                f"ADV={row.advisor_decision}/{row.advisor_regime}"
            )


# =============================================================================
# Entrypoint
# =============================================================================
def main() -> int:
    setup_logging(log_file=LOG_FILE, log_level="INFO")
    logger.info(
        "Starting BREAKOUT_INITIATION report | policy=%s dynamic_boundaries_only=%s failed_signals=%s stock_advisor=%s observation_only=True date=%s symbols=%s types=%s",
        POLICY_VERSION,
        DYNAMIC_BOUNDARIES_ONLY,
        GENERATE_FAILED_SIGNALS,
        ENABLE_HYPOTHETICAL_STOCK_ADVISOR,
        TEST_DATE,
        SYMBOL_FILTER or "ALL",
        SYMBOL_TYPE_FILTER,
    )
    snapshots_by_symbol = _load_snapshots()
    if not snapshots_by_symbol:
        raise RuntimeError(
            f"No snapshots found for TEST_DATE={TEST_DATE}, "
            f"SYMBOL_FILTER={SYMBOL_FILTER or 'ALL'}, SYMBOL_TYPE_FILTER={SYMBOL_TYPE_FILTER}"
        )

    blocker_counts: Counter = Counter()
    conflict_counts: Counter = Counter()
    completed: List[AnalyticalSignal] = []
    total_symbols = len(snapshots_by_symbol)

    for index, (symbol, snapshots) in enumerate(sorted(snapshots_by_symbol.items()), start=1):
        try:
            completed.extend(
                _evaluate_symbol(
                    symbol,
                    snapshots,
                    blocker_counts,
                    conflict_counts,
                )
            )
        except Exception:
            logger.exception("BREAKOUT_INITIATION evaluation failed for %s", symbol)
            if STRICT_EVALUATION:
                raise
        if index % PROGRESS_EVERY_SYMBOLS == 0 or index == total_symbols:
            logger.info("Evaluated %d/%d symbols", index, total_symbols)

    completed.sort(key=lambda row: (row.signal_time, row.symbol, row.resolution, row.side))
    output = _write_csv(completed)
    snapshot_count = sum(len(rows) for rows in snapshots_by_symbol.values())
    _print_summary(
        completed,
        blocker_counts,
        conflict_counts,
        symbol_count=total_symbols,
        snapshot_count=snapshot_count,
    )
    logger.info(
        "Completed BREAKOUT_INITIATION report | policy=%s dynamic_boundaries_only=%s failed_signals=%s stock_advisor=%s observation_only=True output=%s signals=%d unique_episode_blockers=%s conflicts=%s",
        POLICY_VERSION,
        DYNAMIC_BOUNDARIES_ONLY,
        GENERATE_FAILED_SIGNALS,
        ENABLE_HYPOTHETICAL_STOCK_ADVISOR,
        output.resolve(),
        len(completed),
        dict(blocker_counts.most_common(20)),
        dict(conflict_counts.most_common(10)),
    )
    print(f"\nCSV written to: {output.resolve()}")
    print(f"Log written to: {Path(LOG_FILE).resolve()}")
    print("Share both files together with the frozen confirmed-acceptance reports for early-versus-confirmed comparison.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
