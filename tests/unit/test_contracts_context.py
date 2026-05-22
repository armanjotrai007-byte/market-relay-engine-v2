from market_relay_engine.contracts.context import (
    ContextAIEvent,
    ContextFlag,
    ContextIndicatorSnapshot,
    ContextStateSnapshot,
)
from tests.unit.contract_test_utils import assert_contract_serializes, example_for


def test_context_indicator_snapshot_serializes() -> None:
    parsed = assert_contract_serializes(example_for(ContextIndicatorSnapshot))

    assert parsed["indicator_name"] == "eia_window"
    assert parsed["snapshot_time"].endswith("Z")
    assert parsed["source_event_time"].endswith("Z")


def test_context_ai_event_serializes_with_id_and_validity_window() -> None:
    parsed = assert_contract_serializes(example_for(ContextAIEvent))

    assert parsed["context_event_id"]
    assert parsed["affected_tickers"] == ["XOM"]
    assert parsed["valid_from"].endswith("Z")
    assert parsed["valid_until"].endswith("Z")


def test_context_flag_serializes_with_id() -> None:
    parsed = assert_contract_serializes(example_for(ContextFlag))

    assert parsed["context_flag_id"]
    assert parsed["flag_type"] == "context_risk"
    assert parsed["valid_until"].endswith("Z")


def test_context_state_snapshot_serializes_with_id_lists_and_validity() -> None:
    parsed = assert_contract_serializes(example_for(ContextStateSnapshot))

    assert parsed["context_snapshot_id"]
    assert parsed["ticker"] == "XOM"
    assert parsed["active_indicator_ids"] == ["context_indicator_example"]
    assert parsed["context_summary"] == {"summary": "example_only"}
    assert parsed["valid_until"].endswith("Z")
