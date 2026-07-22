from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


STRICT_CONFIG = ConfigDict(extra="forbid", frozen=True)


class EvidenceSignalWindowConfig(BaseModel):
    model_config = STRICT_CONFIG

    earliest_fresh_signal_time: str = "09:30:00"
    latest_fresh_signal_time: str = "15:00:00"


class EvidenceDataQualityConfig(BaseModel):
    model_config = STRICT_CONFIG

    required_market_windows: tuple[str, ...] = ("current", "15m", "30m", "60m", "sod")
    required_indicator_groups: tuple[str, ...] = (
        "adx",
        "atr",
        "hma",
        "rsi",
        "volume",
        "bollinger",
    )
    required_indicator_windows: tuple[str, ...] = ("current", "15m", "30m", "60m", "sod")
    required_top_level_blocks: tuple[str, ...] = (
        "bar",
        "levels",
        "indicators",
        "volume",
        "market_windows",
        "indicator_windows",
        "price_action",
        "structure",
        "state_context",
    )
    required_numeric_paths: tuple[str, ...] = (
        "close",
        "bar.open",
        "bar.high",
        "bar.low",
        "bar.close",
        "indicators.atr.value",
        "indicators.rsi.value",
        "indicators.bollinger.position",
        "structure.accepted.range.high",
        "structure.accepted.range.low",
        "market_windows.current.close_position_in_range",
        "market_windows.15m.close_position_in_range",
        "market_windows.30m.close_position_in_range",
        "market_windows.sod.close_position_in_range",
        "market_windows.current.move_atr",
        "market_windows.15m.move_atr",
        "market_windows.30m.move_atr",
        "market_windows.sod.move_atr",
    )




class EvidenceSetupStateConfig(BaseModel):
    model_config = STRICT_CONFIG

    # Persistent pre-entry setup memory. Keep disabled by default so applying
    # this patch has no live behaviour change until the stock_setup_state table
    # is created and the flag is enabled in the target environment.
    enabled: bool = True
    write_enabled: bool = True
    read_watch_enabled: bool = True
    fail_silently: bool = False

    table_name: str = "stock_setup_state"
    watch_state: str = "WATCH"
    confirmed_state: str = "CONFIRMED"
    confirmed_pending_state: str = "CONFIRMED_PENDING"
    confirmed_deferred_state: str = "CONFIRMED_DEFERRED"

    # Terminal/current-state lifecycle for one stock/setup/side/day.
    # CONSUMED means a real signal row was created from this setup state.
    # INVALIDATED means the watched extreme was broken before confirmation.
    # EXPIRED means the watch/pending window became stale.
    # COOLDOWN means a consumed/terminal setup tried to create again without
    # first making a fresh side-specific extreme reset.
    consumed_state: str = "CONSUMED"
    invalidated_state: str = "INVALIDATED"
    expired_state: str = "EXPIRED"
    cooldown_state: str = "COOLDOWN"

    # Legacy labels retained so older audit/replay payloads can still be read.
    signal_created_state: str = "SIGNAL_CREATED"
    dropped_state: str = "DROPPED"
    reset_state: str = "RESET_ON_NEW_EXTREME"

    # Churn protection. A terminal same-side setup can create again only after
    # price makes a fresh adverse extreme beyond the consumed/invalidated
    # reference level. The tolerance reuses the setup-specific ATR reset config.
    block_terminal_without_fresh_extreme: bool = True
    terminal_states: tuple[str, ...] = ("CONSUMED", "INVALIDATED", "EXPIRED", "COOLDOWN", "SIGNAL_CREATED", "DROPPED")
    consumed_reason_code: str = "SETUP_STATE_CONSUMED"
    invalidated_reason_code: str = "EXHAUSTION_REVERSAL_SETUP_STATE_INVALIDATED"
    expired_reason_code: str = "EXHAUSTION_REVERSAL_SETUP_STATE_EXPIRED"
    cooldown_reason_code: str = "EXHAUSTION_REVERSAL_SETUP_STATE_COOLDOWN"

    # Same-side reset policy after a setup has already been consumed or made
    # terminal. A marginal new high/low is not enough; the stock must first cool
    # out of the RSI/BB exhaustion zone and then make a meaningful fresh adverse
    # extreme. This targets July-9 churn such as BOSCHLTD/LTF repeated same-side
    # SELL attempts.
    same_side_cooldown_minutes: float = 30.0
    same_side_reset_requires_cooling: bool = True
    same_side_reset_cooling_lookback_bars: int = 20
    same_side_reset_fresh_extreme_buffer_atr: float = 1.00
    same_side_reset_code: str = "EXHAUSTION_REVERSAL_SAME_SIDE_RESET_ALLOWED"
    same_side_no_reset_code: str = "CREATE_BLOCKED_SAME_SIDE_NO_FRESH_RESET"

    # Opposite confirmed exhaustion can exit an active accepted-breakout signal,
    # but same-pass reversal remains disabled. Retain that confirmed exhaustion
    # for one later completed-candle pass so it can be created only if it remains
    # timely and passes the exact StockAdvisor setup-side gate.
    confirmed_pending_enabled: bool = True
    confirmed_pending_valid_bars: int = 1


class EvidenceInstrumentProfileConfig(BaseModel):
    model_config = STRICT_CONFIG

    enabled: bool = False
    index_symbols: tuple[str, ...] = ("NIFTY 50", "NIFTY BANK")
    index_watch_extreme_lookback_bars: int = 10
    index_watch_extreme_valid_minutes: float = 30.5

