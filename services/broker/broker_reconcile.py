# services/broker/broker_reconcile.py

from __future__ import annotations

import logging
from typing import Dict, Any, List

from configs.broker_config import BROKER_CONFIG
from schemas.user import UserSchema
from services.broker.reconcile_helper import (
    get_reconcile_users,
    sync_user_funds,
    sync_user_positions,
    sync_user_orders,
)

logger = logging.getLogger(__name__)

_recon_extras = BROKER_CONFIG.reconcile.extras

DEFAULT_LIMIT_USERS = _recon_extras.limit_users
INVALIDATE_ON_FAILURE = _recon_extras.invalidate_on_failure


class BrokerReconcileService:
    """
    Broker reconcile service.

    Current scope:
      - funds sync
      - positions sync
      - orders sync

    Design:
      - per-user processing
      - funds, positions, and orders are independent slices
      - one slice failing must not block the others
    """

    def run_once(self, *, limit_users: int = DEFAULT_LIMIT_USERS) -> Dict[str, Any]:
        users = self._fetch_due_users(limit_users=limit_users)
        if not users:
            logger.info("BrokerReconcile: no eligible users")
            return self._empty_stats()

        stats = self._empty_stats()
        stats["users_found"] = len(users)

        for user in users:
            userid = str(getattr(user, "userid", "") or "").strip()
            if not userid:
                continue

            try:
                result = self.reconcile_user_once(
                    user=user,
                    invalidate_on_failure=INVALIDATE_ON_FAILURE,
                )

                stats["users_processed"] += 1

                if result.get("funds_ok"):
                    stats["funds_synced"] += 1

                if result.get("positions_ok"):
                    stats["positions_synced"] += 1

                if result.get("orders_ok"):
                    stats["orders_synced"] += 1

                if result.get("user_ok"):
                    stats["users_succeeded"] += 1
                else:
                    stats["users_failed"] += 1

            except Exception:
                stats["errors"] += 1
                stats["users_failed"] += 1
                logger.exception(
                    "BrokerReconcile: fatal user error userid=%s",
                    userid,
                )

        logger.info(
            "BrokerReconcile: run_once users_found=%s users_processed=%s "
            "users_succeeded=%s users_failed=%s "
            "funds_synced=%s positions_synced=%s orders_synced=%s errors=%s",
            stats["users_found"],
            stats["users_processed"],
            stats["users_succeeded"],
            stats["users_failed"],
            stats["funds_synced"],
            stats["positions_synced"],
            stats["orders_synced"],
            stats["errors"],
        )
        return stats

    def reconcile_user_once(
        self,
        *,
        user: UserSchema,
        invalidate_on_failure: bool = True,
    ) -> Dict[str, Any]:
        """
        Per-user broker sync.

        Funds, positions, and orders are intentionally independent.
        Failure in one must not block the others.

        Returns:
          {
            "userid": str,
            "funds_ok": bool,
            "positions_ok": bool,
            "orders_ok": bool,
            "user_ok": bool,
          }
        """
        userid = str(getattr(user, "userid", "") or "").strip()
        if not userid:
            logger.warning("BrokerReconcile: missing userid in user object")
            return {
                "userid": "",
                "funds_ok": False,
                "positions_ok": False,
                "orders_ok": False,
                "user_ok": False,
            }

        funds_ok = False
        positions_ok = False
        orders_ok = False

        # ----------------------------
        # Funds
        # ----------------------------
        try:
            funds_result = sync_user_funds(
                user=user,
                invalidate_on_failure=invalidate_on_failure,
            )

            funds_status = str(funds_result.get("status") or "").lower()
            funds_message = str(funds_result.get("message") or "").strip()

            if funds_status == "success":
                funds_ok = True
                logger.info(
                    "BrokerReconcile: funds success userid=%s message=%s",
                    userid,
                    funds_message or "funds_synced",
                )
            else:
                logger.warning(
                    "BrokerReconcile: funds failed userid=%s message=%s",
                    userid,
                    funds_message or "unknown_error",
                )

        except Exception:
            logger.exception(
                "BrokerReconcile: funds fatal error userid=%s",
                userid,
            )

        # ----------------------------
        # Positions
        # ----------------------------
        try:
            positions_result = sync_user_positions(
                user=user,
                invalidate_on_failure=invalidate_on_failure,
            )

            positions_status = str(positions_result.get("status") or "").lower()
            positions_message = str(positions_result.get("message") or "").strip()

            if positions_status == "success":
                positions_ok = True
                logger.info(
                    "BrokerReconcile: positions success userid=%s message=%s",
                    userid,
                    positions_message or "positions_synced",
                )
            else:
                logger.warning(
                    "BrokerReconcile: positions failed userid=%s message=%s",
                    userid,
                    positions_message or "unknown_error",
                )

        except Exception:
            logger.exception(
                "BrokerReconcile: positions fatal error userid=%s",
                userid,
            )

        # ----------------------------
        # Orders
        # ----------------------------
        try:
            orders_result = sync_user_orders(
                user=user,
                invalidate_on_failure=invalidate_on_failure,
            )

            orders_status = str(orders_result.get("status") or "").lower()
            orders_message = str(orders_result.get("message") or "").strip()

            if orders_status == "success":
                orders_ok = True
                logger.info(
                    "BrokerReconcile: orders success userid=%s message=%s",
                    userid,
                    orders_message or "orders_synced",
                )
            else:
                logger.warning(
                    "BrokerReconcile: orders failed userid=%s message=%s",
                    userid,
                    orders_message or "unknown_error",
                )

        except Exception:
            logger.exception(
                "BrokerReconcile: orders fatal error userid=%s",
                userid,
            )

        user_ok = bool(funds_ok or positions_ok or orders_ok)

        if user_ok:
            logger.info(
                "BrokerReconcile: user done userid=%s funds_ok=%s positions_ok=%s orders_ok=%s",
                userid,
                funds_ok,
                positions_ok,
                orders_ok,
            )
        else:
            logger.warning(
                "BrokerReconcile: user failed userid=%s funds_ok=%s positions_ok=%s orders_ok=%s",
                userid,
                funds_ok,
                positions_ok,
                orders_ok,
            )

        return {
            "userid": userid,
            "funds_ok": funds_ok,
            "positions_ok": positions_ok,
            "orders_ok": orders_ok,
            "user_ok": user_ok,
        }

    def _fetch_due_users(self, *, limit_users: int = DEFAULT_LIMIT_USERS) -> List[UserSchema]:
        """
        Current scope:
          - active REAL users
          - currently marked logged_in=1
        """
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
            "funds_synced": 0,
            "positions_synced": 0,
            "orders_synced": 0,
            "errors": 0,
        }