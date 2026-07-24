from __future__ import annotations

from pydantic import BaseModel, Field


class ExecutionConfig(BaseModel):
    window_start: str = "09:15:00"
    window_end: str = "15:31:00"
    retry_interval_seconds: int = 8
    log_file: str = "/var/www/autotrades/scripts/execution.log"

    max_workers: int = 8
    limit: int = 500

    max_retries: int = 5
    max_reprices_per_pass: int = 3

    order_poll_interval_sec: float = 2.0
    order_poll_timeout_sec: float = 20.0

    use_snapshot: bool = False
    use_live_price_for_virtual: bool = True
    force_virtual_for_replay: bool = True

    cooldown_user_symbol_sec: int = 30
    tick_size: str = "0.05"

    # REAL entry safety: compare the latest executable quote with the refreshed
    # completed-snapshot plan immediately before broker submission.
    max_entry_price_drift_option_pct: float = Field(default=0.20, gt=0, le=1)
    max_entry_price_drift_default_pct: float = Field(default=0.02, gt=0, le=1)

    # Once a READY package passes the refreshed-plan price-drift guard, the
    # executor remains mechanical and does not re-run signal/setup entry policy.


EXECUTION_CONFIG = ExecutionConfig()