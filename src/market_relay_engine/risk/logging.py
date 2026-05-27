"""Opt-in logging helpers for deterministic risk decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from market_relay_engine.contracts.model import ModelSignal
from market_relay_engine.contracts.risk import RiskDecision
from market_relay_engine.market_data.cost_model import CostEstimate
from market_relay_engine.risk.risk_filter import (
    AccountRiskInput,
    ContextRiskInput,
    MarketRiskInput,
    PortfolioRiskInput,
    RiskFilterConfig,
    evaluate_risk,
)


class RiskDecisionWriter(Protocol):
    """Minimal writer interface for risk-decision ledger sinks."""

    def write_risk_decision(
        self,
        decision: RiskDecision,
        **kwargs: Any,
    ) -> object | None:
        """Write one risk decision to a ledger-like sink."""
        ...


@dataclass(frozen=True, kw_only=True)
class RiskDecisionLogResult:
    """Result of attempting to log one RiskDecision."""

    decision: RiskDecision
    attempted: bool
    success: bool
    error_message: str | None = None


def log_risk_decision(
    decision: RiskDecision,
    writer: RiskDecisionWriter | None,
    *,
    raise_on_failure: bool = False,
    **writer_kwargs: Any,
) -> RiskDecisionLogResult:
    """Write one RiskDecision through the supplied writer."""
    if writer is None:
        raise ValueError("No writer provided to log_risk_decision")

    try:
        writer.write_risk_decision(decision, **writer_kwargs)
    except Exception as exc:
        if raise_on_failure:
            raise
        return RiskDecisionLogResult(
            decision=decision,
            attempted=True,
            success=False,
            error_message=str(exc),
        )

    return RiskDecisionLogResult(
        decision=decision,
        attempted=True,
        success=True,
    )


def evaluate_risk_and_log(
    *,
    signal: ModelSignal,
    market: MarketRiskInput,
    writer: RiskDecisionWriter | None,
    cost_estimate: CostEstimate | None = None,
    cost_estimate_id: str | None = None,
    context: ContextRiskInput | None = None,
    account: AccountRiskInput | None = None,
    portfolio: PortfolioRiskInput | None = None,
    evaluation_time: datetime,
    config: RiskFilterConfig | None = None,
    raise_on_failure: bool = False,
    **writer_kwargs: Any,
) -> RiskDecisionLogResult:
    """Evaluate risk, log the resulting decision, and return the log result."""
    decision = evaluate_risk(
        signal=signal,
        market=market,
        cost_estimate=cost_estimate,
        cost_estimate_id=cost_estimate_id,
        context=context,
        account=account,
        portfolio=portfolio,
        evaluation_time=evaluation_time,
        config=config,
    )
    return log_risk_decision(
        decision,
        writer,
        raise_on_failure=raise_on_failure,
        **writer_kwargs,
    )
