#!/usr/bin/env python3
"""Phase 5A single-owner service/checkpoint contract tests."""
from __future__ import annotations

import sys
import json
import unittest
from datetime import datetime, timedelta
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from services.auction_engine.active_context import ActiveContext
from services.auction_engine.engine import AuctionEngine
from services.auction_engine.decision_engine import DecisionEngine
from services.auction_engine.contracts import FinalAction, TradeSide
from services.auction_engine.service_runner import AuctionServiceRunner
from services.signals.signal_lifecycle_service import (
    SignalLifecycleResult,
    SignalLifecycleService,
    _deterministic_signal_id,
    _execution_reason_codes,
)
from services.auction_engine.checkpoint_codec import checkpoint_state_hash
from tests.test_auction_engine_phase2 import _snapshot, _test_config


class _FakeSignalService:
    def __init__(self):
        self.calls = []

    def reset(self):
        self.calls.clear()

    def resolve_equity_ref(self, symbol):
        self.calls.append(("RESOLVE", symbol))
        return symbol

    def get_active_context(self, *, snapshot, equity_ref=None):
        self.calls.append(("CONTEXT", equity_ref))
        return ActiveContext(
            equity_ref=equity_ref or (snapshot.get("symbol") if isinstance(snapshot, dict) else snapshot.symbol),
            snapshot_time=(snapshot.get("snapshot_time") if isinstance(snapshot, dict) else snapshot.snapshot_time),
            evaluation_status="AVAILABLE",
            reason_codes=("NO_ACTIVE_SIGNAL_AS_OF_SNAPSHOT",),
        )

    def apply_instruction(self, *, snapshot, result, context_before):
        self.calls.append(("APPLY", result.symbol))
        action = (
            "WOULD_CREATE"
            if result.final_decision.action.value == "CREATE"
            else "NO_ACTION"
        )
        return SignalLifecycleResult(
            symbol=result.symbol,
            snapshot_time=result.snapshot_time,
            requested_action=result.final_decision.action.value,
            applied_action=action,
            opportunity_key=(
                result.final_decision.selected_candidate.opportunity_key
                if result.final_decision.selected_candidate else None
            ),
        )


class _FakePersistence:
    def reset(self):
        pass

    def load_checkpoint(self, *, trading_day, symbol):
        return None

    def persist_after_signal(self, *, result, engine, signal_result):
        return {"opportunities_written": 0, "checkpoints_written": 0}


