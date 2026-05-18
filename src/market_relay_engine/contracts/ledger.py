"""Ledger record contract shapes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from market_relay_engine.common.ids import new_record_id
from market_relay_engine.contracts.base import (
    DEFAULT_SCHEMA_VERSION,
    optional_utc_datetime,
    require_non_empty_string,
    require_optional_non_empty_string,
    utc_datetime,
)


@dataclass(frozen=True, kw_only=True)
class TradeOutcome:
    """Trade outcome shape for future ledger and weekly analysis records."""

    signal_id: str
    order_id: str
    ticker: str
    entry_time: datetime
    outcome_id: str = field(default_factory=lambda: new_record_id("outcome"))
    exit_time: datetime | None = None
    realized_pnl: float | None = None
    return_1m: float | None = None
    return_5m: float | None = None
    return_15m: float | None = None
    max_favorable_excursion: float | None = None
    max_adverse_excursion: float | None = None
    result: str | None = None
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_time", utc_datetime(self.entry_time))
        object.__setattr__(self, "exit_time", optional_utc_datetime(self.exit_time))
        require_non_empty_string(self.outcome_id, "outcome_id")
        require_non_empty_string(self.signal_id, "signal_id")
        require_non_empty_string(self.order_id, "order_id")
        require_optional_non_empty_string(self.trace_id, "trace_id")


@dataclass(frozen=True, kw_only=True)
class LatencyMetric:
    """Latency metric shape for future component timing records."""

    measured_time: datetime
    component: str
    latency_ms: float
    source: str
    latency_metric_id: str = field(default_factory=lambda: new_record_id("latency_metric"))
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "measured_time", utc_datetime(self.measured_time))
        require_non_empty_string(self.latency_metric_id, "latency_metric_id")
        require_optional_non_empty_string(self.trace_id, "trace_id")
