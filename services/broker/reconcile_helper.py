from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from kiteconnect import KiteConnect

from configs.broker_config import BROKER_CONFIG
from schemas.user import UserSchema
from schemas.user_funds import UserFundsSchema
from schemas.user_funds_history import UserFundsHistorySchema
from schemas.user_positions import UserPositionsSchema
from schemas.user_positions_history import UserPositionsHistorySchema
from schemas.user_orders import UserOrdersSchema
from schemas.user_orders_history import UserOrdersHistorySchema
from utils.datetime_utils import now_ist
from utils.trading_day import get_trading_day

logger = logging.getLogger(__name__)

_recon_extras = BROKER_CONFIG.reconcile.extras

DEFAULT_WRITE_FUNDS_HISTORY = _recon_extras.write_funds_history
DEFAULT_WRITE_POSITIONS_HISTORY = _recon_extras.write_positions_history
DEFAULT_WRITE_ORDERS_HISTORY = _recon_extras.write_orders_history


def _safe_str(x: Any, default: str = "") -> str:
    try:
        return str(x) if x is not None else default
    except Exception:
        return default


def _safe_num(dct: Any, *keys, default=None):
    cur = dct
    try:
        for k in keys:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(k)
        return cur if cur is not None else default
    except Exception:
        return default


def get_reconcile_users() -> List[UserSchema]:
    return UserSchema.fetch_real_users(logged_in=1)


def get_kite_client(user: UserSchema) -> Tuple[Optional[KiteConnect], Optional[str]]:
    if not user:
        return None, "user_not_found"

    userid = _safe_str(getattr(user, "userid", ""), "").strip()
    if not userid:
        return None, "invalid_userid"

    apikey = _safe_str(getattr(user, "apikey", None), "").strip()
    access_token = _safe_str(getattr(user, "access_token", None), "").strip()

    if not apikey or not access_token:
        return None, "not_logged_into_zerodha"

    try:
        kite = KiteConnect(api_key=apikey)
        kite.set_access_token(access_token)
        return kite, None
    except Exception:
        logger.exception("Failed creating Kite client for userid=%s", userid)
        return None, "kite_client_create_failed"


def invalidate_user_session(user: UserSchema) -> bool:
    userid = _safe_str(getattr(user, "userid", ""), "").strip()
    if not userid:
        return False

    try:
        updated = UserSchema.update_user(userid, {
            "logged_in": 0,
            "logged_time": None,
            "access_token": None,
        })
        return bool(updated) and not isinstance(updated, dict)
    except Exception:
        logger.exception("Failed invalidating Zerodha session for userid=%s", userid)
        return False


# ---------------------------------------------------------------------
# Funds
# ---------------------------------------------------------------------

