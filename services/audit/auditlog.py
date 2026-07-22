# services/audit/auditlog.py

from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from datetime import datetime, timedelta
from decimal import Decimal
from threading import Lock
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import text

from configs.audit_config import AUDIT_CONFIG
from database.database import get_trades_db
from models.trade_models import AuditLog
from utils.json_utils import sanitize_json

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
AUDIT_LOCK_WAIT_TIMEOUT_SECONDS = 1
AUDIT_TRANSIENT_MYSQL_ERRORS = {1205, 1213}

# Each service process owns a bounded stream cache.  It is intentionally
# in-memory: audit policy must not read/lock the audit table before deciding
# whether an event deserves a row.
_AUDIT_STREAMS: "OrderedDict[str, Tuple[str, datetime]]" = OrderedDict()
_AUDIT_STREAM_LOCK = Lock()

_MONITOR_SIGNATURE_FIELDS = (
    "posture",
    "target_expansion_allowed",
    "trail_mode",
    "exit_pressure",
    "active_evidence_action",
    "active_evidence_reason_code",
    "current_target_price",
    "current_stop_price",
    "target_r_multiple",
    "stop_r_multiple",
    "risk_reduced",
    "expansion_count",
    "last_target_hit_price",
    "last_signal_stage",
)

_IMPORTANT_PAYLOAD_KEYS = (
    "signal_id",
    "instrument_type",
    "side",
    "snapshot_time",
    "close",
    "originating_setup",
    "current_setup_label",
    "setup_label",
    "strategy",
    "decision",
    "evaluator_state",
    "entry_permission",
    "blocked_by",
    "risk_flags",
    "analytics",
    "management",
    "monitor_resolution",
    "update_fields",
    "exit_status",
    "trade_ids",
    "result",
    "lifecycle_name",
    "primary_trade_id",
    "primary_exit_reason",
    "primary_exit_rule",
)


def _to_ist_naive(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(IST).replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.astimezone(IST).replace(tzinfo=None) if dt.tzinfo else dt
        except Exception:
            return None
    return None


def _deep_get(obj: Any, path: list[str]) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _source_ts_from_payload(payload: Optional[Dict[str, Any]]) -> Optional[datetime]:
    if not isinstance(payload, dict):
        return None

    paths = [
        ["snapshot_time"],
        ["decision", "details", "snapshot_time"],
        ["decision", "snapshot_time"],
        ["details", "snapshot_time"],
        ["signal", "last_eval_time"],
        ["signal", "last_snapshot_time"],
        ["trade", "last_time"],
        ["trade", "entry_time"],
        ["context", "snapshot_time"],
    ]

    for path in paths:
        dt = _to_ist_naive(_deep_get(payload, path))
        if dt:
            return dt
    return None


def _resolve_audit_ts(ts: Optional[Any], payload_json: Optional[Dict[str, Any]]) -> datetime:
    """Resolve the authoritative source/snapshot timestamp for an audit row.

    Audit is replayable lifecycle evidence. It must never manufacture time from
    the wall clock. Callers must provide ``ts`` directly or include an explicit
    snapshot/source timestamp in the payload.
    """
    explicit = _to_ist_naive(ts)
    if explicit:
        return explicit

    payload_ts = _source_ts_from_payload(payload_json)
    if payload_ts:
        return payload_ts

    raise ValueError(
        "auditlog write requires an explicit source/snapshot timestamp; "
        "wall-clock fallback is disabled"
    )


def _enum_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    text = str(raw or "").strip()
    return text or None


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _reason_family(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    # Numeric confidence/profit values should not turn an unchanged management
    # posture into a new permanent audit event every monitor pass.
    return re.sub(r"[-+]?\d+(?:\.\d+)?", "#", text)


def _small_mapping(value: Any, *, allowed: Optional[Tuple[str, ...]] = None) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    keys = allowed or tuple(sorted(value.keys())[:20])
    out: Dict[str, Any] = {}
    for key in keys:
        if key not in value:
            continue
        item = value.get(key)
        if item is None or isinstance(item, (str, int, float, bool)):
            out[key] = item
        elif isinstance(item, Decimal):
            out[key] = float(item)
        elif isinstance(item, datetime):
            out[key] = item.isoformat()
        elif isinstance(item, list):
            out[key] = item[:10]
        elif isinstance(item, dict):
            out[key] = {
                str(k): v
                for k, v in list(item.items())[:15]
                if v is None or isinstance(v, (str, int, float, bool))
            }
    return sanitize_json(out)


def _payload_summary(clean: Dict[str, Any], *, original_bytes: int) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "payload_compacted": True,
        "original_payload_bytes": int(original_bytes),
        "original_top_level_keys": sorted(str(k) for k in clean.keys()),
    }
    for key in _IMPORTANT_PAYLOAD_KEYS:
        if key not in clean:
            continue
        value = clean.get(key)
        if key == "management":
            summary[key] = _small_mapping(value, allowed=_MONITOR_SIGNATURE_FIELDS + ("last_update_reason",))
        elif key in {"decision", "result", "monitor_resolution", "analytics"}:
            summary[key] = _small_mapping(value)
        elif value is None or isinstance(value, (str, int, float, bool, list)):
            summary[key] = value
        elif isinstance(value, dict):
            summary[key] = _small_mapping(value)
    return sanitize_json(summary)


def _payload_size_bytes(payload: Dict[str, Any]) -> int:
    try:
        return len(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))
    except Exception:
        return 0


