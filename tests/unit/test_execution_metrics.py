from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from market_relay_engine.common.serialization import to_json_string
from market_relay_engine.contracts.execution import OrderType
from market_relay_engine.execution.alpaca_paper import AlpacaPaperResponse
from market_relay_engine.execution.execution_metrics import (
    LATENCY_METRICS_PAYLOAD_KEYS,
    ORDER_EVENTS_PAYLOAD_KEYS,
    ORDER_SUBMIT_LATENCY_METRIC_NAME,
    ExecutionCaptureError,
    OrderSubmissionResult,
    build_latency_metric_payload,
    build_order_event_payload,
    capture_order_submission_result,
)
from market_relay_engine.execution.order_manager import OrderIntentSide


STARTED_AT = datetime(2026, 1, 2, 14, 30, 0, tzinfo=UTC)
COMPLETED_AT = STARTED_AT + timedelta(milliseconds=100)
ORDER_EVENT_KEYS = {
    "order_time",
    "write_time",
    "order_id",
    "ticker",
    "side",
    "order_type",
    "quantity",
    "status",
    "expected_price",
    "submitted_price",
    "broker",
    "broker_order_id",
    "paper_trading",
    "model_signal_id",
    "risk_decision_id",
    "feature_snapshot_id",
    "run_id",
    "session_id",
    "schema_version",
    "trace_id",
}
LATENCY_METRIC_KEYS = {
    "measured_time",
    "write_time",
    "latency_metric_id",
    "component",
    "source",
    "latency_ms",
    "ticker",
    "event_type",
    "run_id",
    "session_id",
    "schema_version",
    "trace_id",
}


@dataclass(frozen=True, kw_only=True)
class FlexibleResolvedIntent:
    ticker: str = "AAPL"
    side: OrderIntentSide | str = OrderIntentSide.BUY
    quantity: float = 1.5
    source_signal_id: str | None = "signal_1"
    risk_decision_id: str | None = "risk_decision_1"
    reason: str = "test"
    order_id: str | None = None
    order_type: OrderType | str | None = None
    time_in_force: str | None = None


def test_order_submission_result_accepts_successful_result() -> None:
    result = _result(success=True, broker_order_id="paper_order_1")

    assert result.success is True
    assert result.broker_order_id == "paper_order_1"
    assert result.status_code == 200
    assert result.ticker == "AAPL"


def test_order_submission_result_accepts_failed_result() -> None:
    result = _result(
        success=False,
        broker_order_id=None,
        status_code=422,
        error_message="qty is invalid",
    )

    assert result.success is False
    assert result.status_code == 422
    assert result.error_message == "qty is invalid"


def test_latency_calculation_uses_total_seconds_milliseconds() -> None:
    result = capture_order_submission_result(
        intent=_intent(),
        response=_success_response(),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
    )

    assert result.latency_ms == 100.0


def test_same_timestamp_produces_zero_latency() -> None:
    result = capture_order_submission_result(
        intent=_intent(),
        response=_success_response(),
        submit_started_at=STARTED_AT,
        submit_completed_at=STARTED_AT,
    )

    assert result.latency_ms == 0.0


def test_reversed_timestamps_fail_validation() -> None:
    with pytest.raises(ExecutionCaptureError, match="submit_completed_at"):
        capture_order_submission_result(
            intent=_intent(),
            response=_success_response(),
            submit_started_at=COMPLETED_AT,
            submit_completed_at=STARTED_AT,
        )


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ExecutionCaptureError, match="timezone-aware"):
        capture_order_submission_result(
            intent=_intent(),
            response=_success_response(),
            submit_started_at=datetime(2026, 1, 2, 14, 30, 0),
            submit_completed_at=COMPLETED_AT,
        )


@pytest.mark.parametrize("quantity", [float("nan"), float("inf"), -1.0])
def test_invalid_quantity_rejected(quantity: float) -> None:
    with pytest.raises(ExecutionCaptureError, match="quantity"):
        capture_order_submission_result(
            intent=_intent(quantity=quantity),
            response=_success_response(),
            submit_started_at=STARTED_AT,
            submit_completed_at=COMPLETED_AT,
        )


@pytest.mark.parametrize("latency_ms", [float("nan"), float("inf"), -0.1])
def test_invalid_latency_rejected(latency_ms: float) -> None:
    with pytest.raises(ExecutionCaptureError, match="latency_ms"):
        _result(latency_ms=latency_ms)


def test_empty_ticker_rejected() -> None:
    with pytest.raises(ExecutionCaptureError, match="ticker"):
        capture_order_submission_result(
            intent=_intent(ticker=""),
            response=_success_response(),
            submit_started_at=STARTED_AT,
            submit_completed_at=COMPLETED_AT,
        )


