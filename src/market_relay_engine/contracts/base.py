"""Small shared helpers for contract dataclasses."""

from __future__ import annotations

from datetime import datetime

from market_relay_engine.common.time import ensure_timezone_aware_utc


DEFAULT_SCHEMA_VERSION = "contracts_v1"


def utc_datetime(value: datetime) -> datetime:
    """Normalize a required datetime to timezone-aware UTC."""
    return ensure_timezone_aware_utc(value)


def optional_utc_datetime(value: datetime | None) -> datetime | None:
    """Normalize an optional datetime to timezone-aware UTC."""
    if value is None:
        return None
    return ensure_timezone_aware_utc(value)


def require_non_empty_string(value: str, field_name: str) -> None:
    """Validate that an explicit ID string is non-empty."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def require_optional_non_empty_string(value: str | None, field_name: str) -> None:
    """Validate that an optional ID string is non-empty when provided."""
    if value is not None:
        require_non_empty_string(value, field_name)
