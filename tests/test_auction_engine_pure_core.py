#!/usr/bin/env python3
"""Offline validation for the signal-agnostic Auction Engine core.

Run from the project root:

    python -m unittest tests.test_auction_engine_pure_core -v

No database connection is opened and the signal/trade pipeline is not called.
"""
from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timedelta

from configs.auction_engine_config import AuctionEngineConfig
from services.auction_engine.contracts import (
    LocalDecisionAction,
    ManagerAction,
    ManagerDecision,
)
from services.auction_engine.decision_engine import DecisionEngine
from services.auction_engine.engine import AuctionEngine
from services.auction_engine.setup_manager import SetupManager
from tests.test_auction_engine_phase2 import _snapshot, _test_config


class PureAuctionCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = datetime(2026, 7, 20, 10, 0)

    @staticmethod
    def _sequence():
        return (
            (0, dict(open_price=100.0, high=100.2, low=99.8, close=100.0)),
            (1, dict(open_price=100.1, high=100.3, low=99.9, close=100.1)),
            (2, dict(open_price=99.95, high=100.15, low=99.75, close=99.95)),
            (3, dict(open_price=100.05, high=100.25, low=99.85, close=100.05)),
            (4, dict(open_price=100.7, high=100.9, low=100.6, close=100.8)),
            (5, dict(open_price=100.9, high=101.5, low=100.85, close=101.25)),
            (6, dict(open_price=101.2, high=101.4, low=101.1, close=101.3)),
            (7, dict(open_price=101.25, high=101.45, low=101.2, close=101.35)),
        )

    def _run(self, config: AuctionEngineConfig | None = None):
        engine = AuctionEngine(config or _test_config())
        rows = []
        for offset, values in self._sequence():
            rows.append(
                engine.evaluate_snapshot(
                    _snapshot(
                        self.ts + timedelta(minutes=offset * 3),
                        range_type="MICRO_COMPRESSION",
                        range_low=99.0,
                        range_high=101.0,
                        range_width_atr=2.0,
                        **values,
                    )
                )
            )
        return engine, rows

    def test_public_engine_call_has_no_active_signal_context(self):
        signature = inspect.signature(AuctionEngine.evaluate_snapshot)
        self.assertNotIn("active_context", signature.parameters)
        manager_signature = inspect.signature(SetupManager.evaluate)
        self.assertNotIn("active_context", manager_signature.parameters)

    def test_engine_has_no_advisor_or_active_context_runtime(self):
        engine = AuctionEngine(_test_config())
        self.assertFalse(hasattr(engine, "context_advisor"))
        self.assertFalse(hasattr(engine, "active_context_provider"))

    def test_eligible_opportunity_becomes_local_confirmed(self):
        engine, rows = self._run()
        confirmed = [
            row
            for row in rows
            if row.local_decision.action is LocalDecisionAction.CONFIRMED
        ]
        self.assertTrue(confirmed)
        result = confirmed[0]
        self.assertEqual(result.manager_decision.action, ManagerAction.SELECT)
        self.assertIsNotNone(result.local_decision.selected_candidate)
        self.assertIsNone(result.final_decision)
        self.assertEqual(result.advisor_decisions, ())
        self.assertFalse(result.diagnostics["signal_lifecycle_applied"])
        self.assertFalse(result.diagnostics["advisor_context_applied"])

        key = result.local_decision.selected_candidate.opportunity_key
        record = next(
            item
            for item in engine.opportunity_ledger.records()
            if item.opportunity_key == key
        )
        self.assertEqual(record.lifecycle_state, "ELIGIBLE")
        self.assertIsNone(record.consumed_time)
        self.assertIsNotNone(record.selected_time)
        self.assertEqual(record.decision_count, 1)

    def test_repeated_snapshot_does_not_consume_or_reselect_opportunity(self):
        engine, rows = self._run()
        first = next(
            row
            for row in rows
            if row.local_decision.action is LocalDecisionAction.CONFIRMED
        )
        candidate = first.local_decision.selected_candidate
        record = next(
            item
            for item in engine.opportunity_ledger.records()
            if item.opportunity_key == candidate.opportunity_key
        )
        selected_time = record.selected_time

        later = engine.evaluate_snapshot(
            _snapshot(
                self.ts + timedelta(minutes=25),
                open_price=101.3,
                high=101.5,
                low=101.2,
                close=101.4,
                range_type="MICRO_COMPRESSION",
                range_low=99.0,
                range_high=101.0,
                range_width_atr=2.0,
            )
        )
        record = next(
            item
            for item in engine.opportunity_ledger.records()
            if item.opportunity_key == candidate.opportunity_key
        )
        self.assertIsNone(record.consumed_time)
        self.assertEqual(record.selected_time, selected_time)
        self.assertEqual(record.decision_count, 1)
        self.assertNotIn(
            later.local_decision.action,
            {LocalDecisionAction.NO_OPPORTUNITY},
        )

    def test_advisor_and_active_context_config_do_not_change_local_result(self):
        base = _test_config()
        payload = base.resolved_dict()
        payload["advisor"]["observation_only"] = False
        payload["advisor"]["enforcement_mode"] = "FULL_ENFORCEMENT"
        payload["advisor"]["enforcement_enabled"] = True
        payload["decision"]["active_context_mode"] = "FULL_ENFORCEMENT"
        payload["decision"]["active_context_final_decision_enabled"] = True
        altered = AuctionEngineConfig.model_validate(payload)

        _, base_rows = self._run(base)
        _, altered_rows = self._run(altered)
        base_signature = [
            (
                row.local_decision.action.value,
                row.manager_decision.action.value,
                tuple(candidate.candidate_id for candidate in row.candidates),
            )
            for row in base_rows
        ]
        altered_signature = [
            (
                row.local_decision.action.value,
                row.manager_decision.action.value,
                tuple(candidate.candidate_id for candidate in row.candidates),
            )
            for row in altered_rows
        ]
        self.assertEqual(base_signature, altered_signature)

    def test_invalid_stop_geometry_is_local_block(self):
        _, rows = self._run()
        confirmed = next(
            row
            for row in rows
            if row.local_decision.action is LocalDecisionAction.CONFIRMED
        )
        selected = confirmed.local_decision.selected_candidate
        invalid = selected.model_copy(
            update={"stop_anchor_price": selected.entry_price + 1.0}
        )
        manager = ManagerDecision(
            symbol=invalid.symbol,
            snapshot_time=invalid.snapshot_time,
            action=ManagerAction.SELECT,
            selected_candidate_id=invalid.candidate_id,
            config_version=_test_config().engine.config_version,
        )
        decision = DecisionEngine(_test_config()).evaluate(
            manager=manager,
            selected=invalid,
        )
        self.assertEqual(decision.action, LocalDecisionAction.BLOCKED)
        self.assertIn("INVALID_STRUCTURAL_STOP_GEOMETRY", decision.reason_codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
