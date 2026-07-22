#!/usr/bin/env python3
"""services/signals/signal_generator.py

Clean evidence-based signal generator.

Flow:
    SnapshotSchema -> EvidenceEvaluator -> EvidenceLifecycleAdapter -> signals table

Assumptions:
    - signals.lifecycle remains the downstream grouping column.
    - AutoTrades signal decisions are evidence-based.
    - Old lifecycle evaluators are not called from this generator.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from configs.signal_config import SIGNAL_CONFIG
from configs.evidence_config import EVIDENCE_CONFIG
from configs.stock_advisor_config import STOCK_ADVISOR_CONFIG
from enums.enums import LifecycleStage, SignalAction, SignalSide, SignalStatus
from schemas.signal import SignalSchema
from schemas.snapshot import SnapshotSchema
from schemas.stock_setup_state import StockSetupStateSchema
from schemas.symbol import SymbolSchema
from schemas.user_trade import UserTradeSchema
from services.audit.auditlog import write_auditlog
from services.evidence.evidence_evaluator import EvidenceEvaluator
from services.evidence.evidence_lifecycle_adapter import EvidenceLifecycleAdapter
from services.selection.stock_advisor import StockAdvisor
from services.selection.stock_advisor_result import StockAdvisorResult
from utils.json_utils import sanitize_json
from utils.datetime_utils import to_ist_naive

logger = logging.getLogger(__name__)

DEFAULT_LIFECYCLE = EVIDENCE_CONFIG.lifecycle_name.strip().upper()


def _safe_model_dump(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="python")
    if hasattr(obj, "dict"):
        return obj.dict()
    return {}


def _to_dec(x: Any) -> Optional[Decimal]:
    if x is None:
        return None
    try:
        return Decimal(str(x))
    except Exception:
        return None


def _dec_zero(x: Any) -> Decimal:
    v = _to_dec(x)
    return v if v is not None else Decimal("0")


def _enum_upper(x: Any, default: str = "") -> str:
    if x is None:
        return default
    s = str(x.value if hasattr(x, "value") else x).strip().upper()
    return s or default


def _stock_advisor_enabled() -> bool:
    return bool(getattr(STOCK_ADVISOR_CONFIG, "enabled", False))


def _compact_stock_advisor_result(result: Optional[StockAdvisorResult]) -> Dict[str, Any]:
    if result is None:
        return {}
    if hasattr(result, "to_compact_dict"):
        return sanitize_json(result.to_compact_dict())
    if hasattr(result, "to_dict"):
        return sanitize_json(result.to_dict())
    return {}


def _attach_stock_advisor_to_lifecycle_result(result: Any, advisor_result: Optional[StockAdvisorResult]) -> None:
    payload = _compact_stock_advisor_result(advisor_result)
    if not payload:
        return
    meta = getattr(result, "meta", None)
    if not isinstance(meta, dict):
        return
    meta["stock_advisor"] = payload
    evidence_payload = meta.get("evidence_result")
    if isinstance(evidence_payload, dict):
        evidence_payload["stock_advisor"] = payload
        details = evidence_payload.get("details") if isinstance(evidence_payload.get("details"), dict) else {}
        details["stock_advisor"] = payload
        evidence_payload["details"] = details


def _matching_entry_candidate(
    meta_json: Dict[str, Any],
    *,
    setup: str,
    side: str,
) -> Dict[str, Any]:
    """Return the immutable entry-time setup candidate for state persistence.

    Signal metadata is updated throughout the signal lifecycle, so top-level
    ``setup_decision`` may later describe a different/opposite candidate.  The
    ``entry_criteria_json`` snapshot is immutable and must be preferred when
    linking a setup event to the signal that consumed it.
    """
    setup_u = _enum_upper(setup)
    side_u = _enum_upper(side)

    entry = meta_json.get("entry_criteria_json") if isinstance(meta_json.get("entry_criteria_json"), dict) else {}
    containers = [
        entry.get("setup_decision"),
        entry.get("current_evidence"),
        entry.get("active_signal_evidence"),
        meta_json.get("setup_decision"),
        meta_json.get("current_evidence"),
        meta_json.get("active_signal_evidence"),
    ]
    candidate_keys = ("primary_candidate", "top_same_side_candidate")
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in candidate_keys:
            candidate = container.get(key)
            if not isinstance(candidate, dict):
                continue
            if _enum_upper(candidate.get("setup_label")) != setup_u:
                continue
            if _enum_upper(candidate.get("side")) != side_u:
                continue
            return sanitize_json(candidate)
    return {}


def _setup_state_event_context_from_meta(
    meta_json: Dict[str, Any],
    *,
    setup: str,
    side: str,
) -> Dict[str, Any]:
    """Build the exact event identity/reference consumed by a new signal."""
    candidate = _matching_entry_candidate(meta_json, setup=setup, side=side)
    if not candidate:
        return {}
    reference_price = (
        candidate.get("setup_reference_price")
        if candidate.get("setup_reference_price") is not None
        else candidate.get("level_price")
    )
    reference_source = (
        candidate.get("setup_reference_source")
        or candidate.get("level_source")
    )
    return sanitize_json({
        "event_key": candidate.get("event_key"),
        "event_source": candidate.get("event_source"),
        "event_time": candidate.get("event_time"),
        "reference_id": candidate.get("reference_id"),
        "level_type": candidate.get("level_type"),
        "level_price": candidate.get("level_price"),
        "reference_price": reference_price,
        "reference_source": reference_source,
        # Persist the path that actually authorised CREATE, not merely the
        # raw neutral-observation label. For displacement and terminal-pending
        # flows the evaluator intentionally rewrites the effective path after
        # confirmation. Falling back preserves compatibility with candidates
        # created before effective_acceptance_path was introduced.
        "acceptance_path": (
            candidate.get("effective_acceptance_path")
            or candidate.get("acceptance_path")
        ),
    })


def _stock_advisor_candidate_context(result: Any) -> Dict[str, Any]:
    """Return the normalized Evidence candidate for Advisor alignment.

    Snapshot structure is intentionally strategy-neutral.  Advisor therefore
    receives the exact setup/side/reference selected by Evidence instead of
    trying to reconstruct breakout state from removed snapshot fields.
    """
    meta = getattr(result, "meta", None)
    if not isinstance(meta, dict):
        return {}
    evidence_payload = meta.get("evidence_result")
    if not isinstance(evidence_payload, dict):
        return {}
    details = evidence_payload.get("details") if isinstance(evidence_payload.get("details"), dict) else {}
    setup_decision = details.get("setup_decision") if isinstance(details.get("setup_decision"), dict) else {}
    primary = setup_decision.get("primary_candidate") if isinstance(setup_decision.get("primary_candidate"), dict) else {}
    if not primary:
        return {}
    return sanitize_json(primary)


def _confirmed_exhaustion_priority_candidate(
    *,
    evidence_payload: Dict[str, Any],
    setup: str,
    side: str,
) -> bool:
    """Return whether the current CREATE is a confirmed, entry-ready exhaustion.

    This is deliberately derived from the normalized setup decision produced by
    EvidenceEvaluator, not from setup name alone.
    """
    if _enum_upper(setup) != _enum_upper(STOCK_ADVISOR_CONFIG.exhaustion_setup):
        return False
    details = evidence_payload.get("details") if isinstance(evidence_payload.get("details"), dict) else {}
    setup_decision = (
        details.get("setup_decision")
        if isinstance(details.get("setup_decision"), dict)
        else evidence_payload.get("setup_decision")
        if isinstance(evidence_payload.get("setup_decision"), dict)
        else {}
    )
    primary = setup_decision.get("primary_candidate") if isinstance(setup_decision.get("primary_candidate"), dict) else {}
    return bool(
        _enum_upper(primary.get("setup_label")) == _enum_upper(setup)
        and _enum_upper(primary.get("side")) == _enum_upper(side)
        and bool(primary.get("price_action_confirmed"))
        and bool(primary.get("entry_ready"))
    )


def _confirmed_exhaustion_watch_override_allowed(
    *,
    alignment: Any,
    setup: str,
    confirmed_exhaustion_priority: bool,
) -> bool:
    cfg = STOCK_ADVISOR_CONFIG
    if not bool(getattr(cfg, "confirmed_exhaustion_watch_override_enabled", False)):
        return False
    if not confirmed_exhaustion_priority:
        return False
    if _enum_upper(setup) != _enum_upper(getattr(cfg, "exhaustion_setup", "EXHAUSTION_REVERSAL")):
        return False
    if _enum_upper(getattr(alignment, "alignment", None)) != "WATCH":
        return False
    score = float(getattr(alignment, "score", 0.0) or 0.0)
    if score < float(getattr(cfg, "confirmed_exhaustion_watch_min_score", 90.0) or 90.0):
        return False
    reason_code = str(getattr(alignment, "reason_code", "") or "").strip().lower()
    terms = [
        str(term or "").strip().lower()
        for term in getattr(cfg, "confirmed_exhaustion_watch_reason_terms", [])
        if str(term or "").strip()
    ]
    return not terms or any(term in reason_code for term in terms)


def _stock_advisor_gate_reason(
    *,
    advisor_result: Optional[StockAdvisorResult],
    setup: str,
    side: str,
    confirmed_exhaustion_priority: bool = False,
) -> Dict[str, Any]:
    """Return the Advisor finding for one normalized CREATE candidate.

    Evidence owns setup confirmation.  Advisor evaluates stock/day/family/side
    suitability and always returns an observable ALLOW/DEFER/VETO finding.
    Whether that finding is enforced is controlled separately by Evidence decision integration config.
    """
    cfg = STOCK_ADVISOR_CONFIG
    setup_s = str(setup or "").strip().upper()
    side_s = str(side or "").strip().upper()
    policy = "ALLOW_ONLY" if not bool(getattr(cfg, "allow_setup_watch", False)) else "ALLOW_OR_WATCH"

    if advisor_result is None:
        return {
            "code": "stock_advisor_missing_result_defer",
            "text": "StockAdvisor returned no result for the normalized CREATE candidate.",
            "setup": setup_s,
            "side": side_s,
            "would_gate_action": "DEFER",
            "would_entry_permission": "DEFER",
            "alignment_policy": policy,
        }

    decision = str(advisor_result.decision or "").strip().upper()
    alignment = advisor_result.alignment_for(setup_s, side_s)
    alignment_s = str(alignment.alignment or "").strip().upper()
    common = {
        "setup": setup_s,
        "side": side_s,
        "advisor_decision": decision,
        "advisor_regime": advisor_result.regime,
        "advisor_reason_code": advisor_result.reason_code,
        "advisor_reason_text": advisor_result.reason_text,
        "alignment": alignment.to_dict(),
        "allow_setup_watch": bool(getattr(cfg, "allow_setup_watch", False)),
        "alignment_policy": policy,
    }

    if bool(getattr(cfg, "block_on_stock_skip", True)) and decision == "SKIP":
        return {
            **common,
            "code": "stock_advisor_stock_skip_veto",
            "text": f"StockAdvisor would veto {setup_s} {side_s}: stock decision is SKIP ({advisor_result.regime}).",
            "would_gate_action": "VETO",
            "would_entry_permission": "BLOCK",
        }

    if bool(getattr(cfg, "block_on_setup_block", True)) and alignment_s == "BLOCK":
        return {
            **common,
            "code": "stock_advisor_setup_block_veto",
            "text": f"StockAdvisor would veto {setup_s} {side_s}: exact setup-side alignment is BLOCK ({alignment.reason_code}).",
            "would_gate_action": "VETO",
            "would_entry_permission": "BLOCK",
        }

    exhaustion_watch_override = _confirmed_exhaustion_watch_override_allowed(
        alignment=alignment,
        setup=setup_s,
        confirmed_exhaustion_priority=confirmed_exhaustion_priority,
    )
    if alignment_s == "WATCH" and not bool(getattr(cfg, "allow_setup_watch", False)) and not exhaustion_watch_override:
        return {
            **common,
            "code": "stock_advisor_setup_watch_defer",
            "text": f"StockAdvisor would defer {setup_s} {side_s}: exact setup-side alignment is WATCH ({alignment.reason_code}).",
            "would_gate_action": "DEFER",
            "would_entry_permission": "DEFER",
        }

    code = "stock_advisor_confirmed_exhaustion_watch_override" if exhaustion_watch_override else "stock_advisor_create_allowed"
    return {
        **common,
        "code": code,
        "text": (
            f"StockAdvisor would allow confirmed {setup_s} {side_s} under the exhaustion WATCH override."
            if exhaustion_watch_override
            else f"StockAdvisor allows {setup_s} {side_s}: exact setup-side alignment is {alignment.alignment} ({alignment.reason_code})."
        ),
        "confirmed_exhaustion_priority": confirmed_exhaustion_priority,
        "confirmed_exhaustion_watch_override": exhaustion_watch_override,
        "would_gate_action": "ALLOW",
        "would_entry_permission": "ALLOW",
    }


def _stock_advisor_gate_audit_action(reason: Dict[str, Any]) -> str:
    if not bool(reason.get("advisor_enforced")):
        return "STOCK_ADVISOR_OBSERVATION"
    code = str(reason.get("code") or "").strip().lower()
    if code == "stock_advisor_confirmed_exhaustion_watch_override":
        return "STOCK_ADVISOR_CONFIRMED_EXHAUSTION_WATCH_ALLOW"
    if code == "stock_advisor_create_allowed":
        return "STOCK_ADVISOR_CREATE_ALLOWED"
    if code == "stock_advisor_setup_watch_defer":
        return "STOCK_ADVISOR_WATCH_DEFER"
    if code == "stock_advisor_setup_block_veto":
        return "STOCK_ADVISOR_BLOCK_VETO"
    if code == "stock_advisor_stock_skip_veto":
        return "STOCK_ADVISOR_STOCK_SKIP"
    if code == "stock_advisor_missing_result_defer":
        return "STOCK_ADVISOR_MISSING_RESULT_DEFER"
    return "STOCK_ADVISOR_GATE_DECISION"


def _write_stock_advisor_gate_audit(
    *,
    symbol: str,
    snapshot_time: Any,
    candidate_price: Any,
    reason: Dict[str, Any],
    lifecycle_result: Any,
) -> None:
    if not bool(getattr(EVIDENCE_CONFIG.decision_integration, "stock_advisor_audit_enabled", True)):
        return
    action = _stock_advisor_gate_audit_action(reason)
    setup = str(reason.get("setup") or "UNKNOWN").strip().upper()
    side = str(reason.get("side") or "UNKNOWN").strip().upper()
    alignment = _as_dict(reason.get("alignment"))
    alignment_name = str(alignment.get("alignment") or "MISSING").strip().upper()
    enforced = bool(reason.get("advisor_enforced"))
    effective_action = str(reason.get("gate_action") or "ALLOW").strip().upper()
    would_action = str(reason.get("would_gate_action") or effective_action).strip().upper()

    if isinstance(snapshot_time, datetime):
        ts_key = snapshot_time.strftime("%Y%m%d%H%M%S")
        audit_ts = snapshot_time
    else:
        ts_key = str(snapshot_time or "UNKNOWN").replace(" ", "T")[:20]
        audit_ts = None

    entity_id = f"{str(symbol or 'UNKNOWN').upper()}:{ts_key}:{setup}:{side}"[:80]
    new_state = (
        {"ALLOW": "CREATE_ALLOWED", "DEFER": "WATCH_DEFERRED", "VETO": "CREATE_VETOED"}.get(effective_action, "GATE_DECIDED")
        if enforced
        else "CREATE_UNCHANGED"
    )

    write_auditlog(
        entity_type="SIGNAL_CANDIDATE",
        entity_id=entity_id,
        symbol=symbol,
        evaluation_stage="STOCK_ADVISOR_GATE",
        previous_state="CREATE_CANDIDATE",
        new_state=new_state,
        action=action,
        reason_code=reason.get("code"),
        reason_text=reason.get("text"),
        confidence=alignment.get("score"),
        ts=audit_ts,
        payload_json={
            "symbol": symbol,
            "snapshot_time": snapshot_time,
            "setup_type": setup,
            "side": side,
            "candidate_price": candidate_price,
            "advisor_alignment": alignment_name,
            "advisor_score": alignment.get("score"),
            "advisor_reason_code": alignment.get("reason_code") or reason.get("advisor_reason_code"),
            "advisor_reason": alignment.get("reason_text") or reason.get("advisor_reason_text"),
            "overall_advisor_decision": reason.get("advisor_decision"),
            "advisor_regime": reason.get("advisor_regime"),
            "advisor_enforced": enforced,
            "would_gate_action": would_action,
            "would_entry_permission": reason.get("would_entry_permission"),
            "effective_gate_action": effective_action,
            "effective_entry_permission": reason.get("entry_permission"),
            "gate_reason_code": reason.get("code"),
            "gate_reason_text": reason.get("text"),
            "alignment_policy": reason.get("alignment_policy"),
            "signal_action_before_gate": "CREATE",
            "signal_action_after_gate": _enum_upper(getattr(lifecycle_result, "signal_action", None)),
        },
    )


def _apply_stock_advisor_hard_gate(
    *,
    lifecycle_result: Any,
    advisor_result: Optional[StockAdvisorResult],
    symbol: str,
    snapshot_time: Any,
    candidate_price: Any,
) -> None:
    """Evaluate Advisor for logging and optionally enforce its finding."""
    if _enum_upper(getattr(lifecycle_result, "signal_action", None)) != "CREATE":
        return
    if not _stock_advisor_enabled():
        return

    evidence_payload = getattr(lifecycle_result, "meta", {}).get("evidence_result") if isinstance(getattr(lifecycle_result, "meta", None), dict) else {}
    if not isinstance(evidence_payload, dict):
        raise ValueError("Evidence lifecycle result missing evidence_result payload before StockAdvisor evaluation")

    setup = str(evidence_payload.get("setup_label") or "").strip().upper()
    side = _enum_upper(getattr(lifecycle_result, "side", None))
    confirmed_exhaustion_priority = _confirmed_exhaustion_priority_candidate(
        evidence_payload=evidence_payload,
        setup=setup,
        side=side,
    )
    reason = _stock_advisor_gate_reason(
        advisor_result=advisor_result,
        setup=setup,
        side=side,
        confirmed_exhaustion_priority=confirmed_exhaustion_priority,
    )

    enforce = bool(getattr(EVIDENCE_CONFIG.decision_integration, "stock_advisor_enforcement_enabled", False))
    would_action = str(reason.get("would_gate_action") or "ALLOW").strip().upper()
    would_permission = str(reason.get("would_entry_permission") or "ALLOW").strip().upper()
    reason["advisor_enforced"] = enforce
    reason["gate_action"] = would_action if enforce else "ALLOW"
    reason["entry_permission"] = would_permission if enforce else "ALLOW"
    reason = sanitize_json(reason)

    lifecycle_result.meta["stock_advisor_gate"] = reason
    details = evidence_payload.get("details") if isinstance(evidence_payload.get("details"), dict) else {}
    details["stock_advisor_gate"] = reason
    evidence_payload["details"] = details

    if enforce and would_action in {"DEFER", "VETO"}:
        signal_state = "BLOCKED" if would_action == "VETO" else "DEFER"
        lifecycle_result.set_signal(SignalAction.WATCH, signal_state, reason["code"])
        lifecycle_result.add_warning(reason["code"], reason["text"], 1.0, reason)
        lifecycle_result.meta["evidence_reason"] = reason
        evidence_payload["entry_permission"] = would_permission
        evidence_payload["decision"] = "STOCK_ADVISOR_VETOED" if would_action == "VETO" else "STOCK_ADVISOR_DEFERRED"
        evidence_payload["blocked_by"] = reason["code"]
        risk_flag = "STOCK_ADVISOR_GATE_VETO" if would_action == "VETO" else "STOCK_ADVISOR_GATE_DEFER"
        risk_flags = evidence_payload.get("risk_flags") if isinstance(evidence_payload.get("risk_flags"), list) else []
        if risk_flag not in risk_flags:
            risk_flags.append(risk_flag)
        evidence_payload["risk_flags"] = risk_flags

    _write_stock_advisor_gate_audit(
        symbol=symbol,
        snapshot_time=snapshot_time,
        candidate_price=candidate_price,
        reason=reason,
        lifecycle_result=lifecycle_result,
    )
    logger.info(
        "SIG_ADVISOR_GATE | %s @ %s | setup=%s side=%s enforced=%s would=%s effective=%s decision=%s regime=%s alignment=%s reason=%s",
        symbol, snapshot_time, setup, side, enforce, would_action, reason.get("gate_action"),
        reason.get("advisor_decision"), reason.get("advisor_regime"),
        _as_dict(reason.get("alignment")).get("alignment"), reason.get("code"),
    )


def _required_setup_label(meta_json: Any) -> str:
    """Return the explicit setup label stored by the current signal decision.

    No lifecycle/legacy/current/initiated metadata fields are accepted here. If the
    decision did not write setup_label explicitly, persistence should fail.
    """
    if not isinstance(meta_json, dict):
        raise ValueError("Missing meta_json while resolving signal setup")
    label = str(meta_json.get("setup_label") or "").strip().upper()
    if not label:
        raise ValueError("Missing required meta_json.setup_label for signal setup")
    return label


def _setup_label_for_log(meta_json: Any, lifecycle_result: Any = None, persisted_signal: Any = None) -> str:
    """Best-effort setup label for human-readable logs.

    The persisted lifecycle is still DEFAULT for active-signal lookup compatibility;
    logs should show the setup that actually drove the decision.
    """
    candidates = []

    if persisted_signal is not None:
        candidates.append(getattr(persisted_signal, "setup", None))

    if isinstance(meta_json, dict):
        candidates.append(meta_json.get("setup_label"))
        signal_block = meta_json.get("signal")
        if isinstance(signal_block, dict):
            candidates.append(signal_block.get("setup_label"))
        current_evidence = meta_json.get("current_evidence")
        if isinstance(current_evidence, dict):
            candidates.append(current_evidence.get("setup_label"))
            candidate = current_evidence.get("primary_candidate")
            if isinstance(candidate, dict):
                candidates.append(candidate.get("setup_label"))

    result_meta = getattr(lifecycle_result, "meta", None) if lifecycle_result is not None else None
    if isinstance(result_meta, dict):
        evidence = result_meta.get("evidence_result")
        if isinstance(evidence, dict):
            candidates.append(evidence.get("setup_label"))

    for value in candidates:
        label = str(value or "").strip().upper()
        if label:
            return label
    return "UNKNOWN"


def _reasons_to_list(items: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for x in items or []:
        if hasattr(x, "model_dump"):
            out.append(x.model_dump(mode="python"))
        elif isinstance(x, dict):
            out.append(x)
        else:
            out.append({"message": str(x)})
    return out




def _parse_time_hms(value: Any) -> Optional[time]:
    if isinstance(value, time):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except Exception:
            pass
    return None


def _fresh_signal_window_valid(ts: datetime) -> bool:
    start = _parse_time_hms(EVIDENCE_CONFIG.window.earliest_fresh_signal_time)
    end = _parse_time_hms(EVIDENCE_CONFIG.window.latest_fresh_signal_time)
    clock = ts.timetz().replace(tzinfo=None)
    if start is not None and clock < start:
        return False
    if end is not None and clock > end:
        return False
    return True


def _fresh_signal_window_reason(ts: datetime) -> Tuple[str, str]:
    start = _parse_time_hms(EVIDENCE_CONFIG.window.earliest_fresh_signal_time)
    end = _parse_time_hms(EVIDENCE_CONFIG.window.latest_fresh_signal_time)
    clock = ts.timetz().replace(tzinfo=None)
    if start is not None and clock < start:
        return (
            "fresh_signal_window_not_started",
            f"Fresh signals are disabled before {start.strftime('%H:%M:%S')}.",
        )
    if end is not None and clock > end:
        return (
            "fresh_signal_window_closed",
            f"Fresh signals are disabled after {end.strftime('%H:%M:%S')}.",
        )
    return ("fresh_signal_window_valid", "Fresh signal window is valid.")

def _side_to_signal_side(side: Any) -> Optional[SignalSide]:
    side_s = _enum_upper(side)
    if side_s == "BUY":
        return SignalSide.BUY
    if side_s == "SELL":
        return SignalSide.SELL
    return None


def _reason_item_text(item: Any) -> str:
    if item is None:
        return ""
    if isinstance(item, dict):
        return str(
            item.get("message")
            or item.get("reason")
            or item.get("label")
            or item.get("key")
            or ""
        ).strip()
    return str(
        getattr(item, "message", None)
        or getattr(item, "reason", None)
        or getattr(item, "label", None)
        or getattr(item, "key", None)
        or item
    ).strip()


def _is_low_value_reason(text: str) -> bool:
    t = str(text or "").upper()
    low_value_tokens = (
        "ADX=WEAK",
        "ATR=WEAK",
        "ADX WEAK",
        "ATR WEAK",
    )
    return any(tok in t for tok in low_value_tokens)


def _first_reason(result: Any, attrs: Tuple[str, ...], *, skip_low_value: bool = False) -> Optional[str]:
    for attr in attrs:
        items = getattr(result, attr, None) or []
        if isinstance(items, str):
            text = items.strip()
            if text and not (skip_low_value and _is_low_value_reason(text)):
                return text
            continue

        for item in items:
            text = _reason_item_text(item)
            if not text:
                continue
            if skip_low_value and _is_low_value_reason(text):
                continue
            return text
    return None




def _first_transition_reason(result: Any) -> Optional[str]:
    """Prefer transition-confirmed reasons over legacy HMA/indicator text."""
    for attr in ("supports", "reasons"):
        for item in getattr(result, attr, None) or []:
            key = ""
            if isinstance(item, dict):
                key = str(item.get("key") or "")
            else:
                key = str(getattr(item, "key", "") or "")
            if key.startswith("transition_"):
                text = _reason_item_text(item)
                if text:
                    return text
    ctx = getattr(result, "transition_context", None) or {}
    if isinstance(ctx, dict) and str(ctx.get("entry_action") or "").upper() == "CONFIRM":
        entry_type = str(ctx.get("entry_type") or "TRANSITION").upper()
        entry_side = str(ctx.get("entry_side") or "").upper()
        if entry_side in {"BUY", "SELL"}:
            return f"Transition confirmed {entry_type} {entry_side}"
    return None

def _evidence_reason_code_text(result: Any) -> Tuple[str, str]:
    """Return evidence reason code/text from the evidence adapter.

    The old reason builder was intentionally removed from the active
    AutoTrades path. Missing evidence reason metadata is an error because hidden
    implicit substitutions make replay analysis misleading.
    """
    meta = getattr(result, "meta", None)
    if not isinstance(meta, dict):
        raise ValueError("Evidence result missing result.meta")
    reason = meta.get("evidence_reason")
    if not isinstance(reason, dict):
        raise ValueError("Evidence result missing result.meta['evidence_reason']")
    code = str(reason.get("code") or "").strip()
    text = str(reason.get("text") or "").strip()
    if not code or not text:
        raise ValueError(f"Evidence reason must include code and text: {reason!r}")
    return code, text

def _primary_reason(result: Any) -> Optional[str]:
    code, text = _evidence_reason_code_text(result)
    if text:
        return text

    action = _enum_upper(getattr(result, "signal_action", None))
    signal_reason = str(getattr(result, "signal_reason", "") or "").strip()

    positive_actions = {
        "CREATE",
        "PROMOTE",
        "UPDATE",
        "HOLD",
        "INVALIDATE_OPPOSITE",
        "REVIEW_OPPOSITE",
    }

    if action in positive_actions:
        return (
            _first_transition_reason(result)
            or _first_reason(result, ("supports",), skip_low_value=True)
            or (signal_reason if signal_reason and not _is_low_value_reason(signal_reason) else None)
            or _first_reason(result, ("conflicts", "warnings", "supports"), skip_low_value=True)
            or signal_reason
            or _first_reason(result, ("warnings", "conflicts", "supports"))
        )

    return (
        _first_reason(result, ("conflicts", "warnings", "supports"))
        or signal_reason
    )


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _reason_record(result: Any) -> Dict[str, Any]:
    code, text = _evidence_reason_code_text(result)
    meta = getattr(result, "meta", None)
    if not isinstance(meta, dict) or "evidence_result" not in meta:
        raise ValueError("Evidence result missing result.meta['evidence_result']")
    evidence_result = meta["evidence_result"]
    return sanitize_json({
        "code": code,
        "text": text,
        "signal_reason": getattr(result, "signal_reason", ""),
        "action": _enum_upper(getattr(result, "signal_action", None)),
        "signal_state": getattr(result, "signal_state", ""),
        "stage": _enum_upper(getattr(result, "stage", None)),
        "side": _enum_upper(getattr(result, "side", None)),
        "engine_name": meta.get("engine_name"),
        "engine_version": meta.get("engine_version"),
        "preferred_side": evidence_result.get("preferred_side") if isinstance(evidence_result, dict) else None,
        "preferred_opportunity_score": evidence_result.get("preferred_opportunity_score") if isinstance(evidence_result, dict) else None,
        "strategy": evidence_result.get("strategy") if isinstance(evidence_result, dict) else None,
        "setup_label": evidence_result.get("setup_label") if isinstance(evidence_result, dict) else None,
        "primary_pattern": evidence_result.get("primary_pattern") if isinstance(evidence_result, dict) else None,
        "entry_permission": evidence_result.get("entry_permission") if isinstance(evidence_result, dict) else None,
    })

def _existing_meta(signal: Optional[SignalSchema]) -> Dict[str, Any]:
    meta = getattr(signal, "meta_json", None) if signal else None
    return meta if isinstance(meta, dict) else {}


def _nested_setup_levels(container: Any, *path: str) -> Dict[str, Any]:
    current: Any = container
    for key in path:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return dict(current) if isinstance(current, dict) else {}


def _snapshot_atr_value(snapshot: Any) -> Optional[float]:
    if not isinstance(snapshot, dict):
        return None
    indicators = snapshot.get("indicators") if isinstance(snapshot.get("indicators"), dict) else {}
    atr_block = indicators.get("atr") if isinstance(indicators.get("atr"), dict) else {}
    try:
        value = atr_block.get("value")
        return float(value) if value is not None else None
    except Exception:
        return None


def _resolve_open_signal_setup_levels(signal: Optional[SignalSchema]) -> Dict[str, Any]:
    """Resolve immutable entry setup levels for active-signal evaluation.

    ``meta_json.setup_levels`` is current-evaluation data and may change on HOLD
    updates.  Prefer the immutable initiated/entry payload, then use older storage
    locations only as compatibility fallbacks.
    """
    if signal is None:
        return {}
    meta = _existing_meta(signal)
    criteria = getattr(signal, "criteria_json", None)
    snapshot = getattr(signal, "snapshot_json", None)

    candidates = [
        _nested_setup_levels(meta, "initiated_setup", "setup_levels"),
        _nested_setup_levels(meta, "entry_criteria_json", "setup_levels"),
        _nested_setup_levels(meta, "entry_criteria_json", "current_evidence", "setup_levels"),
        _nested_setup_levels(criteria, "setup_levels"),
        _nested_setup_levels(criteria, "current_evidence", "setup_levels"),
        _nested_setup_levels(meta, "setup_levels"),
        _nested_setup_levels(meta, "signal", "setup_levels"),
        _nested_setup_levels(meta, "current_evidence", "setup_levels"),
        _nested_setup_levels(meta, "evidence", "details", "setup_levels"),
    ]
    resolved: Dict[str, Any] = {}
    for candidate in candidates:
        if candidate:
            resolved = dict(candidate)
            break
    if not resolved:
        return {}

    # Fill missing immutable metadata without allowing a later evaluation to
    # overwrite the original structural reference.
    for candidate in candidates:
        for key, value in candidate.items():
            current = resolved.get(key)
            if key not in resolved or current is None or current == "":
                resolved[key] = value

    if not resolved.get("setup_label"):
        resolved["setup_label"] = getattr(signal, "setup", None)
    if not resolved.get("side"):
        resolved["side"] = _enum_upper(getattr(signal, "side", None))
    if resolved.get("event_atr") in {None, ""}:
        entry_snapshot = meta.get("entry_snapshot_json") if isinstance(meta.get("entry_snapshot_json"), dict) else None
        event_atr = _snapshot_atr_value(entry_snapshot) or _snapshot_atr_value(snapshot)
        if event_atr is not None and event_atr > 0:
            resolved["event_atr"] = event_atr
            resolved.setdefault("signal_invalidation_atr_policy", "FROZEN_EVENT_ATR_FROM_ENTRY_SNAPSHOT")
    return sanitize_json(resolved)


def _low_information_reason(record: Any) -> bool:
    rec = _as_dict(record)
    code = _enum_upper(rec.get("code"))
    action = _enum_upper(rec.get("action"))
    return code in {"EVIDENCE_NO_ACTION"} and action in {"HOLD", "WATCH", ""}


def _reason_stage(record: Any) -> str:
    return _enum_upper(_as_dict(record).get("stage"))


def _reason_action(record: Any) -> str:
    return _enum_upper(_as_dict(record).get("action"))


def _is_downgrade_action(record: Any) -> bool:
    """Return True only for an explicit DOWNGRADE decision.

    A HOLD can legitimately keep a signal in WEAKENING stage.  That must not
    be treated as a fresh downgrade reason, otherwise the stored
    downgrade_reason gets overwritten by a normal hold message.
    """
    rec = _as_dict(record)
    if not rec or _low_information_reason(rec):
        return False
    return _reason_action(rec) == "DOWNGRADE"


def _has_meaningful_downgrade(record: Any) -> bool:
    return _is_downgrade_action(record)



def _setup_decision_from_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    details = evidence.get("details") if isinstance(evidence.get("details"), dict) else {}
    setup_decision = details.get("setup_decision") if isinstance(details.get("setup_decision"), dict) else {}
    return setup_decision


def _compact_candidate_for_signal(candidate: Any) -> Dict[str, Any] | None:
    if not isinstance(candidate, dict):
        return None
    keys = (
        "setup_label",
        "strategy",
        "side",
        "entry_ready",
        "price_action_confirmed",
        "price_action_strength",
        "blocked_by",
        "reference_id",
        "level_type",
        "level_source",
        "level_price",
        "entry_price",
        "setup_reference_price",
        "setup_reference_source",
        "invalidation_side",
        "acceptance_path",
        "event_key",
        "event_source",
        "event_time",
    )
    return sanitize_json({k: candidate.get(k) for k in keys if k in candidate})


def _compact_setup_decision(setup_decision: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(setup_decision, dict):
        return {}
    active_signal_evidence = setup_decision.get("active_signal_evidence") if isinstance(setup_decision.get("active_signal_evidence"), dict) else {}
    return sanitize_json({
        "phase": setup_decision.get("phase"),
        "has_active_signal": setup_decision.get("has_active_signal"),
        "active_side": setup_decision.get("active_side"),
        "reference_side": setup_decision.get("reference_side"),
        "decision": setup_decision.get("decision"),
        "evaluator_state": setup_decision.get("evaluator_state"),
        "preferred_side": setup_decision.get("preferred_side"),
        "candidate_count": setup_decision.get("candidate_count"),
        "confirmed_candidate_count": setup_decision.get("confirmed_candidate_count"),
        "entry_ready_candidate_count": setup_decision.get("entry_ready_candidate_count"),
        "same_side_candidate_count": setup_decision.get("same_side_candidate_count"),
        "same_side_confirmed_count": setup_decision.get("same_side_confirmed_count"),
        "same_side_entry_ready_count": setup_decision.get("same_side_entry_ready_count"),
        "opposite_candidate_count": setup_decision.get("opposite_candidate_count"),
        "opposite_confirmed_count": setup_decision.get("opposite_confirmed_count"),
        "opposite_entry_ready_count": setup_decision.get("opposite_entry_ready_count"),
        "setup_counts": setup_decision.get("setup_counts", {}),
        "side_counts": setup_decision.get("side_counts", {}),
        "blocker_counts": setup_decision.get("blocker_counts", {}),
        "primary_candidate": _compact_candidate_for_signal(setup_decision.get("primary_candidate")),
        "entry_ready_candidates": [
            _compact_candidate_for_signal(c)
            for c in (setup_decision.get("entry_ready_candidates") or [])[:5]
            if isinstance(c, dict)
        ],
        "same_side_entry_ready_candidates": [
            _compact_candidate_for_signal(c)
            for c in (setup_decision.get("same_side_entry_ready_candidates") or [])[:5]
            if isinstance(c, dict)
        ],
        "opposite_entry_ready_candidates": [
            _compact_candidate_for_signal(c)
            for c in (setup_decision.get("opposite_entry_ready_candidates") or [])[:5]
            if isinstance(c, dict)
        ],
        "active_signal_evidence": active_signal_evidence,
    })


def _current_evidence_record(evidence: Dict[str, Any], lifecycle_result: Any) -> Dict[str, Any]:
    setup_decision = _setup_decision_from_evidence(evidence)
    compact_decision = _compact_setup_decision(setup_decision)
    code, text = _evidence_reason_code_text(lifecycle_result)
    return sanitize_json({
        "snapshot_time": evidence.get("snapshot_time"),
        "stage": _enum_upper(getattr(lifecycle_result, "stage", None)),
        "side": _enum_upper(getattr(lifecycle_result, "side", None)),
        "signal_action": _enum_upper(getattr(lifecycle_result, "signal_action", None)),
        "signal_state": getattr(lifecycle_result, "signal_state", ""),
        "signal_reason": getattr(lifecycle_result, "signal_reason", ""),
        "reason": {"code": code, "text": text},
        "strategy": evidence.get("strategy"),
        "setup_label": evidence.get("setup_label"),
        "primary_pattern": evidence.get("primary_pattern"),
        "entry_permission": evidence.get("entry_permission"),
        "evaluator_state": evidence.get("evaluator_state"),
        "decision": evidence.get("decision"),
        "preferred_side": evidence.get("preferred_side"),
        "preferred_opportunity_score": evidence.get("preferred_opportunity_score"),
        "opposite_pressure": evidence.get("opposite_pressure"),
        "blocked_by": evidence.get("blocked_by"),
        "risk_flags": evidence.get("risk_flags", []),
        "stock_advisor": sanitize_json(evidence.get("stock_advisor")) if isinstance(evidence.get("stock_advisor"), dict) else {},
        "stock_advisor_gate": sanitize_json(evidence.get("details", {}).get("stock_advisor_gate")) if isinstance(evidence.get("details"), dict) and isinstance(evidence.get("details", {}).get("stock_advisor_gate"), dict) else {},
        "setup_decision": compact_decision,
        "active_signal_evidence": compact_decision.get("active_signal_evidence", {}),
        "primary_candidate": compact_decision.get("primary_candidate"),
        "entry_ready_candidates": compact_decision.get("entry_ready_candidates", []),
    })


def _compact_current_evidence_for_audit(current_evidence: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(current_evidence, dict):
        return {}
    return sanitize_json({
        "snapshot_time": current_evidence.get("snapshot_time"),
        "stage": current_evidence.get("stage"),
        "side": current_evidence.get("side"),
        "signal_action": current_evidence.get("signal_action"),
        "signal_state": current_evidence.get("signal_state"),
        "signal_reason": current_evidence.get("signal_reason"),
        "reason": _as_dict(current_evidence.get("reason")),
        "strategy": current_evidence.get("strategy"),
        "setup_label": current_evidence.get("setup_label"),
        "primary_pattern": current_evidence.get("primary_pattern"),
        "entry_permission": current_evidence.get("entry_permission"),
        "evaluator_state": current_evidence.get("evaluator_state"),
        "decision": current_evidence.get("decision"),
        "preferred_side": current_evidence.get("preferred_side"),
        "preferred_opportunity_score": current_evidence.get("preferred_opportunity_score"),
        "opposite_pressure": current_evidence.get("opposite_pressure"),
        "blocked_by": current_evidence.get("blocked_by"),
        "risk_flags": current_evidence.get("risk_flags", []),
        "stock_advisor": sanitize_json(current_evidence.get("stock_advisor")) if isinstance(current_evidence.get("stock_advisor"), dict) else {},
        "stock_advisor_gate": sanitize_json(current_evidence.get("stock_advisor_gate")) if isinstance(current_evidence.get("stock_advisor_gate"), dict) else {},
        "setup_decision": sanitize_json(current_evidence.get("setup_decision")) if isinstance(current_evidence.get("setup_decision"), dict) else {},
        "active_signal_evidence": sanitize_json(current_evidence.get("active_signal_evidence")) if isinstance(current_evidence.get("active_signal_evidence"), dict) else {},
        "primary_candidate": sanitize_json(current_evidence.get("primary_candidate")) if isinstance(current_evidence.get("primary_candidate"), dict) else None,
        "entry_ready_candidates": sanitize_json(current_evidence.get("entry_ready_candidates")) if isinstance(current_evidence.get("entry_ready_candidates"), list) else [],
    })


def _compact_evidence_for_audit(evidence: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(evidence, dict):
        return {}
    setup_decision = _setup_decision_from_evidence(evidence)
    compact_decision = _compact_setup_decision(setup_decision)
    return sanitize_json({
        "snapshot_time": evidence.get("snapshot_time"),
        "strategy": evidence.get("strategy"),
        "setup_label": evidence.get("setup_label"),
        "primary_pattern": evidence.get("primary_pattern"),
        "entry_permission": evidence.get("entry_permission"),
        "evaluator_state": evidence.get("evaluator_state"),
        "decision": evidence.get("decision"),
        "price_action_confirmed": evidence.get("price_action_confirmed"),
        "price_action_strength": evidence.get("price_action_strength"),
        "preferred_side": evidence.get("preferred_side"),
        "preferred_opportunity_score": evidence.get("preferred_opportunity_score"),
        "opposite_pressure": evidence.get("opposite_pressure"),
        "blocked_by": evidence.get("blocked_by"),
        "risk_flags": evidence.get("risk_flags", []),
        "discovered_setups": evidence.get("discovered_setups", []),
        "confirmed_setups": evidence.get("confirmed_setups", []),
        "supporting_setups": evidence.get("supporting_setups", []),
        "setup_levels": evidence.get("details", {}).get("setup_levels") if isinstance(evidence.get("details"), dict) else None,
        "active_price_action": evidence.get("details", {}).get("active_price_action") if isinstance(evidence.get("details"), dict) else None,
        "stock_advisor": sanitize_json(evidence.get("stock_advisor")) if isinstance(evidence.get("stock_advisor"), dict) else {},
        "stock_advisor_gate": sanitize_json(evidence.get("details", {}).get("stock_advisor_gate")) if isinstance(evidence.get("details"), dict) and isinstance(evidence.get("details", {}).get("stock_advisor_gate"), dict) else {},
        "setup_decision": compact_decision,
    })


def _initiated_setup_from_current(current_evidence: Dict[str, Any], meta_json: Dict[str, Any], entry_reason: Dict[str, Any]) -> Dict[str, Any]:
    candidate = current_evidence.get("primary_candidate") if isinstance(current_evidence.get("primary_candidate"), dict) else {}
    signal_block = meta_json.get("signal") if isinstance(meta_json.get("signal"), dict) else {}
    setup_levels = meta_json.get("setup_levels") if isinstance(meta_json.get("setup_levels"), dict) else signal_block.get("setup_levels")
    return sanitize_json({
        "setup_label": candidate.get("setup_label") or signal_block.get("setup_label") or meta_json.get("setup_label"),
        "strategy": candidate.get("strategy") or signal_block.get("strategy") or meta_json.get("strategy"),
        "side": candidate.get("side") or signal_block.get("side"),
        "entry_price": candidate.get("entry_price"),
        "level_type": candidate.get("level_type"),
        "level_source": candidate.get("level_source"),
        "level_price": candidate.get("level_price"),
        "setup_reference_price": candidate.get("setup_reference_price"),
        "setup_reference_source": candidate.get("setup_reference_source"),
        "invalidation_side": candidate.get("invalidation_side"),
        "price_action_strength": candidate.get("price_action_strength"),
        "entry_reason": entry_reason,
        "setup_levels": setup_levels if isinstance(setup_levels, dict) else None,
    })


def _evidence_history_record(current_evidence: Dict[str, Any]) -> Dict[str, Any]:
    active_ev = current_evidence.get("active_signal_evidence") if isinstance(current_evidence.get("active_signal_evidence"), dict) else {}
    decision = current_evidence.get("setup_decision") if isinstance(current_evidence.get("setup_decision"), dict) else {}
    return sanitize_json({
        "snapshot_time": current_evidence.get("snapshot_time"),
        "stage": current_evidence.get("stage"),
        "side": current_evidence.get("side"),
        "signal_action": current_evidence.get("signal_action"),
        "reason_code": _as_dict(current_evidence.get("reason")).get("code"),
        "active_evidence_action": active_ev.get("active_evidence_action") or active_ev.get("evidence_action"),
        "active_evidence_reason_code": active_ev.get("reason_code"),
        "exit_pressure": active_ev.get("exit_pressure"),
        "trail_mode": active_ev.get("trail_mode"),
        "target_expansion_allowed": active_ev.get("target_expansion_allowed"),
        "should_exit_signal": active_ev.get("should_exit_signal"),
        "same_pass_reversal_allowed": active_ev.get("same_pass_reversal_allowed"),
        "support_score": active_ev.get("support_score"),
        "opposition_score": active_ev.get("opposition_score"),
        "same_side_entry_ready_count": active_ev.get("same_side_entry_ready_count"),
        "opposite_entry_ready_count": active_ev.get("opposite_entry_ready_count"),
        "same_side_confirmed_count": active_ev.get("same_side_confirmed_count"),
        "opposite_confirmed_count": active_ev.get("opposite_confirmed_count"),
        "top_same_side_candidate": active_ev.get("top_same_side_candidate"),
        "top_opposite_candidate": active_ev.get("top_opposite_candidate"),
        "candidate_count": decision.get("candidate_count"),
        "entry_ready_candidate_count": decision.get("entry_ready_candidate_count"),
    })



def _no_same_pass_exit_policy_from_active_evidence(active_ev: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(active_ev, dict):
        return {}
    action = str(active_ev.get("active_evidence_action") or active_ev.get("evidence_action") or "").upper().strip()
    should_exit = bool(active_ev.get("should_exit_signal")) or action == "EXIT"
    if not should_exit:
        return {}
    return {
        "active_signal_exited_this_pass": True,
        "new_signal_blocked_until_next_pass": True,
        "blocked_create_reason": "ACTIVE_SIGNAL_EXITED_THIS_PASS_WAIT_NEXT_PASS",
        "same_pass_reversal_allowed": False,
        "next_pass_create_policy": "ALLOW_ONLY_IF_NO_ACTIVE_SIGNAL_AT_PASS_START",
    }


def _apply_no_same_pass_exit_policy(meta_json: Dict[str, Any]) -> Dict[str, Any]:
    meta = dict(meta_json or {})
    active_ev = meta.get("active_signal_evidence") if isinstance(meta.get("active_signal_evidence"), dict) else {}
    policy = _no_same_pass_exit_policy_from_active_evidence(active_ev)
    if not policy:
        return meta

    active_ev = dict(active_ev)
    active_ev.update(policy)
    meta["active_signal_evidence"] = sanitize_json(active_ev)
    meta["no_same_pass_reversal_policy"] = sanitize_json(policy)
    meta.update(policy)
    return sanitize_json(meta)

def _with_reason_history(
    *,
    existing_signal: Optional[SignalSchema],
    lifecycle_result: Any,
    meta_json: Dict[str, Any],
    event: str,
    criteria_json: Optional[Dict[str, Any]] = None,
    snapshot_json: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Attach stable entry/current/exit reason records to signal metadata.

    Entry context should remain immutable after signal creation.  Current context
    should move forward only when it adds information.  A later HOLD/no-setup
    audit row must not erase a meaningful DOWNGRADE/WEAKENING reason.
    """
    out = dict(meta_json or {})
    old = _existing_meta(existing_signal)
    old_history = _as_dict(old.get("reason_history"))
    old_entry = old.get("entry_reason") or old_history.get("entry")
    old_current = old.get("current_reason") or old_history.get("current")
    old_exit = old.get("exit_reason") or old_history.get("exit")
    old_downgrade = old.get("downgrade_reason") or old_history.get("downgrade")

    raw_current = _reason_record(lifecycle_result)

    existing_stage = _enum_upper(getattr(existing_signal, "stage", None)) if existing_signal else ""
    result_stage = _enum_upper(getattr(lifecycle_result, "stage", None))

    preserve_meaningful_current = (
        event == "update"
        and _low_information_reason(raw_current)
        and bool(old_current)
        and (
            existing_stage == "WEAKENING"
            or result_stage == "WEAKENING"
            or bool(old_downgrade)
            or _has_meaningful_downgrade(old_current)
        )
    )
    current = old_current if preserve_meaningful_current else raw_current

    if event in {"create", "replace_create"} or not old_entry:
        entry = raw_current
    else:
        entry = old_entry

    exit_reason = current if event in {"exit", "replace_close"} else old_exit

    if _is_downgrade_action(raw_current):
        downgrade_reason = raw_current
    else:
        downgrade_reason = old_downgrade

    # Keep immutable entry payloads in metadata so a later REPLACED close can
    # preserve the original entry criteria/snapshot instead of writing the
    # opposite setup into the old row.
    old_entry_criteria = old.get("entry_criteria_json")
    old_entry_snapshot = old.get("entry_snapshot_json")
    if event in {"create", "replace_create"} or not old_entry_criteria:
        entry_criteria = criteria_json
    else:
        entry_criteria = old_entry_criteria
    if event in {"create", "replace_create"} or not old_entry_snapshot:
        entry_snapshot = snapshot_json
    else:
        entry_snapshot = old_entry_snapshot

    # setup_levels is an entry invariant for active signals.  A later evaluation
    # may discover the opposite setup while managing the existing row; that fresh
    # setup_levels must not overwrite the original setup reference level.  Only
    # new rows (create / replace_create) should take the newly discovered levels.
    old_setup_levels = old.get("setup_levels")
    if event not in {"create", "replace_create"} and isinstance(old_setup_levels, dict):
        preserved_setup_levels = sanitize_json(old_setup_levels)
        out["setup_levels"] = preserved_setup_levels
        signal_block = out.get("signal") if isinstance(out.get("signal"), dict) else {}
        signal_block["setup_levels"] = preserved_setup_levels
        out["signal"] = signal_block
    elif not isinstance(out.get("setup_levels"), dict) and isinstance(old_setup_levels, dict):
        preserved_setup_levels = sanitize_json(old_setup_levels)
        out["setup_levels"] = preserved_setup_levels
        signal_block = out.get("signal") if isinstance(out.get("signal"), dict) else {}
        signal_block["setup_levels"] = preserved_setup_levels
        out["signal"] = signal_block

    # Signal targets are deliberately not persisted.  Trade manager derives and
    # manages targets independently from the signal layer.

    current_evidence = _as_dict(meta_json.get("current_evidence"))
    old_initiated_setup = old.get("initiated_setup")
    if event in {"create", "replace_create"} or not isinstance(old_initiated_setup, dict):
        initiated_setup = _initiated_setup_from_current(current_evidence, meta_json, _as_dict(entry))
    else:
        initiated_setup = old_initiated_setup

    old_history_rows = old.get("active_evidence_history")
    history_rows = list(old_history_rows) if isinstance(old_history_rows, list) else []
    if current_evidence:
        history_rows.append(_evidence_history_record(current_evidence))
        history_rows = history_rows[-20:]

    setup_label = str(_as_dict(initiated_setup).get("setup_label") or "").strip().upper()
    if not setup_label:
        raise ValueError("Missing initiated setup_label while building signal meta")
    out["setup_label"] = setup_label
    # Explicit immutable identity for downstream consumers.  The nested
    # initiated_setup payload remains the detailed provenance record, while
    # current_evidence/active_signal_evidence are free to evolve.
    out["initiated_setup_label"] = setup_label

    signal_block = out.get("signal") if isinstance(out.get("signal"), dict) else {}
    if signal_block:
        out["signal"] = signal_block

    lifecycle_block = out.get("lifecycle") if isinstance(out.get("lifecycle"), dict) else {}
    if lifecycle_block:
        out["lifecycle"] = lifecycle_block

    out["entry_reason"] = sanitize_json(entry)
    out["current_reason"] = sanitize_json(current)
    out["initiated_setup"] = sanitize_json(initiated_setup)
    if current_evidence:
        out["current_evidence"] = sanitize_json(current_evidence)
        out["active_signal_evidence"] = sanitize_json(current_evidence.get("active_signal_evidence", {}))
    if history_rows:
        out["active_evidence_history"] = sanitize_json(history_rows)
    if event in {"exit", "replace_close"} and current_evidence:
        out["exit_evidence"] = sanitize_json(current_evidence)
    if exit_reason:
        out["exit_reason"] = sanitize_json(exit_reason)
    if downgrade_reason:
        out["downgrade_reason"] = sanitize_json(downgrade_reason)
    if isinstance(entry_criteria, dict):
        out["entry_criteria_json"] = sanitize_json(entry_criteria)
    if isinstance(entry_snapshot, dict):
        out["entry_snapshot_json"] = sanitize_json(entry_snapshot)

    out["reason_history"] = sanitize_json({
        "entry": entry,
        "current": current,
        "exit": exit_reason,
        "downgrade": downgrade_reason,
    })

    # Preserve the UI-friendly latest meaningful reason text while exposing the
    # stable records above for diagnostics and replay comparisons.  Once a row
    # is WEAKENING, a later HOLD should not make status_reason look normal again.
    raw_action = _reason_action(raw_current)
    if event == "update" and raw_action == "HOLD" and downgrade_reason and (existing_stage == "WEAKENING" or result_stage == "WEAKENING"):
        # Keep status_reason anchored to the explicit downgrade reason.  The
        # latest HOLD still remains visible in current_reason/reason_history,
        # but the top-level signal reason should not imply that a WEAKENING
        # signal has returned to normal.
        out["reason"] = _as_dict(downgrade_reason).get("text") or out.get("reason")
    else:
        out["reason"] = _as_dict(current).get("text") or out.get("reason")
    return sanitize_json(out)


