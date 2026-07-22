from __future__ import annotations

from typing import Dict, Union
from pydantic import BaseModel, Field


WindowSpec = Union[int, str]


class DerivativesServiceConfig(BaseModel):
    """Runner/service settings for the derivatives generation process."""

    window_start: str = "09:16:00"
    window_end: str = "15:31:00"
    retry_interval_seconds: int = 10
    log_file: str = "/var/www/autotrades/scripts/derivatives.log"

    min_refresh_seconds: int = 180
    tick_minutes: int = 3
    lead_minutes: int = 1
    max_workers: int = 4


class OptionsLiteConfig(BaseModel):
    enabled: bool = True
    top_n: int = 5


class OptionLadderConfig(BaseModel):
    enabled: bool = True
    window: int = 5


class OptionSentimentConfig(BaseModel):
    enabled: bool = True

    atm_window: int = 3
    notional_floor: float = 0.0
    min_contracts_floor: float = 10.0

    windows: Dict[str, WindowSpec] = Field(default_factory=lambda: {
        "sod": "SOD",
        "60m": 60,
        "15m": 15,
        "5m": 5,
    })

    history_rows: int = 360

    one_sided_flow_ratio: float = 1.2
    driver_min_share: float = 0.25


class FutureSentimentConfig(BaseModel):
    default_directional_strength: float = 0.65


class DerivativesDerivedConfig(BaseModel):
    options_lite: OptionsLiteConfig = Field(default_factory=OptionsLiteConfig)
    option_ladder: OptionLadderConfig = Field(default_factory=OptionLadderConfig)
    option_sentiment: OptionSentimentConfig = Field(default_factory=OptionSentimentConfig)
    future_sentiment: FutureSentimentConfig = Field(default_factory=FutureSentimentConfig)


class DerivativesLifecycleConfig(BaseModel):
    """How derivatives evidence is summarized for lifecycle/trade decisions."""

    option_window_weights: Dict[str, float] = Field(default_factory=lambda: {
        "5m": 0.25,
        "15m": 0.40,
        "60m": 0.25,
        "sod": 0.10,
    })

    future_window_weights: Dict[str, float] = Field(default_factory=lambda: {
        "5m": 0.20,
        "15m": 0.40,
        "60m": 0.30,
        "sod": 0.10,
    })

    bias_threshold: float = 0.25

    option_weight: float = 0.65
    future_weight: float = 0.35

    momentum_conflict_primary_strength: float = 0.65
    contra_conflict_primary_strength: float = 0.80

    neutral_score: int = 50
    default_option_strength: float = 0.50
    default_future_directional_strength: float = 0.65


class DerivativesConfig(BaseModel):
    service: DerivativesServiceConfig = Field(default_factory=DerivativesServiceConfig)
    derived: DerivativesDerivedConfig = Field(default_factory=DerivativesDerivedConfig)
    lifecycle: DerivativesLifecycleConfig = Field(default_factory=DerivativesLifecycleConfig)


DERIVATIVES_CONFIG = DerivativesConfig()