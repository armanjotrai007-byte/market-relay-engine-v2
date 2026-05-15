from datetime import UTC, datetime

from market_relay_engine.common.time import current_utc_datetime, format_utc_iso


def test_current_utc_datetime_is_timezone_aware_utc() -> None:
    value = current_utc_datetime()
    assert value.tzinfo is UTC


def test_format_utc_iso_returns_z_suffix() -> None:
    value = datetime(2026, 5, 15, 12, 30, 45, tzinfo=UTC)
    assert format_utc_iso(value) == "2026-05-15T12:30:45Z"


def test_format_utc_iso_default_is_parseable_utc() -> None:
    value = format_utc_iso()
    assert value.endswith("Z")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
