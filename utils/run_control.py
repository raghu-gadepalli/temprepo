# utils/run_control.py

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Set
from zoneinfo import ZoneInfo

from configs.service_config import SERVICE_CONFIG


@dataclass(frozen=True)
class _DayControl:
    tz: ZoneInfo
    holidays: Set[str]
    blackout_dates: Set[str]
    whitelist_dates: Set[str]


def _load_control() -> _DayControl:
    rc = SERVICE_CONFIG.run_control
    return _DayControl(
        tz=ZoneInfo(rc.tz),
        holidays=set(rc.holidays),
        blackout_dates=set(rc.blackout_dates),
        whitelist_dates=set(rc.whitelist_dates),
    )


def _market_open_today(now: datetime, dc: _DayControl) -> bool:
    d = now.date().isoformat()

    if d in dc.whitelist_dates:
        return True

    if now.weekday() >= 5:
        return False

    if d in dc.holidays:
        return False

    if d in dc.blackout_dates:
        return False

    return True


def allow_run_today(logger, service_name: str = "service") -> bool:
    dc = _load_control()
    now = datetime.now(dc.tz)

    if _market_open_today(now, dc):
        logger.info(
            "[%s] market open (%s) → allowed",
            service_name,
            now.date().isoformat(),
        )
        return True

    logger.info(
        "[%s] market closed (%s) → exiting",
        service_name,
        now.date().isoformat(),
    )
    return False
