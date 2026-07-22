from datetime import datetime
import logging

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from routes.routes import login_required
from routes.zero import fetch_zerodha_funds_by_userid
from schemas.user import UserSchema
from schemas.user_funds import UserFundsSchema
from utils.access import get_current_userid, is_operator
from utils.account_scope import (
    managed_users_for_actor,
    filter_requested_users,
)
from utils.datetime_utils import IST

logger = logging.getLogger(__name__)
user_bp = Blueprint("user", __name__, url_prefix="/user")


# =============================================================================
# HELPERS
# =============================================================================

def _fmt_ist(dt):
    if not dt:
        return ""
    try:
        dt_ist = dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)
        return dt_ist.strftime("%d-%m-%Y %H:%M")
    except Exception:
        return ""


def _safe_str(x, default=""):
    try:
        return str(x) if x is not None else default
    except Exception:
        return default


def _safe_int(x, default=0):
    try:
        return int(x) if x is not None else default
    except Exception:
        return default


def _safe_float(x, default=0.0):
    try:
        return float(x) if x is not None else default
    except Exception:
        return default


def _safe_float_or_none(x):
    try:
        return float(x) if x is not None else None
    except Exception:
        return None


def _normalize_userids(raw):
    out = []
    seen = set()

    if raw is None:
        return out

    if isinstance(raw, str):
        raw = [raw]

    if not isinstance(raw, (list, tuple)):
        return out

    for item in raw:
        uid = _safe_str(item, "").strip()
        if not uid:
            continue
        key = uid.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(uid)

    return out


def _requested_userids_from_query() -> list[str]:
    """
    Optional narrowing for GET/data routes.
    Accepts:
      ?userids=A&userids=B
      ?userids=A,B
      ?userid=A
    """
    requested = request.args.getlist("userids")
    if not requested:
        csv_userids = _safe_str(request.args.get("userids"), "").strip()
        if csv_userids:
            requested = [u.strip() for u in csv_userids.split(",") if u.strip()]

    userids = _normalize_userids(requested)

    if not userids:
        single = _safe_str(request.args.get("userid"), "").strip()
        if single:
            userids = [single]

    return userids


def _managed_users_for_actor(actor: str | None = None):
    """Normal user: self. Operator/admin: all active REAL and VIRTUAL users."""
    actor = _safe_str(actor or get_current_userid(), "").strip()
    return managed_users_for_actor(actor) if actor else []


def _visible_users_for_actor(actor: str | None = None, requested_userids: list[str] | None = None):
    actor = _safe_str(actor or get_current_userid(), "").strip()
    if not actor:
        return []

    users = _managed_users_for_actor(actor)
    if not is_operator(actor):
        return users
    return filter_requested_users(users, requested_userids)


def _resolve_target_userid(actor: str | None = None) -> str | None:
    actor = _safe_str(actor or get_current_userid(), "").strip()
    if not actor:
        return None

    if not is_operator(actor):
        return actor

    requested = _safe_str(
        request.values.get("userid") or request.args.get("userid"),
        ""
    ).strip()

    users = _managed_users_for_actor(actor)
    allowed = {_safe_str(getattr(u, "userid", ""), "").upper() for u in users}

    if requested and requested.upper() in allowed:
        return requested
    if actor.upper() in allowed:
        return actor
    return _safe_str(getattr(users[0], "userid", ""), actor) if users else actor


def _build_user_picker(actor: str | None = None, selected_userid: str | None = None):
    actor = _safe_str(actor or get_current_userid(), "").strip()
    selected_userid = _safe_str(selected_userid, "").strip()

    users = _managed_users_for_actor(actor)
    operator = is_operator(actor)

    data = []
    for user in users:
        userid = _safe_str(getattr(user, "userid", ""), "").strip()
        if not userid:
            continue
        data.append({
            "userid": userid,
            "selected": userid == selected_userid,
            "disabled": not operator,
        })

    return data


def _is_real_broker_user(user_obj) -> bool:
    return (
        _safe_int(getattr(user_obj, "broker_login", 0), 0) == 1
        and _safe_str(getattr(user_obj, "execution_mode", ""), "").upper() == "REAL"
        and _safe_str(getattr(user_obj, "broker_name", ""), "").strip() != ""
    )


