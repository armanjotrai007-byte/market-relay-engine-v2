from datetime import UTC, datetime, timezone, timedelta

import pytest

from market_relay_engine.common.time import (
    current_utc_datetime,
    ensure_timezone_aware_utc,
    format_utc_iso,
    monotonic_time_ms,
    parse_utc_iso,
    to_utc_iso,
    utc_now,
)


def test_current_utc_datetime_is_timezone_aware_utc() -> None:
    value = current_utc_datetime()
    assert value.tzinfo is UTC


def test_utc_now_is_timezone_aware_utc() -> None:
    value = utc_now()
    assert value.tzinfo is UTC


def test_format_utc_iso_returns_z_suffix() -> None:
    value = datetime(2026, 5, 15, 12, 30, 45, tzinfo=UTC)
    assert format_utc_iso(value) == "2026-05-15T12:30:45Z"


def test_format_utc_iso_default_is_parseable_utc() -> None:
    value = format_utc_iso()
    assert value.endswith("Z")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_to_utc_iso_normalizes_offsets_to_z_suffix() -> None:
    value = datetime(2026, 5, 15, 8, 30, 45, tzinfo=timezone(timedelta(hours=-4)))
    assert to_utc_iso(value) == "2026-05-15T12:30:45Z"


def test_parse_utc_iso_returns_aware_utc() -> None:
    parsed = parse_utc_iso("2026-05-15T08:30:45-04:00")
    assert parsed == datetime(2026, 5, 15, 12, 30, 45, tzinfo=UTC)


def test_naive_datetimes_are_rejected() -> None:
    naive = datetime(2026, 5, 15, 12, 30, 45)
    with pytest.raises(ValueError, match="timezone-aware"):
        ensure_timezone_aware_utc(naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        to_utc_iso(naive)


def test_parse_utc_iso_rejects_naive_strings() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_utc_iso("2026-05-15T12:30:45")


def test_monotonic_time_ms_returns_non_negative_integer() -> None:
    first = monotonic_time_ms()
    second = monotonic_time_ms()
    assert isinstance(first, int)
    assert first >= 0
    assert second >= first
