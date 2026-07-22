# utils/hold_exit_advisor.py
from __future__ import annotations

import logging
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from schemas.signal import SignalSchema
from schemas.snapshot import SnapshotSchema

logger = logging.getLogger(__name__)

# 
# Config
# 

@dataclass
class AdvisorCfg:
    # scoring window (recent minutes to compute continuation score & ratios)
    lookback_bars: int = 8

    # "hard exit" guards (check very recent tail)
    trend_flip_bars: int = 3       # require 3 consecutive flips to hard-exit
    consec_breaks: int = 3         # consecutive bars breaking HMAfast or ADX thresholds

    # thresholds
    min_adx: float = 18.0
    hma_tolerance_pct: float = 0.25  # how far below/above HMA* still counts as ok (percent)

    # composite scoring weights (current-bar checks; only available metrics are counted)
    w_hmafast: float = 1.0
    w_hmamid1: float = 0.75
    w_hmamid2: float = 0.75
    w_hmaslow: float = 0.70
    w_vwap: float    = 0.90
    w_adx: float     = 0.75
    w_slope: float   = 1.0

    # decision thresholds (composite 0..1)
    hold_threshold: float  = 0.60
    exit_threshold: float  = 0.35

    # single-bar override (setup flip on last bar but overall panel strong)
    override_if_single_bar_flip: bool = True
    override_enable: Optional[bool]   = True
    override_threshold: float         = 0.75

    # ATR movement confidence
    atr_move_threshold: float = 2.0   # |move from entry| >= N x ATR(entry)
    atr_field: str = "atr"            # minute key for ATR (e.g., "atr")

# 
# Helpers
# 

def _snap_to_dict(snap) -> Dict[str, Any]:
    """Best effort snapshot  plain dict (handles BaseModel/dataclass-ish objects)."""
    if snap is None:
        return {}
    if isinstance(snap, dict):
        return snap
    try:
        if hasattr(snap, "model_dump"):
            return snap.model_dump()
    except Exception:
        pass
    try:
        if hasattr(snap, "dict"):
            return snap.dict()
    except Exception:
        pass
    try:
        if hasattr(snap, "json"):
            return json.loads(snap.json())
    except Exception:
        pass
    try:
        return dict(getattr(snap, "__dict__", {}))
    except Exception:
        return {}

