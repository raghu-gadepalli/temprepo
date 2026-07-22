#!/usr/bin/env python3
"""
services/generator/snapshot_generator.py

Snapshot generator with structure block.

Key changes:
- Removed legacy price_action generation.
- Adds structure block.
- Adds state_context block.
- Computes state memory/context using in-memory candle replay.
- Does not require DB read for structure/state counts.
- Keeps derivatives attach unchanged.
"""

from __future__ import annotations

import json
import os
import sys
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, Tuple

import numpy as np
import pandas as pd
from kiteconnect.exceptions import TokenException, InputException

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from configs.snapshot_config import SNAPSHOT_CONFIG
from schemas.snapshot import SnapshotSchema
from schemas.symbol import SymbolSchema
from schemas.derivatives import DerivativesChainSchema

from services.auction_engine.snapshot_adapter import enrich_snapshot_with_auction

from services.snapshot.snapshot_helper import (
    aggregate_ohlcv,
    compute_prev_day_ohlc,
    compute_initial_accepted_range,
    compute_today_open,
    compute_moves,
    compute_envelopes,
    compute_px_vs_vwap_pct,
    compute_opening_range_15m,
    classify_rsi_zone,
    classify_adx_band,
    classify_atr_band,
    compute_volume_metrics,
    build_events,
    compute_bollinger_position_zone,
    compute_structure_state_from_memory,
    build_price_action_block,
    build_market_windows_block,
    build_indicator_windows_block,
    build_state_context_block,
)

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

BASE_INTERVAL = SNAPSHOT_CONFIG.indicators.frequency
LOOKBACK_DAYS = SNAPSHOT_CONFIG.indicators.lookback_days
STRUCTURE_REPLAY_BARS = SNAPSHOT_CONFIG.structure.structure_replay_bars
STRUCTURE_REPLAY_15M_BARS = max(50, int(STRUCTURE_REPLAY_BARS / 5))

HMA_LENGTHS = SNAPSHOT_CONFIG.indicators.hma_lengths
EMA_LENGTHS = SNAPSHOT_CONFIG.indicators.ema_lengths

RSI_PERIOD = SNAPSHOT_CONFIG.indicators.rsi_period
ATR_PERIOD = SNAPSHOT_CONFIG.indicators.atr_period
ADX_PERIOD = SNAPSHOT_CONFIG.indicators.adx_period

BB_PERIOD = SNAPSHOT_CONFIG.indicators.bb_period
BB_STD_MULT = SNAPSHOT_CONFIG.indicators.bb_std_mult

MARKET_OPEN_HHMM: Tuple[int, int] = (9, 15)
MARKET_CLOSE_HHMM: Tuple[int, int] = (15, 30)

TF_PRIMARY_MIN = 3
TF_15_MIN = 15


def _ema(series: pd.Series, span: int) -> pd.Series:
    span = int(span)
    if span <= 0:
        return pd.Series(index=series.index, dtype=float)
    return series.ewm(span=span, adjust=False).mean()


def _wma(series: pd.Series, length: int) -> pd.Series:
    length = int(length)
    if length <= 0:
        return pd.Series(index=series.index, dtype=float)

    weights = np.arange(1, length + 1, dtype=float)

    def _calc(x: np.ndarray) -> float:
        return float(np.dot(x, weights) / weights.sum())

    return series.rolling(length, min_periods=length).apply(_calc, raw=True)


def _hma(series: pd.Series, length: int) -> pd.Series:
    n = int(length)
    if n <= 1:
        return series.astype(float)

    half = max(1, n // 2)
    root = max(1, int(np.sqrt(n)))

    wma_half = _wma(series, half)
    wma_full = _wma(series, n)

    return _wma(2.0 * wma_half - wma_full, root)


def _rsi(close: pd.Series, period: int) -> pd.Series:
    period = int(period)
    if period <= 0:
        return pd.Series(index=close.index, dtype=float)

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    period = int(period)
    if period <= 0:
        return pd.Series(index=df.index, dtype=float)

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    prev_close = close.shift(1)

    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _adx(df: pd.DataFrame, period: int) -> pd.Series:
    period = int(period)
    if period <= 0:
        return pd.Series(index=df.index, dtype=float)

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    prev_close = close.shift(1)

    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    plus_dm_sm = (
        pd.Series(plus_dm, index=df.index)
        .ewm(alpha=1 / period, adjust=False, min_periods=period)
        .mean()
    )

    minus_dm_sm = (
        pd.Series(minus_dm, index=df.index)
        .ewm(alpha=1 / period, adjust=False, min_periods=period)
        .mean()
    )

    plus_di = 100.0 * (plus_dm_sm / atr.replace(0, np.nan))
    minus_di = 100.0 * (minus_dm_sm / atr.replace(0, np.nan))

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)

    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _vwap_session(df: pd.DataFrame) -> pd.Series:
    d = df.copy()

    tp = (
        d["high"].astype(float)
        + d["low"].astype(float)
        + d["close"].astype(float)
    ) / 3.0

    vol = d["volume"].astype(float)

    out = pd.Series(index=d.index, dtype=float)

    for _, g in d.groupby(d["date"].dt.date):
        pv = (tp.loc[g.index] * vol.loc[g.index]).cumsum()
        vv = vol.loc[g.index].cumsum().replace(0, np.nan)
        out.loc[g.index] = pv / vv

    return out


