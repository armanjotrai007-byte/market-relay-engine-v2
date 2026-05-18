"""Feature snapshot contract shapes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from market_relay_engine.common.ids import new_record_id
from market_relay_engine.contracts.base import (
    DEFAULT_SCHEMA_VERSION,
    require_non_empty_string,
    require_optional_non_empty_string,
    utc_datetime,
)


@dataclass(frozen=True, kw_only=True)
class FeatureSnapshot:
    """Feature vector snapshot produced by a future canonical feature builder."""

    snapshot_time: datetime
    ticker: str
    feature_version: str
    features: dict[str, Any]
    source_record_count: int
    lookback_window_seconds: float
    feature_snapshot_id: str = field(
        default_factory=lambda: new_record_id("feature_snapshot")
    )
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_time", utc_datetime(self.snapshot_time))
        require_non_empty_string(self.feature_snapshot_id, "feature_snapshot_id")
        require_optional_non_empty_string(self.trace_id, "trace_id")
