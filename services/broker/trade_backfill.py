from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import or_, and_

from configs.broker_config import BROKER_CONFIG
from database.database import get_trades_db
from enums.enums import EntryStatus, ExitStatus, OrderStatus, TradeType
from models.trade_models import (
    UserOrders as OMSOrder,
    UserPositions as OMSPosition,
    UserTrade as UserTradeORM,
)
from schemas.orderprofile import OrderProfileSchema
from schemas.user import UserSchema
from schemas.user_trade import UserTradeSchema
from services.broker.reconcile_helper import get_reconcile_users
from services.trade.monitor.trademon_helper import TradeMonHelper
from utils.datetime_utils import IST

logger = logging.getLogger(__name__)

_extras = BROKER_CONFIG.trade_backfill.extras

DEFAULT_LIMIT_USERS = _extras.limit_users
DEFAULT_LIMIT_TRADES_PER_USER = _extras.limit_trades_per_user
DRY_RUN = _extras.dry_run


# ---------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------
def _now_ist_naive() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _today_ist() -> date:
    return datetime.now(IST).date()


def _to_ist_naive(ts: Optional[datetime]) -> Optional[datetime]:
    if not isinstance(ts, datetime):
        return None
    try:
        if ts.tzinfo is None:
            return ts.replace(tzinfo=None)
        return ts.astimezone(IST).replace(tzinfo=None)
    except Exception:
        return ts.replace(tzinfo=None)


