"""Local execution-result capture helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any

from market_relay_engine.common.ids import new_record_id
from market_relay_engine.common.time import ensure_timezone_aware_utc
from market_relay_engine.contracts.base import DEFAULT_SCHEMA_VERSION
from market_relay_engine.execution.alpaca_paper import AlpacaPaperResponse
from market_relay_engine.execution.order_manager import OrderIntentSide
from market_relay_engine.execution.position_state import ResolvedOrderIntent


ORDER_SUBMIT_LATENCY_METRIC_NAME = "alpaca_order_submit_latency_ms"
ORDER_EVENTS_PAYLOAD_KEYS = frozenset(
    {
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
)
LATENCY_METRICS_PAYLOAD_KEYS = frozenset(
    {
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
)


class ExecutionCaptureError(ValueError):
    """Raised for invalid local execution-capture inputs."""


@dataclass(frozen=True, kw_only=True)
class OrderSubmissionResult:
    """Local record linking one resolved intent to one Alpaca paper response."""

    ticker: str
    side: str
    quantity: float
    order_type: str
    time_in_force: str
    submit_started_at: datetime
    submit_completed_at: datetime
    latency_ms: float
    success: bool
    local_order_id: str | None = None
    client_order_id: str | None = None
    broker_order_id: str | None = None
    status_code: int | None = None
    error_message: str | None = None
    paper_trading: bool = True
    source_signal_id: str | None = None
    risk_decision_id: str | None = None
    trace_id: str | None = None
    arrival_midprice: float | None = None

    def __post_init__(self) -> None:
        started_at = _utc_datetime(self.submit_started_at, "submit_started_at")
        completed_at = _utc_datetime(self.submit_completed_at, "submit_completed_at")
        if completed_at < started_at:
            raise ExecutionCaptureError(
                "submit_completed_at must be greater than or equal to submit_started_at"
            )

        object.__setattr__(self, "ticker", _required_string(self.ticker, "ticker").upper())
        object.__setattr__(self, "side", _required_string(self.side, "side"))
        object.__setattr__(
            self,
            "quantity",
            _non_negative_finite_float(self.quantity, "quantity"),
        )
        object.__setattr__(
            self,
            "order_type",
            _required_string(self.order_type, "order_type"),
        )
        object.__setattr__(
            self,
            "time_in_force",
            _required_string(self.time_in_force, "time_in_force"),
        )
        object.__setattr__(self, "submit_started_at", started_at)
        object.__setattr__(self, "submit_completed_at", completed_at)
        object.__setattr__(
            self,
            "latency_ms",
            _non_negative_finite_float(self.latency_ms, "latency_ms"),
        )
        object.__setattr__(self, "success", _bool(self.success, "success"))
        object.__setattr__(
            self,
            "local_order_id",
            _optional_string(self.local_order_id, "local_order_id"),
        )
        object.__setattr__(
            self,
            "client_order_id",
            _optional_string(self.client_order_id, "client_order_id"),
        )
        object.__setattr__(
            self,
            "broker_order_id",
            _optional_string(self.broker_order_id, "broker_order_id"),
        )
        object.__setattr__(
            self,
            "status_code",
            _optional_status_code(self.status_code),
        )
        object.__setattr__(
            self,
            "error_message",
            _optional_string(self.error_message, "error_message"),
        )
        object.__setattr__(
            self,
            "paper_trading",
            _bool(self.paper_trading, "paper_trading"),
        )
        object.__setattr__(
            self,
            "source_signal_id",
            _optional_string(self.source_signal_id, "source_signal_id"),
        )
        object.__setattr__(
            self,
            "risk_decision_id",
            _optional_string(self.risk_decision_id, "risk_decision_id"),
        )
        object.__setattr__(self, "trace_id", _optional_string(self.trace_id, "trace_id"))
        object.__setattr__(
            self,
            "arrival_midprice",
            _optional_positive_finite_float(
                self.arrival_midprice,
                "arrival_midprice",
            ),
        )


def capture_order_submission_result(
    *,
    intent: ResolvedOrderIntent,
    response: AlpacaPaperResponse,
    submit_started_at: datetime,
    submit_completed_at: datetime,
    client_order_id: str | None = None,
    local_order_id: str | None = None,
    paper_trading: bool = True,
    trace_id: str | None = None,
    arrival_midprice: float | None = None,
) -> OrderSubmissionResult:
    """Capture one local order-submission result without broker or ledger I/O."""
    started_at = _utc_datetime(submit_started_at, "submit_started_at")
    completed_at = _utc_datetime(submit_completed_at, "submit_completed_at")
    if completed_at < started_at:
        raise ExecutionCaptureError(
            "submit_completed_at must be greater than or equal to submit_started_at"
        )
    latency_ms = (completed_at - started_at).total_seconds() * 1000.0

    raw_response = _raw_response_dict(response)
    source_signal_id = _optional_string(
        getattr(intent, "source_signal_id", None),
        "source_signal_id",
    )
    intent_order_id = _optional_string(getattr(intent, "order_id", None), "intent.order_id")
    resolved_local_order_id = _first_present_string(local_order_id, intent_order_id)
    resolved_client_order_id = _first_present_string(
        client_order_id,
        _safe_raw_string(raw_response, "client_order_id"),
        local_order_id,
        intent_order_id,
        source_signal_id,
    )
    side = _side_for_intent(intent)

    return OrderSubmissionResult(
        local_order_id=resolved_local_order_id,
        client_order_id=resolved_client_order_id,
        broker_order_id=response.broker_order_id,
        ticker=_required_string(getattr(intent, "ticker", None), "intent.ticker"),
        side=side,
        quantity=_non_negative_finite_float(
            getattr(intent, "quantity", None),
            "intent.quantity",
        ),
        order_type=_order_type_for_intent_and_response(intent, raw_response),
        time_in_force=_time_in_force_for_intent_and_response(intent, raw_response),
        submit_started_at=started_at,
        submit_completed_at=completed_at,
        latency_ms=latency_ms,
        success=response.success,
        status_code=response.status_code,
        error_message=response.error_message,
        paper_trading=paper_trading,
        source_signal_id=source_signal_id,
        risk_decision_id=_optional_string(
            getattr(intent, "risk_decision_id", None),
            "risk_decision_id",
        ),
        trace_id=trace_id,
        arrival_midprice=arrival_midprice,
    )


def build_order_event_payload(result: OrderSubmissionResult) -> dict[str, object]:
    """Build a schema-compatible future ``order_events`` payload."""
    payload: dict[str, object] = {
        "order_time": result.submit_completed_at,
        "write_time": None,
        "order_id": _payload_order_id(result),
        "ticker": result.ticker,
        "side": result.side,
        "order_type": result.order_type,
        "quantity": result.quantity,
        "status": "SUBMITTED" if result.success else "REJECTED",
        "expected_price": result.arrival_midprice,
        "submitted_price": None,
        "broker": "alpaca",
        "broker_order_id": result.broker_order_id,
        "paper_trading": result.paper_trading,
        "model_signal_id": result.source_signal_id,
        "risk_decision_id": result.risk_decision_id,
        "feature_snapshot_id": None,
        "run_id": None,
        "session_id": None,
        "schema_version": DEFAULT_SCHEMA_VERSION,
        "trace_id": result.trace_id,
    }
    unknown_keys = set(payload) - ORDER_EVENTS_PAYLOAD_KEYS
    if unknown_keys:
        raise ExecutionCaptureError(f"order event payload has unknown keys: {sorted(unknown_keys)}")
    return payload


def build_latency_metric_payload(result: OrderSubmissionResult) -> dict[str, object]:
    """Build a schema-compatible future ``latency_metrics`` payload."""
    payload: dict[str, object] = {
        "measured_time": result.submit_completed_at,
        "write_time": None,
        "latency_metric_id": new_record_id("latency_metric"),
        "component": "execution",
        "source": "alpaca_paper",
        "latency_ms": result.latency_ms,
        "ticker": result.ticker,
        "event_type": ORDER_SUBMIT_LATENCY_METRIC_NAME,
        "run_id": None,
        "session_id": None,
        "schema_version": DEFAULT_SCHEMA_VERSION,
        "trace_id": result.trace_id,
    }
    unknown_keys = set(payload) - LATENCY_METRICS_PAYLOAD_KEYS
    if unknown_keys:
        raise ExecutionCaptureError(f"latency metric payload has unknown keys: {sorted(unknown_keys)}")
    return payload


def _utc_datetime(value: datetime, field_name: str) -> datetime:
    try:
        return ensure_timezone_aware_utc(value)
    except (TypeError, ValueError) as exc:
        raise ExecutionCaptureError(f"{field_name} must be timezone-aware UTC") from exc


def _required_string(value: object, field_name: str) -> str:
    if hasattr(value, "value"):
        value = getattr(value, "value")
    if not isinstance(value, str) or not value.strip():
        raise ExecutionCaptureError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_string(value: object, field_name: str) -> str | None:
    if hasattr(value, "value"):
        value = getattr(value, "value")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExecutionCaptureError(f"{field_name} must be a string or None")
    text = value.strip()
    return text or None


def _metadata_string(value: object) -> str | None:
    if hasattr(value, "value"):
        value = getattr(value, "value")
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _first_present_string(*values: object) -> str | None:
    for value in values:
        text = _metadata_string(value)
        if text is not None:
            return text
    return None


def _non_negative_finite_float(value: object, field_name: str) -> float:
    if isinstance(value, bool):
        raise ExecutionCaptureError(f"{field_name} must be numeric, not bool")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ExecutionCaptureError(f"{field_name} must be numeric") from exc
    if not math.isfinite(number):
        raise ExecutionCaptureError(f"{field_name} must be finite")
    if number < 0:
        raise ExecutionCaptureError(f"{field_name} must be non-negative")
    return number


def _optional_positive_finite_float(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ExecutionCaptureError(f"{field_name} must be numeric, not bool")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ExecutionCaptureError(f"{field_name} must be numeric") from exc
    if not math.isfinite(number):
        raise ExecutionCaptureError(f"{field_name} must be finite")
    if number <= 0:
        raise ExecutionCaptureError(f"{field_name} must be positive")
    return number


def _bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ExecutionCaptureError(f"{field_name} must be bool")
    return value


def _optional_status_code(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionCaptureError("status_code must be an integer or None")
    return value


def _raw_response_dict(response: AlpacaPaperResponse) -> dict[str, object]:
    raw_response = getattr(response, "raw_response", None)
    if isinstance(raw_response, dict):
        return raw_response
    return {}


def _safe_raw_string(raw_response: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = raw_response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _side_for_intent(intent: object) -> str:
    side = _required_string(getattr(intent, "side", None), "intent.side")
    if side.upper() == OrderIntentSide.CLOSE_POSITION.value:
        raise ExecutionCaptureError(
            "capture_order_submission_result expects a resolved BUY/SELL intent"
        )
    return side


def _order_type_for_intent_and_response(
    intent: object,
    raw_response: dict[str, object],
) -> str:
    return _first_present_string(
        getattr(intent, "order_type", None),
        _safe_raw_string(raw_response, "order_type", "type"),
        "market",
    ) or "market"


def _time_in_force_for_intent_and_response(
    intent: object,
    raw_response: dict[str, object],
) -> str:
    return _first_present_string(
        getattr(intent, "time_in_force", None),
        _safe_raw_string(raw_response, "time_in_force"),
        "day",
    ) or "day"


def _payload_order_id(result: OrderSubmissionResult) -> str | None:
    return _first_present_string(
        result.local_order_id,
        result.client_order_id,
        result.source_signal_id,
    )
