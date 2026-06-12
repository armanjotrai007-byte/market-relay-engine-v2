"""Local fill conversion and position reconciliation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math

from market_relay_engine.common.time import (
    ensure_timezone_aware_utc,
    parse_utc_iso,
    utc_now,
)
from market_relay_engine.contracts.execution import FillEvent, OrderSide
from market_relay_engine.contracts.system import SystemHealthEvent
from market_relay_engine.execution.execution_metrics import OrderSubmissionResult
from market_relay_engine.execution.position_state import (
    PortfolioState,
    PositionUpdateResult,
    apply_fill_to_portfolio,
)


class FillReconciliationError(ValueError):
    """Raised for invalid local fill-reconciliation inputs."""


@dataclass(frozen=True, kw_only=True)
class BrokerPositionSnapshot:
    """Signed broker position snapshot from a broker payload."""

    ticker: str
    quantity: float
    average_price: float | None = None
    broker_position_id: str | None = None
    source: str = "alpaca_paper"

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", _normalize_ticker(self.ticker))
        object.__setattr__(self, "quantity", _finite_float(self.quantity, "quantity"))
        object.__setattr__(
            self,
            "average_price",
            _optional_non_negative_float(self.average_price, "average_price"),
        )
        object.__setattr__(
            self,
            "broker_position_id",
            _optional_string(self.broker_position_id, "broker_position_id"),
        )
        object.__setattr__(self, "source", _required_string(self.source, "source"))


@dataclass(frozen=True, kw_only=True)
class PositionReconciliationResult:
    """Local-vs-broker position comparison result."""

    ticker: str
    matched: bool
    local_quantity: float
    broker_quantity: float
    quantity_difference: float
    tolerance: float
    reasons: list[str]


@dataclass(frozen=True, kw_only=True)
class FillProcessingResult:
    """Result of applying one fill and optionally reconciling position state."""

    fill_event: FillEvent
    position_update: PositionUpdateResult
    reconciliation: PositionReconciliationResult | None = None


def broker_position_snapshot_from_alpaca_payload(
    payload: dict[str, object],
) -> BrokerPositionSnapshot:
    """Build a signed broker position snapshot from an Alpaca-like payload."""
    if not isinstance(payload, dict):
        raise FillReconciliationError("payload must be a dictionary")

    ticker = _payload_required_string(payload, ("symbol", "ticker"), "ticker")
    raw_quantity = _payload_required_value(payload, ("qty", "quantity"), "quantity")
    quantity = _finite_float(raw_quantity, "quantity")
    side = _payload_optional_string(payload, ("side",), "side")
    if side is not None:
        normalized_side = side.lower()
        if normalized_side == "short":
            quantity = -abs(quantity)
        elif normalized_side == "long":
            quantity = abs(quantity)
        else:
            raise FillReconciliationError("side must be long or short")

    raw_average_price = _payload_optional_value(
        payload,
        ("avg_entry_price", "average_price"),
    )
    average_price = _optional_non_negative_float(raw_average_price, "average_price")

    return BrokerPositionSnapshot(
        ticker=ticker,
        quantity=quantity,
        average_price=average_price,
        broker_position_id=_payload_optional_string(
            payload,
            ("id", "asset_id", "position_id"),
            "broker_position_id",
        ),
    )


def fill_event_from_alpaca_fill_payload(
    *,
    payload: dict[str, object],
    order_result: OrderSubmissionResult,
    expected_price: float | None = None,
    trace_id: str | None = None,
) -> FillEvent:
    """Convert an execution-level Alpaca-like fill payload into a FillEvent."""
    if not isinstance(payload, dict):
        raise FillReconciliationError("payload must be a dictionary")

    fill_id = _payload_required_string(
        payload,
        ("execution_id", "activity_id", "id", "trade_id"),
        "fill_id",
    )
    side = _normalize_order_side(
        _payload_or_nested_order_required_string(payload, ("side",), "side")
    )
    quantity = _positive_float(
        _payload_required_value(payload, ("qty", "quantity"), "quantity"),
        "quantity",
    )
    fill_price = _positive_float(
        _payload_required_value(payload, ("price", "fill_price", "avg_price"), "fill_price"),
        "fill_price",
    )
    fill_time = _payload_datetime(
        _payload_required_value(
            payload,
            ("transaction_time", "filled_at", "fill_time", "timestamp"),
            "fill_time",
        ),
        "fill_time",
    )
    resolved_expected_price = _resolve_expected_price(expected_price, order_result)
    slippage, slippage_bps = _calculate_slippage(
        side=side,
        fill_price=fill_price,
        expected_price=resolved_expected_price,
    )
    ticker = _payload_or_nested_order_optional_string(
        payload,
        ("symbol", "ticker"),
        "ticker",
    ) or order_result.ticker

    return FillEvent(
        fill_time=fill_time,
        order_id=_order_correlation_id(order_result),
        ticker=_normalize_ticker(ticker),
        side=side,
        quantity=quantity,
        fill_price=fill_price,
        fill_id=fill_id,
        expected_price=resolved_expected_price,
        slippage=slippage,
        slippage_bps=slippage_bps,
        broker_status=_payload_optional_string(payload, ("status", "type"), "broker_status")
        or "filled",
        broker_fill_id=fill_id,
        model_signal_id=order_result.source_signal_id,
        risk_decision_id=order_result.risk_decision_id,
        trace_id=_optional_string(trace_id, "trace_id") or order_result.trace_id,
    )


def reconcile_position(
    *,
    portfolio: PortfolioState,
    broker_position: BrokerPositionSnapshot,
    tolerance: float = 1e-9,
) -> PositionReconciliationResult:
    """Compare local signed quantity against a broker position snapshot."""
    if not isinstance(portfolio, PortfolioState):
        raise FillReconciliationError("portfolio must be a PortfolioState")
    tolerance = _non_negative_float(tolerance, "tolerance")
    ticker = _normalize_ticker(broker_position.ticker)
    local_position = portfolio.get_position(ticker)
    local_quantity = local_position.quantity if local_position is not None else 0.0
    broker_quantity = broker_position.quantity
    quantity_difference = local_quantity - broker_quantity
    matched = abs(quantity_difference) <= tolerance
    reasons = ["position_quantity_match" if matched else "position_quantity_mismatch"]
    return PositionReconciliationResult(
        ticker=ticker,
        matched=matched,
        local_quantity=local_quantity,
        broker_quantity=broker_quantity,
        quantity_difference=quantity_difference,
        tolerance=tolerance,
        reasons=reasons,
    )


def apply_fill_and_reconcile(
    *,
    portfolio: PortfolioState,
    fill_event: FillEvent,
    broker_position: BrokerPositionSnapshot | None = None,
    tolerance: float = 1e-9,
) -> FillProcessingResult:
    """Apply a fill locally and optionally compare against a broker snapshot."""
    position_update = apply_fill_to_portfolio(portfolio=portfolio, fill=fill_event)
    reconciliation = None
    if broker_position is not None:
        reconciliation = reconcile_position(
            portfolio=portfolio,
            broker_position=broker_position,
            tolerance=tolerance,
        )
    return FillProcessingResult(
        fill_event=fill_event,
        position_update=position_update,
        reconciliation=reconciliation,
    )


def build_position_reconciliation_health_event(
    result: PositionReconciliationResult,
    *,
    event_time: datetime | None = None,
    trace_id: str | None = None,
) -> SystemHealthEvent:
    """Build a local health event for future reconciliation monitoring."""
    status = "OK" if result.matched else "WARNING"
    message = (
        f"ticker={result.ticker} local_quantity={result.local_quantity} "
        f"broker_quantity={result.broker_quantity} "
        f"quantity_difference={result.quantity_difference} "
        f"reasons={','.join(result.reasons)}"
    )
    return SystemHealthEvent(
        event_time=ensure_timezone_aware_utc(event_time) if event_time else utc_now(),
        component="position_reconciliation",
        status=status,
        message=message,
        trace_id=_optional_string(trace_id, "trace_id"),
    )


def _payload_required_value(
    payload: dict[str, object],
    keys: tuple[str, ...],
    field_name: str,
) -> object:
    value = _payload_optional_value(payload, keys)
    if value is None:
        raise FillReconciliationError(f"{field_name} is required")
    return value


def _payload_optional_value(payload: dict[str, object], keys: tuple[str, ...]) -> object | None:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _payload_required_string(
    payload: dict[str, object],
    keys: tuple[str, ...],
    field_name: str,
) -> str:
    return _required_string(_payload_required_value(payload, keys, field_name), field_name)


def _payload_optional_string(
    payload: dict[str, object],
    keys: tuple[str, ...],
    field_name: str,
) -> str | None:
    return _optional_string(_payload_optional_value(payload, keys), field_name)


def _nested_order_payload(payload: dict[str, object]) -> dict[str, object]:
    if "order" not in payload:
        return {}
    order = payload["order"]
    if not isinstance(order, dict):
        raise FillReconciliationError("order must be a dictionary when provided")
    return order


def _payload_or_nested_order_optional_string(
    payload: dict[str, object],
    keys: tuple[str, ...],
    field_name: str,
) -> str | None:
    nested_order = _nested_order_payload(payload)
    top_level = _payload_optional_string(payload, keys, field_name)
    if top_level is not None:
        return top_level
    if not nested_order:
        return None
    return _payload_optional_string(nested_order, keys, field_name)


def _payload_or_nested_order_required_string(
    payload: dict[str, object],
    keys: tuple[str, ...],
    field_name: str,
) -> str:
    value = _payload_or_nested_order_optional_string(payload, keys, field_name)
    if value is None:
        raise FillReconciliationError(f"{field_name} is required")
    return value


def _order_correlation_id(order_result: OrderSubmissionResult) -> str:
    for value in (
        order_result.local_order_id,
        order_result.client_order_id,
        order_result.source_signal_id,
    ):
        text = _optional_string(value, "order_id")
        if text is not None:
            return text
    raise FillReconciliationError("order_result must include order correlation")


def _resolve_expected_price(
    expected_price: float | None,
    order_result: OrderSubmissionResult,
) -> float | None:
    for value in (expected_price, order_result.arrival_midprice):
        if value is None:
            continue
        try:
            return _positive_float(value, "expected_price")
        except FillReconciliationError:
            return None
    return None


def _calculate_slippage(
    *,
    side: OrderSide,
    fill_price: float,
    expected_price: float | None,
) -> tuple[float | None, float | None]:
    if expected_price is None:
        return None, None
    slippage = (
        fill_price - expected_price
        if side is OrderSide.BUY
        else expected_price - fill_price
    )
    return slippage, (slippage / expected_price) * 10000.0


def _normalize_order_side(value: object) -> OrderSide:
    text = _required_string(value, "side").upper()
    try:
        return OrderSide(text)
    except ValueError as exc:
        raise FillReconciliationError("side must be BUY or SELL") from exc


def _payload_datetime(value: object, field_name: str) -> datetime:
    try:
        if isinstance(value, datetime):
            return ensure_timezone_aware_utc(value)
        if isinstance(value, str):
            return parse_utc_iso(value)
    except (TypeError, ValueError) as exc:
        raise FillReconciliationError(f"{field_name} must be timezone-aware UTC") from exc
    raise FillReconciliationError(f"{field_name} must be a datetime or ISO string")


def _normalize_ticker(value: object) -> str:
    return _required_string(value, "ticker").upper()


def _required_string(value: object, field_name: str) -> str:
    if hasattr(value, "value"):
        value = getattr(value, "value")
    if not isinstance(value, str) or not value.strip():
        raise FillReconciliationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_string(value: object, field_name: str) -> str | None:
    if hasattr(value, "value"):
        value = getattr(value, "value")
    if value is None:
        return None
    if not isinstance(value, str):
        raise FillReconciliationError(f"{field_name} must be a string or None")
    text = value.strip()
    return text or None


def _finite_float(value: object, field_name: str) -> float:
    if isinstance(value, bool):
        raise FillReconciliationError(f"{field_name} must be numeric, not bool")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise FillReconciliationError(f"{field_name} must be numeric") from exc
    if not math.isfinite(number):
        raise FillReconciliationError(f"{field_name} must be finite")
    return number


def _non_negative_float(value: object, field_name: str) -> float:
    number = _finite_float(value, field_name)
    if number < 0:
        raise FillReconciliationError(f"{field_name} must be non-negative")
    return number


def _optional_non_negative_float(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    return _non_negative_float(value, field_name)


def _positive_float(value: object, field_name: str) -> float:
    number = _finite_float(value, field_name)
    if number <= 0:
        raise FillReconciliationError(f"{field_name} must be positive")
    return number
