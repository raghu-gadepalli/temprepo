#!/usr/bin/env python3
"""Read-only Phase 4A.1 ledger and decision-contract correction report."""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# Default replay day.  Change this one value when running a different day, or
# pass --date/--start-date on the command line to override it.
REPORT_DATE = "2026-07-20"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG
from services.auction_engine.engine import AuctionEngine
from services.auction_engine.active_context import ActiveContextProvider
from tests.test_auction_engine_report import (
    parse_args, resolve_dates, load_snapshot_rows, result_row,
    candidate_observation_rows, build_episode_summary, build_candidate_lifecycle,
    build_opportunity_lifecycle, build_summary, write_csv, git_value, database_name,
)


def _manager_row(result: Any, run_id: str) -> Dict[str, Any]:
    m = result.manager_decision
    return {
        "run_id": run_id, "symbol": result.symbol,
        "snapshot_time": result.snapshot_time.isoformat(sep=" "),
        "action": m.action.value, "selected_candidate_id": m.selected_candidate_id,
        "selected_opportunity_key": m.diagnostics.get("selected_opportunity_key"),
        "same_direction_support_ids": list(m.same_direction_support_ids),
        "opposing_candidate_ids": list(m.opposing_candidate_ids),
        "material_opposition": m.material_opposition,
        "active_signal_id": m.active_signal_id,
        "reason_codes": list(m.reason_codes),
        **{f"diag_{k}": v for k, v in m.diagnostics.items()},
    }


def _advisor_row(result: Any, run_id: str) -> Optional[Dict[str, Any]]:
    if not result.advisor_decisions:
        return None
    a = result.advisor_decisions[0]
    return {
        "run_id": run_id, "symbol": result.symbol,
        "snapshot_time": result.snapshot_time.isoformat(sep=" "),
        "candidate_id": a.candidate_id, "family": a.family.value, "side": a.side.value,
        "recommendation": a.recommendation.value,
        "derivatives_alignment": a.derivatives_alignment.value,
        "data_quality": a.data_quality.value,
        "reason_codes": list(a.reason_codes),
        "enforcement_mode": a.diagnostics.get("enforcement_mode"),
        "futures_bias": a.diagnostics.get("futures_bias"),
        "options_bias": a.diagnostics.get("options_bias"),
        "futures_window": a.diagnostics.get("futures_window"),
        "options_window": a.diagnostics.get("options_window"),
        "raw_derivatives_diagnostics": a.diagnostics.get("raw_derivatives_diagnostics"),
        "channels": [c.to_storage_dict(exclude_none=False) for c in a.channels],
    }


def _decision_row(result: Any, run_id: str) -> Dict[str, Any]:
    d = result.final_decision
    c = d.selected_candidate
    return {
        "run_id": run_id, "symbol": result.symbol,
        "snapshot_time": result.snapshot_time.isoformat(sep=" "),
        "action": d.action.value,
        "selected_candidate_id": c.candidate_id if c else None,
        "selected_opportunity_key": c.opportunity_key if c else None,
        "selected_family": c.family.value if c else None,
        "selected_subtype": c.subtype if c else None,
        "selected_side": c.side.value if c else None,
        "entry_price": c.entry_price if c else None,
        "stop_anchor_price": c.stop_anchor_price if c else None,
        "manager_action": d.manager_decision.action.value,
        "advisor_recommendation": d.advisor_decision.recommendation.value if d.advisor_decision else "NOT_EVALUATED",
        "reason_codes": list(d.reason_codes),
        "decision_without_advisor": d.diagnostics.get("decision_without_advisor"),
        "decision_with_advisor": d.diagnostics.get("decision_with_advisor"),
        "advisor_enforcement_mode": d.diagnostics.get("advisor_enforcement_mode"),
        "signal_payload_preview": d.signal_payload.to_storage_dict(exclude_none=False) if d.signal_payload else None,
    }


def _event_dict(event: Any, run_id: str) -> Dict[str, Any]:
    row = event.to_dict()
    row["run_id"] = run_id
    return row


