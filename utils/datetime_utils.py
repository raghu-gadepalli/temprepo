# utils/datetime_utils.py

import logging
from datetime import date, datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------
# Timezone helpers
# ----------------------------------------------------------------

# Our canonical India zone
IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    """
    Return the current time with tzinfo=IST.
    """
    return datetime.now(IST)


def now_ist_naive() -> datetime:
    """
    Return current IST time as naive datetime.
    Use this when DB/application stores naive IST timestamps.
    """
    return now_ist().replace(tzinfo=None)


def to_tz(dt: datetime, tz: ZoneInfo) -> datetime:
    """
    Convert a datetime (aware or naive) to the given tz.
    If dt is naive, assume it's already in that tz.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def to_ist_naive(value: Any) -> Optional[datetime]:
    """
    Normalize a datetime-like value to naive IST.

    The application stores DB DateTime values as naive IST, while replay and
    snapshot payloads can carry timezone-aware datetimes/ISO strings. Use this
    before comparing or doing arithmetic across DB and snapshot timestamps.

    Returns None when the value is empty or cannot be parsed.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            logger.debug("to_ist_naive: unable to parse datetime value %r", value)
            return None

    try:
        if dt.tzinfo is not None:
            return to_tz(dt, IST).replace(tzinfo=None)
        return dt.replace(tzinfo=None)
    except Exception:
        logger.debug("to_ist_naive: unable to normalize datetime value %r", value)
        return None


def parse_iso(dt_str: str) -> datetime:
    """
    Parse an ISO-8601 string into a tz-aware datetime.

    Example:
    "2025-05-10T09:16:00+05:30"

    If parsed datetime is naive, IST will be attached.
    """
    try:
        dt = datetime.fromisoformat(dt_str)
    except Exception as e:
        logger.error("parse_iso: invalid format %r: %s", dt_str, e)
        raise

    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)

    return dt.astimezone(IST)


# ----------------------------------------------------------------
# Business Time Helpers (NEW)
# ----------------------------------------------------------------

def business_now() -> datetime:
    """
    Central source of current business time.

    Currently identical to now_ist(), but in future
    can be overridden for replay/backtest/simulation modes.
    """
    return now_ist()


def business_now_naive() -> datetime:
    """
    Central business time returned as naive IST datetime.
    Useful for DB timestamps stored as naive IST.
    """
    return business_now().replace(tzinfo=None)


def business_date() -> date:
    """
    Central source of business date.
    """
    return business_now().date()


# ----------------------------------------------------------------
# Business-day helpers (existing utilities)
# ----------------------------------------------------------------

def ensure_aware(dt: datetime, tz: ZoneInfo = ZoneInfo("UTC")) -> datetime:
    """
    Ensure dt is timezone-aware. If naive, set tzinfo=tz.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt


def subtract_weekdays(start_date: datetime, num_weekdays: int) -> datetime:
    """
    Subtract N business days (Mon–Fri) from start_date.
    """
    current = start_date
    remaining = num_weekdays

    while remaining > 0:
        current -= timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1

    return current


# ----------------------------------------------------------------
# F&O expiry helpers
# ----------------------------------------------------------------

def current_fo_expiry() -> date:
    """Return the configured front F&O expiry.

    We intentionally do not calculate monthly expiry or rollover from today's
    date. The operational process updates FoConfig.FRONT_EXPIRY once a month,
    and backtests consume F&O evidence embedded in snapshots rather than
    simulating F&O contract rollover.
    """
    try:
        from config import FoConfig
        exp = FoConfig.FRONT_EXPIRY
        if not exp:
            raise ValueError("FoConfig.FRONT_EXPIRY is empty")
        if isinstance(exp, datetime):
            return exp.date()
        return exp
    except Exception:
        logger.exception("Unable to read FoConfig.FRONT_EXPIRY")
        raise


def fo_expiry_cutoff() -> date:
    """Backward-compatible alias for runtime front F&O expiry.

    New code should call current_fo_expiry(). This alias remains so older
    callers do not reintroduce date-based rollover logic.
    """
    return current_fo_expiry()


def fo_load_expiry_count() -> int:
    """Number of expiries monthly generator should load into symbols."""
    try:
        from config import FoConfig
        return max(1, int(FoConfig.LOAD_EXPIRY_COUNT or 1))
    except Exception:
        logger.exception("Unable to read FoConfig.LOAD_EXPIRY_COUNT; using 1")
        return 1
