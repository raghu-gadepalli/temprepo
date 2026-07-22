#!/usr/bin/env python3
"""Offline Phase 4A opportunity-ledger/manager/decision tests."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from utils.datetime_utils import IST

from services.auction_engine.contracts import (
    CandidateEligibility, DirectionalBias, FinalAction, ManagerAction,
    ManagerDecision, SetupFamily, TradeSide,
)
from services.auction_engine.engine import AuctionEngine
from services.auction_engine.opportunity_ledger import OpportunityLedger
from services.auction_engine.decision_engine import DecisionEngine
from services.auction_engine.active_context import ActiveContext
from tests.test_auction_engine_phase2 import _snapshot, _test_config


class Phase4ATests(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = datetime(2026, 7, 20, 10, 0)
        self.engine = AuctionEngine(_test_config())

    def _warm_and_accept(self):
        for index, close in enumerate((100.0, 100.1, 99.95, 100.05)):
            self.engine.evaluate_snapshot(_snapshot(self.ts + timedelta(minutes=index*3), open_price=close, high=close+.2, low=close-.2, close=close, range_type="MICRO_COMPRESSION", range_low=99.0, range_high=101.0, range_width_atr=2.0))
        rows = [
            dict(open_price=100.7, high=100.9, low=100.6, close=100.8),
            dict(open_price=100.9, high=101.5, low=100.85, close=101.25),
            dict(open_price=101.2, high=101.4, low=101.1, close=101.3),
            dict(open_price=101.25, high=101.45, low=101.2, close=101.35),
        ]
        out=[]
        for offset, values in enumerate(rows, start=4):
            out.append(self.engine.evaluate_snapshot(_snapshot(self.ts + timedelta(minutes=offset*3), range_type="MICRO_COMPRESSION", **values)))
        return out

    def test_eligible_opportunity_is_selected_and_simulated_consumed(self):
        rows = self._warm_and_accept()
        creates = [r for r in rows if r.final_decision.action is FinalAction.CREATE]
        self.assertTrue(creates)
        selected = creates[0]
        self.assertEqual(selected.manager_decision.action, ManagerAction.SELECT)
        key = selected.final_decision.selected_candidate.opportunity_key
        record = next(r for r in self.engine.opportunity_ledger.records() if r.opportunity_key == key)
        self.assertEqual(record.lifecycle_state, "CONSUMED")
        self.assertIsNotNone(record.consumed_time)

    def test_same_opportunity_does_not_create_repeatedly(self):
        self._warm_and_accept()
        later = self.engine.evaluate_snapshot(_snapshot(self.ts + timedelta(minutes=25), open_price=101.3, high=101.5, low=101.2, close=101.4, range_type="MICRO_COMPRESSION"))
        self.assertNotEqual(later.final_decision.action, FinalAction.CREATE)

    def test_advisor_is_thin_and_log_only(self):
        creates = [r for r in self._warm_and_accept() if r.final_decision.action is FinalAction.CREATE]
        decision = creates[0].final_decision
        self.assertIsNotNone(decision.advisor_decision)
        self.assertEqual(decision.diagnostics["advisor_enforcement_mode"], "LOG_ONLY")
        self.assertFalse(decision.advisor_decision.diagnostics["local_stock_context_recomputed"])
        placeholders = {c.name: c.diagnostics.get("evaluation_status") for c in decision.advisor_decision.channels}
        self.assertEqual(placeholders["NIFTY"], "NOT_EVALUATED")
        self.assertEqual(placeholders["VIX"], "NOT_EVALUATED")

    def test_engine_remains_persistence_agnostic(self):
        self.assertFalse(self.engine.config.decision.create_enabled)
        self.assertFalse(self.engine.config.persistence.write_enabled)
        self.assertEqual(self.engine.config.engine.engine_version, "0.5.4")

    def test_opportunity_primary_alias_must_be_currently_eligible(self):
        rows = self._warm_and_accept()
        initiation = next(
            candidate
            for result in rows
            for candidate in result.candidates
            if candidate.family is SetupFamily.BREAKOUT_INITIATION
        )
        accepted = next(
            candidate
            for result in rows
            for candidate in result.candidates
            if candidate.family is SetupFamily.ACCEPTED_BREAKOUT
        )
        expired_initiation = initiation.model_copy(update={
            "snapshot_time": accepted.snapshot_time,
            "eligibility": CandidateEligibility.EXPIRED,
            "blockers": ("INITIATION_CONFIRMATION_WINDOW_EXPIRED",),
            "terminal": True,
        })
        ledger = OpportunityLedger()
        ledger.update("TEST", accepted.snapshot_time, (expired_initiation,))
        records = ledger.update(
            "TEST",
            accepted.snapshot_time,
            (accepted,),
            boundary_episode=rows[-1].boundary_episode,
        )
        record = next(row for row in records if row.opportunity_key == accepted.opportunity_key)
        self.assertEqual(record.lifecycle_state, "ELIGIBLE")
        self.assertEqual(record.selected_candidate().candidate_id, accepted.candidate_id)
        self.assertEqual(record.primary_candidate.candidate_id, accepted.candidate_id)
        self.assertIsNone(record.terminal_time)

    def test_derivatives_schema_windows_drive_thin_advisor(self):
        snapshot = _snapshot(
            self.ts, open_price=100.0, high=100.2, low=99.8, close=100.0,
        )
        snapshot["derivatives"] = {
            "spot_price": 100.0,
            "future": {"last_price": 100.4, "oi": 1100},
            "options_lite": {"pcr": 1.2},
            "option_sentiment_windows": {
                "15m": {
                    "status": "ok", "indication": "bullish", "strength": 0.7,
                    "pcr_now": 1.2, "pcr_delta": 0.1,
                }
            },
            "future_sentiment_windows": {
                "15m": {
                    "status": "ok", "label": "LONG_BUILDUP", "strength": 0.8,
                    "fut_ltp_now": 100.4, "fut_ltp_delta": 0.4,
                    "fut_oi_now": 1100, "fut_oi_delta": 100,
                }
            },
        }
        result = self.engine.evaluate_snapshot(snapshot)
        evidence = result.evidence.derivatives
        self.assertEqual(evidence.options_bias, DirectionalBias.UP)
        self.assertEqual(evidence.futures_bias, DirectionalBias.UP)
        self.assertEqual(evidence.options_window, "15m")
        self.assertEqual(evidence.futures_window, "15m")
        self.assertAlmostEqual(evidence.basis_points, 0.4)
        self.assertAlmostEqual(evidence.futures_oi_change_pct, 10.0)

    def test_invalid_stop_geometry_blocks_payload(self):
        creates = [result for result in self._warm_and_accept() if result.final_decision.action is FinalAction.CREATE]
        selected = creates[0].final_decision.selected_candidate
        invalid = selected.model_copy(update={"stop_anchor_price": selected.entry_price + 1.0})
        manager = ManagerDecision(
            symbol=invalid.symbol, snapshot_time=invalid.snapshot_time,
            action=ManagerAction.SELECT, selected_candidate_id=invalid.candidate_id,
            config_version=self.engine.config.engine.config_version,
        )
        decision = DecisionEngine(self.engine.config).evaluate(
            manager=manager, selected=invalid, advisor=None, equity_ref=invalid.symbol,
        )
        self.assertEqual(decision.action, FinalAction.BLOCK)
        self.assertIn("INVALID_STRUCTURAL_STOP_GEOMETRY", decision.reason_codes)
        self.assertIsNone(decision.signal_payload)

    def test_active_context_is_reported_but_not_enforced_in_log_only_mode(self):
        rows = self._warm_and_accept()
        candidate = next(
            result.final_decision.selected_candidate
            for result in rows
            if result.final_decision.action is FinalAction.CREATE
        )
        ledger = OpportunityLedger()
        records = ledger.update("TEST", candidate.snapshot_time, (candidate,))
        context = ActiveContext(
            equity_ref="TEST", snapshot_time=candidate.snapshot_time,
            active_signal_id="SIG-1", active_signal_side="BUY",
            reason_codes=("ACTIVE_SIGNAL_PRESENT",),
            diagnostics={"time_basis": "CAUSAL_AS_OF_SNAPSHOT"},
        )
        manager = self.engine.setup_manager.evaluate(
            "TEST", candidate.snapshot_time, records, active_context=context,
        )
        self.assertEqual(manager.action, ManagerAction.SELECT)
        self.assertEqual(manager.active_signal_id, "SIG-1")
        self.assertEqual(manager.diagnostics["manager_action_before_active_context"], "SELECT")
        self.assertEqual(manager.diagnostics["manager_action_after_active_context"], "SELECT")

    def test_rotation_uses_consumed_stock_day_history(self):
        creates = [result for result in self._warm_and_accept() if result.final_decision.action is FinalAction.CREATE]
        first = creates[0].final_decision.selected_candidate
        ledger = OpportunityLedger()
        ledger.update("TEST", first.snapshot_time, (first,))
        ledger.mark_selected(first.opportunity_key, first.snapshot_time, first.candidate_id)
        ledger.mark_consumed(first.opportunity_key, first.snapshot_time, candidate_id=first.candidate_id)
        later_time = first.snapshot_time + timedelta(minutes=30)
        second_side = TradeSide.SELL if first.side is TradeSide.BUY else TradeSide.BUY
        second = first.model_copy(update={
            "candidate_id": "SECOND-CANDIDATE",
            "snapshot_time": later_time,
            "candidate_time": later_time,
            "event_time": later_time,
            "event_key": "SECOND-EVENT",
            "source_boundary_event_key": "SECOND-EVENT",
            "opportunity_key": "SECOND-OPPORTUNITY",
            "boundary_thesis_key": "SECOND-THESIS",
            "support_group_key": "SECOND-OPPORTUNITY",
            "side": second_side,
            "entry_price": 100.0,
            "stop_anchor_price": 101.0 if second_side is TradeSide.SELL else 99.0,
        })
        records = ledger.update("TEST", later_time, (second,))
        manager = self.engine.setup_manager.evaluate("TEST", later_time, records)
        self.assertEqual(manager.action, ManagerAction.SELECT)
        self.assertEqual(manager.diagnostics["recent_eligible_side_switches"], 1)
        self.assertEqual(len(manager.diagnostics["historical_selected_side_sequence"]), 1)

    def test_fresh_pending_opposite_watch_is_explicit_material_opposition(self):
        creates = [result for result in self._warm_and_accept() if result.final_decision.action is FinalAction.CREATE]
        selected = creates[0].final_decision.selected_candidate
        opposite_side = TradeSide.SELL if selected.side is TradeSide.BUY else TradeSide.BUY
        watch = selected.model_copy(update={
            "candidate_id": "OPPOSITE-WATCH",
            "event_key": "OPPOSITE-EVENT",
            "source_boundary_event_key": "OPPOSITE-EVENT",
            "opportunity_key": "OPPOSITE-OPPORTUNITY",
            "boundary_thesis_key": "OPPOSITE-THESIS",
            "support_group_key": "OPPOSITE-OPPORTUNITY",
            "side": opposite_side,
            "eligibility": CandidateEligibility.WATCH,
            "blockers": ("FAILED_DIRECTIONAL_FOLLOWTHROUGH_PENDING",),
            "terminal": False,
            "valid_until": selected.snapshot_time + timedelta(minutes=15),
            "diagnostics": {"dynamic_watch": True},
        })
        ledger = OpportunityLedger()
        records = ledger.update("TEST", selected.snapshot_time, (selected, watch))
        manager = self.engine.setup_manager.evaluate("TEST", selected.snapshot_time, records)
        self.assertEqual(manager.action, ManagerAction.DEFER)
        self.assertTrue(manager.material_opposition)
        self.assertIn("OPPOSITE-WATCH", manager.opposing_candidate_ids)
        self.assertEqual(manager.diagnostics["material_opposing_watch_count"], 1)

    def test_active_context_normalizes_db_naive_and_snapshot_aware_times(self):
        aware_as_of = datetime(2026, 7, 20, 10, 0, tzinfo=IST)
        signal = SimpleNamespace(
            first_seen_time=datetime(2026, 7, 20, 9, 30),
            actionable_time=None, qualified_time=None,
            last_snapshot_time=datetime(2026, 7, 20, 9, 30),
            closed_time=None,
        )
        from services.auction_engine.active_context import ActiveContextProvider
        self.assertTrue(ActiveContextProvider._signal_active_as_of(signal, aware_as_of))

    def test_expired_failed_watch_is_terminal_before_renewed_outside_watch(self):
        from services.auction_engine.setup_engine import SetupCandidateEngine, _FailedWatch
        from services.auction_engine.contracts import (
            AuctionStateName, BoundaryEpisodeStatus, BoundaryResolution,
            BoundarySide, CandidateEligibility, TradeSide,
        )

        snapshot_time = self.ts + timedelta(minutes=18)
        state_result = self.engine.evaluate_snapshot(_snapshot(
            snapshot_time, open_price=101.1, high=101.3, low=101.0, close=101.2,
            range_low=99.0, range_high=101.0,
        ))
        setup = SetupCandidateEngine(_test_config())
        watch = _FailedWatch(
            symbol="TEST", candidate_id="EXPIRED-FAIL-WATCH", event_key="FAIL-EVENT",
            side=TradeSide.SELL, subtype="NEUTRAL_RANGE_FAILED_AUCTION",
            event_time=self.ts, failed_time=self.ts, resolution_price=100.5,
            boundary_price=101.0, frozen_low=99.0, frozen_high=101.0,
            source_boundary_status=BoundaryEpisodeStatus.FAILED,
            source_boundary_resolution=BoundaryResolution.FAILED,
            source_boundary_id="BOUNDARY-1", source_boundary_side=BoundarySide.UPPER,
            source_boundary_source="DYNAMIC", source_frozen_range_id="RANGE-1",
            source_frozen_range_version=1,
            resolution_basis="DIRECTIONAL_FOLLOWTHROUGH",
            state_at_failure=AuctionStateName.BALANCE,
            expires_at=self.ts + timedelta(minutes=15),
        )
        setup._failed[watch.candidate_id] = watch

        candidates = setup._evaluate_failed_watches(
            state_result.evidence, state_result.auction_state, None, {},
        )

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.eligibility, CandidateEligibility.EXPIRED)
        self.assertTrue(candidate.terminal)
        self.assertIsNone(candidate.valid_until)
        self.assertIn("FAILED_WATCH_WINDOW_EXPIRED", candidate.blockers)
        self.assertNotIn(watch.candidate_id, setup._failed)


    def test_report_snapshot_decoder_accepts_text_bytes_and_mapping(self):
        from tests.test_auction_engine_report import _decode_snapshot_data
        payload = {"symbol": "TEST", "close": 100.0}
        encoded = json.dumps(payload)
        self.assertEqual(_decode_snapshot_data(payload), payload)
        self.assertEqual(_decode_snapshot_data(encoded), payload)
        self.assertEqual(_decode_snapshot_data(encoded.encode("utf-8")), payload)



if __name__ == "__main__": unittest.main()