def run_report(args: Any) -> Dict[str, Path]:
    start_day, end_day = resolve_dates(args)
    symbols = tuple(sorted({x.strip().upper() for x in (args.symbols or "").split(",") if x.strip()}))
    started_at = datetime.now()
    config_hash = AUCTION_ENGINE_CONFIG.stable_hash()
    run_id = f"DECISION4A1-{start_day}-{end_day}-{started_at:%Y%m%dT%H%M%S}-{config_hash[:8]}"
    out = Path(args.output_dir).expanduser().resolve(); out.mkdir(parents=True, exist_ok=True)
    stem = f"auction_engine_decisions_{start_day}_{end_day}_{started_at:%Y%m%d_%H%M%S}"
    snapshots = load_snapshot_rows(start_day, end_day, symbols=symbols, limit=max(0, int(args.limit or 0)))
    engine = AuctionEngine(
        AUCTION_ENGINE_CONFIG,
        active_context_provider=ActiveContextProvider(
            AUCTION_ENGINE_CONFIG.decision.active_signal_lifecycle
        ),
    )
    timeline: List[Dict[str, Any]] = []; candidate_rows: List[Dict[str, Any]] = []
    manager_rows: List[Dict[str, Any]] = []; advisor_rows: List[Dict[str, Any]] = []
    decision_rows: List[Dict[str, Any]] = []; errors: List[Dict[str, Any]] = []; symbol_set=set()

    for i, snapshot in enumerate(snapshots, 1):
        try:
            result = engine.evaluate_snapshot(snapshot)
            symbol_set.add(result.symbol)
            base = result_row(result, run_id)
            obs = candidate_observation_rows(result, run_id); candidate_rows.extend(obs)
            mrow = _manager_row(result, run_id); manager_rows.append(mrow)
            arow = _advisor_row(result, run_id)
            if arow: advisor_rows.append(arow)
            drow = _decision_row(result, run_id); decision_rows.append(drow)
            base.update({"manager_action": mrow["action"], "manager_selected_opportunity_key": mrow.get("selected_opportunity_key"), "advisor_recommendation": arow.get("recommendation") if arow else "NOT_EVALUATED", "final_action": drow["action"]})
            if not args.transitions_only or base.get("transitioned") or base.get("boundary_transitioned") or obs or drow["action"] != "HOLD":
                timeline.append(base)
        except Exception as exc:
            errors.append({"row_number": i, "symbol": snapshot.get("symbol"), "snapshot_time": snapshot.get("snapshot_time"), "error_type": type(exc).__name__, "error": str(exc)})
            if not args.continue_on_error and AUCTION_ENGINE_CONFIG.engine.strict_evaluation: raise

    episodes = build_episode_summary(timeline)
    candidate_lifecycle = build_candidate_lifecycle(candidate_rows, snapshots, AUCTION_ENGINE_CONFIG.diagnostics.outcome_horizons_bars)
    phase3_opportunities = build_opportunity_lifecycle(candidate_lifecycle)
    ledger_rows = list(engine.opportunity_ledger.record_dicts())
    event_rows = [_event_dict(e, run_id) for e in engine.opportunity_ledger.events()]
    would_create = [r for r in decision_rows if r["action"] == "CREATE"]
    summary = build_summary(timeline, candidate_rows, candidate_lifecycle)
    summary.extend([
        {"section": "PHASE4A", "metric": "LEDGER_OPPORTUNITIES", "count": len(ledger_rows)},
        {"section": "PHASE4A", "metric": "WOULD_CREATE", "count": len(would_create)},
        {"section": "PHASE4A", "metric": "WOULD_CREATE_SYMBOLS", "count": len({r['symbol'] for r in would_create})},
        {"section": "PHASE4A", "metric": "MANAGER_DEFER", "count": sum(r['action']=='DEFER' for r in manager_rows)},
        {"section": "PHASE4A", "metric": "ADVISOR_ALLOW", "count": sum(r['recommendation']=='ALLOW' for r in advisor_rows)},
        {"section": "PHASE4A", "metric": "ADVISOR_WATCH", "count": sum(r['recommendation']=='WATCH' for r in advisor_rows)},
        {"section": "PHASE4A", "metric": "ADVISOR_BLOCK", "count": sum(r['recommendation']=='BLOCK' for r in advisor_rows)},
    ])
    manifest = {
        "run_id": run_id, "run_type": "PHASE4A1_LEDGER_AND_DECISION_CONTRACT_CORRECTION_REPORT",
        "started_at": started_at.isoformat(), "completed_at": datetime.now().isoformat(),
        "start_day": start_day.isoformat(), "end_day": end_day.isoformat(),
        "config_version": AUCTION_ENGINE_CONFIG.engine.config_version, "config_hash": config_hash,
        "resolved_config": AUCTION_ENGINE_CONFIG.resolved_dict(), "database_name": database_name(),
        "git_commit": git_value("rev-parse", "HEAD"), "git_tag": git_value("describe", "--tags", "--exact-match"),
        "symbol_count": len(symbol_set), "snapshot_count_loaded": len(snapshots),
        "candidate_count": len(candidate_lifecycle), "operational_opportunity_count": len(phase3_opportunities),
        "ledger_opportunity_count": len(ledger_rows), "ledger_event_count": len(event_rows),
        "manager_action_counts": dict(Counter(r['action'] for r in manager_rows)),
        "advisor_recommendation_counts": dict(Counter(r['recommendation'] for r in advisor_rows)),
        "final_action_counts": dict(Counter(r['action'] for r in decision_rows)),
        "would_create_count": len(would_create), "would_create_symbol_count": len({r['symbol'] for r in would_create}),
        "signals_created": 0, "opportunity_rows_written": 0,
        "setup_state_rows_written": 0, "processed_flags_changed": 0, "error_count": len(errors),
        "notes": [
            "Phase 4A.1 is report-only and does not persist opportunities or signals",
            "One stock_opportunities table stores current state plus JSON event/candidate history",
            "Opportunity lifecycle is aggregated across candidate aliases and source boundary state",
            "Setup Manager selects an actually ELIGIBLE alias and reads historical side switches",
            "Fresh WATCH opposition and active-signal context are explicit diagnostics",
            "Context Advisor consumes derivativeschain_v2 sentiment windows and remains LOG_ONLY by default",
            "NIFTY, BANKNIFTY, VIX and sector context are explicit NOT_EVALUATED placeholders",
            "CREATE actions are WOULD_CREATE previews and are simulated-consumed to prevent repetition",
            "Phase 2.5, Phase 3A.1 and Phase 3B.2 policies remain unchanged",
        ],
    }
    paths = {
        "timeline": out/f"{stem}.csv", "episodes": out/f"{stem}_episodes.csv",
        "candidates": out/f"{stem}_candidates.csv", "candidate_lifecycle": out/f"{stem}_candidate_lifecycle.csv",
        "opportunities": out/f"{stem}_opportunities.csv", "ledger": out/f"{stem}_ledger.csv",
        "opportunity_events": out/f"{stem}_opportunity_events.csv", "manager": out/f"{stem}_manager.csv",
        "advisor": out/f"{stem}_advisor.csv", "decision_lifecycle": out/f"{stem}_decision_lifecycle.csv",
        "would_create": out/f"{stem}_would_create.csv", "summary": out/f"{stem}_summary.csv",
        "manifest": out/f"{stem}_manifest.json", "errors": out/f"{stem}_errors.csv",
    }
    for name, rows in [("timeline",timeline),("episodes",episodes),("candidates",candidate_rows),("candidate_lifecycle",candidate_lifecycle),("opportunities",phase3_opportunities),("ledger",ledger_rows),("opportunity_events",event_rows),("manager",manager_rows),("advisor",advisor_rows),("decision_lifecycle",decision_rows),("would_create",would_create),("summary",summary),("errors",errors)]: write_csv(paths[name], rows)
    paths["manifest"].write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return paths


def main(argv: Optional[Sequence[str]] = None) -> int:
    effective_argv = list(argv) if argv is not None else list(sys.argv[1:])
    if not effective_argv:
        effective_argv = ["--date", REPORT_DATE]
    args = parse_args(effective_argv); paths = run_report(args)
    print("Auction-engine Phase 4A.1 report complete")
    for label, path in paths.items(): print(f"  {label}: {path}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
