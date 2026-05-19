from market_relay_engine.common.serialization import from_json_string, to_json_string
from market_relay_engine.contracts.risk import RiskDecision, RiskDecisionType
from tests.fixtures.risk_decisions import (
    build_risk_decision_examples,
    make_approve_risk_decision,
    make_block_risk_decision,
    make_reduce_size_risk_decision,
)


def test_risk_decision_fixtures_include_expected_decisions() -> None:
    examples = build_risk_decision_examples()

    assert all(isinstance(example, RiskDecision) for example in examples)
    assert {example.decision for example in examples} == {
        RiskDecisionType.APPROVE,
        RiskDecisionType.BLOCK,
        RiskDecisionType.REDUCE_SIZE,
    }


def test_risk_decision_reasons_are_reusable_examples() -> None:
    approve = make_approve_risk_decision()
    block = make_block_risk_decision()
    reduce = make_reduce_size_risk_decision()

    assert approve.approved is True
    assert block.approved is False
    assert {"spread_too_wide", "confidence_too_low"}.issubset(block.reasons)
    assert reduce.decision is RiskDecisionType.REDUCE_SIZE
    assert reduce.reduce_size_factor == 0.5
    assert {"eia_window", "ai_context_high_risk"}.issubset(reduce.reasons)


def test_risk_decision_serializes_to_json_string() -> None:
    parsed = from_json_string(to_json_string(make_block_risk_decision()))

    assert parsed["decision"] == RiskDecisionType.BLOCK.value
    assert parsed["risk_decision_id"] == "FIXTURE-RISK-DECISION-0001"
    assert parsed["decision_time"].endswith("Z")