class EvidenceContributionConfig(BaseModel):
    model_config = STRICT_CONFIG

    weight: float
    confirm_score: float
    oppose_score: float
    neutral_score: float


class EvidenceOpportunityConfig(BaseModel):
    model_config = STRICT_CONFIG

    # Kept for compatibility with existing diagnostics/active-signal code.
    # V2 fresh-entry creation is setup/price-action gated, not weight-mixer gated.
    structure_accepted_breakout: EvidenceContributionConfig = Field(
        default_factory=lambda: EvidenceContributionConfig(weight=0.32, confirm_score=92.0, oppose_score=18.0, neutral_score=50.0)
    )
    structure_breakout_testing: EvidenceContributionConfig = Field(
        default_factory=lambda: EvidenceContributionConfig(weight=0.22, confirm_score=68.0, oppose_score=34.0, neutral_score=50.0)
    )
    price_slope: EvidenceContributionConfig = Field(
        default_factory=lambda: EvidenceContributionConfig(weight=0.16, confirm_score=76.0, oppose_score=30.0, neutral_score=50.0)
    )
    hma_state: EvidenceContributionConfig = Field(
        default_factory=lambda: EvidenceContributionConfig(weight=0.12, confirm_score=70.0, oppose_score=35.0, neutral_score=50.0)
    )
    vwap_side: EvidenceContributionConfig = Field(
        default_factory=lambda: EvidenceContributionConfig(weight=0.10, confirm_score=66.0, oppose_score=40.0, neutral_score=50.0)
    )
    window_move: EvidenceContributionConfig = Field(
        default_factory=lambda: EvidenceContributionConfig(weight=0.10, confirm_score=68.0, oppose_score=36.0, neutral_score=50.0)
    )
    reversal_exhaustion: EvidenceContributionConfig = Field(
        default_factory=lambda: EvidenceContributionConfig(weight=0.24, confirm_score=82.0, oppose_score=30.0, neutral_score=50.0)
    )
    participation: EvidenceContributionConfig = Field(
        default_factory=lambda: EvidenceContributionConfig(weight=0.08, confirm_score=68.0, oppose_score=42.0, neutral_score=50.0)
    )

    accepted_breakout_statuses: tuple[str, ...] = ("ACCEPTED_BREAKOUT",)
    developing_breakout_statuses: tuple[str, ...] = ("BREAKOUT_ATTEMPT", "BREAKOUT_TESTING")
    buy_slope_states: tuple[str, ...] = ("TURNING_UP", "UP_ACCELERATING", "UP_SLOWING")
    sell_slope_states: tuple[str, ...] = ("TURNING_DOWN", "DOWN_ACCELERATING", "DOWN_SLOWING")
    buy_hma_states: tuple[str, ...] = ("BUY", "STRONG_BUY", "MEDIUM_BUY", "UP_ACCELERATING", "UP_SLOWING", "TURNING_UP")
    sell_hma_states: tuple[str, ...] = ("SELL", "STRONG_SELL", "MEDIUM_SELL", "DOWN_ACCELERATING", "DOWN_SLOWING", "TURNING_DOWN")
    buy_vwap_sides: tuple[str, ...] = ("ABOVE", "ABOVE_VWAP", "BULLISH")
    sell_vwap_sides: tuple[str, ...] = ("BELOW", "BELOW_VWAP", "BEARISH")
    strong_participation_bands: tuple[str, ...] = ("STRONG", "HIGH", "VERY_HIGH")
    weak_participation_bands: tuple[str, ...] = ("WEAK", "LOW", "VERY_LOW")
    overbought_rsi: float = 75.0
    oversold_rsi: float = 25.0
    upper_bollinger_position: float = 0.90
    lower_bollinger_position: float = 0.10
    directional_window_names: tuple[str, ...] = ("15m", "30m", "60m")
    directional_window_min_abs_move_atr: float = 0.20


class EvidenceRiskConfig(BaseModel):
    model_config = STRICT_CONFIG

    vwap_extension_weight: float = 0.28
    sod_extension_weight: float = 0.22
    rsi_exhaustion_weight: float = 0.20
    opposite_pressure_weight: float = 0.30

    neutral_risk_score: float = 45.0
    low_risk_score: float = 25.0
    medium_risk_score: float = 55.0
    high_risk_score: float = 82.0

    moderate_vwap_distance_atr: float = 0.80
    high_vwap_distance_atr: float = 1.50
    moderate_sod_move_atr: float = 1.20
    high_sod_move_atr: float = 2.00
    buy_rsi_high_risk: float = 72.0
    sell_rsi_high_risk: float = 28.0
    opposite_pressure_high: float = 68.0
    opposite_pressure_medium: float = 55.0


class EvidenceActionConfig(BaseModel):
    model_config = STRICT_CONFIG

    create_min_opportunity: float = 68.0
    create_max_entry_risk: float = 58.0
    watch_min_opportunity: float = 55.0

    # Conservative Phase-A orchestration default: opposite setup evidence can
    # weaken/downgrade or invalidate by setup SL, but it must not auto-reverse
    # into a fresh opposite signal until replay validates conflict handling.
    enable_opposite_auto_replace: bool = False
    replace_min_opportunity: float = 74.0
    replace_max_entry_risk: float = 58.0
    replace_min_score_gap: float = 10.0

    hold_min_continuation_quality: float = 52.0
    downgrade_below_continuation_quality: float = 46.0
    invalidate_below_continuation_quality: float = 28.0
    downgrade_opposite_pressure_min: float = 64.0
    invalidate_opposite_pressure_min: float = 82.0

    high_quality_min: float = 75.0
    medium_quality_min: float = 55.0


