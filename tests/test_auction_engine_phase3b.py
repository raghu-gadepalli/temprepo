#!/usr/bin/env python3
"""Offline tests for Phase-3B observation-only setup candidates.

Run from the project root:

    python -m unittest tests.test_auction_engine_phase3b -v

Synthetic snapshots only; no database, signal persistence or TradeManager calls.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from services.auction_engine.contracts import (
    AuctionStateName,
    BoundaryEpisodeStatus,
    BoundaryResolution,
    BoundarySide,
    CandidateEligibility,
    CandidateRole,
    LocalDecisionAction,
    SetupFamily,
    TradeSide,
)
from services.auction_engine.engine import AuctionEngine
from services.auction_engine.setup_engine import SetupCandidateEngine
from tests.test_auction_engine_phase2 import _snapshot, _test_config
from tests.test_auction_engine_report import (
    build_candidate_lifecycle,
    build_opportunity_lifecycle,
    candidate_observation_rows,
)


class SetupCandidateEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = datetime(2026, 7, 20, 10, 0)
        self.engine = AuctionEngine(_test_config())

    def _warm_compression(self, *, range_low: float = 99.0, range_high: float = 101.0) -> list[dict]:
        rows = []
        for index, close in enumerate((100.0, 100.1, 99.95, 100.05)):
            row = _snapshot(
                self.ts + timedelta(minutes=index * 3),
                open_price=close,
                high=close + 0.2,
                low=close - 0.2,
                close=close,
                range_type="MICRO_COMPRESSION",
                range_low=range_low,
                range_high=range_high,
                range_width_atr=range_high - range_low,
            )
            self.engine.evaluate_snapshot(row)
            rows.append(row)
        return rows

    def test_breakout_initiation_watch_becomes_eligible_on_immediate_hold(self) -> None:
        self._warm_compression()
        approach = self.engine.evaluate_snapshot(_snapshot(
            self.ts + timedelta(minutes=12),
            open_price=100.7, high=100.9, low=100.6, close=100.8,
            range_type="MICRO_COMPRESSION",
        ))
        self.assertFalse(approach.candidates)

        attempt = self.engine.evaluate_snapshot(_snapshot(
            self.ts + timedelta(minutes=15),
            open_price=100.9, high=101.5, low=100.85, close=101.25,
            range_type="MICRO_COMPRESSION",
        ))
        initiation = next(item for item in attempt.candidates if item.family is SetupFamily.BREAKOUT_INITIATION)
        self.assertEqual(initiation.eligibility, CandidateEligibility.WATCH)
        self.assertIn("INITIATION_WAITING_FOR_IMMEDIATE_HOLD_OR_RETEST", initiation.blockers)

        hold = self.engine.evaluate_snapshot(_snapshot(
            self.ts + timedelta(minutes=18),
            open_price=101.2, high=101.4, low=101.1, close=101.3,
            range_type="MICRO_COMPRESSION",
        ))
        confirmed = next(item for item in hold.candidates if item.family is SetupFamily.BREAKOUT_INITIATION)
        self.assertEqual(confirmed.candidate_id, initiation.candidate_id)
        self.assertEqual(confirmed.subtype, initiation.subtype)
        self.assertEqual(confirmed.candidate_role, CandidateRole.EARLY_INITIATION)
        self.assertEqual(confirmed.eligibility, CandidateEligibility.ELIGIBLE)
        self.assertEqual(confirmed.blockers, ())
        self.assertEqual(confirmed.target_basis, "OPEN_ENDED_BREAKOUT_NO_ASSUMED_TARGET")
        self.assertIsNone(confirmed.target_reference_price)
        self.assertIsNone(confirmed.room_pct)
        self.assertTrue(confirmed.diagnostics["measured_move_reference_is_diagnostic_only"])
        self.assertTrue(confirmed.terminal)
        self.assertEqual(hold.local_decision.action, LocalDecisionAction.CONFIRMED)

    def test_accepted_resolution_emits_accepted_and_boundary_continuation_interpretations(self) -> None:
        self._warm_compression()
        sequence = [
            dict(open_price=100.7, high=100.9, low=100.6, close=100.8),
            dict(open_price=100.9, high=101.5, low=100.85, close=101.25),
            dict(open_price=101.2, high=101.4, low=101.1, close=101.3),
            dict(open_price=101.25, high=101.45, low=101.2, close=101.35),
        ]
        result = None
        for offset, values in enumerate(sequence, start=4):
            result = self.engine.evaluate_snapshot(_snapshot(
                self.ts + timedelta(minutes=offset * 3),
                range_type="MICRO_COMPRESSION",
                **values,
            ))
        self.assertIsNotNone(result)
        by_family = {item.family: item for item in result.candidates}
        self.assertIn(SetupFamily.ACCEPTED_BREAKOUT, by_family)
        self.assertIn(SetupFamily.CONTINUATION, by_family)
        accepted = by_family[SetupFamily.ACCEPTED_BREAKOUT]
        self.assertEqual(accepted.eligibility, CandidateEligibility.ELIGIBLE)
        self.assertEqual(accepted.target_basis, "OPEN_ENDED_BREAKOUT_NO_ASSUMED_TARGET")
        self.assertIsNone(accepted.target_reference_price)
        self.assertIsNone(accepted.room_atr)
        self.assertFalse(accepted.first_move_consumed)
        self.assertEqual(accepted.diagnostics["reward_model"], "OPEN_ENDED_BREAKOUT")
        self.assertFalse(accepted.diagnostics["assumed_target_hard_gate"])
        self.assertTrue(accepted.diagnostics["measured_move_reference_is_diagnostic_only"])
        self.assertNotIn("BREAKOUT_FIRST_MOVE_BELOW_MINIMUM_PCT", accepted.blockers)
        self.assertEqual(
            by_family[SetupFamily.CONTINUATION].subtype,
            "BOUNDARY_CONTINUATION_ACCEPTANCE",
        )
        self.assertEqual(
            by_family[SetupFamily.ACCEPTED_BREAKOUT].opportunity_key,
            by_family[SetupFamily.CONTINUATION].opportunity_key,
        )
        self.assertEqual(
            by_family[SetupFamily.ACCEPTED_BREAKOUT].support_group_key,
            by_family[SetupFamily.CONTINUATION].support_group_key,
        )
        self.assertEqual(
            by_family[SetupFamily.ACCEPTED_BREAKOUT].candidate_role,
            CandidateRole.ACCEPTED_RESOLUTION_ENTRY,
        )
        self.assertEqual(
            by_family[SetupFamily.CONTINUATION].candidate_role,
            CandidateRole.CONTINUATION_INTERPRETATION,
        )
        self.assertEqual(result.manager_decision.diagnostics["unique_opportunity_count"], 1)
        self.assertTrue(result.manager_decision.diagnostics["alias_double_counting_prevented"])
        self.assertEqual(result.advisor_decisions, ())
        self.assertEqual(result.local_decision.action, LocalDecisionAction.CONFIRMED)

    def test_deep_reentry_failed_auction_watch_can_become_eligible_after_followthrough(self) -> None:
        self._warm_compression(range_low=97.0, range_high=101.0)
        sequence = [
            dict(open_price=100.7, high=100.9, low=100.6, close=100.8),
            dict(open_price=100.9, high=101.3, low=100.85, close=100.98),
            dict(open_price=100.95, high=101.0, low=100.65, close=100.70),
            dict(open_price=100.70, high=100.85, low=100.65, close=100.72),
        ]
        failed_result = None
        for offset, values in enumerate(sequence, start=4):
            failed_result = self.engine.evaluate_snapshot(_snapshot(
                self.ts + timedelta(minutes=offset * 3),
                range_type="BALANCE",
                range_low=97.0,
                range_high=101.0,
                range_width_atr=4.0,
                **values,
            ))
        self.assertIsNotNone(failed_result)
        failed = next(item for item in failed_result.candidates if item.family is SetupFamily.FAILED_BREAKOUT)
        self.assertEqual(failed.eligibility, CandidateEligibility.WATCH)
        self.assertIn("FAILED_DIRECTIONAL_FOLLOWTHROUGH_PENDING", failed.blockers)
        self.assertEqual(failed.diagnostics["resolution_basis"], "DEEP_REENTRY_INSIDE_HOLD")

        followthrough = self.engine.evaluate_snapshot(_snapshot(
            self.ts + timedelta(minutes=24),
            open_price=100.72, high=100.75, low=100.30, close=100.40,
            range_type="BALANCE",
            range_low=97.0,
            range_high=101.0,
            range_width_atr=4.0,
        ))
        promoted = next(item for item in followthrough.candidates if item.family is SetupFamily.FAILED_BREAKOUT)
        self.assertEqual(promoted.candidate_id, failed.candidate_id)
        self.assertEqual(promoted.eligibility, CandidateEligibility.ELIGIBLE)
        self.assertTrue(promoted.diagnostics["followthrough_confirmed"])
        self.assertEqual(promoted.target_basis, "FROZEN_RANGE_OPPOSITE_EDGE")
        self.assertAlmostEqual(promoted.target_reference_price, 97.0)
        self.assertAlmostEqual(promoted.room_points, 3.4)
        self.assertEqual(promoted.diagnostics["reward_model"], "RETURN_TO_OPPOSITE_FROZEN_RANGE_EDGE")
        self.assertAlmostEqual(promoted.diagnostics["failed_midpoint_price"], 99.0)
        self.assertTrue(promoted.diagnostics["midpoint_vwap_are_diagnostic_only"])
        self.assertEqual(promoted.source_boundary_status, BoundaryEpisodeStatus.FAILED)
        self.assertEqual(promoted.source_boundary_event_key, promoted.event_key)

        archived_view = followthrough.model_copy(update={"boundary_episode": None})
        report_row = candidate_observation_rows(archived_view, "TEST-RUN")[0]
        self.assertEqual(report_row["source_boundary_status"], "FAILED")
        self.assertEqual(report_row["boundary_status"], "FAILED")
        self.assertIsNone(report_row["current_boundary_status"])

    def test_failed_candidate_after_latest_create_time_is_ineligible(self) -> None:
        base = datetime(2026, 7, 20, 14, 42)
        engine = AuctionEngine(_test_config())
        for index, close in enumerate((100.0, 100.1, 99.95, 100.05)):
            engine.evaluate_snapshot(_snapshot(
                base + timedelta(minutes=index * 3),
                open_price=close,
                high=close + 0.2,
                low=close - 0.2,
                close=close,
                range_type="BALANCE",
                range_low=97.0,
                range_high=101.0,
                range_width_atr=4.0,
            ))
        sequence = [
            dict(open_price=100.7, high=100.9, low=100.6, close=100.8),
            dict(open_price=100.9, high=101.3, low=100.85, close=100.98),
            dict(open_price=100.95, high=101.0, low=100.65, close=100.70),
            dict(open_price=100.70, high=100.85, low=100.65, close=100.72),
        ]
        result = None
        for offset, values in enumerate(sequence, start=4):
            result = engine.evaluate_snapshot(_snapshot(
                base + timedelta(minutes=offset * 3),
                range_type="BALANCE",
                range_low=97.0,
                range_high=101.0,
                range_width_atr=4.0,
                **values,
            ))
        self.assertIsNotNone(result)
        failed = next(item for item in result.candidates if item.family is SetupFamily.FAILED_BREAKOUT)
        self.assertGreater(failed.snapshot_time.time(), datetime.strptime("15:00:00", "%H:%M:%S").time())
        self.assertEqual(failed.eligibility, CandidateEligibility.INELIGIBLE)
        self.assertIn("FAILED_INSUFFICIENT_SESSION_TIME", failed.blockers)
        self.assertTrue(failed.terminal)

    def test_initiation_subtype_is_frozen_when_later_state_becomes_trend_aligned(self) -> None:
        self._warm_compression()
        self.engine.evaluate_snapshot(_snapshot(
            self.ts + timedelta(minutes=12),
            open_price=100.7, high=100.9, low=100.6, close=100.8,
            range_type="MICRO_COMPRESSION",
        ))
        attempt = self.engine.evaluate_snapshot(_snapshot(
            self.ts + timedelta(minutes=15),
            open_price=100.9, high=101.5, low=100.85, close=101.25,
            range_type="MICRO_COMPRESSION",
        ))
        first = next(item for item in attempt.candidates if item.family is SetupFamily.BREAKOUT_INITIATION)
        self.assertEqual(first.subtype, "FRESH_EXPANSION_INITIATION")

        hold_time = self.ts + timedelta(minutes=18)
        hold_snapshot = _snapshot(
            hold_time,
            open_price=101.2, high=101.4, low=101.1, close=101.3,
            range_type="MICRO_COMPRESSION",
        )
        evidence = self.engine.evidence_builder.build(
            hold_snapshot,
            history=tuple(self.engine._history["TEST"]),
        )
        fake_state = attempt.auction_state.model_copy(update={
            "snapshot_time": hold_time,
            "current_state": AuctionStateName.ORDERLY_UPTREND,
            "transition_time": hold_time,
            "entered_at": hold_time,
            "expires_at": hold_time + timedelta(minutes=30),
        })
        candidates = self.engine.setup_engine._evaluate_initiation_watches(
            evidence,
            fake_state,
            attempt.boundary_episode,
            {"established_trend_side": "UP"},
        )
        confirmed = next(item for item in candidates if item.family is SetupFamily.BREAKOUT_INITIATION)
        self.assertEqual(confirmed.candidate_id, first.candidate_id)
        self.assertEqual(confirmed.subtype, first.subtype)
        self.assertEqual(confirmed.diagnostics["frozen_subtype"], first.subtype)

    def test_assumed_breakout_projection_never_becomes_a_room_blocker(self) -> None:
        # A narrow frozen range creates a measured-move reference with much less
        # than 0.5% remaining room. It must remain diagnostic rather than reject
        # an otherwise valid accepted breakout.
        engine = AuctionEngine(_test_config())
        low, high = 99.8, 100.2
        for index, close in enumerate((100.0, 100.02, 99.98, 100.01)):
            engine.evaluate_snapshot(_snapshot(
                self.ts + timedelta(minutes=index * 3),
                open_price=close, high=close + 0.05, low=close - 0.05, close=close,
                range_type="MICRO_COMPRESSION", range_low=low, range_high=high,
                range_width_atr=0.4, atr=1.0,
            ))
        sequence = (
            dict(open_price=100.12, high=100.18, low=100.10, close=100.16),
            dict(open_price=100.18, high=100.38, low=100.16, close=100.31),
            dict(open_price=100.30, high=100.38, low=100.26, close=100.32),
            dict(open_price=100.31, high=100.40, low=100.29, close=100.35),
        )
        result = None
        for offset, values in enumerate(sequence, start=4):
            result = engine.evaluate_snapshot(_snapshot(
                self.ts + timedelta(minutes=offset * 3),
                range_type="MICRO_COMPRESSION", range_low=low, range_high=high,
                range_width_atr=0.4, atr=1.0, **values,
            ))
        self.assertIsNotNone(result)
        accepted = next(item for item in result.candidates if item.family is SetupFamily.ACCEPTED_BREAKOUT)
        self.assertEqual(accepted.eligibility, CandidateEligibility.ELIGIBLE)
        self.assertIsNone(accepted.target_reference_price)
        self.assertIsNone(accepted.room_pct)
        self.assertLess(accepted.diagnostics["measured_move_distance_from_entry_pct"], 0.005)
        self.assertNotIn("BREAKOUT_FIRST_MOVE_BELOW_MINIMUM_PCT", accepted.blockers)
        self.assertNotIn("BREAKOUT_EXTERNAL_ROOM_ATR_INSUFFICIENT", accepted.blockers)

    def test_failed_room_uses_only_opposite_range_edge(self) -> None:
        setup = SetupCandidateEngine(_test_config())
        state_result = self.engine.evaluate_snapshot(_snapshot(
            self.ts, open_price=100.0, high=100.2, low=99.8, close=100.0,
        ))
        from services.auction_engine.setup_engine import _FailedWatch
        watch = _FailedWatch(
            symbol="TEST", candidate_id="C", event_key="E", side=TradeSide.BUY,
            subtype="NEUTRAL_RANGE_FAILED_AUCTION", event_time=self.ts,
            failed_time=self.ts, resolution_price=100.5, boundary_price=99.0,
            frozen_low=99.0, frozen_high=105.0,
            source_boundary_status=BoundaryEpisodeStatus.FAILED,
            source_boundary_resolution=BoundaryResolution.FAILED,
            source_boundary_id="B", source_boundary_side=BoundarySide.LOWER,
            source_boundary_source="DYNAMIC", source_frozen_range_id="R",
            source_frozen_range_version=1, resolution_basis="DIRECTIONAL_FOLLOWTHROUGH",
            state_at_failure=AuctionStateName.BALANCE,
            expires_at=self.ts + timedelta(minutes=15),
        )
        evidence = state_result.evidence.model_copy(update={"close": 101.0})
        room = setup._failed_room(evidence, watch)
        self.assertEqual(room[3], "FROZEN_RANGE_OPPOSITE_EDGE")
        self.assertEqual(room[0], 105.0)
        self.assertEqual(room[1], 4.0)

    def test_failed_auction_context_subtypes_are_explicit(self) -> None:
        state_result = self.engine.evaluate_snapshot(_snapshot(
            self.ts,
            open_price=100.0, high=100.2, low=99.8, close=100.0,
        ))
        setup = SetupCandidateEngine(_test_config())
        self.assertEqual(
            setup._failed_subtype(
                TradeSide.BUY,
                state_result.auction_state,
                {"established_trend_side": "UP"},
            ),
            "TREND_ALIGNED_FAILED_AUCTION",
        )
        self.assertEqual(
            setup._failed_subtype(
                TradeSide.SELL,
                state_result.auction_state,
                {"established_trend_side": "UP"},
            ),
            "COUNTERTREND_FAILED_AUCTION",
        )
        self.assertEqual(
            setup._failed_subtype(
                TradeSide.SELL,
                state_result.auction_state,
                {"established_trend_side": "UNKNOWN"},
            ),
            "NEUTRAL_RANGE_FAILED_AUCTION",
        )

    def test_candidate_lifecycle_outcomes_are_post_processed(self) -> None:
        snapshots = self._warm_compression()
        sequence = [
            _snapshot(self.ts + timedelta(minutes=12), open_price=100.7, high=100.9, low=100.6, close=100.8, range_type="MICRO_COMPRESSION"),
            _snapshot(self.ts + timedelta(minutes=15), open_price=100.9, high=101.5, low=100.85, close=101.25, range_type="MICRO_COMPRESSION"),
            _snapshot(self.ts + timedelta(minutes=18), open_price=101.2, high=101.4, low=101.1, close=101.3, range_type="MICRO_COMPRESSION"),
            _snapshot(self.ts + timedelta(minutes=21), open_price=101.25, high=101.8, low=101.2, close=101.7, range_type="MICRO_COMPRESSION"),
            _snapshot(self.ts + timedelta(minutes=24), open_price=101.7, high=102.2, low=101.6, close=102.0, range_type="MICRO_COMPRESSION"),
        ]
        observations = []
        for snapshot in sequence:
            snapshots.append(snapshot)
            result = self.engine.evaluate_snapshot(snapshot)
            observations.extend(candidate_observation_rows(result, "TEST-RUN"))
        lifecycle = build_candidate_lifecycle(observations, snapshots, (3, 6, 9))
        initiation = next(row for row in lifecycle if row["family"] == "BREAKOUT_INITIATION")
        self.assertEqual(initiation["best_eligibility"], "ELIGIBLE")
        self.assertIn("mfe_pct_3bars", initiation)
        self.assertGreater(initiation["mfe_pct_3bars"], 0.0)
        self.assertIn("eod_move_pct", initiation)

        opportunities = build_opportunity_lifecycle(lifecycle)
        grouped = next(row for row in opportunities if row["opportunity_key"] == initiation["opportunity_key"])
        self.assertGreaterEqual(grouped["candidate_record_count"], 1)
        self.assertTrue(grouped["alias_double_counting_prevented"] or grouped["candidate_record_count"] == 1)

    def test_default_phase3b_remains_non_invasive(self) -> None:
        result = self.engine.evaluate_snapshot(_snapshot(
            self.ts,
            open_price=100.0, high=100.2, low=99.8, close=100.0,
        ))
        self.assertEqual(result.local_decision.action, LocalDecisionAction.NO_OPPORTUNITY)
        self.assertEqual(result.advisor_decisions, ())
        self.assertFalse(self.engine.config.engine.enabled)
        self.assertFalse(self.engine.config.decision.create_enabled)
        self.assertFalse(self.engine.config.persistence.write_enabled)
        self.assertFalse(self.engine.config.engine.replace_current_signal_path)

    def test_initiation_shallow_retest_waits_for_outside_reclaim(self) -> None:
        self._warm_compression()
        self.engine.evaluate_snapshot(_snapshot(
            self.ts + timedelta(minutes=12),
            open_price=100.7, high=100.9, low=100.6, close=100.8,
            range_type="MICRO_COMPRESSION",
        ))
        self.engine.evaluate_snapshot(_snapshot(
            self.ts + timedelta(minutes=15),
            open_price=100.9, high=101.5, low=100.85, close=101.25,
            range_type="MICRO_COMPRESSION",
        ))
        retest = self.engine.evaluate_snapshot(_snapshot(
            self.ts + timedelta(minutes=18),
            open_price=101.05, high=101.12, low=100.86, close=100.95,
            range_type="MICRO_COMPRESSION",
        ))
        candidate = next(item for item in retest.candidates if item.family is SetupFamily.BREAKOUT_INITIATION)
        self.assertEqual(candidate.eligibility, CandidateEligibility.WATCH)
        self.assertIn("INITIATION_WAITING_FOR_OUTSIDE_RECLAIM", candidate.blockers)
        self.assertFalse(candidate.diagnostics["outside_reclaimed"])
        self.assertNotEqual(retest.local_decision.action, LocalDecisionAction.CONFIRMED)

    def test_failed_auction_first_renewed_outside_close_blocks_entry_without_terminalising(self) -> None:
        self._warm_compression(range_low=97.0, range_high=101.0)
        sequence = [
            dict(open_price=100.7, high=100.9, low=100.6, close=100.8),
            dict(open_price=100.9, high=101.3, low=100.85, close=100.98),
            dict(open_price=100.95, high=101.0, low=100.65, close=100.70),
            dict(open_price=100.70, high=100.85, low=100.65, close=100.72),
        ]
        for offset, values in enumerate(sequence, start=4):
            self.engine.evaluate_snapshot(_snapshot(
                self.ts + timedelta(minutes=offset * 3),
                range_type="BALANCE", range_low=97.0, range_high=101.0,
                range_width_atr=4.0, **values,
            ))
        renewed = self.engine.evaluate_snapshot(_snapshot(
            self.ts + timedelta(minutes=24),
            open_price=100.95, high=101.4, low=100.9, close=101.2,
            range_type="BALANCE", range_low=97.0, range_high=101.0,
            range_width_atr=4.0,
        ))
        failed = next(item for item in renewed.candidates if item.family is SetupFamily.FAILED_BREAKOUT)
        self.assertEqual(failed.eligibility, CandidateEligibility.WATCH)
        self.assertIn("FAILED_RENEWED_OUTSIDE_ENTRY_BLOCKED", failed.blockers)
        self.assertFalse(failed.terminal)
        self.assertEqual(failed.diagnostics["renewed_acceptance_closes"], 1)
        self.assertNotEqual(renewed.local_decision.action, LocalDecisionAction.CONFIRMED)


if __name__ == "__main__":
    unittest.main()
