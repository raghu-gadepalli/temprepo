# services/derivatives_helper.py
#
# PURE computation helpers for derivatives (NO DB, NO SQLAlchemy, NO Pydantic).
# Designed for v2 table:
#   symbol, snapshot_time, raw(json), derived(json nullable)
#
# Key design:
# - Helper expects ALL samples since start-of-day (chronological)
# - Computes deltas for windows (5m/15m/60m/SOD) in-memory
# - Produces `derived` dict matching the new schema blocks.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, time
from typing import Any, Dict, List, Optional, Literal, Tuple, Union
import math

from configs.derivatives_config import DERIVATIVES_CONFIG


# -----------------------------
# Types (for readability only)
# -----------------------------
SentIndication = Literal["bullish", "bearish", "neutral"]
SentStatus = Literal["ok", "na", "error"]

FutLabel = Literal[
    "LONG_BUILDUP",
    "SHORT_BUILDUP",
    "SHORT_COVERING",
    "LONG_UNWINDING",
    "NEUTRAL"
]


# ============================================================
# Small utilities
# ============================================================

def _safe_float(x) -> Optional[float]:
    try:
        if x is None:
            return None
        v = float(x)
        if math.isnan(v):
            return None
        return v
    except Exception:
        return None


def _safe_int(x) -> Optional[int]:
    try:
        if x is None:
            return None
        return int(float(x))
    except Exception:
        return None


def _parse_dt(x: Any) -> Optional[datetime]:
    if x is None:
        return None
    if isinstance(x, datetime):
        return x
    if isinstance(x, str):
        try:
            return datetime.fromisoformat(x)
        except Exception:
            return None
    return None


def _day_start(ts: datetime) -> datetime:
    # naive datetime assumed consistent with DB storage in your project
    return datetime.combine(ts.date(), time(0, 0, 0))


def _is_dict(d: Any) -> bool:
    return isinstance(d, dict)


