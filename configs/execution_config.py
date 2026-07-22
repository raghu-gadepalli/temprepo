from __future__ import annotations

from pydantic import BaseModel


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

    # The executor is intentionally mechanical: once TradeGenerator persists
    # a READY package, execution does not re-run signal/setup/entry policy.
    # It only requires an executable price and timestamp.


EXECUTION_CONFIG = ExecutionConfig()