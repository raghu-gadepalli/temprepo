from functools import wraps
import logging
from datetime import date, datetime
from decimal import Decimal

from flask import Blueprint, abort, jsonify, render_template, request, session
from sqlalchemy import text

from routes.routes import login_required
from utils.access import is_operator
from utils.account_scope import broker_users_for_actor, userids as scope_userids

from database.database import get_trades_db
from utils.trading_day import get_trading_day, get_order_variety
from services.symbol_service import get_symbol
from services.oms.oms_kite_service import get_kite_client_by_id,  kite_get_ltp

logger = logging.getLogger(__name__)

oms_bp = Blueprint("oms", __name__, url_prefix="/oms")


# =============================================================================
# ACCESS
# =============================================================================

def oms_access_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        actor_userid = session.get("userid")
        if not is_operator(actor_userid):
            logger.warning("OMS access denied for userid=%s", actor_userid)
            abort(403)
        return f(*args, **kwargs)
    return decorated_function


# =============================================================================
# JSON HELPERS
# =============================================================================

def json_ok(data=None, message=None, meta=None, status_code=200):
    payload = {"status": "success", "data": data if data is not None else {}}
    if message:
        payload["message"] = message
    if meta is not None:
        payload["meta"] = meta
    return jsonify(payload), status_code


def json_error(message, status_code=400, data=None, meta=None):
    payload = {"status": "error", "message": message, "data": data or {}}
    if meta is not None:
        payload["meta"] = meta
    return jsonify(payload), status_code


