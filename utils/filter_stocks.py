#!/usr/bin/env python3
"""
filter_stocks.py (weekly structural universe builder + universe metrics CSV)

Outputs one CSV with ALL EQ symbols (including < MIN_PRICE) and final enabled + reason.

Disable rule (structural):
    disable if (ATR% < MIN_ATR_PCT) AND (abs(beta) < BETA_ABS_FLOOR)

Reason priority:
  PROTECTED_SKIP
  NO_LTP
  BELOW_MIN_PRICE
  BLACKLIST
  VB_SKIP_NO_TOKEN
  VB_SKIP_NO_VOL_DATA
  VB_SKIP_NO_BETA_DATA
  VB_SKIP_INSUFF_DATA
  VB_FAIL
  LOW_VOL_AND_LOW_ABS_BETA
  ENABLED_OK
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from logconfig import setup_logging
from config import AppConfig
from configs.scanner_config import SCANNER_CONFIG
from utils.universe_policy import universe_blacklist, universe_whitelist
from database.database import get_trades_db
from models.trade_models import Symbol as SymbolORM
from schemas.symbol import SymbolSchema
from schemas.user import UserSchema
from services.zerodha.kiteconnect_service import KiteConnectService

IST = ZoneInfo("Asia/Kolkata")
logger: Optional[logging.Logger] = None

# ---------------- CONFIG ----------------
CONF = SCANNER_CONFIG.filter.model_dump()

LOG_FILE = CONF.get("log_file", "/var/www/Autotrades/scripts/filter_stocks.log")
CSV_FILE = CONF.get("csv_file", "/var/www/Autotrades/scripts/universe_metrics.csv")

MIN_PRICE = float(CONF.get("min_price", 200.0))
BATCH = int(CONF.get("quote_batch", 250))
EXCHANGE = str(CONF.get("exchange", "NSE")).strip().upper()

BLACKLIST = universe_blacklist()
SKIP = universe_whitelist()

RATE_SLEEP = float(CONF.get("rate_sleep_sec", 0.35))

# Vol + Beta
ENABLE_VOL_BETA = bool(CONF.get("enable_vol_beta_filter", True))

# ATR% is computed on intraday bars (minute by default)
VOL_INTERVAL = str(CONF.get("vol_interval", "minute")).strip()
VOL_LOOKBACK_DAYS = int(CONF.get("vol_lookback_days", 21))
ATR_PERIOD = int(CONF.get("atr_period", 14))
MIN_ATR_PCT = float(CONF.get("min_atr_pct", 0.08))  # percent units

# Beta is computed on daily bars vs index
BETA_INDEX_SYMBOL = str(CONF.get("beta_index_symbol", "NIFTY 50")).strip()
# Backward compat: users previously configured "beta_floor"; we now interpret it as ABS(beta) floor.
BETA_ABS_FLOOR = float(CONF.get("beta_abs_floor", CONF.get("beta_floor", 0.65)))
BETA_LOOKBACK_DAYS = int(CONF.get("beta_lookback_days", 60))

VOL_SLEEP = float(CONF.get("vol_rate_sleep_sec", 0.35))
MAX_VOL_SYMBOLS = int(CONF.get("max_symbols_vol", 0))  # 0 = all

# If True, compute ATR/Beta metrics for *all* EQ symbols with token (even if later disabled by price/blacklist).
# Keeps the CSV useful for building the blacklist list over time.
COMPUTE_ALL_METRICS = bool(CONF.get("compute_all_metrics", True))


# ---------------- HELPERS ----------------
def _norm(s: str) -> str:
    return (s or "").strip().upper()


def _batched(items, n):
    batch = []
    for x in items:
        batch.append(x)
        if len(batch) >= n:
            yield batch
            batch = []
    if batch:
        yield batch


def _fetch_all_eq() -> List[SymbolORM]:
    with get_trades_db() as db:
        return db.query(SymbolORM).filter(SymbolORM.type == "EQ").all()


def _fetch_symbol_any(symbol_name: str) -> Optional[SymbolORM]:
    with get_trades_db() as db:
        return db.query(SymbolORM).filter(SymbolORM.symbol == symbol_name).one_or_none()


def _fetch_enabled_map_eq() -> Dict[str, bool]:
    """Read final enabled flags from DB in one pass (avoids stale ORM objects)."""
    with get_trades_db() as db:
        rows = db.query(SymbolORM.symbol, SymbolORM.enabled).filter(SymbolORM.type == "EQ").all()
        return {sym: bool(en) for sym, en in rows}


def _quote_key(r: SymbolORM) -> str:
    sym = _norm(r.symbol)
    ex = _norm(getattr(r, "exchange", None)) or EXCHANGE
    return f"{ex}:{sym}"


def _get_token_int(r: SymbolORM) -> Optional[int]:
    tok = getattr(r, "token", None)
    if not tok:
        return None
    try:
        return int(str(tok).strip())
    except Exception:
        return None


def _bars_to_df(bars: List[dict]) -> Optional[pd.DataFrame]:
    if not bars:
        return None
    df = pd.DataFrame(bars)
    if df.empty or "date" not in df.columns:
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")
    df["d"] = df["date"].dt.date
    return df


def _compute_atr_pct(df: pd.DataFrame, atr_period: int) -> Optional[float]:
    if df is None or df.empty:
        return None
    if not {"high", "low", "close"}.issubset(df.columns):
        return None
    if len(df) < max(atr_period + 5, 50):
        return None

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(span=atr_period, adjust=False).mean()
    atr_pct = (atr / close.replace(0, np.nan)) * 100.0
    atr_pct = atr_pct.iloc[atr_period:].dropna()
    if atr_pct.empty:
        return None
    return float(atr_pct.mean())


def _compute_daily_returns(df: pd.DataFrame) -> Optional[pd.Series]:
    if df is None or df.empty:
        return None
    if "close" not in df.columns or "d" not in df.columns:
        return None
    s = df.groupby("d")["close"].last().astype(float)
    if len(s) < 20:
        return None
    r = np.log(s / s.shift(1)).dropna()
    if r.empty:
        return None
    return r


def _compute_beta(stock_ret: pd.Series, index_ret: pd.Series) -> Optional[float]:
    if stock_ret is None or index_ret is None:
        return None

    joined = pd.concat([stock_ret.rename("s"), index_ret.rename("m")], axis=1).dropna()
    if len(joined) < 20:
        return None

    var_m = joined["m"].var()
    if var_m == 0 or np.isnan(var_m):
        return None

    cov_sm = joined[["s", "m"]].cov().iloc[0, 1]
    if np.isnan(cov_sm):
        return None

    return float(cov_sm / var_m)


def _pct_summary(values: List[float]) -> Optional[Dict[str, float]]:
    if not values:
        return None
    arr = np.array(values, dtype=float)
    arr = arr[~np.isnan(arr)]
    if arr.size == 0:
        return None
    p10, p25, p50, p75, p90 = np.percentile(arr, [10, 25, 50, 75, 90])
    return {
        "p10": float(p10),
        "p25": float(p25),
        "p50": float(p50),
        "p75": float(p75),
        "p90": float(p90),
        "min": float(np.min(arr)),
        "max": float(np.max(arr)),
    }


# ---------------- MAIN ----------------
def main():
    global logger
    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)

    run_ts = datetime.now(IST).replace(microsecond=0).isoformat()

    logger.info(
        "=== filter_stocks start | run_ts=%s | min_price=%.2f | atr_floor=%.3f | abs_beta_floor=%.2f | "
        "vol_interval=%s vol_days=%d | beta_days=%d index=%s | csv=%s ===",
        run_ts, MIN_PRICE, MIN_ATR_PCT, BETA_ABS_FLOOR,
        VOL_INTERVAL, VOL_LOOKBACK_DAYS,
        BETA_LOOKBACK_DAYS, BETA_INDEX_SYMBOL,
        CSV_FILE
    )

    user = UserSchema.fetch_user(AppConfig.DATA_USER)
    if not user:
        logger.error("No DATA_USER %s; abort.", AppConfig.DATA_USER)
        return

    kite = KiteConnectService(api_key=user.apikey, access_token=user.access_token)

    eq_rows = _fetch_all_eq()
    if not eq_rows:
        logger.warning("No EQ symbols in DB")
        return

    # -------- STAGE 0: baseline enable (except protected) --------
    enabled_reset = 0
    for r in eq_rows:
        if _norm(r.symbol) in SKIP:
            continue
        if SymbolSchema.update_symbol(r.symbol, {"enabled": True}) is not None:
            enabled_reset += 1
    logger.info("STAGE0 baseline: enabled=True set for %d EQ symbols (whitelist_protected=%d)", enabled_reset, len(SKIP))

    # -------- STAGE 1: quotes -> prices --------
    key_to_symbol = {_quote_key(r): r.symbol for r in eq_rows}
    keys = list(key_to_symbol.keys())

    ltp_by_symbol: Dict[str, float] = {}
    for batch in _batched(keys, BATCH):
        try:
            data = kite.fetch_quote(batch) or {}
            for k, q in data.items():
                sym = key_to_symbol.get(k)
                if not sym:
                    continue
                lp = (q or {}).get("last_price")
                if lp is not None:
                    ltp_by_symbol[sym] = float(lp)
        except Exception:
            logger.exception("Quote batch failed (%d keys)", len(batch))

        if RATE_SLEEP > 0:
            time.sleep(RATE_SLEEP)

    price_updates = 0
    for sym, ltp in ltp_by_symbol.items():
        if SymbolSchema.update_symbol(sym, {"price": ltp}) is not None:
            price_updates += 1
    logger.info("STAGE1 prices: priced=%d/%d updated=%d", len(ltp_by_symbol), len(eq_rows), price_updates)

    # -------- STAGE 2: disable NO_LTP + below min price --------
    disabled_price = 0
    disabled_price_set = set()

    disabled_no_ltp = 0
    disabled_no_ltp_set = set()

    for r in eq_rows:
        su = _norm(r.symbol)
        if su in SKIP:
            continue

        ltp = ltp_by_symbol.get(r.symbol)
        if ltp is None:
            # Keep the DB consistent with the report: if there's no LTP, disable the symbol.
            SymbolSchema.update_symbol(r.symbol, {"enabled": False})
            disabled_no_ltp += 1
            disabled_no_ltp_set.add(su)
            logger.warning("DISABLE NO_LTP symbol=%s", r.symbol)
            continue

        if ltp < MIN_PRICE:
            SymbolSchema.update_symbol(r.symbol, {"enabled": False})
            disabled_price += 1
            disabled_price_set.add(su)
            logger.info("DISABLE BELOW_MIN_PRICE symbol=%s ltp=%.2f min=%.2f", r.symbol, ltp, MIN_PRICE)

    logger.info("STAGE2 min_price: disabled=%d | no_ltp_disabled=%d", disabled_price, disabled_no_ltp)

    # -------- STAGE 3: disable blacklist --------
    disabled_blacklist = 0
    disabled_blacklist_set = set()
    for r in eq_rows:
        su = _norm(r.symbol)
        if su in SKIP:
            continue
        if su in BLACKLIST:
            SymbolSchema.update_symbol(r.symbol, {"enabled": False})
            disabled_blacklist += 1
            disabled_blacklist_set.add(su)
            logger.info("DISABLE BLACKLIST symbol=%s", r.symbol)

    logger.info("STAGE3 blacklist: disabled=%d", disabled_blacklist)

    # -------- STAGE 4: vol + beta metrics --------
    metrics: Dict[str, dict] = {}  # symbol -> {atr_pct, beta, abs_beta, vb_reason}

    idx_ret = None
    end = datetime.now(IST).replace(second=0, microsecond=0)
    vol_start = end - timedelta(days=VOL_LOOKBACK_DAYS + 3)
    beta_start = end - timedelta(days=BETA_LOOKBACK_DAYS + 10)

    if ENABLE_VOL_BETA:
        idx_row = _fetch_symbol_any(BETA_INDEX_SYMBOL)
        if not idx_row:
            logger.error("BETA index symbol not found: %s", BETA_INDEX_SYMBOL)
        else:
            idx_token = _get_token_int(idx_row)
            if not idx_token:
                logger.error("BETA index token missing/invalid for %s token=%r", BETA_INDEX_SYMBOL, getattr(idx_row, "token", None))
            else:
                try:
                    idx_bars = kite.fetch_historical_data(
                        instrument_token=idx_token,
                        from_date=beta_start,
                        to_date=end,
                        interval="day",
                        oi=False,
                    ) or []
                    idx_df = _bars_to_df(idx_bars)
                    idx_ret = _compute_daily_returns(idx_df) if idx_df is not None else None
                    if idx_ret is None or idx_ret.empty:
                        logger.error("Index returns empty for %s; beta unavailable.", BETA_INDEX_SYMBOL)
                        idx_ret = None
                    else:
                        logger.info("STAGE4 index ready: %s returns_n=%d", BETA_INDEX_SYMBOL, len(idx_ret))
                except Exception:
                    logger.exception("Index historical fetch failed for %s", BETA_INDEX_SYMBOL)
                    idx_ret = None

    processed = 0
    disabled_low_both = 0

    atr_values: List[float] = []
    absbeta_values: List[float] = []

    n_ok = n_atr_low = n_absbeta_low = n_both_low = 0
    min_atr = float("inf")
    min_absbeta = float("inf")

    if ENABLE_VOL_BETA and idx_ret is not None:
        compute_set: List[SymbolORM] = []
        for r in eq_rows:
            tok = _get_token_int(r)
            if tok:
                compute_set.append(r)
            else:
                metrics[r.symbol] = {"atr_pct": None, "beta": None, "abs_beta": None, "vb_reason": "VB_SKIP_NO_TOKEN"}

        if not COMPUTE_ALL_METRICS:
            # Only compute metrics for symbols that survive earlier structural filters (saves API calls).
            compute_set = [
                r for r in compute_set
                if _norm(r.symbol) not in SKIP
                and _norm(r.symbol) not in disabled_no_ltp_set
                and _norm(r.symbol) not in disabled_price_set
                and _norm(r.symbol) not in disabled_blacklist_set
            ]

        if MAX_VOL_SYMBOLS > 0:
            compute_set = compute_set[:MAX_VOL_SYMBOLS]

        logger.info(
            "STAGE4 compute set=%d (max_symbols_vol=%d, compute_all_metrics=%s)",
            len(compute_set), MAX_VOL_SYMBOLS, str(COMPUTE_ALL_METRICS)
        )

        for i, r in enumerate(compute_set, 1):
            sym = r.symbol
            tok = _get_token_int(r)
            if not tok:
                continue

            try:
                # ATR% on intraday bars (minute by default)
                vol_bars = kite.fetch_historical_data(
                    instrument_token=tok,
                    from_date=vol_start,
                    to_date=end,
                    interval=VOL_INTERVAL,
                    oi=False,
                ) or []
                vol_df = _bars_to_df(vol_bars)
                if vol_df is None or vol_df.empty:
                    metrics[sym] = {"atr_pct": None, "beta": None, "abs_beta": None, "vb_reason": "VB_SKIP_NO_VOL_DATA"}
                    continue

                atr_pct = _compute_atr_pct(vol_df, ATR_PERIOD)
                if atr_pct is None:
                    metrics[sym] = {"atr_pct": None, "beta": None, "abs_beta": None, "vb_reason": "VB_SKIP_INSUFF_DATA"}
                    continue

                # Beta on daily bars
                beta_bars = kite.fetch_historical_data(
                    instrument_token=tok,
                    from_date=beta_start,
                    to_date=end,
                    interval="day",
                    oi=False,
                ) or []
                beta_df = _bars_to_df(beta_bars)
                if beta_df is None or beta_df.empty:
                    metrics[sym] = {"atr_pct": atr_pct, "beta": None, "abs_beta": None, "vb_reason": "VB_SKIP_NO_BETA_DATA"}
                    continue

                stock_ret = _compute_daily_returns(beta_df)
                beta = _compute_beta(stock_ret, idx_ret)
                if beta is None:
                    metrics[sym] = {"atr_pct": atr_pct, "beta": None, "abs_beta": None, "vb_reason": "VB_SKIP_INSUFF_DATA"}
                    continue

                abs_beta = abs(beta)

                processed += 1
                metrics[sym] = {"atr_pct": atr_pct, "beta": beta, "abs_beta": abs_beta, "vb_reason": "VB_OK"}

                # stats
                n_ok += 1
                atr_values.append(atr_pct)
                absbeta_values.append(abs_beta)
                min_atr = min(min_atr, atr_pct)
                min_absbeta = min(min_absbeta, abs_beta)

                atr_low = atr_pct < MIN_ATR_PCT
                absbeta_low = abs_beta < BETA_ABS_FLOOR

                if atr_low:
                    n_atr_low += 1
                if absbeta_low:
                    n_absbeta_low += 1
                if atr_low and absbeta_low:
                    n_both_low += 1

                # Apply disable only if not already disabled by earlier rules and not protected
                su = _norm(sym)
                if su not in SKIP and su not in disabled_no_ltp_set and su not in disabled_price_set and su not in disabled_blacklist_set:
                    if atr_low and absbeta_low:
                        SymbolSchema.update_symbol(sym, {"enabled": False})
                        disabled_low_both += 1
                        logger.info(
                            "DISABLE LOW_VOL_AND_LOW_ABS_BETA symbol=%s atr_pct=%.3f(<%.3f) beta=%.2f abs_beta=%.2f(<%.2f)",
                            sym, atr_pct, MIN_ATR_PCT, beta, abs_beta, BETA_ABS_FLOOR
                        )

            except Exception:
                logger.exception("VB_FAIL symbol=%s token=%s", sym, tok)
                metrics[sym] = {"atr_pct": None, "beta": None, "abs_beta": None, "vb_reason": "VB_FAIL"}

            if VOL_SLEEP > 0:
                time.sleep(VOL_SLEEP)

            if i % 25 == 0:
                logger.info("STAGE4 progress: %d/%d", i, len(compute_set))

        if n_ok > 0:
            logger.info(
                "STAGE4 thresholds: atr_floor=%.3f abs_beta_floor=%.2f | ok=%d atr_low=%d abs_beta_low=%d both_low=%d | min_atr=%.3f min_abs_beta=%.2f",
                MIN_ATR_PCT, BETA_ABS_FLOOR, n_ok, n_atr_low, n_absbeta_low, n_both_low, min_atr, min_absbeta
            )
            atr_sum = _pct_summary(atr_values)
            ab_sum = _pct_summary(absbeta_values)
            if atr_sum:
                logger.info(
                    "STAGE4 atr_pct dist: p10=%.3f p25=%.3f p50=%.3f p75=%.3f p90=%.3f min=%.3f max=%.3f",
                    atr_sum["p10"], atr_sum["p25"], atr_sum["p50"],
                    atr_sum["p75"], atr_sum["p90"], atr_sum["min"], atr_sum["max"]
                )
            if ab_sum:
                logger.info(
                    "STAGE4 abs_beta dist: p10=%.2f p25=%.2f p50=%.2f p75=%.2f p90=%.2f min=%.2f max=%.2f",
                    ab_sum["p10"], ab_sum["p25"], ab_sum["p50"],
                    ab_sum["p75"], ab_sum["p90"], ab_sum["min"], ab_sum["max"]
                )
    else:
        if not ENABLE_VOL_BETA:
            logger.info("STAGE4 vol+beta: disabled (enable_vol_beta_filter=false)")
        elif idx_ret is None:
            logger.warning("STAGE4 vol+beta: index returns unavailable; skipping beta-based filtering.")

    # -------- FINAL ENABLED MAP (from DB) --------
    enabled_map = _fetch_enabled_map_eq()

    # ---------------- CSV REPORT (ALL EQ symbols) ----------------
    report_rows: List[dict] = []
    for r in eq_rows:
        sym = r.symbol
        su = _norm(sym)

        price = ltp_by_symbol.get(sym)
        tok = _get_token_int(r)

        m = metrics.get(sym, {})
        atr_pct = m.get("atr_pct")
        beta = m.get("beta")
        abs_beta = m.get("abs_beta")
        vb_reason = m.get("vb_reason")

        enabled_final = "Y" if enabled_map.get(sym, bool(getattr(r, "enabled", True))) else "N"

        # Reason priority (report only; DB already updated by stages)
        reason = "ENABLED_OK"
        if su in SKIP:
            reason = "PROTECTED_SKIP"
        elif su in disabled_no_ltp_set or price is None:
            reason = "NO_LTP"
        elif su in disabled_price_set or (price is not None and price < MIN_PRICE):
            reason = "BELOW_MIN_PRICE"
        elif su in BLACKLIST:
            reason = "BLACKLIST"
        elif ENABLE_VOL_BETA and idx_ret is not None:
            if vb_reason and vb_reason != "VB_OK":
                reason = vb_reason
            else:
                if atr_pct is not None and abs_beta is not None:
                    if (atr_pct < MIN_ATR_PCT) and (abs_beta < BETA_ABS_FLOOR):
                        reason = "LOW_VOL_AND_LOW_ABS_BETA"

        report_rows.append(
            {
                "run_ts": run_ts,
                "symbol": sym,
                "exchange": getattr(r, "exchange", EXCHANGE) or EXCHANGE,
                "token": tok,
                "price": None if price is None else round(float(price), 2),
                "atr_pct": None if atr_pct is None else round(float(atr_pct), 3),
                "beta": None if beta is None else round(float(beta), 2),
                "abs_beta": None if abs_beta is None else round(float(abs_beta), 2),
                "enabled": enabled_final,
                "reason": reason,
                "atr_floor": round(float(MIN_ATR_PCT), 3),
                "abs_beta_floor": round(float(BETA_ABS_FLOOR), 2),
                "vol_interval": VOL_INTERVAL,
                "vol_days": int(VOL_LOOKBACK_DAYS),
                "beta_days": int(BETA_LOOKBACK_DAYS),
            }
        )

    try:
        os.makedirs(os.path.dirname(CSV_FILE), exist_ok=True)
    except Exception:
        pass

    df = pd.DataFrame(report_rows)
    df.to_csv(CSV_FILE, index=False)
    logger.info("CSV written: %s rows=%d", CSV_FILE, len(df))

    logger.info(
        "=== filter_stocks done | disabled_price=%d disabled_no_ltp=%d disabled_blacklist=%d disabled_low_vol_beta=%d whitelist_protected=%d ===",
        disabled_price, disabled_no_ltp, disabled_blacklist, disabled_low_both, len(SKIP)
    )


if __name__ == "__main__":
    main()
