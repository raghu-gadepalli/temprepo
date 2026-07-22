from __future__ import annotations

import logging
import json
from datetime import date, datetime
from typing import Optional, List, Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from database.database import get_trades_db
from models.trade_models import UserOrdersHistory as UserOrdersHistoryORM

logger = logging.getLogger(__name__)


def json_safe(obj):
    """Recursively convert non-JSON-serializable objects into safe formats."""
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_safe(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj


class UserOrdersHistorySchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    client_id: str
    trading_day: date
    polled_at: datetime
    broker_payload: Any

    @staticmethod
    def create_snapshot(
        client_id: str,
        trading_day: date,
        broker_payload: Any,
        polled_at: Optional[datetime] = None,
    ) -> Optional["UserOrdersHistorySchema"]:
        client_id = str(client_id or "").strip()
        if not client_id or not trading_day or broker_payload is None:
            logger.error(
                "Missing required fields for UserOrdersHistory.create_snapshot "
                "(client_id=%s trading_day=%s)",
                client_id,
                trading_day,
            )
            return None

        safe_payload = json_safe(broker_payload)

        payload = {
            "client_id": client_id,
            "trading_day": trading_day,
            "polled_at": polled_at or datetime.now(),
            "broker_payload": safe_payload,
        }

        try:
            with get_trades_db() as db:
                rec = UserOrdersHistoryORM(**payload)
                db.add(rec)
                db.commit()
                db.refresh(rec)

            return UserOrdersHistorySchema.model_validate(rec)

        except SQLAlchemyError as e:
            logger.error(
                "Error inserting orders history for client_id=%s trading_day=%s: %s",
                client_id,
                trading_day,
                e,
                exc_info=True,
            )
            return None

    @staticmethod
    def fetch_for_user_day(
        client_id: str,
        trading_day: date,
    ) -> List["UserOrdersHistorySchema"]:
        client_id = str(client_id or "").strip()
        if not client_id or not trading_day:
            return []

        try:
            with get_trades_db() as db:
                rows = (
                    db.query(UserOrdersHistoryORM)
                    .filter(UserOrdersHistoryORM.client_id == client_id)
                    .filter(UserOrdersHistoryORM.trading_day == trading_day)
                    .order_by(UserOrdersHistoryORM.polled_at.asc())
                    .all()
                )

            return [UserOrdersHistorySchema.model_validate(r) for r in rows]

        except Exception as e:
            logger.error(
                "Error fetching orders history for client_id=%s trading_day=%s: %s",
                client_id,
                trading_day,
                e,
                exc_info=True,
            )
            return []

    @staticmethod
    def fetch_latest_for_user(
        client_id: str,
    ) -> Optional["UserOrdersHistorySchema"]:
        client_id = str(client_id or "").strip()
        if not client_id:
            return None

        try:
            with get_trades_db() as db:
                rec = (
                    db.query(UserOrdersHistoryORM)
                    .filter(UserOrdersHistoryORM.client_id == client_id)
                    .order_by(
                        UserOrdersHistoryORM.trading_day.desc(),
                        UserOrdersHistoryORM.polled_at.desc(),
                    )
                    .first()
                )

            return UserOrdersHistorySchema.model_validate(rec) if rec else None

        except Exception as e:
            logger.error(
                "Error fetching latest orders history for client_id=%s: %s",
                client_id,
                e,
                exc_info=True,
            )
            return None

    @staticmethod
    def fetch_latest_for_user_day(
        client_id: str,
        trading_day: date,
    ) -> Optional["UserOrdersHistorySchema"]:
        client_id = str(client_id or "").strip()
        if not client_id or not trading_day:
            return None

        try:
            with get_trades_db() as db:
                rec = (
                    db.query(UserOrdersHistoryORM)
                    .filter(UserOrdersHistoryORM.client_id == client_id)
                    .filter(UserOrdersHistoryORM.trading_day == trading_day)
                    .order_by(UserOrdersHistoryORM.polled_at.desc())
                    .first()
                )

            return UserOrdersHistorySchema.model_validate(rec) if rec else None

        except Exception as e:
            logger.error(
                "Error fetching latest orders history for client_id=%s trading_day=%s: %s",
                client_id,
                trading_day,
                e,
                exc_info=True,
            )
            return None