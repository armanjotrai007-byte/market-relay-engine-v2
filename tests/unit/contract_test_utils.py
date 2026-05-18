from __future__ import annotations

from typing import TypeVar

from market_relay_engine.common.serialization import (
    from_json_string,
    to_json_dict,
    to_json_string,
)
from scripts.check_contracts import build_contract_examples


T = TypeVar("T")


def example_for(contract_type: type[T]) -> T:
    for example in build_contract_examples():
        if isinstance(example, contract_type):
            return example
    raise AssertionError(f"No example found for {contract_type.__name__}")


def assert_contract_serializes(example: object) -> dict[str, object]:
    json_dict = to_json_dict(example)
    json_string = to_json_string(example)
    parsed = from_json_string(json_string)

    assert isinstance(json_dict, dict)
    assert isinstance(json_string, str)
    assert isinstance(parsed, dict)
    assert parsed["schema_version"] == "contracts_v1"
    return parsed