def test_success_result_preserves_broker_order_id() -> None:
    result = capture_order_submission_result(
        intent=_intent(),
        response=_success_response(broker_order_id="broker_order_123"),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
    )

    assert result.broker_order_id == "broker_order_123"


def test_failure_result_preserves_status_code_and_error_message() -> None:
    result = capture_order_submission_result(
        intent=_intent(),
        response=_failed_response(status_code=403, error_message="forbidden"),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
    )

    assert result.success is False
    assert result.status_code == 403
    assert result.error_message == "forbidden"


def test_capture_pulls_source_and_risk_ids_from_intent() -> None:
    result = capture_order_submission_result(
        intent=_intent(source_signal_id="signal_capture", risk_decision_id="risk_capture"),
        response=_success_response(),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
    )

    assert result.source_signal_id == "signal_capture"
    assert result.risk_decision_id == "risk_capture"


def test_explicit_client_order_id_wins() -> None:
    result = capture_order_submission_result(
        intent=_intent(order_id="intent_order", source_signal_id="signal_1"),
        response=_success_response(raw_response={"client_order_id": "raw_client"}),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
        client_order_id="explicit_client",
        local_order_id="local_order",
    )

    assert result.client_order_id == "explicit_client"


def test_raw_client_order_id_wins_when_explicit_missing() -> None:
    result = capture_order_submission_result(
        intent=_intent(order_id="intent_order", source_signal_id="signal_1"),
        response=_success_response(raw_response={"client_order_id": "raw_client"}),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
        local_order_id="local_order",
    )

    assert result.client_order_id == "raw_client"


def test_client_order_id_uses_intent_order_id_before_local_order_id() -> None:
    result = capture_order_submission_result(
        intent=_intent(order_id="intent_order", source_signal_id="signal_1"),
        response=_success_response(raw_response={}),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
        local_order_id="local_order",
    )

    assert result.client_order_id == "intent_order"
    assert result.local_order_id == "local_order"


def test_client_order_id_uses_source_signal_id_before_local_order_id() -> None:
    result = capture_order_submission_result(
        intent=_intent(source_signal_id="signal_fallback"),
        response=_success_response(raw_response={}),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
        local_order_id="local_order",
    )

    assert result.client_order_id == "signal_fallback"
    assert result.local_order_id == "local_order"


def test_client_order_id_uses_local_order_id_only_as_final_fallback() -> None:
    result = capture_order_submission_result(
        intent=_intent(source_signal_id=None),
        response=_success_response(raw_response={}),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
        local_order_id="local_order",
    )

    assert result.client_order_id == "local_order"


def test_raw_response_none_works() -> None:
    result = capture_order_submission_result(
        intent=_intent(source_signal_id="signal_none"),
        response=_success_response(raw_response=None),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
    )

    assert result.client_order_id == "signal_none"


@pytest.mark.parametrize("raw_response", [["client_order_id"], "client_order_id"])
def test_raw_response_non_dict_does_not_crash(raw_response: object) -> None:
    result = capture_order_submission_result(
        intent=_intent(order_id="intent_order"),
        response=AlpacaPaperResponse(
            success=True,
            status_code=200,
            broker_order_id="broker_order_1",
            raw_response=raw_response,  # type: ignore[arg-type]
            error_message=None,
        ),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
    )

    assert result.client_order_id == "intent_order"


@pytest.mark.parametrize(
    "raw_response",
    [
        {},
        {"client_order_id": ""},
        {"client_order_id": "   "},
        {"client_order_id": 123},
    ],
)
def test_raw_response_invalid_client_order_id_falls_back(raw_response: dict[str, object]) -> None:
    result = capture_order_submission_result(
        intent=_intent(order_id="intent_order", source_signal_id="signal_1"),
        response=_success_response(raw_response=raw_response),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
        local_order_id="local_order",
    )

    assert result.client_order_id == "intent_order"


def test_raw_response_is_not_stored_or_exposed() -> None:
    result = capture_order_submission_result(
        intent=_intent(),
        response=_success_response(
            raw_response={"client_order_id": "raw_client", "secret": "api-secret"}
        ),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
    )

    assert not hasattr(result, "raw_response")
    assert "api-secret" not in repr(result)


def test_arrival_midprice_is_preserved() -> None:
    result = capture_order_submission_result(
        intent=_intent(),
        response=_success_response(),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
        arrival_midprice=189.25,
    )

    assert result.arrival_midprice == 189.25


def test_missing_arrival_midprice_allowed() -> None:
    result = capture_order_submission_result(
        intent=_intent(),
        response=_success_response(),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
    )

    assert result.arrival_midprice is None


