from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Tuple

from schemas.snapshot import SnapshotSchema
from services.selection.stock_advisor_result import StockAdvisorFeatures


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


def _window(snapshot: SnapshotSchema, key: str) -> Any:
    windows = getattr(snapshot, "market_windows", None) or {}
    return windows.get(key) or windows.get(key.lower()) or windows.get(key.upper())


def _best_recent_window(snapshot: SnapshotSchema) -> Any:
    for key in ("15m", "30m", "60m", "current"):
        w = _window(snapshot, key)
        if w is not None and (_safe_float(_get(w, "range_pct")) is not None):
            return w
    return None


def _range_position(close: Optional[float], low: Optional[float], high: Optional[float]) -> Optional[float]:
    if close is None or low is None or high is None:
        return None
    width = high - low
    if width <= 0:
        return None
    return max(0.0, min(1.0, (close - low) / width))


def _pct_range(high: Optional[float], low: Optional[float], ref: Optional[float]) -> Optional[float]:
    if high is None or low is None or ref is None or ref <= 0:
        return None
    return abs(high - low) / ref * 100.0


def _nearest_level(snapshot: SnapshotSchema, close: Optional[float], atr: Optional[float]) -> Tuple[str, Optional[float]]:
    if close is None or atr is None or atr <= 0:
        return "NONE", None

    levels: Dict[str, Optional[float]] = {
        "PDH": _safe_float(_get(snapshot, "structure", "anchors", "pdh")),
        "PDL": _safe_float(_get(snapshot, "structure", "anchors", "pdl")),
        "ORB_HIGH": _safe_float(_get(snapshot, "structure", "anchors", "orb_high")),
        "ORB_LOW": _safe_float(_get(snapshot, "structure", "anchors", "orb_low")),
        "RECENT15_HIGH": _safe_float(_get(snapshot, "structure", "anchors", "recent15_high")),
        "RECENT15_LOW": _safe_float(_get(snapshot, "structure", "anchors", "recent15_low")),
        "ACCEPTED_HIGH": _safe_float(_get(snapshot, "structure", "accepted", "range", "high")),
        "ACCEPTED_LOW": _safe_float(_get(snapshot, "structure", "accepted", "range", "low")),
    }

    best_type = "NONE"
    best_dist = None
    for level_type, price in levels.items():
        if price is None or price <= 0:
            continue
        dist = abs(close - price) / atr
        if best_dist is None or dist < best_dist:
            best_type = level_type
            best_dist = dist
    return best_type, round(best_dist, 4) if best_dist is not None else None