def map_kite_equity_funds(
    user: UserSchema,
    funds: Dict[str, Any],
    polled_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    userid = _safe_str(getattr(user, "userid", ""), "").strip()
    now = polled_at or now_ist()
    trading_day = get_trading_day()

    available = funds.get("available", {}) or {}
    utilised = funds.get("utilised", {}) or {}

    return {
        "trading_day": trading_day,
        "client_id": userid,
        "net_balance": funds.get("net"),
        "available_cash": available.get("cash"),
        "opening_balance": available.get("opening_balance"),
        "live_balance": available.get("live_balance"),
        "collateral": available.get("collateral"),
        "utilised_margin": _safe_num(utilised, "debits", default=0) or 0,
        "span_margin": _safe_num(utilised, "span"),
        "exposure_margin": _safe_num(utilised, "exposure"),
        "option_premium": _safe_num(utilised, "option_premium"),
        "m2m_realised": _safe_num(utilised, "m2m_realised"),
        "m2m_unrealised": _safe_num(utilised, "m2m_unrealised"),
        "available_margin": available.get("cash"),
        "polled_at": now,
    }


def sync_user_funds(
    user: UserSchema,
    invalidate_on_failure: bool = False,
    write_history: Optional[bool] = None,
) -> Dict[str, Any]:
    userid = _safe_str(getattr(user, "userid", ""), "").strip()
    kite, kite_err = get_kite_client(user)

    if write_history is None:
        write_history = DEFAULT_WRITE_FUNDS_HISTORY

    if kite_err:
        if invalidate_on_failure:
            invalidate_user_session(user)

        return {
            "status": "error",
            "userid": userid,
            "message": kite_err,
            "record": None,
            "history_record": None,
        }

    try:
        funds = kite.margins("equity") or {}
        payload = map_kite_equity_funds(user=user, funds=funds)

        history_rec = None
        if write_history:
            history_rec = UserFundsHistorySchema.create_snapshot(
                client_id=payload["client_id"],
                trading_day=payload["trading_day"],
                snapshot_json=funds,
                polled_at=payload["polled_at"],
            )
            if not history_rec:
                logger.warning(
                    "Funds history insert failed for userid=%s trading_day=%s",
                    userid,
                    payload["trading_day"],
                )

        saved = UserFundsSchema.upsert_for_user_day(payload)
        if not saved:
            logger.error("Funds fetched but failed to upsert user_funds for userid=%s", userid)
            return {
                "status": "error",
                "userid": userid,
                "message": "funds_persist_failed",
                "record": None,
                "history_record": history_rec,
            }

        return {
            "status": "success",
            "userid": userid,
            "message": "funds_synced",
            "record": saved,
            "history_record": history_rec,
        }

    except Exception:
        logger.exception("Error fetching funds for userid=%s", userid)

        if invalidate_on_failure:
            invalidate_user_session(user)

        return {
            "status": "error",
            "userid": userid,
            "message": "funds_fetch_failed",
            "record": None,
            "history_record": None,
        }


# ---------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------

def derive_position_instrument(exchange: Any, tradingsymbol: Any) -> Optional[str]:
    tradingsymbol = _safe_str(tradingsymbol, "").strip()
    exchange = _safe_str(exchange, "").strip().upper()

    if not tradingsymbol:
        return None

    if exchange in ("NSE", "BSE"):
        return "EQUITY"

    if exchange == "NFO":
        if tradingsymbol.endswith("FUT"):
            return "FUT"
        if tradingsymbol.endswith("CE"):
            return "OPT_CE"
        if tradingsymbol.endswith("PE"):
            return "OPT_PE"
        return "FNO"

    if exchange == "MCX":
        return "COMMODITY"

    if exchange == "CDS":
        return "CURRENCY"

    return None


def map_kite_positions(
    user: UserSchema,
    positions: List[Dict[str, Any]],
    polled_at: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    userid = _safe_str(getattr(user, "userid", ""), "").strip()
    now = polled_at or now_ist()
    trading_day = get_trading_day()

    out: List[Dict[str, Any]] = []

    for pos in positions or []:
        if not isinstance(pos, dict):
            continue

        tradingsymbol = _safe_str(pos.get("tradingsymbol"), "").strip()
        if not tradingsymbol:
            continue

        exchange = pos.get("exchange")
        instrument = derive_position_instrument(exchange, tradingsymbol)

        out.append({
            "trading_day": trading_day,
            "client_id": userid,
            "tradingsymbol": tradingsymbol,
            "instrument": instrument,
            "instrument_token": pos.get("instrument_token"),
            "exchange": pos.get("exchange"),
            "segment": pos.get("segment"),
            "product": pos.get("product"),
            "quantity": pos.get("quantity"),
            "overnight_quantity": pos.get("overnight_quantity"),
            "multiplier": pos.get("multiplier"),
            "average_price": pos.get("average_price"),
            "close_price": pos.get("close_price"),
            "last_price": pos.get("last_price"),
            "value": pos.get("value"),
            "pnl": pos.get("pnl"),
            "m2m": pos.get("m2m"),
            "unrealised": pos.get("unrealised"),
            "realised": pos.get("realised"),
            "buy_quantity": pos.get("buy_quantity"),
            "buy_price": pos.get("buy_price"),
            "buy_value": pos.get("buy_value"),
            "buy_m2m": pos.get("buy_m2m"),
            "sell_quantity": pos.get("sell_quantity"),
            "sell_price": pos.get("sell_price"),
            "sell_value": pos.get("sell_value"),
            "sell_m2m": pos.get("sell_m2m"),
            "day_buy_quantity": pos.get("day_buy_quantity"),
            "day_buy_price": pos.get("day_buy_price"),
            "day_buy_value": pos.get("day_buy_value"),
            "day_sell_quantity": pos.get("day_sell_quantity"),
            "day_sell_price": pos.get("day_sell_price"),
            "day_sell_value": pos.get("day_sell_value"),
            "polled_at": now,
        })

    return out


def sync_user_positions(
    user: UserSchema,
    invalidate_on_failure: bool = False,
    write_history: Optional[bool] = None,
) -> Dict[str, Any]:
    userid = _safe_str(getattr(user, "userid", ""), "").strip()
    kite, kite_err = get_kite_client(user)

    if write_history is None:
        write_history = DEFAULT_WRITE_POSITIONS_HISTORY

    if kite_err:
        if invalidate_on_failure:
            invalidate_user_session(user)

        return {
            "status": "error",
            "userid": userid,
            "message": kite_err,
            "deleted_count": 0,
            "inserted_count": 0,
            "history_record": None,
        }

    try:
        positions_payload = kite.positions() or {}
        positions_net = positions_payload.get("net") or []
        positions_day = positions_payload.get("day") or []

        polled_at = now_ist()
        trading_day = get_trading_day()

        history_rec = None
        if write_history:
            history_rec = UserPositionsHistorySchema.create_snapshot(
                client_id=userid,
                trading_day=trading_day,
                broker_payload={
                    "net": positions_net,
                    "day": positions_day,
                },
                polled_at=polled_at,
            )
            if not history_rec:
                logger.warning(
                    "Positions history insert failed for userid=%s trading_day=%s",
                    userid,
                    trading_day,
                )

        # Prefer NET, but fall back to DAY if NET is empty.
        source_positions = positions_net if positions_net else positions_day

        logger.info(
            "Positions payload userid=%s net_count=%s day_count=%s source=%s",
            userid,
            len(positions_net),
            len(positions_day),
            "net" if positions_net else "day" if positions_day else "none",
        )

        rows = map_kite_positions(
            user=user,
            positions=source_positions,
            polled_at=polled_at,
        )

        logger.info(
            "Positions mapped userid=%s mapped_rows=%s",
            userid,
            len(rows),
        )

        # If broker returned positions but mapping produced zero rows, surface it.
        if source_positions and not rows:
            logger.error(
                "Positions mapping produced zero rows for userid=%s despite non-empty broker payload",
                userid,
            )
            return {
                "status": "error",
                "userid": userid,
                "message": "positions_mapping_failed",
                "deleted_count": 0,
                "inserted_count": 0,
                "history_record": history_rec,
            }

        deleted_count = UserPositionsSchema.delete_for_user_day(
            client_id=userid,
            trading_day=trading_day,
        )

        inserted_count = 0
        if rows:
            inserted_count = UserPositionsSchema.bulk_insert_for_user_day(rows)

            if inserted_count != len(rows):
                logger.error(
                    "Positions bulk insert mismatch for userid=%s expected=%s inserted=%s",
                    userid,
                    len(rows),
                    inserted_count,
                )
                return {
                    "status": "error",
                    "userid": userid,
                    "message": "positions_persist_failed",
                    "deleted_count": deleted_count,
                    "inserted_count": inserted_count,
                    "history_record": history_rec,
                }

        return {
            "status": "success",
            "userid": userid,
            "message": "positions_synced",
            "deleted_count": deleted_count,
            "inserted_count": inserted_count,
            "history_record": history_rec,
        }

    except Exception:
        logger.exception("Error fetching positions for userid=%s", userid)

        if invalidate_on_failure:
            invalidate_user_session(user)

        return {
            "status": "error",
            "userid": userid,
            "message": "positions_fetch_failed",
            "deleted_count": 0,
            "inserted_count": 0,
            "history_record": None,
        }


# ---------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------

def derive_order_instrument(exchange: Any, tradingsymbol: Any) -> Optional[str]:
    tradingsymbol = _safe_str(tradingsymbol, "").strip()
    exchange = _safe_str(exchange, "").strip().upper()

    if not tradingsymbol:
        return None

    if exchange in ("NSE", "BSE"):
        return "EQUITY"

    if exchange == "NFO":
        if tradingsymbol.endswith("FUT"):
            return "FUT"
        if tradingsymbol.endswith("CE"):
            return "OPT_CE"
        if tradingsymbol.endswith("PE"):
            return "OPT_PE"
        return "FNO"

    if exchange == "MCX":
        return "COMMODITY"

    if exchange == "CDS":
        return "CURRENCY"

    return None


def map_kite_orders(
    user: UserSchema,
    orders: List[Dict[str, Any]],
    polled_at: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    userid = _safe_str(getattr(user, "userid", ""), "").strip()
    now = polled_at or now_ist()
    trading_day = get_trading_day()

    out: List[Dict[str, Any]] = []

    for od in orders or []:
        if not isinstance(od, dict):
            continue

        order_id = _safe_str(od.get("order_id"), "").strip()
        if not order_id:
            continue

        tradingsymbol = _safe_str(od.get("tradingsymbol"), "").strip()
        exchange = od.get("exchange")
        instrument = derive_order_instrument(exchange, tradingsymbol)

        out.append({
            "trading_day": trading_day,
            "client_id": userid,
            "order_id": order_id,
            "exchange_order_id": od.get("exchange_order_id"),
            "tradingsymbol": tradingsymbol or None,
            "instrument": instrument,
            "instrument_token": od.get("instrument_token"),
            "exchange": od.get("exchange"),
            "transaction_type": od.get("transaction_type"),
            "product": od.get("product"),
            "order_type": od.get("order_type"),
            "variety": od.get("variety"),
            "validity": od.get("validity"),
            "validity_ttl": od.get("validity_ttl"),
            "quantity": od.get("quantity"),
            "disclosed_quantity": od.get("disclosed_quantity"),
            "filled_quantity": od.get("filled_quantity"),
            "pending_quantity": od.get("pending_quantity"),
            "cancelled_quantity": od.get("cancelled_quantity"),
            "price": od.get("price"),
            "average_price": od.get("average_price"),
            "trigger_price": od.get("trigger_price"),
            "status": od.get("status"),
            "order_timestamp": od.get("order_timestamp"),
            "exchange_timestamp": od.get("exchange_timestamp"),
            "tag": od.get("tag"),
            "order_issued_at": od.get("order_issued_at"),
            "order_placed_by": od.get("order_placed_by"),
            "recon_status": od.get("recon_status"),
            "polled_at": now,
        })

    return out


def sync_user_orders(
    user: UserSchema,
    invalidate_on_failure: bool = False,
    write_history: Optional[bool] = None,
) -> Dict[str, Any]:
    userid = _safe_str(getattr(user, "userid", ""), "").strip()
    kite, kite_err = get_kite_client(user)

    if write_history is None:
        write_history = DEFAULT_WRITE_ORDERS_HISTORY

    if kite_err:
        if invalidate_on_failure:
            invalidate_user_session(user)

        return {
            "status": "error",
            "userid": userid,
            "message": kite_err,
            "upserted_count": 0,
            "history_record": None,
        }

    try:
        orders_payload = kite.orders() or []
        polled_at = now_ist()
        trading_day = get_trading_day()

        history_rec = None
        if write_history:
            history_rec = UserOrdersHistorySchema.create_snapshot(
                client_id=userid,
                trading_day=trading_day,
                broker_payload=orders_payload,
                polled_at=polled_at,
            )
            if not history_rec:
                logger.warning(
                    "Orders history insert failed for userid=%s trading_day=%s",
                    userid,
                    trading_day,
                )

        rows = map_kite_orders(
            user=user,
            orders=orders_payload,
            polled_at=polled_at,
        )

        upserted_count = 0
        for row in rows:
            saved = UserOrdersSchema.upsert_order(row)
            if saved:
                upserted_count += 1
            else:
                logger.warning(
                    "Order upsert failed for userid=%s order_id=%s",
                    userid,
                    row.get("order_id"),
                )

        if rows and upserted_count != len(rows):
            logger.error(
                "Orders upsert mismatch for userid=%s expected=%s upserted=%s",
                userid,
                len(rows),
                upserted_count,
            )
            return {
                "status": "error",
                "userid": userid,
                "message": "orders_persist_failed",
                "upserted_count": upserted_count,
                "history_record": history_rec,
            }

        return {
            "status": "success",
            "userid": userid,
            "message": "orders_synced",
            "upserted_count": upserted_count,
            "history_record": history_rec,
        }

    except Exception:
        logger.exception("Error fetching orders for userid=%s", userid)

        if invalidate_on_failure:
            invalidate_user_session(user)

        return {
            "status": "error",
            "userid": userid,
            "message": "orders_fetch_failed",
            "upserted_count": 0,
            "history_record": None,
        }