from market_relay_engine.common.serialization import from_json_string, to_json_string
from market_relay_engine.contracts.context import (
    ContextAIEvent,
    ContextFlag,
    ContextIndicatorSnapshot,
)
from tests.fixtures.context import (
    build_context_examples,
    make_context_ai_event,
    make_eia_window_indicator,
    make_social_context_flag,
)


def test_context_fixtures_include_indicator_event_and_flag_records() -> None:
    examples = build_context_examples()

    assert any(isinstance(example, ContextIndicatorSnapshot) for example in examples)
    assert any(isinstance(example, ContextAIEvent) for example in examples)
    assert any(isinstance(example, ContextFlag) for example in examples)


def test_context_indicator_examples_are_source_aware_without_api_calls() -> None:
    indicator = make_eia_window_indicator()

    assert indicator.source == "fake_eia_calendar_fixture"
    assert indicator.indicator_name == "eia_window"
    assert indicator.freshness_seconds is not None


def test_context_event_and_flag_serialize_with_validity_windows() -> None:
    event = make_context_ai_event()
    flag = make_social_context_flag()

    parsed_event = from_json_string(to_json_string(event))
    parsed_flag = from_json_string(to_json_string(flag))

    assert parsed_event["context_event_id"] == "FIXTURE-CONTEXT-EVENT-0001"
    assert parsed_event["valid_until"].endswith("Z")
    assert parsed_flag["context_flag_id"] == "FIXTURE-CONTEXT-FLAG-0001"
    assert parsed_flag["valid_until"].endswith("Z")

