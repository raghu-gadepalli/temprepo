"""Evidence engine package for AutoTrades."""

from services.evidence.evidence_evaluator import EvidenceEvaluator
from services.evidence.evidence_lifecycle_adapter import EvidenceLifecycleAdapter
from services.evidence.evidence_result import EvidenceDataError, EvidenceResult
from services.evidence.setup_discovery_helper import SetupCandidate, SetupDiscoverer, SetupDiscoveryResult

__all__ = [
    "EvidenceEvaluator",
    "EvidenceLifecycleAdapter",
    "EvidenceDataError",
    "EvidenceResult",
    "SetupCandidate",
    "SetupDiscoverer",
    "SetupDiscoveryResult",
]
