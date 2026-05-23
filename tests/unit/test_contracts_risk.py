from market_relay_engine.contracts.risk import RiskDecision, RiskDecisionType
from tests.unit.contract_test_utils import assert_contract_serializes, example_for


def test_risk_decision_serializes_enum_reasons_and_thresholds() -> None:
    parsed = assert_contract_serializes(example_for(RiskDecision))

    assert parsed["risk_decision_id"]
    assert parsed["decision"] == RiskDecisionType.BLOCK.value
    assert parsed["approved"] is False
    assert parsed["reasons"] == ["example_only"]
    assert parsed["thresholds_used"]["max_spread_bps"] == 10
    assert parsed["cost_estimate_id"] == "cost_estimate_example"
