from __future__ import annotations

from typing import Dict, List
from pydantic import BaseModel, Field


class ProtectiveStopConfig(BaseModel):
    enabled: bool = True
    default_pct: float = 2.0
    by_instrument: Dict[str, float] = Field(default_factory=lambda: {
        "EQ": 2.0,
        "FUT": 2.0,
        "CE": 20.0,
        "PE": 20.0,
    })
    apply_when_missing_or_zero: bool = True


class ProfitProtectionConfig(BaseModel):
    """First profit-protection step after a trade proves itself.

    ``stop_profit_r`` is signed in profit-space relative to executed entry:
    - ``-0.50`` leaves 0.50R of risk,
    - ``0.00`` moves to cost,
    - ``+0.50`` locks 0.50R of profit.
    """

    enabled: bool = True
    trigger_mfe_r: float = 1.0
    stop_profit_r: float = -0.5


class MaeRatchetStepConfig(BaseModel):
    """One group MAE threshold and the signed stop retained at that point."""

    mae_r: float
    stop_profit_r: float


class GroupManagementConfig(BaseModel):
    """FUT-first management shared by every trade leg for one signal.

    The reference FUT/EQ owns MFE, MAE, target, stop, and exit decisions.
    Option/EQ sibling levels are projected proportionally from the reference
    state using each instrument's frozen ATR unit.
    """

    enabled: bool = True
    reference_priority: List[str] = Field(default_factory=lambda: ["FUT", "EQ"])
    map_levels_to_siblings: bool = True

    # All adaptive buckets use the same 0.5 ATR/R step.
    step_r: float = 0.5

    # A trade remains unproven until the reference has produced this MFE.
    # MAE tightening additionally requires weakening evidence by default.
    unproven_mfe_max_r: float = 0.5
    mae_requires_weakening: bool = True
    mae_steps: List[MaeRatchetStepConfig] = Field(default_factory=lambda: [
        MaeRatchetStepConfig(mae_r=0.5, stop_profit_r=-2.5),
        MaeRatchetStepConfig(mae_r=1.0, stop_profit_r=-2.0),
        MaeRatchetStepConfig(mae_r=1.5, stop_profit_r=-1.5),
    ])

    # Once the existing profit-protection trigger is reached, every additional
    # step_r of reference MFE tightens the signed stop by the same amount.
    mfe_ratchet_enabled: bool = True


class TradeManagementConfig(BaseModel):
    """Clean Phase-1 adaptive trade-management parameters.

    The monitor manages one target and one stop per trade using instrument
    prices. ATR comes from the underlying equity snapshot; FUT uses it directly,
    ATM option premiums use ``option_atr_factor`` as an approximation.
    """
    mode: str = "EVIDENCE_ADAPTIVE_V1"

    initial_target_r_multiple: float = 2.0
    initial_stop_r_multiple: float = 3.0
    initial_option_stop_r_multiple: float = 2.25

    # Setup-derived initial stop and target.
    # BREAKOUT uses accepted breakout reference +/- ATR buffer.
    # REVERSAL/CONTRA uses the signal candle high/low +/- ATR buffer.
    # Initial target is derived from the same setup risk distance.
    # For options, premium risk/target distance is capped as a fraction of
    # the underlying setup risk distance because options are premium instruments.
    setup_initial_stop_enabled: bool = True
    setup_initial_target_enabled: bool = True
    setup_stop_buffer_r: float = 0.5
    setup_initial_target_r_multiple: float = 1.0
    setup_option_risk_cap_pct: float = 0.75

    target_expand_r_step: float = 0.5
    target_lock_buffer_r: float = 0.5
    stop_tighten_r_step: float = 0.5
    protect_buffer_r: float = 0.5
    option_atr_factor: float = 0.5

    expand_confidence_min: float = 70.0
    expand_quality_min: float = 55.0
    expand_progress_min: float = 0.75

    # The first protection move is intentionally aggressive: once MFE reaches
    # the configured trigger, pull the emergency stop close to executed cost.
    # Subsequent management remains conservative and continues to use lifecycle
    # posture plus target milestones/expansion.
    profit_protection: ProfitProtectionConfig = Field(default_factory=ProfitProtectionConfig)

    adverse_tighten_profit_r: float = 1.0
    adverse_tighten_stop_r_multiple: float = 2.0
    expand_ready_profit_r: float = 1.0

    protect_confidence_drop: float = 10.0
    protect_quality_max: float = 45.0

    group_management: GroupManagementConfig = Field(default_factory=GroupManagementConfig)

    exit_on_current_target: bool = True
    exit_on_current_stop: bool = True
    skip_exit_on_entry_tick: bool = True

class SignalExitConfig(BaseModel):
    enabled: bool = True
    actions: List[str] = Field(default_factory=lambda: [
        "DOWNGRADE",
        "INVALIDATE",
        "INVALIDATE_OPPOSITE",
        "CLOSE",
        "REVERSE",
    ])
    states: List[str] = Field(default_factory=lambda: [
        "WEAKENING",
        "REVERSED",
        "CLOSED",
        "INVALIDATED",
    ])
    by_instrument: Dict[str, Dict[str, str]] = Field(default_factory=lambda: {
        "EQ": {
            "on_weakening": "TIGHTEN_STOP",
            "on_reversal": "EXIT_FULL",
            "on_invalidated": "EXIT_FULL",
        },
        "FUT": {
            "on_weakening": "TIGHTEN_STOP",
            "on_reversal": "EXIT_FULL",
            "on_invalidated": "EXIT_FULL",
        },
        "CE": {
            "on_weakening": "EXIT_FULL",
            "on_reversal": "EXIT_FULL",
            "on_invalidated": "EXIT_FULL",
        },
        "PE": {
            "on_weakening": "EXIT_FULL",
            "on_reversal": "EXIT_FULL",
            "on_invalidated": "EXIT_FULL",
        },
    })


class MonitorConfig(BaseModel):
    window_start: str = "09:15:00"
    window_end: str = "15:31:00"
    retry_interval_seconds: int = 30
    log_file: str = "/var/www/autotrades/scripts/monitor.log"

    data_user: str = "VZS807"
    quote_batch_size: int = 180
    quote_batch_sleep_sec: float = 0.35
    intraday_cutoff_time: str = "15:20:00"
    monitor_entry_status: List[str] = Field(default_factory=lambda: ["FILLED"])

    # Live quote control. Replay uses EXECUTION_CONFIG.use_snapshot as the single switch.
    use_live_quotes: bool = True

    protective_sl: ProtectiveStopConfig = Field(default_factory=ProtectiveStopConfig)
    signal_exit: SignalExitConfig = Field(default_factory=SignalExitConfig)
    trade_management: TradeManagementConfig = Field(default_factory=TradeManagementConfig)

    setup_policy: Dict[str, float] = Field(default_factory=dict)


MONITOR_CONFIG = MonitorConfig()