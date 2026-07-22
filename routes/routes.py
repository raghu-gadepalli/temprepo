# routes.py

import json
import logging
from datetime import datetime, timedelta, time
import re
from functools import wraps

from flask import (
    Blueprint, flash, jsonify, redirect, render_template,
    request, send_from_directory, session, url_for
)

from config import AppConfig
from schemas.alert import AlertSchema
from schemas.user import UserSchema
from utils.datetime_utils import now_ist, parse_iso, IST

from database.database import get_trades_db

logger = logging.getLogger(__name__)
main_bp = Blueprint("main", __name__)


# =============================================================================
# AUTH HELPERS
# =============================================================================

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "userid" not in session:
            return redirect(url_for("main.login"))
        return f(*args, **kwargs)
    return decorated_function


# =============================================================================
# SESSION EXPIRY
# =============================================================================

@main_bp.before_app_request
def _enforce_session_expiry():
    if not request.endpoint:
        return

    if request.endpoint.startswith((
        "static",
        "main.login",
        "main.register",
        "main.favicon",
    )):
        return

    if not session.get("userid"):
        return

    exp_s = session.get("expires_at")
    if not exp_s:
        session.clear()
        return redirect(url_for("main.login"))

    try:
        exp_dt = parse_iso(exp_s)
        now_dt = now_ist()
        if now_dt > exp_dt:
            session.clear()
            return redirect(url_for("main.login"))
    except Exception:
        session.clear()
        return redirect(url_for("main.login"))


# =============================================================================
# STATIC
# =============================================================================

@main_bp.route("/favicon.ico")
def favicon():
    return send_from_directory("static/images", "favicon.ico", mimetype="image/x-icon")


# =============================================================================
# LOGIN
# =============================================================================

@main_bp.route("/", methods=["GET", "POST"])
def login():
    def render_login():
        return render_template("login.html")

    if request.method == "GET":
        logger.debug("LOGIN GET: render")
        return render_login()

    login_mode = (request.form.get("login_mode") or "userid").strip().lower()
    userid = (request.form.get("userid") or "").strip()
    mobile = (request.form.get("mobile") or "").strip()
    password = (request.form.get("password") or "")

    logger.info(
        "LOGIN POST: mode=%s userid='%s' mobile='%s' len(password)=%s",
        login_mode, userid, mobile, len(password)
    )

    if not password:
        logger.warning("LOGIN FAIL: missing password (mode=%s, userid=%s, mobile=%s)", login_mode, userid, mobile)
        flash("Password is required.", "error")
        return render_login()

    if login_mode == "phone":
        if not re.fullmatch(r"\d{10}", mobile):
            logger.warning("LOGIN FAIL: bad phone format '%s'", mobile)
            flash("Please enter a valid 10-digit phone number.", "error")
            return render_login()
        user = UserSchema.fetch_user_by_mobile(mobile)
        logger.debug("LOGIN: fetch by mobile => %s", "HIT" if user else "MISS")
    else:
        if not userid:
            logger.warning("LOGIN FAIL: missing userid")
            flash("User ID is required.", "error")
            return render_login()
        user = UserSchema.fetch_user(userid)
        logger.debug("LOGIN: fetch by userid => %s", "HIT" if user else "MISS")

    if not user:
        logger.warning("LOGIN FAIL: user not found (mode=%s, userid=%s, mobile=%s)", login_mode, userid, mobile)
        flash("Invalid User ID or password.", "error")
        return render_login()

    pw_ok = ((user.password or "").strip() == (password or "").strip())
    logger.debug(
        "LOGIN: password_match=%s stored_len=%d input_len=%d",
        pw_ok, len(user.password or ""), len(password)
    )
    if not pw_ok:
        logger.warning("LOGIN FAIL: bad password (userid=%s)", user.userid)
        flash("Invalid User ID or password.", "error")
        return render_login()

    if not user.active:
        logger.warning("LOGIN FAIL: inactive user (userid=%s)", user.userid)
        flash("User account is inactive.", "error")
        return render_login()

    session.clear()
    session.update({
        "userid": user.userid,
        "name": user.name,
        "email": user.email,
        "apikey": user.apikey,
        "secretkey": user.secretkey,
        "broker_login": bool(user.broker_login),
        "broker_name": (user.broker_name or ""),
        "active": user.active,
    })

    session.permanent = True
    now_ist_dt = now_ist()

    demo_user = AppConfig.DEMO_USER.strip()
    demo_hours = int(AppConfig.DEMO_SESSION_HOURS)

    if user.userid == demo_user:
        ttl = timedelta(hours=demo_hours)
        expires_at = now_ist_dt + ttl
        session["expires_at"] = expires_at.isoformat()
        session["is_demo"] = True
    else:
        eod_ist = datetime.combine(now_ist_dt.date(), time(23, 59, 59), tzinfo=IST)
        session["expires_at"] = eod_ist.isoformat()
        session["is_demo"] = False

    stocks_str = (user.stocks or "")
    session["filter_symbols"] = [s.strip() for s in stocks_str.split(",") if s.strip()]

    instr = []
    if user.equity:
        instr.append("EQ")
    if user.futures:
        instr.append("FUT")
    if user.options:
        instr.extend(["CE", "PE"])
    session["filter_instrument"] = instr

    try:
        update_fields = {"logged_time": now_ist(), "logged_in": 1}
        if (user.broker_name or "").lower() == "zerodha":
            update_fields["access_token"] = ""
        UserSchema.update_user(user.userid, update_fields)
        logger.debug("LOGIN: login stamp updated (userid=%s)", user.userid)
    except Exception as e:
        logger.error("LOGIN: failed updating login status (userid=%s): %s", user.userid, e)

    if user.broker_login and (user.broker_name or "").lower() == "zerodha":
        logger.info("LOGIN OK: redirect zero.login (userid=%s)", user.userid)
        return redirect(url_for("zero.login"))

    logger.info("LOGIN OK: redirect main.dashboard_home (userid=%s)", user.userid)
    return redirect(url_for("main.dashboard_home"))


