#!/usr/bin/env python3
# schemas/orderprofile.py  (Ultra-Simplified JSON-backed)

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_DEFAULT_JSON_PATH = os.path.join(os.path.dirname(__file__), "orderprofiles.json")


def _norm_side(side: Any) -> str:
    s = str(side or "").strip().upper()
    if s in ("BUY", "SELL"):
        return s
    return ""


class OrderProfileSchema(BaseModel, frozen=True):
    product_type: str
    exchange: str
    order_type: str
    order_variety: str

    allow: Optional[Dict[str, bool]] = Field(default=None)

    def is_side_allowed(self, side: Any) -> bool:
        s = _norm_side(side)
        if not s:
            return True
        if not self.allow:
            return True
        return bool(self.allow.get(s, True))

    @staticmethod
    @lru_cache(maxsize=1)
    def _load_json(path: str = _DEFAULT_JSON_PATH) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            logger.exception("Failed to load orderprofiles.json")
            return {}

    @staticmethod
    def fetch_order_profile(
        profile_name: str,
        instrument_type: str,
        side: Optional[str] = None,
    ) -> Optional["OrderProfileSchema"]:

        pn = str(profile_name or "").strip().lower()
        it = str(instrument_type or "").strip().upper()

        doc = OrderProfileSchema._load_json()

        block = doc.get(pn)
        if not block:
            return None

        data = block.get(it)
        if not data:
            return None

        try:
            op = OrderProfileSchema.model_validate(data)
        except Exception:
            logger.exception("Invalid order profile config for %s/%s", pn, it)
            return None

        s = _norm_side(side)
        if s and not op.is_side_allowed(s):
            return None

        return op

    @staticmethod
    def reload_cache():
        OrderProfileSchema._load_json.cache_clear()