class EvidencePatternConfig(BaseModel):
    model_config = STRICT_CONFIG

    strategy_none: str = "NONE"
    strategy_breakout: str = "BREAKOUT"
    strategy_contra: str = "CONTRA"
    strategy_reversal: str = "REVERSAL"
    strategy_continuation: str = "CONTINUATION"
    strategy_mixed: str = "MIXED"

    setup_no_action: str = "NO_ACTION"
    setup_exhaustion_reversal: str = "EXHAUSTION_REVERSAL"
    setup_accepted_breakout: str = "ACCEPTED_BREAKOUT"
    setup_developing_breakout: str = "DEVELOPING_BREAKOUT"
    setup_failed_breakout: str = "FAILED_BREAKOUT"
    setup_range_reabsorption: str = "RANGE_REABSORPTION"
    setup_contra_rejection: str = "CONTRA_REJECTION"
    setup_pullback_continuation: str = "PULLBACK_CONTINUATION"
    setup_compression_release: str = "COMPRESSION_RELEASE"
    setup_mixed_evidence: str = "MIXED_EVIDENCE"


class EvidenceReasonConfig(BaseModel):
    model_config = STRICT_CONFIG

    no_entry_code: str = "evidence_no_action"
    defer_code: str = "evidence_defer"
    create_code: str = "evidence_create"
    hold_code: str = "evidence_hold"
    downgrade_code: str = "evidence_downgrade"
    invalidate_code: str = "evidence_invalidate"
    replace_code: str = "evidence_replace"
    data_error_code: str = "evidence_data_error"

    setup_not_confirmed_code: str = "setup_discovered_but_price_action_not_confirmed"
    no_setup_code: str = "no_enabled_setup_discovered"
    blocked_by_location_code: str = "entry_deferred_by_setup_location_filter"
    side_conflict_code: str = "side_conflict_strengths_too_close"
    reversal_invalidation_code: str = "reversal_candle_invalidation_exit"
    reversal_price_action_failed_code: str = "reversal_price_action_no_longer_supporting"

    # Pair-specific continuation/reversal arbitration. Numeric priority still
    # orders ordinary candidates, but accepted breakout may not overrule a
    # current opposing exhaustion WATCH/confirmation merely because the latest
    # continuation candle has the larger generic strength score.
    exhaustion_overrides_breakout_code: str = "CONFIRMED_EXHAUSTION_OVERRIDES_ACCEPTED_BREAKOUT"
    breakout_deferred_by_exhaustion_watch_code: str = "ACCEPTED_BREAKOUT_DEFERRED_BY_OPPOSING_EXHAUSTION_WATCH"
    breakout_blocked_by_confirmed_exhaustion_code: str = "ACCEPTED_BREAKOUT_BLOCKED_BY_CONFIRMED_EXHAUSTION"
    active_breakout_exit_confirmed_exhaustion_code: str = "ACTIVE_ACCEPTED_BREAKOUT_EXITED_OPPOSITE_EXHAUSTION_CONFIRMED"
    active_exhaustion_overrides_breakout_code: str = "ACTIVE_EXHAUSTION_RETAINS_PRIORITY_OVER_OPPOSING_ACCEPTED_BREAKOUT"
    exhaustion_confirmed_pending_code: str = "EXHAUSTION_REVERSAL_CONFIRMED_PENDING_NEXT_PASS"


class EvidenceSetupRuleConfig(BaseModel):
    model_config = STRICT_CONFIG

    enabled: bool
    priority: int
    allow_create: bool
    strategy: str


class EvidenceSetupDiscoveryConfig(BaseModel):
    model_config = STRICT_CONFIG

    setup_rules: dict[str, EvidenceSetupRuleConfig] = Field(
        default_factory=lambda: {
            # Dedicated FAILED_BREAKOUT replay profile. Keep the frozen
            # ACCEPTED_BREAKOUT implementation disabled during this workstream so
            # candidate, lifecycle and MFE/MAE distributions are attributable to
            # FAILED_BREAKOUT alone.
            "FAILED_BREAKOUT": EvidenceSetupRuleConfig(enabled=False, priority=1, allow_create=True, strategy="REVERSAL"),
            "EXHAUSTION_REVERSAL": EvidenceSetupRuleConfig(enabled=False, priority=2, allow_create=True, strategy="REVERSAL"),
            "ACCEPTED_BREAKOUT": EvidenceSetupRuleConfig(enabled=True, priority=3, allow_create=True, strategy="BREAKOUT"),

            # Range reabsorption remains disabled because the current snapshot
            # contract does not emit a distinct reabsorption history separate from
            # FAILED_BREAKOUT.
            "RANGE_REABSORPTION": EvidenceSetupRuleConfig(enabled=False, priority=4, allow_create=False, strategy="REVERSAL"),

            # Do not reintroduce standalone developing/compression/pullback setups.
            # They may support the completed setup families, but cannot CREATE.
            "CONTRA_REJECTION": EvidenceSetupRuleConfig(enabled=False, priority=5, allow_create=False, strategy="CONTRA"),
            "DEVELOPING_BREAKOUT": EvidenceSetupRuleConfig(enabled=False, priority=6, allow_create=False, strategy="BREAKOUT"),
            "PULLBACK_CONTINUATION": EvidenceSetupRuleConfig(enabled=False, priority=7, allow_create=False, strategy="CONTINUATION"),
            "COMPRESSION_RELEASE": EvidenceSetupRuleConfig(enabled=False, priority=8, allow_create=False, strategy="CONTINUATION"),
        }
    )


