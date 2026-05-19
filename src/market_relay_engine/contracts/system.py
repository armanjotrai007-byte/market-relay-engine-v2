"""System health contract shapes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from market_relay_engine.common.ids import new_record_id
from market_relay_engine.contracts.base import (
    DEFAULT_SCHEMA_VERSION,
    require_non_empty_string,
    require_optional_non_empty_string,
    utc_datetime,
)


@dataclass(frozen=True, kw_only=True)
class SystemHealthEvent:
    """System health event shape for future monitoring ledger records."""

    event_time: datetime
    component: str
    status: str
    health_event_id: str = field(default_factory=lambda: new_record_id("health_event"))
    message: str | None = None
    cpu_percent: float | None = None
    memory_percent: float | None = None
    clock_offset_ms: float | None = None
    feed_delay_ms: float | None = None
    reconnect_count: int | None = None
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_time", utc_datetime(self.event_time))
        require_non_empty_string(self.health_event_id, "health_event_id")
        require_optional_non_empty_string(self.trace_id, "trace_id")
