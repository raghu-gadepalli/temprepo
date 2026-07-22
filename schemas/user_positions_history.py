from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional, List, Dict, Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from database.database import get_trades_db
from models.trade_models import UserPositionsHistory as UserPositionsHistoryORM

logger = logging.getLogger(__name__)


class UserPositionsHistorySchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    client_id: str
    trading_day: date
    polled_at: datetime
    broker_payload: Any

    # ----------------------
    # WRITE
    # ----------------------

    @staticmethod
    def create_snapshot(
        client_id: str,
        trading_day: date,
        broker_payload: Any,
        polled_at: Optional[datetime] = None,
    ) -> Optional["UserPositionsHistorySchema"]:
        """
        Append one history row for a positions poll snapshot.
        """
        client_id = str(client_id or "").strip()
        if not client_id or not trading_day or broker_payload is None:
            logger.error(
                "Missing required fields for UserPositionsHistory.create_snapshot "
                "(client_id=%s trading_day=%s)",
                client_id, trading_day
            )
            return None

        payload = {
            "client_id": client_id,
            "trading_day": trading_day,
            "polled_at": polled_at or datetime.now(),
            "broker_payload": broker_payload,
        }

        try:
            with get_trades_db() as db:
                rec = UserPositionsHistoryORM(**payload)
                db.add(rec)
                db.commit()
                db.refresh(rec)

            return UserPositionsHistorySchema.model_validate(rec)

        except SQLAlchemyError as e:
            logger.error(
                "Error inserting positions history for client_id=%s trading_day=%s: %s",
                client_id, trading_day, e, exc_info=True
            )
            return None

    # ----------------------
    # READ
    # ----------------------

    @staticmethod
    def fetch_for_user_day(
        client_id: str,
        trading_day: date,
    ) -> List["UserPositionsHistorySchema"]:
        client_id = str(client_id or "").strip()
        if not client_id or not trading_day:
            return []

        try:
            with get_trades_db() as db:
                rows = (
                    db.query(UserPositionsHistoryORM)
                    .filter(UserPositionsHistoryORM.client_id == client_id)
                    .filter(UserPositionsHistoryORM.trading_day == trading_day)
                    .order_by(UserPositionsHistoryORM.polled_at.asc())
                    .all()
                )

            return [UserPositionsHistorySchema.model_validate(r) for r in rows]

        except Exception as e:
            logger.error(
                "Error fetching positions history for client_id=%s trading_day=%s: %s",
                client_id, trading_day, e, exc_info=True
            )
            return []

    @staticmethod
    def fetch_latest_for_user(
        client_id: str,
    ) -> Optional["UserPositionsHistorySchema"]:
        client_id = str(client_id or "").strip()
        if not client_id:
            return None

        try:
            with get_trades_db() as db:
                rec = (
                    db.query(UserPositionsHistoryORM)
                    .filter(UserPositionsHistoryORM.client_id == client_id)
                    .order_by(
                        UserPositionsHistoryORM.trading_day.desc(),
                        UserPositionsHistoryORM.polled_at.desc(),
                    )
                    .first()
                )

            return UserPositionsHistorySchema.model_validate(rec) if rec else None

        except Exception as e:
            logger.error(
                "Error fetching latest positions history for client_id=%s: %s",
                client_id, e, exc_info=True
            )
            return None

    @staticmethod
    def fetch_latest_for_user_day(
        client_id: str,
        trading_day: date,
    ) -> Optional["UserPositionsHistorySchema"]:
        client_id = str(client_id or "").strip()
        if not client_id or not trading_day:
            return None

        try:
            with get_trades_db() as db:
                rec = (
                    db.query(UserPositionsHistoryORM)
                    .filter(UserPositionsHistoryORM.client_id == client_id)
                    .filter(UserPositionsHistoryORM.trading_day == trading_day)
                    .order_by(UserPositionsHistoryORM.polled_at.desc())
                    .first()
                )

            return UserPositionsHistorySchema.model_validate(rec) if rec else None

        except Exception as e:
            logger.error(
                "Error fetching latest positions history for client_id=%s trading_day=%s: %s",
                client_id, trading_day, e, exc_info=True
            )
            return None