"""Context contract shapes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from market_relay_engine.common.ids import new_record_id
from market_relay_engine.contracts.base import (
    DEFAULT_SCHEMA_VERSION,
    optional_utc_datetime,
    require_non_empty_string,
    require_optional_non_empty_string,
    utc_datetime,
)


@dataclass(frozen=True, kw_only=True)
class ContextIndicatorSnapshot:
    """Structured context indicator snapshot for future risk inputs."""

    snapshot_time: datetime
    source: str
    ticker_or_sector: str
    indicator_name: str
    value: Any
    window: str | None = None
    units: str | None = None
    freshness_seconds: float | None = None
    source_event_time: datetime | None = None
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_time", utc_datetime(self.snapshot_time))
        object.__setattr__(
            self,
            "source_event_time",
            optional_utc_datetime(self.source_event_time),
        )
        require_optional_non_empty_string(self.trace_id, "trace_id")


@dataclass(frozen=True, kw_only=True)
class ContextAIEvent:
    """Structured output from a future AI context filter."""

    event_time: datetime
    source: str
    source_id: str
    affected_tickers: list[str]
    event_type: str
    context_event_id: str = field(default_factory=lambda: new_record_id("context_event"))
    affected_sector: str | None = None
    sentiment: str | None = None
    urgency: str | None = None
    risk_level: str | None = None
    confidence: float | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    summary: str | None = None
    prompt_version: str | None = None
    model_version: str | None = None
    raw_input_hash: str | None = None
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_time", utc_datetime(self.event_time))
        object.__setattr__(self, "valid_from", optional_utc_datetime(self.valid_from))
        object.__setattr__(self, "valid_until", optional_utc_datetime(self.valid_until))
        require_non_empty_string(self.context_event_id, "context_event_id")
        require_optional_non_empty_string(self.trace_id, "trace_id")


@dataclass(frozen=True, kw_only=True)
class ContextFlag:
    """Structured risk flag consumed by a future deterministic risk gate."""

    event_time: datetime
    source: str
    flag_type: str
    severity: str
    context_flag_id: str = field(default_factory=lambda: new_record_id("context_flag"))
    ticker: str | None = None
    sector: str | None = None
    confidence: float | None = None
    valid_until: datetime | None = None
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_time", utc_datetime(self.event_time))
        object.__setattr__(self, "valid_until", optional_utc_datetime(self.valid_until))
        require_non_empty_string(self.context_flag_id, "context_flag_id")
        require_optional_non_empty_string(self.trace_id, "trace_id")
