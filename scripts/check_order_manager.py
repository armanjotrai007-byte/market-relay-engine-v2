"""Validate local order manager behavior without external services."""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.contracts.model import SignalSide  # noqa: E402
from market_relay_engine.contracts.risk import RiskDecisionType  # noqa: E402
from market_relay_engine.execution.order_manager import (  # noqa: E402
    OrderIntentSide,
    OrderManagerConfig,
    OrderManagerState,
    build_order_intent,
    reserve_order_intent,
)
from tests.fixtures.model_signals import make_model_signal  # noqa: E402
from tests.fixtures.risk_decisions import make_risk_decision  # noqa: E402


def main() -> int:
    config = OrderManagerConfig(default_quantity=1, max_open_orders_per_symbol=5)

    approved_signal = make_model_signal(signal=SignalSide.BUY, index=1)
    approved_decision = make_risk_decision(
        model_signal=approved_signal,
        ticker=approved_signal.ticker,
        decision=RiskDecisionType.APPROVE,
        approved=True,
    )
    approved_result = build_order_intent(
        signal=approved_signal,
        decision=approved_decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        config=config,
    )
    assert approved_result.allowed is True
    assert approved_result.intent is not None
    assert approved_result.intent.side is OrderIntentSide.BUY
    assert approved_result.intent.quantity == 10

    reduced_signal = make_model_signal(signal=SignalSide.BUY, index=2)
    reduced_decision = make_risk_decision(
        model_signal=reduced_signal,
        ticker=reduced_signal.ticker,
        decision=RiskDecisionType.REDUCE_SIZE,
        approved=True,
        reduce_size_factor=0.5,
    )
    reduced_result = build_order_intent(
        signal=reduced_signal,
        decision=reduced_decision,
        risk_log_succeeded=True,
        desired_quantity=1.5,
        config=config,
    )
    assert reduced_result.allowed is True
    assert reduced_result.intent is not None
    assert reduced_result.intent.quantity == 0.75

    blocked_signal = make_model_signal(signal=SignalSide.BUY, index=3)
    blocked_decision = make_risk_decision(
        model_signal=blocked_signal,
        ticker=blocked_signal.ticker,
        decision=RiskDecisionType.BLOCK,
        approved=False,
    )
    blocked_result = build_order_intent(
        signal=blocked_signal,
        decision=blocked_decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        config=config,
    )
    assert blocked_result.allowed is False
    assert blocked_result.intent is None

    mismatch_signal = make_model_signal(signal=SignalSide.BUY, index=30)
    stale_signal = make_model_signal(signal=SignalSide.BUY, index=31)
    mismatch_decision = make_risk_decision(
        model_signal=stale_signal,
        ticker=stale_signal.ticker,
        decision=RiskDecisionType.APPROVE,
        approved=True,
    )
    mismatch_result = build_order_intent(
        signal=mismatch_signal,
        decision=mismatch_decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        config=config,
    )
    assert mismatch_result.allowed is False
    assert mismatch_result.reasons == ["risk_decision_signal_mismatch"]

    exit_signal = make_model_signal(signal=SignalSide.EXIT, index=4)
    exit_decision = make_risk_decision(
        model_signal=exit_signal,
        ticker=exit_signal.ticker,
        decision=RiskDecisionType.EXIT,
        approved=True,
    )
    exit_result = build_order_intent(
        signal=exit_signal,
        decision=exit_decision,
        risk_log_succeeded=False,
        desired_quantity=0,
        config=config,
    )
    assert exit_result.allowed is True
    assert exit_result.intent is not None
    assert exit_result.intent.side is OrderIntentSide.CLOSE_POSITION
    assert exit_result.intent.quantity is None

    state = OrderManagerState()
    reserve_order_intent(state=state, intent=approved_result.intent)
    duplicate_result = build_order_intent(
        signal=approved_signal,
        decision=approved_decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        state=state,
        config=config,
    )
    assert duplicate_result.allowed is False
    assert duplicate_result.reasons == ["duplicate_signal_id"]

    print("Order manager check PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
