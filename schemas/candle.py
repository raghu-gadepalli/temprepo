# schemas/candle.py

from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from database.database import get_trades_db
from models.trade_models import Candle as CandleORM

logger  = logging.getLogger(__name__)

class CandleSchema(BaseModel, frozen=True):
    model_config = {"from_attributes": True}

    id: Optional[int] = None
    symbol: str
    frequency: int
    candle_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    oi: float
    active: bool = True

    @staticmethod
    def create_candle(data: dict) -> CandleSchema:
        valid = {
            "symbol", "frequency", "candle_time",
            "open", "high", "low", "close",
            "volume", "oi", "active",
        }
        payload = {k: v for k, v in data.items() if k in valid}

        with get_trades_db() as db:
            orm = CandleORM(**payload)
            db.add(orm)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                logger.debug(
                    "Candle[%s/%s @ %s] duplicate  Ignoring Write",
                    payload["symbol"], payload["frequency"], payload["candle_time"],
                )
                existing = (
                    db.query(CandleORM)
                      .filter_by(
                          symbol=payload["symbol"],
                          frequency=payload["frequency"],
                          candle_time=payload["candle_time"],
                      )
                      .one_or_none()
                )
                if not existing:
                    raise
                return CandleSchema.model_validate(existing)

            db.refresh(orm)
        return CandleSchema.model_validate(orm)

    @staticmethod
    def fetch_candle(
        symbol: str,
        frequency: int,
        candle_time: datetime
    ) -> Optional[CandleSchema]:
        with get_trades_db() as db:
            rec = (
                db.query(CandleORM)
                  .filter_by(
                      symbol=symbol,
                      frequency=frequency,
                      candle_time=candle_time
                  )
                  .one_or_none()
            )
        return CandleSchema.model_validate(rec) if rec else None

    @staticmethod
    def fetch_candles(
        symbol: Optional[str] = None,
        frequency: Optional[int] = None,
        active: Optional[bool] = True,
        limit: Optional[int] = None
    ) -> List[CandleSchema]:
        with get_trades_db() as db:
            q = db.query(CandleORM)
            if symbol is not None:
                q = q.filter(CandleORM.symbol == symbol)
            if frequency is not None:
                q = q.filter(CandleORM.frequency == frequency)
            if active is not None:
                q = q.filter(CandleORM.active == active)
            q = q.order_by(CandleORM.candle_time.desc())
            if limit:
                q = q.limit(limit)
            rows = q.all()

        return [CandleSchema.model_validate(r) for r in rows]

    @staticmethod
    def update_candle(
        id: int,
        update_data: dict
    ) -> Optional[CandleSchema]:
        with get_trades_db() as db:
            rec = db.get(CandleORM, id)
            if not rec:
                return None
            for k, v in update_data.items():
                if k != "id":
                    setattr(rec, k, v)
            db.commit()
            db.refresh(rec)
        return CandleSchema.model_validate(rec)

    @staticmethod
    def delete_candle(id: int) -> bool:
        with get_trades_db() as db:
            rec = db.get(CandleORM, id)
            if not rec:
                return False
            rec.active = False
            db.commit()
        return True
