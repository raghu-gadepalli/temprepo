from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional, List

import pytz
from sqlalchemy import asc
from pydantic import BaseModel

from database.database import get_trades_db
from models.trade_models import Instrument as InstrumentORM, Symbol as SymbolORM
from enums.enums import SymbolType
from utils.datetime_utils import current_fo_expiry

logger = logging.getLogger(__name__)


class InstrumentSchema(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    instrument_token: str
    exchange_token: str
    tradingsymbol: str
    name: str
    last_price: Optional[float] = None
    expiry: Optional[date] = None
    strike: Optional[float] = None
    tick_size: Optional[float] = None
    lot_size: Optional[float] = None
    instrument_type: str
    segment: str
    exchange: str

    @staticmethod
    def create_instrument(instrument_data: dict) -> InstrumentSchema:
        valid_fields = {
            "instrument_token", "exchange_token", "tradingsymbol", "name",
            "last_price", "expiry", "strike", "tick_size", "lot_size",
            "instrument_type", "segment", "exchange",
        }
        filtered = {k: v for k, v in instrument_data.items() if k in valid_fields}
        with get_trades_db() as db:
            inst = InstrumentORM(**filtered)
            db.add(inst)
            db.commit()
            db.refresh(inst)
        return InstrumentSchema.model_validate(inst)

    @staticmethod
    def fetch_instruments() -> List[InstrumentSchema]:
        with get_trades_db() as db:
            rows = db.query(InstrumentORM).all()
        return [InstrumentSchema.model_validate(r) for r in rows]

    @staticmethod
    def fetch_instrument(tradingsymbol: str) -> Optional[InstrumentSchema]:
        with get_trades_db() as db:
            rec = (
                db.query(InstrumentORM)
                  .filter(InstrumentORM.tradingsymbol == tradingsymbol)
                  .one_or_none()
            )
        return InstrumentSchema.model_validate(rec) if rec else None

    @staticmethod
    def create_symbol_from_instrument(instrument: InstrumentORM) -> SymbolORM:
        """
        Build a Symbol ORM instance from an Instrument ORM instance.
        Does NOT persistcaller must upsert via SymbolSchema.
        """
        try:
            sym = SymbolORM(
                symbol=instrument.tradingsymbol,
                token=instrument.instrument_token,
                name=instrument.name,
                type=instrument.instrument_type,
                exchange=instrument.exchange,
                signal_profile="DEFAULT",
                generate_candles=1,
                merge_candles=1,
                update_performance=1,
                generate_signals=0,
                lotsize=instrument.lot_size,
                expiry=instrument.expiry,
                equity_ref=instrument.name,
                processed=0,
                active=1,
            )
            return sym
        except Exception as e:
            logger.exception(
                "Error creating SymbolORM from Instrument '%s': %s",
                instrument.tradingsymbol, e
            )
            raise

    @staticmethod
    def fetch_closest_future_for_equity(
        equity_symbol: str,
        as_of: Optional[date] = None
    ) -> Optional[InstrumentSchema]:
        as_of = as_of or datetime.now(pytz.timezone("Asia/Kolkata")).date()
        cutoff = current_fo_expiry()

        with get_trades_db() as db:
            orm = (
                db.query(InstrumentORM)
                  .filter(
                      InstrumentORM.tradingsymbol.like(f"%{equity_symbol}%"),
                      InstrumentORM.instrument_type == SymbolType.FUT.value,
                      InstrumentORM.expiry >= cutoff,
                      InstrumentORM.segment == "NFO-FUT",
                  )
                  .order_by(asc(InstrumentORM.expiry))
                  .first()
            )
        return InstrumentSchema.model_validate(orm) if orm else None

    @staticmethod
    def fetch_closest_option_for_future(
        fut_tradingsymbol: str,
        strike_price: float,
        as_of_date: date,
        is_buy: bool
    ) -> Optional[InstrumentSchema]:
        """
        1) Strip trailing 'FUT' from future symbol.
        2) Filter for front-month options / strike.
        3) Order by expiry asc, then strike asc (CE) or desc (PE).
        """
        if fut_tradingsymbol.upper().endswith("FUT"):
            base = fut_tradingsymbol[:-3]
        else:
            base = fut_tradingsymbol

        opt_type = SymbolType.CE.value if is_buy else SymbolType.PE.value

        with get_trades_db() as db:
            q = (
                db.query(InstrumentORM)
                  .filter(
                      InstrumentORM.tradingsymbol.like(f"{base}%"),
                      InstrumentORM.instrument_type == opt_type,
                      InstrumentORM.expiry >= as_of_date,
                  )
            )

            if is_buy:
                q = q.filter(InstrumentORM.strike >= strike_price) \
                     .order_by(InstrumentORM.expiry.asc(), InstrumentORM.strike.asc())
            else:
                q = q.filter(InstrumentORM.strike <= strike_price) \
                     .order_by(InstrumentORM.expiry.asc(), InstrumentORM.strike.desc())

            inst = q.first()
        return InstrumentSchema.model_validate(inst) if inst else None

    @staticmethod
    def fetch_by_underlying(underlying: str) -> list[InstrumentSchema]:
        """
        Return every instrument whose `name` exactly matches the given underlying
        symbol (e.g. 'SBIN', 'NIFTY', etc.).
        """
        try:
            with get_trades_db() as db:
                rows = (
                    db.query(InstrumentORM)
                      .filter(InstrumentORM.name == underlying.upper())
                      .all()
                )
            return [InstrumentSchema.model_validate(r) for r in rows]
        except Exception as e:
            logger.warning("Error fetching instruments for %s: %s", underlying, e, exc_info=True)
            return []
