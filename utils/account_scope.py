"""Central account-scope helpers for dashboard, user, and broker routes.

Role, execution mode, broker eligibility, and autotrade eligibility are
independent concerns. Database access remains centralized in ``UserSchema``;
this module only selects the appropriate schema-level account universe and
validates requested userids against that already-authorized universe.
"""

from __future__ import annotations

from typing import Iterable, Optional

from schemas.user import UserSchema
from utils.access import get_current_userid, is_operator


def _normalize_userid(value: object) -> str:
    """Normalize a userid for case-insensitive membership comparisons."""
    return str(value or "").strip().upper()


def _self_user(actor: str) -> list[UserSchema]:
    user = UserSchema.fetch_user(actor)
    return [user] if user else []


def managed_users_for_actor(actor: str | None = None) -> list[UserSchema]:
    """Users visible in managed Orders/Positions/Performance/Profile/Funds.

    Normal users see only themselves. Operators/admins see every active user,
    regardless of REAL/VIRTUAL mode or current broker-login state. Existing
    managed trades must remain visible when a user logs out or a token expires.
    """
    actor_userid = _normalize_userid(actor or get_current_userid())
    if not actor_userid:
        return []
    if not is_operator(actor_userid):
        return _self_user(actor_userid)
    return UserSchema.fetch_managed_users()


def tradeable_users_for_actor(actor: str | None = None) -> list[UserSchema]:
    """Users eligible for current dashboard trade actions.

    Operator/admin actions use active app-logged-in users. A normal signed-in
    user remains scoped to self. This scope is intentionally narrower than the
    managed-view scope above.
    """
    actor_userid = _normalize_userid(actor or get_current_userid())
    if not actor_userid:
        return []
    if not is_operator(actor_userid):
        return _self_user(actor_userid)
    return UserSchema.fetch_tradeable_users()


def broker_users_for_actor(
    actor: str | None = None,
    *,
    logged_in: Optional[int] = None,
) -> list[UserSchema]:
    """REAL, broker-enabled users visible to Zerodha/OMS routes only."""
    actor_userid = _normalize_userid(actor or get_current_userid())
    if not actor_userid:
        return []

    if is_operator(actor_userid):
        return UserSchema.fetch_real_users(logged_in=logged_in)

    users = _self_user(actor_userid)
    if not users:
        return []

    user = users[0]
    is_real = (
        int(getattr(user, "active", 0) or 0) == 1
        and int(getattr(user, "broker_login", 0) or 0) == 1
        and str(getattr(user, "execution_mode", "") or "").strip().upper()
        == "REAL"
    )
    if not is_real:
        return []

    if logged_in is not None:
        user_logged_in = int(getattr(user, "logged_in", 0) or 0)
        if user_logged_in != int(logged_in):
            return []

    return users


def filter_requested_users(
    users: Iterable[UserSchema],
    requested_userids: Iterable[str] | None,
) -> list[UserSchema]:
    """Restrict an authorized user list to requested IDs without widening it.

    Dropdown values are still checked server-side because request data can be
    modified outside the browser UI. Validation is an in-memory membership
    check against the supplied schema result; it does not query the ORM again.
    """
    rows = list(users or [])
    requested = {
        normalized
        for value in (requested_userids or [])
        if (normalized := _normalize_userid(value))
    }
    if not requested:
        return rows

    return [
        user
        for user in rows
        if _normalize_userid(getattr(user, "userid", "")) in requested
    ]


def userids(users: Iterable[UserSchema]) -> list[str]:
    """Return unique userids while preserving schema-result order/casing."""
    output: list[str] = []
    seen: set[str] = set()

    for user in users or []:
        userid = str(getattr(user, "userid", "") or "").strip()
        key = _normalize_userid(userid)
        if not userid or key in seen:
            continue
        seen.add(key)
        output.append(userid)

    return output
