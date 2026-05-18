"""Execution event contract shapes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from market_relay_engine.common.ids import new_record_id
from market_relay_engine.contracts.base import (
    DEFAULT_SCHEMA_VERSION,
    require_non_empty_string,
    require_optional_non_empty_string,
    utc_datetime,
)


class OrderSide(str, Enum):
    """Order side values."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """Order type values."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderStatus(str, Enum):
    """Order status values."""

    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, kw_only=True)
class OrderEvent:
    """Order event shape for future paper/live execution metrics."""

    order_time: datetime
    ticker: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    status: OrderStatus
    order_id: str = field(default_factory=lambda: new_record_id("order"))
    expected_price: float | None = None
    submitted_price: float | None = None
    broker: str | None = None
    paper_trading: bool = True
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_time", utc_datetime(self.order_time))
        require_non_empty_string(self.order_id, "order_id")
        require_optional_non_empty_string(self.trace_id, "trace_id")


@dataclass(frozen=True, kw_only=True)
class FillEvent:
    """Fill event shape for future execution quality measurement."""

    fill_time: datetime
    order_id: str
    ticker: str
    side: OrderSide
    quantity: float
    fill_price: float
    fill_id: str = field(default_factory=lambda: new_record_id("fill"))
    expected_price: float | None = None
    slippage: float | None = None
    broker_status: str | None = None
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "fill_time", utc_datetime(self.fill_time))
        require_non_empty_string(self.fill_id, "fill_id")
        require_non_empty_string(self.order_id, "order_id")
        require_optional_non_empty_string(self.trace_id, "trace_id")
