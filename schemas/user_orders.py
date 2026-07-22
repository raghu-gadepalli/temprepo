from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, List, Dict, Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from database.database import get_trades_db
from models.trade_models import UserOrders as UserOrdersORM

logger = logging.getLogger(__name__)


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


class UserOrdersSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trading_day: date
    client_id: str

    order_id: str
    exchange_order_id: Optional[str] = None

    tradingsymbol: Optional[str] = None
    instrument: Optional[str] = None
    instrument_token: Optional[int] = None

    exchange: Optional[str] = None
    transaction_type: Optional[str] = None
    product: Optional[str] = None
    order_type: Optional[str] = None
    variety: Optional[str] = None

    validity: Optional[str] = None
    validity_ttl: Optional[int] = None

    quantity: Optional[int] = None
    disclosed_quantity: Optional[int] = None
    filled_quantity: Optional[int] = None
    pending_quantity: Optional[int] = None
    cancelled_quantity: Optional[int] = None

    price: Optional[Decimal] = None
    average_price: Optional[Decimal] = None
    trigger_price: Optional[Decimal] = None

    status: Optional[str] = None

    order_timestamp: Optional[datetime] = None
    exchange_timestamp: Optional[datetime] = None

    tag: Optional[str] = None
    order_issued_at: Optional[str] = None
    order_placed_by: Optional[str] = None

    recon_status: Optional[str] = None

    created_at: Optional[datetime] = None
    first_seen_at: Optional[datetime] = None
    polled_at: datetime

    # ----------------------
    # READ
    # ----------------------

    @staticmethod
    def fetch_for_user_day(client_id: str, trading_day: date) -> List["UserOrdersSchema"]:
        client_id = str(client_id or "").strip()
        if not client_id or not trading_day:
            return []

        try:
            with get_trades_db() as db:
                rows = (
                    db.query(UserOrdersORM)
                    .filter(UserOrdersORM.client_id == client_id)
                    .filter(UserOrdersORM.trading_day == trading_day)
                    .order_by(UserOrdersORM.order_timestamp.desc(), UserOrdersORM.order_id.desc())
                    .all()
                )
            return [UserOrdersSchema.model_validate(r) for r in rows]
        except Exception as e:
            logger.error(
                "Error fetching orders for client_id=%s trading_day=%s: %s",
                client_id, trading_day, e, exc_info=True
            )
            return []

    @staticmethod
    def fetch_latest_for_user(client_id: str) -> List["UserOrdersSchema"]:
        client_id = str(client_id or "").strip()
        if not client_id:
            return []

        try:
            with get_trades_db() as db:
                latest_day = (
                    db.query(UserOrdersORM.trading_day)
                    .filter(UserOrdersORM.client_id == client_id)
                    .order_by(UserOrdersORM.trading_day.desc())
                    .limit(1)
                    .scalar()
                )

                if not latest_day:
                    return []

                rows = (
                    db.query(UserOrdersORM)
                    .filter(UserOrdersORM.client_id == client_id)
                    .filter(UserOrdersORM.trading_day == latest_day)
                    .order_by(UserOrdersORM.order_timestamp.desc(), UserOrdersORM.order_id.desc())
                    .all()
                )

            return [UserOrdersSchema.model_validate(r) for r in rows]

        except Exception as e:
            logger.error(
                "Error fetching latest orders for client_id=%s: %s",
                client_id, e, exc_info=True
            )
            return []

    @staticmethod
    def fetch_for_users_day(client_ids: List[str], trading_day: date) -> List["UserOrdersSchema"]:
        client_ids = [str(c).strip() for c in (client_ids or []) if str(c).strip()]
        if not client_ids or not trading_day:
            return []

        try:
            with get_trades_db() as db:
                rows = (
                    db.query(UserOrdersORM)
                    .filter(UserOrdersORM.client_id.in_(client_ids))
                    .filter(UserOrdersORM.trading_day == trading_day)
                    .order_by(
                        UserOrdersORM.client_id.asc(),
                        UserOrdersORM.order_timestamp.desc(),
                        UserOrdersORM.order_id.desc(),
                    )
                    .all()
                )
            return [UserOrdersSchema.model_validate(r) for r in rows]
        except Exception as e:
            logger.error(
                "Error fetching orders for multiple users trading_day=%s: %s",
                trading_day, e, exc_info=True
            )
            return []

    @staticmethod
    def fetch_by_client_order_id(client_id: str, order_id: str) -> Optional["UserOrdersSchema"]:
        client_id = str(client_id or "").strip()
        order_id = str(order_id or "").strip()
        if not client_id or not order_id:
            return None

        try:
            with get_trades_db() as db:
                rec = (
                    db.query(UserOrdersORM)
                    .filter(UserOrdersORM.client_id == client_id)
                    .filter(UserOrdersORM.order_id == order_id)
                    .one_or_none()
                )
            return UserOrdersSchema.model_validate(rec) if rec else None
        except Exception as e:
            logger.error(
                "Error fetching order for client_id=%s order_id=%s: %s",
                client_id, order_id, e, exc_info=True
            )
            return None

    # ----------------------
    # WRITE
    # ----------------------

    @staticmethod
    def upsert_order(data: Dict[str, Any]) -> Optional["UserOrdersSchema"]:
        """
        Upsert one row for (client_id, order_id).
        Preserves first_seen_at for existing rows.
        """
        client_id = str(data.get("client_id") or "").strip()
        order_id = str(data.get("order_id") or "").strip()
        trading_day = data.get("trading_day")

        if not client_id or not order_id or not trading_day:
            logger.error("Missing client_id, order_id or trading_day in upsert_order")
            return None

        now = data.get("polled_at") or datetime.now()

        payload = {
            "trading_day": trading_day,
            "client_id": client_id,
            "order_id": order_id,
            "exchange_order_id": data.get("exchange_order_id"),
            "tradingsymbol": data.get("tradingsymbol"),
            "instrument": data.get("instrument"),
            "instrument_token": _to_int(data.get("instrument_token")),
            "exchange": data.get("exchange"),
            "transaction_type": data.get("transaction_type"),
            "product": data.get("product"),
            "order_type": data.get("order_type"),
            "variety": data.get("variety"),
            "validity": data.get("validity"),
            "validity_ttl": _to_int(data.get("validity_ttl")),
            "quantity": _to_int(data.get("quantity")),
            "disclosed_quantity": _to_int(data.get("disclosed_quantity")),
            "filled_quantity": _to_int(data.get("filled_quantity")),
            "pending_quantity": _to_int(data.get("pending_quantity")),
            "cancelled_quantity": _to_int(data.get("cancelled_quantity")),
            "price": _to_decimal(data.get("price")),
            "average_price": _to_decimal(data.get("average_price")),
            "trigger_price": _to_decimal(data.get("trigger_price")),
            "status": data.get("status"),
            "order_timestamp": data.get("order_timestamp"),
            "exchange_timestamp": data.get("exchange_timestamp"),
            "tag": data.get("tag"),
            "order_issued_at": data.get("order_issued_at"),
            "order_placed_by": data.get("order_placed_by"),
            "recon_status": data.get("recon_status"),
            "polled_at": now,
        }

        try:
            with get_trades_db() as db:
                rec = (
                    db.query(UserOrdersORM)
                    .filter(UserOrdersORM.client_id == client_id)
                    .filter(UserOrdersORM.order_id == order_id)
                    .one_or_none()
                )

                if rec:
                    first_seen = rec.first_seen_at
                    for k, v in payload.items():
                        setattr(rec, k, v)
                    rec.first_seen_at = first_seen
                else:
                    rec = UserOrdersORM(
                        **payload,
                        first_seen_at=data.get("first_seen_at") or now,
                    )
                    db.add(rec)

                db.commit()
                db.refresh(rec)

            return UserOrdersSchema.model_validate(rec)

        except SQLAlchemyError as e:
            logger.error(
                "Error upserting order for client_id=%s order_id=%s: %s",
                client_id, order_id, e, exc_info=True
            )
            return None

    @staticmethod
    def delete_for_user_day(client_id: str, trading_day: date) -> int:
        """
        Optional cleanup helper if ever needed.
        Orders are normally upserted, not delete+insert like positions.
        """
        client_id = str(client_id or "").strip()
        if not client_id or not trading_day:
            return 0

        try:
            with get_trades_db() as db:
                deleted = (
                    db.query(UserOrdersORM)
                    .filter(UserOrdersORM.client_id == client_id)
                    .filter(UserOrdersORM.trading_day == trading_day)
                    .delete(synchronize_session=False)
                )
                db.commit()
            return int(deleted or 0)

        except SQLAlchemyError as e:
            logger.error(
                "Error deleting orders for client_id=%s trading_day=%s: %s",
                client_id, trading_day, e, exc_info=True
            )
            return 0

    # ----------------------
    # UI helper
    # ----------------------

    def to_ui_dict(self) -> Dict[str, Any]:
        return {
            "client_id": self.client_id,
            "trading_day": self.trading_day.isoformat() if self.trading_day else None,
            "order_id": self.order_id,
            "exchange_order_id": self.exchange_order_id,
            "tradingsymbol": self.tradingsymbol,
            "instrument": self.instrument,
            "instrument_token": self.instrument_token,
            "exchange": self.exchange,
            "transaction_type": self.transaction_type,
            "product": self.product,
            "order_type": self.order_type,
            "variety": self.variety,
            "validity": self.validity,
            "validity_ttl": self.validity_ttl,
            "quantity": self.quantity,
            "disclosed_quantity": self.disclosed_quantity,
            "filled_quantity": self.filled_quantity,
            "pending_quantity": self.pending_quantity,
            "cancelled_quantity": self.cancelled_quantity,
            "price": float(self.price) if self.price is not None else None,
            "average_price": float(self.average_price) if self.average_price is not None else None,
            "trigger_price": float(self.trigger_price) if self.trigger_price is not None else None,
            "status": self.status,
            "order_timestamp": self.order_timestamp.isoformat() if self.order_timestamp else None,
            "exchange_timestamp": self.exchange_timestamp.isoformat() if self.exchange_timestamp else None,
            "tag": self.tag,
            "order_issued_at": self.order_issued_at,
            "order_placed_by": self.order_placed_by,
            "recon_status": self.recon_status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "first_seen_at": self.first_seen_at.isoformat() if self.first_seen_at else None,
            "polled_at": self.polled_at.isoformat() if self.polled_at else None,
        }