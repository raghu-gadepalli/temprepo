from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from configs.signal_config import SIGNAL_CONFIG
from enums.enums import TradePosture
from services.signals.signal_generator import SignalAssembler
from services.trade.monitor.signal_contract import AuctionTradeSignalContext
from services.trade.monitor.trademon_helper import TradeMonHelper
from services.trade.monitor.trade_monitor import TradeMonitor, _audit_trade_monitor
from tests.test_signal_generator_auction_snapshot import (
    FakeFetcher,
    FakePersister,
    _candidate,
    _opportunity,
    _snapshot,
)


TS = datetime(2026, 7, 20, 11, 48, tzinfo=timezone.utc)


def _signal(*, state: str = "ORDERLY_DOWNTREND"):
    snapshot = _snapshot(
        action="LOCAL_CONFIRMED",
        auction_state=state,
        opportunities=[_opportunity()],
        candidates=[_candidate()],
    )
    events = SignalAssembler(
        fetcher=FakeFetcher(), persister=FakePersister()
    ).assemble(snapshot)
    return events[0][1]


def _trade_management():
    return TradeMonHelper.initialize_trade_management(
        side="SELL",
        instrument_type="EQ",
        entry_price=100,
        underlying_atr=2,
        asof_time=TS,
    )


class StrictAuctionTradeMonitorContractTests(unittest.TestCase):
    def setUp(self):
        self._audit_enabled = SIGNAL_CONFIG.audit.enabled
        SIGNAL_CONFIG.audit.enabled = False

    def tearDown(self):
        SIGNAL_CONFIG.audit.enabled = self._audit_enabled

    def test_valid_signal_contract_parses_exact_identity(self):
        context = AuctionTradeSignalContext.from_signal(_signal())
        self.assertEqual("AUCTION_SIGNAL_DOWNSTREAM_V2", context.contract_version)
        self.assertEqual("ACTIVE", context.stage)
        self.assertEqual("STRENGTHEN", context.management_posture)
        self.assertTrue(context.is_strengthening)
        self.assertFalse(context.requires_exit)

    def test_missing_downstream_contract_fails(self):
        signal = _signal()
        signal.meta_json = deepcopy(signal.meta_json)
        del signal.meta_json["downstream_contract"]
        with self.assertRaisesRegex(ValueError, "downstream_contract"):
            AuctionTradeSignalContext.from_signal(signal)

    def test_identity_mismatch_fails(self):
        signal = _signal()
        signal.meta_json = deepcopy(signal.meta_json)
        signal.meta_json["setup_levels"]["candidate_id"] = "OTHER"
        with self.assertRaisesRegex(ValueError, "candidate"):
            AuctionTradeSignalContext.from_signal(signal)

    def test_old_trade_management_version_fails(self):
        tm = _trade_management()
        tm["version"] = 1
        tm["mode"] = "EVIDENCE_ADAPTIVE_V1"
        with self.assertRaisesRegex(ValueError, "version must be 2"):
            TradeMonHelper.normalize_trade_management(
                raw=tm,
                side="SELL",
                instrument_type="EQ",
                entry_price=100,
                underlying_atr=2.5,
                asof_time=TS,
            )

    def test_current_snapshot_atr_does_not_rebase_frozen_atr(self):
        tm = _trade_management()
        normalized = TradeMonHelper.normalize_trade_management(
            raw=tm,
            side="SELL",
            instrument_type="EQ",
            entry_price=100,
            underlying_atr=3,
            asof_time=TS,
        )
        self.assertEqual(2.0, normalized["atr_at_entry"])
        self.assertEqual(2.0, normalized["instrument_atr"])

    def test_manual_trade_is_price_only(self):
        decision = TradeMonHelper.evaluate(
            trade=SimpleNamespace(),
            signal_context=None,
            side="SELL",
            instrument_type="EQ",
            entry_price=100,
            last_price=99,
            trade_management=_trade_management(),
            asof_time=TS,
            max_favorable_price=99,
            max_adverse_price=100,
            manual_trade_context=True,
        )
        self.assertEqual(TradePosture.HOLD.value, decision.posture)
        self.assertEqual("MANUAL_PRICE_ONLY", decision.trade_management["management_context"])
        self.assertFalse(decision.trade_management["signal_context_available"])

    def test_signal_trade_requires_signal_context(self):
        with self.assertRaisesRegex(ValueError, "requires Auction signal context"):
            TradeMonHelper.evaluate(
                trade=SimpleNamespace(),
                signal_context=None,
                side="SELL",
                instrument_type="EQ",
                entry_price=100,
                last_price=99,
                trade_management=_trade_management(),
                asof_time=TS,
                max_favorable_price=99,
                max_adverse_price=100,
                manual_trade_context=False,
            )

    def test_strengthening_signal_allows_expansion_posture(self):
        context = AuctionTradeSignalContext.from_signal(_signal())
        tm = _trade_management()
        decision = TradeMonHelper.evaluate(
            trade=SimpleNamespace(),
            signal_context=context,
            side="SELL",
            instrument_type="EQ",
            entry_price=100,
            last_price=97,
            trade_management=tm,
            asof_time=TS,
            max_favorable_price=97,
            max_adverse_price=100,
            manual_trade_context=False,
        )
        self.assertEqual(TradePosture.EXPAND.value, decision.posture)
        self.assertEqual("STRENGTHEN", decision.trade_management["management_posture"])
        self.assertTrue(decision.trade_management["target_expansion_allowed"])

    def test_exit_contract_drives_exit_posture(self):
        signal = _signal()
        signal.meta_json = deepcopy(signal.meta_json)
        signal.stage = "EXIT_BIAS"
        signal.meta_json["signal"]["stage"] = "EXIT_BIAS"
        signal.meta_json["lifecycle"]["stage"] = "EXIT_BIAS"
        signal.meta_json["lifecycle"]["trade_action"] = "EXIT_POSITION"
        management = signal.meta_json["management"]
        management["stage"] = "EXIT_BIAS"
        management["action"] = "EXIT"
        management["should_exit_signal"] = True
        management["target_expansion_allowed"] = False
        context = AuctionTradeSignalContext.from_signal(signal)
        self.assertTrue(context.requires_exit)
        decision = TradeMonHelper.evaluate(
            trade=SimpleNamespace(),
            signal_context=context,
            side="SELL",
            instrument_type="EQ",
            entry_price=100,
            last_price=101,
            trade_management=_trade_management(),
            asof_time=TS,
            max_favorable_price=100,
            max_adverse_price=101,
            manual_trade_context=False,
        )
        self.assertEqual(TradePosture.EXIT.value, decision.posture)
        self.assertTrue(decision.trade_management["should_exit_signal"])

    def test_target_expansion_called_while_disabled_fails(self):
        tm = _trade_management()
        with self.assertRaisesRegex(ValueError, "disabled"):
            TradeMonHelper.expand_after_target_hit(
                side="SELL",
                entry_price=100,
                last_price=95,
                trade_management=tm,
                asof_time=TS,
            )

    def test_monitor_runtime_contains_no_dict_get_or_peer_resolver(self):
        root = Path(__file__).resolve().parents[1]
        monitor_dir = root / "services" / "trade" / "monitor"
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(monitor_dir.glob("*.py"))
        )
        self.assertNotIn(".get(", source)
        self.assertNotIn("SignalHelper", source)
        self.assertNotIn("monitor_resolution", source)
        self.assertNotIn("last_signal_confidence", source)
        self.assertNotIn("last_signal_quality", source)

    def test_removed_signal_helper_is_not_retained(self):
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / "services" / "signals" / "signal_helper.py").exists())


    def test_trade_monitor_audit_executes_with_canonical_management_converter(self):
        ctx = SimpleNamespace(
            trade=SimpleNamespace(
                id=1,
                symbol="COFORGE",
                userid="DR1812",
                signal_id="signal-1",
                exit_status="PENDING",
            ),
            instrument_type="EQ",
            side="SELL",
            last_price=99.0,
            entry_price=100.0,
            basis_price=100.0,
            pnl_per_unit=1.0,
            pnl_value=1.0,
            last_time=TS,
            trade_management=_trade_management(),
            signal_contract=None,
        )
        updates = {
            "trade_management": _trade_management(),
            "last_price": 99.0,
        }
        with patch(
            "services.trade.monitor.trade_monitor.write_auditlog",
            return_value=True,
        ) as writer:
            _audit_trade_monitor(ctx, updates)
        writer.assert_called_once()
        kwargs = writer.call_args.kwargs
        self.assertTrue(kwargs["strict"])
        self.assertTrue(kwargs["force_persist"])
        self.assertEqual("TRADE_MONITOR", kwargs["evaluation_stage"])
        self.assertIn("management", kwargs["payload_json"])

    def test_trade_monitor_audit_is_strict_and_forced(self):
        root = Path(__file__).resolve().parents[1]
        source = (
            root / "services" / "trade" / "monitor" / "trade_monitor.py"
        ).read_text(encoding="utf-8")
        self.assertIn("strict=True", source)
        self.assertIn("force_persist=True", source)
        self.assertIn("strict TradeMonitor audit was not persisted", source)
        self.assertNotIn("trade monitor audit failed", source)

    def test_replay_has_overridable_adaptive_multi_defaults(self):
        root = Path(__file__).resolve().parents[1]
        source = (
            root / "tests" / "replay_auction_signal_trade_pipeline.py"
        ).read_text(encoding="utf-8")
        self.assertIn('DEFAULT_TRADING_DAY = "2026-07-20"', source)
        self.assertIn('DEFAULT_SYMBOLS = "COFORGE"', source)
        self.assertIn('DEFAULT_USERID = "DR1812"', source)
        self.assertIn('DEFAULT_INSTRUMENT_CHOICE = "MULTI"', source)
        self.assertIn('DEFAULT_TEST_MODE = "ADAPTIVE_EXIT"', source)
        self.assertIn('DEFAULT_REQUIRE_DERIVATIVES = True', source)
        self.assertIn("argparse.BooleanOptionalAction", source)
        self.assertIn("_deterministic_replay_clock", source)
        self.assertIn("SIGNAL_LIFECYCLE_EXIT", source)

    def test_replay_separates_snapshot_signal_and_frozen_trade_state(self):
        root = Path(__file__).resolve().parents[1]
        source = (
            root / "tests" / "replay_auction_signal_trade_pipeline.py"
        ).read_text(encoding="utf-8")
        self.assertIn("snapshot_auction_action", source)
        self.assertIn("signal_auction_action", source)
        self.assertIn("current_signal_stage", source)
        self.assertIn("trade_state_frozen_at_exit", source)

    def test_signal_lifecycle_exit_uses_exact_canonical_rule(self):
        signal = _signal()
        signal.meta_json = deepcopy(signal.meta_json)
        signal.stage = "EXIT_BIAS"
        signal.meta_json["signal"]["stage"] = "EXIT_BIAS"
        signal.meta_json["lifecycle"]["stage"] = "EXIT_BIAS"
        signal.meta_json["lifecycle"]["trade_action"] = "EXIT_POSITION"
        management = signal.meta_json["management"]
        management["stage"] = "EXIT_BIAS"
        management["action"] = "EXIT"
        management["should_exit_signal"] = True
        management["target_expansion_allowed"] = False
        contract = AuctionTradeSignalContext.from_signal(signal)
        ctx = SimpleNamespace(
            signal_contract=contract,
            last_time=TS,
            last_price=101.0,
            pnl_value=-1.0,
            pnl_per_unit=-1.0,
        )
        payload = TradeMonitor()._signal_exit_payload(ctx)
        self.assertEqual("SIGNAL_LIFECYCLE_EXIT", payload["exit_reason"])
        self.assertEqual(
            "exit_on_auction_signal_downstream_contract",
            payload["exit_rule"],
        )
        self.assertNotIn(":", payload["exit_rule"])

    def test_monitor_records_item_errors_and_continues_contract(self):
        root = Path(__file__).resolve().parents[1]
        source = (
            root / "services" / "trade" / "monitor" / "trade_monitor.py"
        ).read_text(encoding="utf-8")
        self.assertIn("self.last_pass_errors", source)
        self.assertIn("continuing with next trade", source)
        self.assertIn("pass completed with item errors", source)
        self.assertNotIn("fatal trade evaluation error", source)

    def test_replay_writes_validation_reports_and_returns_clean_failure_code(self):
        root = Path(__file__).resolve().parents[1]
        source = (
            root / "tests" / "replay_auction_signal_trade_pipeline.py"
        ).read_text(encoding="utf-8")
        self.assertIn("_validation.csv", source)
        self.assertIn("_monitor_errors.csv", source)
        self.assertIn('"validation_status"', source)
        self.assertIn('return 2', source)
        self.assertIn("REPLAY_VALIDATION_FAILED", source)
        self.assertNotIn(
            'raise AssertionError("SIGNAL_EXIT replay did not produce',
            source,
        )

    def test_exact_signal_exit_precedes_generic_adaptive_exit(self):
        root = Path(__file__).resolve().parents[1]
        source = (
            root / "services" / "trade" / "monitor" / "trade_monitor.py"
        ).read_text(encoding="utf-8")
        signal_index = source.index("signal_exit = self._signal_exit_payload(ctx)")
        adaptive_index = source.index(
            '_required(ctx.trade_management, "posture", "trade_management") == TradePosture.EXIT.value'
        )
        self.assertLess(signal_index, adaptive_index)


if __name__ == "__main__":
    unittest.main()
