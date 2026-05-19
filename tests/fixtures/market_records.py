"""Fake market record fixtures for tests."""

from __future__ import annotations

from market_relay_engine.contracts.market import MarketRecord
from tests.fixtures.ids import TRACE_ID_APPROVED_OIL, TRACE_ID_BLOCKED_DEFENSE
from tests.fixtures.times import (
    milliseconds_after_market_open,
    seconds_after_market_open,
)

# These fixtures use the project's generic `MarketRecord` contract fields. They
# are fake test records and do not represent exact Databento DBN schema field
# names or field mappings. Real Databento DBN inspection and source-to-contract
# mapping will be handled in later PRs.


def make_market_trade_record(
    *,
    ticker: str = "XOM",
    price: float = 118.42,
    size: float = 100.0,
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
) -> MarketRecord:
    """Return a fake generic trade-like market record."""
    event_time = seconds_after_market_open(index)
    return MarketRecord(
        event_time=event_time,
        ticker=ticker,
        source="fake_market_data_fixture",
        record_type="trade",
        price=price,
        size=size,
        source_event_time=event_time,
        local_receive_time=milliseconds_after_market_open(index * 1000 + 5),
        trace_id=trace_id,
    )


def make_market_quote_record(
    *,
    ticker: str = "XOM",
    bid_price: float = 118.41,
    ask_price: float = 118.43,
    bid_size: float = 500.0,
    ask_size: float = 400.0,
    index: int = 2,
    trace_id: str = TRACE_ID_APPROVED_OIL,
) -> MarketRecord:
    """Return a fake generic quote/BBO-like market record."""
    event_time = seconds_after_market_open(index)
    spread = round(ask_price - bid_price, 4)
    midprice = round((bid_price + ask_price) / 2, 4)
    return MarketRecord(
        event_time=event_time,
        ticker=ticker,
        source="fake_market_data_fixture",
        record_type="quote",
        bid_price=bid_price,
        ask_price=ask_price,
        bid_size=bid_size,
        ask_size=ask_size,
        spread=spread,
        midprice=midprice,
        source_event_time=event_time,
        local_receive_time=milliseconds_after_market_open(index * 1000 + 6),
        trace_id=trace_id,
    )


def make_oil_market_record(
    *,
    ticker: str = "XOM",
    index: int = 3,
    trace_id: str = TRACE_ID_APPROVED_OIL,
) -> MarketRecord:
    """Return a fake oil-sector market record."""
    return make_market_quote_record(
        ticker=ticker,
        bid_price=118.40,
        ask_price=118.44,
        bid_size=700.0,
        ask_size=600.0,
        index=index,
        trace_id=trace_id,
    )


def make_defense_market_record(
    *,
    ticker: str = "LMT",
    index: int = 4,
    trace_id: str = TRACE_ID_BLOCKED_DEFENSE,
) -> MarketRecord:
    """Return a fake defense-sector market record."""
    return make_market_quote_record(
        ticker=ticker,
        bid_price=472.15,
        ask_price=472.55,
        bid_size=80.0,
        ask_size=60.0,
        index=index,
        trace_id=trace_id,
    )


def build_market_record_examples() -> list[MarketRecord]:
    """Return representative fake market records for validation and tests."""
    return [
        make_market_trade_record(),
        make_market_quote_record(),
        make_oil_market_record(ticker="XOM"),
        make_oil_market_record(ticker="CVX", index=5),
        make_defense_market_record(ticker="LMT"),
        make_defense_market_record(ticker="RTX", index=6),
    ]

