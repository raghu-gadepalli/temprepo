"""JSON-safe codec for Auction Engine restart checkpoints.

The checkpoint stores only trusted AutoTrades engine objects.  Type tags are
restricted to ``services.auction_engine`` contracts/dataclasses plus Python
standard date/time, decimal and enum values.  No pickle or arbitrary code
execution is used.
"""
from __future__ import annotations

from collections import deque
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import importlib
import hashlib
import json
from typing import Any, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel


_ALLOWED_PREFIXES = (
    "services.auction_engine.",
)


def encode_checkpoint_value(value: Any) -> Any:
    # Enum must be checked before primitive types. Several Auction contracts use
    # ``str, Enum`` classes; treating those as plain strings works in memory but
    # loses the enum type after a JSON/database round trip.
    if isinstance(value, Enum):
        return {
            "__kind__": "enum",
            "type": _type_name(type(value)),
            "value": value.value,
        }
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return {"__kind__": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"__kind__": "date", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"__kind__": "decimal", "value": str(value)}
    if isinstance(value, BaseModel):
        return {
            "__kind__": "pydantic",
            "type": _type_name(type(value)),
            "data": encode_checkpoint_value(
                value.model_dump(mode="python", exclude_none=False)
            ),
        }
    if is_dataclass(value):
        return {
            "__kind__": "dataclass",
            "type": _type_name(type(value)),
            "data": {
                item.name: encode_checkpoint_value(getattr(value, item.name))
                for item in fields(value)
            },
        }
    if isinstance(value, deque):
        return {
            "__kind__": "deque",
            "maxlen": value.maxlen,
            "items": [encode_checkpoint_value(item) for item in value],
        }
    if isinstance(value, tuple):
        return {
            "__kind__": "tuple",
            "items": [encode_checkpoint_value(item) for item in value],
        }
    if isinstance(value, set):
        return {
            "__kind__": "set",
            "items": [
                encode_checkpoint_value(item)
                for item in sorted(value, key=str)
            ],
        }
    if isinstance(value, list):
        return [encode_checkpoint_value(item) for item in value]
    if isinstance(value, dict):
        return {
            "__kind__": "dict",
            "items": [
                [encode_checkpoint_value(key), encode_checkpoint_value(item)]
                for key, item in value.items()
            ],
        }
    raise TypeError(f"Unsupported checkpoint value: {type(value)!r}")


def decode_checkpoint_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [decode_checkpoint_value(item) for item in value]
    if not isinstance(value, dict):
        raise TypeError(f"Invalid checkpoint payload value: {type(value)!r}")

    if "__kind__" not in value:
        return {key: decode_checkpoint_value(item) for key, item in value.items()}
    kind = value["__kind__"]
    if not isinstance(kind, str) or not kind:
        raise ValueError("Checkpoint __kind__ must be a non-empty string")
    if kind == "datetime":
        return datetime.fromisoformat(value["value"])
    if kind == "date":
        return date.fromisoformat(value["value"])
    if kind == "decimal":
        return Decimal(value["value"])
    if kind == "enum":
        enum_type = _resolve_type(value["type"])
        return enum_type(value["value"])
    if kind == "pydantic":
        model_type = _resolve_type(value["type"])
        data = decode_checkpoint_value(value["data"])
        return model_type.model_validate(data)
    if kind == "dataclass":
        data_type = _resolve_type(value["type"])
        data = {
            key: decode_checkpoint_value(item)
            for key, item in value["data"].items()
        }
        annotations = get_type_hints(data_type)
        missing_annotations = set(data).difference(annotations)
        if missing_annotations:
            raise ValueError(
                f"Checkpoint dataclass {data_type.__name__} has untyped fields: "
                f"{sorted(missing_annotations)}"
            )
        data = {
            key: _coerce_annotated_value(annotations[key], item)
            for key, item in data.items()
        }
        return data_type(**data)
    if kind == "deque":
        _require_codec_keys(value, {"__kind__", "items", "maxlen"}, kind)
        return deque(
            (decode_checkpoint_value(item) for item in value["items"]),
            maxlen=value["maxlen"],
        )
    if kind == "tuple":
        _require_codec_keys(value, {"__kind__", "items"}, kind)
        return tuple(decode_checkpoint_value(item) for item in value["items"])
    if kind == "set":
        _require_codec_keys(value, {"__kind__", "items"}, kind)
        return {decode_checkpoint_value(item) for item in value["items"]}
    if kind == "dict":
        _require_codec_keys(value, {"__kind__", "items"}, kind)
        return {
            decode_checkpoint_value(key): decode_checkpoint_value(item)
            for key, item in value["items"]
        }
    raise ValueError(f"Unknown checkpoint kind: {kind}")



def _require_codec_keys(
    value: dict[str, Any],
    required: set[str],
    kind: str,
) -> None:
    missing = required.difference(value)
    extra = set(value).difference(required)
    if missing or extra:
        raise ValueError(
            f"Invalid {kind} checkpoint payload; "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )

def _coerce_annotated_value(annotation: Any, value: Any) -> Any:
    """Coerce JSON-decoded legacy values using a trusted dataclass annotation."""
    if annotation is None or value is None:
        return value

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is Union:
        errors = []
        for option in args:
            if option is type(None):
                continue
            try:
                return _coerce_annotated_value(option, value)
            except (TypeError, ValueError) as exc:
                errors.append(str(exc))
        raise ValueError(
            f"Checkpoint value {value!r} does not match {annotation!r}: {errors}"
        )

    if origin in (list, tuple, set, frozenset):
        if not args:
            return value
        coerced = [_coerce_annotated_value(args[0], item) for item in value]
        if origin is tuple:
            return tuple(coerced)
        if origin is set:
            return set(coerced)
        if origin is frozenset:
            return frozenset(coerced)
        return coerced

    if origin is dict and len(args) == 2:
        return {
            _coerce_annotated_value(args[0], key):
            _coerce_annotated_value(args[1], item)
            for key, item in value.items()
        }

    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return value if isinstance(value, annotation) else annotation(value)

    return value


def checkpoint_state_hash(payload: Any) -> str:
    """Return the canonical SHA-256 hash of a JSON-safe checkpoint payload.

    ``AuctionEngine.export_checkpoint`` already returns the encoded JSON-safe
    structure. Hash it directly so in-memory, persisted, continuous, and restored
    runs all use the same comparison contract.
    """
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _type_name(value_type: type) -> str:
    return f"{value_type.__module__}:{value_type.__qualname__}"


def _resolve_type(type_name: str) -> type:
    module_name, separator, qualname = str(type_name or "").partition(":")
    if not separator or not module_name.startswith(_ALLOWED_PREFIXES):
        raise ValueError(f"Checkpoint type is not allowed: {type_name}")
    module = importlib.import_module(module_name)
    obj: Any = module
    for part in qualname.split("."):
        obj = getattr(obj, part)
    if not isinstance(obj, type):
        raise TypeError(f"Checkpoint target is not a type: {type_name}")
    return obj


__all__ = ["encode_checkpoint_value", "decode_checkpoint_value", "checkpoint_state_hash"]
