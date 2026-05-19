from market_relay_engine.contracts.ledger import LatencyMetric, TradeOutcome
from tests.unit.contract_test_utils import assert_contract_serializes, example_for


def test_trade_outcome_serializes_references_and_returns() -> None:
    parsed = assert_contract_serializes(example_for(TradeOutcome))

    assert parsed["outcome_id"]
    assert parsed["signal_id"]
    assert parsed["order_id"]
    assert parsed["entry_time"].endswith("Z")
    assert parsed["exit_time"].endswith("Z")


def test_latency_metric_serializes_with_id() -> None:
    parsed = assert_contract_serializes(example_for(LatencyMetric))

    assert parsed["latency_metric_id"]
    assert parsed["component"] == "feature_builder"
    assert parsed["measured_time"].endswith("Z")
