from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


STRICT_MODEL_CONFIG = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class EvidenceDataError(ValueError):
    """Raised when Snapshot V1 is missing a required evidence input."""


class EvidenceContribution(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    key: str
    label: str
    side: str
    score: float
    weight: float
    message: str
    data: Dict[str, Any] = Field(default_factory=dict)


class SideEvidence(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    side: str
    opportunity_score: float
    entry_risk: float
    continuation_quality: float
    opportunity_contributions: List[EvidenceContribution] = Field(default_factory=list)
    risk_contributions: List[EvidenceContribution] = Field(default_factory=list)
    continuation_contributions: List[EvidenceContribution] = Field(default_factory=list)


class EvidenceReason(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    code: str
    text: str
    data: Dict[str, Any] = Field(default_factory=dict)


class EvidenceResult(BaseModel):
    model_config = STRICT_MODEL_CONFIG

    symbol: str
    snapshot_time: Optional[datetime]
    engine_name: str
    engine_version: str

    buy: SideEvidence
    sell: SideEvidence

    preferred_side: str
    preferred_opportunity_score: float
    preferred_entry_risk: float
    opposite_pressure: float

    market_condition: str
    strategy: str
    setup_label: str
    primary_pattern: str
    entry_permission: str
    evaluator_state: str
    decision: str
    price_action_confirmed: bool
    price_action_strength: float
    discovered_setups: List[Dict[str, Any]] = Field(default_factory=list)
    confirmed_setups: List[Dict[str, Any]] = Field(default_factory=list)
    supporting_setups: List[Dict[str, Any]] = Field(default_factory=list)
    blocked_by: Optional[str] = None
    risk_flags: List[str] = Field(default_factory=list)

    reason: EvidenceReason
    details: Dict[str, Any] = Field(default_factory=dict)

    def side_evidence(self, side: str) -> SideEvidence:
        side_s = str(side or "").strip().upper()
        if side_s == "BUY":
            return self.buy
        if side_s == "SELL":
            return self.sell
        raise ValueError(f"Unsupported evidence side: {side!r}")
