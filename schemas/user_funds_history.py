# schemas/user_funds_history.py

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional, List, Dict, Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from database.database import get_trades_db
from models.trade_models import UserFundsHistory as UserFundsHistoryORM

logger = logging.getLogger(__name__)


class UserFundsHistorySchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    client_id: str
    trading_day: date
    snapshot_json: Dict[str, Any]
    polled_at: datetime

    # ----------------------
    # WRITE
    # ----------------------

    @staticmethod
    def create_snapshot(
        client_id: str,
        trading_day: date,
        snapshot_json: Dict[str, Any],
        polled_at: Optional[datetime] = None,
    ) -> Optional["UserFundsHistorySchema"]:
        """
        Append one history row for a poll snapshot.
        """
        client_id = str(client_id or "").strip()
        if not client_id or not trading_day or snapshot_json is None:
            logger.error(
                "Missing required fields for UserFundsHistory.create_snapshot "
                "(client_id=%s, trading_day=%s)",
                client_id, trading_day
            )
            return None

        payload = {
            "client_id": client_id,
            "trading_day": trading_day,
            "snapshot_json": snapshot_json,
            "polled_at": polled_at or datetime.now(),
        }

        try:
            with get_trades_db() as db:
                rec = UserFundsHistoryORM(**payload)
                db.add(rec)
                db.commit()
                db.refresh(rec)

            return UserFundsHistorySchema.model_validate(rec)

        except SQLAlchemyError as e:
            logger.error(
                "Error inserting funds history for client_id=%s trading_day=%s: %s",
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
    ) -> List["UserFundsHistorySchema"]:
        client_id = str(client_id or "").strip()
        if not client_id or not trading_day:
            return []

        try:
            with get_trades_db() as db:
                rows = (
                    db.query(UserFundsHistoryORM)
                    .filter(UserFundsHistoryORM.client_id == client_id)
                    .filter(UserFundsHistoryORM.trading_day == trading_day)
                    .order_by(UserFundsHistoryORM.polled_at.asc())
                    .all()
                )

            return [UserFundsHistorySchema.model_validate(r) for r in rows]

        except Exception as e:
            logger.error(
                "Error fetching funds history for client_id=%s trading_day=%s: %s",
                client_id, trading_day, e, exc_info=True
            )
            return []

    @staticmethod
    def fetch_latest_for_user(
        client_id: str,
    ) -> Optional["UserFundsHistorySchema"]:
        client_id = str(client_id or "").strip()
        if not client_id:
            return None

        try:
            with get_trades_db() as db:
                rec = (
                    db.query(UserFundsHistoryORM)
                    .filter(UserFundsHistoryORM.client_id == client_id)
                    .order_by(
                        UserFundsHistoryORM.trading_day.desc(),
                        UserFundsHistoryORM.polled_at.desc(),
                    )
                    .first()
                )

            return UserFundsHistorySchema.model_validate(rec) if rec else None

        except Exception as e:
            logger.error(
                "Error fetching latest funds history for client_id=%s: %s",
                client_id, e, exc_info=True
            )
            return None

    @staticmethod
    def fetch_latest_for_user_day(
        client_id: str,
        trading_day: date,
    ) -> Optional["UserFundsHistorySchema"]:
        client_id = str(client_id or "").strip()
        if not client_id or not trading_day:
            return None

        try:
            with get_trades_db() as db:
                rec = (
                    db.query(UserFundsHistoryORM)
                    .filter(UserFundsHistoryORM.client_id == client_id)
                    .filter(UserFundsHistoryORM.trading_day == trading_day)
                    .order_by(UserFundsHistoryORM.polled_at.desc())
                    .first()
                )

            return UserFundsHistorySchema.model_validate(rec) if rec else None

        except Exception as e:
            logger.error(
                "Error fetching latest funds history for client_id=%s trading_day=%s: %s",
                client_id, trading_day, e, exc_info=True
            )
            return None

    @staticmethod
    def fetch_for_users_day(
        client_ids: List[str],
        trading_day: date,
    ) -> List["UserFundsHistorySchema"]:
        client_ids = [str(c).strip() for c in (client_ids or []) if str(c).strip()]
        if not client_ids or not trading_day:
            return []

        try:
            with get_trades_db() as db:
                rows = (
                    db.query(UserFundsHistoryORM)
                    .filter(UserFundsHistoryORM.client_id.in_(client_ids))
                    .filter(UserFundsHistoryORM.trading_day == trading_day)
                    .order_by(
                        UserFundsHistoryORM.client_id.asc(),
                        UserFundsHistoryORM.polled_at.asc(),
                    )
                    .all()
                )

            return [UserFundsHistorySchema.model_validate(r) for r in rows]

        except Exception as e:
            logger.error(
                "Error fetching funds history for multiple users trading_day=%s: %s",
                trading_day, e, exc_info=True
            )
            return []

    # ----------------------
    # OPTIONAL CLEANUP HELPERS
    # ----------------------

    @staticmethod
    def delete_older_for_user_day_keep_latest(
        client_id: str,
        trading_day: date,
    ) -> int:
        """
        Optional cleanup helper:
        delete all rows for (client_id, trading_day) except the latest polled row.
        Useful only if you later decide to trim intraday history.
        """
        client_id = str(client_id or "").strip()
        if not client_id or not trading_day:
            return 0

        try:
            with get_trades_db() as db:
                rows = (
                    db.query(UserFundsHistoryORM)
                    .filter(UserFundsHistoryORM.client_id == client_id)
                    .filter(UserFundsHistoryORM.trading_day == trading_day)
                    .order_by(UserFundsHistoryORM.polled_at.desc(), UserFundsHistoryORM.id.desc())
                    .all()
                )

                if len(rows) <= 1:
                    return 0

                keep = rows[0].id
                deleted = (
                    db.query(UserFundsHistoryORM)
                    .filter(UserFundsHistoryORM.client_id == client_id)
                    .filter(UserFundsHistoryORM.trading_day == trading_day)
                    .filter(UserFundsHistoryORM.id != keep)
                    .delete(synchronize_session=False)
                )
                db.commit()
                return int(deleted or 0)

        except Exception as e:
            logger.error(
                "Error trimming funds history for client_id=%s trading_day=%s: %s",
                client_id, trading_day, e, exc_info=True
            )
            return 0