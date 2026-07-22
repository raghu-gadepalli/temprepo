from __future__ import annotations

from typing import Dict, List
from pydantic import BaseModel, Field


class DuplicateGuardConfig(BaseModel):
    enabled: bool = True
    keys: List[str] = Field(default_factory=lambda: ["userid", "signal_id", "instrument_type"])
    block_statuses: List[str] = Field(default_factory=lambda: ["CREATED", "READY", "SUBMITTED", "FILLED"])


class TradeDefaultsConfig(BaseModel):
    default_exec_status: str = "CREATED"
    default_execution_mode: str = "VIRTUAL"
    default_intraday_only: bool = True

    min_eq_amt: float = 2500.0


    default_position_style: str = "NAKED"
    default_product_type: str = "MIS"


class TradeDecisionPolicyConfig(BaseModel):
    # AUTO-only eligibility is an invariant enforced by the validator:
    # users.autotrade must be 1. This compatibility field remains true so old
    # serialized config payloads stay readable; setting it false does not
    # disable the per-user opt-in.
    require_autotrade_for_auto: bool = True

    # Generic signal-entry price safeguard. This is deliberately minimal and
    # applies to every originating setup. TradeGenerator may deploy an OPEN
    # signal when the current price is at breakeven or favorable versus the
    # signal creation price:
    #   BUY  -> current_price >= created_price
    #   SELL -> current_price <= created_price
    # A temporarily adverse signal remains OPEN and is reconsidered later.
    # There is no age, candle-count, ATR-chase, target-consumed, or lifecycle-
    # stage deployment filter here; signal generation owns signal quality.
    signal_entry_not_in_loss_enabled: bool = True
    signal_entry_wait_in_loss_code: str = "SIGNAL_ENTRY_WAIT_NOT_IN_LOSS"
    signal_entry_wait_price_missing_code: str = "SIGNAL_ENTRY_WAIT_PRICE_UNAVAILABLE"

    # Manual signal trades are allowed through an explicit confirmation flow.
    # These thresholds create informational UI warnings only; they never block
    # AUTO and never invalidate the signal.
    manual_entry_warning_enabled: bool = True
    manual_entry_delay_warning_minutes: float = 6.0
    manual_entry_move_warning_pct: float = 0.50

    # TradeGenerator still owns duplicate-deployment protection.
    block_duplicate_signal_trade: bool = True
    block_duplicate_symbol_trade: bool = True


class TradePolicyConfig(BaseModel):
    auto_allowed_stages: List[str] = Field(default_factory=lambda: [
        "MOMENTUM_CONFIRMED",
        "BREAKOUT_EXPANSION",
        "MATURE_TREND",
    ])

    ui_allowed_stages: List[str] = Field(default_factory=lambda: [
        "OPENING_IMBALANCE",
        "DISCOVERY",
        "FAILED_CONTINUATION",
        "STRUCTURAL_RECOVERY",
        "MOMENTUM_FORMING",
        "MOMENTUM_CONFIRMED",
        "BREAKOUT_EXPANSION",
        "MATURE_TREND",
        "ROTATIONAL_EXHAUSTION",
        "WEAKENING",
        "INVALIDATED",
        "NO_TRADE",
    ])

    terminal_statuses: List[str] = Field(default_factory=lambda: [
        "INVALIDATED",
        "EXPIRED",
        "REPLACED",
        "CLOSED",
        "CANCELLED",
        "BLOCKED",
    ])

    duplicate_guard: DuplicateGuardConfig = Field(default_factory=DuplicateGuardConfig)
    option_side_policy: str = "BUY_OPTIONS_ONLY"
    autotrade_ready_source: str = "TRADE_GENERATOR"

    min_confidence_for_trade: float = 0.0

    decision: TradeDecisionPolicyConfig = Field(default_factory=TradeDecisionPolicyConfig)


class TradeServiceConfig(BaseModel):
    window_start: str = "09:15:00"
    window_end: str = "15:31:00"
    retry_interval_seconds: int = 15
    log_file: str = "/var/www/autotrades/scripts/trades.log"

    defaults: TradeDefaultsConfig = Field(default_factory=TradeDefaultsConfig)
    policy: TradePolicyConfig = Field(default_factory=TradePolicyConfig)


TRADE_CONFIG = TradeServiceConfig()