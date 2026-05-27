"""Risk decision construction helpers."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from market_relay_engine.contracts.model import ModelSignal
from market_relay_engine.contracts.risk import RiskDecision, RiskDecisionType


RISK_VERSION = "risk_filter_v1"

REASON_APPROVED = "approved"
REASON_CONFIDENCE_TOO_LOW = "confidence_too_low"
REASON_CONSECUTIVE_LOSS_LIMIT_HIT = "consecutive_loss_limit_hit"
REASON_COST_ESTIMATE_TICKER_MISMATCH = "cost_estimate_ticker_mismatch"
REASON_COST_NOT_PROFITABLE_AFTER_COSTS = "cost_not_profitable_after_costs"
REASON_DAILY_LOSS_LIMIT_HIT = "daily_loss_limit_hit"
REASON_DUPLICATE_OR_CONFLICTING_ORDER = "duplicate_or_conflicting_order"
REASON_ELEVATED_CONTEXT_RISK = "elevated_context_risk"
REASON_EVENT_WINDOW_ACTIVE = "event_window_active"
REASON_HIGH_CONTEXT_RISK = "high_context_risk"
REASON_LATENCY_TOO_HIGH = "latency_too_high"
REASON_MAX_OPEN_POSITIONS_HIT = "max_open_positions_hit"
REASON_MAX_POSITION_PER_SYMBOL_HIT = "max_position_per_symbol_hit"
REASON_MISSING_COST_ESTIMATE = "missing_cost_estimate"
REASON_MISSING_LATENCY_MS = "missing_latency_ms"
REASON_MISSING_MARKET_DATA_TIME = "missing_market_data_time"
REASON_MISSING_SPREAD = "missing_spread"
REASON_SIGNAL_EXIT = "signal_exit"
REASON_SIGNAL_NO_ACTION = "signal_no_action"
REASON_SPREAD_BPS_TOO_WIDE = "spread_bps_too_wide"
REASON_SPREAD_DOLLARS_TOO_WIDE = "spread_dollars_too_wide"
REASON_STALE_MARKET_DATA = "stale_market_data"


def build_risk_decision(
    *,
    signal: ModelSignal,
    evaluation_time: datetime,
    decision: RiskDecisionType,
    approved: bool,
    reason: str,
    thresholds_used: dict[str, Any],
    cost_estimate_id: str | None = None,
    context_snapshot_id: str | None = None,
    reduce_size_factor: float | None = None,
    extra_reasons: Iterable[str] = (),
) -> RiskDecision:
    """Return a validated RiskDecision for one deterministic risk outcome."""
    _validate_reduce_size_factor(decision, reduce_size_factor)
    reasons = _unique_reasons([reason, *extra_reasons])
    return RiskDecision(
        decision_time=evaluation_time,
        ticker=signal.ticker,
        model_signal_id=signal.signal_id,
        decision=decision,
        approved=approved,
        risk_version=RISK_VERSION,
        reduce_size_factor=reduce_size_factor,
        reasons=reasons,
        thresholds_used=thresholds_used,
        cost_estimate_id=cost_estimate_id,
        context_snapshot_id=context_snapshot_id,
        trace_id=signal.trace_id,
    )


def is_entry_allowed(decision: RiskDecision) -> bool:
    """Return true for entry decisions that may proceed to sizing/execution."""
    return decision.decision in {
        RiskDecisionType.APPROVE,
        RiskDecisionType.REDUCE_SIZE,
    }


def effective_size_factor(decision: RiskDecision) -> float:
    """Return the entry size factor implied by a RiskDecision."""
    if decision.decision is RiskDecisionType.APPROVE:
        return 1.0
    if decision.decision is RiskDecisionType.REDUCE_SIZE:
        factor = decision.reduce_size_factor
        if factor is None:
            raise ValueError("REDUCE_SIZE decision requires reduce_size_factor")
        if factor <= 0 or factor > 1.0:
            raise ValueError("reduce_size_factor must be > 0 and <= 1.0")
        return factor
    return 0.0


def _validate_reduce_size_factor(
    decision: RiskDecisionType,
    reduce_size_factor: float | None,
) -> None:
    if decision is RiskDecisionType.REDUCE_SIZE:
        if reduce_size_factor is None:
            raise ValueError("REDUCE_SIZE decision requires reduce_size_factor")
        if reduce_size_factor <= 0 or reduce_size_factor > 1.0:
            raise ValueError("reduce_size_factor must be > 0 and <= 1.0")
        return
    if reduce_size_factor is not None:
        raise ValueError("reduce_size_factor is only valid for REDUCE_SIZE")


def _unique_reasons(reasons: Iterable[str]) -> list[str]:
    unique: list[str] = []
    for reason in reasons:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("risk decision reasons must be non-empty strings")
        normalized = reason.strip()
        if normalized not in unique:
            unique.append(normalized)
    return unique