@pytest.mark.parametrize("arrival_midprice", [float("nan"), float("inf"), 0.0, -1.0])
def test_invalid_arrival_midprice_rejected(arrival_midprice: float) -> None:
    with pytest.raises(ExecutionCaptureError, match="arrival_midprice"):
        capture_order_submission_result(
            intent=_intent(),
            response=_success_response(),
            submit_started_at=STARTED_AT,
            submit_completed_at=COMPLETED_AT,
            arrival_midprice=arrival_midprice,
        )


def test_intent_order_type_string_is_normalized_and_time_in_force_is_used() -> None:
    result = capture_order_submission_result(
        intent=_intent(order_type="market", time_in_force="gtc"),
        response=_success_response(raw_response={"type": "limit", "time_in_force": "day"}),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
    )

    assert result.order_type == OrderType.MARKET.value
    assert result.time_in_force == "gtc"


def test_intent_order_type_enum_is_used_when_present() -> None:
    result = capture_order_submission_result(
        intent=_intent(order_type=OrderType.MARKET),
        response=_success_response(raw_response={"type": "limit"}),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
    )

    assert result.order_type == OrderType.MARKET.value


def test_raw_response_market_order_type_is_normalized_when_intent_missing() -> None:
    result = capture_order_submission_result(
        intent=_intent(),
        response=_success_response(raw_response={"type": "market", "time_in_force": "day"}),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
    )

    assert result.order_type == OrderType.MARKET.value
    assert result.time_in_force == "day"


def test_raw_response_order_type_key_wins_over_type_key() -> None:
    result = capture_order_submission_result(
        intent=_intent(),
        response=_success_response(raw_response={"order_type": "MARKET", "type": "limit"}),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
    )

    assert result.order_type == OrderType.MARKET.value


def test_missing_order_type_falls_back_to_market_contract_value() -> None:
    result = capture_order_submission_result(
        intent=_intent(),
        response=_success_response(raw_response={}),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
    )

    assert result.order_type == OrderType.MARKET.value
    assert result.time_in_force == "day"


@pytest.mark.parametrize(
    ("intent_order_type", "raw_response"),
    [
        ("limit", {}),
        (None, {"type": "limit"}),
    ],
)
def test_unsupported_order_type_raises(
    intent_order_type: str | None,
    raw_response: dict[str, object],
) -> None:
    with pytest.raises(ExecutionCaptureError, match="unsupported order_type"):
        capture_order_submission_result(
            intent=_intent(order_type=intent_order_type),
            response=_success_response(raw_response=raw_response),
            submit_started_at=STARTED_AT,
            submit_completed_at=COMPLETED_AT,
        )


def test_unresolved_close_position_intent_is_rejected() -> None:
    with pytest.raises(ExecutionCaptureError, match="resolved BUY/SELL"):
        capture_order_submission_result(
            intent=_intent(side=OrderIntentSide.CLOSE_POSITION),
            response=_success_response(),
            submit_started_at=STARTED_AT,
            submit_completed_at=COMPLETED_AT,
        )


def test_order_event_payload_uses_submit_started_at_for_order_time() -> None:
    payload = build_order_event_payload(_result())

    assert payload["order_time"] == STARTED_AT


def test_order_event_payload_maps_arrival_midprice_to_expected_price() -> None:
    result = capture_order_submission_result(
        intent=_intent(order_id="local_order_1"),
        response=_success_response(broker_order_id="broker_order_1"),
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
        arrival_midprice=189.25,
        trace_id="trace_1",
    )

    payload = build_order_event_payload(result)

    assert "arrival_midprice" not in payload
    assert payload["order_time"] == STARTED_AT
    assert payload["expected_price"] == 189.25
    assert payload["submitted_price"] is None
    assert payload["order_id"] == "local_order_1"
    assert payload["order_type"] == OrderType.MARKET.value
    assert payload["broker"] == "alpaca"
    assert payload["broker_order_id"] == "broker_order_1"
    assert payload["model_signal_id"] == "signal_1"
    assert payload["risk_decision_id"] == "risk_decision_1"
    assert payload["trace_id"] == "trace_1"


def test_order_event_payload_contains_only_schema_writer_compatible_keys() -> None:
    payload = build_order_event_payload(_result(arrival_midprice=189.25))

    assert set(payload) == ORDER_EVENT_KEYS
    assert set(payload) == ORDER_EVENTS_PAYLOAD_KEYS
    assert "client_order_id" not in payload
    assert "status_code" not in payload
    assert "error_message" not in payload
    assert "submit_started_at" not in payload
    assert "submit_completed_at" not in payload


