from market_relay_engine.common.serialization import from_json_string, to_json_string
from market_relay_engine.contracts.execution import FillEvent, OrderEvent, OrderSide
from tests.fixtures.execution import build_execution_examples, make_fill_event, make_order_event


def test_execution_fixtures_include_order_and_fill_contracts() -> None:
    examples = build_execution_examples()

    assert any(isinstance(example, OrderEvent) for example in examples)
    assert any(isinstance(example, FillEvent) for example in examples)


def test_order_and_fill_are_paper_only_and_internally_consistent() -> None:
    order = make_order_event()
    fill = make_fill_event(order_event=order)

    assert order.paper_trading is True
    assert order.broker == "paper_fixture"
    assert order.side is OrderSide.BUY
    assert fill.order_id == order.order_id
    assert fill.expected_price == order.expected_price
    assert fill.slippage is not None


def test_execution_fixtures_serialize_to_json_string() -> None:
    order = make_order_event()
    fill = make_fill_event(order_event=order)

    parsed_order = from_json_string(to_json_string(order))
    parsed_fill = from_json_string(to_json_string(fill))

    assert parsed_order["order_id"] == "FIXTURE-ORDER-0001"
    assert parsed_order["paper_trading"] is True
    assert parsed_fill["fill_id"] == "FIXTURE-FILL-0001"
    assert parsed_fill["order_id"] == parsed_order["order_id"]