class EvidencePriceActionConfig(BaseModel):
    model_config = STRICT_CONFIG

    strict_gate_for_create: bool = True
    strength_confirm_min: float = 63.0
    side_conflict_min_strength_gap: float = 15.0

    buy_single_candle_close_position_min: float = 0.62
    sell_single_candle_close_position_max: float = 0.38
    min_single_candle_move_atr: float = 0.05

    # Conflict policy: recent price action remains authoritative. An unresolved
    # opposing exhaustion WATCH defers accepted breakout for another completed
    # candle; a confirmed exhaustion wins the pair-specific conflict. An active
    # accepted breakout exits as soon as opposite exhaustion is confirmed, even
    # when the reversal entry itself is blocked as already consumed/too late.
    exhaustion_watch_defers_opposing_accepted_breakout: bool = True
    confirmed_exhaustion_overrides_accepted_breakout: bool = True
    active_accepted_breakout_exit_on_confirmed_exhaustion: bool = True
    # Once an exhaustion reversal signal is active, an opposing accepted-breakout
    # candidate cannot invalidate it. The reversal remains active until its own
    # lifecycle/invalidation rule fails. This is the active-signal mirror of the
    # fresh-candidate rule that confirmed exhaustion outranks continuation.
    active_exhaustion_overrides_opposing_accepted_breakout: bool = True

    multi_candle_enabled: bool = True
    multi_candle_buy_15m_min_move_atr: float = 0.10
    multi_candle_sell_15m_max_move_atr: float = -0.10
    multi_candle_buy_15m_close_position_min: float = 0.55
    multi_candle_sell_15m_close_position_max: float = 0.45
    single_candle_strength_points: float = 45.0
    multi_candle_strength_points: float = 35.0
    close_position_strength_points: float = 20.0



class EvidenceSetupScoringConfig(BaseModel):
    model_config = STRICT_CONFIG

    # Setup-level diagnostic scores used by EvidenceEvaluator for audit/active-signal
    # context. These are not trade SL/target risk-reward gates. Fresh CREATE remains
    # setup-specific and price-action gated.
    opportunity_score_discovered: float = 60.0
    opportunity_score_confirmed: float = 72.0
    opportunity_score_ready: float = 78.0
    neutral_entry_quality_risk: float = 45.0
    blocked_entry_quality_risk: float = 82.0


