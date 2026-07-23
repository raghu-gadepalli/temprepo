#!/usr/bin/env python3
"""Offline contract smoke tests for auction-engine Phase 1.

Run from the AutoTrades project root:

    python -m unittest tests.test_auction_engine_contracts -v

These tests do not connect to the database and do not invoke the current signal
pipeline.
"""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta

from pydantic import ValidationError

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from services.auction_engine.contracts import (
    AuctionState,
    AuctionStateName,
    BarEvidence,
    BoundaryEpisode,
    BoundaryEpisodeStatus,
    BoundaryResolution,
    BoundarySide,
    CandidateEligibility,
    CandidateRole,
    DirectionalBias,
    EvidenceFact,
    EvidencePolarity,
    EvidenceSnapshot,
    FrozenRange,
    ManagerAction,
    ManagerDecision,
    SetupCandidate,
    SetupFamily,
    TradeSide,
    stable_key,
)


class AuctionEngineConfigTests(unittest.TestCase):
    def test_default_config_is_non_invasive(self) -> None:
        self.assertFalse(AUCTION_ENGINE_CONFIG.engine.enabled)
        self.assertFalse(AUCTION_ENGINE_CONFIG.engine.replace_current_signal_path)
        self.assertFalse(AUCTION_ENGINE_CONFIG.decision.create_enabled)

    def test_config_hash_is_stable_and_json_safe(self) -> None:
        first = AUCTION_ENGINE_CONFIG.stable_hash()
        second = AuctionEngineConfig().stable_hash()
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        json.dumps(AUCTION_ENGINE_CONFIG.resolved_dict(), sort_keys=True)

    def test_config_rejects_unknown_fields(self) -> None:
        with self.assertRaises(ValidationError):
            AuctionEngineConfig(unknown_section={})

    def test_config_is_frozen(self) -> None:
        with self.assertRaises(ValidationError):
            AUCTION_ENGINE_CONFIG.engine.enabled = True


class AuctionEngineContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = datetime(2026, 7, 20, 10, 0, 0)
        self.version = AUCTION_ENGINE_CONFIG.engine.config_version

    def _bar(self) -> BarEvidence:
        return BarEvidence(
            snapshot_time=self.ts,
            open=100.0,
            high=102.0,
            low=99.5,
            close=101.5,
            volume=10000,
            direction=DirectionalBias.UP,
            body_fraction=0.60,
            close_position=0.80,
        )

    def _candidate(self) -> SetupCandidate:
        return SetupCandidate(
            candidate_id="candidate-1",
            symbol="TEST",
            trading_day=self.ts.date(),
            snapshot_time=self.ts,
            candidate_time=self.ts,
            family=SetupFamily.BREAKOUT_INITIATION,
            subtype="FRESH_BALANCE_DEPARTURE",
            side=TradeSide.BUY,
            event_key="event-1",
            event_time=self.ts - timedelta(minutes=3),
            opportunity_key="opportunity-1",
            boundary_thesis_key="boundary-thesis-1",
            support_group_key="opportunity-1",
            candidate_role=CandidateRole.EARLY_INITIATION,
            source_boundary_event_key="event-1",
            source_boundary_status=BoundaryEpisodeStatus.OUTSIDE_ATTEMPT,
            source_boundary_resolution=BoundaryResolution.UNRESOLVED,
            source_boundary_id="range-1:UPPER",
            source_boundary_side=BoundarySide.UPPER,
            source_boundary_source="MICRO_COMPRESSION",
            source_boundary_price=100.0,
            source_frozen_range_id="range-1",
            source_frozen_range_version=1,
            source_frozen_range_low=99.0,
            source_frozen_range_high=100.0,
            entry_price=101.5,
            stop_anchor_price=100.0,
            stop_anchor_type="FROZEN_RANGE_HIGH",
            target_basis="EXTERNAL_ROOM",
            room_atr=1.5,
            entry_distance_atr=0.4,
            freshness_minutes=3.0,
            auction_state=AuctionStateName.FRESH_EXPANSION,
            eligibility=CandidateEligibility.ELIGIBLE,
            config_version=self.version,
        )

    def test_stable_key_is_deterministic(self) -> None:
        a = stable_key("episode", "TEST", date(2026, 7, 20), "RANGE-1", 1, "UPPER")
        b = stable_key("episode", "TEST", date(2026, 7, 20), "RANGE-1", 1, "UPPER")
        c = stable_key("episode", "TEST", date(2026, 7, 20), "RANGE-1", 2, "UPPER")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_evidence_snapshot_is_causal(self) -> None:
        evidence = EvidenceSnapshot(
            symbol="test",
            trading_day=self.ts.date(),
            snapshot_time=self.ts,
            close=101.5,
            atr=1.0,
            bar=self._bar(),
            config_version=self.version,
        )
        self.assertEqual(evidence.symbol, "TEST")
        json.dumps(evidence.to_storage_dict())

        future_fact = EvidenceFact(
            code="FUTURE_FACT",
            domain="price_action",
            polarity=EvidencePolarity.SUPPORT,
            observed_at=self.ts + timedelta(minutes=3),
        )
        with self.assertRaises(ValidationError):
            EvidenceSnapshot(
                symbol="TEST",
                trading_day=self.ts.date(),
                snapshot_time=self.ts,
                close=101.5,
                atr=1.0,
                bar=self._bar(),
                price_action={"supporting_facts": [future_fact]},
                config_version=self.version,
            )

    def test_frozen_range_rejects_inverted_values(self) -> None:
        with self.assertRaises(ValidationError):
            FrozenRange(
                range_id="range-1",
                range_version=1,
                source="INTRADAY_BALANCE",
                low=102.0,
                high=100.0,
                start_time=self.ts - timedelta(minutes=30),
                frozen_at=self.ts - timedelta(minutes=3),
            )

    def test_accepted_episode_requires_accepted_time(self) -> None:
        frozen_range = FrozenRange(
            range_id="range-1",
            range_version=1,
            source="INTRADAY_BALANCE",
            low=98.0,
            high=100.0,
            start_time=self.ts - timedelta(minutes=30),
            frozen_at=self.ts - timedelta(minutes=3),
        )
        with self.assertRaises(ValidationError):
            BoundaryEpisode(
                event_key="event-1",
                structural_key="structure-1",
                attempt_id="attempt-1",
                symbol="TEST",
                trading_day=self.ts.date(),
                snapshot_time=self.ts,
                event_time=self.ts - timedelta(minutes=3),
                first_seen_time=self.ts - timedelta(minutes=3),
                last_seen_time=self.ts,
                boundary_id="range-1:upper",
                boundary_side=BoundarySide.UPPER,
                boundary_source="INTRADAY_BALANCE",
                boundary_price=100.0,
                breakout_side=TradeSide.BUY,
                failure_side=TradeSide.SELL,
                frozen_range=frozen_range,
                status=BoundaryEpisodeStatus.ACCEPTED,
                resolution=BoundaryResolution.ACCEPTED,
                config_version=self.version,
            )

    def test_eligible_candidate_cannot_have_blockers(self) -> None:
        payload = self._candidate().model_dump(mode="python")
        payload["blockers"] = ("NO_ROOM",)
        with self.assertRaises(ValidationError):
            SetupCandidate.model_validate(payload)

    def test_auction_state_rejects_future_transition(self) -> None:
        with self.assertRaises(ValidationError):
            AuctionState(
                state_key="state-1",
                symbol="TEST",
                snapshot_time=self.ts,
                previous_state=AuctionStateName.BALANCE,
                current_state=AuctionStateName.FRESH_EXPANSION,
                transition_time=self.ts + timedelta(minutes=3),
                entered_at=self.ts,
                config_version=self.version,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
