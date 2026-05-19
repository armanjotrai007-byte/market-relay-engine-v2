from market_relay_engine.common.serialization import from_json_string, to_json_string
from market_relay_engine.contracts.risk import RiskDecisionType
from tests.fixtures.scenarios import (
    SCENARIO_KEYS,
    approved_oil_trade_scenario,
    blocked_defense_trade_scenario,
    build_scenario_examples,
    latency_slippage_warning_scenario,
    reduced_size_context_risk_scenario,
    stale_context_block_scenario,
)


def test_all_scenarios_have_exact_documented_key_set() -> None:
    expected_keys = set(SCENARIO_KEYS)

    for scenario in build_scenario_examples():
        assert set(scenario) == expected_keys


def test_approved_oil_trade_scenario_contains_consistent_order_fill_outcome() -> None:
    scenario = approved_oil_trade_scenario()
    feature_snapshot = scenario["feature_snapshot"]
    model_signal = scenario["model_signal"]
    risk_decision = scenario["risk_decision"]
    order_event = scenario["order_event"]
    fill_event = scenario["fill_event"]
    trade_outcome = scenario["trade_outcome"]

    assert order_event is not None
    assert fill_event is not None
    assert trade_outcome is not None
    assert model_signal.feature_snapshot_id == feature_snapshot.feature_snapshot_id
    assert risk_decision.model_signal_id == model_signal.signal_id
    assert fill_event.order_id == order_event.order_id
    assert trade_outcome.order_id == order_event.order_id
    assert trade_outcome.signal_id == model_signal.signal_id


def test_blocked_scenarios_have_no_order_fill_or_outcome() -> None:
    for scenario in [blocked_defense_trade_scenario(), stale_context_block_scenario()]:
        assert scenario["order_event"] is None
        assert scenario["fill_event"] is None
        assert scenario["trade_outcome"] is None


def test_reduced_size_scenario_has_reduce_size_decision() -> None:
    scenario = reduced_size_context_risk_scenario()
    risk_decision = scenario["risk_decision"]

    assert risk_decision.decision is RiskDecisionType.REDUCE_SIZE
    assert risk_decision.reduce_size_factor == 0.5


def test_latency_slippage_scenario_includes_warning_records() -> None:
    scenario = latency_slippage_warning_scenario()

    assert scenario["latency_metric"].latency_ms == 420.0
    assert scenario["system_health_event"].status == "warning"
    assert scenario["fill_event"].slippage == 0.15


def test_stale_context_scenario_uses_stale_and_expired_context() -> None:
    scenario = stale_context_block_scenario()
    risk_decision = scenario["risk_decision"]
    model_signal = scenario["model_signal"]
    context_indicator = scenario["context_indicators"][0]
    context_event = scenario["context_events"][0]
    context_flag = scenario["context_flags"][0]

    assert risk_decision.decision is RiskDecisionType.BLOCK
    assert "stale_context" in risk_decision.reasons
    assert context_indicator.freshness_seconds == 7200.0
    assert context_event.valid_until < model_signal.signal_time
    assert context_flag.valid_until < model_signal.signal_time


def test_scenarios_serialize_to_json_strings() -> None:
    for scenario in build_scenario_examples():
        parsed = from_json_string(to_json_string(scenario))
        assert set(parsed) == set(SCENARIO_KEYS)

