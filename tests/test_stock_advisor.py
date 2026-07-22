#!/usr/bin/env python3
"""Simple StockAdvisor test runner.

Edit TEST SETTINGS and run:

    python tests/test_stock_advisor.py

This is read-only. It does not change signal generation, active flags, trades,
or auditlog. It reads latest/as-of snapshots and writes advisor decisions to CSV.
"""

from __future__ import annotations

import csv
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from configs.stock_advisor_config import STOCK_ADVISOR_CONFIG
from logconfig import setup_logging
from schemas.snapshot import SnapshotSchema
from schemas.symbol import SymbolSchema
from services.selection.stock_advisor import StockAdvisor
from utils.datetime_utils import IST


# =============================================================================
# TEST SETTINGS
# =============================================================================
# None = latest snapshot per active EQ symbol.
# Or set explicitly, for example: "2026-07-10 14:45:00".
TEST_AS_OF: Optional[str] = None

# True = only active EQ symbols. False = enabled EQ universe.
ACTIVE_ONLY: bool = True

# Empty list = all eligible symbols from ACTIVE_ONLY. Or provide symbols.
SYMBOLS: List[str] = []

# Number of rows to print.
PRINT_TOP_N: int = 100
PROGRESS_EVERY: int = 25

# Empty string disables CSV output.
CSV_PATH: str = "stock_advisor_results.csv"

LOG_FILE: str = "test_stock_advisor.log"


# =============================================================================
# Helpers
# =============================================================================
def _as_of() -> Optional[datetime]:
    if not TEST_AS_OF:
        return None
    text = TEST_AS_OF.strip()
    if not text:
        return None
    return datetime.fromisoformat(text).replace(tzinfo=IST)


def _fetch_symbols() -> List[str]:
    if SYMBOLS:
        return [str(s).strip().upper() for s in SYMBOLS if str(s).strip()]

    rows = SymbolSchema.fetch_symbols(
        active=1 if ACTIVE_ONLY else None,
        type_filter="EQ",
    ) or []
    return sorted({str(r.symbol).strip().upper() for r in rows if str(r.symbol).strip()})


def _fetch_snapshot(symbol: str, as_of: Optional[datetime]):
    if as_of is not None:
        return SnapshotSchema.fetch_latest_for_symbol_asof(symbol, as_of)
    return SnapshotSchema.fetch_latest_for_symbol(symbol)


def _write_csv(path: str, rows: List[Dict]) -> None:
    if not path:
        return

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

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
        "failed_breakout_buy_alignment", "failed_breakout_sell_alignment",
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
    extras = sorted({k for row in rows for k in row.keys()} - set(core))
    fieldnames = core + extras

    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _simple_count(rows: List[Dict], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "UNKNOWN")
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

def _print_summary(rows: List[Dict], missing: List[str]) -> None:
    counts: Dict[str, int] = {}
    regimes: Dict[str, int] = {}
    for row in rows:
        counts[row.get("decision") or "UNKNOWN"] = counts.get(row.get("decision") or "UNKNOWN", 0) + 1
        regimes[row.get("regime") or "UNKNOWN"] = regimes.get(row.get("regime") or "UNKNOWN", 0) + 1

    print("\nStockAdvisor test summary")
    print("-" * 100)
    print(f"mode              : {STOCK_ADVISOR_CONFIG.mode}")
    print(f"active_only       : {ACTIVE_ONLY}")
    print(f"test_as_of        : {TEST_AS_OF or 'latest'}")
    print(f"rows              : {len(rows)}")
    print(f"missing_snapshots : {len(missing)}")
    print(f"csv_path          : {CSV_PATH or '(disabled)'}")
    print(f"decision_counts   : {counts}")
    print(f"regime_counts     : {regimes}")
    print(f"stock_contexts    : {_simple_count(rows, 'stock_context')}")
    print(f"vwap_contexts     : {_simple_count(rows, 'vwap_context')}")
    print(f"trend_contexts    : {_simple_count(rows, 'trend_context')}")
    print(f"chop_contexts     : {_simple_count(rows, 'chop_context')}")

    print(f"\nTop {min(PRINT_TOP_N, len(rows))} advisor rows")
    print("-" * 100)
    for row in rows[: max(0, int(PRINT_TOP_N))]:
        print(
            f"{str(row.get('symbol') or ''):14s} "
            f"{str(row.get('snapshot_time') or '')[:19]:19s} "
            f"{str(row.get('decision') or ''):8s} "
            f"{str(row.get('regime') or ''):24s} "
            f"score={float(row.get('tradeability_score') or 0):6.2f} "
            f"pos={float(row.get('range_position') or 0):5.2f} "
            f"day_rng={float(row.get('day_range_pct') or 0):6.3f}% "
            f"rec_rng={float(row.get('recent_range_pct') or 0):6.3f}% "
            f"vwap={float(row.get('vwap_gap_pct') or 0):6.3f}% "
            f"rsi={float(row.get('rsi') or 0):5.1f} "
            f"eligible={str(row.get('eligible_setups') or '')} "
            f"watch={str(row.get('watch_setups') or '')} "
            f"exh={str(row.get('exhaustion_reversal_alignment') or '')} "
            f"fb={str(row.get('failed_breakout_alignment') or '')} "
            f"ab={str(row.get('accepted_breakout_alignment') or '')} "
            f"reason={str(row.get('reason_code') or '')}"
        )

    if missing:
        print("\nMissing snapshots")
        print("-" * 100)
        print(", ".join(missing[:100]))


# =============================================================================
# Main
# =============================================================================
def main() -> None:
    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)

    as_of = _as_of()
    advisor = StockAdvisor()
    symbols = _fetch_symbols()

    rows: List[Dict] = []
    missing: List[str] = []

    total_symbols = len(symbols)
    start_line = (
        f"Starting test_stock_advisor | symbols={total_symbols} | "
        f"as_of={as_of.isoformat() if as_of else 'latest'} | active_only={ACTIVE_ONLY}"
    )
    print(start_line, flush=True)
    logger.info(start_line)

    for idx, symbol in enumerate(symbols, start=1):
        snap = _fetch_snapshot(symbol, as_of)
        if snap is None:
            missing.append(symbol)
            continue
        result = advisor.analyze(snap, recent_snapshots=None)
        rows.append(result.to_dict())

        if PROGRESS_EVERY and (idx % PROGRESS_EVERY == 0 or idx == total_symbols):
            line = (
                f"Processed {idx}/{total_symbols} symbols | "
                f"advisor_rows={len(rows)} | missing={len(missing)}"
            )
            print(line, flush=True)
            logger.info(line)

    rows.sort(key=lambda r: (str(r.get("decision") or ""), -float(r.get("tradeability_score") or 0)))
    _write_csv(CSV_PATH, rows)
    _print_summary(rows, missing)

    logger.info(
        "StockAdvisor test complete | rows=%d | missing=%d | csv=%s",
        len(rows),
        len(missing),
        CSV_PATH or "disabled",
    )


if __name__ == "__main__":
    main()
