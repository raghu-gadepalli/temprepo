from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, List, Dict, Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from database.database import get_trades_db
from models.trade_models import UserFunds as UserFundsORM

logger = logging.getLogger(__name__)


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


class UserFundsSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trading_day: date
    client_id: str

    net_balance: Optional[Decimal] = None
    available_cash: Optional[Decimal] = None
    opening_balance: Optional[Decimal] = None
    live_balance: Optional[Decimal] = None
    collateral: Optional[Decimal] = None
    utilised_margin: Optional[Decimal] = None

    span_margin: Optional[Decimal] = None
    exposure_margin: Optional[Decimal] = None
    option_premium: Optional[Decimal] = None
    m2m_realised: Optional[Decimal] = None
    m2m_unrealised: Optional[Decimal] = None

    available_margin: Optional[Decimal] = None
    polled_at: datetime

    # ----------------------
    # READ
    # ----------------------

    @staticmethod
    def fetch_for_user_day(client_id: str, trading_day: date) -> Optional["UserFundsSchema"]:
        try:
            with get_trades_db() as db:
                rec = (
                    db.query(UserFundsORM)
                    .filter(UserFundsORM.client_id == client_id)
                    .filter(UserFundsORM.trading_day == trading_day)
                    .one_or_none()
                )
            return UserFundsSchema.model_validate(rec) if rec else None
        except Exception as e:
            logger.error(
                "Error fetching funds for client_id=%s trading_day=%s: %s",
                client_id, trading_day, e, exc_info=True
            )
            return None

    @staticmethod
    def fetch_latest_for_user(client_id: str) -> Optional["UserFundsSchema"]:
        try:
            with get_trades_db() as db:
                rec = (
                    db.query(UserFundsORM)
                    .filter(UserFundsORM.client_id == client_id)
                    .order_by(UserFundsORM.trading_day.desc(), UserFundsORM.polled_at.desc())
                    .first()
                )
            return UserFundsSchema.model_validate(rec) if rec else None
        except Exception as e:
            logger.error("Error fetching latest funds for client_id=%s: %s", client_id, e, exc_info=True)
            return None

    @staticmethod
    def fetch_for_users_day(client_ids: List[str], trading_day: date) -> List["UserFundsSchema"]:
        client_ids = [c for c in (client_ids or []) if c]
        if not client_ids:
            return []

        try:
            with get_trades_db() as db:
                rows = (
                    db.query(UserFundsORM)
                    .filter(UserFundsORM.client_id.in_(client_ids))
                    .filter(UserFundsORM.trading_day == trading_day)
                    .order_by(UserFundsORM.client_id.asc())
                    .all()
                )
            return [UserFundsSchema.model_validate(r) for r in rows]
        except Exception as e:
            logger.error(
                "Error fetching funds for multiple users trading_day=%s: %s",
                trading_day, e, exc_info=True
            )
            return []

    # ----------------------
    # WRITE
    # ----------------------

    @staticmethod
    def upsert_for_user_day(data: Dict[str, Any]) -> Optional["UserFundsSchema"]:
        """
        Upsert one row for (trading_day, client_id).
        """
        required_client = str(data.get("client_id") or "").strip()
        trading_day = data.get("trading_day")

        if not required_client or not trading_day:
            logger.error("Missing client_id or trading_day in upsert_for_user_day")
            return None

        payload = {
            "trading_day": trading_day,
            "client_id": required_client,
            "net_balance": _to_decimal(data.get("net_balance")),
            "available_cash": _to_decimal(data.get("available_cash")),
            "opening_balance": _to_decimal(data.get("opening_balance")),
            "live_balance": _to_decimal(data.get("live_balance")),
            "collateral": _to_decimal(data.get("collateral")),
            "utilised_margin": _to_decimal(data.get("utilised_margin")),
            "span_margin": _to_decimal(data.get("span_margin")),
            "exposure_margin": _to_decimal(data.get("exposure_margin")),
            "option_premium": _to_decimal(data.get("option_premium")),
            "m2m_realised": _to_decimal(data.get("m2m_realised")),
            "m2m_unrealised": _to_decimal(data.get("m2m_unrealised")),
            "available_margin": _to_decimal(data.get("available_margin")),
            "polled_at": data.get("polled_at") or datetime.now(),
        }

        try:
            with get_trades_db() as db:
                rec = (
                    db.query(UserFundsORM)
                    .filter(UserFundsORM.client_id == payload["client_id"])
                    .filter(UserFundsORM.trading_day == payload["trading_day"])
                    .one_or_none()
                )

                if rec:
                    for k, v in payload.items():
                        setattr(rec, k, v)
                else:
                    rec = UserFundsORM(**payload)
                    db.add(rec)

                db.commit()
                db.refresh(rec)

            return UserFundsSchema.model_validate(rec)

        except SQLAlchemyError as e:
            logger.error(
                "Error upserting funds for client_id=%s trading_day=%s: %s",
                payload["client_id"], payload["trading_day"], e, exc_info=True
            )
            return None

    # ----------------------
    # UI/route helper
    # ----------------------

    def to_ui_dict(self) -> Dict[str, Any]:
        return {
            "userid": self.client_id,
            "total_balance": float(self.net_balance) if self.net_balance is not None else None,
            "available_margin": float(self.available_margin) if self.available_margin is not None else None,
            "opening_balance": float(self.opening_balance) if self.opening_balance is not None else None,
            "live_balance": float(self.live_balance) if self.live_balance is not None else None,
            "intraday_payin": None,
            "collateral": float(self.collateral) if self.collateral is not None else None,
            "adhoc_margin": None,
            "utilized_margin_total": float(self.utilised_margin) if self.utilised_margin is not None else None,
            "utilized_margin_details": {
                "Span": float(self.span_margin) if self.span_margin is not None else None,
                "Exposure": float(self.exposure_margin) if self.exposure_margin is not None else None,
                "Option Premium": float(self.option_premium) if self.option_premium is not None else None,
                "M2M Realised": float(self.m2m_realised) if self.m2m_realised is not None else None,
                "M2M Unrealised": float(self.m2m_unrealised) if self.m2m_unrealised is not None else None,
            },
            "polled_at": self.polled_at.isoformat() if self.polled_at else None,
        }