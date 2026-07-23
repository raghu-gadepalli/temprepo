#!/usr/bin/env python3
"""Strict Auction-to-TradeMonitor signal contract.

TradeMonitor does not rank peer signals, recompute evidence, infer confidence, or
search compatibility paths. A signal-linked trade must resolve the exact source
signal and that signal must carry the current ``AUCTION_SIGNAL_DOWNSTREAM_V1``
contract emitted by SignalGenerator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional


CONTRACT_VERSION = "AUCTION_SIGNAL_DOWNSTREAM_V1"
_ALLOWED_STAGES = {
    "DISCOVERY",
    "BUILDING",
    "ACTIVE",
    "EXPAND",
    "PROTECT",
    "TRANSITION",
    "WEAKENING",
    "EXIT_BIAS",
    "FORCE_EXIT",
}
_ALLOWED_STATUSES = {
    "OPEN",
    "CLOSED",
    "INVALIDATED",
    "EXPIRED",
    "REPLACED",
    "BLOCKED",
    "CANCELLED",
}
_ALLOWED_MANAGEMENT_POSTURES = {"STRENGTHEN", "CAUTION", "EXIT"}
_ALLOWED_TRADE_ACTIONS = {
    "CREATE_TRADE",
    "HOLD_POSITION",
    "TIGHTEN_STOP",
    "EXIT_POSITION",
    "FORCE_EXIT",
}
_TERMINAL_STATUSES = {
    "CLOSED",
    "INVALIDATED",
    "EXPIRED",
    "REPLACED",
    "BLOCKED",
    "CANCELLED",
}
_EXIT_STAGES = {"EXIT_BIAS", "FORCE_EXIT"}
_EXIT_TRADE_ACTIONS = {"EXIT_POSITION", "FORCE_EXIT"}


def _enum_text(value: Any, field_name: str) -> str:
    raw = getattr(value, "value", value)
    text = str(raw).strip().upper()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _required_mapping(container: Mapping[str, Any], key: str, path: str) -> Mapping[str, Any]:
    if key not in container:
        raise ValueError(f"{path}.{key} is required")
    value = container[key]
    if not isinstance(value, Mapping):
        raise TypeError(f"{path}.{key} must be an object")
    return value


def _required_text(container: Mapping[str, Any], key: str, path: str) -> str:
    if key not in container:
        raise ValueError(f"{path}.{key} is required")
    value = container[key]
    text = str(value).strip().upper()
    if not text:
        raise ValueError(f"{path}.{key} is required")
    return text


def _required_raw_text(container: Mapping[str, Any], key: str, path: str) -> str:
    if key not in container:
        raise ValueError(f"{path}.{key} is required")
    value = container[key]
    text = str(value).strip()
    if not text:
        raise ValueError(f"{path}.{key} is required")
    return text


def _required_bool(container: Mapping[str, Any], key: str, path: str) -> bool:
    if key not in container:
        raise ValueError(f"{path}.{key} is required")
    value = container[key]
    if not isinstance(value, bool):
        raise TypeError(f"{path}.{key} must be boolean")
    return value


def _required_datetime_text(container: Mapping[str, Any], key: str, path: str) -> str:
    text = _required_raw_text(container, key, path)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception as exc:
        raise ValueError(f"{path}.{key} must be ISO datetime") from exc
    return text


def _assert_equal(label: str, left: Any, right: Any) -> None:
    if left != right:
        raise ValueError(f"{label} mismatch: {left!r} != {right!r}")


@dataclass(frozen=True)
class AuctionTradeSignalContext:
    signal_id: str
    contract_version: str
    side: str
    setup_family: str
    setup_subtype: str
    stage: str
    status: str
    signal_action: str
    signal_state: str
    lifecycle_trade_action: str
    management_posture: str
    lifecycle_reason: str
    management_reason_code: str
    auction_action: str
    auction_state: str
    directional_alignment: str
    target_expansion_allowed: bool
    should_exit_signal: bool
    trail_mode: str
    exit_pressure: str
    opportunity_key: str
    candidate_id: str
    boundary_event_key: str
    snapshot_time: str

    @property
    def requires_exit(self) -> bool:
        return bool(
            self.should_exit_signal
            or self.management_posture == "EXIT"
            or self.stage in _EXIT_STAGES
            or self.status in _TERMINAL_STATUSES
            or self.lifecycle_trade_action in _EXIT_TRADE_ACTIONS
        )

    @property
    def is_defensive(self) -> bool:
        return self.management_posture == "CAUTION" or self.stage in {
            "PROTECT",
            "TRANSITION",
            "WEAKENING",
        }

    @property
    def is_strengthening(self) -> bool:
        return bool(
            self.management_posture == "STRENGTHEN"
            and self.stage in {"ACTIVE", "EXPAND"}
            and not self.requires_exit
        )

    @classmethod
    def from_signal(cls, signal: Any) -> "AuctionTradeSignalContext":
        if signal is None:
            raise ValueError("source signal is required")

        signal_id = _required_attribute_text(signal, "signal_id")
        orm_side = _enum_text(_required_attribute(signal, "side"), "signal.side")
        orm_setup = _enum_text(_required_attribute(signal, "setup"), "signal.setup")
        orm_stage = _enum_text(_required_attribute(signal, "stage"), "signal.stage")
        orm_status = _enum_text(_required_attribute(signal, "status"), "signal.status")

        raw_meta = _required_attribute(signal, "meta_json")
        if not isinstance(raw_meta, Mapping):
            raise TypeError("signal.meta_json must be an object")
        meta: Mapping[str, Any] = raw_meta

        downstream = _required_mapping(meta, "downstream_contract", "signal.meta_json")
        signal_block = _required_mapping(meta, "signal", "signal.meta_json")
        lifecycle = _required_mapping(meta, "lifecycle", "signal.meta_json")
        evidence = _required_mapping(meta, "active_signal_evidence", "signal.meta_json")
        setup_levels = _required_mapping(meta, "setup_levels", "signal.meta_json")
        identity = _required_mapping(meta, "auction_signal", "signal.meta_json")

        version = _required_raw_text(downstream, "version", "downstream_contract")
        _assert_equal("downstream contract version", version, CONTRACT_VERSION)
        _assert_equal(
            "signal block contract version",
            _required_raw_text(signal_block, "contract_version", "signal"),
            CONTRACT_VERSION,
        )
        _assert_equal(
            "lifecycle contract version",
            _required_raw_text(lifecycle, "contract_version", "lifecycle"),
            CONTRACT_VERSION,
        )
        _assert_equal(
            "active evidence contract version",
            _required_raw_text(evidence, "contract_version", "active_signal_evidence"),
            CONTRACT_VERSION,
        )
        _assert_equal(
            "setup levels contract version",
            _required_raw_text(setup_levels, "contract_version", "setup_levels"),
            CONTRACT_VERSION,
        )

        side = _required_text(signal_block, "side", "signal")
        setup_family = _required_text(signal_block, "setup_label", "signal")
        stage = _required_text(signal_block, "stage", "signal")
        signal_action = _required_text(signal_block, "signal_action", "signal")
        signal_state = _required_text(signal_block, "signal_state", "signal")
        lifecycle_reason = _required_raw_text(signal_block, "signal_reason", "signal")

        lifecycle_stage = _required_text(lifecycle, "stage", "lifecycle")
        lifecycle_action = _required_text(lifecycle, "signal_action", "lifecycle")
        lifecycle_state = _required_text(lifecycle, "signal_state", "lifecycle")
        lifecycle_reason_2 = _required_raw_text(lifecycle, "signal_reason", "lifecycle")
        trade_action = _required_text(lifecycle, "trade_action", "lifecycle")

        management_posture = _required_text(
            evidence,
            "active_evidence_action",
            "active_signal_evidence",
        )
        evidence_action_alias = _required_text(
            evidence,
            "evidence_action",
            "active_signal_evidence",
        )
        management_reason = _required_raw_text(
            evidence,
            "reason_code",
            "active_signal_evidence",
        )
        evidence_stage = _required_text(evidence, "stage", "active_signal_evidence")
        evidence_side = _required_text(evidence, "active_side", "active_signal_evidence")
        evidence_status = _required_text(evidence, "signal_status", "active_signal_evidence")
        auction_action = _required_text(evidence, "auction_action", "active_signal_evidence")
        auction_state = _required_text(evidence, "auction_state", "active_signal_evidence")
        directional_alignment = _required_text(
            evidence,
            "directional_alignment",
            "active_signal_evidence",
        )
        target_expansion_allowed = _required_bool(
            evidence,
            "target_expansion_allowed",
            "active_signal_evidence",
        )
        should_exit = _required_bool(
            evidence,
            "should_exit_signal",
            "active_signal_evidence",
        )
        trail_mode = _required_text(evidence, "trail_mode", "active_signal_evidence")
        exit_pressure = _required_text(evidence, "exit_pressure", "active_signal_evidence")
        snapshot_time = _required_datetime_text(
            evidence,
            "snapshot_time",
            "active_signal_evidence",
        )

        opportunity_key = _required_raw_text(setup_levels, "opportunity_key", "setup_levels")
        candidate_id = _required_raw_text(setup_levels, "candidate_id", "setup_levels")
        boundary_event_key = _required_raw_text(
            setup_levels,
            "boundary_event_key",
            "setup_levels",
        )
        setup_subtype = _required_text(setup_levels, "setup_subtype", "setup_levels")

        _assert_equal("ORM side", side, orm_side)
        _assert_equal("ORM setup", setup_family, orm_setup)
        _assert_equal("ORM stage", stage, orm_stage)
        _assert_equal("signal/lifecycle stage", stage, lifecycle_stage)
        _assert_equal("signal/evidence stage", stage, evidence_stage)
        _assert_equal("signal/lifecycle action", signal_action, lifecycle_action)
        _assert_equal("signal/lifecycle state", signal_state, lifecycle_state)
        _assert_equal("signal/lifecycle reason", lifecycle_reason, lifecycle_reason_2)
        _assert_equal("signal/evidence side", side, evidence_side)
        _assert_equal("ORM/evidence status", orm_status, evidence_status)
        _assert_equal("evidence action alias", management_posture, evidence_action_alias)
        _assert_equal(
            "setup levels/identity opportunity",
            opportunity_key,
            _required_raw_text(identity, "opportunity_key", "auction_signal"),
        )
        _assert_equal(
            "setup levels/identity candidate",
            candidate_id,
            _required_raw_text(identity, "candidate_id", "auction_signal"),
        )
        _assert_equal(
            "setup levels/identity boundary",
            boundary_event_key,
            _required_raw_text(identity, "boundary_event_key", "auction_signal"),
        )
        _assert_equal(
            "setup levels/identity setup",
            setup_family,
            _required_text(identity, "setup_family", "auction_signal"),
        )
        _assert_equal(
            "setup levels/identity subtype",
            setup_subtype,
            _required_text(identity, "setup_subtype", "auction_signal"),
        )
        _assert_equal(
            "setup levels/identity side",
            side,
            _required_text(identity, "side", "auction_signal"),
        )
        _assert_equal(
            "active evidence opportunity",
            opportunity_key,
            _required_raw_text(
                evidence,
                "active_opportunity_key",
                "active_signal_evidence",
            ),
        )
        _assert_equal(
            "active evidence candidate",
            candidate_id,
            _required_raw_text(
                evidence,
                "active_candidate_id",
                "active_signal_evidence",
            ),
        )
        _assert_equal(
            "active evidence boundary",
            boundary_event_key,
            _required_raw_text(
                evidence,
                "active_boundary_event_key",
                "active_signal_evidence",
            ),
        )
        _assert_equal(
            "active evidence setup",
            setup_family,
            _required_text(
                evidence,
                "active_setup_label",
                "active_signal_evidence",
            ),
        )
        _assert_equal(
            "active evidence subtype",
            setup_subtype,
            _required_text(
                evidence,
                "active_setup_subtype",
                "active_signal_evidence",
            ),
        )

        if stage not in _ALLOWED_STAGES:
            raise ValueError(f"unsupported signal stage: {stage}")
        if orm_status not in _ALLOWED_STATUSES:
            raise ValueError(f"unsupported signal status: {orm_status}")
        if management_posture not in _ALLOWED_MANAGEMENT_POSTURES:
            raise ValueError(f"unsupported management posture: {management_posture}")
        if trade_action not in _ALLOWED_TRADE_ACTIONS:
            raise ValueError(f"unsupported lifecycle trade action: {trade_action}")

        derived_exit = bool(
            management_posture == "EXIT"
            or stage in _EXIT_STAGES
            or orm_status in _TERMINAL_STATUSES
            or trade_action in _EXIT_TRADE_ACTIONS
        )
        if should_exit != derived_exit:
            raise ValueError(
                "active_signal_evidence.should_exit_signal is inconsistent with "
                "stage/status/posture/trade_action"
            )
        if target_expansion_allowed and not (
            management_posture == "STRENGTHEN" and stage in {"ACTIVE", "EXPAND"}
        ):
            raise ValueError(
                "target_expansion_allowed requires STRENGTHEN with ACTIVE/EXPAND"
            )

        return cls(
            signal_id=signal_id,
            contract_version=version,
            side=side,
            setup_family=setup_family,
            setup_subtype=setup_subtype,
            stage=stage,
            status=orm_status,
            signal_action=signal_action,
            signal_state=signal_state,
            lifecycle_trade_action=trade_action,
            management_posture=management_posture,
            lifecycle_reason=lifecycle_reason,
            management_reason_code=management_reason,
            auction_action=auction_action,
            auction_state=auction_state,
            directional_alignment=directional_alignment,
            target_expansion_allowed=target_expansion_allowed,
            should_exit_signal=should_exit,
            trail_mode=trail_mode,
            exit_pressure=exit_pressure,
            opportunity_key=opportunity_key,
            candidate_id=candidate_id,
            boundary_event_key=boundary_event_key,
            snapshot_time=snapshot_time,
        )


def _required_attribute(obj: Any, name: str) -> Any:
    if not hasattr(obj, name):
        raise AttributeError(f"signal.{name} is required")
    value = getattr(obj, name)
    if value is None:
        raise ValueError(f"signal.{name} is required")
    return value


def _required_attribute_text(obj: Any, name: str) -> str:
    value = _required_attribute(obj, name)
    text = str(value).strip()
    if not text:
        raise ValueError(f"signal.{name} is required")
    return text


__all__ = ["AuctionTradeSignalContext", "CONTRACT_VERSION"]
