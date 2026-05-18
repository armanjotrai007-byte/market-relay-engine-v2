import re

from market_relay_engine.common.ids import (
    new_record_id,
    new_run_id,
    new_session_id,
    new_trace_id,
)


ID_PATTERN = re.compile(r"^[a-z_]+_[0-9a-f]{32}$")


def test_runtime_ids_are_non_empty_log_safe_strings() -> None:
    ids = [new_run_id(), new_session_id(), new_trace_id(), new_record_id("signal")]

    assert len(set(ids)) == len(ids)
    for value in ids:
        assert isinstance(value, str)
        assert ID_PATTERN.match(value)


def test_new_record_id_rejects_empty_prefix() -> None:
    try:
        new_record_id("")
    except ValueError as exc:
        assert "prefix" in str(exc)
    else:
        raise AssertionError("Expected empty prefix to raise ValueError")