class EvidenceExhaustionReversalConfig(BaseModel):
    model_config = STRICT_CONFIG

    buy_rsi_max: float = 35.0
    sell_rsi_min: float = 65.0
    buy_bollinger_position_max: float = 0.18
    sell_bollinger_position_min: float = 0.82
    buy_bollinger_zones: tuple[str, ...] = ("NEAR_LOWER", "BELOW_BAND")
    sell_bollinger_zones: tuple[str, ...] = ("NEAR_UPPER", "ABOVE_BAND")

    buy_sod_position_max: float = 0.35
    sell_sod_position_min: float = 0.65
    buy_30m_position_max: float = 0.40
    sell_30m_position_min: float = 0.60
    buy_min_down_move_atr: float = -0.80
    sell_min_up_move_atr: float = 0.80


    block_buy_if_rsi_min: float = 68.0
    block_sell_if_rsi_max: float = 32.0
    block_buy_bollinger_position_min: float = 0.82
    block_sell_bollinger_position_max: float = 0.18

    vwap_filter_enabled: bool = True
    # For EXHAUSTION_REVERSAL, VWAP is used as a first value/reference magnet,
    # not as a trend indicator.  A fresh BUY exhaustion should still have room
    # back up to VWAP; a fresh SELL exhaustion should still have room back down
    # to VWAP.  If price has already crossed/consumed VWAP, the move may still
    # be tradeable later, but it is no longer a clean exhaustion-reversal entry.
    min_directional_vwap_room_pct: float = 0.25
    min_abs_vwap_gap_pct: float = 0.20  # legacy audit field; directional room is authoritative for exhaustion CREATE

    reversal_atr_buffer: float = 2.00
    risk_model: str = "REVERSAL_CANDLE_ATR"


    # Price-action-only quality gate for single-candle exhaustion CREATE.
    # Weak one-candle rejection remains WATCH/DEFER.  Direct CREATE is allowed
    # only when the candle itself is a large reversal/displacement candle and
    # it has pulled the short-term auction away from the extreme.  This avoids
    # fading a fresh accepted breakout just because one small red/green candle
    # appears near the band.
    single_candle_reclaim_min_move_atr: float = 0.50  # legacy fallback for other setup helpers
    single_candle_strong_reversal_min_move_atr: float = 1.20
    single_candle_buy_15m_position_min: float = 0.35
    single_candle_sell_15m_position_max: float = 0.65

    # WATCH -> CREATE promotion for extreme-location exhaustion.
    # Example: candle N is ABOVE_BAND / RSI-extreme but still closes near the
    # high, so it is only WATCH. Candle N+1 is a large red reversal and breaks
    # the extreme candle low; that should CREATE without requiring the current
    # candle to still be RSI/BB extreme.
    watch_extreme_promotion_enabled: bool = True
    # Keep DISCOVER memory for the last 5 completed 3-minute candles.
    # RSI/BB extreme only creates WATCH memory; it still needs later price action
    # confirmation before a real signal can be created.
    watch_extreme_lookback_bars: int = 5
    watch_extreme_valid_minutes: float = 15.5
    watch_extreme_sell_rsi_min: float = 70.0
    watch_extreme_buy_rsi_max: float = 30.0
    watch_extreme_sell_bollinger_position_min: float = 1.0
    watch_extreme_buy_bollinger_position_max: float = 0.0
    watch_extreme_sell_bollinger_zones: tuple[str, ...] = ("ABOVE_BAND",)
    watch_extreme_buy_bollinger_zones: tuple[str, ...] = ("BELOW_BAND",)
    watch_promotion_min_move_atr: float = 0.75
    watch_promotion_sell_close_position_max: float = 0.25
    watch_promotion_buy_close_position_min: float = 0.75
    # Do not require the confirming candle to break the whole extreme candle low/high.
    # AUBANK-style reversals can confirm by a large red/green candle from the
    # extreme high/low even if the prior extreme candle had a wider wick.
    watch_promotion_require_extreme_candle_break: bool = False
    # Reset stale WATCH memory if a later completed candle extends beyond the
    # watched extreme before confirmation.  A small tolerance avoids resetting
    # on harmless wick/noise differences.
    watch_extreme_reset_tolerance_atr: float = 0.10
    watch_promotion_code: str = "EXHAUSTION_REVERSAL_WATCH_EXTREME_CONFIRMED"
    watch_promotion_weak_single_candle_code: str = "EXHAUSTION_REVERSAL_WATCH_PROMOTION_WEAK_SINGLE_CANDLE"
    # A WATCH promotion may be valid even when the current candle alone is not
    # a large displacement, provided the full watch-window rejection/reclaim is
    # strong enough.  This preserves MUTHOOTFIN-style delayed confirmations while
    # still blocking BIOCON-style small bounces from a falling extreme.
    watch_promotion_strong_window_enabled: bool = True
    watch_promotion_strong_window_min_move_atr: float = 2.0
    watch_promotion_strong_window_min_age_bars: int = 2
    watch_promotion_strong_window_code: str = "EXHAUSTION_REVERSAL_WATCH_STRONG_WINDOW_CONFIRMATION"

    # Early WATCH-relative confirmation. A reversal can be confirmed by the
    # completed sequence away from the watched RSI/BB extreme even when the
    # latest candle alone is a small pause/bounce and therefore fails the generic
    # single-candle confirmation. This is still recent price action: it uses only
    # the original WATCH candle, the current completed candle/window and ATR.
    watch_relative_confirmation_enabled: bool = True
    watch_relative_min_move_from_extreme_atr: float = 0.75
    watch_relative_max_move_from_extreme_atr: float = 1.50
    watch_relative_min_close_displacement_from_watch_close_atr: float = 0.50
    watch_relative_min_extreme_separation_atr: float = 0.25
    watch_relative_sell_15m_position_max: float = 0.80
    watch_relative_buy_15m_position_min: float = 0.20
    watch_relative_min_age_bars: int = 1
    watch_relative_max_age_bars: int = 3
    watch_relative_strength_base: float = 72.0
    watch_relative_strength_per_atr: float = 18.0
    watch_relative_confirmation_code: str = "EXHAUSTION_REVERSAL_WATCH_RELATIVE_REJECTION_CONFIRMED"

    # Entry-quality guard for watched exhaustion. If the confirming candle has
    # already travelled too far from the original WATCH extreme, the first easy
    # mean-reversion move is considered consumed and CREATE is deferred.
    first_move_consumed_guard_enabled: bool = True
    max_confirmation_move_from_watch_atr: float = 1.50
    first_move_consumed_code: str = "CREATE_BLOCKED_FIRST_MOVE_ALREADY_CONSUMED"




