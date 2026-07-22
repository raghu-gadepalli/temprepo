# schemas/user.py

from __future__ import annotations

import logging
from datetime import datetime
import re
from typing import Optional, List, Union
from typing_extensions import Literal
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from database.database import get_trades_db
from models.trade_models import User as UserORM

logger = logging.getLogger(__name__)

# maps the code prefix to the canonical name in users preferences
UNDERLYING_MAP = {
    "NIFTY":     "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    # add more index mappings here if needed
}


class UserSchema(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    userid: str
    name: str
    email: str
    mobile: str
    password: str  # Should be excluded from API responses for security
    broker_login: int  # 0 or 1
    broker_name: Optional[str]
    apikey: Optional[str]
    secretkey: Optional[str]
    access_token: Optional[str]
    intraday_only: int    # 0 or 1  whether to autoexit at EOD
    stocks: str
    equity: int   # 0 or 1
    futures: int  # 0 or 1
    options: int  # 0 or 1
    execution_mode: Literal['REAL','VIRTUAL'] | str = 'VIRTUAL'
    autotrade: int  # 0 or 1
    active: int     # 0 or 1
    logged_in: int  # 0 or 1
    logged_time: Optional[datetime]

    @staticmethod
    def fetch_user(userid: str) -> Optional[UserSchema]:
        logger.info("Fetching user [%s]", userid)
        with get_trades_db() as db:
            rec = db.query(UserORM).filter(UserORM.userid == userid).one_or_none()
        if not rec:
            logger.info("User[%s] not found", userid)
            return None
        return UserSchema.model_validate(rec)


    @staticmethod
    def fetch_user_by_mobile(mobile: str) -> Optional["UserSchema"]:
        mobile = (mobile or "").strip()
        logger.info("Fetching user by mobile [%s]", mobile)
        with get_trades_db() as db:
            rec = db.query(UserORM).filter(UserORM.mobile == mobile).one_or_none()
        if not rec:
            logger.info("User with mobile[%s] not found", mobile)
            return None
        return UserSchema.model_validate(rec)


    @staticmethod
    def fetch_users(
        active: Optional[int] = None,
        broker_login: Optional[int] = None,
        logged_in: Optional[int] = None,
        autotrade: Optional[int] = None
    ) -> List[UserSchema]:
        logger.info("Fetching users (active=%s, broker_login=%s, logged_in=%s)", active, broker_login, logged_in)
        with get_trades_db() as db:
            q = db.query(UserORM)
            if active is not None:
                q = q.filter(UserORM.active == active)
            if broker_login is not None:
                q = q.filter(UserORM.broker_login == broker_login)
            if logged_in is not None:
                q = q.filter(UserORM.logged_in == logged_in)
            if autotrade is not None:
                q = q.filter(UserORM.autotrade == autotrade)
            rows = q.all()
        return [UserSchema.model_validate(r) for r in rows]

    @staticmethod
    def create_user(user_data: dict) -> Union[UserSchema, dict]:
        valid = {
            "userid","name","email","mobile","password",
            "broker_login","broker_name","apikey","secretkey",
            "access_token","intraday_only","stocks","equity","futures","options",
            "autotrade","active","logged_in","logged_time"
        }
        payload = {k: v for k, v in user_data.items() if k in valid}
        logger.info("Creating user [%s]", payload.get("userid"))

        with get_trades_db() as db:
            if "userid" in payload:
                exists = db.query(UserORM).filter(UserORM.userid == payload["userid"]).one_or_none()
                if exists:
                    logger.warning("User[%s] already exists", payload["userid"])
                    return {"error": f"User with userid {payload['userid']} already exists."}

            new = UserORM(**payload)
            db.add(new)
            try:
                db.commit()
                db.refresh(new)
                logger.info("Created user [%s]", new.userid)
                return UserSchema.model_validate(new)
            except SQLAlchemyError as e:
                db.rollback()
                logger.error("Error inserting user[%s]: %s", payload.get("userid"), e, exc_info=True)
                return {"error": f"Error inserting user: {e}"}

    @staticmethod
    def update_user(userid: str, update_data: dict) -> Union[UserSchema, None, dict]:
        logger.info("Updating user [%s]", userid)
        with get_trades_db() as db:
            rec = db.query(UserORM).filter(UserORM.userid == userid).one_or_none()
            if not rec:
                logger.info("User[%s] not found for update", userid)
                return None

            for k, v in update_data.items():
                if k != "userid" and hasattr(rec, k):
                    setattr(rec, k, v)

            try:
                db.commit()
                db.refresh(rec)
                logger.info("Updated user [%s]", userid)
                return UserSchema.model_validate(rec)
            except SQLAlchemyError as e:
                db.rollback()
                logger.error("Error updating user[%s]: %s", userid, e, exc_info=True)
                return {"error": f"Error updating user: {e}"}

    @staticmethod
    def delete_user(userid: str) -> Union[str, None, dict]:
        logger.info("Deleting user [%s]", userid)
        with get_trades_db() as db:
            rec = db.query(UserORM).filter(UserORM.userid == userid).one_or_none()
            if not rec:
                logger.info("User[%s] not found for delete", userid)
                return None
            rec.active = 0
            try:
                db.commit()
                logger.info("Deleted user [%s]", userid)
                return f"User {userid} deactivated."
            except SQLAlchemyError as e:
                db.rollback()
                logger.error("Error deleting user[%s]: %s", userid, e, exc_info=True)
                return {"error": f"Error deleting user: {e}"}

    @staticmethod
    def fetch_managed_users() -> List["UserSchema"]:
        """Return every active account visible in managed-trade views.

        Visibility is independent of current broker/app login state and of the
        user's REAL/VIRTUAL default. Existing orders, positions, performance,
        profile, and funds rows must not disappear when a token expires.
        """
        return UserSchema.fetch_users(active=1)

    @staticmethod
    def fetch_tradeable_users() -> List["UserSchema"]:
        """Return active, app-logged-in users for current trade actions.

        This method is intentionally execution-mode neutral. Automatic signal
        deployment uses ``fetch_autogen_users`` below, which additionally
        requires the explicit per-user autotrade flag.
        """
        return UserSchema.fetch_users(active=1, logged_in=1)

    @staticmethod
    def fetch_autogen_users() -> List["UserSchema"]:
        """Return users explicitly eligible for automatic trade generation."""
        return UserSchema.fetch_users(active=1, logged_in=1, autotrade=1)

    @staticmethod
    def fetch_real_users(logged_in: Optional[int] = None) -> List["UserSchema"]:
        """
        Returns active REAL broker-enabled users.

        Base criteria:
        - active = 1
        - broker_login = 1
        - execution_mode = 'REAL'

        Optional:
        - logged_in filter (0 or 1)
        """
        logger.info("Fetching REAL users (logged_in=%s)", logged_in)

        with get_trades_db() as db:
            q = (
                db.query(UserORM)
                .filter(UserORM.active == 1)
                .filter(UserORM.broker_login == 1)
                .filter(UserORM.execution_mode == "REAL")
            )

            if logged_in is not None:
                q = q.filter(UserORM.logged_in == logged_in)

            rows = q.order_by(UserORM.userid.asc()).all()

        return [UserSchema.model_validate(r) for r in rows]

    def allowed_symbols(self, full_symbol: str) -> bool:
        """
        Returns True if this user's stocks preference (e.g. "SBIN,NIFTY BANK")
        contains the underlying ticker of full_symbol (e.g. "SBIN25MAYFUT"  "SBIN",
        "BANKNIFTY25JULFUT"  "NIFTY BANK").
        If no preferences are set, allows everything.
        """
        raw = self.stocks or ""
        tickers = {s.strip().upper() for s in raw.split(",") if s.strip()}
        if not tickers:
            return True

        # extract the alpha prefix (e.g. "NIFTY", "BANKNIFTY", "RELIANCE")
        m = re.match(r"^([A-Za-z]+)", full_symbol)
        prefix = m.group(1).upper() if m else full_symbol.upper()

        # map to the user-facing name if present
        underlying = UNDERLYING_MAP.get(prefix, prefix)

        return underlying in tickers

    def is_transaction_permitted(self) -> bool:
        return (
            self.broker_login == 1
            and self.active == 1
            and bool(self.broker_name)
        )