def extract_stock_advisor_features(
    snapshot: SnapshotSchema,
    recent_snapshots: Optional[Iterable[SnapshotSchema]] = None,
    candidate_context: Optional[Dict[str, Any]] = None,
) -> StockAdvisorFeatures:
    close = _safe_float(getattr(snapshot, "close", None)) or _safe_float(_get(snapshot, "bar", "close"))
    bar_high = _safe_float(_get(snapshot, "bar", "high"))
    bar_low = _safe_float(_get(snapshot, "bar", "low"))

    sod = _window(snapshot, "sod")
    day_open = _safe_float(_get(sod, "open")) or _safe_float(_get(snapshot, "levels", "today", "open"))
    day_high = _safe_float(_get(sod, "high")) or bar_high
    day_low = _safe_float(_get(sod, "low")) or bar_low
    day_range_pct = _safe_float(_get(sod, "range_pct")) or _pct_range(day_high, day_low, day_open or close)
    range_pos = _safe_float(_get(sod, "close_position_in_range"))
    if range_pos is None:
        range_pos = _range_position(close, day_low, day_high)

    recent = _best_recent_window(snapshot)
    recent_range_pct = _safe_float(_get(recent, "range_pct"))
    recent_move_pct = _safe_float(_get(recent, "move_pct"))
    recent_move_atr = _safe_float(_get(recent, "move_atr"))

    w30 = _window(snapshot, "30m")
    w60 = _window(snapshot, "60m")
    move_30m_atr = _safe_float(_get(w30, "move_atr"))
    move_60m_atr = _safe_float(_get(w60, "move_atr"))

    atr = _safe_float(_get(snapshot, "indicators", "atr", "value"))
    atr_pct = _safe_float(_get(snapshot, "indicators", "atr", "pct"))
    nearest_type, nearest_dist = _nearest_level(snapshot, close, atr)

    # StockAdvisor evaluates day/stock/family/side suitability after Evidence has
    # identified the exact setup candidate.  The snapshot remains strategy-neutral;
    # candidate-specific level/status data is supplied by Evidence for this pass.
    candidate = candidate_context if isinstance(candidate_context, dict) else {}
    candidate_setup = str(candidate.get("setup_label") or "").strip().upper()
    candidate_side = str(candidate.get("side") or "").strip().upper()
    candidate_status = str(candidate.get("breakout_status") or candidate_setup or "").strip().upper()
    candidate_level_type = str(candidate.get("level_type") or candidate.get("reference_id") or "").strip().upper()
    candidate_level_price = _safe_float(candidate.get("level_price"))
    setup_levels = candidate.get("setup_levels") if isinstance(candidate.get("setup_levels"), dict) else {}
    original_breakout_side = str(setup_levels.get("original_breakout_side") or "").strip().upper()

    if close is not None and atr is not None and atr > 0 and candidate_level_price is not None:
        nearest_type = candidate_level_type or nearest_type
        nearest_dist = round(abs(close - candidate_level_price) / atr, 4)

    structure_side = candidate_side if candidate_side in {"BUY", "SELL"} else str(
        _get(snapshot, "structure", "raw", "side", default="NEUTRAL") or "NEUTRAL"
    )
    breakout_status = candidate_status or "NONE"
    if candidate_setup == "FAILED_BREAKOUT" and original_breakout_side in {"BUY", "SELL"}:
        breakout_side = original_breakout_side
    elif candidate_side in {"BUY", "SELL"}:
        breakout_side = candidate_side
    else:
        breakout_side = "NEUTRAL"

    bb_position = _safe_float(_get(snapshot, "indicators", "bollinger", "position"))
    vwap_gap = _safe_float(_get(snapshot, "indicators", "vwap", "distance_pct"))
    volume_ratio = _safe_float(_get(snapshot, "volume", "bar_rvol"))
    if volume_ratio is None:
        volume_ratio = _safe_float(_get(snapshot, "volume", "today_vs_prev_ratio"))

    return StockAdvisorFeatures(
        symbol=str(getattr(snapshot, "symbol", "") or "").strip().upper(),
        snapshot_time=str(getattr(snapshot, "snapshot_time", "")),
        close=close,
        day_open=day_open,
        day_high=day_high,
        day_low=day_low,
        day_range_pct=day_range_pct,
        range_position=range_pos,
        recent_range_pct=recent_range_pct,
        recent_move_pct=recent_move_pct,
        recent_move_atr=recent_move_atr,
        move_30m_atr=move_30m_atr,
        move_60m_atr=move_60m_atr,
        vwap_gap_pct=vwap_gap,
        vwap_side=str(_get(snapshot, "indicators", "vwap", "side", default="UNKNOWN") or "UNKNOWN"),
        bb_position=bb_position,
        bb_zone=str(_get(snapshot, "indicators", "bollinger", "zone", default="UNKNOWN") or "UNKNOWN"),
        rsi=_safe_float(_get(snapshot, "indicators", "rsi", "value")),
        rsi_zone=str(_get(snapshot, "indicators", "rsi", "zone", default="NA") or "NA"),
        atr=atr,
        atr_pct=atr_pct,
        volume_ratio=volume_ratio,
        hma_state=str(_get(snapshot, "indicators", "hma", "state", default="UNKNOWN") or "UNKNOWN"),
        hma_strength=str(_get(snapshot, "indicators", "hma", "strength", default="UNKNOWN") or "UNKNOWN"),
        structure_state=str(_get(snapshot, "structure", "accepted", "state", default="UNKNOWN") or "UNKNOWN"),
        structure_side=structure_side,
        breakout_status=breakout_status,
        breakout_side=breakout_side,
        nearest_level_type=nearest_type,
        nearest_level_distance_atr=nearest_dist,
    )