def _compact_payload(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not payload:
        return None
    try:
        clean = sanitize_json(payload)
        clean = clean if isinstance(clean, dict) else {"value": clean}
        limit = (
            int(AUDIT_CONFIG.debugging_max_payload_bytes)
            if AUDIT_CONFIG.mode == "DEBUGGING"
            else int(AUDIT_CONFIG.production_max_payload_bytes)
        )
        size = _payload_size_bytes(clean)
        if size > limit:
            return _payload_summary(clean, original_bytes=size)
        return clean
    except Exception:
        logger.debug("auditlog payload sanitize failed", exc_info=True)
        return {"payload_error": "sanitize_failed"}


def _stream_key(
    *,
    entity_type: Any,
    entity_id: Any,
    userid: Any,
    evaluation_stage: Any,
) -> str:
    return "|".join([
        str(entity_type or "").strip().upper(),
        str(entity_id or "").strip(),
        str(userid or "").strip(),
        str(evaluation_stage or "").strip().upper(),
    ])


def _event_signature(
    *,
    action: Any,
    reason_code: Any,
    previous_state: Any,
    new_state: Any,
    evaluation_stage: Any,
    payload_json: Optional[Dict[str, Any]],
) -> str:
    payload = payload_json if isinstance(payload_json, dict) else {}
    stage = str(evaluation_stage or "").upper().strip()
    signature: Dict[str, Any] = {
        "action": str(action or "").upper().strip(),
        "reason_code": str(reason_code or "").upper().strip(),
        "previous_state": str(previous_state or "").upper().strip(),
        "new_state": str(new_state or "").upper().strip(),
    }
    if stage == "TRADE_MONITOR":
        management = payload.get("management") if isinstance(payload.get("management"), dict) else {}
        signature["management"] = {
            key: management.get(key)
            for key in _MONITOR_SIGNATURE_FIELDS
            if key in management
        }
        signature["management_reason_family"] = _reason_family(management.get("last_update_reason"))
        signature["exit_status"] = payload.get("exit_status")
    return json.dumps(sanitize_json(signature), sort_keys=True, separators=(",", ":"), default=str)


def _production_low_value_event(
    *,
    action: Any,
    reason_code: Any,
    evaluation_stage: Any,
    payload_json: Optional[Dict[str, Any]],
) -> bool:
    stage = str(evaluation_stage or "").upper().strip()
    action_s = str(action or "").upper().strip()
    reason_s = str(reason_code or "").upper().strip()
    if stage != "TRADE_MONITOR" or action_s != "MONITOR":
        return False
    payload = payload_json if isinstance(payload_json, dict) else {}
    management = payload.get("management") if isinstance(payload.get("management"), dict) else {}
    reason_family = _reason_family(management.get("last_update_reason"))
    posture = str(management.get("posture") or "").upper().strip()
    # Generic HOLD/price refreshes belong in the service log, not permanent DB
    # audit. Any protection/expansion/exit/level change has a distinct signature
    # and is retained.
    generic_reason = reason_s in {"", "MONITOR_UPDATE", "EVIDENCE_HOLD"}
    generic_management = posture in {"", "HOLD"} and (
        not reason_family
        or reason_family.startswith("HOLD")
        or reason_family.startswith("EVIDENCE_HOLD")
    )
    return generic_reason and generic_management


def _should_persist_event(
    *,
    entity_type: Any,
    entity_id: Any,
    userid: Any,
    evaluation_stage: Any,
    previous_state: Any,
    new_state: Any,
    action: Any,
    reason_code: Any,
    payload_json: Optional[Dict[str, Any]],
    resolved_ts: datetime,
) -> bool:
    if not bool(AUDIT_CONFIG.enabled):
        return False

    key = _stream_key(
        entity_type=entity_type,
        entity_id=entity_id,
        userid=userid,
        evaluation_stage=evaluation_stage,
    )
    signature = _event_signature(
        action=action,
        reason_code=reason_code,
        previous_state=previous_state,
        new_state=new_state,
        evaluation_stage=evaluation_stage,
        payload_json=payload_json,
    )

    with _AUDIT_STREAM_LOCK:
        prior = _AUDIT_STREAMS.get(key)
        changed = prior is None or prior[0] != signature
        if changed:
            _AUDIT_STREAMS[key] = (signature, resolved_ts)
            _AUDIT_STREAMS.move_to_end(key)
        else:
            _AUDIT_STREAMS.move_to_end(key)

        while len(_AUDIT_STREAMS) > int(AUDIT_CONFIG.stream_cache_size):
            _AUDIT_STREAMS.popitem(last=False)

        if changed:
            # In PRODUCTION the first generic monitor snapshot establishes the
            # cache but does not deserve a DB row.  Once a stream exists, any
            # signature change is meaningful by construction (posture, stop,
            # target, risk reduction, expansion, evidence or exit state) and
            # must be retained even if the caller's broad reason is still HOLD.
            if (
                AUDIT_CONFIG.mode == "PRODUCTION"
                and prior is None
                and _production_low_value_event(
                    action=action,
                    reason_code=reason_code,
                    evaluation_stage=evaluation_stage,
                    payload_json=payload_json,
                )
            ):
                return False
            return True

        if AUDIT_CONFIG.mode == "DEBUGGING" and prior is not None:
            heartbeat = timedelta(minutes=int(AUDIT_CONFIG.debugging_heartbeat_minutes))
            if resolved_ts - prior[1] >= heartbeat:
                _AUDIT_STREAMS[key] = (signature, resolved_ts)
                return True
        return False


def _release_failed_event(
    *,
    entity_type: Any,
    entity_id: Any,
    userid: Any,
    evaluation_stage: Any,
    previous_state: Any,
    new_state: Any,
    action: Any,
    reason_code: Any,
    payload_json: Optional[Dict[str, Any]],
) -> None:
    """Release an in-memory reservation when the DB insert did not commit.

    Policy decisions reserve the latest stream signature before the insert so
    concurrent loops can deduplicate immediately.  A failed insert must remove
    only that exact reservation; otherwise a transient lock could permanently
    hide the lifecycle transition we intended to preserve.
    """
    key = _stream_key(
        entity_type=entity_type,
        entity_id=entity_id,
        userid=userid,
        evaluation_stage=evaluation_stage,
    )
    signature = _event_signature(
        action=action,
        reason_code=reason_code,
        previous_state=previous_state,
        new_state=new_state,
        evaluation_stage=evaluation_stage,
        payload_json=payload_json,
    )
    with _AUDIT_STREAM_LOCK:
        current = _AUDIT_STREAMS.get(key)
        if current is not None and current[0] == signature:
            _AUDIT_STREAMS.pop(key, None)


def _payload_with_audit_mode(
    payload: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Return payload with non-temporal audit policy metadata only.

    ``auditlog.ts`` is the single authoritative timestamp. Duplicate source and
    wall-clock write timestamps are deliberately not embedded in the payload.
    """
    if not isinstance(payload, dict):
        payload = {} if payload is None else {"value": payload}
    else:
        payload = dict(payload)

    payload.setdefault("audit_mode", AUDIT_CONFIG.mode)
    return payload


def _mysql_error_code(exc: BaseException) -> Optional[int]:
    """Best-effort extraction of the underlying MySQL error number."""
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        errno = getattr(cur, "errno", None)
        if isinstance(errno, int):
            return errno

        args = getattr(cur, "args", ())
        if args and isinstance(args[0], int):
            return args[0]

        nxt = getattr(cur, "orig", None) or getattr(cur, "__cause__", None)
        cur = nxt if isinstance(nxt, BaseException) else None
    return None

def write_auditlog(
    *,
    entity_type: str,
    evaluation_stage: str,
    entity_id: Optional[Any] = None,
    symbol: Optional[Any] = None,
    userid: Optional[Any] = None,
    previous_state: Optional[Any] = None,
    new_state: Optional[Any] = None,
    action: Optional[Any] = None,
    reason_code: Optional[Any] = None,
    reason_text: Optional[Any] = None,
    confidence: Optional[Any] = None,
    payload_json: Optional[Dict[str, Any]] = None,
    ts: Optional[datetime] = None,
) -> bool:
    """Policy-controlled, best-effort append-only lifecycle writer.

    Services emit candidate events; this central policy decides whether a row
    is useful in DEBUGGING/PRODUCTION mode. It must never interrupt trading.
    Any policy skip or insert error is non-fatal.
    """
    audit_reserved = False
    try:
        resolved_ts = _resolve_audit_ts(ts, payload_json)
        if not _should_persist_event(
            entity_type=entity_type,
            entity_id=entity_id,
            userid=userid,
            evaluation_stage=evaluation_stage,
            previous_state=previous_state,
            new_state=new_state,
            action=action,
            reason_code=reason_code,
            payload_json=payload_json,
            resolved_ts=resolved_ts,
        ):
            return False
        audit_reserved = True

        row = AuditLog(
            ts=resolved_ts,
            entity_type=str(entity_type or "").strip().upper()[:30],
            entity_id=_enum_str(entity_id),
            symbol=_enum_str(symbol),
            userid=_enum_str(userid),
            evaluation_stage=str(evaluation_stage or "").strip().upper()[:50],
            previous_state=_enum_str(previous_state),
            new_state=_enum_str(new_state),
            action=_enum_str(action),
            reason_code=_enum_str(reason_code),
            reason_text=str(reason_text or "").strip() or None,
            confidence=Decimal(str(_to_float(confidence))) if _to_float(confidence) is not None else None,
            payload_json=_compact_payload(_payload_with_audit_mode(payload_json)),
        )

        with get_trades_db() as db:
            # Audit is explicitly non-critical.  Do not allow a locked audit
            # table/row to stall the monitor for MySQL's normal 50-second
            # lock-wait timeout.  This setting is scoped to this DB session.
            db.execute(
                text(
                    f"SET SESSION innodb_lock_wait_timeout = "
                    f"{AUDIT_LOCK_WAIT_TIMEOUT_SECONDS}"
                )
            )
            db.add(row)
            db.commit()
        return True

    except Exception as exc:
        if audit_reserved:
            _release_failed_event(
                entity_type=entity_type,
                entity_id=entity_id,
                userid=userid,
                evaluation_stage=evaluation_stage,
                previous_state=previous_state,
                new_state=new_state,
                action=action,
                reason_code=reason_code,
                payload_json=payload_json,
            )
        error_code = _mysql_error_code(exc)
        if error_code in AUDIT_TRANSIENT_MYSQL_ERRORS:
            logger.warning(
                "auditlog skipped after transient MySQL contention "
                "error=%s entity_type=%s entity_id=%s stage=%s",
                error_code,
                entity_type,
                entity_id,
                evaluation_stage,
            )
        else:
            logger.warning("auditlog write failed: %s", exc, exc_info=True)
        return False
