import logging
import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from database.database import get_trades_db
from models.trade_models import Event as EventORM
from configs.service_config import SERVICE_CONFIG

logger = logging.getLogger(__name__)

# Grab defaults from typed event config
_event_extras = SERVICE_CONFIG.event.extras
DEFAULT_MAX_ATTEMPTS   = _event_extras.retry_attempts
BACKOFF_BASE_SECONDS   = _event_extras.backoff_base_seconds
BACKOFF_MAX_SECONDS    = _event_extras.backoff_max_seconds


class EventStatus(str, Enum):
    PENDING     = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED   = "succeeded"
    FAILED      = "failed"
    DEAD        = "dead"


def compute_backoff(attempts: int) -> int:
    """
    Exponential backoff capped at BACKOFF_MAX_SECONDS.
    """
    if attempts <= 0:
        return BACKOFF_BASE_SECONDS
    val = BACKOFF_BASE_SECONDS * (2 ** (attempts - 1))
    return min(BACKOFF_MAX_SECONDS, val)


class EventSchema(BaseModel):
    model_config = {"from_attributes": True}

    id: Optional[int]
    event_type: str
    aggregate_key: str
    payload: Optional[Dict[str, Any]]
    correlation_id: Optional[str]
    status: str
    attempts: int
    last_error: Optional[str]
    available_at: datetime
    created_at: datetime
    updated_at: datetime

    @staticmethod
    def emit_event(
        event_type: str,
        aggregate_key: str,
        payload: Optional[Dict[str, Any]] = None,
        correlation_id: Optional[str] = None,
    ) -> Optional["EventSchema"]:
        if correlation_id is None:
            correlation_id = str(uuid.uuid4())

        try:
            with get_trades_db() as db:
                existing = (
                    db.query(EventORM)
                      .filter(
                          EventORM.event_type == event_type,
                          EventORM.aggregate_key == aggregate_key,
                          EventORM.status.in_(
                              [EventStatus.PENDING.value, EventStatus.IN_PROGRESS.value]
                          ),
                      )
                      .one_or_none()
                )
                if existing:
                    logger.debug(
                        "Event already pending/in_progress: %s %s",
                        event_type, aggregate_key
                    )
                    return EventSchema.model_validate(existing)

                ev = EventORM(
                    event_type=event_type,
                    aggregate_key=aggregate_key,
                    payload=payload or {},
                    correlation_id=correlation_id,
                    status=EventStatus.PENDING.value,
                )
                db.add(ev)
                db.commit()
                db.refresh(ev)
                return EventSchema.model_validate(ev)

        except Exception:
            logger.exception(
                "Error emitting event %s for %s", event_type, aggregate_key
            )
            return None

    @staticmethod
    def fetch_due_event() -> Optional["EventSchema"]:
        try:
            with get_trades_db() as db:
                row = db.execute(
                    text(
                        """
                        SELECT id FROM events
                        WHERE status = :pending AND available_at <= :now
                        ORDER BY created_at ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                        """
                    ),
                    {"pending": EventStatus.PENDING.value, "now": datetime.now()},
                ).first()
                if not row:
                    return None

                event_id = row[0]
                ev = db.query(EventORM).filter(EventORM.id == event_id).one_or_none()
                if not ev:
                    return None

                ev.status = EventStatus.IN_PROGRESS.value
                ev.attempts += 1
                db.commit()
                db.refresh(ev)
                return EventSchema.model_validate(ev)

        except Exception:
            logger.exception("Error fetching due event")
            return None

    def mark_succeeded(self) -> Optional["EventSchema"]:
        try:
            with get_trades_db() as db:
                ev = db.query(EventORM).filter(EventORM.id == self.id).one_or_none()
                if not ev:
                    logger.warning("Event to mark succeeded not found: %s", self.id)
                    return None

                ev.status = EventStatus.SUCCEEDED.value
                db.commit()
                db.refresh(ev)

                updated = EventSchema.model_validate(ev)
                self.status = updated.status
                self.updated_at = updated.updated_at
                return updated

        except Exception:
            logger.exception("Failed to mark event succeeded %s", self)
            return None

    def mark_failed(self, error: str) -> Optional["EventSchema"]:
        try:
            with get_trades_db() as db:
                ev = db.query(EventORM).filter(EventORM.id == self.id).one_or_none()
                if not ev:
                    logger.warning("Event to mark failed not found: %s", self.id)
                    return None

                give_up = ev.attempts >= DEFAULT_MAX_ATTEMPTS
                ev.last_error   = error[:1000]
                ev.status       = EventStatus.DEAD.value if give_up else EventStatus.PENDING.value
                ev.available_at = datetime.now() + timedelta(seconds=compute_backoff(ev.attempts))

                db.commit()
                db.refresh(ev)

                updated = EventSchema.model_validate(ev)
                self.status       = updated.status
                self.available_at = updated.available_at
                self.last_error   = updated.last_error
                self.updated_at   = updated.updated_at
                return updated

        except Exception:
            logger.exception("Failed to mark event failed %s", self)
            return None
