#!/usr/bin/env python3
"""Offline unittests for the strict cleaned snapshot/Auction contract."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import unittest

from pydantic import ValidationError

from schemas.snapshot import SnapshotSchema
from services.auction_engine.engine import AuctionEngine
from services.auction_engine.snapshot_adapter import (
    empty_auction_block,
    empty_auction_memory,
    enrich_snapshot_with_auction,
)
from test_auction_engine_phase2 import _snapshot, _test_config


_STATE_KEYS = (
    "hma.state",
    "vwap.side",
    "structure.accepted",
    "structure.raw.side",
    "structure.candidate",
    "structure.raw.state",
    "structure.session_phase",
    "structure.accepted.state",
    "structure.candidate.active",
)


def _state_entry(state: str) -> dict:
    return {
        "raw_state": state,
        "state": state,
        "count": 1,
        "previous_state": None,
        "previous_count": 0,
        "candidate_state": None,
        "candidate_count": 0,
        "flip_count_today": 0,
    }


def _market_window(source: dict | None, *, bars: int) -> dict:
    row = source if source is not None else {}
    return {
        "status": row["status"] if "status" in row else "na",
        "bars": int(row["bars"]) if "bars" in row else bars,
        "move_points": row["move_points"] if "move_points" in row else None,
        "move_pct": row["move_pct"] if "move_pct" in row else None,
        "move_atr": row["move_atr"] if "move_atr" in row else None,
        "range_points": row["range_points"] if "range_points" in row else None,
        "range_pct": row["range_pct"] if "range_pct" in row else None,
        "close_position_in_range": (
            row["close_position_in_range"]
            if "close_position_in_range" in row
            else None
        ),
    }


def _strict_snapshot(raw: dict) -> SnapshotSchema:
    data = deepcopy(raw)
    close = float(data["close"])
    ts = data["snapshot_time"]

    data["version"] = "SNAPSHOT_AUCTION_V1"
    data["ltp"] = close
    data["ltp_time"] = ts
    data["gen_signals"] = True
    data["levels"]["opening_range"]["window"] = "09:15-09:29"

    data["indicators"]["ema"] = {
        "fast": None,
        "mid1": None,
        "mid2": None,
        "slow": None,
        "ref": None,
    }
    data["indicators"]["hma"]["flip_count_today"] = 0
    data["indicators"]["vwap"].update({
        "distance_pct": 0.0,
        "distance_points": 0.0,
        "flip_count_today": 0,
    })
    data["indicators"]["bollinger"].update({
        "upper": None,
        "mid": None,
        "lower": None,
        "bb_width": None,
    })

    data["volume"] = {
        "bar_volume": 10000.0,
        "bar_rvol": 1.1,
        "bar_rvol_pct": 110.0,
        "bar_rvol_band": "NORMAL",
        "bar_volume_slope": None,
        "today_cum": 10000.0,
        "prev_day_total": 100000.0,
        "today_vs_prev_ratio": 0.1,
        "periods": {},
    }
    source_windows = data["market_windows"]
    data["market_windows"] = {
        "15m": _market_window(source_windows["15m"], bars=5),
        "30m": _market_window(source_windows["30m"], bars=10),
        "60m": _market_window(None, bars=0),
        "sod": _market_window(source_windows["sod"], bars=10),
    }

    slope = data["price_action"]["slope"]
    data["price_action"] = {
        "slope": {
            "status": "ok",
            "bars_3_atr": None,
            "bars_5_atr": None,
            "bars_3_atr_per_bar": slope["bars_3_atr_per_bar"],
            "bars_5_atr_per_bar": slope["bars_5_atr_per_bar"],
            "previous_3_atr_per_bar": None,
            "state": slope["state"],
        }
    }
    data["structure"]["flip_count_today"] = 0
    data["derivatives"] = {
        "spot_price": None,
        "future": None,
        "options_lite": None,
        "option_ladder": None,
        "option_sentiment_windows": None,
        "future_sentiment_windows": None,
    }

    memory_state = {key: _state_entry("UNKNOWN") for key in _STATE_KEYS}
    data["memory"] = {
        "structure": {
            "snapshot_time": ts,
            "bars_3m": [{
                "date": ts,
                "open": data["bar"]["open"],
                "high": data["bar"]["high"],
                "low": data["bar"]["low"],
                "close": data["bar"]["close"],
                "volume": data["bar"]["volume"],
            }],
            "bars_15m": [],
            "state": memory_state,
        },
        "auction": empty_auction_memory().model_dump(mode="python"),
    }
    data["auction"] = empty_auction_block().model_dump(
        mode="python", by_alias=True
    )

    for obsolete in (
        "indicator_windows",
        "state_context",
        "state_memory",
        "structure_memory",
        "events",
        "generation_diagnostics",
    ):
        if obsolete in data:
            del data[obsolete]
    return SnapshotSchema.model_validate(data)


class AuctionSnapshotStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = datetime(2026, 7, 20, 10, 0)

    def _rows(self) -> list[SnapshotSchema]:
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
            _strict_snapshot(_snapshot(
                self.ts + timedelta(minutes=offset * 3),
                range_type="MICRO_COMPRESSION",
                range_low=99.0,
                range_high=101.0,
                range_width_atr=2.0,
                **values,
            ))
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
            "opportunities": tuple(sorted(
                (
                    item.opportunity_key,
                    item.lifecycle_state,
                    item.selected_candidate_id,
                )
                for item in engine.opportunity_ledger.records("TEST")
            )),
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

        self.assertEqual(
            set(carried_state),
            {
                "history", "state_memory", "boundary_current",
                "boundary_last_time", "boundary_sequences",
                "boundary_last_terminal", "setup_initiation", "setup_failed",
                "setup_emitted_once", "setup_completed", "setup_last_time",
                "ledger_records", "ledger_last_day",
            },
        )
        encoded = json.dumps(
            carried_state, sort_keys=True, separators=(",", ":"), default=str
        )
        self.assertNotIn("engine_version", encoded)
        self.assertNotIn("config_version", encoded)
        self.assertNotIn("__kind__", encoded)

    def test_adapter_uses_previous_snapshot_continuity(self):
        previous = None
        modes = []
        for pre in self._rows()[:3]:
            block, memory = enrich_snapshot_with_auction(
                pre,
                previous_snapshot=previous,
            )
            payload = pre.model_dump(mode="python", by_alias=True)
            payload["auction"] = block.model_dump(mode="python", by_alias=True)
            payload["memory"]["auction"] = memory.model_dump(mode="python")
            previous = SnapshotSchema.model_validate(payload)
            modes.append(block.continuity_mode)

        self.assertEqual(modes[0], "COLD_START")
        self.assertEqual(modes[1:], [
            "INCREMENTAL_PREVIOUS_SNAPSHOT",
            "INCREMENTAL_PREVIOUS_SNAPSHOT",
        ])
        self.assertEqual(previous.auction.status, "OK")
        self.assertIsInstance(previous.auction.changes, list)

    def test_schema_rejects_missing_mismatch_and_obsolete_fields(self):
        valid = self._rows()[0].model_dump(mode="python", by_alias=True)

        missing = deepcopy(valid)
        del missing["close"]
        with self.assertRaises(ValidationError):
            SnapshotSchema.model_validate(missing)

        mismatch = deepcopy(valid)
        mismatch["bar"]["close"] += 0.5
        with self.assertRaises(ValidationError):
            SnapshotSchema.model_validate(mismatch)

        obsolete = deepcopy(valid)
        obsolete["indicator_windows"] = {}
        with self.assertRaises(ValidationError):
            SnapshotSchema.model_validate(obsolete)

    def test_persisted_projection_is_clean(self):
        payload = self._rows()[0].to_db_dict()
        for key in (
            "indicator_windows", "events", "generation_diagnostics",
            "state_context", "state_memory", "structure_memory",
        ):
            self.assertNotIn(key, payload)
        self.assertNotIn("ltp", payload)
        self.assertNotIn("ltp_time", payload)
        self.assertNotIn("diagnostics", payload["structure"])
        self.assertNotIn("recent_closes", payload["structure"])
        self.assertNotIn("anchors", payload["structure"])
        self.assertNotIn("breakout_context", payload["structure"])
        self.assertEqual(payload["close"], payload["bar"]["close"])

    def test_snapshot_pipeline_has_no_dict_get_or_second_snapshot_write(self):
        generator = Path("services/snapshot/snapshot_generator.py").read_text(
            encoding="utf-8"
        )
        adapter = Path("services/auction_engine/snapshot_adapter.py").read_text(
            encoding="utf-8"
        )
        schema = Path("schemas/snapshot.py").read_text(encoding="utf-8")
        self.assertNotIn(".get(", generator)
        self.assertNotIn(".get(", adapter)
        self.assertNotIn(".get(", schema)
        self.assertIn("SnapshotSchema.create_snapshot(snapshot)", generator)
        self.assertNotIn("SnapshotSchema.update_snapshot(", generator)
        self.assertNotIn('"generation_diagnostics"', generator)


if __name__ == "__main__":
    unittest.main(verbosity=2)
