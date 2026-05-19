"""Fake paper execution event fixtures for tests."""

from __future__ import annotations

from market_relay_engine.contracts.execution import (
    FillEvent,
    OrderEvent,
    OrderSide,
    OrderStatus,
    OrderType,
)
from tests.fixtures.ids import TRACE_ID_APPROVED_OIL, stable_record_id
from tests.fixtures.times import seconds_after_market_open


def make_order_event(
    *,
    ticker: str = "XOM",
    side: OrderSide = OrderSide.BUY,
    quantity: float = 100.0,
    expected_price: float = 118.42,
    submitted_price: float = 118.43,
    status: OrderStatus = OrderStatus.SUBMITTED,
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
) -> OrderEvent:
    """Return a fake paper order event without broker execution."""
    return OrderEvent(
        order_time=seconds_after_market_open(index + 9),
        ticker=ticker,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        status=status,
        order_id=stable_record_id("order", index),
        expected_price=expected_price,
        submitted_price=submitted_price,
        broker="paper_fixture",
        paper_trading=True,
        trace_id=trace_id,
    )


def make_fill_event(
    *,
    order_event: OrderEvent | None = None,
    fill_price: float = 118.44,
    slippage: float = 0.02,
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
) -> FillEvent:
    """Return a fake fill event tied to a fake paper order."""
    order_event = order_event or make_order_event(index=index, trace_id=trace_id)
    return FillEvent(
        fill_time=seconds_after_market_open(index + 10),
        order_id=order_event.order_id,
        ticker=order_event.ticker,
        side=order_event.side,
        quantity=order_event.quantity,
        fill_price=fill_price,
        fill_id=stable_record_id("fill", index),
        expected_price=order_event.expected_price,
        slippage=slippage,
        broker_status="filled",
        trace_id=trace_id,
    )


def build_execution_examples() -> list[object]:
    """Return representative fake paper execution records."""
    order = make_order_event()
    fill = make_fill_event(order_event=order)
    return [order, fill]

