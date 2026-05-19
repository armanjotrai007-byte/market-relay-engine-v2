from datetime import UTC, datetime

from tests.fixtures.times import (
    MARKET_OPEN_UTC,
    milliseconds_after_market_open,
    minutes_after_market_open,
    seconds_after_market_open,
)


def _assert_aware_utc(value: datetime) -> None:
    assert value.tzinfo is not None
    assert value.utcoffset() == UTC.utcoffset(value)


def test_market_open_timestamp_is_timezone_aware_utc() -> None:
    _assert_aware_utc(MARKET_OPEN_UTC)


def test_millisecond_offset_helper_returns_timezone_aware_utc() -> None:
    value = milliseconds_after_market_open(5)

    _assert_aware_utc(value)
    assert value > MARKET_OPEN_UTC


def test_second_offset_helper_returns_timezone_aware_utc() -> None:
    value = seconds_after_market_open(1)

    _assert_aware_utc(value)
    assert value > MARKET_OPEN_UTC


def test_minute_offset_helper_returns_timezone_aware_utc() -> None:
    value = minutes_after_market_open(1)

    _assert_aware_utc(value)
    assert value > MARKET_OPEN_UTC


def test_time_offsets_preserve_ordering_and_are_not_naive() -> None:
    values = [
        MARKET_OPEN_UTC,
        milliseconds_after_market_open(1),
        seconds_after_market_open(1),
        minutes_after_market_open(1),
    ]

    assert values == sorted(values)
    for value in values:
        _assert_aware_utc(value)