def _profile_row(user_obj):
    return {
        "userid": user_obj.userid,
        "name": user_obj.name,
        "email": user_obj.email,
        "mobile": user_obj.mobile,
        "broker_login": _safe_int(user_obj.broker_login, 0),
        "broker_name": _safe_str(user_obj.broker_name, "").upper(),
        "apikey": _safe_str(getattr(user_obj, "apikey", ""), ""),
        "secretkey": _safe_str(getattr(user_obj, "secretkey", ""), ""),
        "intraday_only": _safe_int(user_obj.intraday_only, 0),
        "stocks": user_obj.stocks or "",
        "equity": _safe_int(user_obj.equity, 0),
        "futures": _safe_int(user_obj.futures, 0),
        "options": _safe_int(user_obj.options, 0),
        "execution_mode": user_obj.execution_mode or "",
        "autotrade": _safe_int(user_obj.autotrade, 0),
        "active": _safe_int(user_obj.active, 0),
        "logged_in": _safe_int(user_obj.logged_in, 0),
        "logged_time": _fmt_ist(user_obj.logged_time),
        "status": "LOGGED_IN" if _safe_int(user_obj.logged_in, 0) == 1 else "NOT_LOGGED_IN",
    }


def _preferences_row(user_obj):
    """
    User Preferences now manage only user/broker/execution flags.

    Legacy target/stop-loss/trailing preferences were removed in Phase-1.
    Trade management is controlled by lifecycle config and per-trade
    trade_management JSON.
    """
    return {
        "userid": user_obj.userid,
        "name": user_obj.name,
        "email": user_obj.email,
        "mobile": user_obj.mobile,
        "broker_login": _safe_int(user_obj.broker_login, 0),
        "broker_name": _safe_str(user_obj.broker_name, "").upper(),
        "equity": _safe_int(user_obj.equity, 0),
        "futures": _safe_int(user_obj.futures, 0),
        "options": _safe_int(user_obj.options, 0),
        "execution_mode": user_obj.execution_mode or "",
        "autotrade": _safe_int(user_obj.autotrade, 0),
        "intraday_only": _safe_int(user_obj.intraday_only, 0),
        "stocks": user_obj.stocks or "",
        "trade_management_source": "TRADE_CONFIG",
    }

def _default_funds_row(user_obj):
    logged_in = _safe_int(user_obj.logged_in, 0)

    return {
        "userid": user_obj.userid,
        "name": user_obj.name,
        "broker_name": _safe_str(user_obj.broker_name, "").upper(),
        "logged_in": logged_in,
        "logged_time": _fmt_ist(user_obj.logged_time),
        "status": "LOGGED_IN" if logged_in == 1 else "NOT_LOGGED_IN",
        "funds_status": "NOT_REFRESHED" if logged_in == 1 else "NOT_LOGGED_IN",
        "live": False,
        "total_balance": None,
        "available_margin": None,
        "opening_balance": None,
        "live_balance": None,
        "intraday_payin": None,
        "collateral": None,
        "adhoc_margin": None,
        "utilized_margin_total": None,
        "utilized_margin_details": None,
        "polled_at": None,
    }


def _simulated_funds_row(user_obj):
    """
    Deterministic simulated values for non-REAL / non-broker users.
    Stable per userid so values do not jump around across page loads.
    """
    base = sum(ord(c) for c in _safe_str(user_obj.userid, ""))

    total_balance = round(50000 + (base % 50000), 2)
    available_margin = round(total_balance * 0.42, 2)
    opening_balance = round(total_balance * 0.88, 2)
    live_balance = round(total_balance * 0.31, 2)
    collateral = round(total_balance * 0.08, 2)
    adhoc_margin = round(total_balance * 0.01, 2)

    span_margin = round(total_balance * 0.06, 2)
    exposure_margin = round(total_balance * 0.03, 2)
    option_premium = round(total_balance * 0.02, 2)
    m2m_realised = round((base % 3000) / 10.0, 2)
    m2m_unrealised = round((base % 2000) / 10.0, 2)

    utilized_total = round(
        span_margin + exposure_margin + option_premium + m2m_realised + m2m_unrealised,
        2,
    )

    logged_in = _safe_int(getattr(user_obj, "logged_in", 0), 0)

    return {
        "userid": user_obj.userid,
        "name": user_obj.name,
        "broker_name": _safe_str(user_obj.broker_name, "").upper() or "SIMULATED",
        "logged_in": logged_in,
        "logged_time": _fmt_ist(user_obj.logged_time),
        "status": "LOGGED_IN" if logged_in == 1 else "NOT_LOGGED_IN",
        "funds_status": "SIMULATED",
        "live": False,
        "total_balance": total_balance,
        "available_margin": available_margin,
        "opening_balance": opening_balance,
        "live_balance": live_balance,
        "intraday_payin": 0.0,
        "collateral": collateral,
        "adhoc_margin": adhoc_margin,
        "utilized_margin_total": utilized_total,
        "utilized_margin_details": {
            "Span": span_margin,
            "Exposure": exposure_margin,
            "Option Premium": option_premium,
            "M2M Realised": m2m_realised,
            "M2M Unrealised": m2m_unrealised,
        },
        "polled_at": None,
    }