class EvidenceFailedBreakoutConfig(BaseModel):
    model_config = STRICT_CONFIG

    discovery_statuses: tuple[str, ...] = ("FAILED_BREAKOUT", "RANGE_REABSORBED")
    min_bars_reclaimed: int = 1
    require_inside_accepted_range: bool = True
    require_price_action_confirmation: bool = True

    # A FAILED_BREAKOUT structure event can disappear from the next snapshot
    # before the opposite-side rejection candle confirms. Retain the most recent
    # real FAILED_BREAKOUT event for five completed 3-minute candles and re-run
    # the same price-action and location filters on each subsequent snapshot.
    # This does not loosen confirmation, inside-range, distance, stretch, or
    # range-quality rules; it only preserves the event long enough to confirm.
    watch_event_enabled: bool = True
    watch_event_statuses: tuple[str, ...] = ("FAILED_BREAKOUT",)
    watch_event_lookback_bars: int = 5
    watch_event_valid_minutes: float = 15.5
    watch_state_reason_code: str = "FAILED_BREAKOUT_EVENT_WATCH"
    watch_confirmed_reason_code: str = "FAILED_BREAKOUT_EVENT_WATCH_CONFIRMED"
    watch_invalidated_reason_code: str = "FAILED_BREAKOUT_EVENT_WATCH_INVALIDATED"
    watch_expired_reason_code: str = "FAILED_BREAKOUT_EVENT_WATCH_EXPIRED"

    # A failed upside breakout becomes a SELL candidate; failed downside becomes BUY.
    # Invalidation is the broken level +/- ATR buffer. If the current candle extreme
    # is farther than the broken level, the farther value is used.
    invalidation_atr_buffer: float = 1.00
    risk_model: str = "FAILED_BREAKOUT_LEVEL_ATR"

    block_entry_if_opposite_exhaustion: bool = True

    # Conservative failed-breakout entry-location gate. A failed breakout should be
    # entered near the reclaim/retest level. If the reclaim has already stretched,
    # DEFER is better than chasing.
    block_buy_bollinger_position_min: float = 0.90
    block_sell_bollinger_position_max: float = 0.10
    block_buy_rsi_min: float = 62.0
    block_sell_rsi_max: float = 38.0
    block_buy_rsi_bollinger_position_min: float = 0.85
    block_sell_rsi_bollinger_position_max: float = 0.15
    block_buy_15m_position_min: float = 0.85
    block_sell_15m_position_max: float = 0.15
    max_entry_distance_from_level_atr: float = 1.00

    # Range-quality filter. Keep only setup-identity/location checks here;
    # trade deployability, targets and reward:R belong downstream.
    min_accepted_range_width_atr: float = 2.50
    single_candle_reclaim_min_move_atr: float = 0.50


class EvidenceRangeReabsorptionConfig(BaseModel):
    model_config = STRICT_CONFIG

    discovery_statuses: tuple[str, ...] = ("RANGE_REABSORBED",)
    min_bars_reclaimed: int = 2
    require_inside_accepted_range: bool = True
    require_price_action_confirmation: bool = True

    invalidation_atr_buffer: float = 0.50
    risk_model: str = "RANGE_REABSORPTION_EXTREME_ATR"

    block_entry_if_opposite_exhaustion: bool = True

    # Range reabsorption is kept audit/WATCH-only for now. It uses the same
    # conservative location and reward metrics as failed breakout so replay can
    # show whether it ever offers a distinct, worthwhile entry.
    block_buy_bollinger_position_min: float = 0.90
    block_sell_bollinger_position_max: float = 0.10
    block_buy_rsi_min: float = 62.0
    block_sell_rsi_max: float = 38.0
    block_buy_rsi_bollinger_position_min: float = 0.85
    block_sell_rsi_bollinger_position_max: float = 0.15
    block_buy_15m_position_min: float = 0.85
    block_sell_15m_position_max: float = 0.15
    max_entry_distance_from_level_atr: float = 0.50
    min_accepted_range_width_atr: float = 2.50
    single_candle_reclaim_min_move_atr: float = 0.50