def _preserve_existing_payload_for_update(existing_signal: Optional[SignalSchema], lifecycle_result: Any, updated_meta: Dict[str, Any]) -> bool:
    if not existing_signal:
        return False
    raw_current = _reason_record(lifecycle_result)
    if not _low_information_reason(raw_current):
        return False
    existing_stage = _enum_upper(getattr(existing_signal, "stage", None))
    result_stage = _enum_upper(getattr(lifecycle_result, "stage", None))
    return existing_stage == "WEAKENING" or result_stage == "WEAKENING" or bool(_as_dict(updated_meta).get("downgrade_reason"))


def _calc_signal_metrics(
    *,
    existing_signal: Optional[SignalSchema],
    side: Any,
    current_price: Any,
    current_time: Any,
) -> Dict[str, Any]:
    """Calculate side-adjusted signal analytics.

    Raw price extremes are stored as observed prices:
        max_price = highest price seen during signal life
        min_price = lowest price seen during signal life

    P&L fields are side-adjusted:
        max_pnl / max_pnl_value = MFE from the signal side
        min_pnl / min_pnl_value = MAE from the signal side (zero or negative)

    This matters for SELL signals because the favourable price extreme is
    min_price, not max_price.  The function recomputes MFE/MAE from raw
    extremes each time so it can correct older rows whose raw extremes are
    valid but whose side-adjusted P&L was previously wrong.
    """
    side_s = _enum_upper(side)
    px = _to_dec(current_price)
    if px is None:
        return {}

    created_px = (
        _to_dec(existing_signal.created_price)
        if existing_signal and existing_signal.created_price is not None
        else px
    )
    if created_px is None or created_px == 0:
        return {}

    if side_s not in {"BUY", "SELL"}:
        side_s = ""

    def _pnl_for_price(price: Decimal) -> Tuple[Decimal, Decimal]:
        if side_s == "BUY":
            pnl_value = price - created_px
        elif side_s == "SELL":
            pnl_value = created_px - price
        else:
            pnl_value = Decimal("0")
        pnl_pct = (pnl_value / created_px) * Decimal("100") if created_px else Decimal("0")
        return pnl_pct, pnl_value

    last_pnl_raw, last_pnl_value_raw = _pnl_for_price(px)
    last_pnl = last_pnl_raw.quantize(Decimal("0.0001"))
    last_pnl_value = last_pnl_value_raw.quantize(Decimal("0.01"))

    # Maintain raw price extremes.  Seed missing/zero extremes from the
    # created price so entry itself participates in MFE/MAE calculations.
    if existing_signal:
        prev_max_price = _to_dec(existing_signal.max_price) or created_px
        prev_min_price = _to_dec(existing_signal.min_price) or created_px
        prev_max_time = existing_signal.max_time or current_time
        prev_min_time = existing_signal.min_time or current_time
    else:
        prev_max_price = created_px
        prev_min_price = created_px
        prev_max_time = current_time
        prev_min_time = current_time

    max_price = prev_max_price
    min_price = prev_min_price
    max_time = prev_max_time
    min_time = prev_min_time

    if px > max_price:
        max_price = px
        max_time = current_time
    if px < min_price:
        min_price = px
        min_time = current_time

    # Side-adjusted excursions. For BUY, high is favourable and low adverse.
    # For SELL, low is favourable and high adverse.
    if side_s == "BUY":
        mfe_price = max_price
        mae_price = min_price
    elif side_s == "SELL":
        mfe_price = min_price
        mae_price = max_price
    else:
        mfe_price = px
        mae_price = px

    max_pnl_raw, max_pnl_value_raw = _pnl_for_price(mfe_price)
    min_pnl_raw, min_pnl_value_raw = _pnl_for_price(mae_price)

    return {
        "last_pnl": last_pnl,
        "last_pnl_value": last_pnl_value,
        "max_price": max_price.quantize(Decimal("0.01")),
        "min_price": min_price.quantize(Decimal("0.01")),
        "max_time": max_time,
        "min_time": min_time,
        "max_pnl": max_pnl_raw.quantize(Decimal("0.0001")),
        "min_pnl": min_pnl_raw.quantize(Decimal("0.0001")),
        "max_pnl_value": max_pnl_value_raw.quantize(Decimal("0.01")),
        "min_pnl_value": min_pnl_value_raw.quantize(Decimal("0.01")),
    }


