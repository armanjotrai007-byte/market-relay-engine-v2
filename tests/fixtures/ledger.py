"""Fake ledger record fixtures for tests."""

from __future__ import annotations

from market_relay_engine.contracts.execution import OrderEvent
from market_relay_engine.contracts.ledger import LatencyMetric, TradeOutcome
from market_relay_engine.contracts.model import ModelSignal
from tests.fixtures.execution import make_order_event
from tests.fixtures.ids import TRACE_ID_APPROVED_OIL, stable_record_id
from tests.fixtures.model_signals import make_model_signal
from tests.fixtures.times import seconds_after_market_open


def make_trade_outcome(
    *,
    model_signal: ModelSignal | None = None,
    order_event: OrderEvent | None = None,
    ticker: str = "XOM",
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
) -> TradeOutcome:
    """Return a fake trade outcome without ledger writes."""
    model_signal = model_signal or make_model_signal(ticker=ticker, index=index, trace_id=trace_id)
    order_event = order_event or make_order_event(ticker=ticker, index=index, trace_id=trace_id)
    return TradeOutcome(
        signal_id=model_signal.signal_id,
        order_id=order_event.order_id,
        ticker=ticker,
        entry_time=order_event.order_time,
        outcome_id=stable_record_id("outcome", index),
        exit_time=seconds_after_market_open(index + 70),
        realized_pnl=42.5,
        return_1m=0.001,
        return_5m=0.0024,
        return_15m=0.0031,
        max_favorable_excursion=0.0042,
        max_adverse_excursion=-0.0013,
        result="fixture_closed",
        trace_id=trace_id,
    )


def make_latency_metric(
    *,
    component: str = "fixture_pipeline",
    latency_ms: float = 18.5,
    source: str = "local_timer_fixture",
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
) -> LatencyMetric:
    """Return a fake latency metric without external monitoring."""
    return LatencyMetric(
        measured_time=seconds_after_market_open(index + 11),
        component=component,
        latency_ms=latency_ms,
        source=source,
        latency_metric_id=stable_record_id("latency_metric", index),
        trace_id=trace_id,
    )


def build_ledger_examples() -> list[object]:
    """Return representative fake ledger records."""
    return [
        make_trade_outcome(),
        make_latency_metric(),
    ]