def _funds_row_from_store(user_obj):
    """
    For REAL broker-enabled users:
      - read latest persisted oms_funds row if available
      - otherwise return default row

    For non-REAL / non-broker users:
      - return simulated display row
    """
    if not _is_real_broker_user(user_obj):
        return _simulated_funds_row(user_obj)

    row = _default_funds_row(user_obj)

    try:
        rec = UserFundsSchema.fetch_latest_for_user(user_obj.userid)
    except Exception:
        logger.exception("Failed fetching persisted funds for userid=%s", user_obj.userid)
        rec = None

    if not rec:
        return row

    ui = rec.to_ui_dict()

    polled_at_raw = ui.get("polled_at")
    polled_at_dt = None
    if polled_at_raw:
        try:
            polled_at_dt = datetime.fromisoformat(polled_at_raw)
        except Exception:
            polled_at_dt = None

    has_polled_data = bool(polled_at_dt)
    is_logged_in = _safe_int(row.get("logged_in"), 0) == 1
    polled_at_fmt = _fmt_ist(polled_at_dt)

    row.update({
        "total_balance": _safe_float_or_none(ui.get("total_balance")),
        "available_margin": _safe_float_or_none(ui.get("available_margin")),
        "opening_balance": _safe_float_or_none(ui.get("opening_balance")),
        "live_balance": _safe_float_or_none(ui.get("live_balance")),
        "intraday_payin": _safe_float_or_none(ui.get("intraday_payin")),
        "collateral": _safe_float_or_none(ui.get("collateral")),
        "adhoc_margin": _safe_float_or_none(ui.get("adhoc_margin")),
        "utilized_margin_total": _safe_float_or_none(ui.get("utilized_margin_total")),
        "utilized_margin_details": ui.get("utilized_margin_details"),

        # keep actual poll time separately
        "polled_at": polled_at_fmt,

        # show poll time in the existing Logged Time column for funds page
        "logged_time": polled_at_fmt if polled_at_fmt else row.get("logged_time", ""),

        "funds_status": (
            "LIVE" if (is_logged_in and has_polled_data)
            else "NOT_REFRESHED" if is_logged_in
            else "NOT_LOGGED_IN"
        ),
        "live": bool(is_logged_in and has_polled_data),
    })

    return row

# =============================================================================
# PAGE ROUTES (SHELL ONLY; DATA COMES FROM /data)
# =============================================================================

@user_bp.route("/profile")
@login_required
def user_profile():
    actor = _safe_str(get_current_userid(), "").strip()
    target_userid = _resolve_target_userid(actor)

    return render_template(
        "dash_userprofile.html",
        user={},
        userid=actor,
        target_userid=target_userid,
        user_options=_build_user_picker(actor, target_userid),
        active="profile",
        nav_section="dashboard",
    )


@user_bp.route("/funds")
@login_required
def user_funds():
    actor = _safe_str(get_current_userid(), "").strip()
    target_userid = _resolve_target_userid(actor)

    return render_template(
        "dash_userfunds.html",
        funds={},
        userid=actor,
        target_userid=target_userid,
        user_options=_build_user_picker(actor, target_userid),
        active="funds",
        nav_section="dashboard",
    )


