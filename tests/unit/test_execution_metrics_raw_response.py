from __future__ import annotations

from datetime import UTC, datetime

from market_relay_engine.execution.alpaca_paper import AlpacaPaperResponse
from market_relay_engine.execution.execution_metrics import capture_order_submission_result
from market_relay_engine.execution.order_manager import OrderIntentSide
from market_relay_engine.execution.position_state import ResolvedOrderIntent


def test_literal_raw_response_none_falls_back_without_crashing() -> None:
    timestamp = datetime(2026, 1, 2, 14, 30, 0, tzinfo=UTC)
    intent = ResolvedOrderIntent(
        ticker="AAPL",
        side=OrderIntentSide.BUY,
        quantity=1.5,
        source_signal_id="signal_raw_none",
        risk_decision_id="risk_raw_none",
        reason="test",
    )
    response = AlpacaPaperResponse(
        success=True,
        status_code=200,
        broker_order_id="paper_order_raw_none",
        raw_response=None,
        error_message=None,
    )

    result = capture_order_submission_result(
        intent=intent,
        response=response,
        submit_started_at=timestamp,
        submit_completed_at=timestamp,
    )

    assert result.client_order_id == "signal_raw_none"
    assert result.latency_ms == 0.0
