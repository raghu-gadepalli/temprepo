#!/usr/bin/env python3
"""Offline tests for Phase-3A unified boundary episodes.

Run from the project root:

    python -m unittest tests.test_auction_engine_phase3 -v

The tests use synthetic snapshots only.  No database connection, signal
persistence, virtual trade or TradeManager code is invoked.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from services.auction_engine.contracts import (
    BoundaryEpisodeStatus,
    BoundaryResolution,
    LocalDecisionAction,
)
from services.auction_engine.engine import AuctionEngine
from tests.test_auction_engine_phase2 import _snapshot, _test_config
from tests.test_auction_engine_report import build_episode_summary, result_row


class UnifiedBoundaryEpisodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = AuctionEngine(_test_config())
        self.ts = datetime(2026, 7, 20, 10, 0)

    def evaluate(self, offset: int, **kwargs):
        return self.engine.evaluate_snapshot(
            _snapshot(self.ts + timedelta(minutes=offset * 3), **kwargs)
        )

    def test_non_dynamic_orb_range_is_not_an_episode(self) -> None:
        snap = _snapshot(
            self.ts,
            open_price=100.7,
            high=100.9,
            low=100.6,
            close=100.8,
        )
        snap["structure"]["accepted"]["range"]["source"] = "ORB"
        result = self.engine.evaluate_snapshot(snap)
        self.assertIsNone(result.boundary_episode)
        self.assertFalse(result.diagnostics["boundary_diagnostics"]["observation_allowed"])

    def test_approach_attempt_acceptance_share_immutable_identity(self) -> None:
        rows = [
            self.evaluate(0, open_price=100.7, high=100.9, low=100.6, close=100.8),
            self.evaluate(1, open_price=100.9, high=101.2, low=100.85, close=100.98),
            self.evaluate(2, open_price=101.0, high=101.3, low=100.95, close=101.2),
            self.evaluate(3, open_price=101.15, high=101.35, low=101.1, close=101.25),
        ]
        episodes = [row.boundary_episode for row in rows]
        self.assertEqual(
            [episode.status for episode in episodes],
            [
                BoundaryEpisodeStatus.APPROACHING,
                BoundaryEpisodeStatus.OUTSIDE_ATTEMPT,
                BoundaryEpisodeStatus.ACCEPTANCE_BUILDING,
                BoundaryEpisodeStatus.ACCEPTED,
            ],
        )
        self.assertEqual(len({episode.event_key for episode in episodes}), 1)
        self.assertEqual(len({episode.attempt_id for episode in episodes}), 1)
        self.assertFalse(episodes[0].frozen_range.diagnostics["range_frozen"])
        self.assertTrue(episodes[1].frozen_range.diagnostics["range_frozen"])
        self.assertEqual(episodes[-1].resolution, BoundaryResolution.ACCEPTED)
        self.assertTrue(episodes[-1].terminal)
        self.assertEqual(rows[-1].local_decision.action, LocalDecisionAction.CONFIRMED)
        self.assertTrue(rows[-1].candidates)
        self.assertIn("ACCEPTED_BREAKOUT", {item.family.value for item in rows[-1].candidates})

    def test_wick_attempt_reentry_and_hold_resolve_failed(self) -> None:
        rows = [
            self.evaluate(0, open_price=100.7, high=100.9, low=100.6, close=100.8),
            self.evaluate(1, open_price=100.9, high=101.3, low=100.85, close=100.98),
            self.evaluate(2, open_price=100.95, high=101.0, low=100.7, close=100.8),
            self.evaluate(3, open_price=100.8, high=100.85, low=100.5, close=100.55),
        ]
        self.assertEqual(rows[1].boundary_episode.status, BoundaryEpisodeStatus.OUTSIDE_ATTEMPT)
        self.assertEqual(rows[2].boundary_episode.status, BoundaryEpisodeStatus.FAILURE_BUILDING)
        failed = rows[3].boundary_episode
        self.assertEqual(failed.status, BoundaryEpisodeStatus.FAILED)
        self.assertEqual(failed.resolution, BoundaryResolution.FAILED)
        self.assertTrue(failed.terminal)
        self.assertGreaterEqual(failed.consecutive_inside_closes, 2)
        self.assertGreaterEqual(failed.diagnostics["failure_followthrough_atr"], 0.10)

    def test_acceptance_building_can_switch_to_failure_in_same_episode(self) -> None:
        first = self.evaluate(0, open_price=100.7, high=100.9, low=100.6, close=100.8)
        attempt = self.evaluate(1, open_price=100.9, high=101.3, low=100.85, close=100.98)
        building = self.evaluate(2, open_price=101.0, high=101.25, low=100.95, close=101.2)
        reentry = self.evaluate(3, open_price=101.15, high=101.18, low=100.7, close=100.8)
        failed = self.evaluate(4, open_price=100.8, high=100.85, low=100.45, close=100.5)

        keys = {
            result.boundary_episode.event_key
            for result in (first, attempt, building, reentry, failed)
        }
        self.assertEqual(len(keys), 1)
        self.assertEqual(building.boundary_episode.status, BoundaryEpisodeStatus.ACCEPTANCE_BUILDING)
        self.assertEqual(reentry.boundary_episode.status, BoundaryEpisodeStatus.FAILURE_BUILDING)
        self.assertEqual(failed.boundary_episode.status, BoundaryEpisodeStatus.FAILED)

    def test_newer_range_supersedes_unresolved_episode(self) -> None:
        self.evaluate(0, open_price=100.7, high=100.9, low=100.6, close=100.8)
        active = self.evaluate(1, open_price=100.9, high=101.2, low=100.85, close=100.98)
        old_key = active.boundary_episode.event_key

        snap = _snapshot(
            self.ts + timedelta(minutes=6),
            open_price=102.0,
            high=102.2,
            low=101.8,
            close=102.0,
            range_low=101.0,
            range_high=103.0,
        )
        for branch in ("accepted", "raw"):
            snap["structure"][branch]["range"]["range_id"] = "RANGE-2"
            snap["structure"][branch]["range"]["version"] = 2
            snap["structure"][branch]["range"]["start_time"] = self.ts + timedelta(minutes=6)
        superseded = self.engine.evaluate_snapshot(snap)
        self.assertIsNone(superseded.boundary_episode)
        episode = superseded.diagnostics["boundary_closed_episode"]
        self.assertEqual(episode["event_key"], old_key)
        self.assertEqual(episode["status"], BoundaryEpisodeStatus.SUPERSEDED.value)
        self.assertTrue(episode["terminal"])
        self.assertTrue(episode["superseded"])
        self.assertIsNotNone(episode["superseded_by"])


    def test_newer_range_outside_attempt_is_anchored_on_supersession_snapshot(self) -> None:
        first = self.evaluate(0, open_price=100.7, high=100.9, low=100.6, close=100.8)
        old = self.evaluate(1, open_price=100.9, high=101.2, low=100.85, close=100.98)
        old_key = old.boundary_episode.event_key

        ts = self.ts + timedelta(minutes=6)
        snap = _snapshot(
            ts,
            open_price=103.0,
            high=103.4,
            low=102.9,
            close=103.2,
            range_low=101.0,
            range_high=103.0,
        )
        for branch in ("accepted", "raw"):
            snap["structure"][branch]["range"]["range_id"] = "RANGE-2"
            snap["structure"][branch]["range"]["version"] = 2
            snap["structure"][branch]["range"]["start_time"] = ts

        result = self.engine.evaluate_snapshot(snap)
        active = result.boundary_episode
        closed = result.diagnostics["boundary_closed_episode"]
        self.assertIsNotNone(active)
        self.assertNotEqual(active.event_key, old_key)
        self.assertEqual(active.status, BoundaryEpisodeStatus.OUTSIDE_ATTEMPT)
        self.assertEqual(active.attempt_time, ts)
        self.assertEqual(closed["event_key"], old_key)
        self.assertEqual(closed["status"], BoundaryEpisodeStatus.SUPERSEDED.value)

        rows = [result_row(item, "TEST-RUN") for item in (first, old, result)]
        episodes = build_episode_summary(rows)
        by_key = {item["boundary_event_key"]: item for item in episodes}
        self.assertEqual(by_key[old_key]["final_status"], "SUPERSEDED")
        self.assertEqual(by_key[active.event_key]["attempt_time"], ts.isoformat(sep=" "))

    def test_approach_edge_switch_starts_opposite_attempt_same_snapshot(self) -> None:
        old = self.evaluate(0, open_price=100.7, high=100.9, low=100.6, close=100.8)
        old_key = old.boundary_episode.event_key
        ts = self.ts + timedelta(minutes=3)
        switched = self.engine.evaluate_snapshot(_snapshot(
            ts,
            open_price=99.1,
            high=99.2,
            low=98.6,
            close=98.8,
        ))
        active = switched.boundary_episode
        closed = switched.diagnostics["boundary_closed_episode"]
        self.assertIsNotNone(active)
        self.assertEqual(active.boundary_side.value, "LOWER")
        self.assertEqual(active.status, BoundaryEpisodeStatus.OUTSIDE_ATTEMPT)
        self.assertEqual(active.attempt_time, ts)
        self.assertEqual(closed["event_key"], old_key)
        self.assertEqual(closed["status"], BoundaryEpisodeStatus.STALE.value)
        row = result_row(switched, "TEST-RUN")
        self.assertEqual(row["observed_boundary_side"], "LOWER")
        self.assertEqual(row["episode_boundary_side"], "LOWER")
        self.assertEqual(row["closed_episode_boundary_side"], "UPPER")

    def test_deep_reentry_hold_has_distinct_failure_resolution_basis(self) -> None:
        self.evaluate(0, open_price=100.7, high=100.9, low=100.6, close=100.8)
        self.evaluate(1, open_price=100.9, high=101.3, low=100.85, close=100.98)
        self.evaluate(2, open_price=100.95, high=101.0, low=100.65, close=100.70)
        failed = self.evaluate(3, open_price=100.70, high=100.85, low=100.65, close=100.72)
        episode = failed.boundary_episode
        self.assertEqual(episode.status, BoundaryEpisodeStatus.FAILED)
        self.assertEqual(
            episode.terminal_reason,
            "DEEP_REENTRY_AND_INSIDE_HOLD_CONFIRMED",
        )
        self.assertEqual(
            episode.diagnostics["failure_resolution_basis"],
            "DEEP_REENTRY_INSIDE_HOLD",
        )
        self.assertLess(
            episode.diagnostics["failure_followthrough_atr"],
            self.engine.config.boundary.failure_followthrough_atr,
        )

    def test_episode_report_separates_resolution_and_archive_time(self) -> None:
        results = [
            self.evaluate(0, open_price=100.7, high=100.9, low=100.6, close=100.8),
            self.evaluate(1, open_price=100.9, high=101.2, low=100.85, close=100.98),
            self.evaluate(2, open_price=101.0, high=101.3, low=100.95, close=101.2),
            self.evaluate(3, open_price=101.15, high=101.35, low=101.1, close=101.25),
            self.evaluate(4, open_price=101.2, high=101.4, low=101.1, close=101.3),
            self.evaluate(5, open_price=100.9, high=101.0, low=100.6, close=100.7),
            self.evaluate(6, open_price=100.7, high=100.8, low=100.5, close=100.6),
        ]
        rows = [result_row(item, "TEST-RUN") for item in results]
        episodes = build_episode_summary(rows)
        self.assertEqual(len(episodes), 1)
        episode = episodes[0]
        self.assertEqual(episode["resolution"], "ACCEPTED")
        self.assertEqual(episode["bars_to_resolution"], 2)
        self.assertEqual(episode["minutes_to_resolution"], 6.0)
        self.assertIsNotNone(episode["archive_time"])
        self.assertEqual(
            episode["archive_reason"],
            "TERMINAL_EPISODE_RESET_INSIDE_VALUE",
        )
        self.assertGreaterEqual(episode["post_terminal_protection_bars"], 3)

    def test_terminal_episode_does_not_reactivate_until_reset(self) -> None:
        accepted_rows = [
            self.evaluate(0, open_price=100.7, high=100.9, low=100.6, close=100.8),
            self.evaluate(1, open_price=100.9, high=101.2, low=100.85, close=100.98),
            self.evaluate(2, open_price=101.0, high=101.3, low=100.95, close=101.2),
            self.evaluate(3, open_price=101.15, high=101.35, low=101.1, close=101.25),
        ]
        event_key = accepted_rows[-1].boundary_episode.event_key
        protected = self.evaluate(4, open_price=101.2, high=101.4, low=101.1, close=101.3)
        self.assertEqual(protected.boundary_episode.event_key, event_key)
        self.assertEqual(protected.boundary_episode.status, BoundaryEpisodeStatus.ACCEPTED)

        reset_one = self.evaluate(5, open_price=100.9, high=101.0, low=100.6, close=100.7)
        self.assertEqual(reset_one.boundary_episode.reset_inside_closes, 1)
        reset_done = self.evaluate(6, open_price=100.7, high=100.8, low=100.5, close=100.6)
        self.assertIsNone(reset_done.boundary_episode)

        restarted = self.evaluate(7, open_price=100.7, high=100.95, low=100.6, close=100.8)
        self.assertIsNotNone(restarted.boundary_episode)
        self.assertNotEqual(restarted.boundary_episode.event_key, event_key)
        self.assertEqual(restarted.boundary_episode.episode_sequence, 2)


    def test_episode_report_collapses_progression_by_event_key(self) -> None:
        results = [
            self.evaluate(0, open_price=100.7, high=100.9, low=100.6, close=100.8),
            self.evaluate(1, open_price=100.9, high=101.2, low=100.85, close=100.98),
            self.evaluate(2, open_price=101.0, high=101.3, low=100.95, close=101.2),
            self.evaluate(3, open_price=101.15, high=101.35, low=101.1, close=101.25),
        ]
        rows = [result_row(result, "TEST-RUN") for result in results]
        episodes = build_episode_summary(rows)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(
            episodes[0]["status_progression"],
            "APPROACHING>OUTSIDE_ATTEMPT>ACCEPTANCE_BUILDING>ACCEPTED",
        )
        self.assertEqual(episodes[0]["resolution"], "ACCEPTED")
        self.assertTrue(episodes[0]["terminal"])

    def test_boundary_report_is_observation_only(self) -> None:
        result = self.evaluate(
            0,
            open_price=100.7,
            high=100.9,
            low=100.6,
            close=100.8,
        )
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.advisor_decisions, ())
        self.assertEqual(result.local_decision.action, LocalDecisionAction.NO_OPPORTUNITY)
        self.assertFalse(self.engine.config.decision.create_enabled)
        self.assertFalse(self.engine.config.persistence.write_enabled)


if __name__ == "__main__":
    unittest.main()
