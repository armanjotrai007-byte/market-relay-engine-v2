"""Composed fake end-to-end scenario fixtures for tests."""

from __future__ import annotations

from market_relay_engine.contracts.execution import OrderSide
from market_relay_engine.contracts.risk import RiskDecisionType
from tests.fixtures.context import (
    make_context_ai_event,
    make_context_flag,
    make_eia_window_indicator,
    make_sector_proxy_move_indicator,
    make_usaspending_defense_award_indicator,
)
from tests.fixtures.execution import make_fill_event, make_order_event
from tests.fixtures.feature_snapshots import make_feature_snapshot
from tests.fixtures.ids import (
    TRACE_ID_APPROVED_OIL,
    TRACE_ID_BLOCKED_DEFENSE,
    TRACE_ID_LATENCY_WARNING,
    TRACE_ID_REDUCED_SIZE,
    TRACE_ID_STALE_CONTEXT,
    stable_record_id,
)
from tests.fixtures.ledger import make_latency_metric, make_trade_outcome
from tests.fixtures.market_records import (
    make_defense_market_record,
    make_market_quote_record,
    make_market_trade_record,
)
from tests.fixtures.model_signals import make_buy_model_signal, make_sell_model_signal
from tests.fixtures.risk_decisions import (
    make_approve_risk_decision,
    make_block_risk_decision,
    make_reduce_size_risk_decision,
    make_risk_decision,
)
from tests.fixtures.system import (
    make_healthy_system_health_event,
    make_warning_system_health_event,
)


SCENARIO_KEYS = (
    "market_records",
    "feature_snapshot",
    "model_signal",
    "risk_decision",
    "context_indicators",
    "context_events",
    "context_flags",
    "order_event",
    "fill_event",
    "trade_outcome",
    "latency_metric",
    "system_health_event",
)


def _scenario(**values: object) -> dict[str, object]:
    return {key: values.get(key) for key in SCENARIO_KEYS}


def approved_oil_trade_scenario() -> dict[str, object]:
    """Return a fake approved oil trade scenario."""
    trace_id = TRACE_ID_APPROVED_OIL
    feature_snapshot = make_feature_snapshot(ticker="XOM", index=1, trace_id=trace_id)
    model_signal = make_buy_model_signal(
        ticker="XOM",
        index=1,
        feature_snapshot=feature_snapshot,
        trace_id=trace_id,
    )
    risk_decision = make_approve_risk_decision(
        ticker="XOM",
        index=1,
        model_signal=model_signal,
        context_snapshot_id=stable_record_id("context_snapshot", 1),
        trace_id=trace_id,
    )
    order_event = make_order_event(ticker="XOM", index=1, trace_id=trace_id)
    fill_event = make_fill_event(order_event=order_event, index=1, trace_id=trace_id)
    trade_outcome = make_trade_outcome(
        model_signal=model_signal,
        order_event=order_event,
        ticker="XOM",
        index=1,
        trace_id=trace_id,
    )
    return _scenario(
        market_records=[
            make_market_trade_record(ticker="XOM", index=1, trace_id=trace_id),
            make_market_quote_record(ticker="XOM", index=2, trace_id=trace_id),
        ],
        feature_snapshot=feature_snapshot,
        model_signal=model_signal,
        risk_decision=risk_decision,
        context_indicators=[
            make_sector_proxy_move_indicator(index=1, trace_id=trace_id),
        ],
        context_events=[
            make_context_ai_event(index=1, trace_id=trace_id),
        ],
        context_flags=[],
        order_event=order_event,
        fill_event=fill_event,
        trade_outcome=trade_outcome,
        latency_metric=make_latency_metric(index=1, trace_id=trace_id),
        system_health_event=make_healthy_system_health_event(index=1, trace_id=trace_id),
    )


