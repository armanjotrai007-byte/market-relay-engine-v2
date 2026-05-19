from market_relay_engine.common.serialization import from_json_string, to_json_string
from market_relay_engine.contracts.system import SystemHealthEvent
from tests.fixtures.system import (
    build_system_examples,
    make_healthy_system_health_event,
    make_warning_system_health_event,
)


def test_system_fixtures_include_healthy_and_warning_records() -> None:
    examples = build_system_examples()

    assert all(isinstance(example, SystemHealthEvent) for example in examples)
    assert {example.status for example in examples} == {"healthy", "warning"}


def test_warning_system_fixture_contains_warning_values() -> None:
    warning = make_warning_system_health_event()

    assert warning.status == "warning"
    assert warning.feed_delay_ms == 420.0
    assert warning.reconnect_count == 0


def test_system_fixtures_serialize_to_json_string() -> None:
    healthy = make_healthy_system_health_event()
    parsed = from_json_string(to_json_string(healthy))

    assert parsed["health_event_id"] == "FIXTURE-HEALTH-EVENT-0001"
    assert parsed["event_time"].endswith("Z")