def _normalize_to_dict(obj) -> Optional[Dict[str, Any]]:
    """Turn obj into a dict if it is a BaseModel / json string / mapping; else None."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, str):
        try:
            d = json.loads(obj)
            return d if isinstance(d, dict) else None
        except Exception:
            return None
    # Pydantic / similar
    for attr in ("model_dump", "dict"):
        try:
            fn = getattr(obj, attr, None)
            if callable(fn):
                d = fn()
                return d if isinstance(d, dict) else None
        except Exception:
            pass
    try:
        fn = getattr(obj, "json", None)
        if callable(fn):
            d = json.loads(fn())
            return d if isinstance(d, dict) else None
    except Exception:
        pass
    return None

def _minute_map(snap) -> Dict[str, Any]:
    """
    Return the minute-frequency map (decoded) or {}.
    Handles dict / JSON string / BaseModel for `frequencies_snapshot`.
    """
    # 1) Try via full snapshot dict first
    sd = _snap_to_dict(snap)
    fs = sd.get("frequencies_snapshot")
    fsd = _normalize_to_dict(fs)
    if fsd is None:
        # 2) Fallback: read attribute directly and normalize
        try:
            fs_attr = getattr(snap, "frequencies_snapshot", None)
        except Exception:
            fs_attr = None
        fsd = _normalize_to_dict(fs_attr)

    if isinstance(fsd, dict):
        minute = fsd.get("minute")
        if isinstance(minute, dict):
            return minute
    return {}

def _side(sig: SignalSchema) -> str:
    v = getattr(sig, "trade_type", None)
    v = v.value if hasattr(v, "value") else v
    return str(v or "").upper()

def _to_naive(dt: Optional[datetime]) -> Optional[datetime]:
    if not dt:
        return None
    try:
        if getattr(dt, "tzinfo", None) is not None:
            return dt.replace(tzinfo=None)
    except Exception:
        pass
    return dt

def _m(snap, key: str, default=None):
    """Minute scope field reader (adx, vwap, close, hmafast, hmamid1/2, hmaslow, atr, etc.)."""
    if not snap:
        return default
    # try attribute first
    try:
        v = getattr(snap, key, None)
        if v is not None:
            return v
    except Exception:
        pass
    # then minute map (robustly decoded)
    try:
        minute = _minute_map(snap)
        if key in minute:
            return minute.get(key, default)
    except Exception:
        pass
    return default

def _close(snap) -> Optional[float]:
    try:
        if getattr(snap, "close", None) is not None:
            return float(snap.close)
    except Exception:
        pass
    v = _m(snap, "close")
    return float(v) if v is not None else None

def _atr_val(snap, field: str) -> Optional[float]:
    v = _m(snap, field)
    if v is None:
        try:
            v = getattr(snap, field, None)
        except Exception:
            v = None
    try:
        return float(v) if v is not None else None
    except Exception:
        return None

def _fetch_range(symbol: str, start_ts: datetime, end_ts: datetime) -> List[SnapshotSchema]:
    """
    Instrument-only, inclusive range:
      1) Pull 'today up to end_ts + pad' in ascending order
      2) Filter to [start_ts, end_ts] in Python (inclusive), using *naive* timestamps
    """
    pad = timedelta(minutes=1)
    try:
        s0 = _to_naive(start_ts)
        e0 = _to_naive(end_ts)

        est_minutes = max(5, int((e0 - s0).total_seconds() // 60) + 2)
        est_minutes = min(est_minutes + 2, 3000)

        rows = SnapshotSchema.fetch_recent_today_for_symbol_before_time(
            symbol, e0 + pad, limit=est_minutes, ascending=True
        ) or []

        out = []
        for s in rows:
            ts = _to_naive(getattr(s, "snapshot_time", None))
            if ts and (s0 <= ts <= e0):
                out.append(s)

        if not out and rows:
            try:
                first_ts = _to_naive(getattr(rows[0], "snapshot_time", None))
                last_ts  = _to_naive(getattr(rows[-1], "snapshot_time", None))
                logger.debug(
                    "hold_exit_advisor: fetched=%d first=%s last=%s wanted=[%s..%s] symbol=%s",
                    len(rows), first_ts, last_ts, s0, e0, symbol
                )
            except Exception:
                pass

        return out
    except Exception:
        logger.exception("hold_exit_advisor: _fetch_range failed for %s", symbol)
        return []

def _consecutive(pred: List[Optional[bool]]) -> int:
    r = 0
    for ok in reversed(pred):
        if ok is True:
            r += 1
        else:
            break
    return r

def _pct_diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    try:
        if b == 0:
            return None
        return (float(a) - float(b)) / float(b) * 100.0
    except Exception:
        return None

def _dir_ok(side: str, price: Optional[float], ref: Optional[float], tol_frac: float) -> Optional[bool]:
    if price is None or ref is None:
        return None
    if side == "BUY":
        return float(price) >= float(ref) * (1.0 - tol_frac)
    else:
        return float(price) <= float(ref) * (1.0 + tol_frac)

def _series(snaps: List[SnapshotSchema], key: str) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    for s in snaps:
        v = _m(s, key)
        out.append(float(v) if v is not None else None)
    return out

def _ok_ratio(series_ok: List[Optional[bool]]) -> Optional[float]:
    vals = [x for x in series_ok if x is not None]
    if not vals:
        return None
    return sum(1 for x in vals if x) / float(len(vals))

# 
# Core: advise_hold_or_exit
# 

def advise_hold_or_exit(
    signal_id: str,
    *,
    as_of: Optional[datetime] = None,
    cfg: AdvisorCfg = AdvisorCfg(),
) -> Dict[str, Any]:
    sig = SignalSchema.fetch_signal(signal_id)
    if not sig:
        return {"signal_id": signal_id, "error": "signal_not_found"}

    side = _side(sig)
    if side not in ("BUY", "SELL"):
        return {"signal_id": signal_id, "error": "unsupported_side"}

    # Window
    start_ts = _to_naive(getattr(sig, "entry_time", None))
    if not start_ts:
        return {"signal_id": signal_id, "error": "missing_entry_time"}

    end_ts = _to_naive(as_of) if as_of is not None else _to_naive(
        getattr(sig, "exit_time", None) or getattr(sig, "last_time", None) or start_ts
    )
    if end_ts < start_ts:
        end_ts = start_ts

    # Fetch snapshots
    snaps: List[SnapshotSchema] = _fetch_range(sig.symbol, start_ts, end_ts)
    if not snaps:
        return {
            "signal_id": signal_id,
            "symbol": sig.symbol,
            "side": side,
            "start": start_ts,
            "end": end_ts,
            "error": "no_snapshots_in_window",
        }

    # Tail for ratios / continuity votes
    tail = snaps[-cfg.lookback_bars:] if len(snaps) > cfg.lookback_bars else snaps[:]

    # Price series
    closes_full = [_close(s) for s in snaps]
    closes_tail = [_close(s) for s in tail]
    cur = closes_tail[-1] if closes_tail else closes_full[-1]

    # Entry, extrema
    entry_px = float(getattr(sig, "entry_price", None) or (closes_full[0] or 0.0))
    cmax = max([v for v in closes_full if v is not None], default=None)
    cmin = min([v for v in closes_full if v is not None], default=None)

    # Quantity (for total PnL)
    qty = float(getattr(sig, "quantity", 0) or 0)

    # ATR (at entry + current)
    atr_entry = _atr_val(snaps[0], cfg.atr_field)
    atr_last  = _atr_val(snaps[-1], cfg.atr_field)

    # MFE/MAE (absolute and in ATRs)
    mfe_abs = mae_abs = None
    if closes_full and entry_px:
        valid = [v for v in closes_full if v is not None]
        if valid:
            if side == "BUY":
                mfe_abs = (max(valid) - entry_px)
                mae_abs = (entry_px - min(valid))
            else:
                mfe_abs = (entry_px - min(valid))
                mae_abs = (max(valid) - entry_px)
    mfe_atr = (mfe_abs / atr_entry) if (mfe_abs is not None and atr_entry) else None
    mae_atr = (mae_abs / atr_entry) if (mae_abs is not None and atr_entry) else None
    current_move_abs = (cur - entry_px) if side == "BUY" else (entry_px - cur)
    current_move_atr = (current_move_abs / atr_entry) if (atr_entry and current_move_abs is not None) else None
    crossed_n_atr = (abs(current_move_atr) >= cfg.atr_move_threshold) if current_move_atr is not None else None

    # Minute fields (tail series)
    adxs   = _series(tail, "adx")
    vwaps  = _series(tail, "vwap")
    hfast  = _series(tail, "hmafast")
    hmid1  = _series(tail, "hmamid1")
    hmid2  = _series(tail, "hmamid2")
    hslow  = _series(tail, "hmaslow")

    tol_frac = float(cfg.hma_tolerance_pct) / 100.0

    # Current-bar location checks
    last_close = closes_tail[-1] if closes_tail else None
    last_vwap  = vwaps[-1] if vwaps else None
    last_adx   = adxs[-1]  if adxs  else None

    def _band_tail_ok(close_ser, ref_ser) -> List[Optional[bool]]:
        out = []
        for c, r in zip(close_ser, ref_ser):
            out.append(_dir_ok(side, c, r, tol_frac))
        return out

    above_fast_ok  = _band_tail_ok(closes_tail, hfast) if tail else []
    above_mid1_ok  = _band_tail_ok(closes_tail, hmid1) if tail else []
    above_mid2_ok  = _band_tail_ok(closes_tail, hmid2) if tail else []
    above_slow_ok  = _band_tail_ok(closes_tail, hslow) if tail else []

    vwap_side_ok: List[Optional[bool]] = []
    for c, v in zip(closes_tail, vwaps):
        if c is None or v is None:
            vwap_side_ok.append(None)
        else:
            vwap_side_ok.append(c >= v if side == "BUY" else c <= v)

    adx_ok_tail: List[Optional[bool]] = []
    for a in adxs:
        if a is None:
            adx_ok_tail.append(None)
        else:
            try:
                adx_ok_tail.append(float(a) >= float(cfg.min_adx))
            except Exception:
                adx_ok_tail.append(None)

    # Slope proxy on HMAfast (fallback to close)
    src = hfast if any(v is not None for v in hfast) else closes_tail
    slopes: List[Optional[float]] = []
    for i in range(1, len(src)):
        p, q = src[i-1], src[i]
        if p is None or q is None:
            slopes.append(None)
        else:
            slopes.append(float(q) - float(p))
    slope_favor_tail: List[Optional[bool]] = [None]
    for s in slopes:
        if s is None:
            slope_favor_tail.append(None)
        else:
            slope_favor_tail.append(s >= 0 if side == "BUY" else s <= 0)

    # Current-bar OK flags (used for composite)
    cur_ok = {
        "hmafast": _dir_ok(side, last_close, (hfast[-1] if hfast else None), tol_frac),
        "hmamid1": _dir_ok(side, last_close, (hmid1[-1] if hmid1 else None), tol_frac),
        "hmamid2": _dir_ok(side, last_close, (hmid2[-1] if hmid2 else None), tol_frac),
        "hmaslow": _dir_ok(side, last_close, (hslow[-1] if hslow else None), tol_frac),
        "vwap":    (None if (last_vwap is None or last_close is None) else (last_close >= last_vwap if side == "BUY" else last_close <= last_vwap)),
        "adx":     (None if last_adx is None else (float(last_adx) >= float(cfg.min_adx))),
        "slope":   (slope_favor_tail[-1] if slope_favor_tail else None),
    }

    # Composite score
    weights = {
        "hmafast": cfg.w_hmafast,
        "hmamid1": cfg.w_hmamid1,
        "hmamid2": cfg.w_hmamid2,
        "hmaslow": cfg.w_hmaslow,
        "vwap":    cfg.w_vwap,
        "adx":     cfg.w_adx,
        "slope":   cfg.w_slope,
    }
    total_w = sum(w for k, w in weights.items() if cur_ok.get(k) is not None)
    pass_w  = sum(weights[k] for k, v in cur_ok.items() if v is True and k in weights)
    composite = (pass_w / total_w) if total_w > 0 else None
    composite_points = {
        k: (weights[k] if cur_ok.get(k) else 0.0) if cur_ok.get(k) is not None else None
        for k in weights
    }

    # Legacy continuation votes
    votes = []
    for i in range(len(tail)):
        v = 0
        v += 1 if (i < len(above_fast_ok) and (above_fast_ok[i] is True)) else 0
        v += 1 if (i < len(vwap_side_ok)  and (vwap_side_ok[i]  is True)) else 0
        v += 1 if (i < len(adx_ok_tail)   and (adx_ok_tail[i]   is True)) else 0
        v += 1 if (i < len(slope_favor_tail) and (slope_favor_tail[i] is True)) else 0
        votes.append(v)
    max_votes = 4
    avg_vote = (sum(votes) / float(len(votes))) if votes else None
    cont_score = (avg_vote / max_votes) if avg_vote is not None else None

    # Hard exit checks (very recent tail)
    recent_len = max(cfg.trend_flip_bars, cfg.consec_breaks)
    recent_slope = (slope_favor_tail[-recent_len:] if slope_favor_tail else [])
    recent_hfast = (above_fast_ok[-recent_len:]    if above_fast_ok    else [])
    recent_adxok = (adx_ok_tail[-recent_len:]      if adx_ok_tail      else [])

    trend_flip   = _consecutive([ (x is False) for x in recent_slope ]) >= cfg.trend_flip_bars if recent_slope else False
    hma_breaks   = _consecutive([ (x is False) for x in recent_hfast ]) >= cfg.consec_breaks    if recent_hfast else False
    adx_collapse = _consecutive([ (x is False) for x in recent_adxok ]) >= cfg.consec_breaks    if recent_adxok else False

    # Locations block (current distance % + tail ratios)
    def _pack_loc(name: str, series: List[Optional[float]], tail_ok: List[Optional[bool]]):
        last = series[-1] if series else None
        return {
            "last": last,
            "distance_pct": _pct_diff(cur, last),
            "ok_now": cur_ok.get(name),
            "ratio_ok_tail": _ok_ratio(tail_ok),
        }

    locations = {
        "hmafast": _pack_loc("hmafast", hfast, above_fast_ok),
        "hmamid1": _pack_loc("hmamid1", hmid1, above_mid1_ok),
        "hmamid2": _pack_loc("hmamid2", hmid2, above_mid2_ok),
        "hmaslow": _pack_loc("hmaslow", hslow, above_slow_ok),
        "vwap":    _pack_loc("vwap",    vwaps, vwap_side_ok),
    }

    band_ratios = {
        "above_hmafast_ratio": _ok_ratio(above_fast_ok),
        "above_hmamid1_ratio": _ok_ratio(above_mid1_ok),
        "above_hmamid2_ratio": _ok_ratio(above_mid2_ok),
        "above_hmaslow_ratio": _ok_ratio(above_slow_ok),
        "vwap_side_ok_ratio":  _ok_ratio(vwap_side_ok),
        "adx_ok_ratio":        _ok_ratio(adx_ok_tail),
        "slope_favor_ratio":   _ok_ratio(slope_favor_tail),
    }

    # Decision
    reasons: List[str] = []
    hard_flags = {
        "trend_flip": trend_flip,
        "hma_breaks": hma_breaks,
        "adx_collapse": adx_collapse,
    }
    override_active = cfg.override_if_single_bar_flip if (cfg.override_enable is None) else bool(cfg.override_enable)

    if any(hard_flags.values()):
        rec = "EXIT_HARD"
        reasons.extend([k for k, v in hard_flags.items() if v])
    else:
        last_slope_ok = cur_ok.get("slope")
        if override_active and (last_slope_ok is False) and (composite is not None) and (composite >= cfg.override_threshold):
            rec = "HOLD_OVERRIDE"
            reasons.append("override_single_bar_flip")
        else:
            if composite is None:
                if cont_score is not None and cont_score >= cfg.hold_threshold:
                    rec = "HOLD"
                elif cont_score is not None and cont_score <= cfg.exit_threshold:
                    rec = "EXIT_SOFT"
                else:
                    rec = "HOLD_WITH_CAUTION"
                    if cont_score is not None and cont_score < 0.50:
                        reasons.append("weak_continuation")
            else:
                if composite >= cfg.hold_threshold:
                    rec = "HOLD"
                elif composite <= cfg.exit_threshold:
                    rec = "EXIT_SOFT"
                    if cont_score is not None and cont_score >= cfg.hold_threshold:
                        reasons.append("composite_low_but_legacy_ok")
                else:
                    rec = "HOLD_WITH_CAUTION"
                    if composite < 0.50:
                        reasons.append("weak_composite")

    # Prices / PnL blocks
    pnl_per_unit = (cur - entry_px) if side == "BUY" else (entry_px - cur)
    pnl_pct = (pnl_per_unit / entry_px * 100.0) if entry_px else None
    total_pnl = (pnl_per_unit * qty) if qty else None

    prices = {
        "entry_price": entry_px,
        "current_price": cur,
        "max_since_entry": cmax,
        "min_since_entry": cmin,
    }
    pnl = {
        "per_unit": pnl_per_unit,
        "percent": pnl_pct,
        "total": total_pnl,
        "in_profit": (pnl_per_unit is not None and pnl_per_unit >= 0.0),
    }
    excursions = {
        "atr_at_entry": atr_entry,
        "atr_at_last":  atr_last,
        "mfe_abs": mfe_abs,
        "mae_abs": mae_abs,
        "mfe_atr": (mfe_abs / atr_entry) if (mfe_abs is not None and atr_entry) else None,
        "mae_atr": (mae_abs / atr_entry) if (mae_abs is not None and atr_entry) else None,
        "current_move_atr": current_move_atr,
        "atr_threshold": cfg.atr_move_threshold,
        "atr_threshold_crossed": crossed_n_atr,
    }

    # Last-bar context (now uses robust minute map to avoid nulls)
    min_map = _minute_map(snaps[-1])
    deriv  = _normalize_to_dict(getattr(snaps[-1], "derivatives", None)) or {}
    opts   = deriv.get("options", {}) if isinstance(deriv, dict) else {}
    last_bar = {
        "state": getattr(snaps[-1], "state", None),
        "strength": getattr(snaps[-1], "strength", None),
        "hma_strength": min_map.get("hma_strength"),
        "rsi_strength": min_map.get("rsi_strength"),
        "hma_slope_conviction": getattr(snaps[-1], "hma_slope_conviction", None),
        "intraday_intensity": getattr(snaps[-1], "intraday_intensity", None),
        "vwap_gap_strength": min_map.get("vwap_gap_strength"),
        "options_sentiment": opts.get("options_sentiment", {}),
    }

    # Diagnostics bundle
    diag: Dict[str, Any] = {
        "symbol": sig.symbol,
        "side": side,
        "entry_time": start_ts,
        "exit_time": getattr(sig, "exit_time", None),
        "as_of": end_ts,
        "window_start": start_ts,
        "window_end": end_ts,
        "bars_considered": len(tail),

        "prices": prices,
        "pnl": pnl,
        "excursions": excursions,

        "locations": locations,
        "band_ratios": band_ratios,

        "continuation": {
            "avg_vote": round(avg_vote, 3) if avg_vote is not None else None,
            "max_votes_per_bar": 4,
            "score_0_to_1": round(cont_score, 3) if cont_score is not None else None,
            "composite_score_0_to_1": round(composite, 3) if composite is not None else None,
            "components_ok_now": cur_ok,
            "composite_points": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in composite_points.items()},
            "weights": {
                "hmafast": cfg.w_hmafast, "hmamid1": cfg.w_hmamid1, "hmamid2": cfg.w_hmamid2,
                "hmaslow": cfg.w_hmaslow, "vwap": cfg.w_vwap, "adx": cfg.w_adx, "slope": cfg.w_slope
            },
            "hold_threshold": cfg.hold_threshold,
            "exit_threshold": cfg.exit_threshold,
            "override_threshold": cfg.override_threshold,
        },

        "hard_exit": hard_flags,
        "last_bar": last_bar,
        "reasons": reasons,
    }

    return {
        "signal_id": signal_id,
        "recommendation": rec,
        "diagnostics": diag,
    }
