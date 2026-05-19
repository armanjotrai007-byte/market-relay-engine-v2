"""UTC time helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic_ns


def utc_now() -> datetime:
    """Return the current timezone-aware UTC datetime."""
    return datetime.now(UTC)


def ensure_timezone_aware_utc(value: datetime) -> datetime:
    """Return ``value`` normalized to UTC, rejecting naive datetimes."""
    if not isinstance(value, datetime):
        raise TypeError("Expected a datetime value")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Datetime values must be timezone-aware")
    return value.astimezone(UTC)


def to_utc_iso(value: datetime) -> str:
    """Format a timezone-aware datetime as an ISO-8601 UTC string ending in Z."""
    timestamp = ensure_timezone_aware_utc(value)
    return timestamp.isoformat().replace("+00:00", "Z")


def parse_utc_iso(value: str) -> datetime:
    """Parse an ISO-8601 datetime string and normalize it to timezone-aware UTC."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("ISO datetime value must be a non-empty string")

    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO datetime value: {value}") from exc
    return ensure_timezone_aware_utc(parsed)


def monotonic_time_ms() -> int:
    """Return the current monotonic clock value in milliseconds."""
    return monotonic_ns() // 1_000_000


def current_utc_datetime() -> datetime:
    """Return the current timezone-aware UTC datetime."""
    return utc_now()


def format_utc_iso(value: datetime | None = None) -> str:
    """Format a datetime as an ISO-8601 UTC string ending in Z."""
    return to_utc_iso(value or utc_now())
