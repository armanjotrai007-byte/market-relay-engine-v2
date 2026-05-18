from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

import pytest

from market_relay_engine.common.serialization import (
    from_json_string,
    to_json_dict,
    to_json_string,
)


class ExampleEnum(str, Enum):
    VALUE = "VALUE"


@dataclass(frozen=True)
class NestedExample:
    event_time: datetime
    status: ExampleEnum
    children: list[dict[str, object]]


def test_serialization_handles_nested_dataclasses_enums_and_datetimes() -> None:
    example = NestedExample(
        event_time=datetime(2026, 5, 18, 14, 30, tzinfo=UTC),
        status=ExampleEnum.VALUE,
        children=[{"ok": True, "count": 2}],
    )

    json_dict = to_json_dict(example)
    json_string = to_json_string(example)
    parsed = from_json_string(json_string)

    assert json_dict["event_time"] == "2026-05-18T14:30:00Z"
    assert json_dict["status"] == "VALUE"
    assert parsed == json_dict


def test_from_json_string_returns_plain_dict_only() -> None:
    parsed = from_json_string('{"a":1}')
    assert parsed == {"a": 1}

    with pytest.raises(ValueError, match="JSON object"):
        from_json_string("[1, 2, 3]")


def test_to_json_dict_rejects_non_string_dict_keys() -> None:
    with pytest.raises(TypeError, match="keys"):
        to_json_dict({1: "bad"})


def test_to_json_string_rejects_unsupported_objects() -> None:
    with pytest.raises(TypeError, match="not JSON serializable"):
        to_json_string(object())
