import logging
from flask import Blueprint, jsonify, redirect, request, session, url_for
from kiteconnect import KiteConnect

from routes.routes import login_required
from utils.datetime_utils import now_ist
from database.database import get_trades_db
from models.trade_models import User
from schemas.user import UserSchema
from utils.access import get_current_userid, is_operator
from utils.account_scope import broker_users_for_actor, userids as scope_userids
from services.broker.reconcile_helper import (
    get_kite_client,
    invalidate_user_session,
    sync_user_funds,
)

logger = logging.getLogger(__name__)
zero_bp = Blueprint("zero", __name__, url_prefix="/zerodha")


# --------------------------------------------------
# Helpers
# --------------------------------------------------

def transform_utilized_margin(utilized_dict):
    mapping = {
        "debits": "Debits",
        "delivery": "Delivery",
        "equity": "Equity",
        "exposure": "Exposure",
        "holding_sales": "Holding Sales",
        "liquid_collateral": "Liquid Collateral",
        "m2m_realised": "M2M Realised",
        "m2m_unrealised": "M2M Unrealised",
        "option_premium": "Option Premium",
        "payout": "Payout",
        "span": "Span",
        "stock_collateral": "Stock Collateral",
        "turnover": "Turnover",
    }
    return {mapping.get(key, key): value for key, value in utilized_dict.items()}


def _safe_str(x, default=""):
    try:
        return str(x) if x is not None else default
    except Exception:
        return default


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


