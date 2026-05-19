"""Model signal contract shapes."""

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


class SignalSide(str, Enum):
    """Future model signal side values."""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    EXIT = "EXIT"
    DO_NOTHING = "DO_NOTHING"


@dataclass(frozen=True, kw_only=True)
class ModelSignal:
    """Signal emitted by a future model inference layer."""

    signal_time: datetime
    ticker: str
    signal: SignalSide
    confidence: float
    raw_score: float | None
    model_version: str
    calibration_version: str
    feature_version: str
    feature_snapshot_id: str
    signal_id: str = field(default_factory=lambda: new_record_id("signal"))
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal_time", utc_datetime(self.signal_time))
        require_non_empty_string(self.signal_id, "signal_id")
        require_optional_non_empty_string(self.trace_id, "trace_id")
