from market_relay_engine.contracts.execution import (
    FillEvent,
    OrderEvent,
    OrderSide,
    OrderStatus,
    OrderType,
)
from tests.unit.contract_test_utils import assert_contract_serializes, example_for


def test_order_event_serializes_enums_and_paper_flag() -> None:
    parsed = assert_contract_serializes(example_for(OrderEvent))

    assert parsed["order_id"]
    assert parsed["side"] == OrderSide.BUY.value
    assert parsed["order_type"] == OrderType.LIMIT.value
    assert parsed["status"] == OrderStatus.SUBMITTED.value
    assert parsed["paper_trading"] is True


def test_fill_event_serializes_with_order_reference() -> None:
    parsed = assert_contract_serializes(example_for(FillEvent))

    assert parsed["fill_id"]
    assert parsed["order_id"]
    assert parsed["side"] == OrderSide.BUY.value
    assert parsed["fill_time"].endswith("Z")
