"""Plain-JSON helpers for snapshot-carried Auction continuity.

This module intentionally avoids the generic checkpoint codec.  Snapshot
continuity is versioned application data: enums become values, datetimes become
ISO strings and dataclasses become ordinary mappings without Python module or
class names.
"""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from types import UnionType
from typing import Any, Dict, Mapping, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel


def to_plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if is_dataclass(value):
        return {field.name: to_plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_plain(item) for item in value]
    return value


def restore_dataclass(cls: type, payload: Mapping[str, Any], *, overrides: Dict[str, Any] | None = None) -> Any:
    hints = get_type_hints(cls)
    overrides = overrides or {}
    kwargs: Dict[str, Any] = {}
    for field in fields(cls):
        if field.name not in payload:
            continue
        if field.name in overrides:
            annotation = overrides[field.name]
        elif field.name in hints:
            annotation = hints[field.name]
        else:
            raise ValueError(
                f"Missing type annotation for {cls.__name__}.{field.name}"
            )
        kwargs[field.name] = restore_typed(payload[field.name], annotation)
    return cls(**kwargs)


def restore_typed(value: Any, annotation: Any) -> Any:
    if value is None:
        return None
    if annotation is Any or annotation is None:
        return value

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin in (Union, UnionType):
        non_none = [item for item in args if item is not type(None)]
        if not non_none:
            return value
        last_error: Exception | None = None
        for item in non_none:
            try:
                return restore_typed(value, item)
            except Exception as exc:  # pragma: no cover - defensive fallback
                last_error = exc
        if last_error is not None:
            raise last_error
        return value

    if annotation is datetime:
        return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if annotation is date:
        return value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value))

    try:
        if isinstance(annotation, type) and issubclass(annotation, Enum):
            return annotation(value)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return annotation.model_validate(value)
    except TypeError:
        pass

    if isinstance(annotation, type) and is_dataclass(annotation):
        if not isinstance(value, Mapping):
            raise TypeError(f"Expected mapping for {annotation.__name__}")
        return restore_dataclass(annotation, value)

    if origin in (list,):
        subtype = args[0] if args else Any
        return [restore_typed(item, subtype) for item in value]
    if origin in (set, frozenset):
        subtype = args[0] if args else Any
        restored = {restore_typed(item, subtype) for item in value}
        return restored if origin is set else frozenset(restored)
    if origin in (tuple,):
        if not args:
            return tuple(value)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(restore_typed(item, args[0]) for item in value)
        return tuple(
            restore_typed(item, args[index] if index < len(args) else Any)
            for index, item in enumerate(value)
        )
    if origin in (dict, Dict):
        key_type = args[0] if args else Any
        value_type = args[1] if len(args) > 1 else Any
        return {
            restore_typed(key, key_type): restore_typed(item, value_type)
            for key, item in value.items()
        }

    return value


__all__ = ["to_plain", "restore_dataclass", "restore_typed"]
