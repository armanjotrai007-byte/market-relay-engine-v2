"""Reusable timezone-aware UTC timestamps for fake fixture records."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


MARKET_OPEN_UTC = datetime(2026, 5, 18, 13, 30, 0, tzinfo=UTC)


def milliseconds_after_market_open(milliseconds: int) -> datetime:
    """Return a timezone-aware UTC timestamp after the fixture market open."""
    return MARKET_OPEN_UTC + timedelta(milliseconds=milliseconds)


def seconds_after_market_open(seconds: int) -> datetime:
    """Return a timezone-aware UTC timestamp after the fixture market open."""
    return MARKET_OPEN_UTC + timedelta(seconds=seconds)


def minutes_after_market_open(minutes: int) -> datetime:
    """Return a timezone-aware UTC timestamp after the fixture market open."""
    return MARKET_OPEN_UTC + timedelta(minutes=minutes)