def _bollinger(close: pd.Series, period: int, std_mult: float = 2.0) -> pd.DataFrame:
    period = int(period)

    if period <= 0:
        return pd.DataFrame(
            index=close.index,
            data={
                "upper": np.nan,
                "mid": np.nan,
                "lower": np.nan,
                "bb_width": np.nan,
            },
        )

    mid = close.rolling(period, min_periods=period).mean()
    std = close.rolling(period, min_periods=period).std(ddof=0)

    upper = mid + std_mult * std
    lower = mid - std_mult * std
    bb_width = (upper - lower) / mid.replace(0, np.nan) * 100.0

    return pd.DataFrame(
        {
            "upper": upper,
            "mid": mid,
            "lower": lower,
            "bb_width": bb_width,
        }
    )


class SnapshotFetcher:
    def __init__(self, api_key: str, access_token: str):
        from services.zerodha.kiteconnect_service import KiteConnectService

        self.kite = KiteConnectService(
            api_key=api_key,
            access_token=access_token,
        ).kite

    def fetch(self, token: int, end: datetime, interval: str, lookback_days: int):
        start = end - timedelta(days=lookback_days)

        return self.kite.historical_data(
            instrument_token=token,
            from_date=start,
            to_date=end,
            interval=interval,
            continuous=False,
            oi=False,
        )


class IndicatorComputer:
    @staticmethod
    def add_indicators(df_tf: pd.DataFrame) -> pd.DataFrame:
        d = df_tf.copy()

        d["date"] = pd.to_datetime(d["date"])

        if d["date"].dt.tz is None:
            d["date"] = d["date"].dt.tz_localize(IST)
        else:
            d["date"] = d["date"].dt.tz_convert(IST)

        d["hmafast"] = _hma(d["close"], HMA_LENGTHS.get("hmafast", 45))
        d["hmamid1"] = _hma(d["close"], HMA_LENGTHS.get("hmamid1", 180))
        d["hmamid2"] = _hma(d["close"], HMA_LENGTHS.get("hmamid2", 240))
        d["hmaslow"] = _hma(d["close"], HMA_LENGTHS.get("hmaslow", 360))

        d["ema_fast"] = _ema(d["close"], EMA_LENGTHS.get("ema_fast", 9))
        d["ema_mid1"] = _ema(d["close"], EMA_LENGTHS.get("ema_mid1", 20))
        d["ema_mid2"] = _ema(d["close"], EMA_LENGTHS.get("ema_mid2", 50))
        d["ema_slow"] = _ema(d["close"], EMA_LENGTHS.get("ema_slow", 100))
        d["ema_ref"] = _ema(d["close"], EMA_LENGTHS.get("ema_ref", 200))

        d["vwap"] = _vwap_session(d)

        d["atr"] = _atr(d, ATR_PERIOD)
        d["adx"] = _adx(d, ADX_PERIOD)
        d["rsi"] = _rsi(d["close"], RSI_PERIOD)

        bb = _bollinger(d["close"], period=BB_PERIOD, std_mult=BB_STD_MULT)

        d["bb_upper"] = bb["upper"]
        d["bb_mid"] = bb["mid"]
        d["bb_lower"] = bb["lower"]
        d["bb_width"] = bb["bb_width"]

        return d


