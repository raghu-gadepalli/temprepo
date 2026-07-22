# utils/access.py

from flask import session

from config import AppConfig


# ------------------------------
# Session helpers
# ------------------------------
def get_current_userid() -> str:
    return str(session.get("userid") or "").strip()


def is_logged_in() -> bool:
    return bool(get_current_userid())


# ------------------------------
# Roles / capabilities
# ------------------------------
def is_operator(userid: str | None = None) -> bool:
    """Return operational cross-user capability.

    ADMIN inherits OPERATOR capabilities even when the userid is not repeated
    in ``AppConfig.OPERATORS``. Role checks remain independent of database
    account scope and execution mode.
    """
    uid = str(userid or get_current_userid() or "").strip().upper()
    return bool(uid) and (uid in AppConfig.OPERATORS or uid in AppConfig.ADMINS)


def is_admin(userid: str | None = None) -> bool:
    """Return system-administration capability."""
    uid = str(userid or get_current_userid() or "").strip().upper()
    return bool(uid) and uid in AppConfig.ADMINS
