"""Central audit/lifecycle telemetry policy.

Switches:
- Repository default is ``PRODUCTION``; use an environment override only for focused debugging.
- Or override at service start with:
    AUTOTRADES_AUDIT_ENABLED=0|1
    AUTOTRADES_AUDIT_MODE=DEBUGGING|PRODUCTION

DEBUGGING stores lifecycle/reason changes plus sampled unchanged heartbeats.
PRODUCTION stores transitions, management changes, execution changes, errors,
and one-time blockers; unchanged heartbeats are discarded.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "disabled"}


class AuditRuntimeConfig(BaseModel):
    enabled: bool = Field(default_factory=lambda: _env_bool("AUTOTRADES_AUDIT_ENABLED", True))
    mode: Literal["DEBUGGING", "PRODUCTION"] = Field(
        default_factory=lambda: str(os.getenv("AUTOTRADES_AUDIT_MODE", "PRODUCTION")).strip().upper()
    )

    # In DEBUGGING, unchanged streams are sampled rather than written every
    # service loop. PRODUCTION writes no unchanged heartbeat rows.
    debugging_heartbeat_minutes: int = 15

    # Central in-process policy cache. Each service process has its own bounded
    # cache; no database read is required to decide whether to persist.
    stream_cache_size: int = 20000

    # Audit rows contain decision deltas, never complete signal/snapshot/trade
    # objects. Oversized payloads are replaced by a deterministic summary.
    debugging_max_payload_bytes: int = 8192
    production_max_payload_bytes: int = 4096

    @field_validator("debugging_heartbeat_minutes", "stream_cache_size", "debugging_max_payload_bytes", "production_max_payload_bytes")
    @classmethod
    def _positive(cls, value: int) -> int:
        if int(value) <= 0:
            raise ValueError("audit policy numeric values must be positive")
        return int(value)


AUDIT_CONFIG = AuditRuntimeConfig()
