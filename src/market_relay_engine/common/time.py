"""UTC time helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def current_utc_datetime() -> datetime:
    """Return the current timezone-aware UTC datetime."""
    return datetime.now(UTC)


def format_utc_iso(value: datetime | None = None) -> str:
    """Format a datetime as an ISO-8601 UTC string ending in Z."""
    timestamp = value or current_utc_datetime()
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    else:
        timestamp = timestamp.astimezone(UTC)
    return timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")
