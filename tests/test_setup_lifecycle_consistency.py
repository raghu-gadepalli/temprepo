#!/usr/bin/env python3
"""Focused invariants shared by setup lifecycles and replay diagnostics.

Run directly:

    python tests/test_setup_lifecycle_consistency.py

These checks are DB-free and intentionally exercise the common mechanics used
by ACCEPTED_BREAKOUT, FAILED_BREAKOUT and EXHAUSTION_REVERSAL.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from schemas.stock_setup_state import _merge_state_json_for_upsert
from services.audit.auditlog import _payload_with_audit_mode, _resolve_audit_ts
from services.evidence.setup_discovery_helper import SetupCandidate, SetupDiscoverer


def test_audit_requires_source_time() -> None:
    try:
        _resolve_audit_ts(None, {})
    except ValueError:
        pass
    else:
        raise AssertionError("audit timestamp silently fell back instead of failing")

    ts = datetime(2026, 7, 17, 11, 30)
    assert _resolve_audit_ts(ts, {}) == ts
    payload = _payload_with_audit_mode({"snapshot_time": ts}) or {}
    assert "audit_written_at" not in payload
    assert "audit_source_ts" not in payload


def test_transition_time_is_observation_snapshot() -> None:
    row = SimpleNamespace(
        setup="FAILED_BREAKOUT",
        side="BUY",
        state="WATCH",
        state_reason="OLD",
        signal_id=None,
        first_seen_time=datetime(2026, 7, 17, 11, 24),
        last_seen_time=datetime(2026, 7, 17, 11, 27),
        expires_at=datetime(2026, 7, 17, 11, 39, 30),
        reference_price=320.20,
        reference_source="OLD",
        discovery_price=320.10,
        discovery_extreme_price=320.20,
        confirmation_price=None,
        confirmation_time=None,
        state_json={
            "event_key": "OLD|SELL|2026-07-17T11:24:00",
            "event_time": "2026-07-17T11:24:00",
            "transition_history": [
                {
                    "event_key": "OLD|SELL|2026-07-17T11:24:00",
                    "state": "WATCH",
                    "state_reason": "OLD",
                    "transition_time": "2026-07-17T11:24:00",
                }
            ],
        },
    )
    merged = _merge_state_json_for_upsert(
        row=row,
        data={
            "setup": "FAILED_BREAKOUT",
            "side": "BUY",
            "state": "CONFIRMED",
            "state_reason": "NEW",
            "first_seen_time": datetime(2026, 7, 17, 11, 24),
            "last_seen_time": datetime(2026, 7, 17, 11, 30),
            "reference_price": 320.20,
            "signal_id": None,
            "state_json": {
                "event_key": "NEW|SELL|2026-07-17T11:24:00",
                "event_time": "2026-07-17T11:24:00",
            },
        },
    )
    new_transition = next(
        item
        for item in merged["transition_history"]
        if str(item.get("event_key") or "").startswith("NEW|")
    )
    assert new_transition["event_time"] == "2026-07-17T11:24:00"
    assert new_transition["transition_time"] == "2026-07-17T11:30:00"


def test_coincident_breakout_boundaries_remain_distinct_with_dynamic_priority() -> None:
    snapshot = {
        "indicators": {"atr": {"value": 1.0}},
        "structure": {
            "anchors": {
                "orb_ready": True,
                "orb_high": 101.0,
                "orb_low": 99.999999,
            },
            "accepted": {
                "quality": 80,
                "range": {
                    "range_id": "R1",
                    "version": 2,
                    "high": 102.0,
                    "low": 100.0,
                    "source": "INTRADAY_BALANCE",
                    "range_type": "BALANCE",
                    "breakout_eligible": True,
                },
            },
        },
        "levels": {
            "opening_range": {
                "ready": True,
                "high": 101.0,
                "low": 99.999999,
            },
            "prev_day": {},
        },
    }
    discoverer = SetupDiscoverer(
        persist_setup_state=False,
        read_persistent_setup_state=False,
    )
    levels = discoverer._structural_level_candidates(snapshot)
    sell_levels = [item for item in levels if item.get("side") == "SELL"]
    assert len(sell_levels) == 2
    assert sell_levels[0]["reference_id"] == "R1:V2:LOW"
    assert sell_levels[0]["level_type"] == "DYNAMIC_RANGE_LOW"
    assert sell_levels[0]["rank"] == 10
    assert sell_levels[0]["range_context"]["source"] == "INTRADAY_BALANCE"
    assert sell_levels[1]["reference_id"] == "ORB_LOW"
    assert sell_levels[1]["level_type"] == "ORB_LOW"
    assert sell_levels[1]["rank"] == 20
    assert sell_levels[1]["range_context"]["source"] == "ORB"


def test_failed_breakout_reference_owns_value_range() -> None:
    snapshot = {
        "levels": {
            "opening_range": {"ready": True, "high": 101.0, "low": 100.0},
            "prev_day": {"high": 105.0, "low": 95.0},
        },
        "structure": {
            "accepted": {
                "quality": 80,
                "range": {
                    "range_id": "R1",
                    "version": 2,
                    "high": 102.0,
                    "low": 100.0,
                    "source": "INTRADAY_BALANCE",
                    "range_type": "BALANCE",
                    "breakout_eligible": True,
                },
            },
        },
    }
    discoverer = SetupDiscoverer(
        persist_setup_state=False,
        read_persistent_setup_state=False,
    )
    dynamic = {
        "level_type": "DYNAMIC_RANGE_LOW",
        "range_context": {
            "range_id": "R1",
            "version": 2,
            "high": 102.0,
            "low": 100.0,
            "source": "INTRADAY_BALANCE",
            "range_type": "BALANCE",
            "breakout_eligible": True,
        },
    }
    orb = {
        "level_type": "ORB_LOW",
        "range_context": {
            "range_id": "ORB",
            "version": 1,
            "high": 101.0,
            "low": 100.0,
            "source": "ORB",
            "range_type": "OPENING_RANGE",
            "breakout_eligible": True,
        },
    }
    dynamic_range = discoverer._failed_breakout_value_range_context(snapshot, dynamic)
    orb_range = discoverer._failed_breakout_value_range_context(snapshot, orb)
    assert dynamic_range["range_id"] == "R1"
    assert dynamic_range["high"] == 102.0
    assert orb_range["range_id"] == "ORB"
    assert orb_range["high"] == 101.0



def test_failed_breakout_watches_keep_orb_and_dynamic_separate() -> None:
    snapshot_time = datetime(2026, 7, 17, 10, 6)
    snapshot = {
        "symbol": "TEST",
        "snapshot_time": snapshot_time,
        "bar": {"open": 100.0, "high": 100.4, "low": 99.8, "close": 100.2},
        "indicators": {"atr": {"value": 1.0}},
        "structure": {
            "anchors": {"orb_ready": True, "orb_high": 101.0, "orb_low": 100.0},
            "recent_closes": [
                {"time": datetime(2026, 7, 17, 10, 0), "close": 99.70},
                {"time": datetime(2026, 7, 17, 10, 3), "close": 99.65},
                {"time": snapshot_time, "close": 100.20},
            ],
            "accepted": {
                "quality": 80,
                "range": {
                    "range_id": "R1",
                    "version": 2,
                    "high": 102.0,
                    "low": 100.0,
                    "source": "INTRADAY_BALANCE",
                    "range_type": "BALANCE",
                    "breakout_eligible": True,
                },
            },
        },
        "levels": {
            "opening_range": {"ready": True, "high": 101.0, "low": 100.0},
            "prev_day": {},
        },
    }
    discoverer = SetupDiscoverer(
        persist_setup_state=False,
        read_persistent_setup_state=False,
    )
    discoverer._snapshot = snapshot
    watches = discoverer._failed_breakout_watches_from_snapshot(
        snapshot,
        age_bars=0,
        source="TEST",
    )
    assert [watch["reference_id"] for watch in watches] == ["R1:V2:LOW", "ORB_LOW"]
    assert watches[0]["accepted_range_id"] == "R1"
    assert watches[0]["accepted_range_width_atr"] == 2.0
    assert watches[1]["accepted_range_id"] == "ORB"
    assert watches[1]["accepted_range_width_atr"] == 1.0
    assert watches[0]["event_key"] != watches[1]["event_key"]


def test_dynamic_failed_breakout_is_preferred_when_equally_eligible() -> None:
    discoverer = SetupDiscoverer(
        persist_setup_state=False,
        read_persistent_setup_state=False,
    )

    def candidate(*, rank: int, blocked: bool, reference_id: str) -> SetupCandidate:
        return SetupCandidate(
            setup_label="FAILED_BREAKOUT",
            strategy="TEST",
            side="BUY",
            priority=1,
            discovered=True,
            price_action_confirmed=True,
            price_action_strength=80.0,
            entry_blocked=blocked,
            blocked_by="BLOCKED" if blocked else None,
            reason_code="TEST",
            reason_text="TEST",
            evidence_state="ENTRY_DEFERRED" if blocked else "ENTRY_READY",
            data={
                "setup_inputs": {"level_rank": rank, "reference_id": reference_id},
                "entry_location_filter": {"entry_distance_from_level_atr": 0.2},
            },
        )

    dynamic = candidate(rank=10, blocked=False, reference_id="R1:V2:LOW")
    orb = candidate(rank=20, blocked=False, reference_id="ORB_LOW")
    ordered = sorted([orb, dynamic], key=discoverer._failed_breakout_candidate_sort_key)
    assert ordered[0].data["setup_inputs"]["reference_id"] == "R1:V2:LOW"

    blocked_dynamic = candidate(rank=10, blocked=True, reference_id="R1:V2:LOW")
    ordered = sorted([blocked_dynamic, orb], key=discoverer._failed_breakout_candidate_sort_key)
    assert ordered[0].data["setup_inputs"]["reference_id"] == "ORB_LOW"


def test_failed_breakout_archived_terminal_event_cannot_reactivate() -> None:
    discoverer = SetupDiscoverer(
        persist_setup_state=False,
        read_persistent_setup_state=True,
    )
    discoverer._snapshot = {
        "symbol": "MANAPPURAM",
        "snapshot_time": datetime(2026, 7, 17, 11, 36),
    }
    consumed_orb_key = "ORB_LOW|SELL|2026-07-17T11:24:00"
    active_dynamic_key = "RANGE:V2:LOW|SELL|2026-07-17T11:24:00"
    row = SimpleNamespace(
        state="WATCH",
        expires_at=datetime(2026, 7, 17, 11, 39, 30),
        state_json={
            "watch": {
                "event_key": active_dynamic_key,
                "reference_id": "RANGE:V2:LOW",
                "breakout_side": "SELL",
                "candidate_side": "BUY",
                "event_time": "2026-07-17T11:24:00",
            },
            "event_history": [
                {
                    "event_key": consumed_orb_key,
                    "event_time": "2026-07-17T11:24:00",
                    "final_state": "CONSUMED",
                    "signal_id": "SIG-1",
                }
            ],
        },
    )
    discoverer._failed_breakout_state_row = lambda *, candidate_side: row

    incoming_orb = {
        "event_key": consumed_orb_key,
        "reference_id": "ORB_LOW",
        "breakout_side": "SELL",
        "candidate_side": "BUY",
        "event_time": datetime(2026, 7, 17, 11, 24),
    }
    assert discoverer._failed_breakout_same_event_is_terminal(incoming_orb)

    genuinely_new_orb = {
        **incoming_orb,
        "event_key": "ORB_LOW|SELL|2026-07-17T12:00:00",
        "event_time": datetime(2026, 7, 17, 12, 0),
    }
    assert not discoverer._failed_breakout_same_event_is_terminal(genuinely_new_orb)


def test_dynamic_accepted_breakout_is_preferred_when_equally_eligible() -> None:
    discoverer = SetupDiscoverer(
        persist_setup_state=False,
        read_persistent_setup_state=False,
    )

    def candidate(*, rank: int, blocked: bool, reference_id: str) -> SetupCandidate:
        return SetupCandidate(
            setup_label="ACCEPTED_BREAKOUT",
            strategy="TEST",
            side="BUY",
            priority=1,
            discovered=True,
            price_action_confirmed=True,
            price_action_strength=80.0,
            entry_blocked=blocked,
            blocked_by="BLOCKED" if blocked else None,
            reason_code="TEST",
            reason_text="TEST",
            evidence_state="ENTRY_DEFERRED" if blocked else "ENTRY_READY",
            data={
                "setup_inputs": {"level_rank": rank, "reference_id": reference_id},
                "entry_location_filter": {"entry_distance_from_level_atr": 0.2},
            },
        )

    dynamic = candidate(rank=10, blocked=False, reference_id="R1:V2:HIGH")
    orb = candidate(rank=20, blocked=False, reference_id="ORB_HIGH")
    ordered = sorted([orb, dynamic], key=discoverer._accepted_breakout_candidate_sort_key)
    assert ordered[0].data["setup_inputs"]["reference_id"] == "R1:V2:HIGH"

    blocked_dynamic = candidate(rank=10, blocked=True, reference_id="R1:V2:HIGH")
    ordered = sorted([blocked_dynamic, orb], key=discoverer._accepted_breakout_candidate_sort_key)
    assert ordered[0].data["setup_inputs"]["reference_id"] == "ORB_HIGH"

def test_exhaustion_event_key_is_causal_and_frozen() -> None:
    discoverer = SetupDiscoverer(
        persist_setup_state=False,
        read_persistent_setup_state=False,
    )
    discoverer._snapshot = {
        "symbol": "INFY",
        "snapshot_time": datetime(2026, 7, 17, 10, 15),
    }
    memory_key = discoverer._exhaustion_watch_key("BUY")
    assert memory_key == "INFY|2026-07-17|BUY"
    event_key = f"{memory_key}|{discoverer._snapshot_dt().isoformat()}"
    assert event_key == "INFY|2026-07-17|BUY|2026-07-17T10:15:00"



def test_exhaustion_persistence_uses_explicit_event_identity() -> None:
    discoverer = SetupDiscoverer(
        persist_setup_state=True,
        read_persistent_setup_state=False,
    )
    ts = datetime(2026, 7, 17, 10, 15)
    discoverer._snapshot = {
        "symbol": "INFY",
        "snapshot_time": ts,
    }
    captured = {}
    discoverer._safe_setup_state_upsert = lambda payload: captured.update(payload)
    watch = {
        "snapshot_time": ts,
        "event_time": ts,
        "event_key": "INFY|2026-07-17|BUY|2026-07-17T10:15:00",
        "source": "SYNTHETIC_EXTREME",
        "side": "BUY",
        "low": 100.0,
        "high": 101.0,
        "close": 100.2,
        "age_bars": 0,
    }
    discoverer._write_exhaustion_watch_state(
        side="BUY",
        watch=watch,
        state_reason="TEST",
    )
    state_json = captured["state_json"]
    assert captured["first_seen_time"] == ts
    assert captured["last_seen_time"] == ts
    assert state_json["event_key"] == watch["event_key"]
    assert state_json["event_time"] == ts


def main() -> None:
    tests = [
        test_audit_requires_source_time,
        test_transition_time_is_observation_snapshot,
        test_coincident_breakout_boundaries_remain_distinct_with_dynamic_priority,
        test_failed_breakout_reference_owns_value_range,
        test_failed_breakout_watches_keep_orb_and_dynamic_separate,
        test_dynamic_failed_breakout_is_preferred_when_equally_eligible,
        test_failed_breakout_archived_terminal_event_cannot_reactivate,
        test_dynamic_accepted_breakout_is_preferred_when_equally_eligible,
        test_exhaustion_event_key_is_causal_and_frozen,
        test_exhaustion_persistence_uses_explicit_event_identity,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS all {len(tests)} setup lifecycle consistency checks")


if __name__ == "__main__":
    main()
