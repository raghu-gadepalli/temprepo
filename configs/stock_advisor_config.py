from __future__ import annotations

from typing import List
from pydantic import BaseModel, Field


class StockAdvisorConfig(BaseModel):
    """Stock/day context advisor configuration.

    StockAdvisor is not a setup evaluator.  It evaluates day-so-far stock
    behaviour and emits family/direction ALLOW / WATCH / BLOCK decisions.
    SignalGenerator may enforce those decisions when hard_gate_enabled=True.
    """

    enabled: bool = True
    mode: str = "ENFORCE_ALLOW_ONLY"

    # Production/replay policy: only the exact setup-family + side alignment
    # ALLOW may remain CREATE-capable. WATCH is deferred and BLOCK vetoes the
    # current CREATE candidate.
    hard_gate_enabled: bool = True
    block_on_stock_skip: bool = True
    block_on_setup_block: bool = True
    allow_setup_watch: bool = False

    # Narrow family-specific exception: a price-action-confirmed exhaustion
    # reversal may pass an exact setup-side WATCH only when Advisor itself gives
    # a very high score and its reason explicitly says the mean-reversion context
    # is supported. BLOCK still vetoes. General WATCH setups, including accepted
    # breakout, remain deferred.
    confirmed_exhaustion_watch_override_enabled: bool = True
    confirmed_exhaustion_watch_min_score: float = 90.0
    confirmed_exhaustion_watch_reason_terms: List[str] = Field(default_factory=lambda: [
        "context_allow",
    ])

    # Day-so-far context.  This is intentionally larger than the old short
    # lookback because Advisor should see behaviour across the full session up
    # to the current snapshot.  Metrics still filter to <= current snapshot_time
    # to avoid future leakage in backtests.
    lookback_snapshots: int = 20
    day_context_snapshot_limit: int = 500
    use_day_context_from_db: bool = True
    use_setup_state_context: bool = True
    use_signal_trade_context: bool = True

    # Family labels used by Advisor.  Setup names map to these families in the
    # result contract / signal generator.
    mean_reversion_family: str = "MEAN_REVERSION"
    breakout_family: str = "BREAKOUT"
    failed_breakout_family: str = "FAILED_BREAKOUT"

    exhaustion_setup: str = "EXHAUSTION_REVERSAL"
    accepted_breakout_setup: str = "ACCEPTED_BREAKOUT"
    failed_breakout_setup: str = "FAILED_BREAKOUT"

    # Movement / range filters.
    min_day_range_pct: float = 0.45
    min_recent_range_pct: float = 0.18
    min_recent_move_atr_for_evaluate: float = 0.75
    min_day_range_pct_for_evaluate: float = 0.65

    # Range-position zones.  0.0 = day low, 1.0 = day high.
    middle_zone_low: float = 0.35
    middle_zone_high: float = 0.65
    edge_zone_low: float = 0.20
    edge_zone_high: float = 0.80

    # VWAP / chop context.
    vwap_chop_max_gap_pct: float = 0.18
    vwap_chop_max_recent_range_pct: float = 0.28
    vwap_chop_cross_count: int = 6
    vwap_mixed_cross_count: int = 3
    vwap_acceptance_ratio: float = 0.70

    # Day-context thresholds.
    atr_contracting_pct: float = -20.0
    atr_expanding_pct: float = 20.0
    context_flip_block_count: int = 10
    context_flip_watch_count: int = 6
    range_growth_stalled_pct: float = 0.03
    range_growth_continuing_pct: float = 0.10
    post_spike_day_range_pct: float = 0.90
    post_spike_recent_range_pct: float = 0.25

    # Trend persistence.
    persistent_move_30m_atr: float = 1.00
    persistent_move_60m_atr: float = 1.50
    developing_move_30m_atr: float = 0.50

    # Prior attempt / follow-through context.
    no_mfe_signal_pct: float = 0.10
    fast_invalid_mfe_pct: float = 0.15
    failed_attempt_block_count: int = 3
    no_mfe_block_count: int = 3
    fast_invalid_block_count: int = 2

    # Current extension markers are allowed only for family-level mean-reversion
    # context; setup-specific confirmation remains inside EvidenceEvaluator.
    upper_bb_position_for_extension: float = 0.85
    lower_bb_position_for_extension: float = 0.15
    sell_rsi_for_extension: float = 68.0
    buy_rsi_for_extension: float = 32.0
    min_vwap_gap_pct_for_extension: float = 0.25

    # Family alignment score thresholds. Scores are diagnostic; final decisions
    # should be explained by reason codes, not silently by numeric score alone.
    family_allow_score: float = 70.0
    family_watch_score: float = 45.0

    # Level / structure context.
    level_proximity_atr: float = 0.75
    failed_breakout_allow_status_terms: List[str] = Field(default_factory=lambda: [
        "FAILED", "REABSORBED", "REABSORPTION", "REJECTED", "REJECTION",
    ])
    breakout_watch_status_terms: List[str] = Field(default_factory=lambda: [
        "BREAKOUT", "ATTEMPT", "TEST", "TESTING", "SUSTAIN", "OUTSIDE",
    ])

    # Tradeability score diagnostics.
    w_day_range: float = 0.25
    w_recent_range: float = 0.20
    w_edge_location: float = 0.20
    w_extension: float = 0.20
    w_volume: float = 0.15
    day_range_norm_pct: float = 1.50
    recent_range_norm_pct: float = 0.75
    extension_norm_pct: float = 0.75
    volume_ratio_norm: float = 2.00

    hard_skip_contexts: List[str] = Field(default_factory=lambda: [
        "STALE_OR_BAD_SNAPSHOT",
        "LOW_MOVEMENT",
        "RANGE_MIDDLE_NO_EDGE",
    ])


STOCK_ADVISOR_CONFIG = StockAdvisorConfig()