class SignalFetcher:
    def fetch_symbol(self, symbol: str) -> Optional[SymbolSchema]:
        try:
            return SymbolSchema.fetch_symbol(symbol)
        except Exception:
            logger.exception("Failed to fetch symbol | %s", symbol)
            return None

    def fetch_active_signal(self, equity_ref: str, lifecycle: str) -> Optional[SignalSchema]:
        try:
            return SignalSchema.fetch_active_signal(equity_ref, lifecycle)
        except Exception:
            logger.exception("Failed to fetch active signal | equity_ref=%s lifecycle=%s", equity_ref, lifecycle)
            return None


class SignalPersister:
    def create(self, *, snapshot: SnapshotSchema, equity_ref: str, lifecycle: str, side: SignalSide, stage: LifecycleStage,
               reason: Optional[str], criteria_json: Dict[str, Any], snapshot_json: Dict[str, Any], meta_json: Dict[str, Any],
               analytics: Dict[str, Any]) -> SignalSchema:
        setup_label = _required_setup_label(meta_json)
        return SignalSchema.create_signal(
            equity_ref=equity_ref,
            symbol=snapshot.symbol,
            lifecycle=lifecycle,
            setup=setup_label,
            side=side,
            stage=stage,
            status=SignalStatus.OPEN,
            status_reason=reason,
            last_eval_time=snapshot.snapshot_time,
            last_snapshot_time=snapshot.snapshot_time,
            criteria_json=criteria_json,
            snapshot_json=snapshot_json,
            meta_json=meta_json,
            last_price=_to_dec(getattr(snapshot, "close", None)),
            ltp=_to_dec(getattr(snapshot, "ltp", None)),
            ltp_time=getattr(snapshot, "ltp_time", None),
            **analytics,
        )

    def update(self, *, signal: SignalSchema, snapshot: SnapshotSchema, stage: LifecycleStage, reason: Optional[str],
               criteria_json: Dict[str, Any], snapshot_json: Dict[str, Any], meta_json: Dict[str, Any],
               analytics: Dict[str, Any]) -> Optional[SignalSchema]:
        return SignalSchema.update_signal(
            signal_id=signal.signal_id,
            stage=stage,
            status=SignalStatus.OPEN,
            status_reason=reason,
            last_eval_time=snapshot.snapshot_time,
            last_snapshot_time=snapshot.snapshot_time,
            criteria_json=criteria_json,
            snapshot_json=snapshot_json,
            meta_json=meta_json,
            last_price=_to_dec(getattr(snapshot, "close", None)),
            ltp=_to_dec(getattr(snapshot, "ltp", None)),
            ltp_time=getattr(snapshot, "ltp_time", None),
            **analytics,
        )

    def close(self, *, signal: SignalSchema, snapshot: SnapshotSchema, status: SignalStatus, reason: Optional[str],
              criteria_json: Dict[str, Any], snapshot_json: Dict[str, Any], meta_json: Dict[str, Any],
              analytics: Dict[str, Any]) -> Optional[SignalSchema]:
        return SignalSchema.close_signal(
            signal_id=signal.signal_id,
            status=status,
            reason=reason,
            ts=snapshot.snapshot_time,
            last_eval_time=snapshot.snapshot_time,
            last_snapshot_time=snapshot.snapshot_time,
            criteria_json=criteria_json,
            snapshot_json=snapshot_json,
            meta_json=meta_json,
            last_price=_to_dec(getattr(snapshot, "close", None)),
            ltp=_to_dec(getattr(snapshot, "ltp", None)),
            ltp_time=getattr(snapshot, "ltp_time", None),
            **analytics,
        )

