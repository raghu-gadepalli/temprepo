#!/usr/bin/env python3
"""Replay StockAdvisor across persisted snapshots.

Edit TEST SETTINGS and run:

    python tests/test_stock_advisor_replay.py

This is read-only. It does not update snapshots, signals, auditlog, trades, or
symbol flags. It writes one Advisor row per snapshot to CSV.
"""

from __future__ import annotations

import csv
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from configs.stock_advisor_config import STOCK_ADVISOR_CONFIG
from database.database import get_trades_db
from logconfig import setup_logging
from models.trade_models import Snapshot as SnapshotORM
from schemas.snapshot import SnapshotSchema
from schemas.symbol import SymbolSchema
from services.selection.stock_advisor import StockAdvisor


# =============================================================================
# TEST SETTINGS
# =============================================================================
# None = latest snapshot date in DB. Or set explicitly, e.g. "2026-07-10".
TEST_DATE: Optional[str] = None

# True = active EQ symbols only. False = enabled EQ universe.
ACTIVE_ONLY: bool = True

# Empty list = all eligible symbols from ACTIVE_ONLY. Or provide symbols.
SYMBOLS: List[str] = []

# Optional intraday time window in HH:MM or HH:MM:SS. None = no filter.
START_TIME: Optional[str] = None
END_TIME: Optional[str] = None

# Optional quick-test cap after symbol/time filtering. None = all matching snapshots.
MAX_RECORDS: Optional[int] = None

# Empty string disables CSV output.
CSV_PATH: str = "stock_advisor_replay.csv"

PRINT_TOP_N: int = 50
PROGRESS_EVERY: int = 250
LOG_FILE: str = "test_stock_advisor_replay.log"


# =============================================================================
# Helpers
# =============================================================================
def _parse_date() -> Optional[datetime]:
    if not TEST_DATE:
        return None
    return datetime.fromisoformat(TEST_DATE.strip()).replace(hour=0, minute=0, second=0, microsecond=0)


def _latest_snapshot_date() -> Optional[datetime]:
    with get_trades_db() as db:
        rec = db.query(SnapshotORM).order_by(SnapshotORM.snapshot_time.desc()).first()
    if not rec or not rec.snapshot_time:
        return None
    ts = rec.snapshot_time
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_time_value(value: Optional[str]) -> Optional[tuple[int, int, int]]:
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


def _fetch_symbols() -> List[str]:
    if SYMBOLS:
        return [str(s).strip().upper() for s in SYMBOLS if str(s).strip()]
    rows = SymbolSchema.fetch_symbols(active=1 if ACTIVE_ONLY else None, type_filter="EQ") or []
    return sorted({str(r.symbol).strip().upper() for r in rows if str(r.symbol).strip()})


def _fetch_snapshot_rows(symbols: List[str], day_start: datetime) -> List[SnapshotORM]:
    start_dt = _combine_time(day_start, START_TIME) or day_start
    end_dt = _combine_time(day_start, END_TIME) or (day_start + timedelta(days=1))
    if end_dt <= start_dt:
        raise ValueError(f"Invalid replay window: START_TIME={START_TIME!r}, END_TIME={END_TIME!r}")

    with get_trades_db() as db:
        q = (
            db.query(SnapshotORM)
            .filter(SnapshotORM.snapshot_time >= start_dt)
            .filter(SnapshotORM.snapshot_time < end_dt)
        )
        if symbols:
            q = q.filter(SnapshotORM.symbol.in_(symbols))
        q = q.order_by(SnapshotORM.snapshot_time.asc(), SnapshotORM.symbol.asc())
        if MAX_RECORDS and int(MAX_RECORDS) > 0:
            q = q.limit(int(MAX_RECORDS))
        rows = q.all()
    return rows


def _fieldnames(rows: Optional[List[Dict]] = None) -> List[str]:
    core = [
        "symbol", "snapshot_time", "decision", "regime", "tradeability_score",
        "stock_context", "volatility_context", "vwap_context", "trend_context",
        "range_context", "chop_context", "attempt_context", "preferred_direction", "avoid_direction",
        "eligible_setups", "watch_setups", "blocked_setups", "reason_code", "reason_codes",
        "mean_reversion_buy_alignment", "mean_reversion_sell_alignment",
        "breakout_buy_alignment", "breakout_sell_alignment",
        "failed_breakout_buy_alignment", "failed_breakout_sell_alignment",
        "exhaustion_reversal_buy_alignment", "exhaustion_reversal_sell_alignment",
        "accepted_breakout_buy_alignment", "accepted_breakout_sell_alignment",
        "close", "day_range_pct", "range_position", "recent_range_pct", "recent_move_pct",
        "recent_move_atr", "move_30m_atr", "move_60m_atr", "vwap_gap_pct", "vwap_side",
        "bb_position", "bb_zone", "rsi", "rsi_zone", "atr_pct", "volume_ratio",
        "hma_state", "hma_strength", "structure_state", "structure_side", "breakout_status",
        "breakout_side", "nearest_level_type", "nearest_level_distance_atr",
        "day_context_snapshot_count", "day_context_vwap_cross_count", "day_context_context_flip_count",
        "day_context_atr_change_pct", "day_context_day_range_recent_growth_pct",
        "day_context_prior_setup_states", "day_context_prior_failed_setup_states",
        "day_context_prior_signals", "day_context_prior_no_mfe_signals", "day_context_prior_fast_invalidations",
        "reason_text",
    ]
    if not rows:
        return core
    extras = sorted({k for row in rows for k in row.keys()} - set(core))
    return core + extras