def to_primitive(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return value


def row_to_dict(row):
    if hasattr(row, "_mapping"):
        src = dict(row._mapping)
    else:
        src = dict(row)
    return {k: to_primitive(v) for k, v in src.items()}


# =============================================================================
# HELPERS
# =============================================================================

def adjust_limit_price(price, side):
    tick = 0.05
    if side == "SELL":
        return round(price - tick, 2)
    return round(price + tick, 2)


def place_exit_order(kite, exchange, symbol, product, qty):
    qty = int(qty)

    transaction_type = "SELL" if qty > 0 else "BUY"
    order_qty = abs(qty)

    instrument = f"{exchange}:{symbol}"

    ltp_data = kite.ltp(instrument)
    ltp = ltp_data[instrument]["last_price"]

    tick = 0.05
    price = round(ltp - tick, 2) if transaction_type == "SELL" else round(ltp + tick, 2)

    logger.info(
        "Placing LIMIT exit order: %s - %s - %s - %s",
        symbol, transaction_type, order_qty, price
    )

    variety = get_order_variety()
    order_id = kite.place_order(
        variety=variety,
        exchange=exchange,
        tradingsymbol=symbol,
        transaction_type=transaction_type,
        quantity=order_qty,
        order_type="LIMIT",
        price=price,
        product=product,
    )

    return order_id


def exit_position_for_client(client_id, exchange, product, symbol, qty):
    if not _is_visible_broker_client(client_id, logged_in=1):
        return {
            "status": "error",
            "message": "Client is not an active REAL broker user",
        }

    kite = get_kite_client_by_id(client_id)

    if not kite:
        return {
            "status": "error",
            "message": "Client not active or logged in"
        }

    try:
        order_id = place_exit_order(kite, exchange, symbol, product, qty)
        logger.info("Exit order placed successfully. Order ID: %s", order_id)
        return {
            "status": "success",
            "data": {
                "order_id": order_id,
                "client_id": client_id,
                "tradingsymbol": symbol,
            },
            "message": f"Exit order placed for {symbol}",
        }
    except Exception as e:
        logger.exception("Order placement failed: %s", e)
        return {
            "status": "error",
            "message": str(e)
        }


def get_page_context(oms_page):
    return {
        "active": "oms",
        "oms_page": oms_page,
        "nav_section": "dashboard",
    }


def _visible_broker_client_ids(*, logged_in=None):
    """Return only REAL broker-enabled clients visible to the OMS actor."""
    actor = session.get("userid")
    return scope_userids(
        broker_users_for_actor(actor, logged_in=logged_in)
    )


def _is_visible_broker_client(client_id, *, logged_in=None):
    requested = str(client_id or "").strip().upper()
    if not requested:
        return False
    return requested in {
        userid.upper()
        for userid in _visible_broker_client_ids(logged_in=logged_in)
    }


# =============================================================================
# DATA BUILDERS
# =============================================================================

def build_dashboard_data(db):
    dashboard = {
        "active_clients": 0,
        "total_clients": 0,
        "open_orders": 0,
        "open_positions": 0,
        "today_pnl": 0.0,
        "total_positions": 0,
        "total_exposure": 0.0,
        "total_pnl": 0.0,
        "total_funds": 0.0,
    }

    cur_trading_day = get_trading_day()

    result = db.execute(text("""
        SELECT COUNT(*)
        FROM users
        WHERE broker_login = 1
          AND logged_in = 1
          AND active = 1
          AND UPPER(execution_mode) = 'REAL'
          AND access_token IS NOT NULL
    """))
    dashboard["active_clients"] = int(result.scalar() or 0)

    result = db.execute(text("""
        SELECT COUNT(*)
        FROM users
        WHERE broker_login = 1
          AND active = 1
          AND UPPER(execution_mode) = 'REAL'
    """))
    dashboard["total_clients"] = int(result.scalar() or 0)

    result = db.execute(text("""
        SELECT
            COUNT(*) AS total_positions,
            COALESCE(SUM(pnl), 0) AS total_pnl
        FROM (
            SELECT p.*
            FROM oms_positions p
            JOIN (
                SELECT client_id, tradingsymbol, product, MAX(polled_at) AS latest
                FROM oms_positions
                GROUP BY client_id, tradingsymbol, product
            ) latest
              ON p.client_id = latest.client_id
             AND p.tradingsymbol = latest.tradingsymbol
             AND (p.product <=> latest.product)
             AND p.polled_at = latest.latest
        ) x
    """)).mappings().first()

    if result:
        dashboard["total_positions"] = int(result["total_positions"] or 0)
        dashboard["total_pnl"] = float(result["total_pnl"] or 0)

    result = db.execute(text("""
        SELECT COUNT(*) AS open_positions
        FROM (
            SELECT p.*
            FROM oms_positions p
            JOIN (
                SELECT client_id, tradingsymbol, product, MAX(polled_at) AS latest
                FROM oms_positions
                GROUP BY client_id, tradingsymbol, product
            ) latest
              ON p.client_id = latest.client_id
             AND p.tradingsymbol = latest.tradingsymbol
             AND (p.product <=> latest.product)
             AND p.polled_at = latest.latest
        ) x
        WHERE quantity <> 0
    """)).mappings().first()

    if result:
        dashboard["open_positions"] = int(result["open_positions"] or 0)

    dashboard["today_pnl"] = float(db.execute(text("""
        SELECT COALESCE(SUM(pnl), 0)
        FROM (
            SELECT p.*
            FROM oms_positions p
            JOIN (
                SELECT client_id, tradingsymbol, product, MAX(polled_at) AS latest
                FROM oms_positions
                GROUP BY client_id, tradingsymbol, product
            ) latest
              ON p.client_id = latest.client_id
             AND p.tradingsymbol = latest.tradingsymbol
             AND (p.product <=> latest.product)
             AND p.polled_at = latest.latest
        ) x
    """)).scalar() or 0)

    funds = db.execute(text("""
        SELECT COALESCE(SUM(net_balance), 0)
        FROM oms_funds
        WHERE trading_day = :cur_trading_day
    """), {"cur_trading_day": cur_trading_day}).scalar()
    dashboard["total_funds"] = float(funds or 0)

    dashboard["open_orders"] = int(db.execute(text("""
        SELECT COUNT(*)
        FROM oms_orders
        WHERE trading_day = :cur_trading_day
    """), {"cur_trading_day": cur_trading_day}).scalar() or 0)

    return dashboard


def build_clients_data(db):
    rows = db.execute(text("""
        SELECT
            u.userid AS client_id,
            u.name,
            u.active,
            u.broker_login,
            u.logged_in,
            u.logged_time,
            u.execution_mode
        FROM users u
            WHERE u.broker_login = 1
            AND u.active = 1
            AND UPPER(u.execution_mode) = 'REAL'
    """)).mappings().all()

    return [row_to_dict(r) for r in rows]


def build_new_order_data(db):
    rows = db.execute(text("""
        SELECT
            userid AS client_id,
            name
        FROM users
        WHERE broker_login = 1
          AND active = 1
          AND logged_in = 1
          AND UPPER(execution_mode) = 'REAL'
          AND access_token IS NOT NULL
        ORDER BY name, userid
    """)).mappings().all()

    return {
        "clients": [row_to_dict(r) for r in rows]
    }


def build_orders_data(db):
    cur_trading_day = get_trading_day()

    rows = db.execute(text("""
        SELECT
            o.id,
            o.trading_day,
            o.client_id,
            CONCAT(u.name, ' (', o.client_id, ')') AS client_name,
            o.order_id,
            o.exchange_order_id,
            o.tradingsymbol,
            o.instrument,
            o.instrument_token,
            o.exchange,
            o.transaction_type,
            o.product,
            o.order_type,
            o.variety,
            o.quantity,
            o.filled_quantity,
            o.pending_quantity,
            o.cancelled_quantity,
            o.price,
            o.average_price,
            o.trigger_price,
            o.status,
            o.order_timestamp,
            o.exchange_timestamp,
            o.tag,
            o.order_placed_by,
            o.order_issued_at,
            o.recon_status,
            o.created_at,
            o.polled_at
        FROM oms_orders o
        JOIN users u
          ON o.client_id = u.userid
        WHERE DATE(o.trading_day) = :cur_trading_day
        ORDER BY u.name, o.client_id, o.order_timestamp DESC, o.order_id DESC
    """), {"cur_trading_day": cur_trading_day}).mappings().all()

    data = [row_to_dict(r) for r in rows]
    meta = {
        "trading_day": str(cur_trading_day),
        "row_count": len(data),
    }
    return data, meta

def build_positions_data(db):
    rows = db.execute(text("""
        SELECT
            p.client_id,
            CONCAT(u.name, ' (', p.client_id, ') ') AS client_name,
            p.tradingsymbol,
            p.product,
            p.exchange,
            p.quantity,
            p.average_price,
            p.last_price,
            p.pnl,
            p.m2m,
            p.polled_at
        FROM oms_positions p
        JOIN users u
          ON p.client_id = u.userid
        JOIN (
            SELECT client_id, tradingsymbol, product, MAX(polled_at) AS latest
            FROM oms_positions
            GROUP BY client_id, tradingsymbol, product
        ) latest
          ON p.client_id = latest.client_id
         AND p.tradingsymbol = latest.tradingsymbol
         AND (p.product <=> latest.product)
         AND p.polled_at = latest.latest
        ORDER BY p.client_id, p.tradingsymbol
    """)).mappings().all()

    data = [row_to_dict(r) for r in rows]
    meta = {"row_count": len(data)}
    return data, meta


def build_funds_data(db):
    rows = db.execute(text("""
        SELECT
            u.userid AS client_id,
            CONCAT(u.name, ' (', u.userid, ')') AS client_name,

            f.net_balance,
            f.available_cash,
            f.collateral,
            f.utilised_margin,
            f.span_margin,
            f.exposure_margin,
            f.option_premium,
            f.m2m_realised,
            f.m2m_unrealised,
            f.polled_at,

            CASE
                WHEN u.logged_in = 1 AND f.client_id IS NOT NULL THEN 'LIVE'
                WHEN u.logged_in = 1 AND f.client_id IS NULL THEN 'NOT_REFRESHED'
                ELSE 'NOT_LOGGED_IN'
            END AS funds_status

        FROM users u
        LEFT JOIN (
            SELECT x.*
            FROM oms_funds x
            JOIN (
                SELECT client_id, MAX(polled_at) AS latest
                FROM oms_funds
                GROUP BY client_id
            ) latest
              ON x.client_id = latest.client_id
             AND x.polled_at = latest.latest
        ) f
          ON u.userid = f.client_id

        WHERE u.broker_login = 1
          AND u.active = 1
          AND UPPER(u.execution_mode) = 'REAL'

        ORDER BY u.name, u.userid
    """)).mappings().all()

    data = [row_to_dict(r) for r in rows]
    meta = {"row_count": len(data)}
    return data, meta

# =============================================================================
# OMS PAGE ROUTES
# =============================================================================

@oms_bp.route("/")
@oms_bp.route("/dashboard")
@login_required
@oms_access_required
def dashboard():
    return render_template("oms_dashboard.html", **get_page_context("dashboard"))


@oms_bp.route("/dashboard/data")
@login_required
@oms_access_required
def dashboard_data():
    with get_trades_db() as db:
        data = build_dashboard_data(db)
    logger.info("OMS dashboard data: %s", data)
    return json_ok(data=data)


@oms_bp.route("/clients")
@login_required
@oms_access_required
def oms_clients():
    return render_template("oms_clients.html", **get_page_context("clients"))


@oms_bp.route("/clients/data")
@login_required
@oms_access_required
def oms_clients_data():
    with get_trades_db() as db:
        data = build_clients_data(db)
    return json_ok(data=data, meta={"row_count": len(data)})


@oms_bp.route("/new-order")
@login_required
@oms_access_required
def oms_new_order():
    return render_template("oms_new_order.html", **get_page_context("new_order"))


@oms_bp.route("/new-order/data")
@login_required
@oms_access_required
def oms_new_order_data():
    with get_trades_db() as db:
        data = build_new_order_data(db)
    return json_ok(data=data, meta={"client_count": len(data.get("clients", []))})


@oms_bp.route("/orders")
@login_required
@oms_access_required
def oms_orders():
    return render_template("oms_orders.html", **get_page_context("orders"))


@oms_bp.route("/orders/data")
@login_required
@oms_access_required
def oms_orders_data():
    with get_trades_db() as db:
        data, meta = build_orders_data(db)
    return json_ok(data=data, meta=meta)


@oms_bp.route("/positions")
@login_required
@oms_access_required
def oms_positions():
    return render_template("oms_positions.html", **get_page_context("positions"))


@oms_bp.route("/positions/data")
@login_required
@oms_access_required
def oms_positions_data():
    with get_trades_db() as db:
        data, meta = build_positions_data(db)
    return json_ok(data=data, meta=meta)


@oms_bp.route("/funds")
@login_required
@oms_access_required
def oms_funds():
    return render_template("oms_funds.html", **get_page_context("funds"))


@oms_bp.route("/funds/data")
@login_required
@oms_access_required
def oms_funds_data():
    with get_trades_db() as db:
        data, meta = build_funds_data(db)
    return json_ok(data=data, meta=meta)


# =============================================================================
# OMS ACTION ROUTES
# =============================================================================

@oms_bp.route("/orders/<client_id>/<order_id>/modify", methods=["POST"])
@login_required
@oms_access_required
def modify_order(client_id, order_id):
    data = request.get_json(silent=True) or {}
    if not _is_visible_broker_client(client_id, logged_in=1):
        return json_error("Client is not an active REAL broker user", 403)
    kite = get_kite_client_by_id(client_id)

    if not kite:
        return json_error("Client not active or logged in", 400)

    try:
        kite.modify_order(
            variety=data["variety"],
            order_id=order_id,
            quantity=int(data["quantity"]),
            price=float(data["price"]),
        )
        return json_ok(
            data={"client_id": client_id, "order_id": order_id},
            message="Order modified",
        )
    except Exception as e:
        logger.exception("Order modify failed for client=%s order_id=%s", client_id, order_id)
        return json_error(str(e), 400)


@oms_bp.route("/place-order", methods=["POST"])
@login_required
@oms_access_required
def place_order():
    data = request.get_json(silent=True) or {}
    logger.info("Place order payload: %s", data)

    symbol = data.get("symbol")
    qty = data.get("qty")
    price = data.get("price")
    order_type = data.get("order_type")
    product = data.get("product")
    side = data.get("transaction_type")
    clients = data.get("clients") or []
    exchange = data.get("exchange") or "NSE"

    if not symbol:
        return json_error("Symbol is required", 400)
    if not order_type:
        return json_error("Order type is required", 400)
    if not product:
        return json_error("Product is required", 400)
    if side not in {"BUY", "SELL"}:
        return json_error("Transaction type must be BUY or SELL", 400)
    if not clients:
        return json_error("At least one client must be selected", 400)

    allowed_clients = {u.upper() for u in _visible_broker_client_ids(logged_in=1)}
    invalid_clients = [
        str(client_id or "").strip()
        for client_id in clients
        if str(client_id or "").strip().upper() not in allowed_clients
    ]
    if invalid_clients:
        return json_error(
            "Only active REAL broker users can receive direct OMS orders",
            403,
            data={"invalid_clients": invalid_clients},
        )

    try:
        qty = int(qty)
    except Exception:
        return json_error("Quantity must be an integer", 400)

    if qty <= 0:
        return json_error("Quantity must be greater than zero", 400)

    if order_type == "LIMIT":
        try:
            price = float(price)
        except Exception:
            return json_error("Price must be a number for LIMIT orders", 400)

    if exchange == "NFO" and order_type == "MARKET":
        order_type = "LIMIT"

    if order_type == "LIMIT":
        price = adjust_limit_price(price, side)

    logger.info("Placing order: %s - %s - %s - %s", symbol, side, qty, price)

    variety = get_order_variety()
    results = []

    for client_id in clients:
        try:
            kite = get_kite_client_by_id(client_id)
            if not kite:
                raise ValueError("Client not active or logged in")

            order_id = kite.place_order(
                variety=variety,
                exchange=exchange,
                tradingsymbol=symbol,
                transaction_type=side,
                quantity=qty,
                order_type=order_type,
                product=product,
                price=price if order_type == "LIMIT" else None,
            )

            results.append({
                "client_id": client_id,
                "status": "success",
                "order_id": order_id,
            })

        except Exception as e:
            logger.exception("Order placement failed for client=%s symbol=%s", client_id, symbol)
            results.append({
                "client_id": client_id,
                "status": "error",
                "message": str(e),
            })

    return json_ok(
        data={"results": results},
        meta={
            "success_count": sum(1 for r in results if r["status"] == "success"),
            "error_count": sum(1 for r in results if r["status"] == "error"),
        },
    )


@oms_bp.route("/exit_position", methods=["POST"])
@login_required
@oms_access_required
def exit_single_position():
    data = request.get_json(silent=True) or {}
    logger.info("Exit position payload: %s", data)

    result = exit_position_for_client(
        client_id=data["client_id"],
        exchange=data["exchange"],
        product=data["product"],
        symbol=data["tradingsymbol"],
        qty=int(data["quantity"]),
    )

    if result.get("status") == "success":
        return jsonify(result), 200
    return jsonify(result), 400


@oms_bp.route("/exit_selected_client", methods=["POST"])
@login_required
@oms_access_required
def exit_selected_client():
    rows = request.get_json(silent=True) or []

    for r in rows:
        exit_position_for_client(
            client_id=r["client_id"],
            exchange=r["exchange"],
            product=r["product"],
            symbol=r["tradingsymbol"],
            qty=r["quantity"],
        )

    return json_ok(
        data={"exited_positions": len(rows)},
        message="Selected client positions processed",
    )


@oms_bp.route("/exit_selected_global", methods=["POST"])
@login_required
@oms_access_required
def exit_selected_global():
    rows = request.get_json(silent=True) or []

    if len(rows) == 0:
        return json_error("No positions selected", 400)

    for r in rows:
        exit_position_for_client(
            client_id=r["client_id"],
            exchange=r["exchange"],
            product=r["product"],
            symbol=r["tradingsymbol"],
            qty=r["quantity"],
        )

    return json_ok(
        data={"exited_positions": len(rows)},
        message="Selected positions processed",
    )


@oms_bp.route("/bulk_exit_positions", methods=["POST"])
@login_required
@oms_access_required
def bulk_exit_positions():
    rows = request.get_json(silent=True) or []
    logger.info("Bulk exit payload: %s", rows)

    if len(rows) == 0:
        return json_error("No positions selected", 400)

    for r in rows:
        exit_position_for_client(
            client_id=r["client_id"],
            exchange=r["exchange"],
            product=r["product"],
            symbol=r["tradingsymbol"],
            qty=r["quantity"],
        )

    return json_ok(
        data={"exited_positions": len(rows)},
        message="Bulk exit processed",
    )


@oms_bp.route("/cancel-order", methods=["POST"])
@login_required
@oms_access_required
def cancel_order():
    data = request.get_json(silent=True) or {}

    client_id = data.get("client_id")
    order_id = data.get("order_id")
    variety = data.get("variety", "regular")

    if not client_id:
        return json_error("client_id is required", 400)
    if not order_id:
        return json_error("order_id is required", 400)
    if not _is_visible_broker_client(client_id, logged_in=1):
        return json_error("Client is not an active REAL broker user", 403)

    try:
        kite = get_kite_client_by_id(client_id)
        if not kite:
            return json_error("Client not active or logged in", 400)

        kite.cancel_order(
            variety=variety,
            order_id=order_id,
        )

        return json_ok(
            data={"client_id": client_id, "order_id": order_id},
            message="Order cancelled",
        )

    except Exception as e:
        logger.exception("Order cancel failed for client=%s order_id=%s", client_id, order_id)
        return json_error(str(e), 400)


# =============================================================================
# OMS REFERENCE / LOOKUP ROUTES
# =============================================================================

@oms_bp.route("/fo-underlyings")
@login_required
@oms_access_required
def fo_underlyings():
    with get_trades_db() as db:
        result = db.execute(text("""
            SELECT symbol
            FROM symbols
            WHERE type = 'EQ'
              AND exchange = 'NSE'
              AND token IS NOT NULL
            ORDER BY symbol
        """))

        rows = [r[0] for r in result]

    return json_ok(data=rows)


@oms_bp.route("/fo-expiry")
@login_required
@oms_access_required
def fo_expiry():
    symbol = request.args.get("symbol")

    with get_trades_db() as db:
        result = db.execute(text("""
            SELECT DISTINCT expiry
            FROM symbols
            WHERE symbol LIKE :prefix
              AND expiry IS NOT NULL
            ORDER BY expiry
        """), {"prefix": f"{symbol}%"})

        rows = [to_primitive(r[0]) for r in result]

    return json_ok(data=rows)


@oms_bp.route("/fo-strikes")
@login_required
@oms_access_required
def fo_strikes():
    symbol = request.args.get("symbol")
    expiry = request.args.get("expiry")
    opt_type = request.args.get("type")

    if not symbol or not expiry or not opt_type:
        return json_error("symbol, expiry and type are required", 400)

    expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()

    with get_trades_db() as db:
        result = db.execute(text("""
            SELECT DISTINCT strike_price
            FROM symbols
            WHERE symbol LIKE :prefix
              AND expiry = :expiry
              AND strike_price > 0
              AND type = :type
            ORDER BY strike_price
        """), {
            "prefix": f"{symbol}%",
            "expiry": expiry_date,
            "type": opt_type,
        })

        rows = [to_primitive(r[0]) for r in result]

    return json_ok(data=rows)


@oms_bp.route("/fo-instrument")
@login_required
@oms_access_required
def fo_instrument():
    symbol = request.args.get("symbol")
    expiry = request.args.get("expiry")
    opt_type = request.args.get("type")
    strike = request.args.get("strike")

    with get_trades_db() as db:
        result = db.execute(text("""
            SELECT symbol, lotsize
            FROM symbols
            WHERE symbol LIKE :prefix
              AND expiry = :expiry
              AND type = :type
              AND (
                    (:type = 'FUT')
                    OR strike_price = :strike
              )
            LIMIT 1
        """), {
            "prefix": f"{symbol}%",
            "expiry": expiry,
            "type": opt_type,
            "strike": strike,
        }).fetchone()

    if not result:
        return json_ok(data={"symbol": "", "lotsize": 1})

    return json_ok(data={"symbol": result[0], "lotsize": result[1]})


@oms_bp.route("/fo-symbol")
@login_required
@oms_access_required
def fo_symbol():
    name = request.args.get("name")
    expiry = request.args.get("expiry")
    strike = request.args.get("strike")
    option_type = request.args.get("type")

    symbol = get_symbol(name, expiry, strike, option_type)

    return json_ok(data={"symbol": symbol[0], "lotsize": symbol[1]})


@oms_bp.route("/equity-symbols")
@login_required
@oms_access_required
def equity_symbols():
    with get_trades_db() as db:
        result = db.execute(text("""
            SELECT symbol, price
            FROM symbols
            WHERE segment = 'NSE'
              AND type = 'EQ'
            ORDER BY symbol
        """))

        data = [
            {
                "symbol": r[0],
                "price": float(r[1]) if r[1] else 0.0,
            }
            for r in result
        ]

    return json_ok(data=data, meta={"row_count": len(data)})


@oms_bp.route("/ltp")
@login_required
@oms_access_required
def get_ltp():
    symbol = request.args.get("symbol")
    exchange = request.args.get("exchange")

    if not symbol or not exchange:
        return json_error("symbol and exchange are required", 400)

    try:
        result = kite_get_ltp(exchange, symbol)
        return json_ok(data=result)
    except Exception:
        logger.exception("LTP fetch failed for %s:%s", exchange, symbol)
        return json_error(
            "LTP_FETCH_FAILED",
            500,
            data={"ltp": None},
        )