@user_bp.route("/preferences", methods=["GET", "POST"])
@login_required
def user_preferences():
    actor = _safe_str(get_current_userid(), "").strip()
    target_userid = _resolve_target_userid(actor)

    if request.method == "POST":
        user_obj = UserSchema.fetch_user(target_userid) if target_userid else None
        if not user_obj:
            flash("User not found.", "error")
            return redirect(url_for("user.user_profile"))

        form = request.form

        user_fields = {
            "name", "email", "mobile",
            "broker_login", "broker_name", "apikey", "secretkey", "autotrade",
            "stocks", "equity", "futures", "options", "intraday_only", "execution_mode",
        }

        def as_int01(v: str) -> int:
            v = str(v).strip().lower()
            return 1 if v in ("1", "true", "yes", "on") else 0

        update_data = {}

        for k in user_fields:
            if k not in form:
                continue

            val = (form.get(k) or "").strip()

            if k in {"broker_login", "autotrade", "equity", "futures", "options", "intraday_only"}:
                update_data[k] = as_int01(val)
            elif k == "execution_mode":
                update_data[k] = "REAL" if val.upper() == "REAL" else "VIRTUAL"
            else:
                update_data[k] = val

        if update_data:
            try:
                UserSchema.update_user(target_userid, update_data)
            except Exception as e:
                logger.exception("Failed to update user fields for %s: %s", target_userid, e)
                flash("Could not save profile fields. Please try again.", "error")
                if is_operator(actor):
                    return redirect(url_for("user.user_preferences", userid=target_userid))
                return redirect(url_for("user.user_preferences"))

        flash("Settings saved successfully", "success")
        if is_operator(actor):
            return redirect(url_for("user.user_preferences", userid=target_userid))
        return redirect(url_for("user.user_preferences"))

    return render_template(
        "dash_userpref.html",
        user={},
        settings={},
        userid=actor,
        target_userid=target_userid,
        user_options=_build_user_picker(actor, target_userid),
        active="preferences",
        nav_section="dashboard",
    )


# =============================================================================
# DATA ROUTES
# =============================================================================

@user_bp.route("/profile/data")
@login_required
def user_profile_data():
    actor = _safe_str(get_current_userid(), "").strip()
    if not actor:
        return jsonify({"status": "error", "reason": "not_authenticated"}), 401

    requested_userids = _requested_userids_from_query()
    users = _visible_users_for_actor(actor, requested_userids)

    data = [_profile_row(u) for u in users]
    return jsonify({"status": "success", "data": data})


@user_bp.route("/funds/data")
@login_required
def user_funds_data():
    """
    Default:
      /user/funds/data           -> DB-backed list only

    Optional:
      /user/funds/data?refresh=1 -> refresh via /zerodha/funds first
                                    for REAL broker-enabled visible users,
                                    then return same normalized list shape
                                    from DB/simulated source.
    """
    actor = _safe_str(get_current_userid(), "").strip()
    if not actor:
        return jsonify({"status": "error", "reason": "not_authenticated"}), 401

    requested_userids = _requested_userids_from_query()
    users = _visible_users_for_actor(actor, requested_userids)

    refresh = _safe_int(request.args.get("refresh"), 0) == 1
    refresh_payload = {"status": "success", "data": [], "refreshed_count": 0, "failed_count": 0}

    if refresh:
        refresh_targets = [u.userid for u in users if _is_real_broker_user(u)]

        for userid in refresh_targets:
            try:
                result = fetch_zerodha_funds_by_userid(userid, invalidate_on_failure=True)
            except Exception:
                logger.exception("Funds refresh failed for userid=%s", userid)
                result = {
                    "status": "error",
                    "userid": userid,
                    "message": "refresh_failed",
                }

            refresh_payload["data"].append(result)

            if result.get("status") == "success":
                refresh_payload["refreshed_count"] += 1
            else:
                refresh_payload["failed_count"] += 1

    # Re-fetch after a REAL refresh because Zerodha may update broker session
    # metadata. Managed visibility itself is independent of logged_in.
    users = _visible_users_for_actor(actor, requested_userids)

    data = [_funds_row_from_store(u) for u in users]

    return jsonify({
        "status": "success",
        "data": data,
        "refresh": refresh_payload,
    })


@user_bp.route("/preferences/data")
@login_required
def user_preferences_data():
    actor = _safe_str(get_current_userid(), "").strip()
    if not actor:
        return jsonify({"status": "error", "reason": "not_authenticated"}), 401

    requested_userids = _requested_userids_from_query()
    users = _visible_users_for_actor(actor, requested_userids)

    data = [_preferences_row(u) for u in users]
    return jsonify({"status": "success", "data": data})