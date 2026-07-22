from __future__ import annotations

from pydantic import BaseModel, Field


class BrokerReconcileExtrasConfig(BaseModel):
    max_workers: int = 5
    limit_users: int = 10
    invalidate_on_failure: bool = True
    write_funds_history: bool = True
    write_positions_history: bool = True
    write_orders_history: bool = True


class BrokerReconcileConfig(BaseModel):
    window_start: str = "08:30:00"
    window_end: str = "16:30:00"
    retry_interval_seconds: int = 60
    log_file: str = "/var/www/autotrades/scripts/broker_reconcile.log"
    extras: BrokerReconcileExtrasConfig = Field(default_factory=BrokerReconcileExtrasConfig)


class TradeBackfillExtrasConfig(BaseModel):
    max_workers: int = 5
    limit_users: int = 10
    limit_trades_per_user: int = 500
    dry_run: bool = False


class TradeBackfillConfig(BaseModel):
    window_start: str = "08:30:00"
    window_end: str = "16:30:00"
    retry_interval_seconds: int = 60
    log_file: str = "/var/www/autotrades/scripts/trade_backfill.log"
    extras: TradeBackfillExtrasConfig = Field(default_factory=TradeBackfillExtrasConfig)


class BrokerConfig(BaseModel):
    reconcile: BrokerReconcileConfig = Field(default_factory=BrokerReconcileConfig)
    trade_backfill: TradeBackfillConfig = Field(default_factory=TradeBackfillConfig)


BROKER_CONFIG = BrokerConfig()