class SignalAssembler:
    def __init__(self):
        self.lifecycle = DEFAULT_LIFECYCLE
        self.fetcher = SignalFetcher()
        self.persister = SignalPersister()
        self.evaluator = EvidenceEvaluator()
        self.adapter = EvidenceLifecycleAdapter()
        self.stock_advisor = StockAdvisor()

    def _active_trade_context(self, equity_ref: str) -> Dict[str, Any]:
        """Return symbol-level active trade context for lifecycle evaluation.

        Signal generation runs once per symbol/lifecycle, while trades are user
        rows. Without this context, an active trade can be hidden from the
        lifecycle decision helper and an opposite snapshot may replace the
        signal too aggressively.
        """
        try:
            rows = UserTradeSchema.fetch_active_trades_for_equity_ref(equity_ref=equity_ref) or []
        except Exception:
            logger.exception("failed to fetch active trade context | equity_ref=%s", equity_ref)
            rows = []

        sides = sorted({
            _enum_upper(getattr(row, "trade_type", None))
            for row in rows
            if _enum_upper(getattr(row, "trade_type", None)) in {"BUY", "SELL"}
        })

        if len(sides) == 1:
            open_trade_side = sides[0]
        elif len(sides) > 1:
            open_trade_side = "MIXED"
        else:
            open_trade_side = ""

        return {
            "open_trade_side": open_trade_side,
            "open_trade_sides": sides,
            "open_trade_count": len(rows),
        }


    def _existing_context(self, existing_signal: Optional[SignalSchema], equity_ref: str = "", symbol: str = "", ts: Optional[datetime] = None) -> Dict[str, Any]:
        ctx: Dict[str, Any] = {}
        if existing_signal:
            ctx.update({
                "open_signal_side": _enum_upper(existing_signal.side),
                "open_signal_stage": _enum_upper(existing_signal.stage),
                "open_signal_status": _enum_upper(existing_signal.status),
                "open_signal_id": getattr(existing_signal, "signal_id", None),
            })
            # Do not place the full previous meta_json into evidence input.
            # meta_json itself stores evidence details; copying it into the next
            # evaluation causes recursive JSON growth on every HOLD/UPDATE.
            meta = getattr(existing_signal, "meta_json", None)
            if isinstance(meta, dict):
                current_reason = meta.get("current_reason") if isinstance(meta.get("current_reason"), dict) else {}
                entry_reason = meta.get("entry_reason") if isinstance(meta.get("entry_reason"), dict) else {}
                ctx["open_signal_current_reason_code"] = current_reason.get("code")
                ctx["open_signal_entry_reason_code"] = entry_reason.get("code")
                setup_levels = _resolve_open_signal_setup_levels(existing_signal)
                if setup_levels:
                    ctx["open_signal_setup_levels"] = setup_levels
                signal_block = meta.get("signal") if isinstance(meta.get("signal"), dict) else {}
                ctx["open_signal_strategy"] = signal_block.get("strategy") or meta.get("strategy")
                ctx["open_signal_setup_label"] = getattr(existing_signal, "setup", None) or signal_block.get("setup_label") or meta.get("setup_label")
        if equity_ref:
            ctx.update(self._active_trade_context(equity_ref))
        return ctx

    def _evidence_payload(self, lifecycle_result: Any) -> Dict[str, Any]:
        meta = getattr(lifecycle_result, "meta", None)
        if not isinstance(meta, dict):
            raise ValueError("Evidence lifecycle result missing meta")
        evidence = meta.get("evidence_result")
        if not isinstance(evidence, dict):
            raise ValueError("Evidence lifecycle result missing evidence_result payload")
        return evidence

    def _criteria_json(self, *, lifecycle_result: Any) -> Dict[str, Any]:
        evidence = self._evidence_payload(lifecycle_result)
        setup_decision = _setup_decision_from_evidence(evidence)
        compact_setup_decision = _compact_setup_decision(setup_decision)
        current_evidence = _current_evidence_record(evidence, lifecycle_result)
        return sanitize_json({
            "lifecycle": self.lifecycle,
            "lifecycle_version": getattr(lifecycle_result, "lifecycle_version", None),
            "engine_name": EVIDENCE_CONFIG.engine_name,
            "engine_version": EVIDENCE_CONFIG.engine_version,
            "stage": _enum_upper(lifecycle_result.stage),
            "side": _enum_upper(lifecycle_result.side),
            "confidence": lifecycle_result.confidence,
            "quality": _enum_upper(lifecycle_result.quality),
            "signal_action": _enum_upper(lifecycle_result.signal_action),
            "signal_state": getattr(lifecycle_result, "signal_state", ""),
            "signal_reason": getattr(lifecycle_result, "signal_reason", ""),
            "preferred_side": evidence["preferred_side"],
            "preferred_opportunity_score": evidence["preferred_opportunity_score"],
            "opposite_pressure": evidence["opposite_pressure"],
            "market_condition": evidence["market_condition"],
            "strategy": evidence["strategy"],
            "setup_label": evidence["setup_label"],
            "primary_pattern": evidence["primary_pattern"],
            "entry_permission": evidence["entry_permission"],
            "evaluator_state": evidence.get("evaluator_state"),
            "decision": evidence.get("decision"),
            "price_action_confirmed": evidence.get("price_action_confirmed"),
            "price_action_strength": evidence.get("price_action_strength"),
            "blocked_by": evidence.get("blocked_by"),
            "risk_flags": evidence.get("risk_flags", []),
            "stock_advisor": sanitize_json(evidence.get("stock_advisor")) if isinstance(evidence.get("stock_advisor"), dict) else {},
            "stock_advisor_gate": sanitize_json(evidence.get("details", {}).get("stock_advisor_gate")) if isinstance(evidence.get("details"), dict) and isinstance(evidence.get("details", {}).get("stock_advisor_gate"), dict) else {},
            "discovered_setups": evidence.get("discovered_setups", []),
            "confirmed_setups": evidence.get("confirmed_setups", []),
            "supporting_setups": evidence.get("supporting_setups", []),
            "setup_levels": evidence.get("details", {}).get("setup_levels") if isinstance(evidence.get("details"), dict) else None,
            "setup_decision": compact_setup_decision,
            "active_signal_evidence": compact_setup_decision.get("active_signal_evidence", {}),
            "current_evidence": current_evidence,
            "buy": {
                "opportunity_score": evidence["buy"]["opportunity_score"],
                "continuation_quality": evidence["buy"]["continuation_quality"],
            },
            "sell": {
                "opportunity_score": evidence["sell"]["opportunity_score"],
                "continuation_quality": evidence["sell"]["continuation_quality"],
            },
        })

    def _meta_json(self, *, lifecycle_result: Any) -> Dict[str, Any]:
        evidence = self._evidence_payload(lifecycle_result)
        code, text = _evidence_reason_code_text(lifecycle_result)
        setup_decision = _setup_decision_from_evidence(evidence)
        compact_setup_decision = _compact_setup_decision(setup_decision)
        current_evidence = _current_evidence_record(evidence, lifecycle_result)
        return sanitize_json({
            "reason": text,
            "engine": {
                "name": EVIDENCE_CONFIG.engine_name,
                "version": EVIDENCE_CONFIG.engine_version,
            },
            "setup_levels": evidence.get("details", {}).get("setup_levels") if isinstance(evidence.get("details"), dict) else None,
            "signal": {
                "stage": _enum_upper(lifecycle_result.stage),
                "side": _enum_upper(lifecycle_result.side),
                "confidence": lifecycle_result.confidence,
                "quality": _enum_upper(lifecycle_result.quality),
                "signal_action": _enum_upper(lifecycle_result.signal_action),
                "signal_state": getattr(lifecycle_result, "signal_state", ""),
                "signal_reason": getattr(lifecycle_result, "signal_reason", ""),
                "strategy": evidence["strategy"],
                "setup_label": evidence["setup_label"],
                "primary_pattern": evidence["primary_pattern"],
                "evaluator_state": evidence.get("evaluator_state"),
                "decision": evidence.get("decision"),
                "price_action_confirmed": evidence.get("price_action_confirmed"),
                "price_action_strength": evidence.get("price_action_strength"),
                "blocked_by": evidence.get("blocked_by"),
                "risk_flags": evidence.get("risk_flags", []),
                "setup_levels": evidence.get("details", {}).get("setup_levels") if isinstance(evidence.get("details"), dict) else None,
            },
            "lifecycle": {
                "stage": _enum_upper(lifecycle_result.stage),
                "side": _enum_upper(lifecycle_result.side),
                "confidence": lifecycle_result.confidence,
                "quality": _enum_upper(lifecycle_result.quality),
                "signal_action": _enum_upper(lifecycle_result.signal_action),
                "signal_state": getattr(lifecycle_result, "signal_state", ""),
                "signal_reason": getattr(lifecycle_result, "signal_reason", ""),
            },
            "strategy": evidence["strategy"],
            "setup_label": evidence["setup_label"],
            "primary_pattern": evidence["primary_pattern"],
            "stock_advisor": sanitize_json(evidence.get("stock_advisor")) if isinstance(evidence.get("stock_advisor"), dict) else {},
            "stock_advisor_gate": sanitize_json(evidence.get("details", {}).get("stock_advisor_gate")) if isinstance(evidence.get("details"), dict) and isinstance(evidence.get("details", {}).get("stock_advisor_gate"), dict) else {},
            "evidence_reason": {
                "code": code,
                "text": text,
            },
            "setup_decision": compact_setup_decision,
            "active_signal_evidence": compact_setup_decision.get("active_signal_evidence", {}),
            "current_evidence": current_evidence,
            "evidence": evidence,
            "supports": _reasons_to_list(lifecycle_result.supports),
            "warnings": _reasons_to_list(lifecycle_result.warnings),
            "conflicts": _reasons_to_list(lifecycle_result.conflicts),
        })

    def _mark_setup_state_consumed(self, *, snapshot: SnapshotSchema, signal: SignalSchema, meta_json: Dict[str, Any]) -> None:
        cfg = getattr(EVIDENCE_CONFIG, "setup_state", None)
        if not cfg or not bool(getattr(cfg, "enabled", False)) or not bool(getattr(cfg, "write_enabled", False)):
            return

        ts = getattr(snapshot, "snapshot_time", None)
        if not isinstance(ts, datetime):
            raise ValueError("setup-state consumption requires snapshot.snapshot_time")

        setup = str(getattr(signal, "setup", None) or meta_json.get("setup_label") or "").strip().upper()
        side = _enum_upper(getattr(signal, "side", None))
        equity_ref = str(getattr(signal, "equity_ref", None) or getattr(snapshot, "symbol", "") or "").strip().upper()
        signal_id = str(getattr(signal, "signal_id", "") or "").strip()
        if not setup or not side or not equity_ref or not signal_id:
            return

        event_context = _setup_state_event_context_from_meta(
            meta_json,
            setup=setup,
            side=side,
        )
        if not event_context.get("event_key"):
            raise ValueError(
                "SETUP_STATE_EVENT_IDENTITY_MISSING "
                f"signal_id={signal_id} setup={setup} side={side}"
            )

        try:
            StockSetupStateSchema.mark_consumed_for_signal(
                trading_day=ts.date(),
                equity_ref=equity_ref,
                symbol=str(getattr(snapshot, "symbol", "") or equity_ref).strip().upper(),
                setup=setup,
                side=side,
                signal_id=signal_id,
                ts=ts,
                state=getattr(cfg, "consumed_state", "CONSUMED"),
                reason=getattr(cfg, "consumed_reason_code", "SETUP_STATE_CONSUMED"),
                event_key=event_context.get("event_key"),
                event_source=event_context.get("event_source"),
                event_time=event_context.get("event_time"),
                reference_price=event_context.get("reference_price"),
                reference_source=event_context.get("reference_source"),
                state_json_update={
                    "event_context": event_context,
                    "consumed_signal": {
                        "signal_id": signal_id,
                        "setup": setup,
                        "side": side,
                        "snapshot_time": ts.isoformat(),
                        "created_price": str(getattr(signal, "created_price", None) or getattr(snapshot, "close", None) or ""),
                        "event_key": event_context.get("event_key"),
                        "reference_id": event_context.get("reference_id"),
                        "level_type": event_context.get("level_type"),
                        "level_price": event_context.get("level_price"),
                        "acceptance_path": event_context.get("acceptance_path"),
                    }
                },
            )
        except Exception:
            if bool(getattr(cfg, "fail_silently", True)):
                logger.warning(
                    "setup_state consumed transition failed | signal_id=%s equity_ref=%s setup=%s side=%s",
                    signal_id, equity_ref, setup, side, exc_info=True,
                )
                return
            raise

    def _mark_opposite_exhaustion_confirmed_pending(
        self,
        *,
        snapshot: SnapshotSchema,
        existing_signal: SignalSchema,
        lifecycle_result: Any,
    ) -> None:
        """Retain confirmed opposite exhaustion for next-pass creation.

        Same-pass reversal remains disabled. This transition is written only
        when an active ACCEPTED_BREAKOUT was closed while the normalized active
        evidence identifies an opposite confirmed EXHAUSTION_REVERSAL.
        """
        cfg = getattr(EVIDENCE_CONFIG, "setup_state", None)
        if (
            not cfg
            or not bool(getattr(cfg, "enabled", False))
            or not bool(getattr(cfg, "write_enabled", False))
            or not bool(getattr(cfg, "confirmed_pending_enabled", False))
        ):
            return
        if _enum_upper(getattr(existing_signal, "setup", None)) != _enum_upper(
            EVIDENCE_CONFIG.pattern.setup_accepted_breakout
        ):
            return

        meta = getattr(lifecycle_result, "meta", None)
        evidence = meta.get("evidence_result") if isinstance(meta, dict) else None
        details = evidence.get("details") if isinstance(evidence, dict) and isinstance(evidence.get("details"), dict) else {}
        setup_decision = details.get("setup_decision") if isinstance(details.get("setup_decision"), dict) else {}
        active_ev = setup_decision.get("active_signal_evidence") if isinstance(setup_decision.get("active_signal_evidence"), dict) else {}
        top_opp = active_ev.get("top_opposite_candidate") if isinstance(active_ev.get("top_opposite_candidate"), dict) else {}

        if _enum_upper(top_opp.get("setup_label")) != _enum_upper(
            EVIDENCE_CONFIG.pattern.setup_exhaustion_reversal
        ):
            return
        if not bool(top_opp.get("price_action_confirmed")):
            return

        ts = getattr(snapshot, "snapshot_time", None)
        if not isinstance(ts, datetime):
            raise ValueError("EXHAUSTION_REVERSAL pending-state retention requires snapshot.snapshot_time")

        event_time = to_ist_naive(
            top_opp.get("event_time")
            or top_opp.get("watch_snapshot_time")
            or ((_as_dict(top_opp.get("setup_levels"))).get("watch_snapshot_time"))
        )
        valid_minutes = float(
            getattr(EVIDENCE_CONFIG.exhaustion_reversal, "watch_extreme_valid_minutes", 15.5) or 15.5
        )
        now_naive = to_ist_naive(ts)
        if event_time is None or now_naive is None:
            logger.warning(
                "SIG_EXHAUSTION_PENDING_SKIP | %s | reason=missing_event_time",
                getattr(snapshot, "symbol", None),
            )
            return
        age_minutes = (now_naive - event_time).total_seconds() / 60.0
        if age_minutes < 0 or age_minutes > valid_minutes:
            logger.info(
                "SIG_EXHAUSTION_PENDING_SKIP | %s @ %s | reason=stale_event event_time=%s age_minutes=%.2f valid_minutes=%.2f",
                getattr(snapshot, "symbol", None), ts, event_time, age_minutes, valid_minutes,
            )
            return

        equity_ref = str(
            getattr(existing_signal, "equity_ref", None)
            or getattr(snapshot, "symbol", "")
            or ""
        ).strip().upper()
        opposite_side = _enum_upper(top_opp.get("side"))
        if not equity_ref or opposite_side not in {"BUY", "SELL"}:
            return

        valid_bars = max(1, int(getattr(cfg, "confirmed_pending_valid_bars", 1) or 1))
        # One 3-minute next-pass window plus a small boundary tolerance.
        expires_at = ts + timedelta(minutes=(valid_bars * 3.0) + 0.5)
        reason_code = getattr(
            EVIDENCE_CONFIG.reason,
            "exhaustion_confirmed_pending_code",
            "EXHAUSTION_REVERSAL_CONFIRMED_PENDING_NEXT_PASS",
        )
        payload = {
            "confirmed_pending": {
                "reason_code": reason_code,
                "snapshot_time": ts.isoformat(),
                "event_time": event_time.isoformat(),
                "event_age_minutes": round(age_minutes, 2),
                "active_signal_id": str(getattr(existing_signal, "signal_id", "") or ""),
                "active_signal_setup": _enum_upper(getattr(existing_signal, "setup", None)),
                "active_signal_side": _enum_upper(getattr(existing_signal, "side", None)),
                "opposite_candidate": sanitize_json(top_opp),
                "entry_ready_at_confirmation": bool(top_opp.get("entry_ready")),
                "same_pass_reversal_allowed": False,
                "next_pass_create_policy": "ALLOW_ONLY_IF_STILL_TIMELY_AND_EVIDENCE_READY",
                "expires_at": expires_at.isoformat(),
            }
        }

        try:
            StockSetupStateSchema.transition_state(
                trading_day=ts.date(),
                equity_ref=equity_ref,
                symbol=str(getattr(snapshot, "symbol", "") or equity_ref).strip().upper(),
                setup=EVIDENCE_CONFIG.pattern.setup_exhaustion_reversal,
                side=opposite_side,
                state=getattr(cfg, "confirmed_pending_state", "CONFIRMED_PENDING"),
                state_reason=reason_code,
                ts=ts,
                expires_at=expires_at,
                state_json_update=payload,
            )
            logger.info(
                "SIG_EXHAUSTION_CONFIRMED_PENDING | %s @ %s | side=%s active_signal_id=%s expires_at=%s",
                equity_ref,
                ts,
                opposite_side,
                getattr(existing_signal, "signal_id", None),
                expires_at,
            )
        except Exception:
            if bool(getattr(cfg, "fail_silently", True)):
                logger.warning(
                    "failed to retain confirmed exhaustion pending | equity_ref=%s side=%s",
                    equity_ref,
                    opposite_side,
                    exc_info=True,
                )
                return
            raise

    def _audit(self, *, symbol: str, ts: Any, result: Any, existing_signal: Optional[SignalSchema], persisted_signal: Optional[SignalSchema], analytics: Dict[str, Any], snapshot: SnapshotSchema) -> None:
        if not SIGNAL_CONFIG.audit.enabled:
            return
        try:
            entity = persisted_signal or existing_signal
            # Services emit candidate lifecycle observations.  The central
            # audit policy decides whether to persist a transition, reason
            # change, or sampled DEBUGGING heartbeat.
            if entity is None:
                return

            new_stage = _enum_upper(getattr(result, "stage", None))
            new_action = _enum_upper(getattr(result, "signal_action", None))
            reason_code, reason_text = _evidence_reason_code_text(result)

            old_stage = _enum_upper(getattr(existing_signal, "stage", None)) if existing_signal else None

            result_meta = getattr(result, "meta", {}) or {}
            evidence_payload = result_meta.get("evidence_result") if isinstance(result_meta.get("evidence_result"), dict) else {}
            setup_decision = _setup_decision_from_evidence(evidence_payload) if evidence_payload else {}
            compact_setup_decision = _compact_setup_decision(setup_decision) if setup_decision else {}
            current_evidence = _current_evidence_record(evidence_payload, result) if evidence_payload else {}
            compact_evidence = _compact_evidence_for_audit(evidence_payload) if evidence_payload else {}
            active_ev = _as_dict(compact_setup_decision.get("active_signal_evidence"))
            primary_candidate = _as_dict(compact_setup_decision.get("primary_candidate"))
            active_summary = sanitize_json({
                "active_evidence_action": active_ev.get("active_evidence_action") or active_ev.get("evidence_action"),
                "reason_code": active_ev.get("reason_code"),
                "exit_pressure": active_ev.get("exit_pressure"),
                "trail_mode": active_ev.get("trail_mode"),
                "target_expansion_allowed": active_ev.get("target_expansion_allowed"),
                "should_exit_signal": active_ev.get("should_exit_signal"),
                "support_score": active_ev.get("support_score"),
                "opposition_score": active_ev.get("opposition_score"),
                "same_side_entry_ready_count": active_ev.get("same_side_entry_ready_count"),
                "opposite_entry_ready_count": active_ev.get("opposite_entry_ready_count"),
                "top_same_side_candidate": active_ev.get("top_same_side_candidate"),
                "top_opposite_candidate": active_ev.get("top_opposite_candidate"),
            })
            write_auditlog(
                entity_type=SIGNAL_CONFIG.audit.entity_type,
                entity_id=getattr(entity, "signal_id", None) if entity else None,
                symbol=symbol,
                evaluation_stage=SIGNAL_CONFIG.audit.evaluation_stage,
                previous_state=old_stage,
                new_state=new_stage,
                action=new_action,
                reason_code=reason_code,
                reason_text=reason_text,
                confidence=getattr(result, "confidence", None),
                ts=ts if isinstance(ts, datetime) else None,
                payload_json={
                    "lifecycle": self.lifecycle,
                    "side": _enum_upper(getattr(result, "side", None)),
                    "quality": _enum_upper(getattr(result, "quality", None)),
                    "signal_state": getattr(result, "signal_state", ""),
                    "signal_id": getattr(entity, "signal_id", None) if entity else None,
                    "existing_signal_id": getattr(existing_signal, "signal_id", None) if existing_signal else None,
                    "snapshot_time": getattr(snapshot, "snapshot_time", None),
                    "close": getattr(snapshot, "close", None),
                    "analytics": analytics,
                    "strategy": compact_evidence.get("strategy"),
                    "originating_setup": getattr(entity, "setup", None),
                    "current_setup_label": compact_evidence.get("setup_label"),
                    "evaluator_state": compact_evidence.get("evaluator_state"),
                    "decision": compact_evidence.get("decision"),
                    "entry_permission": compact_evidence.get("entry_permission"),
                    "price_action_confirmed": compact_evidence.get("price_action_confirmed"),
                    "price_action_strength": compact_evidence.get("price_action_strength"),
                    "blocked_by": compact_evidence.get("blocked_by"),
                    "risk_flags": compact_evidence.get("risk_flags", []),
                    "setup_levels": compact_evidence.get("setup_levels"),
                    "active_price_action": compact_evidence.get("active_price_action"),
                    "setup_counts": compact_setup_decision.get("setup_counts", {}),
                    "candidate_count": compact_setup_decision.get("candidate_count"),
                    "confirmed_candidate_count": compact_setup_decision.get("confirmed_candidate_count"),
                    "entry_ready_candidate_count": compact_setup_decision.get("entry_ready_candidate_count"),
                    "primary_candidate": compact_setup_decision.get("primary_candidate"),
                    "setup_event_key": primary_candidate.get("event_key"),
                    "setup_event_source": primary_candidate.get("event_source"),
                    "setup_event_time": primary_candidate.get("event_time"),
                    "active_signal_evidence": active_summary,
                    **_no_same_pass_exit_policy_from_active_evidence(active_summary),
                    "stock_advisor_gate": compact_evidence.get("stock_advisor_gate", {}),
                    "current_reason": _as_dict(current_evidence.get("reason")),
                    "evidence_reason": sanitize_json(getattr(result, "meta", {}).get("evidence_reason")),
                },
            )
        except Exception:
            logger.debug("signal audit failed", exc_info=True)

    def assemble(self, snapshot: SnapshotSchema) -> List[Tuple[str, SignalSchema]]:
        events: List[Tuple[str, SignalSchema]] = []
        symbol = str(getattr(snapshot, "symbol", "") or "").strip()
        ts = getattr(snapshot, "snapshot_time", None)

        if not symbol or not isinstance(ts, datetime):
            logger.info("SIG_SKIP | %s | reason=invalid_snapshot", symbol or "?")
            return events

        sym_rec = self.fetcher.fetch_symbol(symbol)
        if not sym_rec:
            logger.info("SIG_SKIP | %s @ %s | reason=no_symbol_record", symbol, ts)
            return events
        if not bool(getattr(sym_rec, "active", True)):
            logger.info("SIG_SKIP | %s @ %s | reason=inactive", symbol, ts)
            return events
        if not bool(getattr(sym_rec, "generate_signals", True)):
            logger.info("SIG_SKIP | %s @ %s | reason=generate_signals_disabled", symbol, ts)
            return events
        if not bool(getattr(snapshot, "gen_signals", False)):
            logger.info("SIG_SKIP | %s @ %s | reason=snapshot_generate_signals_disabled", symbol, ts)
            return events

        equity_ref = str(getattr(sym_rec, "equity_ref", None) or symbol).strip().upper()
        existing_signal = self.fetcher.fetch_active_signal(equity_ref, self.lifecycle)
        snapshot_json = sanitize_json(_safe_model_dump(snapshot))

        existing_context = self._existing_context(existing_signal, equity_ref, symbol, ts)
        evidence_result = self.evaluator.evaluate(snapshot=snapshot, existing_context=existing_context)
        result = self.adapter.adapt(evidence_result, existing_context=existing_context)

        # Evidence owns setup confirmation and exact reference selection. Advisor
        # runs afterwards and evaluates only stock/day/family/side suitability for
        # that normalized candidate.
        advisor_candidate = _stock_advisor_candidate_context(result)
        stock_advisor_result = (
            self.stock_advisor.analyze(
                snapshot,
                recent_snapshots=None,
                candidate_context=advisor_candidate,
            )
            if _stock_advisor_enabled()
            else None
        )
        _attach_stock_advisor_to_lifecycle_result(result, stock_advisor_result)
        _apply_stock_advisor_hard_gate(
            lifecycle_result=result,
            advisor_result=stock_advisor_result,
            symbol=equity_ref,
            snapshot_time=ts,
            candidate_price=getattr(snapshot, "close", None),
        )
        signal_action = result.signal_action
        signal_side = _side_to_signal_side(result.side)

        if signal_action == SignalAction.CREATE and not _fresh_signal_window_valid(ts):
            code, text = _fresh_signal_window_reason(ts)
            result.set_signal(SignalAction.WATCH, "WATCH", code)
            result.add_warning(code, text, 0.0, {"snapshot_time": ts.isoformat()})
            result.meta["evidence_reason"] = {"code": code, "text": text, "snapshot_time": ts.isoformat()}
            signal_action = result.signal_action
            signal_side = _side_to_signal_side(result.side)

        criteria_json = self._criteria_json(lifecycle_result=result)
        meta_json = self._meta_json(lifecycle_result=result)
        reason = meta_json.get("reason")
        analytics: Dict[str, Any] = {}

        persisted: Optional[SignalSchema] = None

        if signal_action == SignalAction.WATCH:
            if existing_signal:
                analytics = _calc_signal_metrics(existing_signal=existing_signal, side=existing_signal.side, current_price=getattr(snapshot, "close", None), current_time=ts)
            self._audit(symbol=equity_ref, ts=ts, result=result, existing_signal=existing_signal, persisted_signal=None, analytics=analytics, snapshot=snapshot)
            setup_for_log = _setup_label_for_log(meta_json, result)
            logger.info("SIG_WATCH | %s @ %s | setup=%s stage=%s side=%s conf=%s", equity_ref, ts, setup_for_log, _enum_upper(result.stage), _enum_upper(result.side), result.confidence)
            return events

        if signal_action == SignalAction.CREATE:
            if signal_side is None:
                logger.info("SIG_CREATE_SKIP | %s @ %s | reason=no_directional_side", equity_ref, ts)
                return events
            analytics = _calc_signal_metrics(existing_signal=None, side=result.side, current_price=getattr(snapshot, "close", None), current_time=ts)
            create_meta_json = _with_reason_history(
                existing_signal=None,
                lifecycle_result=result,
                meta_json=meta_json,
                event="create",
                criteria_json=criteria_json,
                snapshot_json=snapshot_json,
            )
            reason = create_meta_json.get("reason") or reason
            persisted = self.persister.create(snapshot=snapshot, equity_ref=equity_ref, lifecycle=self.lifecycle, side=signal_side, stage=result.stage, reason=reason, criteria_json=criteria_json, snapshot_json=snapshot_json, meta_json=create_meta_json, analytics=analytics)
            self._mark_setup_state_consumed(snapshot=snapshot, signal=persisted, meta_json=create_meta_json)
            events.append(("CREATE", persisted))

        elif signal_action in {SignalAction.UPDATE, SignalAction.HOLD, SignalAction.PROMOTE, SignalAction.DOWNGRADE}:
            if not existing_signal:
                logger.info("SIG_%s_SKIP | %s @ %s | reason=no_existing_signal", _enum_upper(signal_action), equity_ref, ts)
                return events
            analytics = _calc_signal_metrics(existing_signal=existing_signal, side=existing_signal.side, current_price=getattr(snapshot, "close", None), current_time=ts)
            update_meta_json = _with_reason_history(
                existing_signal=existing_signal,
                lifecycle_result=result,
                meta_json=meta_json,
                event="update",
                criteria_json=criteria_json,
                snapshot_json=snapshot_json,
            )
            reason = update_meta_json.get("reason") or reason
            if _preserve_existing_payload_for_update(existing_signal, result, update_meta_json):
                write_criteria_json = getattr(existing_signal, "criteria_json", None) or criteria_json
                write_snapshot_json = getattr(existing_signal, "snapshot_json", None) or snapshot_json
            else:
                write_criteria_json = criteria_json
                write_snapshot_json = snapshot_json
            persisted = self.persister.update(signal=existing_signal, snapshot=snapshot, stage=result.stage, reason=reason, criteria_json=write_criteria_json, snapshot_json=write_snapshot_json, meta_json=update_meta_json, analytics=analytics)
            if persisted:
                events.append((_enum_upper(signal_action), persisted))

        elif signal_action == SignalAction.INVALIDATE:
            if not existing_signal:
                logger.info("SIG_INVALIDATE_SKIP | %s @ %s | reason=no_existing_signal", equity_ref, ts)
                return events
            analytics = _calc_signal_metrics(existing_signal=existing_signal, side=existing_signal.side, current_price=getattr(snapshot, "close", None), current_time=ts)
            close_meta_json = _with_reason_history(
                existing_signal=existing_signal,
                lifecycle_result=result,
                meta_json=meta_json,
                event="exit",
                criteria_json=criteria_json,
                snapshot_json=snapshot_json,
            )
            close_meta_json = _apply_no_same_pass_exit_policy(close_meta_json)
            reason = close_meta_json.get("reason") or reason
            persisted = self.persister.close(signal=existing_signal, snapshot=snapshot, status=SignalStatus.INVALIDATED, reason=reason or "lifecycle_invalidated", criteria_json=criteria_json, snapshot_json=snapshot_json, meta_json=close_meta_json, analytics=analytics)
            if persisted:
                self._mark_opposite_exhaustion_confirmed_pending(
                    snapshot=snapshot,
                    existing_signal=existing_signal,
                    lifecycle_result=result,
                )
                events.append(("INVALIDATE", persisted))

        elif signal_action == SignalAction.INVALIDATE_OPPOSITE:
            # Evidence V2 never creates the opposite side in the same pass.
            # Close the existing signal now; a fresh opposite signal may only be
            # created on a later pass if it is still valid and no active signal
            # existed at that pass start.
            if not existing_signal:
                logger.info("SIG_INVALIDATE_OPPOSITE_SKIP | %s @ %s | reason=no_existing_signal", equity_ref, ts)
                return events
            old_analytics = _calc_signal_metrics(existing_signal=existing_signal, side=existing_signal.side, current_price=getattr(snapshot, "close", None), current_time=ts)
            old_close_meta_json = _with_reason_history(
                existing_signal=existing_signal,
                lifecycle_result=result,
                meta_json=meta_json,
                event="exit",
                criteria_json=criteria_json,
                snapshot_json=snapshot_json,
            )
            old_close_meta_json = _apply_no_same_pass_exit_policy(old_close_meta_json)
            old_close_meta_json.setdefault("blocked_create_reason", "ACTIVE_SIGNAL_EXITED_THIS_PASS_WAIT_NEXT_PASS")
            old_close_meta_json.setdefault("new_signal_blocked_until_next_pass", True)
            old_close_meta_json.setdefault("active_signal_exited_this_pass", True)
            old_close_meta_json.setdefault("same_pass_reversal_allowed", False)
            old_close_meta_json.setdefault("next_pass_create_policy", "ALLOW_ONLY_IF_NO_ACTIVE_SIGNAL_AT_PASS_START")
            reason = old_close_meta_json.get("reason") or reason
            old_row = self.persister.close(
                signal=existing_signal,
                snapshot=snapshot,
                status=SignalStatus.INVALIDATED,
                reason=reason or "opposite_setup_confirmed_wait_next_pass",
                criteria_json=criteria_json,
                snapshot_json=snapshot_json,
                meta_json=old_close_meta_json,
                analytics=old_analytics,
            )
            if old_row:
                self._mark_opposite_exhaustion_confirmed_pending(
                    snapshot=snapshot,
                    existing_signal=existing_signal,
                    lifecycle_result=result,
                )
                events.append(("INVALIDATE_OPPOSITE_CLOSE", old_row))
            persisted = old_row
            analytics = old_analytics

        else:
            logger.info("SIG_NO_ACTION | %s @ %s | action=%s", equity_ref, ts, _enum_upper(signal_action))
            return events

        self._audit(symbol=equity_ref, ts=ts, result=result, existing_signal=existing_signal, persisted_signal=persisted, analytics=analytics, snapshot=snapshot)
        setup_for_log = _setup_label_for_log(meta_json, result, persisted)
        logger.info("SIG_%s | %s @ %s | setup=%s signal_id=%s side=%s stage=%s conf=%s", events[-1][0] if events else _enum_upper(signal_action), equity_ref, ts, setup_for_log, getattr(persisted, "signal_id", None), _enum_upper(result.side), _enum_upper(result.stage), result.confidence)
        return events


class SignalGenerator:
    def __init__(self, snapshot: SnapshotSchema):
        self.snapshot = snapshot
        self.assembler = SignalAssembler()

    def generate_signal(self) -> Optional[str]:
        try:
            events = self.assembler.assemble(self.snapshot)
            if not events:
                return None
            return events[-1][0]
        except Exception:
            logger.exception("generate_signal crashed for %s", getattr(self.snapshot, "symbol", "?"))
            return None

    def generate(self) -> Optional[str]:
        return self.generate_signal()
