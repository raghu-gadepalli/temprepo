"""Configuration for the single-owner Auction Signal service.

The process replaces the current signal runner as the sole consumer of
``snapshots.processed``.  Signal persistence is delegated to
``SignalLifecycleService``; the Auction Engine itself remains table-agnostic.
All write switches are false by default for replay validation.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


STRICT = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class AuctionServiceConfig(BaseModel):
    model_config = STRICT

    enabled: bool = True
    service_version: str = "AUCTION_SIGNAL_SERVICE_PHASE5A4_V5"
    window_start: str = "09:16:00"
    window_end: str = "15:30:00"
    retry_interval_seconds: int = Field(default=15, ge=1)
    batch_size: int = Field(default=500, ge=1)
    log_file: str = "/var/www/autotrades/scripts/gen_auction.log"

    # Final ownership model agreed for AutoTrades 2.0.
    use_snapshot_processed_flag: bool = True
    restore_checkpoint_when_memory_missing: bool = True
    checkpoint_enabled: bool = True
    opportunity_persistence_enabled: bool = True

    # Safe validation defaults. Live cutover enables these deliberately after
    # report comparison; merely applying this patch does not write signals.
    checkpoint_write_enabled: bool = False
    opportunity_write_enabled: bool = False
    signal_write_enabled: bool = False
    mark_snapshot_processed_enabled: bool = False

    signal_lifecycle: str = "DEFAULT"
    fail_fast_on_snapshot_error: bool = True

    @model_validator(mode="after")
    def _validate(self) -> "AuctionServiceConfig":
        if not self.use_snapshot_processed_flag:
            raise ValueError(
                "Auction service is the sole owner of snapshots.processed"
            )
        if self.signal_write_enabled and not self.checkpoint_write_enabled:
            raise ValueError(
                "Signal writes require checkpoint_write_enabled=True"
            )
        if self.mark_snapshot_processed_enabled and not self.checkpoint_write_enabled:
            raise ValueError(
                "Processed-flag writes require checkpoint_write_enabled=True"
            )
        return self


AUCTION_SERVICE_CONFIG = AuctionServiceConfig()


__all__ = ["AuctionServiceConfig", "AUCTION_SERVICE_CONFIG"]
