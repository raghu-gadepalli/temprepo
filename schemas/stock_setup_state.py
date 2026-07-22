# schemas/stock_setup_state.py
#
# Persistent pre-entry setup memory for AutoTrades setup discovery.
#
# Pattern alignment:
# - SQLAlchemy ORM lives in models.trade_models.
# - Pydantic schema + DB helper methods live together here, like
#   schemas/signal.py, schemas/user_trade.py and schemas/symbol.py.
# - No separate setup-state store/wrapper is used; callers import this schema directly.

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

from pydantic import BaseModel, Field

from database.database import get_trades_db
from models.trade_models import StockSetupState as StockSetupStateORM
from utils.datetime_utils import to_ist_naive
from utils.json_utils import sanitize_json

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def _require_snapshot_time(value: Any, *, field_name: str = "snapshot_time") -> datetime:
    """Return one normalized market-observation timestamp or fail loudly.

    Setup lifecycle persistence must be deterministic in live processing and
    replay.  Wall-clock time is therefore never a valid fallback here.
    """
    normalized = to_ist_naive(value)
    if normalized is None:
        raise ValueError(f"stock_setup_state requires {field_name}; wall-clock fallback is forbidden")
    return normalized


def _to_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        dt = to_ist_naive(value)
        if dt is None:
            raise ValueError(f"Invalid trading_day for stock_setup_state: {value!r}")
        return dt.date()
    if isinstance(value, str):
        return datetime.fromisoformat(value[:10]).date()
    raise ValueError(f"Invalid trading_day for stock_setup_state: {value!r}")


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _norm_str(value: Any, default: str = "") -> str:
    raw = default if value is None else value
    return str(raw).strip()


def _norm_upper(value: Any, default: str = "") -> str:
    return _norm_str(value, default=default).upper()


_MAX_TRANSITION_HISTORY = 100
_MAX_EVENT_HISTORY = 50
_TERMINAL_SETUP_STATES = {
    "CONSUMED",
    "INVALIDATED",
    "EXPIRED",
    "COOLDOWN",
    "SIGNAL_CREATED",
    "DROPPED",
}


