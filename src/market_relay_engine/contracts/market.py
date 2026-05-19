"""Market data contract shapes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from market_relay_engine.contracts.base import (
    DEFAULT_SCHEMA_VERSION,
    optional_utc_datetime,
    require_optional_non_empty_string,
    utc_datetime,
)


@dataclass(frozen=True, kw_only=True)
class MarketRecord:
    """Canonical market record shape for future market-data adapters."""

    event_time: datetime
    ticker: str
    source: str
    record_type: str
    raw_symbol: str | None = None
    price: float | None = None
    size: float | None = None
    bid_price: float | None = None
    ask_price: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    spread: float | None = None
    midprice: float | None = None
    source_event_time: datetime | None = None
    local_receive_time: datetime | None = None
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_time", utc_datetime(self.event_time))
        object.__setattr__(
            self,
            "source_event_time",
            optional_utc_datetime(self.source_event_time),
        )
        object.__setattr__(
            self,
            "local_receive_time",
            optional_utc_datetime(self.local_receive_time),
        )
        require_optional_non_empty_string(self.trace_id, "trace_id")
