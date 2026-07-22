from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, List, Dict, Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from database.database import get_trades_db
from models.trade_models import UserPositions as UserPositionsORM

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


class UserPositionsSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trading_day: date
    client_id: str

    tradingsymbol: str
    instrument: Optional[str] = None
    instrument_token: Optional[int] = None

    exchange: Optional[str] = None
    segment: Optional[str] = None
    product: Optional[str] = None

    quantity: Optional[int] = None
    overnight_quantity: Optional[int] = None
    multiplier: Optional[Decimal] = None

    average_price: Optional[Decimal] = None
    close_price: Optional[Decimal] = None
    last_price: Optional[Decimal] = None

    value: Optional[Decimal] = None

    pnl: Optional[Decimal] = None
    m2m: Optional[Decimal] = None
    unrealised: Optional[Decimal] = None
    realised: Optional[Decimal] = None

    buy_quantity: Optional[int] = None
    buy_price: Optional[Decimal] = None
    buy_value: Optional[Decimal] = None
    buy_m2m: Optional[Decimal] = None

    sell_quantity: Optional[int] = None
    sell_price: Optional[Decimal] = None
    sell_value: Optional[Decimal] = None
    sell_m2m: Optional[Decimal] = None

    day_buy_quantity: Optional[int] = None
    day_buy_price: Optional[Decimal] = None
    day_buy_value: Optional[Decimal] = None

    day_sell_quantity: Optional[int] = None
    day_sell_price: Optional[Decimal] = None
    day_sell_value: Optional[Decimal] = None

    polled_at: datetime
    created_at: Optional[datetime] = None

    # ----------------------
    # READ
    # ----------------------

    @staticmethod
    def fetch_for_user_day(client_id: str, trading_day: date) -> List["UserPositionsSchema"]:
        client_id = str(client_id or "").strip()
        if not client_id or not trading_day:
            return []

        try:
            with get_trades_db() as db:
                rows = (
                    db.query(UserPositionsORM)
                    .filter(UserPositionsORM.client_id == client_id)
                    .filter(UserPositionsORM.trading_day == trading_day)
                    .order_by(UserPositionsORM.tradingsymbol.asc(), UserPositionsORM.product.asc())
                    .all()
                )
            return [UserPositionsSchema.model_validate(r) for r in rows]
        except Exception as e:
            logger.error(
                "Error fetching positions for client_id=%s trading_day=%s: %s",
                client_id, trading_day, e, exc_info=True
            )
            return []

    @staticmethod
    def fetch_latest_for_user(client_id: str) -> List["UserPositionsSchema"]:
        """
        Fetch latest trading-day snapshot rows for a user.
        """
        client_id = str(client_id or "").strip()
        if not client_id:
            return []

        try:
            with get_trades_db() as db:
                latest_day = (
                    db.query(UserPositionsORM.trading_day)
                    .filter(UserPositionsORM.client_id == client_id)
                    .order_by(UserPositionsORM.trading_day.desc())
                    .limit(1)
                    .scalar()
                )

                if not latest_day:
                    return []

                rows = (
                    db.query(UserPositionsORM)
                    .filter(UserPositionsORM.client_id == client_id)
                    .filter(UserPositionsORM.trading_day == latest_day)
                    .order_by(UserPositionsORM.tradingsymbol.asc(), UserPositionsORM.product.asc())
                    .all()
                )

            return [UserPositionsSchema.model_validate(r) for r in rows]

        except Exception as e:
            logger.error(
                "Error fetching latest positions for client_id=%s: %s",
                client_id, e, exc_info=True
            )
            return []

    @staticmethod
    def fetch_for_users_day(client_ids: List[str], trading_day: date) -> List["UserPositionsSchema"]:
        client_ids = [str(c).strip() for c in (client_ids or []) if str(c).strip()]
        if not client_ids or not trading_day:
            return []

        try:
            with get_trades_db() as db:
                rows = (
                    db.query(UserPositionsORM)
                    .filter(UserPositionsORM.client_id.in_(client_ids))
                    .filter(UserPositionsORM.trading_day == trading_day)
                    .order_by(
                        UserPositionsORM.client_id.asc(),
                        UserPositionsORM.tradingsymbol.asc(),
                        UserPositionsORM.product.asc(),
                    )
                    .all()
                )
            return [UserPositionsSchema.model_validate(r) for r in rows]
        except Exception as e:
            logger.error(
                "Error fetching positions for multiple users trading_day=%s: %s",
                trading_day, e, exc_info=True
            )
            return []

    # ----------------------
    # WRITE
    # ----------------------

    @staticmethod
    def delete_for_user_day(client_id: str, trading_day: date) -> int:
        """
        Delete current snapshot rows for one user/day.
        """
        client_id = str(client_id or "").strip()
        if not client_id or not trading_day:
            return 0

        try:
            with get_trades_db() as db:
                deleted = (
                    db.query(UserPositionsORM)
                    .filter(UserPositionsORM.client_id == client_id)
                    .filter(UserPositionsORM.trading_day == trading_day)
                    .delete(synchronize_session=False)
                )
                db.commit()
            return int(deleted or 0)

        except SQLAlchemyError as e:
            logger.error(
                "Error deleting positions for client_id=%s trading_day=%s: %s",
                client_id, trading_day, e, exc_info=True
            )
            return 0

    @staticmethod
    def bulk_insert_for_user_day(rows: List[Dict[str, Any]]) -> int:
        """
        Bulk insert fresh snapshot rows for one user/day.
        Assumes caller has already deleted existing rows for that user/day.
        """
        if not rows:
            return 0

        payload_rows: List[Dict[str, Any]] = []

        for row in rows:
            client_id = str(row.get("client_id") or "").strip()
            trading_day = row.get("trading_day")
            tradingsymbol = str(row.get("tradingsymbol") or "").strip()

            if not client_id or not trading_day or not tradingsymbol:
                logger.warning(
                    "Skipping invalid position row during bulk insert "
                    "(client_id=%s trading_day=%s tradingsymbol=%s)",
                    client_id, trading_day, tradingsymbol
                )
                continue

            payload_rows.append({
                "trading_day": trading_day,
                "client_id": client_id,
                "tradingsymbol": tradingsymbol,
                "instrument": row.get("instrument"),
                "instrument_token": _to_int(row.get("instrument_token")),
                "exchange": row.get("exchange"),
                "segment": row.get("segment"),
                "product": row.get("product"),
                "quantity": _to_int(row.get("quantity")),
                "overnight_quantity": _to_int(row.get("overnight_quantity")),
                "multiplier": _to_decimal(row.get("multiplier")),
                "average_price": _to_decimal(row.get("average_price")),
                "close_price": _to_decimal(row.get("close_price")),
                "last_price": _to_decimal(row.get("last_price")),
                "value": _to_decimal(row.get("value")),
                "pnl": _to_decimal(row.get("pnl")),
                "m2m": _to_decimal(row.get("m2m")),
                "unrealised": _to_decimal(row.get("unrealised")),
                "realised": _to_decimal(row.get("realised")),
                "buy_quantity": _to_int(row.get("buy_quantity")),
                "buy_price": _to_decimal(row.get("buy_price")),
                "buy_value": _to_decimal(row.get("buy_value")),
                "buy_m2m": _to_decimal(row.get("buy_m2m")),
                "sell_quantity": _to_int(row.get("sell_quantity")),
                "sell_price": _to_decimal(row.get("sell_price")),
                "sell_value": _to_decimal(row.get("sell_value")),
                "sell_m2m": _to_decimal(row.get("sell_m2m")),
                "day_buy_quantity": _to_int(row.get("day_buy_quantity")),
                "day_buy_price": _to_decimal(row.get("day_buy_price")),
                "day_buy_value": _to_decimal(row.get("day_buy_value")),
                "day_sell_quantity": _to_int(row.get("day_sell_quantity")),
                "day_sell_price": _to_decimal(row.get("day_sell_price")),
                "day_sell_value": _to_decimal(row.get("day_sell_value")),
                "polled_at": row.get("polled_at") or datetime.now(),
            })

        if not payload_rows:
            return 0

        try:
            with get_trades_db() as db:
                db.bulk_insert_mappings(UserPositionsORM, payload_rows)
                db.commit()
            return len(payload_rows)

        except SQLAlchemyError as e:
            logger.error(
                "Error bulk inserting positions rows=%s: %s",
                len(payload_rows), e, exc_info=True
            )
            return 0

    # ----------------------
    # UI helper
    # ----------------------

    def to_ui_dict(self) -> Dict[str, Any]:
        return {
            "client_id": self.client_id,
            "trading_day": self.trading_day.isoformat() if self.trading_day else None,
            "tradingsymbol": self.tradingsymbol,
            "instrument": self.instrument,
            "exchange": self.exchange,
            "segment": self.segment,
            "product": self.product,
            "quantity": self.quantity,
            "overnight_quantity": self.overnight_quantity,
            "average_price": float(self.average_price) if self.average_price is not None else None,
            "close_price": float(self.close_price) if self.close_price is not None else None,
            "last_price": float(self.last_price) if self.last_price is not None else None,
            "value": float(self.value) if self.value is not None else None,
            "pnl": float(self.pnl) if self.pnl is not None else None,
            "m2m": float(self.m2m) if self.m2m is not None else None,
            "unrealised": float(self.unrealised) if self.unrealised is not None else None,
            "realised": float(self.realised) if self.realised is not None else None,
            "buy_quantity": self.buy_quantity,
            "buy_price": float(self.buy_price) if self.buy_price is not None else None,
            "buy_value": float(self.buy_value) if self.buy_value is not None else None,
            "sell_quantity": self.sell_quantity,
            "sell_price": float(self.sell_price) if self.sell_price is not None else None,
            "sell_value": float(self.sell_value) if self.sell_value is not None else None,
            "day_buy_quantity": self.day_buy_quantity,
            "day_buy_price": float(self.day_buy_price) if self.day_buy_price is not None else None,
            "day_buy_value": float(self.day_buy_value) if self.day_buy_value is not None else None,
            "day_sell_quantity": self.day_sell_quantity,
            "day_sell_price": float(self.day_sell_price) if self.day_sell_price is not None else None,
            "day_sell_value": float(self.day_sell_value) if self.day_sell_value is not None else None,
            "polled_at": self.polled_at.isoformat() if self.polled_at else None,
        }