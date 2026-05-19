from market_relay_engine.contracts.model import ModelSignal, SignalSide
from tests.unit.contract_test_utils import assert_contract_serializes, example_for


def test_model_signal_serializes_enum_and_id() -> None:
    parsed = assert_contract_serializes(example_for(ModelSignal))

    assert parsed["signal_id"]
    assert parsed["signal"] == SignalSide.BUY.value
    assert parsed["feature_snapshot_id"]
    assert parsed["signal_time"].endswith("Z")