# =============================================================================
# DASHBOARD LANDING
# =============================================================================

@main_bp.route("/dashboard")
@login_required
def dashboard_home():
    return render_template(
        "dashboard.html",
        active="dashboard",
        nav_section="dashboard",
    )


# =============================================================================
# COMMON PAGES
# =============================================================================

@main_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html", active="register")

    name = (request.form.get("name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    email = (request.form.get("email") or "").strip()
    note = (request.form.get("note") or "").strip()
    agree = request.form.get("agree") == "1"

    errs = []
    if not name:
        errs.append("Name is required.")
    if not email:
        errs.append("Email is required.")
    if not phone or not phone.isdigit() or len(phone) != 10:
        errs.append("Phone must be a 10-digit number.")
    if not agree:
        errs.append("You must agree to be contacted.")

    if errs:
        for e in errs:
            flash(e, "error")
        return render_template(
            "register.html",
            active="register",
            name=name,
            phone=phone,
            email=email,
            note=note
        )

    payload = {
        "kind": "REGISTRATION",
        "user": {
            "userid": session.get("userid"),
            "name": name,
            "email": email,
            "phone": phone,
        },
        "note": note,
        "page": request.referrer,
        "ua": request.headers.get("User-Agent"),
    }

    try:
        rec = AlertSchema.create_alert({
            "message": json.dumps(payload, ensure_ascii=False),
            "etime": now_ist()
        })
        flash(f"Request received  well contact you soon. Ref #{rec.id}", "success")
        return redirect(url_for("main.login"))
    except Exception as e:
        logger.exception("Failed to store registration: %s", e)
        flash("Sorry, we couldn't save your request. Please try again.", "error")
        return render_template(
            "register.html",
            active="register",
            name=name,
            phone=phone,
            email=email,
            note=note
        )


@main_bp.route("/feedback", methods=["GET", "POST"])
@login_required
def feedback():
    if request.method == "GET":
        return render_template(
            "feedback.html",
            active="feedback",
            nav_section="dashboard",
        )

    message = (request.form.get("message") or "").strip()
    if not message:
        flash("Please enter your feedback.", "error")
        return render_template(
            "feedback.html",
            active="feedback",
            message=message,
            nav_section="dashboard",
        )

    payload = {
        "kind": "FEEDBACK",
        "user": {
            "userid": session.get("userid"),
            "name": session.get("name"),
            "email": session.get("email"),
        },
        "message": message,
        "page": request.referrer,
        "ua": request.headers.get("User-Agent"),
    }

    try:
        rec = AlertSchema.create_alert({
            "message": json.dumps(payload, ensure_ascii=False),
            "etime": now_ist()
        })
        flash(f"Thanks! Feedback submitted. Ref #{rec.id}", "success")
        return redirect(url_for("main.dashboard_home"))
    except Exception as e:
        logger.exception("Failed to store feedback: %s", e)
        flash("Sorry, we couldn't save your feedback. Please try again.", "error")
        return render_template(
            "feedback.html",
            active="feedback",
            message=message,
            nav_section="dashboard",
        )


@main_bp.route("/notifications")
@login_required
def notifications():
    return jsonify({"status": "success", "message": "Use /notifications/data"}), 200


@main_bp.route("/notifications/data")
@login_required
def notifications_data():
    """
    Returns latest alerts for the bell dropdown.
    Shape expected by default.js:
      [{id, etime, message, processed}, ...]
    """
    from models.trade_models import Alert

    with get_trades_db() as db:
        rows = (
            db.query(Alert)
            .order_by(Alert.etime.desc())
            .limit(10)
            .all()
        )

    data = []
    for a in rows:
        data.append({
            "id": a.id,
            "etime": a.etime.strftime("%d-%m-%Y %H:%M") if a.etime else "",
            "message": a.message,
            "processed": int(a.processed or 0),
        })

    return jsonify({"status": "success", "data": data})


# =============================================================================
# LOGOUT
# =============================================================================

@main_bp.route("/logout")
def logout():
    userid = session.get("userid")
    if userid:
        try:
            user = UserSchema.fetch_user(userid)
            if user:
                UserSchema.update_user(userid, {
                    "access_token": "",
                    "logged_time": None,
                    "logged_in": 0
                })
        except Exception as e:
            logger.error("Error during logout for user %s: %s", userid, e)
    session.clear()
    return redirect(url_for("main.login"))


# =============================================================================
# NO CACHE
# =============================================================================

@main_bp.after_app_request
def add_no_cache_headers(resp):
    if request.path.startswith("/static/"):
        return resp
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["Vary"] = "Cookie"
    return resp