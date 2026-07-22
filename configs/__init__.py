"""Typed configuration objects for AutoTrades."""

from configs.evidence_config import EVIDENCE_CONFIG
from configs.signal_config import SIGNAL_CONFIG
from configs.snapshot_config import SNAPSHOT_CONFIG
from configs.derivatives_config import DERIVATIVES_CONFIG
from configs.trade_config import TRADE_CONFIG
from configs.execution_config import EXECUTION_CONFIG
from configs.monitor_config import MONITOR_CONFIG
from configs.scanner_config import SCANNER_CONFIG
from configs.service_config import SERVICE_CONFIG
from configs.broker_config import BROKER_CONFIG
from configs.audit_config import AUDIT_CONFIG

__all__ = [
    "EVIDENCE_CONFIG",
    "SIGNAL_CONFIG",
    "SNAPSHOT_CONFIG",
    "DERIVATIVES_CONFIG",
    "TRADE_CONFIG",
    "EXECUTION_CONFIG",
    "MONITOR_CONFIG",
    "SCANNER_CONFIG",
    "SERVICE_CONFIG",
    "BROKER_CONFIG",
    "AUDIT_CONFIG",
]