def _select_latest_stable(
    df_tf: pd.DataFrame,
    end: datetime,
    *,
    period_minutes: int,
    market_open_hhmm: Tuple[int, int] = MARKET_OPEN_HHMM,
    market_close_hhmm: Tuple[int, int] = MARKET_CLOSE_HHMM,
) -> Optional[pd.Series]:
    """Return the latest completed candle as of ``end``.

    AutoTrades stores candle timestamps as candle *start* times.  A candle that
    starts at 15:27 is available at 15:30, and there should be no separate
    15:30 candle.  Therefore, at exact 3-minute boundaries we select the
    previous candle-start timestamp; at/after market close we cap the stable
    timestamp at close - period.
    """
    if df_tf is None or df_tf.empty:
        return None

    d = df_tf.copy()
    d["date"] = pd.to_datetime(d["date"])

    if d["date"].dt.tz is None:
        d["date"] = d["date"].dt.tz_localize(IST)
    else:
        d["date"] = d["date"].dt.tz_convert(IST)

    d = d.sort_values("date")

    if end.tzinfo is None:
        end = end.replace(tzinfo=IST)
    else:
        end = end.astimezone(IST)

    open_ = end.replace(
        hour=market_open_hhmm[0],
        minute=market_open_hhmm[1],
        second=0,
        microsecond=0,
    )

    close_ = end.replace(
        hour=market_close_hhmm[0],
        minute=market_close_hhmm[1],
        second=0,
        microsecond=0,
    )

    period_minutes = max(1, int(period_minutes))

    if end <= open_:
        subset = d[d["date"] <= open_]
        return subset.iloc[-1] if not subset.empty else None

    if end >= close_:
        stable_end = close_ - timedelta(minutes=period_minutes)
    else:
        minutes_since_open = int((end - open_).total_seconds() // 60)
        if minutes_since_open % period_minutes == 0:
            minutes_since_open -= period_minutes
        else:
            minutes_since_open = (minutes_since_open // period_minutes) * period_minutes
        stable_end = open_ + timedelta(minutes=minutes_since_open)

    if stable_end < open_:
        return None

    subset = d[d["date"] <= stable_end]
    return subset.iloc[-1] if not subset.empty else None

def _derive_hma_state_strength(row: pd.Series) -> Dict[str, str]:
    fast = row.get("hmafast")
    mid1 = row.get("hmamid1")
    mid2 = row.get("hmamid2")
    slow = row.get("hmaslow")

    if any(pd.isna(x) for x in [fast, mid1, mid2, slow]):
        return {"state": "NO_TREND", "strength": "NA"}

    if fast > mid1 and fast > mid2 and fast > slow:
        return {"state": "BUY", "strength": "STRONG_BUY"}
    if fast > mid1 and fast > mid2:
        return {"state": "BUY", "strength": "MEDIUM_BUY"}
    if fast > mid1:
        return {"state": "BUY", "strength": "WEAK_BUY"}

    if fast < mid1 and fast < mid2 and fast < slow:
        return {"state": "SELL", "strength": "STRONG_SELL"}
    if fast < mid1 and fast < mid2:
        return {"state": "SELL", "strength": "MEDIUM_SELL"}
    if fast < mid1:
        return {"state": "SELL", "strength": "WEAK_SELL"}

    return {"state": "NO_TREND", "strength": "NA"}



def _json_dict(value: Any) -> Dict[str, Any]:
    """Best-effort conversion for JSON/dict/model payloads."""
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    if hasattr(value, "model_dump"):
        try:
            parsed = value.model_dump(mode="python")
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    if hasattr(value, "dict"):
        try:
            parsed = value.dict()
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _to_ist_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = pd.to_datetime(value)
        if pd.isna(ts):
            return None
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.tz_localize(IST)
        else:
            ts = ts.tz_convert(IST)
        return ts.to_pydatetime()
    except Exception:
        return None


def _symbol_last_snapshot_payload(sym: Any) -> Dict[str, Any]:
    if not sym:
        return {}
    return _json_dict(getattr(sym, "last_snapshot", None))


def _snapshot_payload_time(payload: Dict[str, Any]) -> Optional[datetime]:
    return _to_ist_datetime((payload or {}).get("snapshot_time"))


def _payload_state_memory(payload: Dict[str, Any]) -> Dict[str, Any]:
    memory = (payload or {}).get("state_memory")
    return memory if isinstance(memory, dict) else {}


def _payload_structure_memory(payload: Dict[str, Any]) -> Dict[str, Any]:
    memory = (payload or {}).get("structure_memory")
    if not isinstance(memory, dict):
        return {}
    if memory.get("schema") != "STRUCTURE_INCREMENTAL_MEMORY_V1":
        return {}
    return memory


def _frame_to_structure_rows(frame: pd.DataFrame, *, limit: int) -> list[Dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    d = frame.copy()
    d["date"] = pd.to_datetime(d["date"])
    if d["date"].dt.tz is None:
        d["date"] = d["date"].dt.tz_localize(IST)
    else:
        d["date"] = d["date"].dt.tz_convert(IST)
    rows: list[Dict[str, Any]] = []
    for _, row in d.sort_values("date").drop_duplicates("date", keep="last").tail(limit).iterrows():
        rows.append({
            "date": pd.to_datetime(row["date"]).isoformat(),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row.get("volume") or 0.0),
        })
    return rows


def _structure_rows_to_frame(rows: Any) -> pd.DataFrame:
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    d = pd.DataFrame(rows)
    if d.empty or "date" not in d.columns:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    d["date"] = pd.to_datetime(d["date"])
    if d["date"].dt.tz is None:
        d["date"] = d["date"].dt.tz_localize(IST)
    else:
        d["date"] = d["date"].dt.tz_convert(IST)
    return d.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


def _append_structure_current(
    previous_rows: Any,
    current_frame: pd.DataFrame,
    *,
    snap_time: datetime,
    limit: int,
) -> pd.DataFrame:
    previous = _structure_rows_to_frame(previous_rows)
    current = pd.DataFrame()
    if current_frame is not None and not current_frame.empty:
        current = current_frame.copy()
        current["date"] = pd.to_datetime(current["date"])
        if current["date"].dt.tz is None:
            current["date"] = current["date"].dt.tz_localize(IST)
        else:
            current["date"] = current["date"].dt.tz_convert(IST)
        current = current[current["date"] <= snap_time].tail(1)
    combined = pd.concat([previous, current], ignore_index=True)
    if combined.empty:
        return combined
    return (
        combined.sort_values("date")
        .drop_duplicates("date", keep="last")
        .tail(limit)
        .reset_index(drop=True)
    )


def _build_structure_memory(
    df3: pd.DataFrame,
    df15: pd.DataFrame,
    *,
    snapshot_time: datetime,
    lookback_bars: int,
) -> Dict[str, Any]:
    return {
        "schema": "STRUCTURE_INCREMENTAL_MEMORY_V1",
        "snapshot_time": snapshot_time.isoformat(),
        "bars_3m": _frame_to_structure_rows(df3, limit=lookback_bars),
        "bars_15m": _frame_to_structure_rows(df15, limit=3),
    }


def _usable_previous_snapshot_for_structure(
    payload: Dict[str, Any],
    snap_time: datetime,
) -> bool:
    """Return whether a previous snapshot can seed structure continuity.

    Use the fast path only when the previous snapshot is from the same trading
    day, is strictly older than the current stable snapshot, has state_memory,
    and is the immediately preceding generator tick. If a service restart or
    missed tick creates a gap, replay the in-memory candles instead so state
    counts/flip counts are not silently skipped.
    """
    if not bool(SNAPSHOT_CONFIG.structure.use_symbol_last_snapshot_for_structure):
        return False

    if not isinstance(payload, dict) or not payload:
        return False

    prev_time = _snapshot_payload_time(payload)
    curr_time = _to_ist_datetime(snap_time)
    if prev_time is None or curr_time is None:
        return False

    if prev_time.date() != curr_time.date():
        return False

    if prev_time >= curr_time:
        return False

    state_memory = _payload_state_memory(payload)
    structure_memory = _payload_structure_memory(payload)
    if not state_memory or not structure_memory:
        return False
    if not structure_memory.get("bars_3m"):
        return False

    max_gap_minutes = float(SNAPSHOT_CONFIG.service.tick_minutes) + 0.5
    gap_minutes = (curr_time - prev_time).total_seconds() / 60.0
    if gap_minutes <= 0 or gap_minutes > max_gap_minutes:
        return False

    return True


def _load_previous_structure_payload(
    *,
    symbol: str,
    snap_time: datetime,
    symbol_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Load previous persisted payload for incremental structure update.

    Prefer symbols.last_snapshot because it is already fetched with the symbol.
    If that cache is not usable, read the most recent earlier snapshot row for
    the same trading day. If neither is immediately usable, return {} and the
    caller will rebuild from in-memory candles.
    """
    if _usable_previous_snapshot_for_structure(symbol_payload, snap_time):
        return symbol_payload

    try:
        db_payload = SnapshotSchema.fetch_latest_today_payload_before_time(
            symbol,
            snap_time.replace(tzinfo=None) if snap_time.tzinfo is not None else snap_time,
        )
    except Exception:
        logger.exception("Failed to fetch previous snapshot payload for %s", symbol)
        db_payload = None

    db_payload = db_payload if isinstance(db_payload, dict) else {}
    if _usable_previous_snapshot_for_structure(db_payload, snap_time):
        return db_payload

    return {}


class SnapshotGenerator:
    def __init__(self, token: int, symbol: str, api_key: str, access_token: str):
        self.token = token
        self.symbol = symbol
        self.fetcher = SnapshotFetcher(
            api_key=api_key,
            access_token=access_token,
        )

    def generate_snapshot(
        self,
        end_date: Optional[datetime] = None,
        persist_snapshot: bool = True,
    ) -> Optional[SnapshotSchema]:
        total_started = time.perf_counter()
        if end_date is None:
            end_date = datetime.now(IST)

        assemble_started = time.perf_counter()
        snap_dict = self._assemble(end_date)
        assemble_ms = (time.perf_counter() - assemble_started) * 1000.0

        if not snap_dict:
            return None

        validation_started = time.perf_counter()
        snapshot = SnapshotSchema.model_validate(snap_dict)
        validation_ms = (time.perf_counter() - validation_started) * 1000.0

        snapshot_write_ms = 0.0
        symbol_cache_write_ms = 0.0
        if persist_snapshot:
            try:
                last_snap_payload = snapshot.model_dump(mode="json", by_alias=True)
            except Exception:
                logger.exception("Failed to serialize snapshot payload for %s", snapshot.symbol)
                last_snap_payload = {}

            write_started = time.perf_counter()
            try:
                # Single idempotent persistence path. create_snapshot already
                # updates an existing (symbol, snapshot_time) row with the full
                # current payload, so a second update/commit is redundant.
                SnapshotSchema.create_snapshot(snapshot)
            except Exception:
                logger.exception("Failed to persist snapshot row for %s", snapshot.symbol)
            snapshot_write_ms = (time.perf_counter() - write_started) * 1000.0

            cache_started = time.perf_counter()
            try:
                ltp_val = getattr(snapshot, "ltp", None)
                if ltp_val is None:
                    ltp_val = getattr(snapshot, "close", None)

                ltp_time_dt = getattr(snapshot, "ltp_time", None)
                if ltp_time_dt is None:
                    ltp_time_dt = getattr(snapshot, "snapshot_time", None)

                SymbolSchema.update_symbol(
                    snapshot.symbol,
                    {
                        "price": ltp_val,
                        "last_time": ltp_time_dt,
                        "last_snapshot": last_snap_payload,
                    },
                )
            except Exception:
                logger.exception("Failed to update symbol cache for %s", snapshot.symbol)
            symbol_cache_write_ms = (time.perf_counter() - cache_started) * 1000.0

        total_ms = (time.perf_counter() - total_started) * 1000.0
        logger.info(
            "snapshot_timing symbol=%s snapshot_time=%s total_ms=%.3f "
            "assemble_ms=%.3f validation_ms=%.3f snapshot_write_ms=%.3f "
            "symbol_cache_write_ms=%.3f auction_ms=%s structure_ms=%s",
            snapshot.symbol,
            snapshot.snapshot_time,
            total_ms,
            assemble_ms,
            validation_ms,
            snapshot_write_ms,
            symbol_cache_write_ms,
            (snapshot.auction.elapsed_ms if snapshot.auction else None),
            (snapshot.generation_diagnostics or {}).get("structure_ms"),
        )
        return snapshot

    def _assemble(self, end: datetime) -> Optional[Dict[str, Any]]:
        assemble_started = time.perf_counter()
        generation_timings: Dict[str, Any] = {}
        fetch_started = time.perf_counter()
        try:
            raw = self.fetcher.fetch(
                self.token,
                end,
                BASE_INTERVAL,
                lookback_days=LOOKBACK_DAYS,
            )
        except (TokenException, InputException):
            raise
        except Exception:
            logger.exception("Historical fetch failed for %s", self.symbol)
            return None
        generation_timings["historical_fetch_ms"] = round(
            (time.perf_counter() - fetch_started) * 1000.0, 3
        )

        market_started = time.perf_counter()
        df1 = pd.DataFrame(raw)

        if df1.empty or "date" not in df1.columns:
            return None

        df1["date"] = pd.to_datetime(df1["date"])

        if df1["date"].dt.tz is None:
            df1["date"] = df1["date"].dt.tz_localize(IST)
        else:
            df1["date"] = df1["date"].dt.tz_convert(IST)

        df1 = df1.sort_values("date")
        base_ohlcv = df1[["date", "open", "high", "low", "close", "volume"]].copy()

        df3 = aggregate_ohlcv(base_ohlcv, minutes=TF_PRIMARY_MIN)
        df15 = aggregate_ohlcv(base_ohlcv, minutes=TF_15_MIN)

        if df3.empty:
            return None

        df3i = IndicatorComputer.add_indicators(df3)
        df3i = df3i.sort_values("date").reset_index(drop=True)

        last = _select_latest_stable(df3i, end, period_minutes=TF_PRIMARY_MIN)

        if last is None:
            return None

        close_now = float(last["close"])

        snap_ts = pd.to_datetime(last["date"])
        if getattr(snap_ts, "tzinfo", None) is None:
            snap_ts = snap_ts.tz_localize(IST)
        else:
            snap_ts = snap_ts.tz_convert(IST)

        snap_time = snap_ts.to_pydatetime()

        prev_day = compute_prev_day_ohlc(base_ohlcv, snap_time)
        initial_accepted_range = compute_initial_accepted_range(base_ohlcv, snap_time)
        day_open = compute_today_open(base_ohlcv, snap_time)
        opening_range = compute_opening_range_15m(base_ohlcv, snap_time)
        prev_close = prev_day["close"] if prev_day else None

        moves = compute_moves(close_now, prev_close, day_open)
        hma_ss = _derive_hma_state_strength(last)

        vwap_val = float(last["vwap"]) if not pd.isna(last.get("vwap")) else None
        px_vs_vwap_pct = compute_px_vs_vwap_pct(close_now, vwap_val)

        boll = {
            "upper": None if pd.isna(last.get("bb_upper")) else float(last.get("bb_upper")),
            "mid": None if pd.isna(last.get("bb_mid")) else float(last.get("bb_mid")),
            "lower": None if pd.isna(last.get("bb_lower")) else float(last.get("bb_lower")),
            "bb_width": None if pd.isna(last.get("bb_width")) else float(last.get("bb_width")),
        }

        bb_extra = compute_bollinger_position_zone(
            close_now,
            boll.get("upper"),
            boll.get("lower"),
        )

        boll["position"] = bb_extra.get("position")
        boll["zone"] = bb_extra.get("zone", "UNKNOWN")

        envelopes = compute_envelopes(last.to_dict())

        df3_stable = (
            df3[df3["date"] <= snap_time].copy()
            if not df3.empty
            else pd.DataFrame()
        )

        df15_stable = (
            df15[df15["date"] <= snap_time].copy()
            if not df15.empty
            else pd.DataFrame()
        )

        levels = {
            "prev_day": prev_day or {},
            "today": {"open": day_open},
            "opening_range": opening_range or {},
        }
        structure_levels = {
            **levels,
            "initial_accepted_range": initial_accepted_range or prev_day or {},
        }

        rsi_val = None if pd.isna(last.get("rsi")) else float(last.get("rsi"))
        adx_val = None if pd.isna(last.get("adx")) else float(last.get("adx"))
        atr_val = None if pd.isna(last.get("atr")) else float(last.get("atr"))

        context = {
            "rsi": {"value": rsi_val, "zone": classify_rsi_zone(rsi_val)},
            "adx": {"value": adx_val, "band": classify_adx_band(adx_val)},
            "atr": {"value": atr_val, "band": classify_atr_band(atr_val, close_now)},
        }

        hma = {
            "fast": None if pd.isna(last.get("hmafast")) else float(last.get("hmafast")),
            "mid1": None if pd.isna(last.get("hmamid1")) else float(last.get("hmamid1")),
            "mid2": None if pd.isna(last.get("hmamid2")) else float(last.get("hmamid2")),
            "slow": None if pd.isna(last.get("hmaslow")) else float(last.get("hmaslow")),
            "state": hma_ss["state"],
            "strength": hma_ss["strength"],
        }

        ema = {
            "fast": None if pd.isna(last.get("ema_fast")) else float(last.get("ema_fast")),
            "mid1": None if pd.isna(last.get("ema_mid1")) else float(last.get("ema_mid1")),
            "mid2": None if pd.isna(last.get("ema_mid2")) else float(last.get("ema_mid2")),
            "slow": None if pd.isna(last.get("ema_slow")) else float(last.get("ema_slow")),
            "ref": None if pd.isna(last.get("ema_ref")) else float(last.get("ema_ref")),
        }

        vwap = {
            "value": vwap_val,
            "px_vs_vwap_pct": px_vs_vwap_pct,
        }

        vol_block = compute_volume_metrics(
            df_tf=df3_stable if not df3_stable.empty else df3,
            df_1m=base_ohlcv,
            asof=snap_time,
        ) or {}

        volume = {
            "bar_volume": vol_block.get("bar_volume"),
            "bar_rvol": vol_block.get("bar_rvol"),
            "bar_rvol_pct": vol_block.get("bar_rvol_pct"),
            "bar_rvol_band": vol_block.get("bar_rvol_band", "NA"),
            "bar_volume_slope": vol_block.get("bar_volume_slope"),
            "today_cum": vol_block.get("today_cum"),
            "prev_day_total": vol_block.get("prev_day_total"),
            "today_vs_prev_ratio": vol_block.get("today_vs_prev_ratio"),
            "periods": vol_block.get("periods") or {},
        }

        bar = {
            "open": None if pd.isna(last.get("open")) else float(last.get("open")),
            "high": None if pd.isna(last.get("high")) else float(last.get("high")),
            "low": None if pd.isna(last.get("low")) else float(last.get("low")),
            "close": close_now,
            "volume": None if pd.isna(last.get("volume")) else float(last.get("volume")),
        }

        price_action = build_price_action_block(
            df_tf=df3_stable if not df3_stable.empty else df3,
            asof=snap_time,
            close=close_now,
            atr=atr_val,
            today_open=day_open,
            opening_range=opening_range or {},
            vwap_value=vwap_val,
        )

        vwap_distance_points = None if vwap_val is None else close_now - vwap_val
        vwap_distance_atr = (
            None
            if vwap_distance_points is None or atr_val in (None, 0)
            else float(vwap_distance_points / atr_val)
        )
        vwap_side = "ABOVE" if (px_vs_vwap_pct or 0) > 0 else ("BELOW" if (px_vs_vwap_pct or 0) < 0 else "AT")
        atr_pct = None if atr_val in (None, 0) or close_now in (None, 0) else float(atr_val / close_now * 100.0)

        indicators = {
            "ema": ema,
            "hma": hma,
            "vwap": {
                "value": vwap_val,
                "side": vwap_side,
                "distance_pct": px_vs_vwap_pct,
                "distance_points": vwap_distance_points,
                "distance_atr": vwap_distance_atr,
            },
            "rsi": context["rsi"],
            "adx": context["adx"],
            "atr": {**context["atr"], "pct": atr_pct},
            "bollinger": boll,
            "envelopes": envelopes,
        }

        market_windows = build_market_windows_block(
            df3_stable if not df3_stable.empty else df3,
            snap_time,
            atr=atr_val,
        )

        def _move_from_market_window(w):
            w = w or {}
            return {
                "status": w.get("status", "na"),
                "session_elapsed_minutes": w.get("session_elapsed_minutes"),
                "points": w.get("move_points"),
                "pct": w.get("move_pct"),
                "atr": w.get("move_atr"),
            }

        price_action["moves"] = {
            k: _move_from_market_window(market_windows.get(k))
            for k in ("15m", "30m", "60m", "sod")
            if k in market_windows
        }

        indicator_windows = build_indicator_windows_block(
            df3i[df3i["date"] <= snap_time].copy(),
            snap_time,
        )
        generation_timings["market_assembly_ms"] = round(
            (time.perf_counter() - market_started) * 1000.0, 3
        )

        structure_started = time.perf_counter()
        sym = None
        last_snapshot_payload: Dict[str, Any] = {}
        try:
            sym = SymbolSchema.fetch_symbol(self.symbol)
            last_snapshot_payload = _symbol_last_snapshot_payload(sym)
        except Exception:
            logger.exception("Failed to fetch symbol cache for %s", self.symbol)

        # ------------------------------------------------------------
        # Structure continuity
        #
        # Fast path:
        # - Use symbols.last_snapshot when it is from the same trading day and
        #   strictly earlier than the current stable snapshot.
        # - This updates state context incrementally with only the current
        #   snapshot, avoiding a replay that grows through the day.
        #
        # Fallback:
        # - Rebuild today's state context by replaying today-only stable candles.
        # - This is used at the first snapshot of the day, after cache clears,
        #   or during historical runs where symbol cache is stale/future.
        # ------------------------------------------------------------
        state_memory: Dict[str, Any] = {}
        structure = None
        prev_for_events = None

        # Range discovery excludes the latest completed candle, so retain one
        # additional row beyond the largest configured historical window.
        lookback_bars = max(
            int(SNAPSHOT_CONFIG.structure.lookback_bars),
            int(SNAPSHOT_CONFIG.structure.max_intraday_range_bars) + 1,
        )

        previous_structure_payload = _load_previous_structure_payload(
            symbol=self.symbol,
            snap_time=snap_time,
            symbol_payload=last_snapshot_payload,
        )
        previous_state_memory = _payload_state_memory(previous_structure_payload)
        previous_structure_memory = _payload_structure_memory(previous_structure_payload)
        use_cached_structure = bool(previous_state_memory and previous_structure_memory)
        structure_df3_used = pd.DataFrame()
        structure_df15_used = pd.DataFrame()

        if use_cached_structure:
            # Advance the bounded candle ring carried by the immediately
            # previous snapshot.  Structure no longer rediscovers its working
            # frame from the freshly fetched session history on every tick.
            structure_df3_used = _append_structure_current(
                previous_structure_memory.get("bars_3m"),
                df3,
                snap_time=snap_time,
                limit=lookback_bars,
            )
            structure_df15_used = _append_structure_current(
                previous_structure_memory.get("bars_15m"),
                df15,
                snap_time=snap_time,
                limit=3,
            )

            current_df3_stable = structure_df3_used
            current_df15_stable = structure_df15_used

            structure, state_memory = compute_structure_state_from_memory(
                px=close_now,
                df3=current_df3_stable if not current_df3_stable.empty else df3_stable,
                df15=current_df15_stable if not current_df15_stable.empty else df15_stable,
                levels=structure_levels,
                atr=atr_val,
                curr_snapshot_like={
                    "hma": hma,
                    "context": context,
                    "bollinger": boll,
                    "vwap": vwap,
                    "volume": vol_block,
                },
                prev_state_memory=previous_state_memory,
            )

            try:
                structure.diagnostics.update({
                    "structure_update_mode": "INCREMENTAL_PREVIOUS_SNAPSHOT",
                    "previous_snapshot_time": (
                        _snapshot_payload_time(previous_structure_payload).isoformat()
                        if _snapshot_payload_time(previous_structure_payload)
                        else None
                    ),
                })
            except Exception:
                logger.exception("Failed to annotate incremental structure diagnostics for %s", self.symbol)

            prev_for_events = {
                "indicators": previous_structure_payload.get("indicators") or {},
                "volume": previous_structure_payload.get("volume") or {},
                "structure": previous_structure_payload.get("structure") or {},
                "state_context": previous_structure_payload.get("state_context") or {},
            }

        else:
            previous_completed_replay_row = None

            replay_df3 = (
                df3i[
                    (df3i["date"] <= snap_time)
                    & (df3i["date"].dt.date == snap_time.date())
                ]
                .copy()
                .reset_index(drop=True)
            )

            replay_df3_base = (
                df3[
                    (df3["date"] <= snap_time)
                    & (df3["date"].dt.date == snap_time.date())
                ]
                .copy()
                .reset_index(drop=True)
                if not df3.empty
                else pd.DataFrame()
            )

            replay_df15_base = (
                df15[
                    (df15["date"] <= snap_time)
                    & (df15["date"].dt.date == snap_time.date())
                ]
                .copy()
                .reset_index(drop=True)
                if not df15.empty
                else pd.DataFrame()
            )

            for i, (_, row) in enumerate(replay_df3.iterrows()):
                row_close = float(row["close"])

                row_date = pd.to_datetime(row.get("date"))
                if getattr(row_date, "tzinfo", None) is None:
                    row_date = row_date.tz_localize(IST)
                else:
                    row_date = row_date.tz_convert(IST)
                row_time = row_date.to_pydatetime()

                row_hma_ss = _derive_hma_state_strength(row)

                row_hma = {
                    "fast": None if pd.isna(row.get("hmafast")) else float(row.get("hmafast")),
                    "mid1": None if pd.isna(row.get("hmamid1")) else float(row.get("hmamid1")),
                    "mid2": None if pd.isna(row.get("hmamid2")) else float(row.get("hmamid2")),
                    "slow": None if pd.isna(row.get("hmaslow")) else float(row.get("hmaslow")),
                    "state": row_hma_ss["state"],
                    "strength": row_hma_ss["strength"],
                }

                row_vwap_val = None if pd.isna(row.get("vwap")) else float(row.get("vwap"))

                row_vwap = {
                    "value": row_vwap_val,
                    "px_vs_vwap_pct": compute_px_vs_vwap_pct(row_close, row_vwap_val),
                }

                row_boll = {
                    "upper": None if pd.isna(row.get("bb_upper")) else float(row.get("bb_upper")),
                    "mid": None if pd.isna(row.get("bb_mid")) else float(row.get("bb_mid")),
                    "lower": None if pd.isna(row.get("bb_lower")) else float(row.get("bb_lower")),
                    "bb_width": None if pd.isna(row.get("bb_width")) else float(row.get("bb_width")),
                }

                row_bb_extra = compute_bollinger_position_zone(
                    row_close,
                    row_boll.get("upper"),
                    row_boll.get("lower"),
                )

                row_boll["position"] = row_bb_extra.get("position")
                row_boll["zone"] = row_bb_extra.get("zone", "UNKNOWN")

                row_rsi = None if pd.isna(row.get("rsi")) else float(row.get("rsi"))
                row_adx = None if pd.isna(row.get("adx")) else float(row.get("adx"))
                row_atr = None if pd.isna(row.get("atr")) else float(row.get("atr"))

                row_context = {
                    "rsi": {"value": row_rsi, "zone": classify_rsi_zone(row_rsi)},
                    "adx": {"value": row_adx, "band": classify_adx_band(row_adx)},
                    "atr": {"value": row_atr, "band": classify_atr_band(row_atr, row_close)},
                }

                bounded_start = max(0, i + 1 - lookback_bars)

                replay_df3_stable = (
                    replay_df3_base.iloc[bounded_start : i + 1]
                    if not replay_df3_base.empty
                    else df3_stable
                )

                replay_df15_stable = (
                    replay_df15_base[replay_df15_base["date"] <= row_time].tail(3)
                    if not replay_df15_base.empty and "date" in replay_df15_base.columns
                    else df15_stable
                )

                row_levels = {
                    "prev_day": prev_day or {},
                    "initial_accepted_range": initial_accepted_range or prev_day or {},
                    "today": {"open": day_open},
                    "opening_range": compute_opening_range_15m(base_ohlcv, row_time) or {},
                }

                row_vol_block = {
                    "bar_volume": None if pd.isna(row.get("volume")) else float(row.get("volume")),
                    "bar_rvol": None,
                    "bar_rvol_pct": None,
                    "bar_rvol_band": "NA",
                    "bar_volume_slope": None,
                    "today_cum": None,
                    "prev_day_total": None,
                    "today_vs_prev_ratio": None,
                }

                new_structure, new_state_memory = compute_structure_state_from_memory(
                    px=row_close,
                    df3=replay_df3_stable if not replay_df3_stable.empty else df3_stable,
                    df15=replay_df15_stable if not replay_df15_stable.empty else df15_stable,
                    levels=row_levels,
                    atr=row_atr,
                    curr_snapshot_like={
                        "hma": row_hma,
                        "context": row_context,
                        "bollinger": row_boll,
                        "vwap": row_vwap,
                        "volume": row_vol_block,
                    },
                    prev_state_memory=state_memory,
                )

                row_vwap_gap = row_vwap.get("px_vs_vwap_pct")
                row_indicators = {
                    "hma": row_hma,
                    "rsi": row_context["rsi"],
                    "adx": row_context["adx"],
                    "atr": row_context["atr"],
                    "bollinger": row_boll,
                    "vwap": {
                        "value": row_vwap_val,
                        "side": "ABOVE" if (row_vwap_gap or 0) > 0 else ("BELOW" if (row_vwap_gap or 0) < 0 else "AT"),
                        "distance_pct": row_vwap_gap,
                    },
                }
                current_completed_replay_row = {
                    "indicators": row_indicators,
                    "volume": row_vol_block,
                    "structure": new_structure.model_dump(mode="python"),
                }

                prev_for_events = previous_completed_replay_row
                previous_completed_replay_row = current_completed_replay_row

                structure = new_structure
                state_memory = new_state_memory

        if structure is None:
            structure, state_memory = compute_structure_state_from_memory(
                px=close_now,
                df3=df3_stable if not df3_stable.empty else df3,
                df15=df15_stable if not df15_stable.empty else df15,
                levels=structure_levels,
                atr=atr_val,
                curr_snapshot_like={
                    "hma": hma,
                    "context": context,
                    "bollinger": boll,
                    "vwap": vwap,
                    "volume": vol_block,
                },
                prev_state_memory={},
            )

        if structure_df3_used.empty:
            structure_df3_used = (
                df3[
                    (df3["date"] <= snap_time)
                    & (df3["date"].dt.date == snap_time.date())
                ]
                .tail(lookback_bars)
                .reset_index(drop=True)
                if not df3.empty
                else df3_stable
            )
        if structure_df15_used.empty:
            structure_df15_used = (
                df15[
                    (df15["date"] <= snap_time)
                    & (df15["date"].dt.date == snap_time.date())
                ]
                .tail(3)
                .reset_index(drop=True)
                if not df15.empty
                else df15_stable
            )
        structure_memory = _build_structure_memory(
            structure_df3_used,
            structure_df15_used,
            snapshot_time=snap_time,
            lookback_bars=lookback_bars,
        )

        try:
            structure.diagnostics.setdefault(
                "structure_update_mode",
                "FULL_SESSION_REPLAY" if not use_cached_structure else "INCREMENTAL_PREVIOUS_SNAPSHOT",
            )
            structure.diagnostics.setdefault(
                "state_memory_key_count",
                len(state_memory or {}),
            )
        except Exception:
            logger.exception("Failed to annotate structure diagnostics for %s", self.symbol)

        state_context = build_state_context_block(
            state_memory,
            structure,
            current_volume_band=volume.get("bar_rvol_band"),
        )
        generation_timings["structure_ms"] = round(
            (time.perf_counter() - structure_started) * 1000.0, 3
        )

        curr_for_events = {
            "indicators": indicators,
            "volume": volume,
            "structure": structure.model_dump(mode="python"),
            "state_context": state_context,
        }

        events = build_events(curr_for_events, prev_for_events)

        derivatives_started = time.perf_counter()
        derivatives_spot_price = None
        derivatives_future = None
        derivatives_options_lite = None
        derivatives_option_ladder = None
        options_sentiment_windows = None
        future_sentiment_windows = None

        def _as_dict(x):
            if not x:
                return {}
            if isinstance(x, str):
                try:
                    return json.loads(x) or {}
                except Exception:
                    return {}
            return x if isinstance(x, dict) else {}

        try:
            if sym is None:
                sym = SymbolSchema.fetch_symbol(self.symbol)

            equity_ref = (
                self.symbol
                if (sym and getattr(sym, "type", None) == "EQ")
                else getattr(sym, "equity_ref", None)
            )

            if equity_ref:
                asof = snap_time.astimezone(IST).replace(
                    tzinfo=None,
                    second=0,
                    microsecond=0,
                )

                chain_obj = DerivativesChainSchema.fetch_latest_today_for_symbol_before_time(
                    equity_ref,
                    asof,
                )

                if chain_obj:
                    raw_chain = _as_dict(getattr(chain_obj, "raw", None))
                    derived = _as_dict(getattr(chain_obj, "derived", None))

                    derivatives_spot_price = raw_chain.get("spot_price")

                    fut = raw_chain.get("future")
                    derivatives_future = fut if isinstance(fut, dict) else None

                    opt_lite = derived.get("options_lite")
                    derivatives_options_lite = opt_lite if isinstance(opt_lite, dict) else None

                    ladder = derived.get("option_ladder")
                    derivatives_option_ladder = ladder if isinstance(ladder, dict) else None

                    opt_sent = derived.get("option_sentiment_windows")
                    options_sentiment_windows = opt_sent if isinstance(opt_sent, dict) else None

                    fut_sent = derived.get("future_sentiment_windows")
                    future_sentiment_windows = fut_sent if isinstance(fut_sent, dict) else None

        except Exception:
            logger.exception("Derivatives attach failed for %s", self.symbol)
        generation_timings["derivatives_ms"] = round(
            (time.perf_counter() - derivatives_started) * 1000.0, 3
        )

        try:
            gen_signals = bool(getattr(sym, "generate_signals", False)) if sym else False
        except Exception:
            logger.exception("Failed to fetch gen_signals for %s", self.symbol)
            gen_signals = False

        snap = {
            "symbol": self.symbol,
            "snapshot_time": snap_time,
            "tf": "3m",
            "close": close_now,
            "bar": bar,
            "gen_signals": gen_signals,
            "levels": levels,
            "indicators": indicators,
            "volume": volume,
            "market_windows": market_windows,
            "indicator_windows": indicator_windows,
            "price_action": price_action,
            "structure": structure.model_dump(mode="python"),
            "state_context": state_context,
            "state_memory": state_memory,
            "structure_memory": structure_memory,
            "events": events,
            "derivatives": {
                "spot_price": derivatives_spot_price,
                "future": derivatives_future,
                "options_lite": derivatives_options_lite,
                "option_ladder": derivatives_option_ladder,
                "option_sentiment_windows": options_sentiment_windows,
                "future_sentiment_windows": future_sentiment_windows,
            },
            "generation_diagnostics": generation_timings,
        }

        # ------------------------------------------------------------
        # Pure Auction Engine embedded directly in the snapshot.
        #
        # The same immediately previous snapshot already loaded for structure
        # continuity also advances the Auction Engine. No separate checkpoint,
        # opportunity-table write, Advisor call, signal lookup, or processed
        # acknowledgement occurs during snapshot assembly.
        # ------------------------------------------------------------
        auction_started = time.perf_counter()
        snap["auction"] = enrich_snapshot_with_auction(
            snap,
            previous_payload=previous_structure_payload,
        )
        generation_timings["auction_adapter_ms"] = round(
            (time.perf_counter() - auction_started) * 1000.0, 3
        )
        generation_timings["assemble_total_ms"] = round(
            (time.perf_counter() - assemble_started) * 1000.0, 3
        )
        snap["generation_diagnostics"] = generation_timings

        return snap
