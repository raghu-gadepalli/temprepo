from __future__ import annotations

from pydantic import BaseModel, Field


class SignalsUIConfig(BaseModel):
    data_url: str = "/dashboard/signals/data"
    refresh_ms: int = 30000
    default_limit: int = 500
    default_status: str = "ALL"
    row_lengths: list[int] = Field(default_factory=lambda: [20, 50, 100])


class UIConfig(BaseModel):
    signals: SignalsUIConfig = Field(default_factory=SignalsUIConfig)


UI_CONFIG = UIConfig()