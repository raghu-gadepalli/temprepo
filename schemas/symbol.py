import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Literal, Optional, List

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import load_only  # keep as-is (even if not used for these 3 methods)

from database.database import get_trades_db
from enums.enums import SymbolType
from models.trade_models import Symbol as SymbolORM
from utils.datetime_utils import current_fo_expiry
from utils.json_utils import sanitize_json

logger = logging.getLogger(__name__)


class SymbolSchema(BaseModel, frozen=True):
    model_config = {"from_attributes": True}

    id:               Optional[int]
    symbol:           str
    token:            Optional[str]
    name:             Optional[str]
    type:             Literal["EQ", "FUT", "CE", "PE"] = Field(..., description="EQ, FUT, CE or PE")
    price:            Optional[Decimal]
    exchange:         Optional[str]
    segment:          Optional[str]
    signal_profile:   str
    lotsize:          int = Field(..., ge=1)
    expiry:           Optional[date]
    strike_price:     Optional[Decimal]
    tick_size:        Optional[Decimal]
    equity_ref:       Optional[str]
    last_time:        Optional[datetime]
    last_snapshot:    Optional[Any]      # JSON column

    # Intraday dynamic flags
    generate_candles:   bool
    merge_candles:      bool
    update_performance: bool
    generate_signals:   bool
    processed:          bool

    # Long-lived flags (policy + listing)
    active:             bool
    enabled:            bool = True       # policy/universe gate (implicit everywhere)

    # Promotion/demotion timestamps (store consistently as IST-naive or UTC-naive)
    promoted_when:      Optional[datetime]
    demoted_when:       Optional[datetime]

    @field_validator("type")
    @classmethod
    def validate_type(cls, v):
        if v not in ("EQ", "FUT", "CE", "PE"):
            raise ValueError(f"Invalid type: {v}")
        return v

    # ------------------------------------------------------------------
    # FETCHES (implicitly gated by enabled = True)
    # ------------------------------------------------------------------
    @staticmethod
    def fetch_symbols(
        symbol_str: Optional[str] = None,
        active: Optional[int]     = 1,
        processed: Optional[int]  = None,
        type_filter: Optional[str]= None,
    ) -> Optional[List["SymbolSchema"]]:
        try:
            with get_trades_db() as db:
                q = db.query(SymbolORM).filter(SymbolORM.enabled == True)  # implicit policy gate
                if symbol_str:
                    q = q.filter(SymbolORM.symbol == symbol_str)
                if active is not None:
                    q = q.filter(SymbolORM.active == active)
                if processed is not None:
                    q = q.filter(SymbolORM.processed == processed)
                if type_filter is not None:
                    q = q.filter(SymbolORM.type == type_filter)
                rows = q.all()

            return [SymbolSchema.model_validate(r) for r in rows] if rows else None
        except Exception:
            logger.exception("Error fetching symbols")
            return None

    @staticmethod
    def fetch_symbol_strict(symbol_str: str) -> Optional["SymbolSchema"]:
        """Read one enabled symbol and propagate database/validation failures."""
        with get_trades_db() as db:
            rec = (
                db.query(SymbolORM)
                .filter(SymbolORM.symbol == symbol_str, SymbolORM.enabled == True)
                .one_or_none()
            )
        return SymbolSchema.model_validate(rec) if rec is not None else None

    @staticmethod
    def fetch_symbol(symbol_str: str) -> Optional["SymbolSchema"]:
        try:
            with get_trades_db() as db:
                rec = (
                    db.query(SymbolORM)
                      .filter(SymbolORM.symbol == symbol_str, SymbolORM.enabled == True)  # implicit policy gate
                      .one_or_none()
                )
            if not rec:
                logger.warning("Symbol[%s] not found or disabled", symbol_str)
                return None
            return SymbolSchema.model_validate(rec)
        except Exception:
            logger.exception("Error fetching symbol[%s]", symbol_str)
            return None

    # ------------------------------------------------------------------
    # WRITES
    # ------------------------------------------------------------------
    @staticmethod
    def create_symbol(data: dict) -> Optional["SymbolSchema"]:
        valid = {
            "symbol", "token", "name", "type", "price", "exchange",
            "segment", "signal_profile", "lotsize", "expiry",
            "strike_price", "tick_size", "equity_ref",
            "last_time", "last_snapshot",
            "generate_candles", "merge_candles",
            "update_performance", "generate_signals",
            "processed", "active",
            # policy + timestamps at create time
            "enabled", "promoted_when", "demoted_when",
        }
        payload = {k: v for k, v in data.items() if k in valid}

        if "last_snapshot" in payload and isinstance(payload["last_snapshot"], dict):
            payload["last_snapshot"] = sanitize_json(payload["last_snapshot"])

        try:
            with get_trades_db() as db:
                inst = SymbolORM(**payload)
                db.add(inst)
                db.commit()
                db.refresh(inst)
            logger.debug("Created Symbol[%s]", payload["symbol"])
            return SymbolSchema.model_validate(inst)

        except IntegrityError:
            logger.warning("Symbol[%s] exists, fetching existing", payload["symbol"])
            with get_trades_db() as db:
                existing = (
                    db.query(SymbolORM)
                      .filter(SymbolORM.symbol == payload["symbol"])
                      .one_or_none()
                )
            return SymbolSchema.model_validate(existing) if existing else None

        except Exception:
            logger.exception("Error creating Symbol[%s]", payload.get("symbol"))
            return None

    @staticmethod
    def update_symbol(symbol_str: str, updates: dict) -> Optional["SymbolSchema"]:
        if "last_snapshot" in updates and isinstance(updates["last_snapshot"], dict):
            updates["last_snapshot"] = sanitize_json(updates["last_snapshot"])

        try:
            with get_trades_db() as db:
                rec = (
                    db.query(SymbolORM)
                      .filter(SymbolORM.symbol == symbol_str)
                      .one_or_none()
                )
                if not rec:
                    logger.error("Symbol[%s] not found for update", symbol_str)
                    return None

                # Implicit policy gate:
                # Skip updates for disabled symbols UNLESS caller explicitly passes 'enabled' in updates
                if (not rec.enabled) and ("enabled" not in updates):
                    logger.info("Skip update for disabled Symbol[%s] (no 'enabled' in updates)", symbol_str)
                    return SymbolSchema.model_validate(rec)

                for k, v in updates.items():
                    if hasattr(rec, k) and k != "id":
                        setattr(rec, k, v)

                db.commit()
                db.refresh(rec)
            logger.debug("Updated Symbol[%s]", symbol_str)
            return SymbolSchema.model_validate(rec)

        except Exception:
            logger.exception("Error updating Symbol[%s]", symbol_str)
            return None

    # ------------------------------------------------------------------
    # DAILY STOCK-SELECTION HELPERS (policy gate respected)
    # ------------------------------------------------------------------
    @staticmethod
    def fetch_daily_scan_universe(type_filter: str = "EQ") -> List["SymbolSchema"]:
        """Fetch enabled symbols for the once-daily stockscan selector.

        This intentionally ignores active because stockscan is responsible for
        deciding today's active subset. enabled=True remains the monthly /
        expiry-cycle universe gate.
        """
        rows = SymbolSchema.fetch_symbols(active=None, type_filter=type_filter) or []
        return rows

    @staticmethod
    def reset_daily_selection_flags(
        whitelist_symbols: Optional[List[str]] = None,
        type_filter: str = "EQ",
    ) -> Dict[str, int]:
        """Prepare enabled symbols for the trading day.

        For enabled EQ symbols:
          - active=False until stockscan selects the daily basket
          - signal/runtime flags are set to their normal allowed state
          - processed and promotion markers are cleared

        Whitelist symbols are immediately set active=True so index/core symbols
        are available even before stockscan runs.
        """
        whitelist = {
            (x or "").strip().upper()
            for x in (whitelist_symbols or [])
            if (x or "").strip()
        }

        try:
            with get_trades_db() as db:
                base_q = db.query(SymbolORM).filter(SymbolORM.enabled == True)
                if type_filter:
                    base_q = base_q.filter(SymbolORM.type == type_filter)

                reset_count = base_q.update(
                    {
                        "active": False,
                        "generate_signals": True,
                        "generate_candles": True,
                        "merge_candles": True,
                        "update_performance": True,
                        "processed": False,
                        "promoted_when": None,
                        "demoted_when": None,
                    },
                    synchronize_session=False,
                )

                whitelist_count = 0
                if whitelist:
                    whitelist_q = db.query(SymbolORM).filter(
                        SymbolORM.enabled == True,
                        SymbolORM.symbol.in_(whitelist),
                    )
                    if type_filter:
                        whitelist_q = whitelist_q.filter(SymbolORM.type == type_filter)
                    whitelist_count = whitelist_q.update(
                        {"active": True, "processed": False},
                        synchronize_session=False,
                    )

                db.commit()

            out = {
                "reset_count": int(reset_count or 0),
                "whitelist_active_count": int(whitelist_count or 0),
            }
            logger.info("DAILY_SELECTION_RESET | %s", out)
            return out

        except Exception:
            logger.exception("Error resetting daily selection flags")
            raise

    @staticmethod
    def apply_daily_active_selection(
        selected_symbols: List[str],
        whitelist_symbols: Optional[List[str]] = None,
        type_filter: str = "EQ",
    ) -> Dict[str, int]:
        """Apply the once-daily active universe selected by stockscan.

        This method changes only active/processed. It deliberately does not
        change enabled, generate_signals, generate_candles, merge_candles, or
        update_performance. Those flags are controlled by reset/manual policy, not by the daily selector.
        """
        selected = {
            (x or "").strip().upper()
            for x in (selected_symbols or [])
            if (x or "").strip()
        }
        whitelist = {
            (x or "").strip().upper()
            for x in (whitelist_symbols or [])
            if (x or "").strip()
        }
        final_selected = selected | whitelist

        try:
            with get_trades_db() as db:
                base_q = db.query(SymbolORM).filter(SymbolORM.enabled == True)
                if type_filter:
                    base_q = base_q.filter(SymbolORM.type == type_filter)

                deactivated_count = base_q.update(
                    {"active": False},
                    synchronize_session=False,
                )

                activated_count = 0
                if final_selected:
                    activate_q = db.query(SymbolORM).filter(
                        SymbolORM.enabled == True,
                        SymbolORM.symbol.in_(final_selected),
                    )
                    if type_filter:
                        activate_q = activate_q.filter(SymbolORM.type == type_filter)
                    activated_count = activate_q.update(
                        {"active": True, "processed": False},
                        synchronize_session=False,
                    )

                db.commit()

            out = {
                "requested_count": len(final_selected),
                "activated_count": int(activated_count or 0),
                "deactivated_count": int(deactivated_count or 0),
            }
            logger.info("DAILY_ACTIVE_SELECTION | %s", out)
            return out

        except Exception:
            logger.exception("Error applying daily active selection")
            raise


    # ------------------------------------------------------------------
    # CURRENT INSTRUMENT HELPERS (implicitly gated by enabled = True)
    # ------------------------------------------------------------------

    @staticmethod
    def fetch_current_future(
        equity_ref: str,
        as_of: Optional[date] = None
    ) -> Optional["SymbolDerivSchema"]:
        as_of  = as_of or date.today()
        cutoff = current_fo_expiry()

        try:
            with get_trades_db() as db:
                orm = (
                    db.query(SymbolORM)
                    .filter(
                        SymbolORM.enabled == True,
                        SymbolORM.equity_ref == equity_ref.upper(),
                        SymbolORM.type == SymbolType.FUT.value,
                        SymbolORM.expiry == cutoff,
                    )
                    .order_by(SymbolORM.expiry.asc())
                    .first()
                )

                return SymbolDerivSchema.model_validate(orm) if orm else None

        except Exception:
            logger.exception("Error fetching current future for %s", equity_ref)
            return None

    @staticmethod
    def fetch_current_option(
        equity_ref: str,
        strike_price: float,
        as_of: Optional[date] = None,
        is_call: bool = True
    ) -> Optional["SymbolDerivSchema"]:
        as_of  = as_of or date.today()
        cutoff = current_fo_expiry()

        try:
            with get_trades_db() as db:
                orm = (
                    db.query(SymbolORM)
                    .filter(
                        SymbolORM.enabled == True,
                        SymbolORM.equity_ref == equity_ref.upper(),
                        SymbolORM.type == (SymbolType.CE.value if is_call else SymbolType.PE.value),
                        SymbolORM.strike_price == strike_price,
                        SymbolORM.expiry == cutoff,
                    )
                    .order_by(SymbolORM.expiry.asc())
                    .first()
                )

                return SymbolDerivSchema.model_validate(orm) if orm else None

        except Exception:
            logger.exception(
                "Error fetching current option for %s strike=%s is_call=%s",
                equity_ref, strike_price, is_call
            )
            return None

    @staticmethod
    def fetch_current_option_chain(
        equity_ref: str,
        as_of: Optional[date] = None
    ) -> List["SymbolDerivSchema"]:
        as_of  = as_of or date.today()
        cutoff = current_fo_expiry()

        try:
            with get_trades_db() as db:
                orms = (
                    db.query(SymbolORM)
                    .filter(
                        SymbolORM.enabled == True,
                        SymbolORM.equity_ref == equity_ref.upper(),
                        SymbolORM.type.in_([SymbolType.CE.value, SymbolType.PE.value]),
                        SymbolORM.expiry == cutoff,
                    )
                    .order_by(
                        SymbolORM.expiry.asc(),
                        SymbolORM.strike_price.asc()
                    )
                    .all()
                )

                return [SymbolDerivSchema.model_validate(o) for o in orms] if orms else []

        except Exception:
            logger.exception("Error fetching current option chain for %s", equity_ref)
            return []


class SymbolDerivSchema(BaseModel, frozen=True):
    """
    Lightweight schema used ONLY by derivatives generator.
    Does not include dynamic flags or policy fields.
    """
    model_config = {"from_attributes": True}

    symbol: str
    exchange: Optional[str]
    type: Literal["EQ", "FUT", "CE", "PE"]
    expiry: Optional[date] = None
    strike_price: Optional[Decimal] = None
    equity_ref: Optional[str] = None
