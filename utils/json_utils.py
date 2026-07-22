# utils/json_utils.py

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
import math


def sanitize_json(obj: Any) -> Any:
    """
    Recursively convert values into MySQL/SQLAlchemy JSON-safe objects.

    Converts:
      - Enum            -> enum.value
      - Decimal         -> float
      - date/datetime   -> ISO8601 string
      - float NaN/inf   -> None
      - dict            -> string keys + sanitized values
      - list/tuple/set  -> sanitized list
      - pydantic model  -> model_dump() sanitized

    Everything else that is already JSON-safe is returned unchanged.
    Unknown objects are converted to str so audit/log payloads never break
    live/replay processing.
    """
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj

    if isinstance(obj, Enum):
        return sanitize_json(obj.value)

    if isinstance(obj, Decimal):
        return float(obj)

    if isinstance(obj, (date, datetime)):
        return obj.isoformat()

    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None

    if isinstance(obj, dict):
        return {str(k): sanitize_json(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple, set)):
        return [sanitize_json(v) for v in obj]

    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        try:
            return sanitize_json(model_dump())
        except Exception:
            pass

    return str(obj)
