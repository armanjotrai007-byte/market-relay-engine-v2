"""Risk decision contract shapes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from market_relay_engine.common.ids import new_record_id
from market_relay_engine.contracts.base import (
    DEFAULT_SCHEMA_VERSION,
    require_non_empty_string,
    require_optional_non_empty_string,
    utc_datetime,
)


class RiskDecisionType(str, Enum):
    """Future deterministic risk gate decision values."""

    APPROVE = "APPROVE"
    BLOCK = "BLOCK"
    REDUCE_SIZE = "REDUCE_SIZE"
    EXIT = "EXIT"
    DO_NOTHING = "DO_NOTHING"


@dataclass(frozen=True, kw_only=True)
class RiskDecision:
    """Data shape for a future deterministic risk gate result."""

    decision_time: datetime
    ticker: str
    model_signal_id: str
    decision: RiskDecisionType
    approved: bool
    risk_version: str
    reduce_size_factor: float | None = None
    reasons: list[str] = field(default_factory=list)
    thresholds_used: dict[str, Any] = field(default_factory=dict)
    cost_estimate_id: str | None = None
    context_snapshot_id: str | None = None
    risk_decision_id: str = field(default_factory=lambda: new_record_id("risk_decision"))
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_time", utc_datetime(self.decision_time))
        require_non_empty_string(self.risk_decision_id, "risk_decision_id")
        require_optional_non_empty_string(self.cost_estimate_id, "cost_estimate_id")
        require_optional_non_empty_string(self.trace_id, "trace_id")