def _requested_userids_from_query():
    """
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


def _visible_real_userids_for_actor(actor=None):
    """REAL broker-enabled users only; VIRTUAL users never reach Zerodha."""
    actor = _safe_str(actor or get_current_userid(), "").strip()
    if not actor:
        return []

    try:
        return scope_userids(broker_users_for_actor(actor))
    except Exception:
        logger.exception("Failed fetching REAL users for actor=%s", actor)
        return []


def _resolve_target_userid(requested_userid=None):
    """
    Returns:
      (userid, error)

    Behavior:
    - Non-operator → always self
    - Operator:
        - no userid → self
        - valid userid → that userid
        - invalid userid → error
    """
    actor = _safe_str(get_current_userid(), "").strip()
    if not actor:
        return None, "not_authenticated"

    allowed_list = _visible_real_userids_for_actor(actor)
    allowed = {u.upper() for u in allowed_list}

    if not is_operator(actor):
        if actor.upper() in allowed:
            return actor, None
        return None, "not_real_broker_user"

    requested = _safe_str(requested_userid, "").strip()
    if not requested:
        if actor.upper() in allowed:
            return actor, None
        return (allowed_list[0], None) if allowed_list else (None, "no_real_broker_users")

    if requested.upper() in allowed:
        return requested, None

    return None, "userid_not_allowed"


def _resolve_target_userids(requested_userids=None):
    """
    Returns:
      (list[str], error)

    Behavior:
    - Non-operator → always [self]
    - Operator:
        - no userid/userids passed → all visible REAL users
        - explicit valid subset → that subset
        - any invalid requested userid → error
    """
    actor = _safe_str(get_current_userid(), "").strip()
    if not actor:
        return [], "not_authenticated"

    allowed = _visible_real_userids_for_actor(actor)
    allowed_set = {u.upper() for u in allowed}

    if not is_operator(actor):
        if actor.upper() in allowed_set:
            return [actor], None
        return [], "not_real_broker_user"


    requested_userids = _normalize_userids(requested_userids)
    if not requested_userids:
        return allowed, None

    invalid = [u for u in requested_userids if u.upper() not in allowed_set]
    if invalid:
        return [], "userid_not_allowed"

    return requested_userids, None


def _invalidate_zerodha_session(userid):
    """
    Effective logout for a user when broker session is no longer valid.
    Thin wrapper over shared helper.
    """
    userid = _safe_str(userid, "").strip()
    if not userid:
        return

    user = UserSchema.fetch_user(userid)
    if not user:
        return

    invalidate_user_session(user)


def get_kite_client_by_userid(userid):
    """
    Thin wrapper over shared helper.

    Returns:
      (kite_client, error)
    """
    userid = _safe_str(userid, "").strip()
    if not userid:
        return None, "invalid_userid"

    user = UserSchema.fetch_user(userid)
    if not user:
        return None, "user_not_found"

    return get_kite_client(user)


def _funds_data_from_record(rec):
    """
    Preserve zero.py response shape while sourcing persisted values from schema record.
    """
    if not rec:
        return {}

    return {
        "Total Balance": float(rec.net_balance) if rec.net_balance is not None else 0,
        "Available Margin": float(rec.available_cash) if rec.available_cash is not None else 0,
        "Intraday Payin": 0,
        "Live Balance": float(rec.live_balance) if rec.live_balance is not None else 0,
        "Opening Balance": float(rec.opening_balance) if rec.opening_balance is not None else 0,
        "Collateral": float(rec.collateral) if rec.collateral is not None else 0,
        "Adhoc Margin": 0,
        "Utilized Margin": transform_utilized_margin({
            "debits": float(rec.utilised_margin) if rec.utilised_margin is not None else 0,
            "span": float(rec.span_margin) if rec.span_margin is not None else 0,
            "exposure": float(rec.exposure_margin) if rec.exposure_margin is not None else 0,
            "option_premium": float(rec.option_premium) if rec.option_premium is not None else 0,
            "m2m_realised": float(rec.m2m_realised) if rec.m2m_realised is not None else 0,
            "m2m_unrealised": float(rec.m2m_unrealised) if rec.m2m_unrealised is not None else 0,
        }),
    }


def fetch_zerodha_profile_by_userid(userid):
    """
    Low-level profile refresh helper for one userid.
    Returns:
      {
        "status": "success" | "error",
        "userid": ...,
        "data": ...,
        "message": ...
      }
    """
    userid = _safe_str(userid, "").strip()
    kite, kite_err = get_kite_client_by_userid(userid)

    if kite_err:
        return {
            "status": "error",
            "userid": userid,
            "message": kite_err,
        }

    try:
        profile = kite.profile()
        return {
            "status": "success",
            "userid": userid,
            "data": profile,
        }
    except Exception:
        logger.exception("Error fetching profile for userid=%s", userid)
        return {
            "status": "error",
            "userid": userid,
            "message": "profile_fetch_failed",
        }


def fetch_zerodha_funds_by_userid(userid, invalidate_on_failure=True):
    """
    Thin wrapper over shared reconcile helper.

    Returns:
      {
        "status": "success" | "error",
        "userid": ...,
        "data": ...,
        "message": ...
      }
    """
    userid = _safe_str(userid, "").strip()
    if not userid:
        return {
            "status": "error",
            "userid": userid,
            "message": "invalid_userid",
        }

    user = UserSchema.fetch_user(userid)
    if not user:
        if invalidate_on_failure:
            _invalidate_zerodha_session(userid)
        return {
            "status": "error",
            "userid": userid,
            "message": "user_not_found",
        }

    result = sync_user_funds(user, invalidate_on_failure=invalidate_on_failure,write_history=False)

    if result.get("status") != "success":
        return {
            "status": "error",
            "userid": userid,
            "message": result.get("message") or "funds_fetch_failed",
        }

    rec = result.get("record")
    return {
        "status": "success",
        "userid": userid,
        "data": _funds_data_from_record(rec),
    }


# --------------------------------------------------
# LOGIN FLOW (unchanged)
# --------------------------------------------------

@zero_bp.route("/login")
@login_required
def login():
    apikey = session.get("apikey")
    if not apikey:
        return redirect(url_for("main.login"))

    kite = KiteConnect(api_key=apikey)
    return redirect(kite.login_url())


@zero_bp.route("/callback")
@login_required
def callback():
    request_token = request.args.get("request_token")
    if not request_token:
        return redirect(url_for("main.login"))

    api_key = session.get("apikey")
    api_secret = session.get("secretkey")
    user_id = session.get("userid")

    kite = KiteConnect(api_key=api_key)

    try:
        data = kite.generate_session(request_token, api_secret=api_secret)
        access_token = data.get("access_token")

        session["access_token"] = access_token

        with get_trades_db() as db:
            user = db.query(User).filter(User.userid == user_id).one_or_none()
            if user:
                user.access_token = access_token
                user.logged_time = now_ist()
                db.commit()

        return redirect(url_for("main.dashboard_home"))

    except Exception:
        logger.exception("Zerodha callback failed")
        return redirect(url_for("main.login"))


# --------------------------------------------------
# PROFILE
# --------------------------------------------------

@zero_bp.route("/profile")
@login_required
def zero_profile():
    requested_userid = request.args.get("userid")
    target_userid, err = _resolve_target_userid(requested_userid)

    if err == "not_authenticated":
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    if err == "userid_not_allowed":
        return jsonify({
            "status": "error",
            "message": "userid_not_allowed",
            "userid": requested_userid,
        }), 403

    if err in ("not_real_broker_user", "no_real_broker_users"):
        return jsonify({"status": "error", "message": err}), 400

    if err:
        return jsonify({"status": "error", "message": err}), 500

    result = fetch_zerodha_profile_by_userid(target_userid)

    if result.get("status") == "error" and result.get("message") == "not_logged_into_zerodha":
        return jsonify(result), 200

    if result.get("status") == "error":
        return jsonify(result), 500

    return jsonify(result), 200


# --------------------------------------------------
# FUNDS
# --------------------------------------------------

@zero_bp.route("/funds")
@login_required
def zero_funds():
    """
    Broker-facing refresh endpoint.

    Non-operator:
      - always self

    Operator:
      - no userid/userids passed -> refresh all visible REAL users
      - userid/userids passed -> refresh that subset

    Returns:
      {
        "status": "success",
        "data": [...],
        "refreshed_count": N,
        "failed_count": M
      }
    """
    requested_userids = _requested_userids_from_query()
    target_userids, err = _resolve_target_userids(requested_userids)

    if err == "not_authenticated":
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    if err == "userid_not_allowed":
        return jsonify({
            "status": "error",
            "message": "userid_not_allowed",
            "userids": requested_userids,
        }), 403

    if err in ("not_real_broker_user", "no_real_broker_users"):
        return jsonify({"status": "error", "message": err}), 400

    if err:
        return jsonify({"status": "error", "message": err}), 500

    rows = []
    refreshed_count = 0
    failed_count = 0

    for userid in target_userids:
        result = fetch_zerodha_funds_by_userid(userid, invalidate_on_failure=True)
        rows.append(result)

        if result.get("status") == "success":
            refreshed_count += 1
        else:
            failed_count += 1

    return jsonify({
        "status": "success",
        "data": rows,
        "refreshed_count": refreshed_count,
        "failed_count": failed_count,
    }), 200


# --------------------------------------------------
# POSTBACK
# --------------------------------------------------

@zero_bp.route("/postback", methods=["POST"])
def postback():
    data = request.get_json(force=True, silent=True) or {}
    logger.info("Zerodha postback received: %s", data)
    return jsonify({"status": "success"}), 200