def _get(obj: Any, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# ============================================================
# Raw chain parsing / normalization
# ============================================================

def _raw_chain(sample: Any) -> Dict[str, Any]:
    """
    Extract raw chain dict from a sample.
    Supported sample shapes:
      - dict with keys: raw, snapshot_time
      - ORM-like object with attributes: raw, snapshot_time
    """
    raw = _get(sample, "raw", None)
    return raw if isinstance(raw, dict) else {}


def _sample_time(sample: Any) -> Optional[datetime]:
    ts = _get(sample, "snapshot_time", None)
    return _parse_dt(ts)


def _spot_from_raw(raw: Dict[str, Any]) -> Optional[float]:
    return _safe_float(raw.get("spot_price"))


def _future_quote(raw: Dict[str, Any]) -> Dict[str, Any]:
    fut = raw.get("future")
    return fut if isinstance(fut, dict) else {}


def _options_map(raw: Dict[str, Any]) -> Dict[str, Any]:
    opt = raw.get("options")
    return opt if isinstance(opt, dict) else {}


def _parse_strike_kind(key: str, rec: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    """
    Default storage uses keys like "1120_CE", "1120_PE".
    Returns (strike, kind) where kind in {"CE","PE"}.
    """
    strike = None
    kind = None

    if isinstance(key, str) and "_" in key:
        try:
            a, b = key.split("_", 1)
            strike = _safe_float(a)
            kind = (b or "").upper()
        except Exception:
            pass

    if strike is None:
        strike = _safe_float(rec.get("strike_price")) or _safe_float(rec.get("strike"))

    if kind is None:
        k2 = (rec.get("type") or rec.get("instrument_type") or "").upper()
        if k2 in ("CE", "PE"):
            kind = k2
        elif isinstance(key, str) and key.endswith("_CE"):
            kind = "CE"
        elif isinstance(key, str) and key.endswith("_PE"):
            kind = "PE"

    if kind not in ("CE", "PE"):
        return None, None

    return strike, kind


def _iter_options(raw: Dict[str, Any]):
    """
    Yield (strike, kind, rec) for options.
    rec shape (as stored): InstrumentQuote-like dict:
      { instrument, exchange, quote_time, last_price, volume, oi, ohlc, expiry }
    """
    opts = _options_map(raw)
    for k, rec in opts.items():
        if not isinstance(rec, dict):
            continue
        strike, kind = _parse_strike_kind(k, rec)
        if strike is None or kind is None:
            continue
        yield float(strike), kind, rec


def _all_strikes(raw: Dict[str, Any]) -> List[float]:
    s = sorted({strike for strike, _, _ in _iter_options(raw)})
    return s


def _atm_strike(raw: Dict[str, Any], spot: float) -> Optional[float]:
    strikes = _all_strikes(raw)
    if not strikes:
        return None
    return min(strikes, key=lambda x: abs(x - float(spot)))


def _oi_at(raw: Dict[str, Any], strike: float, kind: str) -> float:
    opts = _options_map(raw)
    rec = opts.get(f"{int(strike)}_{kind}")
    if not isinstance(rec, dict):
        # fallback scan (safe but slower)
        for s, k, r in _iter_options(raw):
            if int(s) == int(strike) and k == kind:
                rec = r
                break
    if not isinstance(rec, dict):
        return 0.0
    return float(rec.get("oi") or 0.0)


def _ltp_at(raw: Dict[str, Any], strike: float, kind: str) -> float:
    opts = _options_map(raw)
    rec = opts.get(f"{int(strike)}_{kind}")
    if not isinstance(rec, dict):
        for s, k, r in _iter_options(raw):
            if int(s) == int(strike) and k == kind:
                rec = r
                break
    if not isinstance(rec, dict):
        return 0.0
    # v2 uses last_price
    return float(rec.get("last_price") or 0.0)


def _symbol_at(raw: Dict[str, Any], strike: float, kind: str) -> Optional[str]:
    opts = _options_map(raw)
    rec = opts.get(f"{int(strike)}_{kind}")
    if not isinstance(rec, dict):
        for s, k, r in _iter_options(raw):
            if int(s) == int(strike) and k == kind:
                rec = r
                break
    if not isinstance(rec, dict):
        return None
    return rec.get("instrument") or rec.get("symbol")


# ============================================================
# Sampling: choose baselines for windows
# ============================================================

def _filter_same_day(samples: List[Any], asof: datetime) -> List[Any]:
    day = asof.date()
    out = []
    for s in samples:
        ts = _sample_time(s)
        if ts and ts.date() == day:
            out.append(s)
    out.sort(key=lambda x: _sample_time(x) or datetime.min)
    return out


def _pick_baseline_for_minutes(samples_same_day: List[Any], asof: datetime, minutes: int) -> Optional[Any]:
    """
    Baseline = latest sample with ts <= asof - minutes.
    """
    cutoff = asof - timedelta(minutes=int(minutes))
    base = None
    for s in reversed(samples_same_day):
        ts = _sample_time(s)
        if ts and ts <= cutoff:
            base = s
            break
    return base


def _pick_baseline_sod(samples_same_day: List[Any]) -> Optional[Any]:
    return samples_same_day[0] if samples_same_day else None


# ============================================================
# 1) OptionsLite (ATM/PCR/top calls/puts/support/resistance/max pain optional)
# ============================================================

def compute_options_lite(raw_now: Dict[str, Any], *, top_n: Optional[int] = None) -> Optional[Dict[str, Any]]:
    if top_n is None:
        top_n = DERIVATIVES_CONFIG.derived.options_lite.top_n

    spot = _spot_from_raw(raw_now)
    if spot is None:
        return None

    strikes = _all_strikes(raw_now)
    if not strikes:
        return None

    atm = _atm_strike(raw_now, float(spot))
    if atm is None:
        return None

    calls = []
    puts = []
    for strike, kind, rec in _iter_options(raw_now):
        oi = _safe_float(rec.get("oi")) or 0.0
        if oi <= 0:
            continue
        sym = rec.get("instrument") or rec.get("symbol")
        ltp = _safe_float(rec.get("last_price"))
        entry = {"symbol": sym, "strike": float(strike), "oi": float(oi)}
        if ltp is not None:
            entry["ltp"] = float(ltp)
        if kind == "CE":
            calls.append(entry)
        else:
            puts.append(entry)

    if not calls and not puts:
        return None

    total_call_oi = sum(x["oi"] for x in calls) if calls else 0.0
    total_put_oi  = sum(x["oi"] for x in puts) if puts else 0.0
    pcr = (total_put_oi / total_call_oi) if total_call_oi else None

    # Support/Resistance using max OI strikes (simple and stable)
    support = max(puts, key=lambda x: x["oi"])["strike"] if puts else None
    resistance = max(calls, key=lambda x: x["oi"])["strike"] if calls else None

    # Top calls/puts near ATM (distance first, then OI)
    calls_sorted = sorted(calls, key=lambda x: (abs(x["strike"] - atm), -x["oi"]))[: int(top_n)]
    puts_sorted  = sorted(puts,  key=lambda x: (abs(x["strike"] - atm), -x["oi"]))[: int(top_n)]

    return {
        "atm_strike": float(atm),
        "pcr": float(pcr) if pcr is not None else None,
        "support": float(support) if support is not None else None,
        "resistance": float(resistance) if resistance is not None else None,
        "max_pain": None,  # keep placeholder; compute later if you want
        "top_calls": calls_sorted,
        "top_puts": puts_sorted,
    }


# ============================================================
# 2) OptionLadder (for selection at snapshot time)
# ============================================================

def compute_option_ladder(
    raw_now: Dict[str, Any],
    raw_base: Optional[Dict[str, Any]],
    *,
    window: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    if window is None:
        window = DERIVATIVES_CONFIG.derived.option_ladder.window

    spot = _spot_from_raw(raw_now)
    if spot is None:
        return None

    strikes = _all_strikes(raw_now)
    if not strikes:
        return None

    atm = _atm_strike(raw_now, float(spot))
    if atm is None:
        return None

    # Choose window around ATM by index distance
    try:
        i_atm = strikes.index(atm)
    except Exception:
        i_atm = min(range(len(strikes)), key=lambda i: abs(strikes[i] - atm))
    half = max(0, int(window))
    lo = max(0, i_atm - half)
    hi = min(len(strikes), i_atm + half + 1)
    win_strikes = strikes[lo:hi] or [atm]

    base_ok = isinstance(raw_base, dict) and bool(raw_base)

    calls = []
    puts = []
    for K in win_strikes:
        ce = {
            "symbol": _symbol_at(raw_now, K, "CE"),
            "type": "CE",
            "strike": float(K),
            "oi": _safe_float(_oi_at(raw_now, K, "CE")),
            "oi_chg": None,
            "ltp": _safe_float(_ltp_at(raw_now, K, "CE")),
        }
        pe = {
            "symbol": _symbol_at(raw_now, K, "PE"),
            "type": "PE",
            "strike": float(K),
            "oi": _safe_float(_oi_at(raw_now, K, "PE")),
            "oi_chg": None,
            "ltp": _safe_float(_ltp_at(raw_now, K, "PE")),
        }

        if base_ok:
            ce_prev = _safe_float(_oi_at(raw_base, K, "CE"))
            pe_prev = _safe_float(_oi_at(raw_base, K, "PE"))
            if ce_prev is not None and ce["oi"] is not None:
                ce["oi_chg"] = float(ce["oi"] - ce_prev)
            if pe_prev is not None and pe["oi"] is not None:
                pe["oi_chg"] = float(pe["oi"] - pe_prev)

        calls.append(ce)
        puts.append(pe)

    return {
        "window": int(window),
        "atm_strike": float(atm),
        "calls": calls,
        "puts": puts,
    }


# ============================================================
# 3) OI Windows (rows + totals) for keys: "5m","15m","60m","sod"
# ============================================================

def build_oi_window(
    raw_now: Dict[str, Any],
    raw_base: Optional[Dict[str, Any]],
    *,
    ladder_window: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    if ladder_window is None:
        ladder_window = DERIVATIVES_CONFIG.derived.option_ladder.window

    spot = _spot_from_raw(raw_now)
    if spot is None:
        return None

    strikes = _all_strikes(raw_now)
    if not strikes:
        return None

    atm = _atm_strike(raw_now, float(spot))
    if atm is None:
        return None

    # use same window selection as ladder
    try:
        i_atm = strikes.index(atm)
    except Exception:
        i_atm = min(range(len(strikes)), key=lambda i: abs(strikes[i] - atm))
    half = max(0, int(ladder_window))
    lo = max(0, i_atm - half)
    hi = min(len(strikes), i_atm + half + 1)
    win_strikes = strikes[lo:hi] or [atm]

    base_ok = isinstance(raw_base, dict) and bool(raw_base)

    rows = []
    tot_ce_oi = 0
    tot_pe_oi = 0
    tot_ce_chg = 0
    tot_pe_chg = 0

    for K in win_strikes:
        ce_oi_now = _safe_int(_oi_at(raw_now, K, "CE")) or 0
        pe_oi_now = _safe_int(_oi_at(raw_now, K, "PE")) or 0

        ce_oi_prev = (_safe_int(_oi_at(raw_base, K, "CE")) if base_ok else None)
        pe_oi_prev = (_safe_int(_oi_at(raw_base, K, "PE")) if base_ok else None)

        ce_chg = (ce_oi_now - ce_oi_prev) if (base_ok and ce_oi_prev is not None) else None
        pe_chg = (pe_oi_now - pe_oi_prev) if (base_ok and pe_oi_prev is not None) else None

        row = {
            "strike": float(K),
            "ce_symbol": _symbol_at(raw_now, K, "CE"),
            "ce_oi": int(ce_oi_now),
            "ce_oi_chg": int(ce_chg) if ce_chg is not None else None,
            "ce_ltp": _safe_float(_ltp_at(raw_now, K, "CE")),

            "pe_symbol": _symbol_at(raw_now, K, "PE"),
            "pe_oi": int(pe_oi_now),
            "pe_oi_chg": int(pe_chg) if pe_chg is not None else None,
            "pe_ltp": _safe_float(_ltp_at(raw_now, K, "PE")),
        }
        rows.append(row)

        tot_ce_oi += int(ce_oi_now)
        tot_pe_oi += int(pe_oi_now)
        if ce_chg is not None:
            tot_ce_chg += int(ce_chg)
        if pe_chg is not None:
            tot_pe_chg += int(pe_chg)

    return {
        "atm": float(atm),
        "window": int(ladder_window),
        "rows": rows,
        "totals": {
            "ce_oi": int(tot_ce_oi),
            "pe_oi": int(tot_pe_oi),
            "ce_oi_chg": int(tot_ce_chg) if base_ok else None,
            "pe_oi_chg": int(tot_pe_chg) if base_ok else None,
        },
    }


# ============================================================
# 4) Option sentiment per window (prev vs now pair)
#     (same philosophy as your old helper, adapted to v2 raw shape)
# ============================================================

def _infer_lot_size(_raw_now: Dict[str, Any]) -> float:
    # You can enhance later; default stable.
    return 1.0


def _empty_opt_sent(window: str, window_start: Optional[datetime], window_end: Optional[datetime], reason: str) -> Dict[str, Any]:
    return {
        "status": "na",
        "window": window,
        "window_start": window_start,
        "window_end": window_end,
        "atm": None,
        "indication": None,
        "strength": None,
        "pcr_now": None,
        "pcr_delta": None,
        "driver": None,
        "components": None,
        "reason": reason,
    }


def _compute_opt_sent_pair(
    raw_base: Dict[str, Any],
    raw_now: Dict[str, Any],
    *,
    asof: datetime,
    window_key: str,
    window_start: Optional[datetime],
    window_end: Optional[datetime],
    atm_window: int = 3,
    notional_floor: float = 0.0,
    min_contracts_floor: float = 0.0,
) -> Dict[str, Any]:

    spot = _spot_from_raw(raw_now)
    if spot is None:
        return _empty_opt_sent(window_key, window_start, window_end, "spot_missing")

    strikes = _all_strikes(raw_now)
    if not strikes:
        return _empty_opt_sent(window_key, window_start, window_end, "no_strikes")

    atm = _atm_strike(raw_now, float(spot))
    if atm is None:
        return _empty_opt_sent(window_key, window_start, window_end, "no_atm")

    # ---- Find ATM index ----
    try:
        i_atm = strikes.index(atm)
    except Exception:
        i_atm = min(range(len(strikes)), key=lambda i: abs(strikes[i] - atm))

    half = max(0, int(atm_window))
    lo = max(0, i_atm - half)
    hi = min(len(strikes), i_atm + half + 1)
    near = strikes[lo:hi] or [atm]

    lot_size = _infer_lot_size(raw_now) or 1.0

    # ---- Dynamic floor logic ----
    atm_ce = _safe_float(_ltp_at(raw_now, atm, "CE")) or 0.0
    atm_pe = _safe_float(_ltp_at(raw_now, atm, "PE")) or 0.0
    atm_prem = atm_ce if atm_ce > 0 else (atm_pe if atm_pe > 0 else 0.0)

    dyn_floor = (
        float(min_contracts_floor or 0.0)
        * float(atm_prem)
        * float(lot_size)
        if (min_contracts_floor and atm_prem)
        else 0.0
    )

    eff_floor = max(float(notional_floor or 0.0), float(dyn_floor))

    # ---- Component buckets ----
    comps = {
        "ce_long_buildup": 0.0,
        "ce_writing": 0.0,
        "ce_short_cover": 0.0,
        "ce_long_unwind": 0.0,
        "pe_long_buildup": 0.0,
        "pe_writing": 0.0,
        "pe_short_cover": 0.0,
        "pe_long_unwind": 0.0,
    }

    bull = 0.0
    bear = 0.0

    # ---- Iterate strikes near ATM ----
    for idx, K in enumerate(near):

        # Distance weight (stable + cheap)
        w = 1.0 / (1.0 + abs((lo + idx) - i_atm))

        # =====================
        # CE FLOW
        # =====================
        dCoi = _oi_at(raw_now, K, "CE") - _oi_at(raw_base, K, "CE")
        dCpx = _ltp_at(raw_now, K, "CE") - _ltp_at(raw_base, K, "CE")
        ce_px = _safe_float(_ltp_at(raw_now, K, "CE")) or \
                _safe_float(_ltp_at(raw_base, K, "CE")) or 0.0

        ce_notional = abs(dCoi) * abs(ce_px) * lot_size

        if ce_notional >= eff_floor:

            if dCpx > 0 and dCoi > 0:
                comps["ce_long_buildup"] += w * abs(dCoi)
                bull += w * abs(dCoi)

            elif dCpx < 0 and dCoi > 0:
                comps["ce_writing"] += w * abs(dCoi) * 0.9
                bear += w * abs(dCoi) * 0.9

            elif dCpx > 0 and dCoi < 0:
                comps["ce_short_cover"] += w * abs(dCoi) * 0.6
                bull += w * abs(dCoi) * 0.6

            elif dCpx < 0 and dCoi < 0:
                comps["ce_long_unwind"] += w * abs(dCoi) * 0.4
                bear += w * abs(dCoi) * 0.4

        # =====================
        # PE FLOW
        # =====================
        dPoi = _oi_at(raw_now, K, "PE") - _oi_at(raw_base, K, "PE")
        dPpx = _ltp_at(raw_now, K, "PE") - _ltp_at(raw_base, K, "PE")
        pe_px = _safe_float(_ltp_at(raw_now, K, "PE")) or \
                _safe_float(_ltp_at(raw_base, K, "PE")) or 0.0

        pe_notional = abs(dPoi) * abs(pe_px) * lot_size

        if pe_notional >= eff_floor:

            if dPpx > 0 and dPoi > 0:
                comps["pe_long_buildup"] += w * abs(dPoi)
                bear += w * abs(dPoi)

            elif dPpx < 0 and dPoi > 0:
                comps["pe_writing"] += w * abs(dPoi) * 0.9
                bull += w * abs(dPoi) * 0.9

            elif dPpx > 0 and dPoi < 0:
                comps["pe_short_cover"] += w * abs(dPoi) * 0.6
                bear += w * abs(dPoi) * 0.6

            elif dPpx < 0 and dPoi < 0:
                comps["pe_long_unwind"] += w * abs(dPoi) * 0.4
                bull += w * abs(dPoi) * 0.4

    # =====================
    # Indication
    # =====================
    indication = "neutral"

    if bull >= bear * DERIVATIVES_CONFIG.derived.option_sentiment.one_sided_flow_ratio and bull > 0:
        indication = "bullish"
    elif bear >= bull * DERIVATIVES_CONFIG.derived.option_sentiment.one_sided_flow_ratio and bear > 0:
        indication = "bearish"

    # =====================
    # Strength (Option C)
    # =====================
    # Directional strength should also work for one-sided flow.
    # Example: bull_flow > 0 and bear_flow == 0 means strong bullish
    # confirmation, not unknown/null.
    directional_total = bull + bear
    if directional_total > 0:
        strength = abs(bull - bear) / directional_total
    else:
        strength = None

    # =====================
    # PCR near ATM
    # =====================
    def pcr_near(chain_raw: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
        c_oi = sum(_oi_at(chain_raw, K, "CE") for K in near)
        p_oi = sum(_oi_at(chain_raw, K, "PE") for K in near)
        return p_oi, c_oi

    p_now_p, p_now_c = pcr_near(raw_now)
    p_base_p, p_base_c = pcr_near(raw_base)

    pcr_now = (p_now_p / p_now_c) if p_now_c else None
    pcr_base = (p_base_p / p_base_c) if p_base_c else None
    pcr_delta = (
        pcr_now - pcr_base
        if (pcr_now is not None and pcr_base is not None)
        else None
    )

    # =====================
    # Driver
    # =====================
    total_flow = sum(comps.values())
    driver = None

    if total_flow > 0:
        key, val = max(comps.items(), key=lambda kv: kv[1])
        share = float(val / total_flow)

        label_map = {
            "ce_long_buildup": "CE long buildup",
            "ce_writing": "CE writing",
            "ce_short_cover": "CE short cover",
            "ce_long_unwind": "CE long unwind",
            "pe_long_buildup": "PE long buildup",
            "pe_writing": "PE writing",
            "pe_short_cover": "PE short cover",
            "pe_long_unwind": "PE long unwind",
        }

        bias_map = {
            "ce_long_buildup": "bullish",
            "ce_writing": "bearish",
            "ce_short_cover": "bullish",
            "ce_long_unwind": "bearish",
            "pe_long_buildup": "bearish",
            "pe_writing": "bullish",
            "pe_short_cover": "bearish",
            "pe_long_unwind": "bullish",
        }

        driver = {
            "key": key if share >= DERIVATIVES_CONFIG.derived.option_sentiment.driver_min_share else "mixed",
            "label": label_map.get(key, key) if share >= DERIVATIVES_CONFIG.derived.option_sentiment.driver_min_share else "Mixed flows",
            "bias": bias_map.get(key, indication),
            "share": share,
        }

    return {
        "status": "ok",
        "window": window_key,
        "window_start": window_start,
        "window_end": window_end,
        "atm": float(atm),
        "indication": indication,
        "strength": float(strength) if strength is not None else None,
        "pcr_now": float(pcr_now) if pcr_now is not None else None,
        "pcr_delta": float(pcr_delta) if pcr_delta is not None else None,
        "driver": driver,
        "components": comps,
        "bull_flow": float(bull),
        "bear_flow": float(bear),
        "total_flow": float(bull + bear),
    }

# ============================================================
# 5) Futures sentiment per window (price delta + OI delta)
# ============================================================

def _fut_ltp(raw: Dict[str, Any]) -> Optional[float]:
    fut = _future_quote(raw)
    return _safe_float(fut.get("last_price"))


def _fut_oi(raw: Dict[str, Any]) -> Optional[float]:
    fut = _future_quote(raw)
    return _safe_float(fut.get("oi"))


def compute_future_sentiment_window(
    raw_base: Optional[Dict[str, Any]],
    raw_now: Dict[str, Any],
    *,
    window_key: str,
    window_start: Optional[datetime],
    window_end: Optional[datetime],
) -> Dict[str, Any]:
    """
    Classic build-up classification:
      price up + oi up   => LONG_BUILDUP
      price down + oi up => SHORT_BUILDUP
      price up + oi down => SHORT_COVERING
      price down + oi down => LONG_UNWINDING
      else => NEUTRAL
    """
    ltp_now = _fut_ltp(raw_now)
    oi_now = _fut_oi(raw_now)

    if ltp_now is None or oi_now is None:
        return {
            "status": "na",
            "window": window_key,
            "window_start": window_start,
            "window_end": window_end,
            "label": None,
            "fut_ltp_now": ltp_now,
            "fut_ltp_delta": None,
            "fut_oi_now": oi_now,
            "fut_oi_delta": None,
            "strength": None,
        }

    if not raw_base:
        return {
            "status": "na",
            "window": window_key,
            "window_start": window_start,
            "window_end": window_end,
            "label": None,
            "fut_ltp_now": float(ltp_now),
            "fut_ltp_delta": None,
            "fut_oi_now": float(oi_now),
            "fut_oi_delta": None,
            "strength": None,
        }

    ltp_base = _fut_ltp(raw_base)
    oi_base = _fut_oi(raw_base)
    if ltp_base is None or oi_base is None:
        return {
            "status": "na",
            "window": window_key,
            "window_start": window_start,
            "window_end": window_end,
            "label": None,
            "fut_ltp_now": float(ltp_now),
            "fut_ltp_delta": None,
            "fut_oi_now": float(oi_now),
            "fut_oi_delta": None,
            "strength": None,
        }

    dpx = float(ltp_now - ltp_base)
    doi = float(oi_now - oi_base)

    label: FutLabel = "NEUTRAL"
    if dpx > 0 and doi > 0:
        label = "LONG_BUILDUP"
    elif dpx < 0 and doi > 0:
        label = "SHORT_BUILDUP"
    elif dpx > 0 and doi < 0:
        label = "SHORT_COVERING"
    elif dpx < 0 and doi < 0:
        label = "LONG_UNWINDING"

    # optional: strength scaled by normalized magnitudes (keep simple for now)
    strength = None

    return {
        "status": "ok",
        "window": window_key,
        "window_start": window_start,
        "window_end": window_end,
        "label": label,
        "fut_ltp_now": float(ltp_now),
        "fut_ltp_delta": dpx,
        "fut_oi_now": float(oi_now),
        "fut_oi_delta": doi,
        "strength": strength,
    }


# ============================================================
# 6) Top-level: build derived from day samples
# ============================================================

def build_derived_from_day(
    samples: List[Any],
    *,
    asof: Optional[datetime] = None,
    windows: Optional[Dict[str, Union[int, str]]] = None,
    ladder_window: Optional[int] = None,
    opt_sent_atm_window: Optional[int] = None,
    opt_sent_notional_floor: Optional[float] = None,
    opt_sent_min_contracts_floor: Optional[float] = None,
    top_n: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Build DerivativesDerived dict for a single (symbol, asof time) using
    all samples since start-of-day.

    windows default:
      {"5m": 5, "15m": 15, "60m": 60, "sod": "SOD"}

    Returns:
      {
        "options_lite": {...},
        "option_ladder": {...},
        "oi_windows": {...},
        "option_sentiment_windows": {...},
        "future_sentiment_windows": {...}
      }
    """
    cfg = DERIVATIVES_CONFIG.derived
    sent_cfg = cfg.option_sentiment

    if windows is None:
        windows = sent_cfg.windows
    if ladder_window is None:
        ladder_window = cfg.option_ladder.window
    if opt_sent_atm_window is None:
        opt_sent_atm_window = sent_cfg.atm_window
    if opt_sent_notional_floor is None:
        opt_sent_notional_floor = sent_cfg.notional_floor
    if opt_sent_min_contracts_floor is None:
        opt_sent_min_contracts_floor = sent_cfg.min_contracts_floor
    if top_n is None:
        top_n = cfg.options_lite.top_n

    # pick asof = last sample time if not provided
    if asof is None:
        for s in reversed(samples or []):
            ts = _sample_time(s)
            if ts:
                asof = ts
                break
    if asof is None:
        return {
            "options_lite": None,
            "option_ladder": None,
            "oi_windows": {},
            "option_sentiment_windows": {},
            "future_sentiment_windows": {},
        }

    # keep same-day samples only
    day_samples = _filter_same_day(samples, asof)
    if not day_samples:
        return {
            "options_lite": None,
            "option_ladder": None,
            "oi_windows": {},
            "option_sentiment_windows": {},
            "future_sentiment_windows": {},
        }

    # now sample = latest <= asof
    now_sample = None
    for s in reversed(day_samples):
        ts = _sample_time(s)
        if ts and ts <= asof:
            now_sample = s
            break
    if now_sample is None:
        now_sample = day_samples[-1]

    raw_now = _raw_chain(now_sample)

    # Base for ladder/oi window default: 5m baseline if available, else SOD
    base_5m = _pick_baseline_for_minutes(day_samples, asof, 5)
    base_sod = _pick_baseline_sod(day_samples)
    raw_base_for_ladder = _raw_chain(base_5m) if base_5m else (_raw_chain(base_sod) if base_sod else None)

    # options_lite + option_ladder (computed once, used by snapshot)
    options_lite = compute_options_lite(raw_now, top_n=top_n)
    option_ladder = compute_option_ladder(raw_now, raw_base_for_ladder, window=ladder_window)

    # per-window blocks
    oi_windows: Dict[str, Any] = {}
    opt_sent_windows: Dict[str, Any] = {}
    fut_sent_windows: Dict[str, Any] = {}

    for key, spec in windows.items():
        if isinstance(spec, str) and spec.upper() == "SOD":
            base = base_sod
            w_start = _sample_time(base) if base else None
            w_end = _sample_time(now_sample)
        else:
            mins = int(spec)
            base = _pick_baseline_for_minutes(day_samples, asof, mins)
            w_end = _sample_time(now_sample)
            w_start = (w_end - timedelta(minutes=mins)) if (w_end and mins) else None

        raw_base = _raw_chain(base) if base else None

        # OI window (rows+totals)
        oi = build_oi_window(raw_now, raw_base, ladder_window=ladder_window)
        if oi is None:
            oi = {
                "atm": None,
                "window": int(ladder_window),
                "rows": [],
                "totals": {
                    "ce_oi": 0,
                    "pe_oi": 0,
                    "ce_oi_chg": None,
                    "pe_oi_chg": None,
                },
            }
        oi_windows[key] = oi

        # Option sentiment
        if raw_base:
            opt_sent_windows[key] = _compute_opt_sent_pair(
                raw_base,
                raw_now,
                asof=asof,
                window_key=key,
                window_start=w_start,
                window_end=w_end,
                atm_window=opt_sent_atm_window,
                notional_floor=opt_sent_notional_floor,
                min_contracts_floor=opt_sent_min_contracts_floor,
            )
        else:
            opt_sent_windows[key] = _empty_opt_sent(key, w_start, w_end, "no_baseline")

        # Future sentiment
        fut_sent_windows[key] = compute_future_sentiment_window(
            raw_base,
            raw_now,
            window_key=key,
            window_start=w_start,
            window_end=w_end,
        )

    return {
        "options_lite": options_lite,
        "option_ladder": option_ladder,
        "oi_windows": oi_windows,
        "option_sentiment_windows": opt_sent_windows,
        "future_sentiment_windows": fut_sent_windows,
    }
