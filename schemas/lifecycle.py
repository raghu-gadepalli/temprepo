from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from enums.enums import LifecycleStage, LifecycleSide, LifecycleQuality, SignalAction, SetupType, SetupState


class LifecycleReason(BaseModel):
    key: str
    message: str
    weight: float = 0.0
    data: Dict[str, Any] = Field(default_factory=dict)


class LifecycleConfidenceFactor(BaseModel):
    key: str
    label: str
    score: float = 0.0
    weight: float = 0.0
    contribution: float = 0.0
    direction: str = "NEUTRAL"
    message: str = ""
    data: Dict[str, Any] = Field(default_factory=dict)


class AuctionContext(BaseModel):
    auction_state: str = "UNKNOWN"
    auction_side: str = "NONE"
    structure_state: str = "UNKNOWN"
    structure_side: str = "NONE"
    structure_count: int = 0
    breakout_status: str = "NONE"
    breakout_side: str = "NEUTRAL"
    session_phase: str = "UNKNOWN"
    range_width_pct: Optional[float] = None
    flip_count_today: int = 0
    compression: bool = False
    accepted_range: Dict[str, Any] = Field(default_factory=dict)
    raw_range: Dict[str, Any] = Field(default_factory=dict)
    meta: Dict[str, Any] = Field(default_factory=dict)


class EvidenceItem(BaseModel):
    key: str
    label: str
    direction: str = "NEUTRAL"  # POSITIVE / NEGATIVE / NEUTRAL
    score: float = 50.0
    weight: float = 0.0
    message: str = ""
    data: Dict[str, Any] = Field(default_factory=dict)


class EvidenceSet(BaseModel):
    items: List[EvidenceItem] = Field(default_factory=list)

    def add(self, item: EvidenceItem) -> None:
        self.items.append(item)

    @property
    def positives(self) -> List[EvidenceItem]:
        return [x for x in self.items if x.direction == "POSITIVE"]

    @property
    def negatives(self) -> List[EvidenceItem]:
        return [x for x in self.items if x.direction == "NEGATIVE"]

    def weighted_score(self) -> float:
        total_weight = sum(abs(x.weight) for x in self.items) or 1.0
        raw = sum(x.score * x.weight for x in self.items)
        return max(0.0, min(100.0, raw / total_weight))


class LifecycleResult(BaseModel):
    """Canonical output from lifecycle evaluation."""

    model_config = {"extra": "forbid"}

    symbol: str
    snapshot_time: Optional[datetime] = None
    lifecycle: str = "DEFAULT"
    lifecycle_version: str = "AUCTION_LIFECYCLE_V1"

    stage: LifecycleStage = LifecycleStage.TRANSITION
    side: LifecycleSide = LifecycleSide.NONE
    quality: LifecycleQuality = LifecycleQuality.LOW
    confidence: float = 0.0

    # Setup explainability. These describe why the signal exists; stage remains
    # the generic operational lifecycle used by trade management.
    setup_type: SetupType = SetupType.NONE
    initiated_setup_state: SetupState = SetupState.NONE
    current_setup_state: SetupState = SetupState.NONE
    setup_state: SetupState = SetupState.NONE

    signal_action: SignalAction = SignalAction.WATCH
    signal_state: str = "WATCH"
    signal_reason: str = ""

    structure_state: Optional[str] = None
    structure_side: Optional[str] = None
    structure_count: Optional[int] = None
    structure_raw_state: Optional[str] = None
    structure_raw_side: Optional[str] = None
    structure_candidate_state: Optional[str] = None
    structure_candidate_count: Optional[int] = None
    structure_flip_count_today: Optional[int] = None

    hma_state: Optional[str] = None
    hma_strength: Optional[str] = None
    hma_state_count: Optional[int] = None
    hma_strength_count: Optional[int] = None

    vwap_side: Optional[str] = None
    vwap_gap_pct: Optional[float] = None
    rsi_zone: Optional[str] = None
    rsi_value: Optional[float] = None
    adx_band: Optional[str] = None
    adx_value: Optional[float] = None
    atr_band: Optional[str] = None
    atr_value: Optional[float] = None
    bollinger_zone: Optional[str] = None
    bollinger_position: Optional[float] = None
    bollinger_width: Optional[float] = None
    volume_band: Optional[str] = None
    bar_rvol: Optional[float] = None
    today_vs_prev_volume_ratio: Optional[float] = None

    orb_status: Optional[str] = None
    pdh_pdl_status: Optional[str] = None
    recent15_status: Optional[str] = None
    swing_status: Optional[str] = None
    active_anchor: Optional[str] = None
    range_width_pct: Optional[float] = None

    reasons: List[LifecycleReason] = Field(default_factory=list)
    supports: List[LifecycleReason] = Field(default_factory=list)
    warnings: List[LifecycleReason] = Field(default_factory=list)
    conflicts: List[LifecycleReason] = Field(default_factory=list)
    confidence_factors: List[LifecycleConfidenceFactor] = Field(default_factory=list)
    negative_cluster: List[LifecycleReason] = Field(default_factory=list)
    price_action_context: Dict[str, Any] = Field(default_factory=dict)
    transition_context: Dict[str, Any] = Field(default_factory=dict)
    meta: Dict[str, Any] = Field(default_factory=dict)

    def add_reason(self, key: str, message: str, weight: float = 0.0, data: Optional[Dict[str, Any]] = None) -> None:
        self.reasons.append(LifecycleReason(key=key, message=message, weight=weight, data=data or {}))

    def add_support(self, key: str, message: str, weight: float = 0.0, data: Optional[Dict[str, Any]] = None) -> None:
        item = LifecycleReason(key=key, message=message, weight=weight, data=data or {})
        self.supports.append(item); self.reasons.append(item)

    def add_warning(self, key: str, message: str, weight: float = 0.0, data: Optional[Dict[str, Any]] = None) -> None:
        item = LifecycleReason(key=key, message=message, weight=weight, data=data or {})
        self.warnings.append(item); self.reasons.append(item)

    def add_conflict(self, key: str, message: str, weight: float = 0.0, data: Optional[Dict[str, Any]] = None) -> None:
        item = LifecycleReason(key=key, message=message, weight=weight, data=data or {})
        self.conflicts.append(item); self.reasons.append(item); self.negative_cluster.append(item)

    def add_negative(self, key: str, message: str, weight: float = 0.0, data: Optional[Dict[str, Any]] = None) -> None:
        item = LifecycleReason(key=key, message=message, weight=weight, data=data or {})
        self.negative_cluster.append(item)

    def add_confidence_factor(
        self,
        *,
        key: str,
        label: str,
        score: float,
        weight: float,
        message: str = "",
        direction: str = "NEUTRAL",
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.confidence_factors.append(
            LifecycleConfidenceFactor(
                key=key,
                label=label,
                score=score,
                weight=weight,
                contribution=score * weight,
                direction=direction,
                message=message,
                data=data or {},
            )
        )


    def set_signal(self, action: SignalAction, state: str, reason: str = "") -> None:
        self.signal_action = action
        self.signal_state = str(state or "")
        self.signal_reason = str(reason or "")


    @property
    def is_actionable(self) -> bool:
        return self.signal_action in {SignalAction.CREATE, SignalAction.PROMOTE}

    @property
    def is_terminal(self) -> bool:
        return self.stage in {LifecycleStage.FORCE_EXIT, LifecycleStage.EXIT_BIAS}