def _write_csv(path: str, rows: List[Dict]) -> None:
    if not path:
        return
    out = Path(path)
    if out.parent and str(out.parent) not in ("", "."):
        out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_fieldnames(rows), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _count(rows: List[Dict], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "").strip() or "UNKNOWN"
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def _print_summary(rows: List[Dict], missing: int, day_start: datetime) -> None:
    print("\nStockAdvisor replay summary")
    print("-" * 110)
    print(f"date              : {day_start.date()}")
    print(f"active_only       : {ACTIVE_ONLY}")
    print(f"symbols_filter    : {SYMBOLS or '(all eligible)'}")
    print(f"time_window       : {START_TIME or '(start)'} -> {END_TIME or '(end)'}")
    print(f"max_records       : {MAX_RECORDS or '(all)'}")
    print(f"rows              : {len(rows)}")
    print(f"load_failures     : {missing}")
    print(f"csv_path          : {CSV_PATH or '(disabled)'}")
    print(f"decision_counts   : {_count(rows, 'decision')}")
    print(f"regime_counts     : {_count(rows, 'regime')}")
    print(f"exhaustion_align  : {_count(rows, 'exhaustion_reversal_alignment')}")
    print(f"exh_buy_align     : {_count(rows, 'exhaustion_reversal_buy_alignment')}")
    print(f"exh_sell_align    : {_count(rows, 'exhaustion_reversal_sell_alignment')}")
    print(f"failed_align      : {_count(rows, 'failed_breakout_alignment')}")
    print(f"mean_rev_buy      : {_count(rows, 'mean_reversion_buy_alignment')}")
    print(f"mean_rev_sell     : {_count(rows, 'mean_reversion_sell_alignment')}")
    print(f"breakout_buy      : {_count(rows, 'breakout_buy_alignment')}")
    print(f"breakout_sell     : {_count(rows, 'breakout_sell_alignment')}")
    print(f"failed_buy        : {_count(rows, 'failed_breakout_buy_alignment')}")
    print(f"failed_sell       : {_count(rows, 'failed_breakout_sell_alignment')}")
    print(f"vwap_contexts     : {_count(rows, 'vwap_context')}")
    print(f"chop_contexts     : {_count(rows, 'chop_context')}")

    print(f"\nFirst {min(PRINT_TOP_N, len(rows))} rows")
    print("-" * 110)
    for row in rows[: max(0, int(PRINT_TOP_N))]:
        print(
            f"{str(row.get('snapshot_time') or '')[:19]:19s} "
            f"{str(row.get('symbol') or ''):14s} "
            f"{str(row.get('decision') or ''):8s} "
            f"{str(row.get('regime') or ''):25s} "
            f"score={float(row.get('tradeability_score') or 0):6.2f} "
            f"mrB={str(row.get('mean_reversion_buy_alignment') or ''):5s} "
            f"mrS={str(row.get('mean_reversion_sell_alignment') or ''):5s} "
            f"brB={str(row.get('breakout_buy_alignment') or ''):5s} "
            f"brS={str(row.get('breakout_sell_alignment') or ''):5s} "
            f"reason={str(row.get('reason_code') or '')}"
        )


# =============================================================================
# Main
# =============================================================================
def main() -> None:
    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)

    day_start = _parse_date() or _latest_snapshot_date()
    if day_start is None:
        raise RuntimeError("No snapshot date found. Set TEST_DATE or load snapshots first.")

    advisor = StockAdvisor()
    symbols = _fetch_symbols()
    snapshot_rows = _fetch_snapshot_rows(symbols, day_start)

    rows: List[Dict] = []
    load_failures = 0

    total_snapshots = len(snapshot_rows)
    start_line = (
        f"Starting StockAdvisor replay | date={day_start.date()} | "
        f"symbols={len(symbols)} | snapshots={total_snapshots} | "
        f"window={START_TIME or 'start'}->{END_TIME or 'end'} | "
        f"max_records={MAX_RECORDS or 'all'} | csv={CSV_PATH or 'disabled'}"
    )
    print(start_line, flush=True)
    logger.info(start_line)

    for idx, rec in enumerate(snapshot_rows, start=1):
        try:
            if not rec.data:
                load_failures += 1
                continue
            snap = SnapshotSchema.from_db_dict(rec.data)
            result = advisor.analyze(snap, recent_snapshots=None)
            rows.append(result.to_dict())
        except Exception:
            load_failures += 1
            logger.exception("Failed advisor replay row | symbol=%s time=%s", rec.symbol, rec.snapshot_time)

        if PROGRESS_EVERY and (idx % PROGRESS_EVERY == 0 or idx == total_snapshots):
            line = (
                f"Processed {idx}/{total_snapshots} snapshots | "
                f"advisor_rows={len(rows)} | failures={load_failures}"
            )
            print(line, flush=True)
            logger.info(line)

    _write_csv(CSV_PATH, rows)
    _print_summary(rows, load_failures, day_start)
    logger.info("StockAdvisor replay complete | rows=%d | failures=%d | csv=%s", len(rows), load_failures, CSV_PATH)


if __name__ == "__main__":
    main()
