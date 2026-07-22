#!/usr/bin/env python3
"""Review StockAdvisor context at persisted signal times.

Edit TEST SETTINGS and run:

    python tests/test_stock_advisor_signal_review.py

This is read-only. It reads signals, snapshots, and user_trades directly from
DB, recomputes Advisor at each signal's entry/evaluation snapshot, and writes
one row per signal.  It does not depend on exported signal/trade CSVs.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from configs.stock_advisor_config import STOCK_ADVISOR_CONFIG
from logconfig import setup_logging
from schemas.signal import SignalSchema
from schemas.snapshot import SnapshotSchema
from schemas.user_trade import UserTradeSchema
from services.selection.stock_advisor import StockAdvisor


# =============================================================================
# TEST SETTINGS
# =============================================================================
# None = latest signal date in DB. Or set explicitly, e.g. "2026-07-10".
TEST_DATE: Optional[str] = None

OUTPUT_CSV_PATH: str = "stock_advisor_signal_review.csv"

# Empty list = all symbols/setups. Examples: ["VEDL", "BHEL"], ["EXHAUSTION_REVERSAL"].
SYMBOL_FILTER: List[str] = []
SETUP_FILTER: List[str] = []

# Optional signal-time window in HH:MM or HH:MM:SS. None = full selected date.
START_TIME: Optional[str] = None
END_TIME: Optional[str] = None

# Optional quick-test cap after DB filters. None = all matching signals.
MAX_RECORDS: Optional[int] = None

PRINT_TOP_N: int = 100
PROGRESS_EVERY: int = 25
LOG_FILE: str = "test_stock_advisor_signal_review.log"


# =============================================================================
# Helpers
# =============================================================================
def _json_loads(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    s = str(value or "").strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return {}
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    s = str(value or "").strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return None
    try:
        return datetime.fromisoformat(s.replace("T", " ").split("+")[0]).replace(tzinfo=None)
    except Exception:
        return None


def _parse_date_start() -> Optional[datetime]:
    if not TEST_DATE:
        return None
    return datetime.fromisoformat(str(TEST_DATE).strip()).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


def _latest_signal_date_start() -> Optional[datetime]:
    latest = SignalSchema.fetch_latest_signal_snapshot_time()
    if latest is None:
        return None
    latest = _parse_dt(latest)
    return latest.replace(hour=0, minute=0, second=0, microsecond=0) if latest else None


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


def _signal_time(sig: Any) -> Optional[datetime]:
    return (
        _parse_dt(_value(sig, "qualified_time"))
        or _parse_dt(_value(sig, "actionable_time"))
        or _parse_dt(_value(sig, "last_snapshot_time"))
        or _parse_dt(_value(sig, "first_seen_time"))
        or _parse_dt(_value(sig, "last_eval_time"))
    )


def _num(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        if hasattr(value, "value"):
            value = value.value
        s = str(value).strip()
        if not s or s.lower() in ("nan", "none", "null"):
            return 0.0
        return float(s)
    except Exception:
        return 0.0


def _entry_snapshot_from_signal(sig: Any) -> Optional[SnapshotSchema]:
    """Load the signal-time snapshot from snapshots table first.

    Signal JSON is only a fallback for old/incomplete replay rows.  The normal
    path is snapshots(symbol, snapshot_time), which avoids parsing wide exported
    CSV fields and keeps the review DB-backed.
    """
    ts = _parse_dt(_value(sig, "last_snapshot_time")) or _signal_time(sig)
    symbols = []
    for raw in (_value(sig, "equity_ref"), _value(sig, "symbol")):
        symbol = str(raw or "").strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)

    if ts is not None:
        for symbol in symbols:
            snap = SnapshotSchema.fetch_snapshot(symbol, ts)
            if snap is not None:
                return snap

    meta = _json_loads(_value(sig, "meta_json"))
    entry_snapshot = meta.get("entry_snapshot_json")
    if isinstance(entry_snapshot, str):
        entry_snapshot = _json_loads(entry_snapshot)
    if isinstance(entry_snapshot, dict) and entry_snapshot:
        return SnapshotSchema.from_db_dict(entry_snapshot)

    snap_json = _json_loads(_value(sig, "snapshot_json"))
    if snap_json:
        return SnapshotSchema.from_db_dict(snap_json)
    return None


def _trade_summary(rows: List[Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        sid = str(_value(row, "signal_id") or "").strip()
        if not sid:
            continue
        rec = out.setdefault(sid, {"trade_rows": 0, "users": set(), "pnl_sum": 0.0, "exit_reasons": set()})
        rec["trade_rows"] += 1
        userid = _value(row, "userid")
        if userid:
            rec["users"].add(str(userid))
        rec["pnl_sum"] += _num(_value(row, "exit_pnl") or _value(row, "last_pnl_value") or _value(row, "last_pnl"))
        exit_reason = _value(row, "exit_reason")
        if exit_reason:
            rec["exit_reasons"].add(str(exit_reason))

    for rec in out.values():
        rec["users"] = ",".join(sorted(rec.get("users") or []))
        rec["exit_reasons"] = ",".join(sorted(rec.get("exit_reasons") or []))
    return out


def _fieldnames(rows: Optional[List[Dict]] = None) -> List[str]:
    core = [
        "signal_id", "symbol", "setup", "side", "signal_time", "created_price", "stage", "status", "status_reason",
        "mfe_pct", "mae_pct", "advisor_decision", "advisor_regime", "advisor_score",
        "stock_context", "volatility_context", "vwap_context", "trend_context", "range_context",
        "chop_context", "attempt_context", "preferred_direction", "avoid_direction",
        "advisor_family_key", "family_alignment", "family_alignment_score", "family_alignment_reason",
        "advisor_gate_allows_with_current_config",
        "mean_reversion_buy_alignment", "mean_reversion_sell_alignment",
        "breakout_buy_alignment", "breakout_sell_alignment",
        "failed_breakout_buy_alignment", "failed_breakout_sell_alignment",
        "advisor_reason_code", "advisor_reason_text", "advisor_reason_codes",
        "close", "day_range_pct", "range_position", "recent_range_pct", "recent_move_atr", "vwap_gap_pct",
        "day_context_snapshot_count", "day_context_vwap_cross_count", "day_context_context_flip_count",
        "day_context_atr_change_pct", "day_context_day_range_recent_growth_pct",
        "day_context_prior_setup_states", "day_context_prior_failed_setup_states",
        "day_context_prior_signals", "day_context_prior_no_mfe_signals", "day_context_prior_fast_invalidations",
        "bb_position", "rsi", "nearest_level_type", "nearest_level_distance_atr",
        "trade_rows", "trade_users", "trade_pnl_sum", "trade_exit_reasons",
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
        v = str(row.get(key) or "").strip() or "UNKNOWN"
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


# =============================================================================
# Main
# =============================================================================
def main() -> None:
    setup_logging(log_file=LOG_FILE)
    logger = logging.getLogger(__name__)

    day_start = _parse_date_start() or _latest_signal_date_start()
    if day_start is None:
        raise RuntimeError("No signal rows found. Run replay/signal generation first, or set TEST_DATE.")

    start_dt = _combine_time(day_start, START_TIME) or day_start
    end_dt = _combine_time(day_start, END_TIME) or (day_start + timedelta(days=1))
    if end_dt <= start_dt:
        raise ValueError(f"Invalid review window: START_TIME={START_TIME!r}, END_TIME={END_TIME!r}")

    signals = SignalSchema.fetch_for_advisor_review(
        start_time=start_dt,
        end_time=end_dt,
        symbols=SYMBOL_FILTER,
        setups=SETUP_FILTER,
        limit=MAX_RECORDS,
    )
    advisor = StockAdvisor()
    rows: List[Dict] = []
    failures = 0

    total_signals = len(signals)
    start_line = (
        f"Starting StockAdvisor signal review | source=db | date={day_start.date()} | "
        f"signals={total_signals} | symbols={SYMBOL_FILTER or 'all'} | setups={SETUP_FILTER or 'all'} | "
        f"window={START_TIME or 'start'}->{END_TIME or 'end'} | "
        f"max_records={MAX_RECORDS or 'all'} | output={OUTPUT_CSV_PATH}"
    )
    print(start_line, flush=True)
    logger.info(start_line)

    for idx, sig in enumerate(signals, start=1):
        setup = str(_value(sig, "setup") or "").strip().upper()
        try:
            snap = _entry_snapshot_from_signal(sig)
            if snap is None:
                failures += 1
                logger.warning(
                    "No signal-time snapshot found | signal_id=%s symbol=%s time=%s",
                    _value(sig, "signal_id"),
                    _value(sig, "symbol"),
                    _value(sig, "last_snapshot_time"),
                )
                continue
            result = advisor.analyze(snap, recent_snapshots=None)
            r = result.to_dict()
            side = str(_text(_value(sig, "side"))).strip().upper()
            advisor_alignment = result.alignment_for(setup, side)
            family_key = f"{advisor_alignment.setup}_{side}" if side else advisor_alignment.setup
            signal_ts = _signal_time(sig)
            signal_time = signal_ts.isoformat(sep=" ") if signal_ts else ""
            signal_id = str(_value(sig, "signal_id") or "").strip()
            trade_rows = UserTradeSchema.fetch_for_signal_id(signal_id)
            trade = _trade_summary(trade_rows).get(signal_id, {})

            rows.append({
                "signal_id": signal_id,
                "symbol": str(_value(sig, "symbol") or _value(sig, "equity_ref") or ""),
                "setup": setup,
                "side": side,
                "signal_time": signal_time,
                "created_price": _value(sig, "created_price"),
                "stage": str(_text(_value(sig, "stage"))).upper(),
                "status": str(_text(_value(sig, "status"))).upper(),
                "status_reason": _value(sig, "status_reason"),
                "mfe_pct": _value(sig, "max_pnl"),
                "mae_pct": _value(sig, "min_pnl"),
                "advisor_decision": r.get("decision"),
                "advisor_regime": r.get("regime"),
                "advisor_score": r.get("tradeability_score"),
                "stock_context": r.get("stock_context"),
                "volatility_context": r.get("volatility_context"),
                "vwap_context": r.get("vwap_context"),
                "trend_context": r.get("trend_context"),
                "range_context": r.get("range_context"),
                "chop_context": r.get("chop_context"),
                "attempt_context": r.get("attempt_context"),
                "preferred_direction": r.get("preferred_direction"),
                "avoid_direction": r.get("avoid_direction"),
                "advisor_family_key": family_key,
                "family_alignment": advisor_alignment.alignment,
                "family_alignment_score": round(float(advisor_alignment.score or 0.0), 2),
                "family_alignment_reason": advisor_alignment.reason_code,
                "family_alignment_reason_text": advisor_alignment.reason_text,
                "advisor_gate_allows_with_current_config": result.is_setup_allowed(setup, side, allow_watch=STOCK_ADVISOR_CONFIG.allow_setup_watch),
                "mean_reversion_buy_alignment": r.get("mean_reversion_buy_alignment"),
                "mean_reversion_sell_alignment": r.get("mean_reversion_sell_alignment"),
                "breakout_buy_alignment": r.get("breakout_buy_alignment"),
                "breakout_sell_alignment": r.get("breakout_sell_alignment"),
                "failed_breakout_buy_alignment": r.get("failed_breakout_buy_alignment"),
                "failed_breakout_sell_alignment": r.get("failed_breakout_sell_alignment"),
                "advisor_reason_code": advisor_alignment.reason_code,
                "advisor_reason_text": advisor_alignment.reason_text,
                "advisor_reason_codes": r.get("reason_codes"),
                "close": r.get("close"),
                "day_range_pct": r.get("day_range_pct"),
                "range_position": r.get("range_position"),
                "recent_range_pct": r.get("recent_range_pct"),
                "recent_move_atr": r.get("recent_move_atr"),
                "vwap_gap_pct": r.get("vwap_gap_pct"),
                "bb_position": r.get("bb_position"),
                "rsi": r.get("rsi"),
                "nearest_level_type": r.get("nearest_level_type"),
                "nearest_level_distance_atr": r.get("nearest_level_distance_atr"),
                "day_context_snapshot_count": r.get("day_context_snapshot_count"),
                "day_context_vwap_cross_count": r.get("day_context_vwap_cross_count"),
                "day_context_context_flip_count": r.get("day_context_context_flip_count"),
                "day_context_atr_change_pct": r.get("day_context_atr_change_pct"),
                "day_context_day_range_recent_growth_pct": r.get("day_context_day_range_recent_growth_pct"),
                "day_context_prior_setup_states": r.get("day_context_prior_setup_states"),
                "day_context_prior_failed_setup_states": r.get("day_context_prior_failed_setup_states"),
                "day_context_prior_signals": r.get("day_context_prior_signals"),
                "day_context_prior_no_mfe_signals": r.get("day_context_prior_no_mfe_signals"),
                "day_context_prior_fast_invalidations": r.get("day_context_prior_fast_invalidations"),
                "trade_rows": trade.get("trade_rows", 0),
                "trade_users": trade.get("users", ""),
                "trade_pnl_sum": round(float(trade.get("pnl_sum", 0.0)), 2),
                "trade_exit_reasons": trade.get("exit_reasons", ""),
            })
        except Exception:
            failures += 1
            logger.exception("Failed advisor signal review row | signal_id=%s", _value(sig, "signal_id"))

        if PROGRESS_EVERY and (idx % PROGRESS_EVERY == 0 or idx == total_signals):
            line = (
                f"Processed {idx}/{total_signals} signals | "
                f"review_rows={len(rows)} | failures={failures}"
            )
            print(line, flush=True)
            logger.info(line)

    _write_csv(OUTPUT_CSV_PATH, rows)

    print("\nStockAdvisor signal review")
    print("-" * 120)
    print("source            : db")
    print(f"date              : {day_start.date()}")
    print(f"symbol_filter     : {SYMBOL_FILTER or '(all)'}")
    print(f"setup_filter      : {SETUP_FILTER or '(all)'}")
    print(f"time_window       : {START_TIME or '(start)'} -> {END_TIME or '(end)'}")
    print(f"max_records       : {MAX_RECORDS or '(all)'}")
    print(f"signals           : {len(signals)}")
    print(f"trade_rows        : {sum(int(row.get('trade_rows') or 0) for row in rows)}")
    print(f"rows              : {len(rows)}")
    print(f"failures          : {failures}")
    print(f"output_csv        : {OUTPUT_CSV_PATH}")
    print(f"advisor_decisions : {_count(rows, 'advisor_decision')}")
    print(f"family_alignment  : {_count(rows, 'family_alignment')}")
    print(f"gate_allows       : {_count(rows, 'advisor_gate_allows_with_current_config')}")
    print(f"vwap_contexts     : {_count(rows, 'vwap_context')}")
    print(f"attempt_contexts  : {_count(rows, 'attempt_context')}")

    print(f"\nFirst {min(PRINT_TOP_N, len(rows))} signal review rows")
    print("-" * 120)
    for row in rows[: max(0, int(PRINT_TOP_N))]:
        print(
            f"{str(row.get('symbol') or ''):14s} {str(row.get('side') or ''):4s} "
            f"{str(row.get('setup') or ''):22s} {str(row.get('signal_time') or '')[:19]:19s} "
            f"advisor={str(row.get('advisor_decision') or ''):8s} "
            f"family={str(row.get('advisor_family_key') or ''):24s} "
            f"align={str(row.get('family_alignment') or ''):5s} "
            f"mfe={float(row.get('mfe_pct') or 0):7.4f} mae={float(row.get('mae_pct') or 0):7.4f} "
            f"trades={int(row.get('trade_rows') or 0):2d} reason={str(row.get('family_alignment_reason') or '')}"
        )


if __name__ == "__main__":
    main()
