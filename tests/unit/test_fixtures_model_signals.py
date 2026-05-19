from market_relay_engine.common.serialization import from_json_string, to_json_string
from market_relay_engine.contracts.model import ModelSignal, SignalSide
from tests.fixtures.model_signals import (
    build_model_signal_examples,
    make_buy_model_signal,
)


def test_model_signal_fixtures_include_supported_signal_examples() -> None:
    examples = build_model_signal_examples()

    assert all(isinstance(example, ModelSignal) for example in examples)
    assert {example.signal for example in examples} == {
        SignalSide.BUY,
        SignalSide.SELL,
        SignalSide.HOLD,
        SignalSide.DO_NOTHING,
    }


def test_model_signal_uses_stable_ids_and_versions() -> None:
    signal = make_buy_model_signal()

    assert signal.signal_id == "FIXTURE-SIGNAL-0001"
    assert signal.feature_snapshot_id == "FIXTURE-FEATURE-SNAPSHOT-0001"
    assert signal.model_version
    assert signal.feature_version
    assert signal.calibration_version


def test_model_signal_serializes_to_json_string() -> None:
    parsed = from_json_string(to_json_string(make_buy_model_signal()))

    assert parsed["signal"] == SignalSide.BUY.value
    assert parsed["signal_time"].endswith("Z")

