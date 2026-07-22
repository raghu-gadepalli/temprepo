#!/usr/bin/env python3
import logging
import os
import sys
import time
from zoneinfo import ZoneInfo
from datetime import datetime

# allow imports from project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from logconfig import setup_logging  # existing shared logging setup
setup_logging()
logger = logging.getLogger(__name__)

from schemas.event import EventSchema
from services.event_router import register, dispatch_event
from schemas.user import UserSchema
from services.derivatives.derivatives_generator import DerivativesGenerator

IST = ZoneInfo("Asia/Kolkata")


# --- handler registration ---
@register("derivatives.build")
def handle_derivatives_build(event: EventSchema):
    logger.info("Handler invoked for derivatives.build on %s (corr=%s)", event.aggregate_key, event.correlation_id)
    user = UserSchema.fetch_user(os.getenv("AppConfig.DATA_USER", "DR1812"))
    if not user:
        raise RuntimeError("AppConfig.DATA_USER missing")
    gen = DerivativesGenerator(api_key=user.apikey, access_token=user.access_token)
    gen.generate(event.aggregate_key)
    logger.info("Derivatives generation completed for %s", event.aggregate_key)


def main():
    symbol = "NIFTY BANK"
    event_type = "derivatives.build"

    logger.info("==== event_example: emit event ====")
    ev = EventSchema.emit_event(
        event_type=event_type,
        aggregate_key=symbol,
        payload={"source": "event_example"},
    )
    if not ev:
        logger.error("Failed to emit event")
        return

    logger.info("Emitted event: %s", ev)

    # Poll for due event and process it
    for attempt in range(10):
        logger.info("Polling for due event (attempt %d)...", attempt + 1)
        due = EventSchema.fetch_due_event()
        if not due:
            logger.info("No due event yet, sleeping...")
            time.sleep(1)
            continue

        logger.info("Picked up event: %s", due)
        try:
            dispatch_event(due)
            due.mark_succeeded()
            logger.info("Event succeeded: %s", due)
        except Exception as e:
            logger.exception("Event processing failed: %s", due)
            due.mark_failed(str(e))
        break

    logger.info("Done. Inspect the events table for correlation_id=%s", ev.correlation_id)


if __name__ == "__main__":
    main()
