# routes/admin.py

import json
import logging
from datetime import timedelta

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from config import AppConfig
from configs.snapshot_config import SNAPSHOT_CONFIG
from routes.routes import login_required
from utils.access import is_admin
from utils.datetime_utils import now_ist, parse_iso, to_tz, IST

from schemas.alert import AlertSchema
from schemas.symbol import SymbolSchema
from schemas.user import UserSchema
from services.snapshot.snapshot_generator import SnapshotGenerator
from services.zerodha.kiteconnect_service import (
    KiteConnectService,
    TokenException,
    InputException,
)

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def _deny_admin():
    return jsonify({"error": "Admin access required"}), 403


def _render_admin(page=None, **kwargs):
    ctx = {
        "active": "admin",
        "nav_section": "admin",
    }
    if page:
        ctx["page"] = page
    ctx.update(kwargs)
    return render_template("admin.html", **ctx)


# -------------------------------------------------------------------
# Admin Home
# -------------------------------------------------------------------

@admin_bp.route("/")
@login_required
def admin_home():
    if not is_admin():
        return _deny_admin()

    return _render_admin()


# -------------------------------------------------------------------
# Admin Users
# -------------------------------------------------------------------

@admin_bp.route("/users")
@login_required
def admin_users():
    if not is_admin():
        return _deny_admin()

    return _render_admin(page="users")


# -------------------------------------------------------------------
# Admin System
# -------------------------------------------------------------------

@admin_bp.route("/system")
@login_required
def admin_system():
    if not is_admin():
        return _deny_admin()

    return _render_admin(page="system")


# -------------------------------------------------------------------
# Admin Webhook
# Keep public unless/until you decide to protect it explicitly.
# -------------------------------------------------------------------

@admin_bp.route("/webhook", methods=["POST"])
def admin_webhook():
    raw = request.get_data(as_text=True)
    logger.info("RAW ADMIN WEBHOOK BODY: %s", raw)

    try:
        data = json.loads(raw)
        logger.info("Parsed JSON payload: %s", data)
    except Exception:
        data = {"message": raw}

    message = data.get("message", raw)
    etime = now_ist()

    alert_data = {"message": message, "etime": etime}
    new_alert = AlertSchema.create_alert(alert_data)
    logger.info("Stored alert id=%s with message length=%d", new_alert.id, len(message))

    return jsonify(status="success", alert_id=new_alert.id), 200


# -------------------------------------------------------------------
# Admin Snapshot Tool
# -------------------------------------------------------------------

@admin_bp.route("/snapshot", methods=["GET", "POST"])
@login_required
def admin_snapshot():
    if not is_admin():
        return _deny_admin()

    user = UserSchema.fetch_user(AppConfig.DATA_USER)
    if not user:
        flash("No data-user found", "danger")
        return render_template(
            "dash_snapshot.html",
            symbols=[],
            result=None,
            nav_section="admin",
            active="admin",
            page="snapshot",
        )

    symbols = SymbolSchema.fetch_symbols(active=1) or []
    logger.info("Fetched %d symbols for snapshot dropdown", len(symbols))
    result = None

    if request.method == "POST":
        sym_str = request.form.get("symbol")
        dt_str = request.form.get("snapshot_time")
        operation = request.form.get("operation")
        freq = request.form.get("frequency")

        symbol = next((s for s in symbols if s.symbol == sym_str), None)
        if not symbol:
            flash(f"Unknown symbol: {sym_str}", "warning")
        else:
            try:
                end_dt = parse_iso(dt_str)
                end_dt = to_tz(end_dt, IST)
            except Exception:
                flash(f"Invalid date/time: {dt_str}", "warning")
                return render_template(
                    "dash_snapshot.html",
                    symbols=symbols,
                    frequencies=SNAPSHOT_CONFIG.single_frequencies,
                    result=None,
                    nav_section="admin",
                    active="admin",
                    page="snapshot",
                )

            if operation == "historical":
                svc = KiteConnectService(
                    api_key=user.apikey,
                    access_token=user.access_token,
                )
                try:
                    raw = svc.fetch_historical_data(
                        instrument_token=symbol.token,
                        from_date=end_dt - timedelta(minutes=1),
                        to_date=end_dt,
                        interval="minute",
                    )
                except TokenException as e:
                    logger.critical(
                        "Invalid credentials for %s: %s",
                        user.userid,
                        e,
                        exc_info=True,
                    )
                    flash("Invalid API key or token. Please log in again.", "error")
                    return redirect(url_for("main.logout"))
                except InputException:
                    raw = []

                if not raw:
                    logger.warning("No historical bar for %s @ %s", symbol.symbol, end_dt)
                    flash(f"No bar available for {symbol.symbol} at {end_dt}", "warning")
                else:
                    bar = raw[0]
                    try:
                        bar["date"] = to_tz(bar["date"], IST).isoformat()
                    except Exception:
                        logger.exception(
                            "Failed to convert bar date for %s: %r",
                            symbol.symbol,
                            bar,
                        )
                    result = bar

            elif operation == "frequency":
                gen = SnapshotGenerator(
                    token=symbol.token,
                    symbol=symbol.symbol,
                    api_key=user.apikey,
                    access_token=user.access_token,
                )
                try:
                    fs = gen.frequency_snapshot(
                        end_date=end_dt,
                        frequency=freq,
                        persist_candle=False,
                    )
                except TokenException:
                    logger.critical(
                        "Invalid credentials during freq fetch for %s",
                        user.userid,
                        exc_info=True,
                    )
                    flash("Invalid API key or token. Please log in again.", "error")
                    return redirect(url_for("main.logout"))

                if not fs:
                    logger.warning(
                        "No data for %s @ %s (freq=%s)",
                        symbol.symbol,
                        end_dt,
                        freq,
                    )
                    flash(
                        f"No data for {symbol.symbol} at {end_dt:%Y-%m-%d %H:%M}",
                        "warning",
                    )
                else:
                    result = fs.model_dump(mode="json")

            else:
                gen = SnapshotGenerator(
                    token=symbol.token,
                    symbol=symbol.symbol,
                    api_key=user.apikey,
                    access_token=user.access_token,
                )
                try:
                    snap = gen.generate_snapshot(
                        end_date=end_dt,
                        persist_candle=False,
                        persist_snapshot=False,
                    )
                except TokenException:
                    logger.critical(
                        "Invalid credentials during snapshot gen for %s",
                        user.userid,
                        exc_info=True,
                    )
                    flash("Invalid API key or token. Please log in again.", "error")
                    return redirect(url_for("main.logout"))

                if not snap:
                    logger.warning("No snapshot for %s @ %s", symbol.symbol, end_dt)
                    flash(
                        f"No data for {symbol.symbol} at {end_dt:%Y-%m-%d %H:%M}",
                        "warning",
                    )
                else:
                    result = snap.to_db_dict()

    return render_template(
        "dash_snapshot.html",
        symbols=symbols,
        frequencies=SNAPSHOT_CONFIG.single_frequencies,
        result=result,
        nav_section="admin",
        active="admin",
        page="snapshot",
    )