def _iso_text(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        normalized = to_ist_naive(value)
        return normalized.isoformat() if normalized is not None else value.isoformat()
    return str(value)


def _explicit_state_json_event_identity(
    state_json: Optional[Dict[str, Any]],
) -> Dict[str, Optional[str]]:
    """Return only event identity explicitly supplied by the caller.

    This deliberately does not synthesize a fallback key.  Transition helpers
    use it to decide whether a later setup event is genuinely different from
    the current row before merging any JSON fields.
    """
    payload = dict(state_json or {})
    watch = payload.get("watch") if isinstance(payload.get("watch"), dict) else {}
    event_key = (
        payload.get("event_key")
        or watch.get("event_key")
        or payload.get("watch_event_key")
        or watch.get("watch_event_key")
    )
    event_source = (
        payload.get("event_source")
        or watch.get("source")
        or payload.get("source")
    )
    event_time = (
        payload.get("event_time")
        or watch.get("event_time")
        or watch.get("snapshot_time")
    )
    return {
        "event_key": str(event_key) if event_key else None,
        "event_source": str(event_source) if event_source else None,
        "event_time": _iso_text(event_time),
    }


def _state_json_event_identity(
    state_json: Optional[Dict[str, Any]],
    *,
    setup: Any,
    side: Any,
    first_seen_time: Any = None,
    reference_price: Any = None,
) -> Dict[str, Optional[str]]:
    """Return a stable identity for the current setup event.

    FAILED_BREAKOUT already supplies a deterministic ``watch.event_key``.  The
    fallback keeps the same current-state model usable for the other setup
    families without adding another relational table.
    """
    payload = dict(state_json or {})
    watch = payload.get("watch") if isinstance(payload.get("watch"), dict) else {}
    explicit = _explicit_state_json_event_identity(payload)

    event_key = explicit.get("event_key")
    event_source = explicit.get("event_source")
    event_time = explicit.get("event_time") or first_seen_time

    if not event_key:
        ref = (
            watch.get("level_price")
            if watch.get("level_price") is not None
            else watch.get("low")
            if _norm_upper(side) == "BUY" and watch.get("low") is not None
            else watch.get("high")
            if _norm_upper(side) == "SELL" and watch.get("high") is not None
            else reference_price
        )
        pieces = [
            _norm_upper(setup),
            _norm_upper(side),
            _iso_text(event_time) or "",
        ]
        if ref is not None and ref != "":
            pieces.append(str(ref))
        if any(pieces):
            event_key = "|".join(pieces)

    return {
        "event_key": str(event_key) if event_key else None,
        "event_source": str(event_source) if event_source else None,
        "event_time": _iso_text(event_time),
    }


def _transition_record(
    *,
    state: Any,
    state_reason: Any,
    signal_id: Any,
    transition_time: Any,
    identity: Dict[str, Optional[str]],
    transition_source: Optional[str] = None,
) -> Dict[str, Any]:
    return sanitize_json({
        "event_key": identity.get("event_key"),
        "event_source": identity.get("event_source"),
        "event_time": identity.get("event_time"),
        "state": _norm_upper(state),
        "state_reason": state_reason,
        "signal_id": signal_id,
        "transition_time": _iso_text(transition_time),
        "transition_source": transition_source,
    })


def _append_history_unique(
    history: List[Dict[str, Any]],
    record: Dict[str, Any],
    *,
    max_items: int,
    fingerprint_keys: tuple[str, ...],
) -> List[Dict[str, Any]]:
    clean = [dict(x) for x in history if isinstance(x, dict)]
    fingerprint = tuple(record.get(k) for k in fingerprint_keys)
    existing_fingerprints = {
        tuple(item.get(k) for k in fingerprint_keys)
        for item in clean
    }
    if fingerprint not in existing_fingerprints:
        clean.append(sanitize_json(record))
    return clean[-max_items:]


def _event_state_rank(value: Any) -> int:
    state = _norm_upper(value)
    if state in _TERMINAL_SETUP_STATES or state == "SUPERSEDED":
        return 3
    if state in {"CONFIRMED", "PENDING", "ENTRY_READY"}:
        return 2
    if state == "WATCH":
        return 1
    return 0


def _merge_archived_event(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Merge two archive snapshots for the same setup-event key.

    A current setup row is updated repeatedly while one event matures.  Earlier
    versions appended each intermediate archive snapshot, so the final JSON could
    contain several records for the same ``event_key``.  Event history is an event
    ledger, not an evaluation ledger: keep one record per event and merge its
    transitions and terminal outcome.
    """
    old = dict(existing or {})
    new = dict(incoming or {})

    out = dict(old)
    for key, value in new.items():
        if key == "transitions":
            continue
        if value is not None and value != "":
            out[key] = value

    # Never regress a completed event back to WATCH merely because an older
    # intermediate payload is merged later.
    if _event_state_rank(old.get("final_state")) > _event_state_rank(new.get("final_state")):
        out["final_state"] = old.get("final_state")
        out["final_reason"] = old.get("final_reason")
        if old.get("signal_id"):
            out["signal_id"] = old.get("signal_id")

    if not out.get("signal_id") and old.get("signal_id"):
        out["signal_id"] = old.get("signal_id")

    first_values = [x for x in (old.get("first_seen_time"), new.get("first_seen_time")) if x]
    if first_values:
        out["first_seen_time"] = min(str(x) for x in first_values)
    last_values = [x for x in (old.get("last_seen_time"), new.get("last_seen_time")) if x]
    if last_values:
        out["last_seen_time"] = max(str(x) for x in last_values)
    expiry_values = [x for x in (old.get("expires_at"), new.get("expires_at")) if x]
    if expiry_values:
        out["expires_at"] = max(str(x) for x in expiry_values)

    transitions: List[Dict[str, Any]] = []
    for item in list(old.get("transitions") or []) + list(new.get("transitions") or []):
        if isinstance(item, dict):
            transitions = _append_history_unique(
                transitions,
                item,
                max_items=20,
                fingerprint_keys=("event_key", "state", "state_reason", "signal_id", "transition_time"),
            )
    out["transitions"] = transitions[-20:]
    return sanitize_json(out)


def _upsert_archived_event(
    history: List[Dict[str, Any]],
    record: Dict[str, Any],
    *,
    max_items: int = _MAX_EVENT_HISTORY,
) -> List[Dict[str, Any]]:
    """Return a deduplicated event ledger with one item per event key."""
    clean: List[Dict[str, Any]] = []

    def _add(item: Dict[str, Any]) -> None:
        event_key = str(item.get("event_key") or "").strip()
        if event_key:
            for idx, current in enumerate(clean):
                if str(current.get("event_key") or "").strip() == event_key:
                    clean[idx] = _merge_archived_event(current, item)
                    return
        clean.append(sanitize_json(item))

    for item in history or []:
        if isinstance(item, dict):
            _add(dict(item))
    if isinstance(record, dict) and record:
        _add(dict(record))
    return clean[-max_items:]


def _compact_archived_event(row: Any, state_json: Dict[str, Any]) -> Dict[str, Any]:
    identity = _state_json_event_identity(
        state_json,
        setup=getattr(row, "setup", None),
        side=getattr(row, "side", None),
        first_seen_time=getattr(row, "first_seen_time", None),
        reference_price=getattr(row, "reference_price", None),
    )
    transitions = [
        dict(item)
        for item in (state_json.get("transition_history") or [])
        if isinstance(item, dict)
        and (
            not item.get("event_key")
            or item.get("event_key") == identity.get("event_key")
        )
    ]
    return sanitize_json({
        **identity,
        "setup": _norm_upper(getattr(row, "setup", None)),
        "side": _norm_upper(getattr(row, "side", None)),
        "first_seen_time": _iso_text(getattr(row, "first_seen_time", None)),
        "last_seen_time": _iso_text(getattr(row, "last_seen_time", None)),
        "expires_at": _iso_text(getattr(row, "expires_at", None)),
        "final_state": _norm_upper(getattr(row, "state", None)),
        "final_reason": getattr(row, "state_reason", None),
        "signal_id": getattr(row, "signal_id", None),
        "reference_price": getattr(row, "reference_price", None),
        "reference_source": getattr(row, "reference_source", None),
        "transitions": transitions[-20:],
    })


def _merge_state_json_for_upsert(*, row: Any, data: Dict[str, Any]) -> Dict[str, Any]:
    """Merge current-event state without losing prior same-key event history.

    ``stock_setup_state`` intentionally remains one row per day/equity/setup/side.
    A later event therefore replaces the current event fields, but the completed
    event and its transitions must first be archived inside ``event_history``.
    """
    incoming = dict(data.get("state_json") or {})
    existing = dict(getattr(row, "state_json", None) or {}) if row is not None else {}

    incoming_identity = _state_json_event_identity(
        incoming,
        setup=data.get("setup"),
        side=data.get("side"),
        first_seen_time=data.get("first_seen_time"),
        reference_price=data.get("reference_price"),
    )
    existing_identity = _state_json_event_identity(
        existing,
        setup=getattr(row, "setup", None),
        side=getattr(row, "side", None),
        first_seen_time=getattr(row, "first_seen_time", None),
        reference_price=getattr(row, "reference_price", None),
    ) if row is not None else {"event_key": None, "event_source": None, "event_time": None}

    for key, value in incoming_identity.items():
        if value is not None:
            incoming[key] = value

    # Normalize previously written JSON as part of every upsert.  This also
    # repairs rows created by the first history patch, which could contain more
    # than one archive snapshot for the same event key.
    event_history: List[Dict[str, Any]] = []
    for item in (existing.get("event_history") or []):
        if isinstance(item, dict):
            event_history = _upsert_archived_event(event_history, item)
    for item in (incoming.get("event_history") or []):
        if isinstance(item, dict):
            event_history = _upsert_archived_event(event_history, item)

    transitions = [dict(x) for x in (existing.get("transition_history") or []) if isinstance(x, dict)]
    for item in (incoming.get("transition_history") or []):
        if isinstance(item, dict):
            transitions = _append_history_unique(
                transitions,
                item,
                max_items=_MAX_TRANSITION_HISTORY,
                fingerprint_keys=("event_key", "state", "state_reason", "signal_id", "transition_time"),
            )

    old_key = existing_identity.get("event_key")
    new_key = incoming_identity.get("event_key")
    is_new_event = bool(row is not None and old_key and new_key and old_key != new_key)
    transition_time = _require_snapshot_time(data.get("last_seen_time"), field_name="last_seen_time")

    if is_new_event:
        existing_state = _norm_upper(getattr(row, "state", None))
        archived = _compact_archived_event(row, existing)
        if existing_state not in _TERMINAL_SETUP_STATES:
            archived["final_state"] = "SUPERSEDED"
            archived["final_reason"] = "SETUP_EVENT_REPLACED_BY_NEW_EVENT"
        event_history = _upsert_archived_event(
            event_history,
            archived,
            max_items=_MAX_EVENT_HISTORY,
        )
        if existing_state not in _TERMINAL_SETUP_STATES:
            transitions = _append_history_unique(
                transitions,
                _transition_record(
                    state="SUPERSEDED",
                    state_reason="SETUP_EVENT_REPLACED_BY_NEW_EVENT",
                    signal_id=getattr(row, "signal_id", None),
                    transition_time=transition_time,
                    identity=existing_identity,
                    transition_source="event_rollover",
                ),
                max_items=_MAX_TRANSITION_HISTORY,
                fingerprint_keys=("event_key", "state", "state_reason", "signal_id", "transition_time"),
            )

        transitions = _append_history_unique(
            transitions,
            _transition_record(
                state=data.get("state") or "WATCH",
                state_reason=data.get("state_reason"),
                signal_id=data.get("signal_id"),
                # event_time/first_seen_time identifies the causal event; the
                # transition itself is observed on the current snapshot. Never
                # backdate lifecycle history to the original event candle.
                transition_time=transition_time,
                identity=incoming_identity,
                transition_source="event_rollover",
            ),
            max_items=_MAX_TRANSITION_HISTORY,
            fingerprint_keys=("event_key", "state", "state_reason", "signal_id", "transition_time"),
        )
    else:
        state_changed = row is None or (
            _norm_upper(getattr(row, "state", None)) != _norm_upper(data.get("state"))
            or getattr(row, "state_reason", None) != data.get("state_reason")
            or str(getattr(row, "signal_id", None) or "") != str(data.get("signal_id") or "")
        )
        if state_changed:
            transitions = _append_history_unique(
                transitions,
                _transition_record(
                    state=data.get("state") or "WATCH",
                    state_reason=data.get("state_reason"),
                    signal_id=data.get("signal_id"),
                    transition_time=transition_time,
                    identity=incoming_identity,
                    transition_source="upsert",
                ),
                max_items=_MAX_TRANSITION_HISTORY,
                fingerprint_keys=("event_key", "state", "state_reason", "signal_id", "transition_time"),
            )

    incoming["event_history"] = event_history[-_MAX_EVENT_HISTORY:]
    incoming["transition_history"] = transitions[-_MAX_TRANSITION_HISTORY:]
    return sanitize_json(incoming)


# -------------------------------------------------------------------
# Schema
# -------------------------------------------------------------------
class StockSetupStateSchema(BaseModel):
    """Pydantic representation and DB access layer for stock_setup_state."""

    model_config = {"from_attributes": True, "extra": "allow"}

    id: Optional[int] = None

    trading_day: date
    equity_ref: str
    symbol: str
    lifecycle: str = "DEFAULT"
    setup: str
    side: str

    state: str
    state_reason: Optional[str] = None

    first_seen_time: Optional[datetime] = None
    last_seen_time: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    age_bars: Optional[int] = None

    discovery_price: Optional[Decimal] = None
    discovery_extreme_price: Optional[Decimal] = None
    confirmation_price: Optional[Decimal] = None
    confirmation_time: Optional[datetime] = None
    reference_price: Optional[Decimal] = None
    reference_source: Optional[str] = None
    signal_id: Optional[str] = None

    # Must always be JSON-safe before reaching SQLAlchemy's JSON type.
    state_json: Dict[str, Any] = Field(default_factory=dict)

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def to_db_dict(self) -> Dict[str, Any]:
        data = self.model_dump(exclude_none=True)

        for key in (
            "first_seen_time",
            "last_seen_time",
            "expires_at",
            "confirmation_time",
            "created_at",
            "updated_at",
        ):
            if key in data:
                data[key] = to_ist_naive(data[key])

        data["trading_day"] = _to_date(data["trading_day"])
        data["equity_ref"] = _norm_upper(data.get("equity_ref"))
        data["symbol"] = _norm_upper(data.get("symbol") or data["equity_ref"])
        data["lifecycle"] = _norm_upper(data.get("lifecycle"), default="DEFAULT") or "DEFAULT"
        data["setup"] = _norm_upper(data.get("setup"))
        data["side"] = _norm_upper(data.get("side"))
        data["state"] = _norm_upper(data.get("state"), default="WATCH") or "WATCH"
        data["state_json"] = sanitize_json(data.get("state_json") or {})

        for key in (
            "discovery_price",
            "discovery_extreme_price",
            "confirmation_price",
            "reference_price",
        ):
            if key in data:
                data[key] = _to_decimal(data[key])

        return data

    # ------------------------------------------------------------------
    # CRUD + query helpers
    # ------------------------------------------------------------------
    @staticmethod
    def fetch_state(
        *,
        trading_day: date,
        equity_ref: str,
        setup: str,
        side: str,
    ) -> Optional["StockSetupStateSchema"]:
        """Fetch one setup-state row by its natural key."""
        try:
            day = _to_date(trading_day)
            ref = _norm_upper(equity_ref)
            setup_u = _norm_upper(setup)
            side_u = _norm_upper(side)

            with get_trades_db() as db:
                row = (
                    db.query(StockSetupStateORM)
                    .filter(
                        StockSetupStateORM.trading_day == day,
                        StockSetupStateORM.equity_ref == ref,
                        StockSetupStateORM.setup == setup_u,
                        StockSetupStateORM.side == side_u,
                    )
                    .one_or_none()
                )

            return StockSetupStateSchema.model_validate(row) if row else None
        except Exception:
            logger.exception(
                "Error fetching StockSetupState[%s %s %s %s]",
                trading_day,
                equity_ref,
                setup,
                side,
            )
            raise

    @staticmethod
    def fetch_states_for_symbol(
        *,
        trading_day: date,
        equity_ref: str,
    ) -> List["StockSetupStateSchema"]:
        """Fetch all setup-state rows for one stock/day."""
        try:
            day = _to_date(trading_day)
            ref = _norm_upper(equity_ref)

            with get_trades_db() as db:
                rows = (
                    db.query(StockSetupStateORM)
                    .filter(
                        StockSetupStateORM.trading_day == day,
                        StockSetupStateORM.equity_ref == ref,
                    )
                    .order_by(
                        StockSetupStateORM.setup.asc(),
                        StockSetupStateORM.side.asc(),
                        StockSetupStateORM.updated_at.desc(),
                    )
                    .all()
                )

            return [StockSetupStateSchema.model_validate(row) for row in rows]
        except Exception:
            logger.exception(
                "Error fetching StockSetupState rows for [%s %s]",
                trading_day,
                equity_ref,
            )
            raise

    @staticmethod
    def upsert_state(payload: Dict[str, Any]) -> Optional["StockSetupStateSchema"]:
        """Insert/update one setup-state row.

        The input payload is intentionally a dict, matching the style used by
        create/update helpers in the existing schema files. Datetime-like values
        are normalized with utils.datetime_utils.to_ist_naive, and state_json is
        sanitized with utils.json_utils.sanitize_json before SQLAlchemy sees it.
        """
        valid: Set[str] = {
            "trading_day",
            "equity_ref",
            "symbol",
            "lifecycle",
            "setup",
            "side",
            "state",
            "state_reason",
            "first_seen_time",
            "last_seen_time",
            "expires_at",
            "age_bars",
            "discovery_price",
            "discovery_extreme_price",
            "confirmation_price",
            "confirmation_time",
            "reference_price",
            "reference_source",
            "signal_id",
            "state_json",
            "created_at",
            "updated_at",
        }
        incoming = {k: v for k, v in (payload or {}).items() if k in valid}
        signal_id_was_provided = "signal_id" in incoming

        snapshot_time = _require_snapshot_time(
            incoming.get("last_seen_time"),
            field_name="last_seen_time/snapshot_time",
        )
        trading_day = _to_date(incoming.get("trading_day"))
        if trading_day != snapshot_time.date():
            raise ValueError(
                "stock_setup_state trading_day must match snapshot_time.date(); "
                f"trading_day={trading_day!r} snapshot_time={snapshot_time!r}"
            )
        supplied_updated_at = incoming.get("updated_at")
        if supplied_updated_at is not None:
            normalized_updated_at = _require_snapshot_time(supplied_updated_at, field_name="updated_at")
            if normalized_updated_at != snapshot_time:
                raise ValueError(
                    "stock_setup_state updated_at must equal last_seen_time/snapshot_time; "
                    f"updated_at={normalized_updated_at!r} snapshot_time={snapshot_time!r}"
                )
        supplied_created_at = incoming.get("created_at")
        created_at = (
            _require_snapshot_time(supplied_created_at, field_name="created_at")
            if supplied_created_at is not None
            else snapshot_time
        )

        schema = StockSetupStateSchema.model_validate({
            "trading_day": incoming["trading_day"],
            "equity_ref": incoming["equity_ref"],
            "symbol": incoming.get("symbol") or incoming["equity_ref"],
            "lifecycle": incoming.get("lifecycle") or "DEFAULT",
            "setup": incoming["setup"],
            "side": incoming["side"],
            "state": incoming.get("state") or "WATCH",
            "state_reason": incoming.get("state_reason"),
            "first_seen_time": incoming.get("first_seen_time"),
            "last_seen_time": snapshot_time,
            "expires_at": incoming.get("expires_at"),
            "age_bars": incoming.get("age_bars"),
            "discovery_price": _to_decimal(incoming.get("discovery_price")),
            "discovery_extreme_price": _to_decimal(incoming.get("discovery_extreme_price")),
            "confirmation_price": _to_decimal(incoming.get("confirmation_price")),
            "confirmation_time": incoming.get("confirmation_time"),
            "reference_price": _to_decimal(incoming.get("reference_price")),
            "reference_source": incoming.get("reference_source"),
            "signal_id": incoming.get("signal_id"),
            "state_json": incoming.get("state_json") or {},
            "created_at": created_at,
            "updated_at": snapshot_time,
        })
        data = schema.to_db_dict()

        try:
            with get_trades_db() as db:
                row = (
                    db.query(StockSetupStateORM)
                    .filter(
                        StockSetupStateORM.trading_day == data["trading_day"],
                        StockSetupStateORM.equity_ref == data["equity_ref"],
                        StockSetupStateORM.setup == data["setup"],
                        StockSetupStateORM.side == data["side"],
                    )
                    .one_or_none()
                )
                existing_row = row

                if row is None:
                    row = StockSetupStateORM(
                        trading_day=data["trading_day"],
                        equity_ref=data["equity_ref"],
                        setup=data["setup"],
                        side=data["side"],
                        first_seen_time=data.get("first_seen_time"),
                        created_at=data["created_at"],
                    )
                    db.add(row)

                merge_data = dict(data)
                if not signal_id_was_provided and existing_row is not None:
                    merge_data["signal_id"] = getattr(existing_row, "signal_id", None)
                data["state_json"] = _merge_state_json_for_upsert(row=existing_row, data=merge_data)

                row.symbol = data["symbol"]
                row.lifecycle = data.get("lifecycle") or "DEFAULT"
                row.state = data.get("state") or "WATCH"
                row.state_reason = data.get("state_reason")
                row.first_seen_time = data.get("first_seen_time") or row.first_seen_time
                row.last_seen_time = data.get("last_seen_time")
                row.expires_at = data.get("expires_at")
                row.age_bars = data.get("age_bars")
                row.discovery_price = data.get("discovery_price")
                row.discovery_extreme_price = data.get("discovery_extreme_price")
                row.confirmation_price = data.get("confirmation_price")
                row.confirmation_time = data.get("confirmation_time")
                row.reference_price = data.get("reference_price")
                row.reference_source = data.get("reference_source")
                # Preserve the last consumed signal link during passive state
                # refreshes/transitions.  Callers that intentionally start a
                # fresh WATCH after a true reset should pass signal_id=None
                # explicitly; omitted signal_id means "do not overwrite".
                if signal_id_was_provided:
                    row.signal_id = data.get("signal_id")
                row.state_json = data.get("state_json") or {}
                row.updated_at = data["last_seen_time"]

                db.commit()
                db.refresh(row)
                return StockSetupStateSchema.model_validate(row)
        except Exception:
            logger.exception(
                "Error upserting StockSetupState[%s %s %s %s]",
                data.get("trading_day"),
                data.get("equity_ref"),
                data.get("setup"),
                data.get("side"),
            )
            raise

    @staticmethod
    def transition_state(
        *,
        trading_day: date,
        equity_ref: str,
        setup: str,
        side: str,
        state: str,
        state_reason: Optional[str] = None,
        ts: datetime,
        symbol: Optional[str] = None,
        signal_id: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        event_key: Optional[str] = None,
        event_source: Optional[str] = None,
        event_time: Optional[datetime | str] = None,
        reference_price: Any = None,
        reference_source: Optional[str] = None,
        state_json_update: Optional[Dict[str, Any]] = None,
    ) -> Optional["StockSetupStateSchema"]:
        """Transition one setup-state row while preserving original WATCH context.

        stock_setup_state is a current/latest-state table, not history.  This
        helper keeps first_seen_time from the existing WATCH row, attaches the
        signal_id when a setup is consumed, and appends a compact transition
        event into state_json.transition_history for auditability.
        """
        day = _to_date(trading_day)
        ref = _norm_upper(equity_ref)
        setup_u = _norm_upper(setup)
        side_u = _norm_upper(side)
        state_u = _norm_upper(state)
        now = _require_snapshot_time(ts, field_name="ts/snapshot_time")

        existing = StockSetupStateSchema.fetch_state(
            trading_day=day,
            equity_ref=ref,
            setup=setup_u,
            side=side_u,
        )

        # Idempotency guard: stock_setup_state is the current/latest state, not
        # an event table.  Replaying later ticks must not append the same terminal
        # transition again and again.  This prevents repeated COOLDOWN/EXPIRED
        # history bloat while still allowing a real state change to be recorded.
        existing_state = _norm_upper(getattr(existing, "state", None)) if existing else None
        existing_reason = getattr(existing, "state_reason", None) if existing else None
        existing_signal_id = getattr(existing, "signal_id", None) if existing else None
        existing_json = dict(getattr(existing, "state_json", None) or {}) if existing else {}
        incoming_update = sanitize_json(state_json_update or {}) if isinstance(state_json_update, dict) else {}
        if event_key:
            incoming_update["event_key"] = str(event_key)
        if event_source:
            incoming_update["event_source"] = str(event_source)
        if event_time is not None:
            incoming_update["event_time"] = _iso_text(event_time)

        existing_identity_for_guard = _state_json_event_identity(
            existing_json,
            setup=setup_u,
            side=side_u,
            first_seen_time=getattr(existing, "first_seen_time", None),
            reference_price=getattr(existing, "reference_price", None),
        ) if existing else {"event_key": None}
        incoming_identity_for_guard = _explicit_state_json_event_identity(incoming_update)
        same_event_for_guard = bool(
            not incoming_identity_for_guard.get("event_key")
            or incoming_identity_for_guard.get("event_key") == existing_identity_for_guard.get("event_key")
        )
        if (
            existing is not None
            and same_event_for_guard
            and existing_state == state_u
            and existing_reason == state_reason
            and (signal_id is None or str(existing_signal_id or "") == str(signal_id or ""))
        ):
            return existing

        existing_identity = _state_json_event_identity(
            existing_json,
            setup=setup_u,
            side=side_u,
            first_seen_time=getattr(existing, "first_seen_time", None),
            reference_price=getattr(existing, "reference_price", None),
        ) if existing else {"event_key": None, "event_source": None, "event_time": None}
        incoming_explicit_identity = _explicit_state_json_event_identity(incoming_update)
        incoming_event_key = incoming_explicit_identity.get("event_key")
        existing_event_key = existing_identity.get("event_key")
        is_new_event = bool(
            existing is not None
            and incoming_event_key
            and existing_event_key
            and incoming_event_key != existing_event_key
        )

        existing_json.update(incoming_update)
        event_first_seen = to_ist_naive(event_time) if event_time is not None else None
        if is_new_event and event_first_seen is None:
            event_first_seen = now

        resolved_signal_id = (
            signal_id
            if signal_id is not None
            else None
            if is_new_event
            else getattr(existing, "signal_id", None)
        )
        resolved_reference_price = (
            reference_price
            if reference_price is not None
            else None
            if is_new_event
            else getattr(existing, "reference_price", None)
        )
        resolved_reference_source = (
            reference_source
            if reference_source is not None
            else None
            if is_new_event
            else getattr(existing, "reference_source", None)
        )

        payload = {
            "trading_day": day,
            "equity_ref": ref,
            "symbol": _norm_upper(symbol or getattr(existing, "symbol", None) or ref),
            "lifecycle": getattr(existing, "lifecycle", None) or "DEFAULT",
            "setup": setup_u,
            "side": side_u,
            "state": state_u,
            "state_reason": state_reason,
            "first_seen_time": event_first_seen if is_new_event else getattr(existing, "first_seen_time", None) or now,
            "last_seen_time": now,
            "expires_at": expires_at if expires_at is not None else getattr(existing, "expires_at", None),
            "age_bars": None if is_new_event else getattr(existing, "age_bars", None),
            "discovery_price": None if is_new_event else getattr(existing, "discovery_price", None),
            "discovery_extreme_price": None if is_new_event else getattr(existing, "discovery_extreme_price", None),
            "confirmation_price": None if is_new_event else getattr(existing, "confirmation_price", None),
            "confirmation_time": None if is_new_event else getattr(existing, "confirmation_time", None),
            "reference_price": resolved_reference_price,
            "reference_source": resolved_reference_source,
            "signal_id": resolved_signal_id,
            "state_json": existing_json,
            "updated_at": now,
        }
        return StockSetupStateSchema.upsert_state(payload)

    @staticmethod
    def mark_consumed_for_signal(
        *,
        trading_day: date,
        equity_ref: str,
        setup: str,
        side: str,
        signal_id: str,
        ts: datetime,
        symbol: Optional[str] = None,
        reason: str = "SETUP_STATE_CONSUMED",
        state: str = "CONSUMED",
        event_key: Optional[str] = None,
        event_source: Optional[str] = None,
        event_time: Optional[datetime | str] = None,
        reference_price: Any = None,
        reference_source: Optional[str] = None,
        state_json_update: Optional[Dict[str, Any]] = None,
    ) -> Optional["StockSetupStateSchema"]:
        """Mark the setup state as consumed after the signal row is created."""
        return StockSetupStateSchema.transition_state(
            trading_day=trading_day,
            equity_ref=equity_ref,
            setup=setup,
            side=side,
            state=state,
            state_reason=reason,
            ts=ts,
            symbol=symbol,
            signal_id=signal_id,
            event_key=event_key,
            event_source=event_source,
            event_time=event_time,
            reference_price=reference_price,
            reference_source=reference_source,
            state_json_update=state_json_update,
        )

    @staticmethod
    def expire_due_states(
        *,
        snapshot_time: datetime,
        trading_day: Optional[date] = None,
        symbols: Optional[List[str]] = None,
        reason: str = "SETUP_STATE_EXPIRED_BY_SWEEP",
        force_all_active: bool = False,
        include_prior_days: bool = False,
    ) -> int:
        """Expire overdue active setup memory at a market snapshot timestamp.

        ``snapshot_time`` is mandatory and must come from the snapshot being
        processed (or the replay cursor representing that snapshot). Scheduler
        wall-clock time is not accepted as lifecycle time.

        Previously a WATCH row expired only when that exact setup was evaluated
        again. Service gaps or the end of the CREATE window could therefore
        leave old WATCH rows active for hours. This sweep is deliberately based
        only on expires_at and current state, and is safe to call repeatedly.
        """
        asof = _require_snapshot_time(snapshot_time, field_name="snapshot_time")
        day = _to_date(trading_day or asof.date())
        normalized_symbols = [_norm_upper(s) for s in (symbols or []) if _norm_upper(s)]
        active_states = ["WATCH", "PENDING", "CONFIRMED", "CONFIRMED_PENDING"]

        try:
            with get_trades_db() as db:
                q = db.query(StockSetupStateORM).filter(
                    StockSetupStateORM.state.in_(active_states)
                )
                if include_prior_days:
                    q = q.filter(StockSetupStateORM.trading_day <= day)
                else:
                    q = q.filter(StockSetupStateORM.trading_day == day)
                if not force_all_active:
                    q = (
                        q.filter(StockSetupStateORM.expires_at.isnot(None))
                        .filter(StockSetupStateORM.expires_at <= asof)
                    )
                if normalized_symbols:
                    q = q.filter(StockSetupStateORM.symbol.in_(normalized_symbols))
                rows = q.all()

                for row in rows:
                    state_json = dict(row.state_json or {})
                    identity = _state_json_event_identity(
                        state_json,
                        setup=row.setup,
                        side=row.side,
                        first_seen_time=row.first_seen_time,
                        reference_price=row.reference_price,
                    )
                    history = [
                        dict(item)
                        for item in (state_json.get("transition_history") or [])
                        if isinstance(item, dict)
                    ]
                    history = _append_history_unique(
                        history,
                        _transition_record(
                            state="EXPIRED",
                            state_reason=reason,
                            signal_id=row.signal_id,
                            transition_time=asof,
                            identity=identity,
                            transition_source="expiry_sweep",
                        ),
                        max_items=_MAX_TRANSITION_HISTORY,
                        fingerprint_keys=("event_key", "state", "state_reason", "signal_id", "transition_time"),
                    )
                    state_json.update({k: v for k, v in identity.items() if v is not None})
                    state_json["transition_history"] = history
                    state_json["expired_by_sweep"] = {
                        "asof_time": asof.isoformat(),
                        "previous_state": row.state,
                        "expires_at": row.expires_at.isoformat() if isinstance(row.expires_at, datetime) else row.expires_at,
                        "force_all_active": bool(force_all_active),
                        "include_prior_days": bool(include_prior_days),
                    }
                    row.state = "EXPIRED"
                    row.state_reason = reason
                    # Preserve last_seen_time as the last actual setup observation;
                    # updated_at/transition_time carry housekeeping time.
                    row.updated_at = asof
                    row.state_json = sanitize_json(state_json)

                db.commit()
                return len(rows)
        except Exception:
            logger.exception(
                "Error expiring StockSetupState rows day=%s asof=%s symbols=%s",
                day,
                asof,
                normalized_symbols,
            )
            raise

    @staticmethod
    def delete_for_day(*, trading_day: date, symbols: Optional[List[str]] = None) -> int:
        """Delete setup-state rows for one trading day, optionally limited to symbols.

        Useful for replay resets. Returns number of rows deleted.
        """
        try:
            day = _to_date(trading_day)
            normalized_symbols = [_norm_upper(s) for s in (symbols or []) if _norm_upper(s)]

            with get_trades_db() as db:
                q = db.query(StockSetupStateORM).filter(StockSetupStateORM.trading_day == day)
                if normalized_symbols:
                    q = q.filter(StockSetupStateORM.symbol.in_(normalized_symbols))
                count = q.delete(synchronize_session=False)
                db.commit()
                return int(count or 0)
        except Exception:
            logger.exception("Error deleting StockSetupState rows for day=%s", trading_day)
            raise
