#!/usr/bin/env python3
"""Focused tests for normal/exhaustion reversal economics and progression."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG
from enums.enums import LifecycleStage, SignalStatus
from services.auction_engine.contracts import (
    AuctionState,
    AuctionStateName,
    BarEvidence,
    BoundaryEpisodeStatus,
    BoundaryResolution,
    BoundarySide,
    CandidateEligibility,
    CandidateRole,
    DirectionalBias,
    EvidenceSnapshot,
    ExtensionEvidence,
    OpportunityEvidence,
    PriceActionEvidence,
    SetupCandidate,
    SetupFamily,
    TradeSide,
    stable_key,
)
from services.auction_engine.opportunity_ledger import OpportunityLedger
from services.auction_engine.setup_engine import SetupCandidateEngine
from services.auction_engine.setup_manager import SetupManager
from services.signals.signal_generator import (
    AuctionSignalIdentity,
    _resolve_signal_lifecycle,
)


class AuctionReversalSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = datetime(2026, 7, 24, 10, 45)
        self.version = AUCTION_ENGINE_CONFIG.engine.config_version

    def _evidence(
        self,
        *,
        ts: datetime | None = None,
        close: float = 13283.0,
        vwap: float = 13360.0,
        current_extension_atr: float | None = None,
        extended: bool | None = None,
        mature: bool | None = None,
        rejection: bool = True,
    ) -> EvidenceSnapshot:
        current_ts = ts or self.ts
        return EvidenceSnapshot(
            symbol="MARUTI",
            trading_day=current_ts.date(),
            snapshot_time=current_ts,
            close=close,
            atr=40.0,
            bar=BarEvidence(
                snapshot_time=current_ts,
                open=close - 13.0,
                high=close + 7.0,
                low=close - 18.0,
                close=close,
                volume=1000.0,
                direction=DirectionalBias.UP,
            ),
            price_action=PriceActionEvidence(
                direction=DirectionalBias.UP,
                followthrough=True,
                rejection=rejection,
                failed_extreme=rejection,
            ),
            extension=ExtensionEvidence(
                extended=extended,
                mature=mature,
                move_from_anchor_atr=current_extension_atr,
                progress_decay=0.50 if rejection else 0.05,
                failed_extreme_count=1 if rejection else 0,
            ),
            opportunity=OpportunityEvidence(session_minutes_remaining=270.0),
            raw_facts={
                "source_levels": {
                    "today_open": 13450.0,
                    "vwap": vwap,
                    "prev_day_high": 13500.0,
                    "opening_range_high": 13420.0,
                    "prev_day_low": 13150.0,
                    "opening_range_low": 13200.0,
                }
            },
            config_version=self.version,
        )

    def _state(
        self,
        *,
        ts: datetime | None = None,
        previous: AuctionStateName = AuctionStateName.TREND_FAILURE,
        current: AuctionStateName = AuctionStateName.REVERSAL,
    ) -> AuctionState:
        current_ts = ts or self.ts
        return AuctionState(
            state_key="STATE:MARUTI:REVERSAL",
            symbol="MARUTI",
            snapshot_time=current_ts,
            previous_state=previous,
            current_state=current,
            transition_time=current_ts,
            entered_at=current_ts,
            expires_at=current_ts + timedelta(minutes=30),
            config_version=self.version,
        )

    def _state_diagnostics(
        self,
        *,
        anchor: float = 13450.0,
        extreme: float = 13208.0,
        failure_atr: float = 40.0,
        established_side: str = "UP",
    ) -> dict:
        return {
            "last_failure_terminal_key": "FAILURE:MARUTI:1",
            "last_failure_terminal_reason": "CONFIRMED_OPPOSITE_REVERSAL",
            "last_failure_terminal_time": self.ts,
            "last_failure_watch_onset": self.ts - timedelta(minutes=15),
            "last_failure_side": "UP",
            "last_failure_original_trend_side": "DOWN",
            "last_failure_level": 13250.0,
            "last_failure_level_source": "LOWER_HIGH_PROTECTION",
            "last_failure_level_time": self.ts - timedelta(minutes=30),
            "last_failure_level_version": 1,
            "last_failure_level_episode_key": "PROTECTION:MARUTI:1",
            "last_failure_atr": failure_atr,
            "last_failure_structure_low": extreme,
            "last_failure_structure_high": 13250.0,
            "last_failure_trend_anchor_price": anchor,
            "last_failure_trend_extreme_price": extreme,
            "established_trend_side": established_side,
        }

    def _evaluate_once(
        self,
        *,
        anchor: float = 13450.0,
        extreme: float = 13208.0,
        rejection: bool = True,
        vwap: float = 13360.0,
    ) -> SetupCandidate:
        rows = SetupCandidateEngine().evaluate(
            self._evidence(rejection=rejection, vwap=vwap),
            self._state(),
            None,
            state_diagnostics=self._state_diagnostics(
                anchor=anchor,
                extreme=extreme,
            ),
        )
        self.assertEqual(1, len(rows))
        return rows[0]

    def _old_sell_candidate(self) -> SetupCandidate:
        event_key = "BOUNDARY_EVENT:MARUTI:SELL"
        opportunity_key = stable_key("OPPORTUNITY", event_key, TradeSide.SELL.value)
        return SetupCandidate(
            candidate_id="ACCEPT:MARUTI:SELL",
            symbol="MARUTI",
            trading_day=self.ts.date(),
            snapshot_time=self.ts - timedelta(minutes=33),
            candidate_time=self.ts - timedelta(minutes=33),
            family=SetupFamily.ACCEPTED_BREAKOUT,
            subtype="CONTINUATION_ACCEPTANCE",
            side=TradeSide.SELL,
            event_key=event_key,
            event_time=self.ts - timedelta(minutes=36),
            opportunity_key=opportunity_key,
            boundary_thesis_key=stable_key("BOUNDARY_THESIS", event_key),
            support_group_key=opportunity_key,
            candidate_role=CandidateRole.ACCEPTED_RESOLUTION_ENTRY,
            source_boundary_event_key=event_key,
            source_boundary_status=BoundaryEpisodeStatus.ACCEPTED,
            source_boundary_resolution=BoundaryResolution.ACCEPTED,
            source_boundary_resolution_basis="MULTI_CLOSE_ACCEPTANCE",
            source_boundary_id="RANGE:MARUTI:LOWER",
            source_boundary_side=BoundarySide.LOWER,
            source_boundary_source="INTRADAY_RANGE",
            source_boundary_price=13217.0,
            source_frozen_range_id="RANGE:MARUTI",
            source_frozen_range_version=1,
            source_frozen_range_low=13217.0,
            source_frozen_range_high=13300.0,
            entry_price=13208.0,
            stop_anchor_price=13217.0,
            stop_anchor_type="FROZEN_BOUNDARY",
            target_basis="OPEN_ENDED_BREAKOUT_NO_ASSUMED_TARGET",
            auction_state=AuctionStateName.ORDERLY_DOWNTREND,
            eligibility=CandidateEligibility.ELIGIBLE,
            terminal=True,
            config_version=self.version,
        )

    def test_pure_normal_reversal_is_open_ended_and_eligible(self) -> None:
        candidate = self._evaluate_once(
            rejection=False,
            anchor=13280.0,
            extreme=13230.0,
            vwap=13284.0,
        )
        self.assertEqual(SetupFamily.REVERSAL, candidate.family)
        self.assertEqual("NORMAL_REVERSAL", candidate.subtype)
        self.assertEqual(CandidateEligibility.ELIGIBLE, candidate.eligibility)
        self.assertEqual(
            "OPEN_ENDED_REVERSAL_NO_ASSUMED_TARGET",
            candidate.target_basis,
        )
        self.assertIsNone(candidate.target_reference_price)
        self.assertIsNone(candidate.room_atr)
        self.assertIsNone(candidate.room_pct)

    def test_exhaustion_reversal_uses_vwap_as_first_target(self) -> None:
        candidate = self._evaluate_once(vwap=13360.0)
        self.assertEqual("EXHAUSTION_REVERSAL", candidate.subtype)
        self.assertEqual(CandidateEligibility.ELIGIBLE, candidate.eligibility)
        self.assertEqual(
            "EXHAUSTION_REVERSAL_VWAP_FIRST_TARGET",
            candidate.target_basis,
        )
        self.assertEqual(13360.0, candidate.target_reference_price)
        self.assertGreaterEqual(
            candidate.room_atr,
            AUCTION_ENGINE_CONFIG.reversal.minimum_room_atr,
        )
        self.assertGreaterEqual(
            candidate.room_pct,
            AUCTION_ENGINE_CONFIG.reversal.minimum_room_pct,
        )

    def test_exhaustion_waits_when_vwap_room_is_insufficient(self) -> None:
        candidate = self._evaluate_once(vwap=13300.0)
        self.assertEqual("EXHAUSTION_REVERSAL", candidate.subtype)
        self.assertEqual(CandidateEligibility.WATCH, candidate.eligibility)
        self.assertFalse(candidate.terminal)
        self.assertIn(
            "EXHAUSTION_REVERSAL_VWAP_ROOM_ATR_INSUFFICIENT",
            candidate.blockers,
        )
        self.assertIn(
            "EXHAUSTION_REVERSAL_VWAP_ROOM_BELOW_MINIMUM_PCT",
            candidate.blockers,
        )

    def test_exhaustion_matures_into_open_ended_normal_reversal(self) -> None:
        engine = SetupCandidateEngine()
        diagnostics = self._state_diagnostics()

        first = engine.evaluate(
            self._evidence(vwap=13300.0),
            self._state(),
            None,
            state_diagnostics=diagnostics,
        )
        self.assertEqual(1, len(first))
        self.assertEqual("EXHAUSTION_REVERSAL", first[0].subtype)
        self.assertEqual(CandidateEligibility.WATCH, first[0].eligibility)

        later = self.ts + timedelta(minutes=9)
        second = engine.evaluate(
            self._evidence(ts=later, close=13320.0, vwap=13305.0),
            self._state(
                ts=later,
                previous=AuctionStateName.REVERSAL,
                current=AuctionStateName.ORDERLY_UPTREND,
            ),
            None,
            state_diagnostics=diagnostics,
        )
        self.assertEqual(2, len(second))
        by_subtype = {candidate.subtype: candidate for candidate in second}

        exhausted = by_subtype["EXHAUSTION_REVERSAL"]
        normal = by_subtype["NORMAL_REVERSAL"]
        self.assertEqual(CandidateEligibility.SUPERSEDED, exhausted.eligibility)
        self.assertTrue(exhausted.terminal)
        self.assertEqual(CandidateEligibility.ELIGIBLE, normal.eligibility)
        self.assertTrue(normal.terminal)
        self.assertEqual(
            "OPEN_ENDED_REVERSAL_NO_ASSUMED_TARGET",
            normal.target_basis,
        )
        self.assertIsNone(normal.target_reference_price)
        self.assertTrue(normal.diagnostics["promoted_from_exhaustion"])
        self.assertEqual(first[0].opportunity_key, normal.opportunity_key)
        self.assertNotEqual(first[0].candidate_id, normal.candidate_id)

    def test_eligible_exhaustion_still_records_later_normal_reversal(self) -> None:
        engine = SetupCandidateEngine()
        diagnostics = self._state_diagnostics()
        first = engine.evaluate(
            self._evidence(vwap=13360.0),
            self._state(),
            None,
            state_diagnostics=diagnostics,
        )
        self.assertEqual(CandidateEligibility.ELIGIBLE, first[0].eligibility)
        self.assertEqual("EXHAUSTION_REVERSAL", first[0].subtype)

        later = self.ts + timedelta(minutes=9)
        second = engine.evaluate(
            self._evidence(ts=later, close=13320.0, vwap=13305.0),
            self._state(
                ts=later,
                previous=AuctionStateName.REVERSAL,
                current=AuctionStateName.ORDERLY_UPTREND,
            ),
            None,
            state_diagnostics=diagnostics,
        )
        self.assertEqual(1, len(second))
        normal = second[0]
        self.assertEqual("NORMAL_REVERSAL", normal.subtype)
        self.assertEqual(CandidateEligibility.ELIGIBLE, normal.eligibility)
        self.assertEqual(first[0].opportunity_key, normal.opportunity_key)

    def test_ledger_prefers_normal_after_structural_control(self) -> None:
        engine = SetupCandidateEngine()
        diagnostics = self._state_diagnostics()
        exhaustion = engine.evaluate(
            self._evidence(vwap=13360.0),
            self._state(),
            None,
            state_diagnostics=diagnostics,
        )[0]
        later = self.ts + timedelta(minutes=9)
        normal = engine.evaluate(
            self._evidence(ts=later, close=13320.0, vwap=13305.0),
            self._state(
                ts=later,
                previous=AuctionStateName.REVERSAL,
                current=AuctionStateName.ORDERLY_UPTREND,
            ),
            None,
            state_diagnostics=diagnostics,
        )[0]

        ledger = OpportunityLedger()
        ledger.update("MARUTI", exhaustion.snapshot_time, [exhaustion])
        records = ledger.update("MARUTI", normal.snapshot_time, [normal])
        record = next(
            row for row in records if row.opportunity_key == normal.opportunity_key
        )
        self.assertEqual("NORMAL_REVERSAL", record.primary_candidate.subtype)
        self.assertEqual(2, len(record.candidates))

    def test_later_normal_interpretation_does_not_reopen_consumed_opportunity(self) -> None:
        engine = SetupCandidateEngine()
        diagnostics = self._state_diagnostics()
        exhaustion = engine.evaluate(
            self._evidence(vwap=13360.0),
            self._state(),
            None,
            state_diagnostics=diagnostics,
        )[0]
        later = self.ts + timedelta(minutes=9)
        normal = engine.evaluate(
            self._evidence(ts=later, close=13320.0, vwap=13305.0),
            self._state(
                ts=later,
                previous=AuctionStateName.REVERSAL,
                current=AuctionStateName.ORDERLY_UPTREND,
            ),
            None,
            state_diagnostics=diagnostics,
        )[0]

        ledger = OpportunityLedger()
        ledger.update("MARUTI", exhaustion.snapshot_time, [exhaustion])
        ledger.mark_selected(
            exhaustion.opportunity_key,
            exhaustion.snapshot_time,
            exhaustion.candidate_id,
        )
        ledger.mark_consumed(
            exhaustion.opportunity_key,
            exhaustion.snapshot_time,
            candidate_id=exhaustion.candidate_id,
        )
        records = ledger.update("MARUTI", normal.snapshot_time, [normal])
        record = next(
            row for row in records if row.opportunity_key == normal.opportunity_key
        )
        self.assertEqual("CONSUMED", record.lifecycle_state)
        self.assertEqual(2, len(record.candidates))
        self.assertEqual("NORMAL_REVERSAL", record.primary_candidate.subtype)

    def test_reversal_watch_expires_terminally(self) -> None:
        engine = SetupCandidateEngine()
        diagnostics = self._state_diagnostics()
        first = engine.evaluate(
            self._evidence(vwap=13300.0),
            self._state(),
            None,
            state_diagnostics=diagnostics,
        )
        self.assertEqual(CandidateEligibility.WATCH, first[0].eligibility)

        later = self.ts + timedelta(minutes=16)
        expired = engine.evaluate(
            self._evidence(ts=later, vwap=13300.0),
            self._state(
                ts=later,
                previous=AuctionStateName.REVERSAL,
                current=AuctionStateName.REVERSAL,
            ),
            None,
            state_diagnostics=diagnostics,
        )
        self.assertEqual(1, len(expired))
        self.assertEqual(CandidateEligibility.EXPIRED, expired[0].eligibility)
        self.assertTrue(expired[0].terminal)
        self.assertIn("REVERSAL_WATCH_EXPIRED", expired[0].blockers)

    def test_confirmed_reversal_supersedes_old_opposite_opportunity(self) -> None:
        old_sell = self._old_sell_candidate()
        reversal = self._evaluate_once(vwap=13360.0)
        ledger = OpportunityLedger()
        ledger.update("MARUTI", old_sell.snapshot_time, [old_sell])
        ledger.mark_selected(
            old_sell.opportunity_key,
            old_sell.snapshot_time,
            old_sell.candidate_id,
        )
        records = ledger.update("MARUTI", reversal.snapshot_time, [reversal])
        by_key = {record.opportunity_key: record for record in records}
        self.assertEqual(
            "SUPERSEDED",
            by_key[old_sell.opportunity_key].lifecycle_state,
        )
        self.assertEqual(
            reversal.opportunity_key,
            by_key[old_sell.opportunity_key].superseded_by_opportunity_key,
        )
        self.assertEqual(
            "ELIGIBLE",
            by_key[reversal.opportunity_key].lifecycle_state,
        )

        manager = SetupManager(AUCTION_ENGINE_CONFIG).evaluate(
            "MARUTI",
            reversal.snapshot_time,
            records,
        )
        self.assertEqual(reversal.candidate_id, manager.selected_candidate_id)

    def test_confirmed_reversal_terminally_replaces_old_signal(self) -> None:
        snapshot = SimpleNamespace(
            auction=SimpleNamespace(
                state=SimpleNamespace(current="REVERSAL"),
            ),
        )
        existing = SimpleNamespace(
            status=SignalStatus.OPEN,
            signal_id="old-sell",
            stage=LifecycleStage.ACTIVE,
        )
        identity = AuctionSignalIdentity(
            opportunity_key="OLD:SELL",
            candidate_id="OLD:CANDIDATE",
            boundary_event_key="OLD:EVENT",
            setup_family="ACCEPTED_BREAKOUT",
            setup_subtype="CONTINUATION_ACCEPTANCE",
            side="SELL",
            created_snapshot_time=self.ts - timedelta(minutes=33),
        )
        current_opportunity = SimpleNamespace(
            lifecycle="ELIGIBLE",
            side="BUY",
            primary_family="REVERSAL",
        )
        decision = _resolve_signal_lifecycle(
            snapshot=snapshot,
            existing_signal=existing,
            auction_action="LOCAL_CONFIRMED",
            active_identity=identity,
            current_opportunity=current_opportunity,
            same_opportunity=False,
            competing_confirmed_opportunity=True,
        )
        self.assertTrue(decision.terminal)
        self.assertEqual("REPLACE", decision.signal_action)
        self.assertEqual(SignalStatus.REPLACED, decision.status)
        self.assertEqual(LifecycleStage.FORCE_EXIT, decision.stage)


if __name__ == "__main__":
    unittest.main()