class Phase5ATests(unittest.TestCase):
    def setUp(self):
        self.ts = datetime(2026, 7, 20, 10, 0)
        self.config = _test_config()

    def _rows(self):
        closes = (100.0, 100.1, 99.95, 100.05, 100.8, 101.25, 101.3, 101.35)
        rows = []
        for index, close in enumerate(closes):
            rows.append(_snapshot(
                self.ts + timedelta(minutes=index * 3),
                open_price=close,
                high=close + 0.2,
                low=close - 0.2,
                close=close,
                range_type="MICRO_COMPRESSION",
                range_low=99.0,
                range_high=101.0,
                range_width_atr=2.0,
            ))
        return rows

    def test_checkpoint_roundtrip_matches_continuous_engine(self):
        rows = self._rows()
        continuous = AuctionEngine(self.config)
        continuous_results = [continuous.evaluate_snapshot(row) for row in rows]

        first = AuctionEngine(self.config)
        for row in rows[:5]:
            first.evaluate_snapshot(row)
        checkpoint = first.export_checkpoint("TEST")
        # Exercise the actual JSON/database boundary. ``str, Enum`` values keep
        # their Python type in a direct in-memory call but become plain strings
        # after JSON serialization unless the checkpoint codec tags them first.
        checkpoint = json.loads(json.dumps(checkpoint))

        restored = AuctionEngine(self.config)
        restored.restore_checkpoint("TEST", checkpoint)
        restored_results = [restored.evaluate_snapshot(row) for row in rows[5:]]

        self.assertEqual(
            restored_results[-1].stable_hash(),
            continuous_results[-1].stable_hash(),
        )
        self.assertEqual(
            restored.opportunity_ledger.record_dicts("TEST"),
            continuous.opportunity_ledger.record_dicts("TEST"),
        )

    def test_checkpoint_restore_accepts_legacy_enum_strings(self):
        rows = self._rows()
        first = AuctionEngine(self.config)
        for row in rows[:5]:
            first.evaluate_snapshot(row)

        payload = json.loads(json.dumps(first.export_checkpoint("TEST")))

        def legacy_enum_strings(value):
            if isinstance(value, list):
                return [legacy_enum_strings(item) for item in value]
            if not isinstance(value, dict):
                return value
            if value.get("__kind__") == "enum":
                return value.get("value")
            return {key: legacy_enum_strings(item) for key, item in value.items()}

        restored = AuctionEngine(self.config)
        restored.restore_checkpoint("TEST", legacy_enum_strings(payload))
        memory = restored.state_engine._memory["TEST"]
        self.assertTrue(hasattr(memory.established_trend_side, "value"))

        continuous = AuctionEngine(self.config)
        continuous_results = [continuous.evaluate_snapshot(row) for row in rows]
        restored_results = [restored.evaluate_snapshot(row) for row in rows[5:]]
        self.assertEqual(
            restored_results[-1].stable_hash(),
            continuous_results[-1].stable_hash(),
        )

    def test_active_context_contract_contains_no_trade_fields(self):
        fields = set(ActiveContext.__dataclass_fields__)
        self.assertNotIn("active_trade_ids", fields)
        self.assertNotIn("active_trade_sides", fields)
        self.assertNotIn("active_trade_count", fields)

    def test_runner_pushes_context_then_instruction_once_per_snapshot(self):
        signal_service = _FakeSignalService()
        runner = AuctionServiceRunner(
            engine_config=self.config,
            signal_service=signal_service,
            persistence=_FakePersistence(),
            restore_enabled=False,
            mark_processed_enabled=False,
        )
        runner.start_day(self.ts.date())
        rows = self._rows()[:3]
        results = runner.process_snapshots(rows)
        self.assertEqual(len(results), 3)
        self.assertEqual(runner.stats.snapshots_seen, 3)
        self.assertEqual(runner.stats.snapshots_evaluated, 3)
        self.assertEqual(sum(runner.stats.signal_actions.values()), 3)
        self.assertEqual(runner.stats.snapshots_marked_processed, 0)
        self.assertEqual(
            [name for name, _ in signal_service.calls],
            ["RESOLVE", "CONTEXT", "APPLY"] * 3,
        )

    def test_runner_timing_profile_is_opt_in_and_complete(self):
        runner = AuctionServiceRunner(
            engine_config=self.config,
            signal_service=_FakeSignalService(),
            persistence=_FakePersistence(),
            restore_enabled=False,
            mark_processed_enabled=False,
            profile_timing=True,
        )
        runner.start_day(self.ts.date())
        runner.process_snapshots(self._rows()[:2])

        self.assertEqual(len(runner.stats.timing_rows), 2)
        self.assertEqual(runner.stats.timing_rows[0]["runtime_source"], "INITIALIZED")
        self.assertEqual(runner.stats.timing_rows[1]["runtime_source"], "MEMORY")
        self.assertGreaterEqual(runner.stats.timing_rows[0]["total_ms"], 0.0)
        stages = {row["stage"] for row in runner.timing_summary()}
        self.assertIn("auction_evaluation", stages)
        self.assertIn("signal_lifecycle", stages)
        self.assertIn("total", stages)

    def test_signal_lifecycle_report_only_translates_create_without_writing(self):
        engine = AuctionEngine(self.config)
        create_result = None
        create_snapshot = None
        for row in self._rows():
            result = engine.evaluate_snapshot(row)
            if result.final_decision.action.value == "CREATE":
                create_result = result
                create_snapshot = row
                break
        self.assertIsNotNone(create_result)
        self.assertIsNotNone(create_snapshot)
        context = ActiveContext(
            equity_ref="TEST",
            snapshot_time=create_result.snapshot_time,
            evaluation_status="AVAILABLE",
            reason_codes=("NO_ACTIVE_SIGNAL_AS_OF_SNAPSHOT",),
        )
        service = SignalLifecycleService(
            write_enabled=False,
            enforce_creation_permissions=False,
        )
        outcome = service.apply_instruction(
            snapshot=create_snapshot,
            result=create_result,
            context_before=context,
        )
        self.assertEqual(outcome.applied_action, "WOULD_CREATE")
        self.assertFalse(outcome.persisted)

    def test_write_enabled_create_replaces_report_only_audit_reason(self):
        snapshot, create_result = self._first_create()
        context = ActiveContext(
            equity_ref="TEST",
            snapshot_time=create_result.snapshot_time,
            evaluation_status="AVAILABLE",
            reason_codes=("NO_ACTIVE_SIGNAL_AS_OF_SNAPSHOT",),
        )
        service = SignalLifecycleService(
            write_enabled=True,
            enforce_creation_permissions=False,
        )
        persisted = SimpleNamespace(signal_id="signal-1")
        with patch.object(service, "_create", return_value=persisted) as create_mock:
            outcome = service.apply_instruction(
                snapshot=snapshot,
                result=create_result,
                context_before=context,
            )

        self.assertEqual(outcome.applied_action, "CREATE")
        self.assertIn(
            "SIGNAL_CREATED_FROM_SELECTED_OPPORTUNITY",
            outcome.reason_codes,
        )
        self.assertNotIn("WOULD_CREATE_REPORT_ONLY", outcome.reason_codes)
        persisted_reasons = create_mock.call_args.kwargs["reason_codes"]
        self.assertEqual(persisted_reasons, outcome.reason_codes)

    def test_create_persists_operational_and_engine_reason_codes(self):
        snapshot, create_result = self._first_create()
        service = SignalLifecycleService(
            write_enabled=True,
            enforce_creation_permissions=False,
        )
        reasons = _execution_reason_codes(
            "CREATE",
            create_result.final_decision.reason_codes,
        )
        persisted = SimpleNamespace(signal_id="signal-1")
        captured = {}

        class FakeSignalSchema:
            @staticmethod
            def create_signal(**kwargs):
                captured.update(kwargs)
                return persisted

        fake_signal_module = ModuleType("schemas.signal")
        fake_signal_module.SignalSchema = FakeSignalSchema
        with patch.dict(sys.modules, {"schemas.signal": fake_signal_module}):
            service._create(
                snapshot,
                create_result,
                reason_codes=reasons,
            )

        values = captured
        self.assertEqual(values["status_reason"], ";".join(reasons))
        self.assertEqual(
            values["criteria_json"]["decision_reason_codes"],
            list(reasons),
        )
        self.assertEqual(
            values["meta_json"]["auction_engine"]["decision_reason_codes"],
            list(reasons),
        )
        self.assertIn(
            "WOULD_CREATE_REPORT_ONLY",
            values["criteria_json"]["engine_decision_reason_codes"],
        )

    def test_signal_lifecycle_blocks_create_when_snapshot_generation_is_disabled(self):
        engine = AuctionEngine(self.config)
        create_result = None
        create_snapshot = None
        for row in self._rows():
            result = engine.evaluate_snapshot(row)
            if result.final_decision.action.value == "CREATE":
                create_result = result
                create_snapshot = {**row, "gen_signals": False}
                break
        self.assertIsNotNone(create_result)
        service = SignalLifecycleService(write_enabled=False)
        service._symbol_cache["TEST"] = type(
            "SymbolRow", (),
            {"equity_ref": "TEST", "active": True, "generate_signals": True},
        )()
        context = ActiveContext(
            equity_ref="TEST",
            snapshot_time=create_result.snapshot_time,
            evaluation_status="AVAILABLE",
        )
        outcome = service.apply_instruction(
            snapshot=create_snapshot,
            result=create_result,
            context_before=context,
        )
        self.assertEqual(outcome.applied_action, "WOULD_BLOCK_CREATE")
        self.assertIn(
            "CREATE_BLOCKED_SNAPSHOT_GENERATE_SIGNALS_DISABLED",
            outcome.reason_codes,
        )

    def test_signal_lifecycle_maintains_existing_signal_on_hold(self):
        engine = AuctionEngine(self.config)
        snapshot = self._rows()[0]
        result = engine.evaluate_snapshot(snapshot)
        self.assertNotEqual(result.final_decision.action.value, "CREATE")
        service = SignalLifecycleService(write_enabled=False)
        context = ActiveContext(
            equity_ref="TEST",
            snapshot_time=result.snapshot_time,
            active_signal_id="signal-1",
            active_signal_side="BUY",
            evaluation_status="AVAILABLE",
            reason_codes=("ACTIVE_SIGNAL_PRESENT",),
        )
        outcome = service.apply_instruction(
            snapshot=snapshot,
            result=result,
            context_before=context,
        )
        self.assertEqual(outcome.applied_action, "WOULD_UPDATE")
        self.assertEqual(outcome.signal_id, "signal-1")


    def test_checkpoint_hash_uses_exported_payload_directly(self):
        engine = AuctionEngine(self.config)
        for row in self._rows()[:5]:
            engine.evaluate_snapshot(row)
        payload = engine.export_checkpoint("TEST")
        self.assertEqual(
            checkpoint_state_hash(payload),
            checkpoint_state_hash(engine.export_checkpoint("TEST")),
        )

    def test_deterministic_signal_id_is_stable_per_opportunity(self):
        first = _deterministic_signal_id("DEFAULT", "OPP-1")
        second = _deterministic_signal_id("default", "OPP-1")
        other = _deterministic_signal_id("DEFAULT", "OPP-2")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_same_snapshot_created_signal_is_removed_from_retry_context(self):
        service = SignalLifecycleService(write_enabled=True)
        signal = SimpleNamespace(
            signal_id="signal-1",
            side="SELL",
            setup="ACCEPTED_BREAKOUT",
            stage="ACTIVE",
            status="OPEN",
            meta_json={},
            created_price=100.0,
            first_seen_time=self.ts,
            last_snapshot_time=self.ts,
        )

        class FakeSignalSchema:
            @staticmethod
            def fetch_active_signal(equity_ref, lifecycle):
                return signal

        fake_signal_module = ModuleType("schemas.signal")
        fake_signal_module.SignalSchema = FakeSignalSchema
        with patch.dict(sys.modules, {"schemas.signal": fake_signal_module}):
            context = service.get_active_context(
                snapshot={"symbol": "TEST", "snapshot_time": self.ts},
                equity_ref="TEST",
            )

        self.assertIsNone(context.active_signal_id)
        self.assertIn(
            "SAME_SNAPSHOT_CREATE_RETRY_CONTEXT_RECONSTRUCTED",
            context.reason_codes,
        )
        self.assertEqual(
            context.diagnostics["same_snapshot_create_retry_signal_id"],
            "signal-1",
        )

    def _first_create(self):
        engine = AuctionEngine(self.config)
        for row in self._rows():
            result = engine.evaluate_snapshot(row)
            if result.final_decision.action is FinalAction.CREATE:
                return row, result
        self.fail("Expected one CREATE result")

    def test_final_decision_creates_when_no_active_signal(self):
        _, create_result = self._first_create()
        context = ActiveContext(
            equity_ref="TEST",
            snapshot_time=create_result.snapshot_time,
            evaluation_status="AVAILABLE",
            reason_codes=("NO_ACTIVE_SIGNAL_AS_OF_SNAPSHOT",),
        )
        decision = DecisionEngine(self.config).evaluate(
            manager=create_result.manager_decision,
            selected=create_result.final_decision.selected_candidate,
            advisor=create_result.final_decision.advisor_decision,
            equity_ref="TEST",
            active_context=context,
        )
        self.assertEqual(decision.action, FinalAction.CREATE)
        self.assertIsNotNone(decision.signal_payload)
        self.assertFalse(decision.diagnostics["active_context_resolution_applied"])

    def test_final_decision_holds_same_side_active_signal(self):
        _, create_result = self._first_create()
        selected = create_result.final_decision.selected_candidate
        context = ActiveContext(
            equity_ref="TEST",
            snapshot_time=create_result.snapshot_time,
            active_signal_id="signal-1",
            active_signal_side=selected.side.value,
            evaluation_status="AVAILABLE",
            reason_codes=("ACTIVE_SIGNAL_PRESENT",),
        )
        decision = DecisionEngine(self.config).evaluate(
            manager=create_result.manager_decision,
            selected=selected,
            advisor=create_result.final_decision.advisor_decision,
            equity_ref="TEST",
            active_context=context,
        )
        self.assertEqual(decision.action, FinalAction.HOLD)
        self.assertIn("HOLD_ACTIVE_SIGNAL_SAME_SIDE", decision.reason_codes)
        self.assertEqual(decision.active_signal_id, "signal-1")

    def test_final_decision_defers_opposite_side_active_signal(self):
        _, create_result = self._first_create()
        selected = create_result.final_decision.selected_candidate
        opposite = "SELL" if selected.side is TradeSide.BUY else "BUY"
        context = ActiveContext(
            equity_ref="TEST",
            snapshot_time=create_result.snapshot_time,
            active_signal_id="signal-1",
            active_signal_side=opposite,
            evaluation_status="AVAILABLE",
            reason_codes=("ACTIVE_SIGNAL_PRESENT",),
        )
        decision = DecisionEngine(self.config).evaluate(
            manager=create_result.manager_decision,
            selected=selected,
            advisor=create_result.final_decision.advisor_decision,
            equity_ref="TEST",
            active_context=context,
        )
        self.assertEqual(decision.action, FinalAction.DEFER)
        self.assertIn("DEFER_ACTIVE_SIGNAL_OPPOSING_SIDE", decision.reason_codes)

    def test_lifecycle_only_updates_after_engine_holds_for_active_signal(self):
        snapshot, create_result = self._first_create()
        selected = create_result.final_decision.selected_candidate
        context = ActiveContext(
            equity_ref="TEST",
            snapshot_time=create_result.snapshot_time,
            active_signal_id="signal-1",
            active_signal_side=selected.side.value,
            evaluation_status="AVAILABLE",
            reason_codes=("ACTIVE_SIGNAL_PRESENT",),
        )
        final = DecisionEngine(self.config).evaluate(
            manager=create_result.manager_decision,
            selected=selected,
            advisor=create_result.final_decision.advisor_decision,
            equity_ref="TEST",
            active_context=context,
        )
        result = create_result.model_copy(update={"final_decision": final})
        service = SignalLifecycleService(
            write_enabled=False,
            enforce_creation_permissions=False,
        )
        outcome = service.apply_instruction(
            snapshot=snapshot,
            result=result,
            context_before=context,
        )
        self.assertEqual(outcome.requested_action, "HOLD")
        self.assertEqual(outcome.applied_action, "WOULD_UPDATE")
        self.assertFalse(outcome.diagnostics["defensive_guard_triggered"])

    def test_active_signal_hold_is_consumed_once_without_signal_create(self):
        rows = self._rows()
        probe = AuctionEngine(self.config)
        create_index = None
        selected_side = None
        for index, row in enumerate(rows):
            probe_result = probe.evaluate_snapshot(row)
            if probe_result.final_decision.action is FinalAction.CREATE:
                create_index = index
                selected_side = probe_result.final_decision.selected_candidate.side.value
                break
        self.assertIsNotNone(create_index)

        engine = AuctionEngine(self.config)
        held_result = None
        for index, row in enumerate(rows):
            context = None
            if index == create_index:
                context = ActiveContext(
                    equity_ref="TEST",
                    snapshot_time=row["snapshot_time"],
                    active_signal_id="signal-1",
                    active_signal_side=selected_side,
                    evaluation_status="AVAILABLE",
                    reason_codes=("ACTIVE_SIGNAL_PRESENT",),
                )
            result = engine.evaluate_snapshot(row, active_context=context)
            if index == create_index:
                held_result = result
        self.assertEqual(held_result.manager_decision.action.value, "SELECT")
        self.assertEqual(held_result.final_decision.action, FinalAction.HOLD)
        selected = held_result.final_decision.selected_candidate
        record = next(
            row for row in engine.opportunity_ledger.records("TEST")
            if row.opportunity_key == selected.opportunity_key
        )
        self.assertEqual(record.lifecycle_state, "CONSUMED")
        self.assertEqual(record.decision_count, 1)


if __name__ == "__main__":
    unittest.main()
