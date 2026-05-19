from market_relay_engine.common.serialization import from_json_string, to_json_string
from market_relay_engine.contracts.ledger import LatencyMetric, TradeOutcome
from tests.fixtures.ledger import build_ledger_examples, make_latency_metric, make_trade_outcome


def test_ledger_fixtures_include_trade_outcome_and_latency_metric() -> None:
    examples = build_ledger_examples()

    assert any(isinstance(example, TradeOutcome) for example in examples)
    assert any(isinstance(example, LatencyMetric) for example in examples)


def test_trade_outcome_contains_return_and_excursion_examples() -> None:
    outcome = make_trade_outcome()

    assert outcome.return_1m is not None
    assert outcome.return_5m is not None
    assert outcome.return_15m is not None
    assert outcome.max_favorable_excursion is not None
    assert outcome.max_adverse_excursion is not None


def test_ledger_fixtures_serialize_to_json_string() -> None:
    outcome = make_trade_outcome()
    latency = make_latency_metric()

    parsed_outcome = from_json_string(to_json_string(outcome))
    parsed_latency = from_json_string(to_json_string(latency))

    assert parsed_outcome["outcome_id"] == "FIXTURE-OUTCOME-0001"
    assert parsed_outcome["entry_time"].endswith("Z")
    assert parsed_latency["latency_metric_id"] == "FIXTURE-LATENCY-METRIC-0001"
    assert parsed_latency["measured_time"].endswith("Z")