def test_failed_order_event_payload_uses_rejected_status() -> None:
    payload = build_order_event_payload(_result(success=False, status_code=422))

    assert payload["status"] == "REJECTED"


def test_order_event_payload_is_project_json_serializable() -> None:
    payload = build_order_event_payload(_result(arrival_midprice=189.25))

    serialized = to_json_string(payload)

    assert "expected_price" in serialized
    assert "arrival_midprice" not in serialized


def test_latency_payload_uses_schema_compatible_event_type() -> None:
    payload = build_latency_metric_payload(_result())

    assert "metric_name" not in payload
    assert payload["component"] == "execution"
    assert payload["source"] == "alpaca_paper"
    assert payload["event_type"] == ORDER_SUBMIT_LATENCY_METRIC_NAME
    assert payload["latency_ms"] == 100.0
    assert payload["ticker"] == "AAPL"
    assert payload["trace_id"] == "trace_1"
    assert payload["measured_time"] == COMPLETED_AT
    assert isinstance(payload["latency_metric_id"], str)
    assert str(payload["latency_metric_id"]).startswith("latency_metric_")


def test_latency_payload_contains_only_schema_writer_compatible_keys() -> None:
    payload = build_latency_metric_payload(_result())

    assert set(payload) == LATENCY_METRIC_KEYS
    assert set(payload) == LATENCY_METRICS_PAYLOAD_KEYS
    assert "broker_order_id" not in payload
    assert "client_order_id" not in payload
    assert "source_signal_id" not in payload
    assert "risk_decision_id" not in payload
    assert "metric_name" not in payload


def test_latency_payload_is_project_json_serializable() -> None:
    payload = build_latency_metric_payload(_result())

    serialized = to_json_string(payload)

    assert ORDER_SUBMIT_LATENCY_METRIC_NAME in serialized
    assert "metric_name" not in serialized


def test_execution_metrics_source_keeps_pr21_scope_small() -> None:
    source = Path("src/market_relay_engine/execution/execution_metrics.py").read_text(
        encoding="utf-8"
    )

    assert "requests" not in source
    assert "AlpacaPaperClient" not in source
    assert "market_relay_engine.questdb" not in source
    assert "market_relay_engine.model" not in source
    assert "market_relay_engine.ai_context" not in source
    assert "market_relay_engine.context" not in source
    assert "async def" not in source
    assert "retry" not in source.lower()


def _intent(**overrides: Any) -> FlexibleResolvedIntent:
    values = {
        "ticker": "AAPL",
        "side": OrderIntentSide.BUY,
        "quantity": 1.5,
        "source_signal_id": "signal_1",
        "risk_decision_id": "risk_decision_1",
        "reason": "test",
        "order_id": None,
        "order_type": None,
        "time_in_force": None,
    }
    values.update(overrides)
    return FlexibleResolvedIntent(**values)


def _success_response(
    *,
    broker_order_id: str | None = "paper_order_1",
    raw_response: object = None,
) -> AlpacaPaperResponse:
    if raw_response is None:
        raw_response = {"id": broker_order_id, "status": "accepted"}
    return AlpacaPaperResponse(
        success=True,
        status_code=200,
        broker_order_id=broker_order_id,
        raw_response=raw_response,  # type: ignore[arg-type]
        error_message=None,
    )


def _failed_response(
    *,
    status_code: int = 422,
    error_message: str = "qty is invalid",
    raw_response: object = None,
) -> AlpacaPaperResponse:
    if raw_response is None:
        raw_response = {"message": error_message}
    return AlpacaPaperResponse(
        success=False,
        status_code=status_code,
        broker_order_id=None,
        raw_response=raw_response,  # type: ignore[arg-type]
        error_message=error_message,
    )


def _result(
    *,
    success: bool = True,
    broker_order_id: str | None = "paper_order_1",
    status_code: int | None = 200,
    error_message: str | None = None,
    latency_ms: float = 100.0,
    arrival_midprice: float | None = None,
) -> OrderSubmissionResult:
    return OrderSubmissionResult(
        local_order_id="local_order_1",
        client_order_id="client_order_1",
        broker_order_id=broker_order_id,
        ticker="AAPL",
        side="BUY",
        quantity=1.5,
        order_type=OrderType.MARKET.value,
        time_in_force="day",
        submit_started_at=STARTED_AT,
        submit_completed_at=COMPLETED_AT,
        latency_ms=latency_ms,
        success=success,
        status_code=status_code,
        error_message=error_message,
        paper_trading=True,
        source_signal_id="signal_1",
        risk_decision_id="risk_decision_1",
        trace_id="trace_1",
        arrival_midprice=arrival_midprice,
    )
