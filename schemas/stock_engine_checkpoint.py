"""Schema/repository for one recoverable Auction Engine checkpoint per symbol."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from database.database import get_trades_db
from models.trade_models import StockEngineCheckpoint as StockEngineCheckpointORM
from utils.datetime_utils import to_ist_naive
from utils.json_utils import sanitize_json


def _required_time(value: Any, field_name: str) -> datetime:
    result = to_ist_naive(value)
    if result is None:
        raise ValueError(f"{field_name} is required; wall-clock fallback is forbidden")
    return result


class StockEngineCheckpoint(BaseModel):
    model_config = {"from_attributes": True}

    trading_day: date
    symbol: str
    engine_name: str
    engine_version: str
    config_version: str
    last_processed_snapshot_time: datetime
    last_snapshot_hash: Optional[str] = None
    checkpoint_status: str = "ACTIVE"
    checkpoint_version: int = Field(default=1, ge=1)
    state_json: Dict[str, Any] = Field(default_factory=dict)
    diagnostics_json: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    @classmethod
    def upsert(cls, payload: "StockEngineCheckpoint") -> "StockEngineCheckpoint":
        data = payload.model_dump()
        for field_name in (
            "last_processed_snapshot_time",
            "created_at",
            "updated_at",
        ):
            data[field_name] = _required_time(data[field_name], field_name)
        data["state_json"] = sanitize_json(data.get("state_json") or {})
        data["diagnostics_json"] = sanitize_json(data.get("diagnostics_json") or {})
        data["symbol"] = str(data["symbol"]).strip().upper()
        data["engine_name"] = str(data["engine_name"]).strip().upper()
        data["checkpoint_status"] = str(data["checkpoint_status"]).strip().upper()

        with get_trades_db() as db:
            row = (
                db.query(StockEngineCheckpointORM)
                .filter(StockEngineCheckpointORM.trading_day == payload.trading_day)
                .filter(StockEngineCheckpointORM.symbol == data["symbol"])
                .filter(StockEngineCheckpointORM.engine_name == data["engine_name"])
                .one_or_none()
            )
            if row is None:
                row = StockEngineCheckpointORM(**data)
                db.add(row)
            else:
                data["created_at"] = row.created_at
                data["checkpoint_version"] = int(row.checkpoint_version or 0) + 1
                for key, value in data.items():
                    setattr(row, key, value)
            db.commit()
            db.refresh(row)
            return cls.model_validate(row)

    @classmethod
    def fetch_one(
        cls,
        *,
        trading_day: date,
        symbol: str,
        engine_name: str,
    ) -> Optional["StockEngineCheckpoint"]:
        symbol_key = str(symbol).strip().upper()
        engine_key = str(engine_name).strip().upper()
        with get_trades_db() as db:
            row = (
                db.query(StockEngineCheckpointORM)
                .filter(StockEngineCheckpointORM.trading_day == trading_day)
                .filter(StockEngineCheckpointORM.symbol == symbol_key)
                .filter(StockEngineCheckpointORM.engine_name == engine_key)
                .one_or_none()
            )
        return cls.model_validate(row) if row is not None else None

    @classmethod
    def fetch_day(
        cls,
        *,
        trading_day: date,
        engine_name: str,
        symbols: Optional[List[str]] = None,
    ) -> List["StockEngineCheckpoint"]:
        engine_key = str(engine_name).strip().upper()
        with get_trades_db() as db:
            query = (
                db.query(StockEngineCheckpointORM)
                .filter(StockEngineCheckpointORM.trading_day == trading_day)
                .filter(StockEngineCheckpointORM.engine_name == engine_key)
            )
            if symbols:
                query = query.filter(
                    StockEngineCheckpointORM.symbol.in_([
                        str(symbol).strip().upper() for symbol in symbols
                    ])
                )
            # Keep the ORDER BY query narrow. ``state_json`` can be large, and
            # asking MySQL to filesort complete checkpoint rows can exhaust the
            # per-connection sort buffer even when only a few symbols are read
            # (error 1038 / HY001). Sort only integer primary keys, fetch the
            # wide JSON rows by primary key, then restore order in Python.
            ordered_ids = [
                row_id
                for (row_id,) in (
                    query.with_entities(StockEngineCheckpointORM.id)
                    .order_by(
                        StockEngineCheckpointORM.symbol.asc(),
                        StockEngineCheckpointORM.id.asc(),
                    )
                    .all()
                )
            ]
            if not ordered_ids:
                rows = []
            else:
                fetched = (
                    db.query(StockEngineCheckpointORM)
                    .filter(StockEngineCheckpointORM.id.in_(ordered_ids))
                    .all()
                )
                by_id = {int(row.id): row for row in fetched}
                rows = [by_id[row_id] for row_id in ordered_ids if row_id in by_id]
        return [cls.model_validate(row) for row in rows]

    @classmethod
    def delete_day(
        cls,
        *,
        trading_day: date,
        engine_name: str,
        symbols: Optional[List[str]] = None,
    ) -> int:
        engine_key = str(engine_name).strip().upper()
        with get_trades_db() as db:
            query = (
                db.query(StockEngineCheckpointORM)
                .filter(StockEngineCheckpointORM.trading_day == trading_day)
                .filter(StockEngineCheckpointORM.engine_name == engine_key)
            )
            if symbols:
                query = query.filter(StockEngineCheckpointORM.symbol.in_([
                    str(symbol).strip().upper() for symbol in symbols
                ]))
            count = query.delete(synchronize_session=False)
            db.commit()
        return int(count or 0)


__all__ = ["StockEngineCheckpoint"]