class EvidenceAcceptedBreakoutConfig(BaseModel):
    model_config = STRICT_CONFIG

    # Snapshot is strategy-neutral. Evidence derives exact-level breakout state
    # from structure.recent_closes and the current ATR. These thresholds therefore
    # belong here, not in snapshot generation.
    attempt_buffer_atr: float = 0.15
    acceptance_buffer_atr: float = 0.25
    reclaim_buffer_atr: float = 0.00
    # Neutral observation threshold. Crossing this offset labels the observation
    # as a possible strong displacement, but CREATE is allowed only when the
    # stricter candle/volume rules below also pass.
    strong_displacement_min_atr: float = 1.00
    strong_displacement_max_entry_distance_atr: float = 2.50

    # Physical boundary aliasing. ORB/PDH/PDL and the qualified accepted range
    # can resolve to the same tradable tick while arriving from different
    # structure sources. Merge such co-located boundaries before setup
    # interpretation so ACCEPTED_BREAKOUT and FAILED_BREAKOUT share one event
    # identity rather than rolling over between aliases.
    coincident_level_tolerance_points: float = 0.05

    # High-selectivity CREATE policy. Ordinary one-close attempts remain visible
    # as BREAKOUT_TESTING/WATCH, but they cannot CREATE. A one-close exception is
    # reserved for a genuine displacement candle with strong participation,
    # decisive close location and enough exact-level clearance.
    generic_early_create_enabled: bool = False
    fixed_level_min_bars_outside: int = 2
    dynamic_range_min_bars_outside: int = 3
    other_level_min_bars_outside: int = 3
    fixed_level_types: tuple[str, ...] = (
        "ORB_HIGH",
        "ORB_LOW",
        "PREVIOUS_DAY_HIGH",
        "PREVIOUS_DAY_LOW",
    )
    dynamic_range_level_types: tuple[str, ...] = (
        "DYNAMIC_RANGE_HIGH",
        "DYNAMIC_RANGE_LOW",
        "ACCEPTED_RANGE_HIGH",
        "ACCEPTED_RANGE_LOW",
    )
    strict_displacement_enabled: bool = True
    # A displacement candle remains valuable evidence, but it may not CREATE on
    # its own. It must accumulate the same completed-close acceptance required
    # for the selected structural level (2 closes for fixed levels, 3 for
    # dynamic accepted ranges). Terminal displacement keeps its stricter
    # persistent pending/retest lifecycle below.
    strict_displacement_immediate_create_enabled: bool = False
    strict_displacement_min_candle_move_atr: float = 1.50
    strict_displacement_min_bar_rvol: float = 2.00
    strict_displacement_min_body_fraction: float = 0.65
    strict_displacement_buy_close_position_min: float = 0.80
    strict_displacement_sell_close_position_max: float = 0.20
    strict_displacement_min_break_distance_atr: float = 0.40
    strict_displacement_room_bypass_enabled: bool = True

    # A newly accepted dynamic range must survive for a short period before its
    # boundary can create a signal. This is deliberately much smaller than a
    # blanket 20-candle range requirement: discovery stays responsive, while a
    # fresh range version cannot immediately create a whipsaw breakout.
    dynamic_range_age_guard_enabled: bool = True
    dynamic_range_min_age_minutes: float = 12.0

    # Terminal-extension / first-move-consumed protection. For ordinary paths,
    # two of the three extension components are enough to defer. A strict
    # displacement candle at terminal extension is no longer allowed to CREATE
    # immediately. It becomes a persistent pending event and must prove post-
    # impulse acceptance or a retest/reclaim using the frozen event level + ATR.
    terminal_extension_guard_enabled: bool = True
    terminal_extension_min_move_15m_atr: float = 2.00
    terminal_extension_min_vwap_distance_atr: float = 1.50
    terminal_extension_buy_min_sod_position: float = 0.85
    terminal_extension_sell_max_sod_position: float = 0.15
    terminal_extension_min_components_to_block: int = 2
    terminal_extension_strict_displacement_bypass_enabled: bool = False

    terminal_displacement_pending_enabled: bool = True
    terminal_displacement_min_post_impulse_closes: int = 2
    terminal_displacement_pending_valid_minutes: float = 15.5
    terminal_displacement_pending_reason_code: str = "ACCEPTED_BREAKOUT_TERMINAL_DISPLACEMENT_PENDING"
    terminal_displacement_confirmed_reason_code: str = "ACCEPTED_BREAKOUT_TERMINAL_DISPLACEMENT_CONFIRMED"
    terminal_displacement_invalidated_reason_code: str = "ACCEPTED_BREAKOUT_TERMINAL_DISPLACEMENT_INVALIDATED"
    terminal_displacement_expired_reason_code: str = "ACCEPTED_BREAKOUT_TERMINAL_DISPLACEMENT_EXPIRED"

    # Tight qualified ranges remain valid, but ordinary noise around a narrow
    # boundary needs a wider buffer before it is treated as acceptance.
    micro_compression_attempt_buffer_atr: float = 0.25
    micro_compression_acceptance_buffer_atr: float = 0.35
    micro_compression_strong_displacement_min_atr: float = 1.25

    discovery_statuses: tuple[str, ...] = ("ACCEPTED_BREAKOUT",)

    # Early major-level path: do not add DEVELOPING_BREAKOUT as a separate setup.
    # BREAKOUT_ATTEMPT / BREAKOUT_TESTING may feed ACCEPTED_BREAKOUT only when
    # price has actually closed outside the reference level, price action confirms,
    # entry is close to the level, risk is tight, and room-to-move is favorable.
    early_candidate_statuses: tuple[str, ...] = ("BREAKOUT_ATTEMPT", "BREAKOUT_TESTING")
    require_price_action_confirmation: bool = True

    # Mature accepted-breakout path still requires proven acceptance.
    min_bars_outside: int = 2

    # Early candidate path is intentionally tighter because acceptance is not yet
    # fully mature. This avoids chasing a developing move while still allowing the
    # first high-quality close outside the level to be evaluated.
    early_min_bars_outside: int = 1
    early_min_break_distance_atr: float = 0.05
    early_max_entry_distance_from_level_atr: float = 1.25

    # Setup-local structural room check. This is not a target/R:R calculation;
    # it asks whether an ordinary breakout still has meaningful unconsumed space
    # before the next known external barrier. When no barrier is known, the setup
    # is not blocked. Strict displacement may bypass a nearby level because the
    # displacement itself is evidence that price can auction through it.
    structural_room_guard_enabled: bool = True
    structural_room_min_atr: float = 1.25
    structural_room_min_pct: float = 0.50

    # Level arbitration. A fixed external level may remain relevant after the
    # market has formed a newer qualified intraday balance, but it must not
    # silently override that current accepted structure. When price is still
    # inside an active INTRADAY_BALANCE, ORB/PDH/PDL observations are classified
    # explicitly as MAJOR_LEVEL_REACCEPTANCE. CREATE remains disabled for that
    # path during the first controlled replay.
    major_level_reacceptance_enabled: bool = False
    major_level_reacceptance_min_bars_outside: int = 2
    major_level_reacceptance_level_types: tuple[str, ...] = (
        "ORB_HIGH",
        "ORB_LOW",
        "PREVIOUS_DAY_HIGH",
        "PREVIOUS_DAY_LOW",
    )

    # Invalidation reference is the accepted breakout level. Entry must remain
    # close to the level; otherwise it is momentum chasing, not a high risk-reward
    # breakout entry.
    invalidation_atr_buffer: float = 1.00
    risk_model: str = "ACCEPTED_BREAKOUT_LEVEL_ATR"
    max_entry_distance_from_level_atr: float = 1.50

    # Qualified range width is structural context, not a hard entry gate. A
    # MICRO_COMPRESSION uses the wider thresholds above instead of being rejected.
    min_accepted_range_width_atr: float = 0.0


    # Participation must not be stale/weak. HMA compression/expansion is supporting
    # evidence only, not a separate setup.
    min_bar_rvol: float = 0.80
    weak_participation_bands: tuple[str, ...] = ("WEAK", "LOW", "VERY_LOW")

    # Blanket late-entry / exhaustion guard. Breakouts may naturally run near bands,
    # so these are intentionally wider than reversal filters.
    block_buy_rsi_min: float = 78.0
    block_sell_rsi_max: float = 22.0
    block_buy_bollinger_position_min: float = 1.20
    block_sell_bollinger_position_max: float = -0.20

    # Accepted breakout must have HMA aligned with the breakout side for every
    # level source (structure, ORB, previous-day, recent range, compression box).
    # Price action is already a mandatory CREATE gate; HMA alignment prevents
    # breakout entries that are only level crosses against the active momentum.
    require_hma_side_alignment: bool = True


