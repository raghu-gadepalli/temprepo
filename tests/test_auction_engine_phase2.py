#!/usr/bin/env python3
"""Offline tests for Phase-2 evidence and auction-state reporting.

Run from the project root:

    python -m unittest tests.test_auction_engine_phase2 -v

No database connection is opened and the current signal pipeline is not called.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from configs.auction_engine_config import AUCTION_ENGINE_CONFIG, AuctionEngineConfig
from services.auction_engine.contracts import AuctionStateName, LocalDecisionAction
from services.auction_engine.engine import AuctionEngine
from services.auction_engine.evidence import EvidenceBuilder
from services.auction_engine.state_engine import AuctionStateChronologyError


def _test_config() -> AuctionEngineConfig:
    payload = AUCTION_ENGINE_CONFIG.resolved_dict()
    payload["evidence"]["minimum_history_bars"] = 1
    payload["evidence"]["extension_min_history_bars_for_maturity"] = 2
    return AuctionEngineConfig.model_validate(payload)


def _snapshot(
    ts: datetime,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
    atr: float = 1.0,
    hma_state: str = "NO_TREND",
    hma_strength: str = "NA",
    vwap_side: str = "UNKNOWN",
    vwap_distance_atr: float = 0.0,
    rsi: float = 50.0,
    bb_position: float = 0.5,
    move_15m: float = 0.0,
    move_30m: float = 0.0,
    move_sod: float = 0.0,
    raw_state: str = "RANGE",
    raw_side: str = "NEUTRAL",
    range_type: str = "BALANCE",
    range_low: float = 99.0,
    range_high: float = 101.0,
    range_width_atr: float = 2.0,
    efficiency: float = 0.20,
    overlap: float = 0.75,
    flip_count: int = 0,
    bars: int = 10,
) -> dict:
    return {
        "symbol": "TEST",
        "snapshot_time": ts,
        "tf": "3m",
        "close": close,
        "bar": {
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": 10000,
        },
        "levels": {
            "prev_day": {"open": 98.0, "high": 105.0, "low": 95.0, "close": 99.0},
            "today": {"open": 100.0},
            "opening_range": {"ready": True, "high": 101.0, "low": 99.0},
        },
        "indicators": {
            "ema": {},
            "hma": {
                "fast": close + (0.20 if hma_state == "UPTREND" else -0.20 if hma_state == "DOWNTREND" else 0.02),
                "mid1": close + (0.10 if hma_state == "UPTREND" else -0.10 if hma_state == "DOWNTREND" else 0.01),
                "mid2": close,
                "slow": close - (0.10 if hma_state == "UPTREND" else -0.10 if hma_state == "DOWNTREND" else 0.01),
                "state": hma_state,
                "strength": hma_strength,
            },
            "vwap": {
                "value": close - vwap_distance_atr if vwap_side == "ABOVE" else close + vwap_distance_atr if vwap_side == "BELOW" else close,
                "side": vwap_side,
                "distance_atr": vwap_distance_atr,
            },
            "rsi": {"value": rsi, "zone": "MID"},
            "adx": {"value": 25.0, "band": "MEDIUM"},
            "atr": {"value": atr, "band": "NORMAL", "pct": 1.0},
            "bollinger": {"position": bb_position, "zone": "MID"},
            "envelopes": {"hma_envelope": 0.1, "ema_envelope": 0.2},
        },
        "volume": {"bar_volume": 10000, "bar_rvol": 1.1, "bar_rvol_band": "NORMAL"},
        "market_windows": {
            "sod": {
                "status": "ok", "bars": bars, "minutes": bars * 3,
                "move_atr": move_sod, "move_pct": move_sod,
                "move_points": move_sod * atr, "range_points": 4.0,
                "close_position_in_range": 0.5,
            },
            "15m": {
                "status": "ok", "bars": 5, "minutes": 15,
                "move_atr": move_15m, "move_points": move_15m * atr,
                "range_points": 2.0, "close_position_in_range": 0.5,
            },
            "30m": {
                "status": "ok", "bars": 10, "minutes": 30,
                "move_atr": move_30m, "move_points": move_30m * atr,
                "range_points": 3.0, "close_position_in_range": 0.5,
            },
        },
        "indicator_windows": {"hma": {}, "vwap": {}, "rsi": {}, "adx": {}, "atr": {}, "bollinger": {}, "volume": {}},
        "price_action": {
            "orb": {"position": "INSIDE"},
            "vwap": {"position": vwap_side},
            "moves": {},
            "slope": {
                "bars_3_atr_per_bar": move_15m / 5.0,
                "bars_5_atr_per_bar": move_15m / 5.0,
                "state": "UP" if move_15m > 0 else "DOWN" if move_15m < 0 else "FLAT",
            },
        },
        "structure": {
            "raw": {
                "state": raw_state,
                "side": raw_side,
                "range": {
                    "range_id": "RANGE-1", "version": 1,
                    "high": range_high, "low": range_low,
                    "width_atr": range_width_atr, "source": "DYNAMIC",
                    "range_type": range_type, "bars": bars,
                    "breakout_eligible": True,
                },
                "metrics": {
                    "directional_efficiency": efficiency,
                    "adjacent_overlap_ratio": overlap,
                    "classification": range_type,
                },
            },
            "accepted": {
                "state": "RANGE_ACCEPTED",
                "range": {
                    "range_id": "RANGE-1", "version": 1,
                    "high": range_high, "low": range_low,
                    "width_atr": range_width_atr, "source": "DYNAMIC",
                    "range_type": range_type, "bars": bars,
                    "breakout_eligible": True,
                },
                "metrics": {
                    "directional_efficiency": efficiency,
                    "adjacent_overlap_ratio": overlap,
                    "classification": range_type,
                },
                "age_bars": bars,
            },
            "candidate": {"active": False, "range": {}, "metrics": {}},
            "recent_closes": [], "anchors": {}, "breakout_context": {},
            "diagnostics": {}, "session_phase": "MID", "count": bars,
            "flip_count_today": flip_count,
        },
        "state_context": {
            "hma": {"flip_count_today": flip_count},
            "hma_strength": {},
            "vwap": {"flip_count_today": flip_count},
            "rsi": {}, "adx": {}, "atr": {}, "bollinger": {}, "volume": {},
            "structure": {"flip_count_today": flip_count},
        },
        "derivatives": {},
    }


class EvidenceBuilderTests(unittest.TestCase):
    def test_compression_requires_price_containment(self) -> None:
        cfg = _test_config()
        builder = EvidenceBuilder(cfg)
        ts = datetime(2026, 7, 20, 10, 0)
        compressed = builder.build(_snapshot(ts, open_price=100.0, high=100.3, low=99.8, close=100.1, range_type="MICRO_COMPRESSION"))
        self.assertTrue(compressed.compression.compressed)

        not_contained = builder.build(_snapshot(
            ts + timedelta(minutes=3),
            open_price=100.0, high=100.3, low=99.8, close=100.1,
            range_type="MICRO_COMPRESSION", range_low=95.0, range_high=105.0,
            range_width_atr=10.0,
        ))
        self.assertFalse(not_contained.compression.compressed)

    def test_existing_setup_conclusion_is_not_consumed(self) -> None:
        cfg = _test_config()
        snapshot = _snapshot(datetime(2026, 7, 20, 10, 0), open_price=100.0, high=100.2, low=99.8, close=100.0)
        snapshot["setup"] = "ACCEPTED_BREAKOUT"
        evidence = EvidenceBuilder(cfg).build(snapshot)
        self.assertNotIn("setup", evidence.raw_facts)
        self.assertFalse(cfg.evidence.consume_existing_setup_conclusions)

    def test_rolling_efficiency_replaces_stale_structure_metric(self) -> None:
        cfg = _test_config()
        engine = AuctionEngine(cfg)
        ts = datetime(2026, 7, 20, 10, 0)
        result = None
        for index in range(6):
            close = 100.0 + index * 0.4
            result = engine.evaluate_snapshot(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=close - 0.2,
                high=close + 0.1,
                low=close - 0.3,
                close=close,
                hma_state="UPTREND",
                vwap_side="ABOVE",
                move_15m=0.8,
                move_30m=1.4,
                move_sod=1.8,
                raw_state="TRENDING_UP",
                raw_side="BUY",
                efficiency=0.05,
                overlap=0.90,
            ))
        self.assertIsNotNone(result)
        self.assertGreater(result.evidence.trend.directional_efficiency, 0.90)

    def test_maturity_requires_distance_and_progress_or_rejection(self) -> None:
        cfg = _test_config()
        engine = AuctionEngine(cfg)
        ts = datetime(2026, 7, 20, 10, 0)

        indicator_only = engine.evaluate_snapshot(_snapshot(
            ts,
            open_price=100.0,
            high=100.1,
            low=99.9,
            close=100.0,
            rsi=82.0,
            bb_position=1.2,
            move_15m=0.0,
            move_30m=0.0,
            move_sod=0.0,
        ))
        self.assertTrue(indicator_only.evidence.extension.extended)
        self.assertFalse(indicator_only.evidence.extension.mature)

        distance_without_decay = engine.evaluate_snapshot(_snapshot(
            ts + timedelta(minutes=3),
            open_price=102.9,
            high=103.1,
            low=102.8,
            close=103.0,
            hma_state="UPTREND",
            vwap_side="ABOVE",
            vwap_distance_atr=2.0,
            rsi=82.0,
            bb_position=1.2,
            move_15m=2.0,
            move_30m=2.0,
            move_sod=3.0,
            raw_state="TRENDING_UP",
            raw_side="BUY",
        ))
        self.assertFalse(distance_without_decay.evidence.extension.mature)

        mature = engine.evaluate_snapshot(_snapshot(
            ts + timedelta(minutes=6),
            open_price=103.0,
            high=103.2,
            low=102.9,
            close=103.1,
            hma_state="UPTREND",
            vwap_side="ABOVE",
            vwap_distance_atr=2.0,
            rsi=82.0,
            bb_position=1.2,
            move_15m=0.2,
            move_30m=2.0,
            move_sod=3.1,
            raw_state="TRENDING_UP",
            raw_side="BUY",
        ))
        self.assertTrue(mature.evidence.extension.mature)
        self.assertIn(
            "EXHAUSTION_PROGRESS_DECAY",
            {fact.code for fact in mature.evidence.extension.supporting_facts},
        )


class AuctionStateEngineTests(unittest.TestCase):
    def test_trend_candidate_does_not_establish_before_confirmed_state_transition(self) -> None:
        engine = AuctionEngine(_test_config())
        ts = datetime(2026, 7, 20, 10, 0)
        results = []
        for index, close in enumerate((100.20, 100.45, 100.70)):
            results.append(engine.evaluate_snapshot(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=close - 0.18,
                high=close + 0.08,
                low=close - 0.22,
                close=close,
                hma_state="UPTREND",
                hma_strength="STRONG",
                vwap_side="ABOVE",
                move_15m=0.7,
                move_30m=1.0,
                move_sod=1.2,
                raw_state="TRENDING_UP",
                raw_side="BUY",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.70,
                overlap=0.20,
            )))

        second = results[1].diagnostics["state_diagnostics"]
        self.assertTrue(second["trend_candidate_ready"])
        self.assertEqual(second["established_trend_side"], "UNKNOWN")
        self.assertEqual(results[1].auction_state.current_state, AuctionStateName.UNKNOWN)

        third = results[2].diagnostics["state_diagnostics"]
        self.assertEqual(results[2].auction_state.current_state, AuctionStateName.ORDERLY_UPTREND)
        self.assertEqual(third["established_trend_side"], "UP")

    def test_opposite_context_without_structural_loss_remains_failure_watch(self) -> None:
        engine = AuctionEngine(_test_config())
        ts = datetime(2026, 7, 20, 10, 0)
        rows = []
        for index, close in enumerate((100.20, 100.45, 100.70)):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=close - 0.18,
                high=close + 0.08,
                low=close - 0.22,
                close=close,
                hma_state="UPTREND",
                vwap_side="ABOVE",
                move_15m=0.7,
                move_30m=1.0,
                move_sod=1.2,
                raw_state="TRENDING_UP",
                raw_side="BUY",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.70,
                overlap=0.20,
            ))
        rows.extend([
            _snapshot(
                ts + timedelta(minutes=9),
                open_price=101.00,
                high=101.05,
                low=100.40,
                close=100.45,
                hma_state="DOWNTREND",
                vwap_side="BELOW",
                move_15m=-0.7,
                move_30m=-0.8,
                move_sod=0.7,
                raw_state="TRENDING_UP",
                raw_side="BUY",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.65,
                overlap=0.25,
            ),
            _snapshot(
                ts + timedelta(minutes=12),
                open_price=100.95,
                high=101.00,
                low=100.35,
                close=100.40,
                hma_state="DOWNTREND",
                vwap_side="BELOW",
                move_15m=-0.7,
                move_30m=-0.8,
                move_sod=0.6,
                raw_state="TRENDING_UP",
                raw_side="BUY",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.60,
                overlap=0.30,
            ),
        ])

        results = [engine.evaluate_snapshot(row) for row in rows]
        last = results[-1]
        self.assertNotEqual(last.auction_state.current_state, AuctionStateName.TREND_FAILURE)
        flags = last.diagnostics["state_flags"]
        self.assertGreaterEqual(flags["failure_watch_bars"], 2)
        self.assertFalse(flags["structural_failure_confirmed"])
        self.assertFalse(flags["failure_level_breached"])

    def test_protected_level_breach_confirms_trend_failure(self) -> None:
        engine = AuctionEngine(_test_config())
        ts = datetime(2026, 7, 20, 10, 0)
        rows = []
        for index, close in enumerate((100.20, 100.45, 100.70)):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=close - 0.18,
                high=close + 0.08,
                low=close - 0.22,
                close=close,
                hma_state="UPTREND",
                vwap_side="ABOVE",
                move_15m=0.7,
                move_30m=1.0,
                move_sod=1.2,
                raw_state="TRENDING_UP",
                raw_side="BUY",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.70,
                overlap=0.20,
            ))
        rows.extend([
            _snapshot(
                ts + timedelta(minutes=9),
                open_price=100.70,
                high=100.75,
                low=99.90,
                close=100.00,
                hma_state="DOWNTREND",
                vwap_side="BELOW",
                move_15m=-0.9,
                move_30m=-1.1,
                move_sod=0.2,
                raw_state="TRENDING_DOWN",
                raw_side="SELL",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.70,
                overlap=0.20,
            ),
            _snapshot(
                ts + timedelta(minutes=12),
                open_price=100.00,
                high=100.05,
                low=99.20,
                close=99.30,
                hma_state="DOWNTREND",
                vwap_side="BELOW",
                move_15m=-1.0,
                move_30m=-1.3,
                move_sod=-0.5,
                raw_state="TRENDING_DOWN",
                raw_side="SELL",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.72,
                overlap=0.20,
            ),
        ])

        results = [engine.evaluate_snapshot(row) for row in rows]
        last = results[-1]
        self.assertEqual(last.auction_state.current_state, AuctionStateName.TREND_FAILURE)
        diag = last.diagnostics["state_diagnostics"]
        self.assertEqual(diag["failure_confirmation_reason"], "FROZEN_PROTECTED_LEVEL_BREACH_CONFIRMED")
        self.assertIsNotNone(diag["failure_level"])

    def test_failure_confirmation_streak_resets_on_current_recovery(self) -> None:
        engine = AuctionEngine(_test_config())
        ts = datetime(2026, 7, 20, 10, 0)
        rows = []
        for index, close in enumerate((100.20, 100.45, 100.70)):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=close - 0.18, high=close + 0.08,
                low=close - 0.22, close=close,
                hma_state="UPTREND", vwap_side="ABOVE",
                move_15m=0.7, move_30m=1.0, move_sod=1.2,
                raw_state="TRENDING_UP", raw_side="BUY",
                range_low=95.0, range_high=105.0,
                range_width_atr=10.0, efficiency=0.70, overlap=0.20,
            ))
        rows.extend([
            _snapshot(
                ts + timedelta(minutes=9),
                open_price=100.70, high=100.75, low=99.90, close=100.00,
                hma_state="DOWNTREND", vwap_side="BELOW",
                move_15m=-0.9, move_30m=-1.1, move_sod=0.2,
                raw_state="TRENDING_DOWN", raw_side="SELL",
                range_low=95.0, range_high=105.0,
                range_width_atr=10.0, efficiency=0.70, overlap=0.20,
            ),
            _snapshot(
                ts + timedelta(minutes=12),
                open_price=100.00, high=100.80, low=99.95, close=100.70,
                hma_state="UPTREND", vwap_side="ABOVE",
                move_15m=0.8, move_30m=1.0, move_sod=1.0,
                raw_state="TRENDING_UP", raw_side="BUY",
                range_low=95.0, range_high=105.0,
                range_width_atr=10.0, efficiency=0.70, overlap=0.20,
            ),
            _snapshot(
                ts + timedelta(minutes=15),
                open_price=100.70, high=100.75, low=99.80, close=99.90,
                hma_state="DOWNTREND", vwap_side="BELOW",
                move_15m=-0.9, move_30m=-1.1, move_sod=0.1,
                raw_state="TRENDING_DOWN", raw_side="SELL",
                range_low=95.0, range_high=105.0,
                range_width_atr=10.0, efficiency=0.70, overlap=0.20,
            ),
            _snapshot(
                ts + timedelta(minutes=18),
                open_price=99.90, high=99.95, low=99.20, close=99.30,
                hma_state="DOWNTREND", vwap_side="BELOW",
                move_15m=-1.0, move_30m=-1.3, move_sod=-0.5,
                raw_state="TRENDING_DOWN", raw_side="SELL",
                range_low=95.0, range_high=105.0,
                range_width_atr=10.0, efficiency=0.72, overlap=0.20,
            ),
        ])

        results = [engine.evaluate_snapshot(row) for row in rows]
        first_breach = results[3].diagnostics["state_diagnostics"]
        recovered = results[4].diagnostics["state_diagnostics"]
        second_first_breach = results[5].diagnostics["state_diagnostics"]
        self.assertEqual(first_breach["failure_level_breach_bars"], 1)
        self.assertEqual(recovered["failure_level_breach_bars"], 0)
        self.assertEqual(recovered["failure_structure_loss_bars"], 0)
        self.assertEqual(recovered["failure_watch_reset_reason"], "CURRENT_FAILURE_EVIDENCE_CLEARED")
        self.assertEqual(second_first_breach["failure_level_breach_bars"], 1)
        self.assertNotEqual(results[5].auction_state.current_state, AuctionStateName.TREND_FAILURE)
        self.assertEqual(results[6].auction_state.current_state, AuctionStateName.TREND_FAILURE)

    def test_pullback_reacceleration_promotes_structural_protection(self) -> None:
        engine = AuctionEngine(_test_config())
        ts = datetime(2026, 7, 20, 10, 0)
        rows = []
        for index, close in enumerate((100.20, 100.60, 101.00)):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=close - 0.18, high=close + 0.08,
                low=close - 0.22, close=close,
                hma_state="UPTREND", vwap_side="ABOVE",
                move_15m=0.7, move_30m=1.0, move_sod=1.2,
                raw_state="TRENDING_UP", raw_side="BUY",
                range_low=95.0, range_high=105.0,
                range_width_atr=10.0, efficiency=0.70, overlap=0.20,
            ))
        for index, (open_price, close) in enumerate(((101.00, 100.99), (100.99, 100.98)), start=3):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=open_price, high=open_price + 0.08,
                low=close - 0.08, close=close,
                hma_state="UPTREND", vwap_side="ABOVE",
                move_15m=0.2, move_30m=0.8, move_sod=0.9,
                raw_state="TRENDING_UP", raw_side="BUY",
                range_low=95.0, range_high=105.0,
                range_width_atr=10.0, efficiency=0.60, overlap=0.30,
            ))
        rows.append(_snapshot(
            ts + timedelta(minutes=15),
            open_price=100.90, high=101.60, low=100.85, close=101.50,
            hma_state="UPTREND", vwap_side="ABOVE",
            move_15m=0.8, move_30m=1.2, move_sod=1.4,
            raw_state="TRENDING_UP", raw_side="BUY",
            range_low=95.0, range_high=105.0,
            range_width_atr=10.0, efficiency=0.70, overlap=0.20,
        ))

        results = [engine.evaluate_snapshot(row) for row in rows]
        last = results[-1]
        self.assertEqual(last.auction_state.current_state, AuctionStateName.REACCELERATION)
        diag = last.diagnostics["state_diagnostics"]
        self.assertEqual(diag["trend_protection_source"], "CONFIRMED_PULLBACK_LOW")
        self.assertAlmostEqual(diag["trend_protection_level"], 100.85)
        self.assertGreaterEqual(diag["trend_protection_version"], 2)
        self.assertNotEqual(diag["trend_protection_source"], "CURRENT_LEG_ANCHOR")

    def test_recovered_failure_episode_is_terminal_and_active_fields_clear(self) -> None:
        engine = AuctionEngine(_test_config())
        ts = datetime(2026, 7, 20, 10, 0)
        rows = []
        for index, close in enumerate((100.20, 100.45, 100.70)):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=close - 0.18, high=close + 0.08,
                low=close - 0.22, close=close,
                hma_state="UPTREND", vwap_side="ABOVE",
                move_15m=0.7, move_30m=1.0, move_sod=1.2,
                raw_state="TRENDING_UP", raw_side="BUY",
                range_low=95.0, range_high=105.0,
                range_width_atr=10.0, efficiency=0.70, overlap=0.20,
            ))
        for index, close in enumerate((100.00, 99.30), start=3):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=close + 0.70, high=close + 0.75,
                low=close - 0.10, close=close,
                hma_state="DOWNTREND", vwap_side="BELOW",
                move_15m=-1.0, move_30m=-1.3, move_sod=-0.5,
                raw_state="TRENDING_DOWN", raw_side="SELL",
                range_low=95.0, range_high=105.0,
                range_width_atr=10.0, efficiency=0.72, overlap=0.20,
            ))
        for index, close in enumerate((100.10, 100.80, 101.40, 101.80), start=5):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=close - 0.65, high=close + 0.10,
                low=close - 0.70, close=close,
                hma_state="UPTREND", vwap_side="ABOVE",
                move_15m=0.9, move_30m=1.2, move_sod=1.3,
                raw_state="TRENDING_UP", raw_side="BUY",
                range_low=95.0, range_high=105.0,
                range_width_atr=10.0, efficiency=0.72, overlap=0.20,
            ))

        results = [engine.evaluate_snapshot(row) for row in rows]
        self.assertIn(AuctionStateName.TREND_FAILURE, [item.auction_state.current_state for item in results])
        last = results[-1]
        self.assertEqual(last.auction_state.current_state, AuctionStateName.ORDERLY_UPTREND)
        diag = last.diagnostics["state_diagnostics"]
        self.assertIsNone(diag["failure_episode_key"])
        self.assertIsNone(diag["failure_level"])
        self.assertEqual(diag["failure_watch_bars"], 0)
        self.assertEqual(diag["last_failure_terminal_reason"], "ORIGINAL_TREND_RECOVERED")
        self.assertTrue(diag["last_failure_terminal_key"])

    def _phase25_uptrend_rows(self, ts: datetime) -> list[dict]:
        rows = []
        for index, close in enumerate((98.00, 98.30, 98.60)):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=close - 0.18,
                high=close + 0.08,
                low=close - 0.22,
                close=close,
                hma_state="UPTREND",
                vwap_side="ABOVE",
                move_15m=0.7,
                move_30m=1.0,
                move_sod=1.2,
                raw_state="TRENDING_UP",
                raw_side="BUY",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.70,
                overlap=0.20,
            ))
        return rows

    def test_local_structure_weakening_far_from_protection_remains_watch(self) -> None:
        engine = AuctionEngine(_test_config())
        ts = datetime(2026, 7, 20, 10, 0)
        rows = self._phase25_uptrend_rows(ts)
        for index, (open_price, close) in enumerate(((99.20, 99.00), (99.10, 98.90)), start=3):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=open_price,
                high=open_price + 0.05,
                low=close - 0.10,
                close=close,
                hma_state="UPTREND",
                vwap_side="BELOW",
                move_15m=0.7,
                move_30m=1.0,
                move_sod=1.2,
                raw_state="TRENDING_DOWN",
                raw_side="SELL",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.70,
                overlap=0.20,
            ))

        results = [engine.evaluate_snapshot(row) for row in rows]
        last = results[-1]
        diag = last.diagnostics["state_diagnostics"]
        self.assertNotEqual(last.auction_state.current_state, AuctionStateName.TREND_FAILURE)
        self.assertEqual(diag["local_structure_weakening_bars"], 2)
        self.assertFalse(diag["structure_loss_near_protection"])
        self.assertTrue(diag["structure_loss_directional_corroboration"])
        self.assertIn(
            "PRICE_NOT_NEAR_FROZEN_TREND_PROTECTION",
            diag["structure_loss_confirmation_blockers"],
        )
        self.assertEqual(diag["failure_confirmation_reason"], "")

    def test_uncorroborated_structure_watch_expires_even_with_long_weakening_streak(self) -> None:
        engine = AuctionEngine(_test_config())
        ts = datetime(2026, 7, 20, 10, 0)
        rows = self._phase25_uptrend_rows(ts)
        for index, close in enumerate((99.30, 99.25, 99.20, 99.15, 99.10, 99.05), start=3):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=close + 0.15,
                high=close + 0.20,
                low=close - 0.10,
                close=close,
                hma_state="UPTREND",
                vwap_side="BELOW",
                move_15m=0.7,
                move_30m=1.0,
                move_sod=1.2,
                raw_state="TRENDING_DOWN",
                raw_side="SELL",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.70,
                overlap=0.20,
            ))

        results = [engine.evaluate_snapshot(row) for row in rows]
        self.assertNotIn(
            AuctionStateName.TREND_FAILURE,
            [item.auction_state.current_state for item in results],
        )
        diag = results[-1].diagnostics["state_diagnostics"]
        self.assertTrue(diag["failure_watch_expired"])
        self.assertIsNone(diag["failure_episode_key"])
        self.assertEqual(
            diag["last_failure_terminal_reason"],
            "FAILURE_WATCH_EXPIRED_WITHOUT_CORROBORATED_STRUCTURAL_CONFIRMATION",
        )

    def test_corroborated_structure_weakening_near_protection_can_confirm(self) -> None:
        engine = AuctionEngine(_test_config())
        ts = datetime(2026, 7, 20, 10, 0)
        rows = self._phase25_uptrend_rows(ts)
        for index, (open_price, close) in enumerate(((98.90, 98.75), (98.80, 98.65)), start=3):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=open_price,
                high=open_price + 0.05,
                low=close - 0.10,
                close=close,
                hma_state="UPTREND",
                vwap_side="BELOW",
                move_15m=0.7,
                move_30m=1.0,
                move_sod=1.2,
                raw_state="TRENDING_DOWN",
                raw_side="SELL",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.70,
                overlap=0.20,
            ))

        results = [engine.evaluate_snapshot(row) for row in rows]
        last = results[-1]
        diag = last.diagnostics["state_diagnostics"]
        self.assertEqual(last.auction_state.current_state, AuctionStateName.TREND_FAILURE)
        self.assertEqual(diag["failure_level_breach_bars"], 0)
        self.assertEqual(diag["local_structure_weakening_bars"], 2)
        self.assertTrue(diag["structure_loss_near_protection"])
        self.assertTrue(diag["structure_loss_directional_corroboration"])
        self.assertEqual(diag["structure_loss_confirmation_blockers"], [])
        self.assertEqual(
            diag["failure_confirmation_reason"],
            "CORROBORATED_LOCAL_STRUCTURE_WEAKENING_CONFIRMED",
        )

    def test_structure_weakening_without_directional_corroboration_remains_watch(self) -> None:
        engine = AuctionEngine(_test_config())
        ts = datetime(2026, 7, 20, 10, 0)
        rows = self._phase25_uptrend_rows(ts)
        for index, (open_price, close) in enumerate(((98.90, 98.75), (98.80, 98.65)), start=3):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=open_price,
                high=open_price + 0.05,
                low=close - 0.10,
                close=close,
                hma_state="UPTREND",
                vwap_side="ABOVE",
                move_15m=0.7,
                move_30m=1.0,
                move_sod=1.2,
                raw_state="TRENDING_DOWN",
                raw_side="SELL",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.70,
                overlap=0.20,
            ))

        results = [engine.evaluate_snapshot(row) for row in rows]
        last = results[-1]
        diag = last.diagnostics["state_diagnostics"]
        self.assertNotEqual(last.auction_state.current_state, AuctionStateName.TREND_FAILURE)
        self.assertTrue(diag["structure_loss_near_protection"])
        self.assertFalse(diag["structure_loss_directional_corroboration"])
        self.assertIn(
            "NO_CURRENT_ADVERSE_DIRECTIONAL_CORROBORATION",
            diag["structure_loss_confirmation_blockers"],
        )

    def test_active_failure_confirmation_reason_clears_but_history_remains(self) -> None:
        engine = AuctionEngine(_test_config())
        ts = datetime(2026, 7, 20, 10, 0)
        rows = self._phase25_uptrend_rows(ts)
        for index, (open_price, close) in enumerate(((98.90, 98.75), (98.80, 98.65)), start=3):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=open_price,
                high=open_price + 0.05,
                low=close - 0.10,
                close=close,
                hma_state="UPTREND",
                vwap_side="BELOW",
                move_15m=0.7,
                move_30m=1.0,
                move_sod=1.2,
                raw_state="TRENDING_DOWN",
                raw_side="SELL",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.70,
                overlap=0.20,
            ))
        rows.append(_snapshot(
            ts + timedelta(minutes=15),
            open_price=98.65,
            high=99.35,
            low=98.60,
            close=99.25,
            hma_state="UPTREND",
            vwap_side="ABOVE",
            move_15m=0.8,
            move_30m=1.1,
            move_sod=1.3,
            raw_state="TRENDING_UP",
            raw_side="BUY",
            range_low=95.0,
            range_high=105.0,
            range_width_atr=10.0,
            efficiency=0.70,
            overlap=0.20,
        ))

        results = [engine.evaluate_snapshot(row) for row in rows]
        confirmed = results[-2].diagnostics["state_diagnostics"]
        recovered_evidence = results[-1].diagnostics["state_diagnostics"]
        self.assertEqual(
            confirmed["failure_confirmation_reason"],
            "CORROBORATED_LOCAL_STRUCTURE_WEAKENING_CONFIRMED",
        )
        self.assertEqual(recovered_evidence["failure_confirmation_reason"], "")
        self.assertEqual(
            recovered_evidence["last_failure_confirmation_reason"],
            "CORROBORATED_LOCAL_STRUCTURE_WEAKENING_CONFIRMED",
        )
        self.assertTrue(recovered_evidence["last_failure_confirmation_time"])

    def test_stale_maximum_excursion_does_not_create_mature_extension(self) -> None:
        engine = AuctionEngine(_test_config())
        ts = datetime(2026, 7, 20, 10, 0)
        rows = []
        for index, close in enumerate((100.20, 100.45, 100.70, 101.50, 102.50)):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=close - 0.20,
                high=close + 0.10,
                low=close - 0.25,
                close=close,
                hma_state="UPTREND",
                vwap_side="ABOVE",
                move_15m=0.8,
                move_30m=1.3,
                move_sod=2.0,
                raw_state="TRENDING_UP",
                raw_side="BUY",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.72,
                overlap=0.20,
            ))
        rows.extend([
            _snapshot(
                ts + timedelta(minutes=15),
                open_price=102.50,
                high=102.55,
                low=101.10,
                close=101.20,
                hma_state="UPTREND",
                vwap_side="ABOVE",
                move_15m=0.3,
                move_30m=1.0,
                move_sod=1.2,
                raw_state="TRENDING_UP",
                raw_side="BUY",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.60,
                overlap=0.30,
            ),
            _snapshot(
                ts + timedelta(minutes=18),
                open_price=101.20,
                high=101.25,
                low=100.90,
                close=101.00,
                hma_state="UPTREND",
                vwap_side="ABOVE",
                move_15m=0.2,
                move_30m=0.8,
                move_sod=1.0,
                raw_state="TRENDING_UP",
                raw_side="BUY",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.55,
                overlap=0.35,
            ),
        ])

        results = [engine.evaluate_snapshot(row) for row in rows]
        flags = results[-1].diagnostics["state_flags"]
        self.assertGreater(flags["current_leg_distance_atr"], 1.5)
        self.assertLess(flags["current_leg_current_distance_atr"], 1.0)
        self.assertGreater(flags["current_leg_retracement_fraction"], 0.40)
        self.assertFalse(flags["current_leg_mature"])
        self.assertNotEqual(results[-1].auction_state.current_state, AuctionStateName.MATURE_EXTENSION)

    def test_prolonged_balance_neutralises_established_trend(self) -> None:
        payload = _test_config().resolved_dict()
        payload["state"]["trend_neutralisation_confirmation_bars"] = 3
        cfg = AuctionEngineConfig.model_validate(payload)
        engine = AuctionEngine(cfg)
        ts = datetime(2026, 7, 20, 10, 0)

        rows = []
        for index, close in enumerate((100.20, 100.45, 100.70)):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=close - 0.18,
                high=close + 0.08,
                low=close - 0.22,
                close=close,
                hma_state="UPTREND",
                vwap_side="ABOVE",
                move_15m=0.7,
                move_30m=1.0,
                move_sod=1.2,
                raw_state="TRENDING_UP",
                raw_side="BUY",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.70,
                overlap=0.20,
            ))
        neutral_closes = (100.68, 100.70, 100.69, 100.71, 100.70, 100.69, 100.71, 100.70, 100.69, 100.70)
        for index, close in enumerate(neutral_closes, start=3):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=close - 0.01,
                high=close + 0.05,
                low=close - 0.05,
                close=close,
                hma_state="NO_TREND",
                vwap_side="UNKNOWN",
                move_15m=0.0,
                move_30m=0.0,
                move_sod=0.0,
                raw_state="RANGE",
                raw_side="NEUTRAL",
                range_type="BALANCE",
                range_low=100.50,
                range_high=100.90,
                range_width_atr=0.40,
                efficiency=0.10,
                overlap=0.90,
            ))

        results = [engine.evaluate_snapshot(row) for row in rows]
        last = results[-1]
        self.assertIn(
            last.auction_state.current_state,
            {AuctionStateName.BALANCE, AuctionStateName.COMPRESSION},
        )
        self.assertEqual(last.diagnostics["state_diagnostics"]["established_trend_side"], "UNKNOWN")

    def test_unresolved_trend_failure_expires_to_neutral_state(self) -> None:
        payload = _test_config().resolved_dict()
        payload["state"]["trend_failure_max_bars"] = 3
        cfg = AuctionEngineConfig.model_validate(payload)
        engine = AuctionEngine(cfg)
        ts = datetime(2026, 7, 20, 10, 0)

        rows = []
        for index, close in enumerate((100.20, 100.45, 100.70)):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=close - 0.18,
                high=close + 0.08,
                low=close - 0.22,
                close=close,
                hma_state="UPTREND",
                vwap_side="ABOVE",
                move_15m=0.7,
                move_30m=1.0,
                move_sod=1.2,
                raw_state="TRENDING_UP",
                raw_side="BUY",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.70,
                overlap=0.20,
            ))
        rows.extend([
            _snapshot(
                ts + timedelta(minutes=9),
                open_price=100.70,
                high=100.75,
                low=99.90,
                close=100.00,
                hma_state="DOWNTREND",
                vwap_side="BELOW",
                move_15m=-0.9,
                move_30m=-1.1,
                move_sod=0.2,
                raw_state="TRENDING_DOWN",
                raw_side="SELL",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.70,
                overlap=0.20,
            ),
            _snapshot(
                ts + timedelta(minutes=12),
                open_price=100.00,
                high=100.05,
                low=99.20,
                close=99.30,
                hma_state="DOWNTREND",
                vwap_side="BELOW",
                move_15m=-1.0,
                move_30m=-1.3,
                move_sod=-0.5,
                raw_state="TRENDING_DOWN",
                raw_side="SELL",
                range_low=95.0,
                range_high=105.0,
                range_width_atr=10.0,
                efficiency=0.72,
                overlap=0.20,
            ),
        ])
        for index, close in enumerate((99.32, 99.31, 99.33, 99.32), start=5):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=close - 0.01,
                high=close + 0.05,
                low=close - 0.05,
                close=close,
                hma_state="NO_TREND",
                vwap_side="UNKNOWN",
                move_15m=0.0,
                move_30m=0.0,
                move_sod=0.0,
                raw_state="RANGE",
                raw_side="NEUTRAL",
                range_type="BALANCE",
                range_low=99.10,
                range_high=99.50,
                range_width_atr=0.40,
                efficiency=0.10,
                overlap=0.90,
            ))

        results = [engine.evaluate_snapshot(row) for row in rows]
        self.assertIn(AuctionStateName.TREND_FAILURE, [item.auction_state.current_state for item in results])
        last = results[-1]
        self.assertIn(last.auction_state.current_state, {AuctionStateName.BALANCE, AuctionStateName.COMPRESSION, AuctionStateName.UNKNOWN})
        self.assertEqual(last.diagnostics["state_diagnostics"]["established_trend_side"], "UNKNOWN")

    def test_state_timeline_is_chronological_and_report_only(self) -> None:
        cfg = _test_config()
        engine = AuctionEngine(cfg)
        ts = datetime(2026, 7, 20, 10, 0)

        rows = []
        # Three quiet observations confirm and freeze one compression episode.
        for index, close in enumerate((100.00, 100.05, 100.02)):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=close - 0.03,
                high=close + 0.08,
                low=close - 0.08,
                close=close,
                range_type="MICRO_COMPRESSION",
                range_low=99.70,
                range_high=100.30,
                range_width_atr=0.60,
                efficiency=0.10,
                overlap=0.85,
            ))

        # Fresh expansion and orderly trend establishment.
        rows.append(_snapshot(
            ts + timedelta(minutes=9),
            open_price=100.20, high=101.40, low=100.15, close=101.30,
            hma_state="UPTREND", hma_strength="STRONG", vwap_side="ABOVE",
            vwap_distance_atr=0.8, move_15m=0.8, move_30m=1.1, move_sod=1.2,
            raw_state="TRENDING_UP", raw_side="BUY",
            range_low=99.70, range_high=100.30, range_width_atr=0.60,
            efficiency=0.65, overlap=0.20,
        ))
        for index, close in enumerate((101.55, 101.80), start=4):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=close - 0.20, high=close + 0.10, low=close - 0.25, close=close,
                hma_state="UPTREND", hma_strength="STRONG", vwap_side="ABOVE",
                move_15m=0.8, move_30m=1.3, move_sod=1.5,
                raw_state="TRENDING_UP", raw_side="BUY", efficiency=0.70, overlap=0.20,
            ))

        # A single adverse bar does not replace the trend.  The second confirms
        # the pullback episode; the next strong bar anchors reacceleration.
        for index, (open_price, close) in enumerate(((101.80, 101.50), (101.50, 101.20)), start=6):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=open_price, high=open_price + 0.08, low=close - 0.08, close=close,
                hma_state="UPTREND", hma_strength="STRONG", vwap_side="ABOVE",
                move_15m=0.3, move_30m=1.0, move_sod=1.2,
                raw_state="TRENDING_UP", raw_side="BUY", efficiency=0.60, overlap=0.30,
            ))
        for index, (open_price, close) in enumerate(((101.20, 101.80), (101.80, 102.30), (102.30, 102.50)), start=8):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=open_price, high=close + 0.10, low=open_price - 0.08, close=close,
                hma_state="UPTREND", hma_strength="STRONG", vwap_side="ABOVE",
                move_15m=0.9, move_30m=1.4, move_sod=2.0,
                raw_state="TRENDING_UP", raw_side="BUY", efficiency=0.72, overlap=0.20,
            ))

        # Failure and reversal each require their own multi-bar confirmation.
        for index, (open_price, close) in enumerate(
            ((102.50, 101.60), (101.60, 100.70), (100.70, 99.90),
             (99.90, 99.10), (99.10, 98.40), (98.40, 97.80)),
            start=11,
        ):
            rows.append(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=open_price, high=open_price + 0.08, low=close - 0.10, close=close,
                hma_state="DOWNTREND", hma_strength="STRONG", vwap_side="BELOW",
                vwap_distance_atr=-0.8, move_15m=-0.9, move_30m=-1.3, move_sod=0.5,
                raw_state="TRENDING_DOWN", raw_side="SELL", efficiency=0.70, overlap=0.20,
            ))

        results = [engine.evaluate_snapshot(row) for row in rows]
        states = [result.auction_state.current_state for result in results]

        self.assertEqual(states[2], AuctionStateName.COMPRESSION)
        self.assertEqual(states[3], AuctionStateName.FRESH_EXPANSION)
        self.assertEqual(states[5], AuctionStateName.ORDERLY_UPTREND)
        self.assertEqual(states[6], AuctionStateName.ORDERLY_UPTREND)
        self.assertEqual(states[7], AuctionStateName.CONTROLLED_PULLBACK)
        self.assertEqual(states[8], AuctionStateName.REACCELERATION)
        self.assertEqual(states[10], AuctionStateName.ORDERLY_UPTREND)
        self.assertEqual(states[12], AuctionStateName.ORDERLY_UPTREND)
        self.assertEqual(states[13], AuctionStateName.TREND_FAILURE)
        self.assertEqual(states[-1], AuctionStateName.REVERSAL)

        compression_diag = results[2].diagnostics["state_diagnostics"]
        self.assertTrue(compression_diag["compression_episode_key"])
        self.assertLess(compression_diag["compression_box_low"], compression_diag["compression_box_high"])

        pullback_diag = results[7].diagnostics["state_diagnostics"]
        self.assertTrue(pullback_diag["pullback_episode_key"])
        reaccel_diag = results[8].diagnostics["state_diagnostics"]
        self.assertTrue(reaccel_diag["reacceleration_episode_key"])
        self.assertEqual(reaccel_diag["established_trend_side"], "UP")

        reversal_diag = results[-1].diagnostics["state_diagnostics"]
        self.assertEqual(reversal_diag["established_trend_side"], "DOWN")
        self.assertTrue(reversal_diag["reversal_onset_time"])

        last = engine.evaluate_snapshot(rows[-1])
        self.assertEqual(last.local_decision.action, LocalDecisionAction.WATCH)
        self.assertEqual(last.advisor_decisions, ())
        self.assertFalse(engine.config.decision.create_enabled)
        self.assertEqual(last.diagnostics["decision_scope"], "LOCAL_AUCTION_ONLY")
        self.assertFalse(last.diagnostics["signal_lifecycle_applied"])

    def test_cumulative_day_flip_count_does_not_force_chaos(self) -> None:
        engine = AuctionEngine(_test_config())
        ts = datetime(2026, 7, 20, 10, 0)
        result = None
        for index in range(8):
            close = 100.0 + index * 0.25
            result = engine.evaluate_snapshot(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=close - 0.15,
                high=close + 0.10,
                low=close - 0.20,
                close=close,
                hma_state="UPTREND",
                vwap_side="ABOVE",
                move_15m=0.7,
                move_30m=1.2,
                move_sod=1.5,
                raw_state="TRENDING_UP",
                raw_side="BUY",
                efficiency=0.10,
                flip_count=20,
            ))
        self.assertIsNotNone(result)
        self.assertNotEqual(result.auction_state.current_state, AuctionStateName.CHAOTIC_ROTATION)
        flags = result.diagnostics["state_flags"]
        self.assertEqual(flags["cumulative_day_flip_count"], 20)
        self.assertEqual(flags["independent_flip_channels"], 0)

    def test_local_multi_channel_rotation_can_be_chaotic(self) -> None:
        engine = AuctionEngine(_test_config())
        ts = datetime(2026, 7, 20, 10, 0)
        result = None
        for index in range(10):
            up = index % 2 == 0
            close = 100.10 if up else 99.90
            result = engine.evaluate_snapshot(_snapshot(
                ts + timedelta(minutes=index * 3),
                open_price=99.95 if up else 100.05,
                high=100.20,
                low=99.80,
                close=close,
                hma_state="UPTREND" if up else "DOWNTREND",
                vwap_side="ABOVE" if up else "BELOW",
                move_15m=0.05 if up else -0.05,
                move_30m=0.05 if up else -0.05,
                move_sod=0.05 if up else -0.05,
                raw_state="BALANCE_QUALIFIED",
                raw_side="BUY" if up else "SELL",
                efficiency=0.10,
                overlap=0.90,
            ))
        self.assertIsNotNone(result)
        self.assertEqual(result.auction_state.current_state, AuctionStateName.CHAOTIC_ROTATION)
        flags = result.diagnostics["state_flags"]
        self.assertGreaterEqual(flags["independent_flip_channels"], 2)
        self.assertGreaterEqual(flags["bar_flip_count"], 4)

    def test_out_of_order_snapshot_is_rejected(self) -> None:
        engine = AuctionEngine(_test_config())
        ts = datetime(2026, 7, 20, 10, 0)
        engine.evaluate_snapshot(_snapshot(ts, open_price=100.0, high=100.2, low=99.8, close=100.0))
        with self.assertRaises(AuctionStateChronologyError):
            engine.evaluate_snapshot(_snapshot(ts - timedelta(minutes=3), open_price=100.0, high=100.2, low=99.8, close=100.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
