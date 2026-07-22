# database.py

import logging
import os
import time
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import OperationalError

from config import AppConfig  # make sure this points to your Config class

logger = logging.getLogger(__name__)


class DatabaseConnectionError(Exception):
    """Raised when we can't establish a DB session."""


# Internal globals for fork-safe engine/session recreation
_trades_engine = None
_trades_engine_pid = None
_TradesSessionLocal = None


def _get_trades_engine_and_session():
    global _trades_engine, _trades_engine_pid, _TradesSessionLocal
    pid = os.getpid()
    if _trades_engine is None or _trades_engine_pid != pid:
        # (Re)create engine in this process
        _trades_engine = create_engine(
            AppConfig.SQLALCHEMY_BINDS["trades"],
            pool_pre_ping=True,
            pool_recycle=3600,          # recycle connections before MySQLs typical timeout
            pool_size=8,                # adjust based on concurrency needs
            max_overflow=10,            # allow some burst capacity
            connect_args={"connect_timeout": 10},  # fail faster on stalled connects
        )
        _trades_engine_pid = pid
        _TradesSessionLocal = sessionmaker(bind=_trades_engine)
        logger.debug("Initialized new trades engine for PID %s", pid)
    return _trades_engine, _TradesSessionLocal


@contextmanager
def get_trades_db() -> Generator[Session, None, None]:
    """
    Yields a SQLAlchemy Session.
    Raises DatabaseConnectionError if the DB is unreachable.
    """
    try:
        _, SessionLocal = _get_trades_engine_and_session()
        with SessionLocal() as db:
            yield db
    except OperationalError as e:
        logger.error("Unable to connect to trades database: %s", e, exc_info=e)
        raise DatabaseConnectionError("Could not connect to trades database") from e

class _EngineProxy:
    """
    Lazy proxy to the current trades engine so callers can do:
        from database import trades_engine
        trades_engine.dispose()
    and attribute access delegates to the current process-specific engine.
    """

    def __getattr__(self, name):
        engine, _ = _get_trades_engine_and_session()
        return getattr(engine, name)

    def dispose(self):
        engine, _ = _get_trades_engine_and_session()
        engine.dispose()

# expose a proxy instance named exactly as legacy code expected
trades_engine = _EngineProxy()
