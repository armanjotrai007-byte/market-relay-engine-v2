"""Validate execution metrics capture without broker or QuestDB I/O."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.common.serialization import to_json_string  # noqa: E402
from market_relay_engine.execution.alpaca_paper import AlpacaPaperResponse  # noqa: E402
from market_relay_engine.execution.execution_metrics import (  # noqa: E402
    ORDER_SUBMIT_LATENCY_METRIC_NAME,
    build_latency_metric_payload,
    build_order_event_payload,
    capture_order_submission_result,
)
from market_relay_engine.execution.order_manager import OrderIntentSide  # noqa: E402
from market_relay_engine.execution.position_state import ResolvedOrderIntent  # noqa: E402


def main() -> int:
    started_at = datetime(2026, 1, 2, 14, 30, 0, tzinfo=UTC)
    completed_at = started_at + timedelta(milliseconds=100)
    intent = ResolvedOrderIntent(
        ticker="AAPL",
        side=OrderIntentSide.BUY,
        quantity=1.5,
        source_signal_id="signal_check_execution_metrics",
        risk_decision_id="risk_decision_check_execution_metrics",
        reason="offline_check",
    )
    success_response = AlpacaPaperResponse(
        success=True,
        status_code=200,
        broker_order_id="paper_order_check_execution_metrics",
        raw_response={
            "id": "paper_order_check_execution_metrics",
            "client_order_id": "client_order_check_execution_metrics",
            "type": "market",
            "time_in_force": "day",
        },
        error_message=None,
    )
    success_result = capture_order_submission_result(
        intent=intent,
        response=success_response,
        submit_started_at=started_at,
        submit_completed_at=completed_at,
        local_order_id="local_order_check_execution_metrics",
        arrival_midprice=189.25,
        trace_id="trace_check_execution_metrics",
    )
    assert success_result.success is True
    assert success_result.latency_ms == 100.0
    assert success_result.client_order_id == "client_order_check_execution_metrics"
    assert success_result.arrival_midprice == 189.25

    order_payload = build_order_event_payload(success_result)
    assert "arrival_midprice" not in order_payload
    assert order_payload["expected_price"] == 189.25
    assert order_payload["broker_order_id"] == "paper_order_check_execution_metrics"
    to_json_string(order_payload)

    latency_payload = build_latency_metric_payload(success_result)
    assert "metric_name" not in latency_payload
    assert latency_payload["component"] == "execution"
    assert latency_payload["source"] == "alpaca_paper"
    assert latency_payload["event_type"] == ORDER_SUBMIT_LATENCY_METRIC_NAME
    assert latency_payload["latency_ms"] == 100.0
    to_json_string(latency_payload)

    failed_response = AlpacaPaperResponse(
        success=False,
        status_code=422,
        broker_order_id=None,
        raw_response={"message": "qty is invalid"},
        error_message="qty is invalid",
    )
    failed_result = capture_order_submission_result(
        intent=intent,
        response=failed_response,
        submit_started_at=started_at,
        submit_completed_at=started_at,
    )
    assert failed_result.success is False
    assert failed_result.latency_ms == 0.0
    assert failed_result.status_code == 422
    assert failed_result.error_message == "qty is invalid"

    print("Execution metrics check PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
