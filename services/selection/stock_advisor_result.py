from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


BUY = "BUY"
SELL = "SELL"
ALLOW = "ALLOW"
WATCH = "WATCH"
BLOCK = "BLOCK"

SETUP_TO_FAMILY = {
    "EXHAUSTION_REVERSAL": "MEAN_REVERSION",
    "ACCEPTED_BREAKOUT": "BREAKOUT",
    "FAILED_BREAKOUT": "FAILED_BREAKOUT",
}


@dataclass(frozen=True)
class StockAdvisorSetupAlignment:
    setup: str
    alignment: str = BLOCK  # ALLOW / WATCH / BLOCK
    side: str = "ANY"  # BUY / SELL / ANY
    score: float = 0.0
    reason_code: str = ""
    reason_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StockAdvisorResult:
    symbol: str
    snapshot_time: str
    decision: str
    regime: str
    tradeability_score: float

    # Family-oriented output.  These are the primary Advisor decisions.
    family_alignment: Dict[str, StockAdvisorSetupAlignment] = field(default_factory=dict)

    stock_context: str = "UNKNOWN"
    volatility_context: str = "UNKNOWN"
    vwap_context: str = "UNKNOWN"
    trend_context: str = "UNKNOWN"
    range_context: str = "UNKNOWN"
    chop_context: str = "UNKNOWN"
    attempt_context: str = "UNKNOWN"
    preferred_direction: str = "NEUTRAL"
    avoid_direction: str = "NEUTRAL"

    eligible_setups: List[str] = field(default_factory=list)
    watch_setups: List[str] = field(default_factory=list)
    blocked_setups: List[str] = field(default_factory=list)

    # Backward-compatible fields for existing reports.  These now mirror the
    # family decisions through setup->family mapping; they are not independently
    # evaluated setup rules.
    setup_alignment: Dict[str, StockAdvisorSetupAlignment] = field(default_factory=dict)
    side_setup_alignment: Dict[str, StockAdvisorSetupAlignment] = field(default_factory=dict)

    reason_code: str = ""
    reason_text: str = ""
    reason_codes: List[str] = field(default_factory=list)
    features: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["eligible_setups"] = ",".join(self.eligible_setups or [])
        data["watch_setups"] = ",".join(self.watch_setups or [])
        data["blocked_setups"] = ",".join(self.blocked_setups or [])
        data["reason_codes"] = ",".join(self.reason_codes or [])

        for family_side, alignment in (self.family_alignment or {}).items():
            key = str(family_side or "").strip().lower()
            if not key:
                continue
            data[f"{key}_alignment"] = alignment.alignment
            data[f"{key}_advisor_score"] = round(float(alignment.score or 0.0), 2)
            data[f"{key}_reason_code"] = alignment.reason_code
            data[f"{key}_reason_text"] = alignment.reason_text

        for setup, alignment in (self.setup_alignment or {}).items():
            key = str(setup or "").strip().lower()
            if not key:
                continue
            data[f"{key}_alignment"] = alignment.alignment
            data[f"{key}_advisor_score"] = round(float(alignment.score or 0.0), 2)
            data[f"{key}_reason_code"] = alignment.reason_code
            data[f"{key}_reason_text"] = alignment.reason_text

        for setup_side, alignment in (self.side_setup_alignment or {}).items():
            key = str(setup_side or "").strip().lower()
            if not key:
                continue
            data[f"{key}_alignment"] = alignment.alignment
            data[f"{key}_advisor_score"] = round(float(alignment.score or 0.0), 2)
            data[f"{key}_reason_code"] = alignment.reason_code
            data[f"{key}_reason_text"] = alignment.reason_text

        for key, value in (self.features or {}).items():
            data[key] = value
        return data

    def family_for_setup(self, setup: str) -> str:
        setup_s = str(setup or "").strip().upper()
        return SETUP_TO_FAMILY.get(setup_s, setup_s or "UNKNOWN")

    def alignment_for(self, setup: str, side: str) -> StockAdvisorSetupAlignment:
        """Return family+side alignment for a setup.

        SignalGenerator passes actual setup labels.  Advisor maps them to a
        broader family so it remains a stock/day context layer rather than an
        additional setup-specific evaluator.
        """
        setup_s = str(setup or "").strip().upper()
        side_s = str(side or "").strip().upper()
        family = self.family_for_setup(setup_s)
        if family and side_s:
            key = f"{family}_{side_s}"
            found = (self.family_alignment or {}).get(key)
            if found is not None:
                return found
            found = (self.side_setup_alignment or {}).get(f"{setup_s}_{side_s}")
            if found is not None:
                return found
        return StockAdvisorSetupAlignment(
            setup=family or setup_s or "UNKNOWN",
            side=side_s or "ANY",
            alignment=BLOCK,
            score=0.0,
            reason_code="missing_advisor_family_alignment",
            reason_text=f"StockAdvisor did not return family alignment for setup={setup_s or 'UNKNOWN'} side={side_s or 'UNKNOWN'}.",
        )

    def is_setup_allowed(self, setup: str, side: str, *, allow_watch: bool = False) -> bool:
        alignment = self.alignment_for(setup, side).alignment
        if alignment == ALLOW:
            return True
        if alignment == WATCH and allow_watch:
            return True
        return False

    def to_compact_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "snapshot_time": self.snapshot_time,
            "decision": self.decision,
            "regime": self.regime,
            "tradeability_score": self.tradeability_score,
            "stock_context": self.stock_context,
            "volatility_context": self.volatility_context,
            "vwap_context": self.vwap_context,
            "trend_context": self.trend_context,
            "range_context": self.range_context,
            "chop_context": self.chop_context,
            "attempt_context": self.attempt_context,
            "preferred_direction": self.preferred_direction,
            "avoid_direction": self.avoid_direction,
            "eligible_setups": list(self.eligible_setups or []),
            "watch_setups": list(self.watch_setups or []),
            "blocked_setups": list(self.blocked_setups or []),
            "reason_code": self.reason_code,
            "reason_text": self.reason_text,
            "reason_codes": list(self.reason_codes or []),
            "family_alignment": {str(k): v.to_dict() for k, v in (self.family_alignment or {}).items()},
            "setup_alignment": {str(k): v.to_dict() for k, v in (self.setup_alignment or {}).items()},
            "side_setup_alignment": {str(k): v.to_dict() for k, v in (self.side_setup_alignment or {}).items()},
            "features": dict(self.features or {}),
        }


@dataclass(frozen=True)
class StockAdvisorFeatures:
    symbol: str
    snapshot_time: str
    close: Optional[float] = None
    day_open: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    day_range_pct: Optional[float] = None
    range_position: Optional[float] = None
    recent_range_pct: Optional[float] = None
    recent_move_pct: Optional[float] = None
    recent_move_atr: Optional[float] = None
    move_30m_atr: Optional[float] = None
    move_60m_atr: Optional[float] = None
    vwap_gap_pct: Optional[float] = None
    vwap_side: str = "UNKNOWN"
    bb_position: Optional[float] = None
    bb_zone: str = "UNKNOWN"
    rsi: Optional[float] = None
    rsi_zone: str = "NA"
    atr: Optional[float] = None
    atr_pct: Optional[float] = None
    volume_ratio: Optional[float] = None
    hma_state: str = "UNKNOWN"
    hma_strength: str = "UNKNOWN"
    structure_state: str = "UNKNOWN"
    structure_side: str = "NEUTRAL"
    breakout_status: str = "NONE"
    breakout_side: str = "NEUTRAL"
    nearest_level_type: str = "NONE"
    nearest_level_distance_atr: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