def d(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if x is None:
        return Decimal("0")
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")


def _try_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _enum_str(x: Any) -> str:
    return str(getattr(x, "value", x) or "").upper().strip()


def _entry_status(ut: UserTradeSchema) -> str:
    return _enum_str(getattr(ut, "entry_status", EntryStatus.CREATED.value))


def _exit_status(ut: UserTradeSchema) -> str:
    return _enum_str(getattr(ut, "exit_status", ExitStatus.NONE.value))


def _side(ut: UserTradeSchema) -> str:
    return _enum_str(getattr(ut, "trade_type", TradeType.BUY.value))


def _opposite_side(side: str) -> str:
    return TradeType.SELL.value if _enum_str(side) == TradeType.BUY.value else TradeType.BUY.value


def _app_entry_qty(ut: UserTradeSchema) -> int:
    return _try_int(
        getattr(ut, "executed_entry_qty", None)
        or getattr(ut, "quantity", 0)
        or 0,
        0,
    )


def _app_exit_qty(ut: UserTradeSchema) -> int:
    return _try_int(getattr(ut, "executed_exit_qty", None) or 0, 0)


def _app_open_qty_abs(ut: UserTradeSchema) -> int:
    return max(_app_entry_qty(ut) - _app_exit_qty(ut), 0)


def _calc_exit_pnl(ut: UserTradeSchema, exit_px: Any, qty: Any) -> Optional[Decimal]:
    entry_px = d(getattr(ut, "executed_entry_price", None) or getattr(ut, "entry_price", None) or 0)
    exit_px_d = d(exit_px or 0)
    qty_i = _try_int(qty, 0)

    if entry_px <= 0 or exit_px_d <= 0 or qty_i <= 0:
        return None

    if _side(ut) == TradeType.BUY.value:
        return (exit_px_d - entry_px) * Decimal(qty_i)
    return (entry_px - exit_px_d) * Decimal(qty_i)


def _reconcile_obs(code: Optional[str], message: Optional[str]) -> Dict[str, Any]:
    return {
        "reconcile_last_checked_at": _now_ist_naive(),
        "reconcile_status": code,
        "reconcile_status_message": message,
    }


def _order_status_str(order: OMSOrder) -> str:
    return str(getattr(order, "status", "") or "").upper().strip()


def _order_side_str(order: OMSOrder) -> str:
    return str(getattr(order, "transaction_type", "") or "").upper().strip()


def _order_symbol_str(order: OMSOrder) -> str:
    return str(getattr(order, "tradingsymbol", "") or "").upper().strip()


def _order_avg_price(order: OMSOrder) -> Optional[Decimal]:
    avg = d(getattr(order, "average_price", None) or 0)
    return avg if avg > 0 else None


def _order_filled_qty(order: OMSOrder) -> int:
    return _try_int(getattr(order, "filled_quantity", None), 0)


def _order_time(order: OMSOrder) -> Optional[datetime]:
    return (
        _to_ist_naive(getattr(order, "exchange_timestamp", None))
        or _to_ist_naive(getattr(order, "order_timestamp", None))
        or _to_ist_naive(getattr(order, "polled_at", None))
    )


def _position_key(exchange: Any, tradingsymbol: Any, product: Any) -> Tuple[str, str, str]:
    return (
        str(exchange or "").upper().strip(),
        str(tradingsymbol or "").upper().strip(),
        str(product or "").upper().strip(),
    )


def _index_positions(rows: List[OMSPosition]) -> Dict[Tuple[str, str, str], OMSPosition]:
    out: Dict[Tuple[str, str, str], OMSPosition] = {}
    for r in rows or []:
        key = _position_key(
            getattr(r, "exchange", None),
            getattr(r, "tradingsymbol", None),
            getattr(r, "product", None),
        )
        out[key] = r
    return out


def _find_position_record(
    pos_index: Dict[Tuple[str, str, str], OMSPosition],
    *,
    exchange: Any,
    symbol: Any,
    product: Any,
) -> Optional[OMSPosition]:
    key = _position_key(exchange, symbol, product)
    rec = pos_index.get(key)
    if rec is not None:
        return rec

    tsym = str(symbol or "").upper().strip()
    ex = str(exchange or "").upper().strip()
    prod = str(product or "").upper().strip()

    if not tsym:
        return None

    candidates = [
        v for v in pos_index.values()
        if str(getattr(v, "tradingsymbol", "") or "").upper().strip() == tsym
    ]
    if not candidates:
        return None

    if ex:
        ex_matches = [v for v in candidates if str(getattr(v, "exchange", "") or "").upper().strip() == ex]
        if len(ex_matches) == 1:
            return ex_matches[0]
        if ex_matches:
            candidates = ex_matches

    if prod:
        prod_matches = [v for v in candidates if str(getattr(v, "product", "") or "").upper().strip() == prod]
        if len(prod_matches) == 1:
            return prod_matches[0]
        if prod_matches:
            candidates = prod_matches

    return candidates[0] if len(candidates) == 1 else None


def _position_qty_last_price(pos: Optional[OMSPosition]) -> Tuple[int, Optional[Decimal]]:
    if pos is None:
        return 0, None
    qty = _try_int(getattr(pos, "quantity", None), 0)
    lp = d(getattr(pos, "last_price", None) or 0)
    return qty, (lp if lp > 0 else None)


def _position_polled_at(pos: Optional[OMSPosition]) -> Optional[datetime]:
    if pos is None:
        return None
    return _to_ist_naive(getattr(pos, "polled_at", None))


def _position_open_observed_at(ut: UserTradeSchema) -> Optional[datetime]:
    status = str(getattr(ut, "reconcile_status", "") or "").upper().strip()
    if status not in {
        "BROKER_POSITION_OPEN_CONFIRMED",
        "BROKER_PARTIAL_EXIT_ALIGNED",
    }:
        return None
    return _to_ist_naive(getattr(ut, "reconcile_last_checked_at", None))


def _same_reconcile_observation(ut: UserTradeSchema, code: str, message: str) -> bool:
    return (
        str(getattr(ut, "reconcile_status", "") or "") == str(code or "")
        and str(getattr(ut, "reconcile_status_message", "") or "") == str(message or "")
    )


def _build_exit_fill_updates(
    ut: UserTradeSchema,
    *,
    exit_px: Any,
    qty: Any,
    when: Optional[datetime],
    status_code: Optional[str],
    status_message: Optional[str],
) -> Dict[str, Any]:
    when0 = _to_ist_naive(when) or _now_ist_naive()
    exit_px_d = d(exit_px or 0)
    qty_i = _try_int(
        qty
        or getattr(ut, "executed_exit_qty", None)
        or getattr(ut, "executed_entry_qty", None)
        or getattr(ut, "quantity", 0)
        or 1,
        1,
    )

    upd: Dict[str, Any] = {
        "exit_status": ExitStatus.FILLED.value,
        "executed_exit_price": float(exit_px_d) if exit_px_d > 0 else None,
        "executed_exit_qty": qty_i,
        "exit_exec_time": when0,
        "exit_time": _to_ist_naive(getattr(ut, "exit_time", None)) or when0,
        "exit_reconciled_at": _now_ist_naive(),
        "last_time": when0,
        "last_price": float(exit_px_d) if exit_px_d > 0 else getattr(ut, "last_price", None),
    }
    upd.update(_reconcile_obs(status_code, status_message))

    exit_pnl = _calc_exit_pnl(ut, exit_px_d, qty_i)
    if exit_pnl is not None:
        upd["exit_pnl"] = float(exit_pnl)
        upd["last_pnl"] = float(exit_pnl)
        upd["last_pnl_value"] = float(exit_pnl)

    return upd


def _apply_update(trade_id: int, updates: Dict[str, Any], *, dry_run: bool) -> bool:
    if not updates:
        return False

    if dry_run:
        logger.info("TradeBackfill DRY_RUN trade_id=%s updates=%s", trade_id, updates)
        return True

    out = UserTradeSchema.update_user_trade_by_id(trade_id, updates)
    return out is not None


# ---------------------------------------------------------------------
# OMS fetch helpers
# ---------------------------------------------------------------------
def _fetch_candidate_trades(userid: str, limit: int) -> List[UserTradeSchema]:
    """
    Candidate rules after executor cleanup:
    - unresolved SUBMITTED entry/exit
    - materially incomplete FILLED entry (missing actual fill fields)
    - FILLED/open trade still needing OMS alignment
    - FILLED/closed trade needing pnl or exit reconcile repair
    """
    with get_trades_db() as db:
        q = (
            db.query(UserTradeORM)
            .filter(UserTradeORM.userid == userid)
            .filter(UserTradeORM.execution_mode == "REAL")
            .filter(
                or_(
                    # unresolved entry submit
                    UserTradeORM.entry_status == EntryStatus.SUBMITTED.value,

                    # materially incomplete filled entry
                    and_(
                        UserTradeORM.entry_status == EntryStatus.FILLED.value,
                        or_(
                            UserTradeORM.executed_entry_price.is_(None),
                            UserTradeORM.executed_entry_price == 0,
                            UserTradeORM.executed_entry_qty.is_(None),
                            UserTradeORM.executed_entry_qty == 0,
                            UserTradeORM.entry_exec_time.is_(None),
                        ),
                    ),

                    # unresolved exit submit
                    UserTradeORM.exit_status == ExitStatus.SUBMITTED.value,

                    # open filled trade still needing OMS alignment
                    and_(
                        UserTradeORM.entry_status == EntryStatus.FILLED.value,
                        ~UserTradeORM.exit_status.in_([
                            ExitStatus.FILLED.value,
                            ExitStatus.CANCELLED.value,
                        ]),
                    ),

                    # closed trade with missing pnl / missing exit reconciliation completion
                    and_(
                        UserTradeORM.entry_status == EntryStatus.FILLED.value,
                        UserTradeORM.exit_status == ExitStatus.FILLED.value,
                        or_(
                            UserTradeORM.executed_exit_price.is_(None),
                            UserTradeORM.executed_exit_price == 0,
                            UserTradeORM.executed_exit_qty.is_(None),
                            UserTradeORM.executed_exit_qty == 0,
                            UserTradeORM.exit_exec_time.is_(None),
                            UserTradeORM.exit_pnl.is_(None),
                            UserTradeORM.exit_pnl == 0,
                            UserTradeORM.last_pnl.is_(None),
                            UserTradeORM.last_pnl == 0,
                            UserTradeORM.last_pnl_value.is_(None),
                            UserTradeORM.last_pnl_value == 0,
                            UserTradeORM.exit_reconciled_at.is_(None),
                        ),
                    ),
                )
            )
            .order_by(UserTradeORM.id.asc())
            .limit(int(limit))
        )
        rows = q.all()

    return [UserTradeSchema.model_validate(r) for r in rows]


def _fetch_oms_orders_for_user(userid: str, trading_day: date) -> List[OMSOrder]:
    with get_trades_db() as db:
        rows = (
            db.query(OMSOrder)
            .filter(OMSOrder.client_id == userid)
            .filter(OMSOrder.trading_day == trading_day)
            .order_by(OMSOrder.polled_at.desc(), OMSOrder.order_timestamp.desc(), OMSOrder.id.desc())
            .all()
        )
    return rows or []


def _fetch_oms_positions_for_user(userid: str, trading_day: date) -> List[OMSPosition]:
    with get_trades_db() as db:
        rows = (
            db.query(OMSPosition)
            .filter(OMSPosition.client_id == userid)
            .filter(OMSPosition.trading_day == trading_day)
            .order_by(OMSPosition.polled_at.desc(), OMSPosition.id.desc())
            .all()
        )
    return rows or []


# ---------------------------------------------------------------------
# order matching
# ---------------------------------------------------------------------
def _resolve_entry_order_from_oms(ut: UserTradeSchema, orders: List[OMSOrder]) -> Optional[OMSOrder]:
    target_oid = str(getattr(ut, "entry_order_id", "") or "").strip()
    if not target_oid:
        return None

    for order in orders:
        if str(getattr(order, "order_id", "") or "").strip() == target_oid:
            return order
    return None


def _resolve_exit_order_from_oms(ut: UserTradeSchema, orders: List[OMSOrder]) -> Tuple[Optional[OMSOrder], Optional[str]]:
    target_oid = str(getattr(ut, "exit_order_id", "") or "").strip()
    if target_oid:
        for order in orders:
            if str(getattr(order, "order_id", "") or "").strip() == target_oid:
                return order, target_oid

    target_symbol = str(getattr(ut, "symbol", "") or "").upper().strip()
    target_side = _opposite_side(_side(ut))
    target_qty = max(
        _app_open_qty_abs(ut),
        _app_entry_qty(ut),
        _try_int(getattr(ut, "quantity", None), 0),
        0,
    )
    lower_bound = (
        _to_ist_naive(getattr(ut, "exit_intent_time", None))
        or _to_ist_naive(getattr(ut, "entry_exec_time", None))
    )

    candidates: List[Tuple[Tuple[Any, ...], OMSOrder]] = []

    for order in orders:
        if _order_status_str(order) != OrderStatus.COMPLETE.value:
            continue
        if _order_symbol_str(order) != target_symbol:
            continue
        if _order_side_str(order) != target_side:
            continue

        avg = _order_avg_price(order)
        filled_qty = _order_filled_qty(order)
        when = _order_time(order)

        if avg is None or avg <= 0 or filled_qty <= 0:
            continue
        if lower_bound and when and when < lower_bound:
            continue

        qty_gap = abs(filled_qty - target_qty) if target_qty > 0 else 999999
        if target_qty > 0 and filled_qty < min(target_qty, max(1, target_qty // 2)):
            continue

        after_exit_bias = 0 if (_to_ist_naive(getattr(ut, "exit_intent_time", None)) and when and when >= _to_ist_naive(getattr(ut, "exit_intent_time", None))) else 1
        after_entry_bias = 0 if (_to_ist_naive(getattr(ut, "entry_exec_time", None)) and when and when >= _to_ist_naive(getattr(ut, "entry_exec_time", None))) else 1
        time_sort = when or datetime.max.replace(tzinfo=None)

        candidates.append(((qty_gap, after_exit_bias, after_entry_bias, time_sort), order))

    if not candidates:
        return None, None

    candidates.sort(key=lambda x: x[0])
    winner = candidates[0][1]
    matched_oid = str(getattr(winner, "order_id", "") or "").strip() or None
    return winner, matched_oid


# ---------------------------------------------------------------------
# service
# ---------------------------------------------------------------------
class TradeBackfillService:
    """
    OMS-backed reconciliation for app-managed REAL trades.

    Scope:
      - finalize delayed entry fills from oms_orders
      - finalize delayed exit fills from oms_orders / oms_positions
      - align manual partial reductions from oms_positions
      - repair closed-trade pnl / missing exit fills from OMS truth
    """

    def __init__(self, *, dry_run: bool = DRY_RUN):
        self.dry_run = bool(dry_run)

    def run_once(
        self,
        *,
        limit_users: int = DEFAULT_LIMIT_USERS,
        limit_trades_per_user: int = DEFAULT_LIMIT_TRADES_PER_USER,
    ) -> Dict[str, int]:
        users = self._fetch_due_users(limit_users=limit_users)
        if not users:
            logger.info("TradeBackfill: no eligible users")
            return self._empty_stats()

        stats = self._empty_stats()
        stats["users_found"] = len(users)

        for user in users:
            userid = str(getattr(user, "userid", "") or "").strip()
            if not userid:
                continue

            try:
                result = self.backfill_user_once(
                    user=user,
                    limit_trades=limit_trades_per_user,
                )

                stats["users_processed"] += 1
                stats["trades_seen"] += int(result.get("trades_seen", 0) or 0)
                stats["trades_updated"] += int(result.get("trades_updated", 0) or 0)

                if result.get("user_ok"):
                    stats["users_succeeded"] += 1
                else:
                    stats["users_failed"] += 1

            except Exception:
                stats["errors"] += 1
                stats["users_failed"] += 1
                logger.exception("TradeBackfill: fatal user error userid=%s", userid)

        logger.info(
            "TradeBackfill: run_once users_found=%s users_processed=%s users_succeeded=%s "
            "users_failed=%s trades_seen=%s trades_updated=%s errors=%s dry_run=%s",
            stats["users_found"],
            stats["users_processed"],
            stats["users_succeeded"],
            stats["users_failed"],
            stats["trades_seen"],
            stats["trades_updated"],
            stats["errors"],
            self.dry_run,
        )
        return stats

    def backfill_user_once(
        self,
        *,
        user: UserSchema,
        limit_trades: int = DEFAULT_LIMIT_TRADES_PER_USER,
    ) -> Dict[str, Any]:
        userid = str(getattr(user, "userid", "") or "").strip()
        if not userid:
            return {
                "userid": "",
                "trades_seen": 0,
                "trades_updated": 0,
                "user_ok": False,
            }

        trades = _fetch_candidate_trades(userid, limit_trades)
        if not trades:
            logger.info("TradeBackfill: no candidate trades userid=%s", userid)
            return {
                "userid": userid,
                "trades_seen": 0,
                "trades_updated": 0,
                "user_ok": True,
            }

        trading_day = _today_ist()
        oms_orders = _fetch_oms_orders_for_user(userid, trading_day)
        oms_positions = _fetch_oms_positions_for_user(userid, trading_day)
        pos_index = _index_positions(oms_positions)

        trades_updated = 0

        for ut in trades:
            try:
                did = False

                did = self._backfill_entry_from_oms(ut, oms_orders) or did
                did = self._backfill_open_or_exit_from_oms(ut, oms_orders, pos_index) or did
                did = self._repair_closed_trade_from_oms(ut, oms_orders) or did

                if did:
                    trades_updated += 1

            except Exception:
                logger.exception(
                    "TradeBackfill: trade backfill failed userid=%s trade_id=%s",
                    userid,
                    getattr(ut, "id", None),
                )

        logger.info(
            "TradeBackfill: user done userid=%s trades_seen=%s trades_updated=%s dry_run=%s",
            userid,
            len(trades),
            trades_updated,
            self.dry_run,
        )
        return {
            "userid": userid,
            "trades_seen": len(trades),
            "trades_updated": trades_updated,
            "user_ok": True,
        }

    def _backfill_entry_from_oms(self, ut: UserTradeSchema, oms_orders: List[OMSOrder]) -> bool:
        es = _entry_status(ut)
        if es not in (EntryStatus.SUBMITTED.value, EntryStatus.FILLED.value):
            return False

        # If executor already fully filled and reconciled entry, skip.
        if (
            es == EntryStatus.FILLED.value
            and getattr(ut, "entry_reconciled_at", None)
            and d(getattr(ut, "executed_entry_price", None) or 0) > 0
            and _try_int(getattr(ut, "executed_entry_qty", None), 0) > 0
            and getattr(ut, "entry_exec_time", None) is not None
        ):
            return False

        order = _resolve_entry_order_from_oms(ut, oms_orders)
        if order is None:
            return False

        st = _order_status_str(order)
        avg = _order_avg_price(order)
        qty = _order_filled_qty(order)
        when = _order_time(order) or _now_ist_naive()

        if st == OrderStatus.COMPLETE.value and avg is not None and avg > 0:
            trade_management = TradeMonHelper.rebase_trade_management_after_fill(
                raw=getattr(ut, "trade_management", None),
                side=getattr(ut, "trade_type", None),
                instrument_type=getattr(ut, "instrument_type", None),
                planned_entry_price=getattr(ut, "entry_price", None),
                executed_entry_price=avg,
                asof_time=when,
            )
            fill_qty = qty or (_try_int(getattr(ut, "quantity", None), 0) or 1)
            upd = {
                "entry_status": EntryStatus.FILLED.value,
                "executed_entry_price": float(avg),
                "executed_entry_qty": fill_qty,
                "entry_exec_time": when,
                "entry_reconciled_at": _now_ist_naive(),
                "last_time": when,
                "last_price": float(avg),
                "last_pnl": 0,
                "last_pnl_value": 0,
                "max_price": float(avg),
                "min_price": float(avg),
                "max_time": when,
                "min_time": when,
                "trade_management": trade_management,
            }
            upd.update(_reconcile_obs(
                "ENTRY_RECONCILED_FROM_OMS",
                f"Entry reconciled from oms_orders order_id={getattr(order, 'order_id', None)} status={st}.",
            ))
            return _apply_update(ut.id, upd, dry_run=self.dry_run)

        if es == EntryStatus.SUBMITTED.value and st in (
            OrderStatus.CANCELLED.value,
            OrderStatus.REJECTED.value,
            OrderStatus.INVALID.value,
        ):
            left = max(_try_int(getattr(ut, "entry_retries", None), 1) - 1, 0)
            upd = {
                "entry_retries": left,
                "entry_order_id": None if left > 0 else getattr(ut, "entry_order_id", None),
            }
            if left > 0:
                upd["entry_status"] = EntryStatus.READY.value
            else:
                upd["entry_status"] = EntryStatus.INVALID.value

            upd.update(_reconcile_obs(
                f"ENTRY_{st}_FROM_OMS",
                f"Entry terminal status resolved from oms_orders order_id={getattr(order, 'order_id', None)}.",
            ))
            return _apply_update(ut.id, upd, dry_run=self.dry_run)

        return False

    def _backfill_open_or_exit_from_oms(
        self,
        ut: UserTradeSchema,
        oms_orders: List[OMSOrder],
        pos_index: Dict[Tuple[str, str, str], OMSPosition],
    ) -> bool:
        es = _entry_status(ut)
        xs = _exit_status(ut)

        if es != EntryStatus.FILLED.value:
            return False
        if xs in (ExitStatus.FILLED.value, ExitStatus.CANCELLED.value):
            return False

        mode = "intraday" if bool(getattr(ut, "intraday_only", False)) else "carryforward"
        op = OrderProfileSchema.fetch_order_profile(mode, getattr(ut, "instrument_type", "EQ"))

        pos = _find_position_record(
            pos_index,
            exchange=getattr(op, "exchange", None),
            symbol=getattr(ut, "symbol", None),
            product=getattr(op, "product_type", None),
        )
        pos_qty, _pos_lp = _position_qty_last_price(pos)
        pos_polled_at = _position_polled_at(pos)
        open_observed_at = _position_open_observed_at(ut)

        app_side = _side(ut)
        expected_sign = 1 if app_side == TradeType.BUY.value else -1

        # Missing from the locally mirrored OMS positions table is not proof
        # that a live broker position is flat. A newly filled order can appear
        # in oms_orders before the next positions poll is persisted.
        if pos is None:
            if open_observed_at is not None:
                return False
            code = "BROKER_POSITION_NOT_VISIBLE_PENDING"
            message = (
                "OMS position row is not visible yet; keeping app trade open "
                "until broker position truth is observed."
            )
            if _same_reconcile_observation(ut, code, message):
                return False
            return _apply_update(
                ut.id,
                _reconcile_obs(code, message),
                dry_run=self.dry_run,
            )

        if pos_qty != 0 and (pos_qty * expected_sign) < 0:
            return _apply_update(
                ut.id,
                {
                    "exit_status": ExitStatus.FAILED.value,
                    **_reconcile_obs(
                        "BROKER_POSITION_SIGN_MISMATCH",
                        f"OMS position qty={pos_qty} conflicts with app side={app_side}.",
                    ),
                },
                dry_run=self.dry_run,
            )

        if pos_qty == 0:
            order, matched_oid = _resolve_exit_order_from_oms(ut, oms_orders)

            if order is not None:
                avg = _order_avg_price(order)
                qty = _order_filled_qty(order)
                when = _order_time(order) or _now_ist_naive()

                upd = _build_exit_fill_updates(
                    ut,
                    exit_px=(avg if avg is not None and avg > 0 else None),
                    qty=(
                        qty
                        or getattr(ut, "executed_exit_qty", None)
                        or _app_open_qty_abs(ut)
                        or getattr(ut, "executed_entry_qty", None)
                        or getattr(ut, "quantity", 0)
                        or 1
                    ),
                    when=when,
                    status_code=(
                        "BROKER_MANUAL_EXIT_RECONCILED"
                        if avg is not None and avg > 0
                        else "BROKER_MANUAL_EXIT_RECONCILED_NO_PRICE"
                    ),
                    status_message=(
                        f"OMS position flat; exit reconciled using oms_orders order_id={matched_oid or 'unknown'}."
                        if avg is not None and avg > 0
                        else "OMS position flat; reconciled exit without average_price from oms_orders."
                    ),
                )
                if matched_oid:
                    upd["exit_order_id"] = matched_oid
                return _apply_update(ut.id, upd, dry_run=self.dry_run)

            # Accept a zero quantity as an external/manual close only after
            # this app trade was observed open and a newer broker position poll
            # shows it flat. This rejects stale pre-entry zero snapshots.
            if (
                open_observed_at is None
                or pos_polled_at is None
                or pos_polled_at <= open_observed_at
            ):
                if open_observed_at is not None:
                    return False
                code = "BROKER_POSITION_FLAT_UNCONFIRMED"
                message = (
                    "OMS position quantity is zero but no open-to-flat "
                    "transition has been confirmed; keeping app trade open."
                )
                if _same_reconcile_observation(ut, code, message):
                    return False
                return _apply_update(
                    ut.id,
                    _reconcile_obs(code, message),
                    dry_run=self.dry_run,
                )

            upd = _build_exit_fill_updates(
                ut,
                exit_px=None,
                qty=(
                    getattr(ut, "executed_exit_qty", None)
                    or _app_open_qty_abs(ut)
                    or getattr(ut, "executed_entry_qty", None)
                    or getattr(ut, "quantity", 0)
                    or 1
                ),
                when=_now_ist_naive(),
                status_code="BROKER_MANUAL_EXIT_RECONCILED_NO_PRICE",
                status_message=(
                    "OMS position changed from previously observed open to flat; "
                    "reconciled exit without matching oms_orders fill record."
                ),
            )
            return _apply_update(ut.id, upd, dry_run=self.dry_run)

        app_entry_qty = _app_entry_qty(ut)
        app_open_qty = _app_open_qty_abs(ut)
        bqty = abs(int(pos_qty))

        if app_entry_qty > 0 and bqty > 0 and bqty < app_open_qty:
            implied_exit_qty = max(app_entry_qty - bqty, 0)
            return _apply_update(
                ut.id,
                {
                    "executed_exit_qty": implied_exit_qty,
                    **_reconcile_obs(
                        "BROKER_PARTIAL_EXIT_ALIGNED",
                        f"OMS open qty={bqty} < app open qty={app_open_qty}; aligned executed_exit_qty={implied_exit_qty}."
                    ),
                },
                dry_run=self.dry_run,
            )

        poll_text = pos_polled_at.isoformat() if pos_polled_at else "unknown"
        code = "BROKER_POSITION_OPEN_CONFIRMED"
        message = f"OMS position open qty={pos_qty} poll_at={poll_text}."
        if _same_reconcile_observation(ut, code, message):
            return False
        return _apply_update(
            ut.id,
            _reconcile_obs(code, message),
            dry_run=self.dry_run,
        )

    def _repair_closed_trade_from_oms(self, ut: UserTradeSchema, oms_orders: List[OMSOrder]) -> bool:
        if _entry_status(ut) != EntryStatus.FILLED.value:
            return False
        if _exit_status(ut) != ExitStatus.FILLED.value:
            return False

        exit_pnl_now = getattr(ut, "exit_pnl", None)
        last_pnl_now = getattr(ut, "last_pnl", None)
        last_pnl_value_now = getattr(ut, "last_pnl_value", None)

        def _is_missing_or_zero(v: Any) -> bool:
            try:
                return v is None or Decimal(str(v)) == 0
            except Exception:
                return True

        if not (
            _is_missing_or_zero(getattr(ut, "executed_exit_price", None))
            or _is_missing_or_zero(getattr(ut, "executed_exit_qty", None))
            or getattr(ut, "exit_exec_time", None) is None
            or _is_missing_or_zero(exit_pnl_now)
            or _is_missing_or_zero(last_pnl_now)
            or _is_missing_or_zero(last_pnl_value_now)
            or getattr(ut, "exit_reconciled_at", None) is None
        ):
            return False

        order, matched_oid = _resolve_exit_order_from_oms(ut, oms_orders)

        exit_px = d(getattr(ut, "executed_exit_price", None) or 0)
        qty = _try_int(
            getattr(ut, "executed_exit_qty", None)
            or getattr(ut, "executed_entry_qty", None)
            or getattr(ut, "quantity", 0)
            or 0,
            0,
        )
        when = _to_ist_naive(getattr(ut, "exit_exec_time", None)) or _now_ist_naive()

        if order is not None:
            avg = _order_avg_price(order)
            if avg is not None and avg > 0:
                exit_px = avg
            oqty = _order_filled_qty(order)
            if oqty > 0:
                qty = oqty
            owhen = _order_time(order)
            if owhen is not None:
                when = owhen

        upd = _build_exit_fill_updates(
            ut,
            exit_px=(exit_px if exit_px > 0 else None),
            qty=qty,
            when=when,
            status_code=(
                "BROKER_MANUAL_EXIT_RECONCILED"
                if exit_px > 0 else "BROKER_MANUAL_EXIT_RECONCILED_NO_PRICE"
            ),
            status_message=(
                f"Closed trade repaired from OMS order_id={matched_oid or 'unknown'}."
                if exit_px > 0 else
                "Closed trade reconciled but OMS average_price was not available."
            ),
        )
        if matched_oid:
            upd["exit_order_id"] = matched_oid

        return _apply_update(ut.id, upd, dry_run=self.dry_run)

    def _fetch_due_users(self, *, limit_users: int = DEFAULT_LIMIT_USERS) -> List[UserSchema]:
        users = get_reconcile_users() or []
        if limit_users > 0:
            return users[:limit_users]
        return users

    @staticmethod
    def _empty_stats() -> Dict[str, int]:
        return {
            "users_found": 0,
            "users_processed": 0,
            "users_succeeded": 0,
            "users_failed": 0,
            "trades_seen": 0,
            "trades_updated": 0,
            "errors": 0,
        }