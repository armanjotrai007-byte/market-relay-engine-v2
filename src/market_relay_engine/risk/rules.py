"""Simple deterministic risk-rule helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from market_relay_engine.contracts.model import ModelSignal
from market_relay_engine.contracts.risk import RiskDecisionType
from market_relay_engine.market_data.cost_model import CostEstimate
from market_relay_engine.risk.decisions import (
    REASON_CONFIDENCE_TOO_LOW,
    REASON_CONSECUTIVE_LOSS_LIMIT_HIT,
    REASON_COST_ESTIMATE_TICKER_MISMATCH,
    REASON_COST_NOT_PROFITABLE_AFTER_COSTS,
    REASON_DAILY_LOSS_LIMIT_HIT,
    REASON_DUPLICATE_OR_CONFLICTING_ORDER,
    REASON_ELEVATED_CONTEXT_RISK,
    REASON_EVENT_WINDOW_ACTIVE,
    REASON_HIGH_CONTEXT_RISK,
    REASON_LATENCY_TOO_HIGH,
    REASON_MAX_OPEN_POSITIONS_HIT,
    REASON_MAX_POSITION_PER_SYMBOL_HIT,
    REASON_MISSING_COST_ESTIMATE,
    REASON_MISSING_LATENCY_MS,
    REASON_MISSING_MARKET_DATA_TIME,
    REASON_MISSING_SPREAD,
    REASON_SPREAD_BPS_TOO_WIDE,
    REASON_SPREAD_DOLLARS_TOO_WIDE,
    REASON_STALE_MARKET_DATA,
)


RuleBlock = tuple[str, dict[str, Any]]
ContextRuleResult = tuple[RiskDecisionType, bool, str, dict[str, Any], float | None]


def check_cost_estimate(
    *,
    signal: ModelSignal,
    cost_estimate: CostEstimate | None,
    reject_if_expected_edge_below_cost: bool,
) -> RuleBlock | None:
    if not reject_if_expected_edge_below_cost:
        return None
    if cost_estimate is None:
        return (
            REASON_MISSING_COST_ESTIMATE,
            {
                "rule": REASON_MISSING_COST_ESTIMATE,
                "reject_if_expected_edge_below_cost": True,
            },
        )
    if cost_estimate.ticker != signal.ticker:
        return (
            REASON_COST_ESTIMATE_TICKER_MISMATCH,
            {
                "rule": REASON_COST_ESTIMATE_TICKER_MISMATCH,
                "signal_ticker": signal.ticker,
                "cost_estimate_ticker": cost_estimate.ticker,
            },
        )
    if not cost_estimate.profitable_after_costs:
        return (
            REASON_COST_NOT_PROFITABLE_AFTER_COSTS,
            {
                "rule": REASON_COST_NOT_PROFITABLE_AFTER_COSTS,
                "profitable_after_costs": False,
                "net_expected_edge_bps": cost_estimate.net_expected_edge_bps,
                "min_edge_bps": cost_estimate.min_edge_bps,
            },
        )
    return None


def check_confidence(signal: ModelSignal, config: Any) -> RuleBlock | None:
    if signal.confidence < config.min_model_confidence:
        return (
            REASON_CONFIDENCE_TOO_LOW,
            {
                "rule": REASON_CONFIDENCE_TOO_LOW,
                "actual_confidence": signal.confidence,
                "min_model_confidence": config.min_model_confidence,
            },
        )
    return None


def check_spread(market: Any, config: Any) -> RuleBlock | None:
    if market.spread_dollars is None and market.spread_bps is None:
        return (REASON_MISSING_SPREAD, {"rule": REASON_MISSING_SPREAD})
    if (
        market.spread_dollars is not None
        and market.spread_dollars > config.max_spread_dollars
    ):
        return (
            REASON_SPREAD_DOLLARS_TOO_WIDE,
            {
                "rule": REASON_SPREAD_DOLLARS_TOO_WIDE,
                "actual_spread_dollars": market.spread_dollars,
                "max_spread_dollars": config.max_spread_dollars,
            },
        )
    if market.spread_bps is not None and market.spread_bps > config.max_spread_bps:
        return (
            REASON_SPREAD_BPS_TOO_WIDE,
            {
                "rule": REASON_SPREAD_BPS_TOO_WIDE,
                "actual_spread_bps": market.spread_bps,
                "max_spread_bps": config.max_spread_bps,
            },
        )
    return None


def check_latency(market: Any, config: Any) -> RuleBlock | None:
    if market.latency_ms is None:
        return (REASON_MISSING_LATENCY_MS, {"rule": REASON_MISSING_LATENCY_MS})
    if market.latency_ms > config.max_latency_ms:
        return (
            REASON_LATENCY_TOO_HIGH,
            {
                "rule": REASON_LATENCY_TOO_HIGH,
                "actual_latency_ms": market.latency_ms,
                "max_latency_ms": config.max_latency_ms,
            },
        )
    return None


def check_staleness(
    *,
    market: Any,
    evaluation_time: datetime,
    config: Any,
) -> RuleBlock | None:
    if market.market_data_time is None:
        return (
            REASON_MISSING_MARKET_DATA_TIME,
            {"rule": REASON_MISSING_MARKET_DATA_TIME},
        )
    market_data_age_seconds = (
        evaluation_time - market.market_data_time
    ).total_seconds()
    if market_data_age_seconds > config.stale_market_data_seconds:
        return (
            REASON_STALE_MARKET_DATA,
            {
                "rule": REASON_STALE_MARKET_DATA,
                "actual_market_data_age_seconds": market_data_age_seconds,
                "stale_market_data_seconds": config.stale_market_data_seconds,
            },
        )
    return None


def check_daily_limits(account: Any, config: Any) -> RuleBlock | None:
    if account.daily_loss_dollars >= config.max_daily_loss_dollars:
        return (
            REASON_DAILY_LOSS_LIMIT_HIT,
            {
                "rule": REASON_DAILY_LOSS_LIMIT_HIT,
                "actual_daily_loss_dollars": account.daily_loss_dollars,
                "max_daily_loss_dollars": config.max_daily_loss_dollars,
            },
        )
    if account.consecutive_losses >= config.max_consecutive_losses:
        return (
            REASON_CONSECUTIVE_LOSS_LIMIT_HIT,
            {
                "rule": REASON_CONSECUTIVE_LOSS_LIMIT_HIT,
                "actual_consecutive_losses": account.consecutive_losses,
                "max_consecutive_losses": config.max_consecutive_losses,
            },
        )
    return None


def check_portfolio_placeholders(portfolio: Any, config: Any) -> RuleBlock | None:
    if portfolio.duplicate_or_conflicting_order:
        return (
            REASON_DUPLICATE_OR_CONFLICTING_ORDER,
            {"rule": REASON_DUPLICATE_OR_CONFLICTING_ORDER},
        )
    if portfolio.open_positions >= config.max_open_positions:
        return (
            REASON_MAX_OPEN_POSITIONS_HIT,
            {
                "rule": REASON_MAX_OPEN_POSITIONS_HIT,
                "actual_open_positions": portfolio.open_positions,
                "max_open_positions": config.max_open_positions,
            },
        )
    if portfolio.symbol_position_exists and config.max_position_per_symbol <= 1:
        return (
            REASON_MAX_POSITION_PER_SYMBOL_HIT,
            {
                "rule": REASON_MAX_POSITION_PER_SYMBOL_HIT,
                "symbol_position_exists": True,
                "max_position_per_symbol": config.max_position_per_symbol,
            },
        )
    return None


def check_context_risk(context: Any, config: Any) -> ContextRuleResult | None:
    if context.event_window_active and config.event_blocking_enabled:
        return (
            RiskDecisionType.BLOCK,
            False,
            REASON_EVENT_WINDOW_ACTIVE,
            {
                "rule": REASON_EVENT_WINDOW_ACTIVE,
                "event_blocking_enabled": True,
            },
            None,
        )
    if context.high_risk_context_active and config.block_on_ai_high_risk:
        return (
            RiskDecisionType.BLOCK,
            False,
            REASON_HIGH_CONTEXT_RISK,
            {
                "rule": REASON_HIGH_CONTEXT_RISK,
                "block_on_ai_high_risk": True,
            },
            None,
        )
    if context.elevated_risk_context_active and config.reduce_size_on_ai_elevated_risk:
        return (
            RiskDecisionType.REDUCE_SIZE,
            True,
            REASON_ELEVATED_CONTEXT_RISK,
            {
                "rule": REASON_ELEVATED_CONTEXT_RISK,
                "reduce_size_factor": config.reduce_size_factor_on_elevated_risk,
            },
            config.reduce_size_factor_on_elevated_risk,
        )
    return None
