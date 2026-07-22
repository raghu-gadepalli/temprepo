#!/usr/bin/env python3
"""Offline unittest for snapshot-carried Auction Engine continuity."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import unittest

from services.auction_engine.engine import AuctionEngine
from services.auction_engine.snapshot_adapter import enrich_snapshot_with_auction
from tests.test_auction_engine_phase2 import _snapshot, _test_config


class AuctionSnapshotStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = datetime(2026, 7, 20, 10, 0)

    def _rows(self):
        sequence = (
            (0, dict(open_price=100.0, high=100.2, low=99.8, close=100.0)),
            (1, dict(open_price=100.1, high=100.3, low=99.9, close=100.1)),
            (2, dict(open_price=99.95, high=100.15, low=99.75, close=99.95)),
            (3, dict(open_price=100.05, high=100.25, low=99.85, close=100.05)),
            (4, dict(open_price=100.7, high=100.9, low=100.6, close=100.8)),
            (5, dict(open_price=100.9, high=101.5, low=100.85, close=101.25)),
            (6, dict(open_price=101.2, high=101.4, low=101.1, close=101.3)),
            (7, dict(open_price=101.25, high=101.45, low=101.2, close=101.35)),
        )
        return [
            _snapshot(
                self.ts + timedelta(minutes=offset * 3),
                range_type="MICRO_COMPRESSION",
                range_low=99.0,
                range_high=101.0,
                range_width_atr=2.0,
                **values,
            )
            for offset, values in sequence
        ]

    @staticmethod
    def _signature(result, engine):
        boundary = result.boundary_episode
        return {
            "state": result.auction_state.current_state.value,
            "boundary_event_key": boundary.event_key if boundary else None,
            "boundary_status": boundary.status.value if boundary else None,
            "candidate_ids": tuple(item.candidate_id for item in result.candidates),
            "candidate_eligibility": tuple(
                item.eligibility.value for item in result.candidates
            ),
            "manager_action": result.manager_decision.action.value,
            "local_action": result.local_decision.action.value,
            "selected_candidate_id": result.manager_decision.selected_candidate_id,
            "opportunities": tuple(
                sorted(
                    (
                        item.opportunity_key,
                        item.lifecycle_state,
                        item.selected_candidate_id,
                    )
                    for item in engine.opportunity_ledger.records("TEST")
                )
            ),
        }

    def test_incremental_state_matches_continuous_engine(self):
        config = _test_config()
        continuous = AuctionEngine(config)
        carried_state = None

        for row in self._rows():
            expected = continuous.evaluate_snapshot(row)

            incremental = AuctionEngine(config)
            if carried_state is not None:
                incremental.restore_incremental_state("TEST", carried_state)
            actual = incremental.evaluate_snapshot(row)

            self.assertEqual(
                self._signature(expected, continuous),
                self._signature(actual, incremental),
            )
            carried_state = incremental.export_incremental_state("TEST")

        self.assertIsNotNone(carried_state)
        encoded = json.dumps(
            carried_state,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        self.assertLess(len(encoded), 60_000)
        self.assertNotIn("__kind__", carried_state)
        self.assertNotIn("services.auction_engine", encoded.decode("utf-8"))


    def test_snapshot_generator_uses_single_snapshot_write(self):
        from pathlib import Path

        source = Path("services/snapshot/snapshot_generator.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("SnapshotSchema.create_snapshot(snapshot)", source)
        self.assertNotIn("SnapshotSchema.update_snapshot(", source)
        self.assertIn("STRUCTURE_INCREMENTAL_MEMORY_V1", source)

    def test_snapshot_adapter_uses_previous_snapshot_continuity(self):
        previous = None
        blocks = []
        for row in self._rows()[:3]:
            current = dict(row)
            current["symbol"] = "COFORGE"
            block = enrich_snapshot_with_auction(
                current,
                previous_payload=previous,
            )
            self.assertEqual(block["status"], "OK")
            blocks.append(block)
            previous = {**current, "auction": block}

        self.assertEqual(blocks[0]["continuity_mode"], "COLD_START")
        self.assertEqual(
            blocks[1]["continuity_mode"],
            "INCREMENTAL_PREVIOUS_SNAPSHOT",
        )
        self.assertEqual(
            blocks[2]["continuity_mode"],
            "INCREMENTAL_PREVIOUS_SNAPSHOT",
        )
        self.assertGreater(blocks[-1]["continuity_bytes"], 0)
        self.assertTrue(blocks[-1]["continuity_hash"])
        self.assertFalse(blocks[-1]["diagnostics"]["signal_lifecycle_applied"])
        self.assertFalse(blocks[-1]["diagnostics"]["advisor_context_applied"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
