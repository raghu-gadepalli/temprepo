"""Pydantic schema and repository helper for the opportunity ledger.

Pattern alignment:
- ORM definition lives in ``models.trade_models``.
- Pydantic validation and DB IO live in this schema module.
- Snapshot/replay timestamps are mandatory; wall-clock fallbacks are forbidden.
- Auction service persistence writes only changed opportunity projections.
- One ``stock_opportunities`` row stores its compact lifecycle/event history in
  JSON; current state and query-critical timestamps remain normal columns.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from database.database import get_trades_db
from models.trade_models import StockOpportunity as StockOpportunityORM
from utils.datetime_utils import to_ist_naive
from utils.json_utils import sanitize_json


def _required_time(value: Any, name: str) -> datetime:
    result = to_ist_naive(value)
    if result is None:
        raise ValueError(f"{name} is required; wall-clock fallback is forbidden")
    return result


class StockOpportunity(BaseModel):
    trading_day: date
    symbol: str
    opportunity_key: str
    boundary_event_key: str
    range_id: Optional[str] = None
    side: str
    primary_setup_family: str
    primary_setup_subtype: str
    primary_candidate_id: str
    primary_candidate_role: str
    lifecycle_state: str
    attempt_time: Optional[datetime] = None
    first_observed_time: datetime
    last_observed_time: datetime
    eligible_time: Optional[datetime] = None
    terminal_time: Optional[datetime] = None
    selected_time: Optional[datetime] = None
    consumed_time: Optional[datetime] = None
    entry_anchor_price: Optional[Decimal] = None
    boundary_price: Optional[Decimal] = None
    stop_anchor_price: Optional[Decimal] = None
    target_basis: Optional[str] = None
    target_reference_price: Optional[Decimal] = None
    source_auction_state: Optional[str] = None
    established_trend_side: Optional[str] = None
    candidate_interpretations_json: List[Dict[str, Any]] = Field(default_factory=list)
    event_history_json: List[Dict[str, Any]] = Field(default_factory=list)
    reason_codes_json: List[str] = Field(default_factory=list)
    diagnostics_json: Dict[str, Any] = Field(default_factory=dict)
    config_version: str
    signal_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def upsert(cls, payload: "StockOpportunity") -> "StockOpportunity":
        data = payload.model_dump()
        for field in ("first_observed_time", "last_observed_time", "created_at", "updated_at"):
            data[field] = _required_time(data[field], field)
        for field in ("attempt_time", "eligible_time", "terminal_time", "selected_time", "consumed_time"):
            if data.get(field) is not None:
                data[field] = _required_time(data[field], field)
        for field in (
            "candidate_interpretations_json",
            "event_history_json",
            "reason_codes_json",
            "diagnostics_json",
        ):
            data[field] = sanitize_json(data[field])
        with get_trades_db() as db:
            row = (
                db.query(StockOpportunityORM)
                .filter(StockOpportunityORM.opportunity_key == payload.opportunity_key)
                .one_or_none()
            )
            if row is None:
                row = StockOpportunityORM(**data)
                db.add(row)
            else:
                # Signal links are durable once established. A later
                # opportunity projection may not carry the current lifecycle
                # result, so NULL must not erase an existing link.
                if data.get("signal_id") is None:
                    data["signal_id"] = row.signal_id
                for key, value in data.items():
                    setattr(row, key, value)
            db.commit()
            db.refresh(row)
            return cls.model_validate(row, from_attributes=True)

    @classmethod
    def fetch_day(
        cls,
        *,
        trading_day: date,
        symbols: Optional[List[str]] = None,
    ) -> List["StockOpportunity"]:
        with get_trades_db() as db:
            query = db.query(StockOpportunityORM).filter(
                StockOpportunityORM.trading_day == trading_day
            )
            if symbols:
                query = query.filter(
                    StockOpportunityORM.symbol.in_([
                        str(symbol).strip().upper() for symbol in symbols
                    ])
                )
            # The opportunity rows contain several potentially large JSON
            # columns. Filesorting complete ORM rows can exhaust MySQL's sort
            # buffer. Order only the narrow primary-key projection and hydrate
            # the selected rows separately.
            ordered_ids = [
                row_id
                for (row_id,) in (
                    query.with_entities(StockOpportunityORM.id)
                    .order_by(
                        StockOpportunityORM.symbol.asc(),
                        StockOpportunityORM.first_observed_time.asc(),
                        StockOpportunityORM.id.asc(),
                    )
                    .all()
                )
            ]
            if not ordered_ids:
                rows = []
            else:
                fetched = (
                    db.query(StockOpportunityORM)
                    .filter(StockOpportunityORM.id.in_(ordered_ids))
                    .all()
                )
                by_id = {int(row.id): row for row in fetched}
                rows = [by_id[row_id] for row_id in ordered_ids if row_id in by_id]
        return [cls.model_validate(row, from_attributes=True) for row in rows]

    @classmethod
    def delete_day(
        cls,
        *,
        trading_day: date,
        symbols: Optional[List[str]] = None,
    ) -> int:
        with get_trades_db() as db:
            query = db.query(StockOpportunityORM).filter(
                StockOpportunityORM.trading_day == trading_day
            )
            if symbols:
                query = query.filter(StockOpportunityORM.symbol.in_([
                    str(symbol).strip().upper() for symbol in symbols
                ]))
            count = query.delete(synchronize_session=False)
            db.commit()
        return int(count or 0)


__all__ = ["StockOpportunity"]
