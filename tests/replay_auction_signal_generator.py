#!/usr/bin/env python3
"""Replay persisted snapshots through the Auction-driven live SignalGenerator.

This harness does not run Auction again and does not mark snapshots processed.
It reads the already validated snapshot.auction projection, calls the same
SignalGenerator used by scripts/gen_signals.py, and writes compact reports.

It writes signal rows.  --clear-signals is explicit and limited to the selected
symbols and DEFAULT lifecycle.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import date, datetime, time, timedelta
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence
from zoneinfo import ZoneInfo

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from configs.signal_config import SIGNAL_CONFIG
from database.database import get_trades_db
from logconfig import setup_logging
from models.trade_models import Signal as SignalORM
from schemas.signal import SignalSchema
from schemas.snapshot import SnapshotSchema
from services.signals.signal_generator import SignalGenerator
from utils.json_utils import sanitize_json

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


def _args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay stored Auction snapshots through the live SignalGenerator."
    )
    parser.add_argument("--date", required=True, help="Trading day YYYY-MM-DD")
    parser.add_argument("--symbols", required=True, help="Comma-separated symbols")
    parser.add_argument("--clear-signals", action="store_true")
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--log-file")
    return parser.parse_args(argv)


def _symbols(raw: str) -> List[str]:
    values = sorted({item.strip().upper() for item in raw.split(",") if item.strip()})
    if not values:
        raise ValueError("At least one symbol is required")
    return values


def _write_csv(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    data = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not data:
        path.write_text("", encoding="utf-8")
        return
    fields = sorted({key for row in data for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(data)


def _clear_signals(symbols: List[str], lifecycle: str) -> int:
    with get_trades_db() as db:
        deleted = int(
            db.query(SignalORM)
            .filter(
                SignalORM.symbol.in_(symbols),
                SignalORM.lifecycle == lifecycle,
            )
            .delete(synchronize_session=False)
        )
        db.commit()
    return deleted


def _load_snapshots(
    *,
    trading_day: date,
    symbols: List[str],
    batch_size: int,
) -> List[SnapshotSchema]:
    output: List[SnapshotSchema] = []
    after_time: Optional[datetime] = None
    after_symbol = ""
    while True:
        batch = SnapshotSchema.fetch_day_replay_batch(
            trading_day=trading_day,
            after_time=after_time,
            after_symbol=after_symbol,
            symbols=symbols,
            limit=max(1, batch_size),
        )
        if not batch:
            break
        output.extend(batch)
        last = batch[-1]
        after_time = last.snapshot_time.replace(tzinfo=None)
        after_symbol = last.symbol
        if len(batch) < max(1, batch_size):
            break
    return output


def _signal_rows(
    *,
    trading_day: date,
    symbols: List[str],
    lifecycle: str,
) -> List[Dict[str, Any]]:
    start = datetime.combine(trading_day, time.min)
    end = start + timedelta(days=1)
    with get_trades_db() as db:
        rows = (
            db.query(SignalORM)
            .filter(
                SignalORM.symbol.in_(symbols),
                SignalORM.lifecycle == lifecycle,
                SignalORM.first_seen_time >= start,
                SignalORM.first_seen_time < end,
            )
            .order_by(SignalORM.first_seen_time.asc(), SignalORM.id.asc())
            .all()
        )

    result: List[Dict[str, Any]] = []
    for row in rows:
        signal = SignalSchema.model_validate(row)
        meta = signal.meta_json
        if not isinstance(meta, dict) or "auction_signal" not in meta:
            raise ValueError(f"Signal {signal.signal_id} missing auction_signal metadata")
        identity = meta["auction_signal"]
        if not isinstance(identity, dict):
            raise ValueError(f"Signal {signal.signal_id} auction_signal must be an object")
        latest = meta["latest_auction_evaluation"]
        if not isinstance(latest, dict):
            raise ValueError(f"Signal {signal.signal_id} latest_auction_evaluation must be an object")
        history = meta["auction_posture_history"]
        if not isinstance(history, list):
            raise ValueError(f"Signal {signal.signal_id} auction_posture_history must be a list")
        lifecycle_latest = meta["signal_lifecycle"]
        if not isinstance(lifecycle_latest, dict):
            raise ValueError(f"Signal {signal.signal_id} signal_lifecycle must be an object")
        lifecycle_history = meta["signal_lifecycle_history"]
        if not isinstance(lifecycle_history, list):
            raise ValueError(f"Signal {signal.signal_id} signal_lifecycle_history must be a list")
        result.append(sanitize_json({
            "signal_id": signal.signal_id,
            "symbol": signal.symbol,
            "equity_ref": signal.equity_ref,
            "lifecycle": signal.lifecycle,
            "setup": signal.setup,
            "side": signal.side.value,
            "stage": signal.stage.value,
            "status": signal.status.value,
            "status_reason": signal.status_reason,
            "first_seen_time": signal.first_seen_time,
            "last_snapshot_time": signal.last_snapshot_time,
            "created_price": signal.created_price,
            "last_price": signal.last_price,
            "ltp": signal.ltp,
            "last_pnl": signal.last_pnl,
            "max_pnl": signal.max_pnl,
            "min_pnl": signal.min_pnl,
            "opportunity_key": identity["opportunity_key"],
            "candidate_id": identity["candidate_id"],
            "boundary_event_key": identity["boundary_event_key"],
            "latest_auction_action": latest["auction_action"],
            "latest_signal_action": lifecycle_latest["signal_action"],
            "latest_signal_stage": lifecycle_latest["stage"],
            "latest_signal_status": lifecycle_latest["status"],
            "latest_signal_reason_code": lifecycle_latest["reason_code"],
            "latest_directional_alignment": lifecycle_latest["directional_alignment"],
            "posture_history_count": len(history),
            "signal_lifecycle_history_count": len(lifecycle_history),
        }))
    return result


def _latest_signal_for_symbol(
    *,
    trading_day: date,
    symbol: str,
    lifecycle: str,
) -> Optional[SignalSchema]:
    start = datetime.combine(trading_day, time.min)
    end = start + timedelta(days=1)
    with get_trades_db() as db:
        row = (
            db.query(SignalORM)
            .filter(
                SignalORM.symbol == symbol,
                SignalORM.lifecycle == lifecycle,
                SignalORM.first_seen_time >= start,
                SignalORM.first_seen_time < end,
            )
            .order_by(SignalORM.id.desc())
            .first()
        )
    return SignalSchema.model_validate(row) if row is not None else None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _args(argv)
    trading_day = date.fromisoformat(args.date)
    symbols = _symbols(args.symbols)
    lifecycle = SIGNAL_CONFIG.default_lifecycle.strip().upper()
    report_dir = Path(args.report_dir)
    log_file = args.log_file or str(report_dir / "replay_auction_signal_generator.log")
    setup_logging(log_file=log_file)
    global logger
    logger = logging.getLogger(__name__)

    cleared = _clear_signals(symbols, lifecycle) if args.clear_signals else 0
    snapshots = _load_snapshots(
        trading_day=trading_day,
        symbols=symbols,
        batch_size=max(1, int(args.batch_size)),
    )
    if not snapshots:
        raise RuntimeError(
            f"No snapshots found for date={trading_day} symbols={symbols}"
        )

    event_rows: List[Dict[str, Any]] = []
    action_counts: Counter[str] = Counter()
    stage_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    for index, snapshot in enumerate(snapshots, start=1):
        action = SignalGenerator(snapshot).generate()
        action_name = action or "NO_ACTION"
        action_counts[action_name] += 1
        decision = snapshot.auction.decision
        if decision is None:
            raise ValueError("Snapshot Auction decision missing")
        latest_signal = _latest_signal_for_symbol(
            trading_day=trading_day,
            symbol=snapshot.symbol,
            lifecycle=lifecycle,
        )
        signal_stage = latest_signal.stage.value if latest_signal is not None else None
        signal_status = latest_signal.status.value if latest_signal is not None else None
        signal_reason = latest_signal.status_reason if latest_signal is not None else None
        latest_lifecycle_action = None
        latest_alignment = None
        if latest_signal is not None:
            meta = latest_signal.meta_json
            if not isinstance(meta, dict) or "signal_lifecycle" not in meta:
                raise ValueError(
                    f"Signal {latest_signal.signal_id} missing signal_lifecycle metadata"
                )
            lifecycle_payload = meta["signal_lifecycle"]
            if not isinstance(lifecycle_payload, dict):
                raise ValueError("signal_lifecycle metadata must be an object")
            latest_lifecycle_action = lifecycle_payload["signal_action"]
            latest_alignment = lifecycle_payload["directional_alignment"]
        if signal_stage is not None:
            stage_counts[signal_stage] += 1
        if signal_status is not None:
            status_counts[signal_status] += 1
        event_rows.append(sanitize_json({
            "index": index,
            "symbol": snapshot.symbol,
            "snapshot_time": snapshot.snapshot_time,
            "auction_action": decision.action,
            "auction_state": snapshot.auction.state.current if snapshot.auction.state is not None else None,
            "selected_opportunity_key": decision.selected_opportunity_key,
            "selected_candidate_id": decision.selected_candidate_id,
            "signal_action": action_name,
            "persisted_signal_action": latest_lifecycle_action,
            "signal_stage": signal_stage,
            "signal_status": signal_status,
            "signal_status_reason": signal_reason,
            "directional_alignment": latest_alignment,
        }))

    signals = _signal_rows(
        trading_day=trading_day,
        symbols=symbols,
        lifecycle=lifecycle,
    )
    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    prefix = report_dir / f"auction_signal_replay_{trading_day}_{stamp}"
    _write_csv(prefix.with_name(prefix.name + "_lifecycle.csv"), event_rows)
    _write_csv(prefix.with_name(prefix.name + "_signals.csv"), signals)

    summary = sanitize_json({
        "trading_day": trading_day,
        "symbols": symbols,
        "lifecycle": lifecycle,
        "snapshots": len(snapshots),
        "first_snapshot_time": snapshots[0].snapshot_time,
        "last_snapshot_time": snapshots[-1].snapshot_time,
        "signals_cleared": cleared,
        "signal_action_counts": dict(sorted(action_counts.items())),
        "signal_stage_observation_counts": dict(sorted(stage_counts.items())),
        "signal_status_observation_counts": dict(sorted(status_counts.items())),
        "signals_persisted": len(signals),
        "snapshots_marked_processed": 0,
    })
    prefix.with_name(prefix.name + "_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_csv(prefix.with_name(prefix.name + "_summary.csv"), [{
        **summary,
        "symbols": json.dumps(summary["symbols"]),
        "signal_action_counts": json.dumps(summary["signal_action_counts"], sort_keys=True),
        "signal_stage_observation_counts": json.dumps(
            summary["signal_stage_observation_counts"], sort_keys=True
        ),
        "signal_status_observation_counts": json.dumps(
            summary["signal_status_observation_counts"], sort_keys=True
        ),
    }])

    logger.info("Auction SignalGenerator replay complete | %s", summary)
    logger.info("Reports: %s_*", prefix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