def blocked_defense_trade_scenario() -> dict[str, object]:
    """Return a fake blocked defense trade scenario."""
    trace_id = TRACE_ID_BLOCKED_DEFENSE
    feature_snapshot = make_feature_snapshot(
        ticker="LMT",
        index=2,
        trace_id=trace_id,
        midprice=472.35,
        spread=0.40,
        spread_bps=8.47,
        return_1m=-0.0008,
    )
    model_signal = make_sell_model_signal(
        ticker="LMT",
        index=2,
        feature_snapshot=feature_snapshot,
        confidence=0.49,
        raw_score=-0.19,
        trace_id=trace_id,
    )
    risk_decision = make_block_risk_decision(
        ticker="LMT",
        index=2,
        model_signal=model_signal,
        context_snapshot_id=stable_record_id("context_snapshot", 2),
        trace_id=trace_id,
    )
    return _scenario(
        market_records=[
            make_defense_market_record(ticker="LMT", index=2, trace_id=trace_id),
        ],
        feature_snapshot=feature_snapshot,
        model_signal=model_signal,
        risk_decision=risk_decision,
        context_indicators=[
            make_usaspending_defense_award_indicator(index=2, trace_id=trace_id),
        ],
        context_events=[
            make_context_ai_event(
                affected_tickers=["LMT"],
                affected_sector="defense",
                risk_level="high",
                index=2,
                trace_id=trace_id,
            ),
        ],
        context_flags=[
            make_context_flag(
                ticker="LMT",
                sector="defense",
                severity="high",
                index=2,
                trace_id=trace_id,
            ),
        ],
        order_event=None,
        fill_event=None,
        trade_outcome=None,
        latency_metric=None,
        system_health_event=make_healthy_system_health_event(index=2, trace_id=trace_id),
    )


def reduced_size_context_risk_scenario() -> dict[str, object]:
    """Return a fake reduced-size scenario caused by context risk."""
    trace_id = TRACE_ID_REDUCED_SIZE
    feature_snapshot = make_feature_snapshot(ticker="RTX", index=3, trace_id=trace_id)
    model_signal = make_buy_model_signal(
        ticker="RTX",
        index=3,
        feature_snapshot=feature_snapshot,
        confidence=0.67,
        raw_score=0.22,
        trace_id=trace_id,
    )
    risk_decision = make_reduce_size_risk_decision(
        ticker="RTX",
        index=3,
        model_signal=model_signal,
        context_snapshot_id=stable_record_id("context_snapshot", 3),
        trace_id=trace_id,
    )
    order_event = make_order_event(
        ticker="RTX",
        quantity=50.0,
        expected_price=102.25,
        submitted_price=102.27,
        index=3,
        trace_id=trace_id,
    )
    fill_event = make_fill_event(
        order_event=order_event,
        fill_price=102.29,
        slippage=0.04,
        index=3,
        trace_id=trace_id,
    )
    return _scenario(
        market_records=[
            make_defense_market_record(ticker="RTX", index=3, trace_id=trace_id),
        ],
        feature_snapshot=feature_snapshot,
        model_signal=model_signal,
        risk_decision=risk_decision,
        context_indicators=[
            make_eia_window_indicator(index=3, trace_id=trace_id),
        ],
        context_events=[
            make_context_ai_event(
                affected_tickers=["RTX"],
                affected_sector="defense",
                risk_level="elevated",
                index=3,
                trace_id=trace_id,
            ),
        ],
        context_flags=[
            make_context_flag(
                ticker="RTX",
                sector="defense",
                severity="warning",
                index=3,
                trace_id=trace_id,
            ),
        ],
        order_event=order_event,
        fill_event=fill_event,
        trade_outcome=make_trade_outcome(
            model_signal=model_signal,
            order_event=order_event,
            ticker="RTX",
            index=3,
            trace_id=trace_id,
        ),
        latency_metric=make_latency_metric(index=3, trace_id=trace_id),
        system_health_event=make_healthy_system_health_event(index=3, trace_id=trace_id),
    )


