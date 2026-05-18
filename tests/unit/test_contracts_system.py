from market_relay_engine.contracts.system import SystemHealthEvent
from tests.unit.contract_test_utils import assert_contract_serializes, example_for


def test_system_health_event_serializes_with_id() -> None:
    parsed = assert_contract_serializes(example_for(SystemHealthEvent))

    assert parsed["health_event_id"]
    assert parsed["component"] == "local_validation"
    assert parsed["status"] == "ok"
    assert parsed["event_time"].endswith("Z")
