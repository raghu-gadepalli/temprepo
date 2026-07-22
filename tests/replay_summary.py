#!/usr/bin/env python3
"""
scripts/replay_summary.py

Full replay/backtest runner for Autotrades.

Purpose
-------
This script regenerates snapshots over a configured time range and then runs the
normal pipeline in the same order used by live services:

    snapshot generation
    -> signal generation
    -> trade generation
    -> executor entry pass
    -> monitor pass
    -> executor exit pass

It intentionally avoids manual trade-state mutations such as CREATED -> READY or
CREATED -> FILLED. Configure the DB/users/config so the normal pipeline can do
its work:

    - virtual/autotrade user logged in and active for replay, or TEST_USERID set
    - replay_summary forces EXECUTION_CONFIG.use_snapshot = True for deterministic replay pricing
    - execution_mode/user preferences set as required by normal trade generation

Use replay_unprocessed_snapshots.py when snapshots already exist and only need to
be reprocessed. Use this file when snapshots must be regenerated.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from database.database import get_trades_db
from logconfig import setup_logging
from models.trade_models import Candle, Signal, Snapshot as SnapshotORM, UserTrade as TradeORM
from schemas.snapshot import SnapshotSchema
from schemas.stock_setup_state import StockSetupStateSchema
from schemas.symbol import SymbolSchema
from services.signals.signal_generator import SignalGenerator
from services.snapshot.snapshot_generator import SnapshotGenerator
from services.trade.executor.trade_executor import TradeExecutor
from services.trade.generator.trade_generator import TradeGenerator
from configs.execution_config import EXECUTION_CONFIG
from services.trade.monitor.trade_monitor import TradeMonitor


# =============================================================================
# CONFIG
# =============================================================================

IST = ZoneInfo("Asia/Kolkata")

# If empty, all active symbols are replayed. Example: ["NUVAMA"]
# SYMBOLS: List[str] = []
SYMBOLS: List[str] = ['ICICIBANK']
SYMBOLS_FETCH_ACTIVE = 1   

# Set a userid to replay only one user, or None to use normal eligible-user flow.
# TEST_USERID: Optional[str] = None
TEST_USERID: Optional[str] = 'DR1812'

# This script regenerates snapshots. Keep True for clean backtests.
CLEAR_PIPELINE_DATA = True
CLEAR_CANDLES = True
CLEAR_SNAPSHOTS = True
CLEAR_SIGNALS = True
CLEAR_USER_TRADES = True

# Keep 1 minute if you want to mimic the service loop. SnapshotGenerator may
# internally persist only valid/new snapshot bars depending on your implementation.
STEP_MINUTES = 3

# Historical replay window.
START = datetime(2026, 7, 16, 9, 18, tzinfo=IST)
END = datetime(2026, 7, 16, 15, 30, tzinfo=IST)

# Prefer environment variables; fallback kept only for compatibility with the
# older script. Replace/remove locally if you do not want credentials in script.
API_KEY = "d17pao9dsc9jsp84"
ACCESS_TOKEN = "x1SLUcxQZF491tUmzoDpcLtKmg2oAxG9"

LOG_FILE = "replay_summary.log"

logger = logging.getLogger(__name__)

job_stats: Dict[str, List[float]] = {
    "snapshots": [],
    "signals": [],
    "trades": [],
    "execute_entry": [],
    "monitor": [],
    "execute_exit": [],
}


# =============================================================================
# Helpers
# =============================================================================

def _enum_str(v: Any) -> str:
    return str(getattr(v, "value", v) or "").strip().upper()


def _selected_symbols() -> List[Any]:
    symbols = SymbolSchema.fetch_symbols(active=SYMBOLS_FETCH_ACTIVE) or []
    wanted = {str(s).strip().upper() for s in SYMBOLS if str(s).strip()}
    if wanted:
        symbols = [s for s in symbols if str(getattr(s, "symbol", "") or "").strip().upper() in wanted]
    return symbols


def clear_old_data() -> None:
    """Wipe replay pipeline data before a clean backtest."""
    if not CLEAR_PIPELINE_DATA:
        logger.info("CLEAR_PIPELINE_DATA=False; not clearing tables")
        return

    with get_trades_db() as db:
        if CLEAR_CANDLES:
            db.query(Candle).delete()
        if CLEAR_SNAPSHOTS:
            db.query(SnapshotORM).delete()
        if CLEAR_SIGNALS:
            db.query(Signal).delete()
        if CLEAR_USER_TRADES:
            db.query(TradeORM).delete()
        db.commit()

    logger.info(
        "Cleared replay data | candles=%s snapshots=%s signals=%s user_trades=%s",
        CLEAR_CANDLES,
        CLEAR_SNAPSHOTS,
        CLEAR_SIGNALS,
        CLEAR_USER_TRADES,
    )


def _count_user_trades() -> int:
    with get_trades_db() as db:
        return db.query(TradeORM).count()


def _pipeline_summary() -> Dict[str, Any]:
    """Small DB summary after replay, useful for quick sanity checks."""
    out: Dict[str, Any] = {
        "trades": 0,
        "entry_status": defaultdict(int),
        "exit_status": defaultdict(int),
        "instrument_type": defaultdict(int),
        "execution_mode": defaultdict(int),
    }

    with get_trades_db() as db:
        rows = db.query(TradeORM).all()

    out["trades"] = len(rows)
    for r in rows:
        out["entry_status"][_enum_str(getattr(r, "entry_status", ""))] += 1
        out["exit_status"][_enum_str(getattr(r, "exit_status", ""))] += 1
        out["instrument_type"][_enum_str(getattr(r, "instrument_type", ""))] += 1
        out["execution_mode"][_enum_str(getattr(r, "execution_mode", ""))] += 1

    return out


def _log_pipeline_summary() -> None:
    s = _pipeline_summary()
    logger.info("=== DB REPLAY OUTPUT SUMMARY ===")
    logger.info("trades=%s", s["trades"])
    for key in ("entry_status", "exit_status", "instrument_type", "execution_mode"):
        logger.info("%s=%s", key, dict(sorted(s[key].items())))
    logger.info("================================")


# =============================================================================
# Jobs
# =============================================================================

def job_generate_snapshots(current_time: datetime) -> int:
    symbols = _selected_symbols()
    logger.info("Snapshot job for %d symbols @ %s", len(symbols), current_time)

    generated = 0
    snapshot_times: Dict[str, int] = defaultdict(int)

    for sym in symbols:
        symbol = str(getattr(sym, "symbol", "") or "").strip()
        token = getattr(sym, "token", None)
        if not symbol or token is None:
            logger.warning("Skipping symbol with missing symbol/token | row=%s", sym)
            continue

        try:
            snap = SnapshotGenerator(
                token=int(token),
                symbol=symbol,
                api_key=API_KEY,
                access_token=ACCESS_TOKEN,
            ).generate_snapshot(
                end_date=current_time,
                persist_snapshot=True,
            )
            if snap is not None:
                generated += 1
                snapshot_times[str(getattr(snap, "snapshot_time", ""))] += 1
        except Exception:
            logger.exception("Snapshot failed for %s @ %s", symbol, current_time)

    if snapshot_times:
        logger.info(
            "Snapshot persisted times @ %s | %s",
            current_time,
            dict(sorted(snapshot_times.items())),
        )

    return generated


def job_generate_signals(current_time: datetime) -> int:
    StockSetupStateSchema.expire_due_states(
        snapshot_time=current_time,
        trading_day=current_time.astimezone(IST).date() if current_time.tzinfo else current_time.date(),
        symbols=[str(getattr(s, "symbol", "") or "").strip().upper() for s in _selected_symbols()],
    )
    snaps = SnapshotSchema.fetch_unprocessed() or []
    if not snaps:
        logger.info("No unprocessed snapshots to signal @ %s", current_time)
        return 0

    snaps = sorted(
        snaps,
        key=lambda s: (str(getattr(s, "snapshot_time", "")), str(getattr(s, "symbol", ""))),
    )

    logger.info("Signaling %d unprocessed snapshots @ %s", len(snaps), current_time)
    processed = 0

    for snap in snaps:
        symbol = getattr(snap, "symbol", "")
        snapshot_time = getattr(snap, "snapshot_time", None)
        try:
            SignalGenerator(snap).generate_signal()
            processed += 1
        except Exception:
            logger.exception("Signal generation failed | %s @ %s", symbol, snapshot_time)
        finally:
            try:
                SnapshotSchema.mark_processed(symbol, snapshot_time)
            except Exception:
                logger.exception("mark_processed failed | %s @ %s", symbol, snapshot_time)

    return processed


def job_generate_trades(current_time: datetime) -> int:
    before = _count_user_trades()

    if TEST_USERID:
        created = TradeGenerator().generate_user_trades(TEST_USERID) or []
    else:
        created = TradeGenerator().generate_user_trades() or []

    after = _count_user_trades()
    logger.info(
        "Trade generation @ %s | returned=%d db_delta=%d total=%d userid=%s",
        current_time,
        len(created),
        after - before,
        after,
        TEST_USERID or "ELIGIBLE_USERS",
    )
    return len(created)


def job_execute_trades(current_time: datetime, label: str) -> int:
    try:
        result = TradeExecutor().execute_all(snapshot_time=current_time)
    except Exception:
        logger.exception("TradeExecutor failed | pass=%s @ %s", label, current_time)
        raise

    count = len(result) if isinstance(result, list) else int(result or 0)
    logger.info("Executor %s complete @ %s | result_count=%d raw=%s", label, current_time, count, result)
    return count


def job_monitor_trades(current_time: datetime) -> int:
    try:
        result = TradeMonitor().monitor(snapshot_time=current_time)
    except Exception:
        logger.exception("TradeMonitor failed @ %s", current_time)
        raise

    count = len(result) if isinstance(result, list) else int(result or 0)
    logger.info("TradeMonitor complete @ %s | updated=%d raw=%s", current_time, count, result)
    return count


# =============================================================================
# Driver
# =============================================================================

def run_replay(start: datetime, end: datetime) -> None:
    clear_old_data()
    replay_symbols = [str(getattr(s, "symbol", "") or "").strip().upper() for s in _selected_symbols()]
    StockSetupStateSchema.delete_for_day(
        trading_day=start.astimezone(IST).date() if start.tzinfo else start.date(),
        symbols=replay_symbols,
    )

    current = start
    t0_all = time.time()
    loops = 0

    while current <= end:
        loops += 1
        logger.info("=== REPLAY @ %s ===", current)

        t0 = time.time()
        n_snap = job_generate_snapshots(current)
        job_stats["snapshots"].append(time.time() - t0)
        logger.info("snapshots: generated_for_symbols=%d elapsed=%.3fs", n_snap, job_stats["snapshots"][-1])

        t0 = time.time()
        n_sig = job_generate_signals(current)
        job_stats["signals"].append(time.time() - t0)
        logger.info("signals: processed_snapshots=%d elapsed=%.3fs", n_sig, job_stats["signals"][-1])

        t0 = time.time()
        n_trades = job_generate_trades(current)
        job_stats["trades"].append(time.time() - t0)
        logger.info("trades: returned_created=%d elapsed=%.3fs", n_trades, job_stats["trades"][-1])

        # Normal pipeline order: executor entry pass before monitor.
        t0 = time.time()
        n_entry = job_execute_trades(current, "entry-pass")
        job_stats["execute_entry"].append(time.time() - t0)
        logger.info("execute_entry: result=%d elapsed=%.3fs", n_entry, job_stats["execute_entry"][-1])

        t0 = time.time()
        n_monitor = job_monitor_trades(current)
        job_stats["monitor"].append(time.time() - t0)
        logger.info("monitor: updated=%d elapsed=%.3fs", n_monitor, job_stats["monitor"][-1])

        # Exit pass after monitor marks exits READY.
        t0 = time.time()
        n_exit = job_execute_trades(current, "exit-pass")
        job_stats["execute_exit"].append(time.time() - t0)
        logger.info("execute_exit: result=%d elapsed=%.3fs", n_exit, job_stats["execute_exit"][-1])

        current += timedelta(minutes=STEP_MINUTES)

    StockSetupStateSchema.expire_due_states(
        snapshot_time=end,
        trading_day=end.astimezone(IST).date() if end.tzinfo else end.date(),
        symbols=replay_symbols,
        reason="SETUP_STATE_EXPIRED_REPLAY_END",
        force_all_active=True,
    )

    logger.info("=== REPLAY COMPLETE | loops=%d elapsed=%.3fs ===", loops, time.time() - t0_all)

    logger.info("=== REPLAY TIMING SUMMARY ===")
    for name, times in job_stats.items():
        if not times:
            continue
        total = sum(times)
        avg = total / len(times)
        logger.info("%s: total=%.3fs avg=%.3fs runs=%d", name, total, avg, len(times))
    logger.info("=============================")

    _log_pipeline_summary()


def main() -> None:
    setup_logging(log_file=LOG_FILE)
    global logger
    logger = logging.getLogger(__name__)

    logger.info(
        "Starting replay_summary | start=%s end=%s step=%dm symbols=%s userid=%s clear=%s",
        START,
        END,
        STEP_MINUTES,
        SYMBOLS or "ACTIVE_SYMBOLS",
        TEST_USERID or "ELIGIBLE_USERS",
        CLEAR_PIPELINE_DATA,
    )

    # Replay must be deterministic.  Force the single pipeline replay switch so
    # executor and monitor both price from snapshots/as-of market time instead
    # of broker quotes or quote timestamps.  Restore previous values when the
    # run completes so importing this module does not permanently alter config.
    old_use_snapshot = EXECUTION_CONFIG.use_snapshot
    old_use_live_price_for_virtual = EXECUTION_CONFIG.use_live_price_for_virtual
    old_force_virtual_for_replay = EXECUTION_CONFIG.force_virtual_for_replay

    EXECUTION_CONFIG.use_snapshot = True
    EXECUTION_CONFIG.use_live_price_for_virtual = False
    EXECUTION_CONFIG.force_virtual_for_replay = True

    logger.info(
        "Replay forced execution config | use_snapshot=%s use_live_price_for_virtual=%s force_virtual_for_replay=%s",
        EXECUTION_CONFIG.use_snapshot,
        EXECUTION_CONFIG.use_live_price_for_virtual,
        EXECUTION_CONFIG.force_virtual_for_replay,
    )

    try:
        run_replay(START, END)
    finally:
        EXECUTION_CONFIG.use_snapshot = old_use_snapshot
        EXECUTION_CONFIG.use_live_price_for_virtual = old_use_live_price_for_virtual
        EXECUTION_CONFIG.force_virtual_for_replay = old_force_virtual_for_replay

    logger.info("Finished replay_summary")


if __name__ == "__main__":
    main()
