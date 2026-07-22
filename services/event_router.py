# event_router.py

import logging
from typing import Callable, Dict
from schemas.event import EventSchema

logger = logging.getLogger(__name__)

# registry map: event_type -> handler
_ACTION_MAP: Dict[str, Callable[[EventSchema], None]] = {}


def register(event_type: str):
    def decorator(fn: Callable[[EventSchema], None]):
        _ACTION_MAP[event_type] = fn
        return fn
    return decorator


def dispatch_event(event: EventSchema):
    handler = _ACTION_MAP.get(event.event_type)
    if not handler:
        logger.warning("No handler registered for event_type %s", event.event_type)
        return
    handler(event)
