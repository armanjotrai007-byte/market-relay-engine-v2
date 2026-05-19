"""Fake risk decision fixtures for tests."""

from __future__ import annotations

from market_relay_engine.contracts.model import ModelSignal
from market_relay_engine.contracts.risk import RiskDecision, RiskDecisionType
from tests.fixtures.ids import TRACE_ID_APPROVED_OIL, stable_record_id
from tests.fixtures.model_signals import make_model_signal
from tests.fixtures.times import seconds_after_market_open


RISK_VERSION = "fixture_risk_v1"


def make_risk_decision(
    *,
    decision: RiskDecisionType = RiskDecisionType.APPROVE,
    approved: bool = True,
    ticker: str = "XOM",
    index: int = 1,
    model_signal: ModelSignal | None = None,
    reduce_size_factor: float | None = None,
    reasons: list[str] | None = None,
    thresholds_used: dict[str, object] | None = None,
    context_snapshot_id: str | None = None,
    trace_id: str = TRACE_ID_APPROVED_OIL,
) -> RiskDecision:
    """Return a fake risk decision without running risk logic."""
    model_signal = model_signal or make_model_signal(
        ticker=ticker,
        index=index,
        trace_id=trace_id,
    )
    return RiskDecision(
        decision_time=seconds_after_market_open(index + 5),
        ticker=ticker,
        model_signal_id=model_signal.signal_id,
        decision=decision,
        approved=approved,
        risk_version=RISK_VERSION,
        reduce_size_factor=reduce_size_factor,
        reasons=reasons or [],
        thresholds_used=thresholds_used
        or {
            "max_spread_bps": 10.0,
            "max_latency_ms": 250.0,
            "min_confidence": 0.55,
        },
        context_snapshot_id=context_snapshot_id,
        risk_decision_id=stable_record_id("risk_decision", index),
        trace_id=trace_id,
    )


def make_approve_risk_decision(**overrides: object) -> RiskDecision:
    """Return a fake APPROVE decision."""
    return make_risk_decision(
        decision=RiskDecisionType.APPROVE,
        approved=True,
        reasons=[],
        **overrides,
    )


def make_block_risk_decision(**overrides: object) -> RiskDecision:
    """Return a fake BLOCK decision."""
    return make_risk_decision(
        decision=RiskDecisionType.BLOCK,
        approved=False,
        reasons=["spread_too_wide", "confidence_too_low"],
        **overrides,
    )


def make_reduce_size_risk_decision(**overrides: object) -> RiskDecision:
    """Return a fake REDUCE_SIZE decision."""
    return make_risk_decision(
        decision=RiskDecisionType.REDUCE_SIZE,
        approved=True,
        reduce_size_factor=0.5,
        reasons=["eia_window", "ai_context_high_risk"],
        **overrides,
    )


def build_risk_decision_examples() -> list[RiskDecision]:
    """Return representative fake risk decisions."""
    return [
        make_approve_risk_decision(index=1),
        make_block_risk_decision(ticker="LMT", index=2),
        make_reduce_size_risk_decision(ticker="RTX", index=3),
    ]