def latency_slippage_warning_scenario() -> dict[str, object]:
    """Return a fake latency and slippage warning scenario."""
    trace_id = TRACE_ID_LATENCY_WARNING
    feature_snapshot = make_feature_snapshot(ticker="CVX", index=4, trace_id=trace_id)
    model_signal = make_buy_model_signal(
        ticker="CVX",
        index=4,
        feature_snapshot=feature_snapshot,
        confidence=0.7,
        raw_score=0.29,
        trace_id=trace_id,
    )
    risk_decision = make_approve_risk_decision(
        ticker="CVX",
        index=4,
        model_signal=model_signal,
        context_snapshot_id=stable_record_id("context_snapshot", 4),
        trace_id=trace_id,
    )
    order_event = make_order_event(
        ticker="CVX",
        expected_price=162.10,
        submitted_price=162.11,
        index=4,
        trace_id=trace_id,
    )
    fill_event = make_fill_event(
        order_event=order_event,
        fill_price=162.25,
        slippage=0.15,
        index=4,
        trace_id=trace_id,
    )
    return _scenario(
        market_records=[
            make_market_quote_record(
                ticker="CVX",
                bid_price=162.08,
                ask_price=162.12,
                index=4,
                trace_id=trace_id,
            ),
        ],
        feature_snapshot=feature_snapshot,
        model_signal=model_signal,
        risk_decision=risk_decision,
        context_indicators=[],
        context_events=[],
        context_flags=[],
        order_event=order_event,
        fill_event=fill_event,
        trade_outcome=make_trade_outcome(
            model_signal=model_signal,
            order_event=order_event,
            ticker="CVX",
            index=4,
            trace_id=trace_id,
        ),
        latency_metric=make_latency_metric(
            component="fixture_execution_path",
            latency_ms=420.0,
            index=4,
            trace_id=trace_id,
        ),
        system_health_event=make_warning_system_health_event(index=4, trace_id=trace_id),
    )


def stale_context_block_scenario() -> dict[str, object]:
    """Return a fake block scenario where context is stale or expired."""
    trace_id = TRACE_ID_STALE_CONTEXT
    feature_snapshot = make_feature_snapshot(ticker="NOC", index=5, trace_id=trace_id)
    model_signal = make_buy_model_signal(
        ticker="NOC",
        index=5,
        feature_snapshot=feature_snapshot,
        confidence=0.66,
        raw_score=0.21,
        trace_id=trace_id,
    )
    risk_decision = make_risk_decision(
        decision=RiskDecisionType.BLOCK,
        approved=False,
        ticker="NOC",
        index=5,
        model_signal=model_signal,
        reasons=["stale_context"],
        thresholds_used={"max_context_age_seconds": 900.0},
        context_snapshot_id=stable_record_id("context_snapshot", 5),
        trace_id=trace_id,
    )
    return _scenario(
        market_records=[
            make_defense_market_record(ticker="NOC", index=5, trace_id=trace_id),
        ],
        feature_snapshot=feature_snapshot,
        model_signal=model_signal,
        risk_decision=risk_decision,
        context_indicators=[
            make_usaspending_defense_award_indicator(
                index=5,
                trace_id=trace_id,
                stale=True,
            ),
        ],
        context_events=[
            make_context_ai_event(
                affected_tickers=["NOC"],
                affected_sector="defense",
                risk_level="unknown",
                index=5,
                trace_id=trace_id,
                expired=True,
            ),
        ],
        context_flags=[
            make_context_flag(
                ticker="NOC",
                sector="defense",
                flag_type="stale_context",
                severity="warning",
                index=5,
                trace_id=trace_id,
                expired=True,
            ),
        ],
        order_event=None,
        fill_event=None,
        trade_outcome=None,
        latency_metric=make_latency_metric(
            component="fixture_context_age_check",
            latency_ms=12.0,
            index=5,
            trace_id=trace_id,
        ),
        system_health_event=make_warning_system_health_event(index=5, trace_id=trace_id),
    )


def build_scenario_examples() -> list[dict[str, object]]:
    """Return all reusable fake scenario dictionaries."""
    return [
        approved_oil_trade_scenario(),
        blocked_defense_trade_scenario(),
        reduced_size_context_risk_scenario(),
        latency_slippage_warning_scenario(),
        stale_context_block_scenario(),
    ]