class EvidenceSetupInvalidationPolicyConfig(BaseModel):
    model_config = STRICT_CONFIG

    # Signal-lifecycle invalidation is intentionally separate from the live
    # trade stop.  It uses the raw structural level, current ATR and recent
    # completed closes from the neutral snapshot.
    mode: str
    buffer_atr: float
    required_consecutive_closes: int
    strong_single_close_atr: float


class EvidenceSignalInvalidationConfig(BaseModel):
    model_config = STRICT_CONFIG

    accepted_breakout: EvidenceSetupInvalidationPolicyConfig = Field(
        default_factory=lambda: EvidenceSetupInvalidationPolicyConfig(
            mode="REACCEPT_INSIDE_BROKEN_RANGE",
            buffer_atr=0.20,
            required_consecutive_closes=2,
            strong_single_close_atr=0.50,
        )
    )
    failed_breakout: EvidenceSetupInvalidationPolicyConfig = Field(
        default_factory=lambda: EvidenceSetupInvalidationPolicyConfig(
            mode="REACCEPT_OUTSIDE_FAILED_LEVEL",
            buffer_atr=0.20,
            required_consecutive_closes=2,
            strong_single_close_atr=0.50,
        )
    )
    exhaustion_reversal: EvidenceSetupInvalidationPolicyConfig = Field(
        default_factory=lambda: EvidenceSetupInvalidationPolicyConfig(
            mode="EXTREME_INVALIDATION",
            buffer_atr=0.20,
            required_consecutive_closes=1,
            strong_single_close_atr=0.20,
        )
    )


class EvidenceDecisionIntegrationConfig(BaseModel):
    model_config = STRICT_CONFIG

    # StockAdvisor continues to receive the normalized Evidence candidate and
    # publish its ALLOW/WATCH/BLOCK finding. During setup tuning, Evidence owns
    # the CREATE decision and Advisor enforcement remains disabled.
    stock_advisor_enforcement_enabled: bool = False
    stock_advisor_audit_enabled: bool = True


class EvidenceEngineConfig(BaseModel):
    model_config = STRICT_CONFIG

    engine_name: str = "EVIDENCE"
    engine_version: str = "EVIDENCE_V2"
    lifecycle_name: str = "DEFAULT"
    window: EvidenceSignalWindowConfig = Field(default_factory=EvidenceSignalWindowConfig)
    data_quality: EvidenceDataQualityConfig = Field(default_factory=EvidenceDataQualityConfig)
    opportunity: EvidenceOpportunityConfig = Field(default_factory=EvidenceOpportunityConfig)
    risk: EvidenceRiskConfig = Field(default_factory=EvidenceRiskConfig)
    action: EvidenceActionConfig = Field(default_factory=EvidenceActionConfig)
    reason: EvidenceReasonConfig = Field(default_factory=EvidenceReasonConfig)
    pattern: EvidencePatternConfig = Field(default_factory=EvidencePatternConfig)
    setup_discovery: EvidenceSetupDiscoveryConfig = Field(default_factory=EvidenceSetupDiscoveryConfig)
    price_action: EvidencePriceActionConfig = Field(default_factory=EvidencePriceActionConfig)
    setup_scoring: EvidenceSetupScoringConfig = Field(default_factory=EvidenceSetupScoringConfig)
    setup_state: EvidenceSetupStateConfig = Field(default_factory=EvidenceSetupStateConfig)
    instrument_profile: EvidenceInstrumentProfileConfig = Field(default_factory=EvidenceInstrumentProfileConfig)
    exhaustion_reversal: EvidenceExhaustionReversalConfig = Field(default_factory=EvidenceExhaustionReversalConfig)
    failed_breakout: EvidenceFailedBreakoutConfig = Field(default_factory=EvidenceFailedBreakoutConfig)
    range_reabsorption: EvidenceRangeReabsorptionConfig = Field(default_factory=EvidenceRangeReabsorptionConfig)
    accepted_breakout: EvidenceAcceptedBreakoutConfig = Field(default_factory=EvidenceAcceptedBreakoutConfig)
    signal_invalidation: EvidenceSignalInvalidationConfig = Field(default_factory=EvidenceSignalInvalidationConfig)
    decision_integration: EvidenceDecisionIntegrationConfig = Field(default_factory=EvidenceDecisionIntegrationConfig)


EVIDENCE_CONFIG = EvidenceEngineConfig()
