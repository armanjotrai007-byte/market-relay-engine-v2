"""Simple JSON serialization helpers for dataclass contracts."""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from market_relay_engine.common.time import to_utc_iso


JsonValue = Any


def _to_json_safe(value: Any) -> JsonValue:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _to_json_safe(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return to_utc_iso(value)
    if isinstance(value, dict):
        converted: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            converted[key] = _to_json_safe(child)
        return converted
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(child) for child in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"Value of type {type(value).__name__} is not JSON serializable")


def to_json_dict(value: Any) -> JsonValue:
    """Convert dataclasses, enums, datetimes, and containers to JSON-safe values."""
    return _to_json_safe(value)


def to_json_string(value: Any) -> str:
    """Serialize a value to a stable JSON string."""
    return json.dumps(
        to_json_dict(value),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def from_json_string(value: str) -> dict[str, Any]:
    """Parse a JSON object string into a plain dictionary."""
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("Expected a JSON object")
    return parsed
