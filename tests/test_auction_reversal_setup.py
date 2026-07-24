#!/usr/bin/env python3
"""Focused tests for confirmed normal/exhaustion reversal deployment."""

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
                    "vwap": 13360.0,
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
            "established_trend_side": "UP",
        }

    def _reversal_candidate(
        self,
        *,
        current_extension_atr: float | None = None,
        extended: bool | None = None,
        mature: bool | None = None,
        rejection: bool = True,
        anchor: float = 13450.0,
        extreme: float = 13208.0,
    ) -> SetupCandidate:
        rows = SetupCandidateEngine().evaluate(
            self._evidence(
                current_extension_atr=current_extension_atr,
                extended=extended,
                mature=mature,
                rejection=rejection,
            ),
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

    def test_confirmed_normal_reversal_is_eligible(self) -> None:
        candidate = self._reversal_candidate(
            rejection=False,
            anchor=13280.0,
            extreme=13230.0,
        )
        self.assertEqual(SetupFamily.REVERSAL, candidate.family)
        self.assertEqual("NORMAL_REVERSAL", candidate.subtype)
        self.assertEqual(TradeSide.BUY, candidate.side)
        self.assertEqual(CandidateEligibility.ELIGIBLE, candidate.eligibility)
        self.assertEqual("CONFIRMED_TREND_FAILURE_LEVEL", candidate.stop_anchor_type)
        self.assertLess(candidate.stop_anchor_price, candidate.entry_price)

    def test_exhaustion_subtype_uses_frozen_prior_trend_geometry(self) -> None:
        candidate = self._reversal_candidate(
            current_extension_atr=None,
            extended=False,
            mature=False,
            rejection=True,
            anchor=13450.0,
            extreme=13208.0,
        )
        self.assertEqual("EXHAUSTION_REVERSAL", candidate.subtype)
        diag = candidate.diagnostics["exhaustion_classification"]
        self.assertGreater(diag["frozen_prior_move_atr"], 1.50)
        self.assertIsNone(diag["current_extension_move_atr"])
        self.assertTrue(diag["classified_exhaustion"])

    def test_confirmed_terminal_event_is_not_lost_after_transition_snapshot(self) -> None:
        later = self.ts + timedelta(minutes=3)
        rows = SetupCandidateEngine().evaluate(
            self._evidence(ts=later),
            self._state(
                ts=later,
                previous=AuctionStateName.REVERSAL,
                current=AuctionStateName.REVERSAL,
            ),
            None,
            state_diagnostics=self._state_diagnostics(),
        )
        self.assertEqual(1, len(rows))
        self.assertEqual(CandidateEligibility.ELIGIBLE, rows[0].eligibility)
        self.assertTrue(rows[0].diagnostics["watch_re_evaluated"])

    def test_blocked_confirmation_remains_watch_and_becomes_eligible_later(self) -> None:
        engine = SetupCandidateEngine()
        diagnostics = self._state_diagnostics(
            anchor=13350.0,
            extreme=13220.0,
        )
        first = engine.evaluate(
            self._evidence(
                close=13300.0,
                rejection=False,
            ),
            self._state(),
            None,
            state_diagnostics=diagnostics,
        )
        self.assertEqual(1, len(first))
        self.assertEqual(CandidateEligibility.WATCH, first[0].eligibility)
        self.assertFalse(first[0].terminal)
        self.assertIn("REVERSAL_ROOM_BELOW_MINIMUM_PCT", first[0].blockers)

        later = self.ts + timedelta(minutes=3)
        second = engine.evaluate(
            self._evidence(
                ts=later,
                close=13280.0,
                rejection=False,
            ),
            self._state(
                ts=later,
                previous=AuctionStateName.REVERSAL,
                current=AuctionStateName.ORDERLY_UPTREND,
            ),
            None,
            state_diagnostics=diagnostics,
        )
        self.assertEqual(1, len(second))
        self.assertEqual(first[0].candidate_id, second[0].candidate_id)
        self.assertEqual(CandidateEligibility.ELIGIBLE, second[0].eligibility)
        self.assertTrue(second[0].terminal)
        self.assertTrue(second[0].diagnostics["watch_re_evaluated"])

    def test_reversal_watch_expires_terminally(self) -> None:
        engine = SetupCandidateEngine()
        diagnostics = self._state_diagnostics(
            anchor=13350.0,
            extreme=13220.0,
        )
        first = engine.evaluate(
            self._evidence(close=13300.0, rejection=False),
            self._state(),
            None,
            state_diagnostics=diagnostics,
        )
        self.assertEqual(CandidateEligibility.WATCH, first[0].eligibility)

        later = self.ts + timedelta(minutes=16)
        expired = engine.evaluate(
            self._evidence(ts=later, close=13300.0, rejection=False),
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
        reversal = self._reversal_candidate()
        ledger = OpportunityLedger()
        ledger.update(
            "MARUTI",
            old_sell.snapshot_time,
            [old_sell],
        )
        ledger.mark_selected(
            old_sell.opportunity_key,
            old_sell.snapshot_time,
            old_sell.candidate_id,
        )
        records = ledger.update(
            "MARUTI",
            reversal.snapshot_time,
            [reversal],
        )
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
