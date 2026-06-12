"""Deterministic local fake paper execution wiring check."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import math

from market_relay_engine.contracts.execution import FillEvent
from market_relay_engine.contracts.model import ModelSignal, SignalSide
from market_relay_engine.contracts.risk import RiskDecision, RiskDecisionType
from market_relay_engine.execution.alpaca_paper import AlpacaPaperResponse
from market_relay_engine.execution.execution_metrics import (
    OrderSubmissionResult,
    capture_order_submission_result,
)
from market_relay_engine.execution.fill_reconciliation import (
    BrokerPositionSnapshot,
    PositionReconciliationResult,
    apply_fill_and_reconcile,
    fill_event_from_alpaca_fill_payload,
)
from market_relay_engine.execution.order_manager import (
    OrderIntent,
    OrderManagerConfig,
    OrderManagerState,
    build_order_intent,
    release_open_order,
    reserve_order_intent,
)
from market_relay_engine.execution.position_state import (
    PortfolioState,
    PositionUpdateResult,
    ResolvedOrderIntent,
    resolve_close_position_intent,
)


FIXED_SUBMIT_STARTED_AT = datetime(2026, 1, 2, 14, 30, 0, tzinfo=UTC)
FIXED_SUBMIT_COMPLETED_AT = FIXED_SUBMIT_STARTED_AT + timedelta(milliseconds=100)
FIXED_FILL_TIME = datetime(2026, 1, 2, 14, 30, 1, tzinfo=UTC)


class FakePaperLoopError(ValueError):
    """Raised for invalid local fake paper loop inputs."""


@dataclass(frozen=True, kw_only=True)
class FakePaperLoopConfig:
    """Configuration for one deterministic local fake paper trade cycle."""

    ticker: str = "AAPL"
    side: str = "BUY"
    quantity: float = 1.0
    arrival_midprice: float = 100.00
    fill_price: float = 100.02
    order_type: str = "MARKET"
    time_in_force: str = "day"
    source_signal_id: str = "signal_fake_pr23"
    risk_decision_id: str = "risk_fake_pr23"
    local_order_id: str = "order_fake_pr23"
    client_order_id: str = "client_order_fake_pr23"
    broker_order_id: str = "broker_order_fake_pr23"
    execution_id: str = "fill_fake_pr23"
    trace_id: str = "trace_fake_pr23"

    def __post_init__(self) -> None:
        ticker = _required_string(self.ticker, "ticker").upper()
        side = _required_string(self.side, "side").upper()
        if side not in {"BUY", "SELL"}:
            raise FakePaperLoopError("side must be BUY or SELL")

        quantity = _positive_finite_float(self.quantity, "quantity")
        arrival_midprice = _positive_finite_float(
            self.arrival_midprice,
            "arrival_midprice",
        )
        fill_price = _positive_finite_float(self.fill_price, "fill_price")
        order_type = _required_string(self.order_type, "order_type").upper()
        if order_type != "MARKET":
            raise FakePaperLoopError("order_type must be MARKET")

        object.__setattr__(self, "ticker", ticker)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "arrival_midprice", arrival_midprice)
        object.__setattr__(self, "fill_price", fill_price)
        object.__setattr__(self, "order_type", order_type)
        object.__setattr__(
            self,
            "time_in_force",
            _required_string(self.time_in_force, "time_in_force"),
        )
        for field_name in (
            "source_signal_id",
            "risk_decision_id",
            "local_order_id",
            "client_order_id",
            "broker_order_id",
            "execution_id",
            "trace_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_string(getattr(self, field_name), field_name),
            )


@dataclass(frozen=True, kw_only=True)
class FakePaperLoopResult:
    """Result from one deterministic fake paper execution cycle."""

    order_intent: OrderIntent
    resolved_intent: ResolvedOrderIntent
    order_submission_result: OrderSubmissionResult
    fill_event: FillEvent
    position_update: PositionUpdateResult
    reconciliation: PositionReconciliationResult | None
    final_portfolio: PortfolioState
    order_state: OrderManagerState


def run_fake_paper_cycle(
    config: FakePaperLoopConfig | None = None,
    portfolio: PortfolioState | None = None,
    reconcile: bool = True,
) -> FakePaperLoopResult:
    """Run one deterministic local fake paper trade cycle without external I/O."""
    config = config or FakePaperLoopConfig()
    portfolio = portfolio or PortfolioState()
    if not isinstance(portfolio, PortfolioState):
        raise FakePaperLoopError("portfolio must be a PortfolioState")

    order_state = OrderManagerState()
    signal = _fake_model_signal(config)
    decision = _fake_risk_decision(config)
    manager_result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=config.quantity,
        state=order_state,
        config=OrderManagerConfig(
            default_quantity=config.quantity,
            max_open_orders_per_symbol=1,
        ),
    )
    if not manager_result.allowed or manager_result.intent is None:
        raise FakePaperLoopError(
            "fake order intent was blocked: " + ",".join(manager_result.reasons)
        )

    intent = dataclasses.replace(
        manager_result.intent,
        order_type=config.order_type,
        time_in_force=config.time_in_force,
    )
    reserve_order_intent(state=order_state, intent=intent)
    reserved_order_id = _reserved_order_id_from_state(order_state, intent)

    starting_quantity = _current_quantity(portfolio, config.ticker)
    resolved_intent = resolve_close_position_intent(intent=intent, portfolio=portfolio)
    order_result = capture_order_submission_result(
        intent=resolved_intent,
        response=_mock_alpaca_response(config),
        submit_started_at=FIXED_SUBMIT_STARTED_AT,
        submit_completed_at=FIXED_SUBMIT_COMPLETED_AT,
        local_order_id=config.local_order_id,
        client_order_id=config.client_order_id,
        trace_id=config.trace_id,
        arrival_midprice=config.arrival_midprice,
    )
    fill_event = fill_event_from_alpaca_fill_payload(
        payload=_fake_fill_payload(config),
        order_result=order_result,
    )
    broker_position = None
    if reconcile:
        signed_fill_delta = config.quantity if config.side == "BUY" else -config.quantity
        expected_broker_quantity = starting_quantity + signed_fill_delta
        broker_position = BrokerPositionSnapshot(
            ticker=config.ticker,
            quantity=expected_broker_quantity,
        )

    processing_result = apply_fill_and_reconcile(
        portfolio=portfolio,
        fill_event=fill_event,
        broker_position=broker_position,
    )
    released = release_open_order(state=order_state, order_id=reserved_order_id)
    if not released:
        raise FakePaperLoopError("reserved fake order was not released")

    return FakePaperLoopResult(
        order_intent=intent,
        resolved_intent=resolved_intent,
        order_submission_result=order_result,
        fill_event=fill_event,
        position_update=processing_result.position_update,
        reconciliation=processing_result.reconciliation,
        final_portfolio=portfolio,
        order_state=order_state,
    )


def _fake_model_signal(config: FakePaperLoopConfig) -> ModelSignal:
    return ModelSignal(
        signal_time=FIXED_SUBMIT_STARTED_AT,
        ticker=config.ticker,
        signal=SignalSide(config.side),
        confidence=1.0,
        raw_score=1.0 if config.side == "BUY" else -1.0,
        model_version="fake_paper_loop_v1",
        calibration_version="fake_paper_loop_v1",
        feature_version="fake_paper_loop_v1",
        feature_snapshot_id="feature_fake_pr23",
        signal_id=config.source_signal_id,
        trace_id=config.trace_id,
    )


def _fake_risk_decision(config: FakePaperLoopConfig) -> RiskDecision:
    return RiskDecision(
        decision_time=FIXED_SUBMIT_STARTED_AT,
        ticker=config.ticker,
        model_signal_id=config.source_signal_id,
        decision=RiskDecisionType.APPROVE,
        approved=True,
        risk_version="fake_paper_loop_v1",
        reasons=["fake_paper_loop_approved"],
        risk_decision_id=config.risk_decision_id,
        trace_id=config.trace_id,
    )


def _mock_alpaca_response(config: FakePaperLoopConfig) -> AlpacaPaperResponse:
    return AlpacaPaperResponse(
        success=True,
        status_code=200,
        broker_order_id=config.broker_order_id,
        raw_response={
            "id": config.broker_order_id,
            "client_order_id": config.client_order_id,
            "symbol": config.ticker,
            "side": config.side.lower(),
            "type": config.order_type.lower(),
            "time_in_force": config.time_in_force,
        },
        error_message=None,
    )


def _fake_fill_payload(config: FakePaperLoopConfig) -> dict[str, object]:
    return {
        "execution_id": config.execution_id,
        "symbol": config.ticker,
        "side": config.side.lower(),
        "qty": str(config.quantity),
        "price": str(config.fill_price),
        "transaction_time": FIXED_FILL_TIME.isoformat().replace("+00:00", "Z"),
        "status": "filled",
    }


def _reserved_order_id_from_state(
    order_state: OrderManagerState,
    intent: OrderIntent,
) -> str:
    matches = [
        open_order.order_id
        for open_order in order_state.open_orders
        if open_order.source_signal_id == intent.source_signal_id
    ]
    if not matches:
        raise FakePaperLoopError("reserved fake order was not found in order state")
    return matches[-1]


def _current_quantity(portfolio: PortfolioState, ticker: str) -> float:
    position = portfolio.get_position(ticker)
    return position.quantity if position is not None else 0.0


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FakePaperLoopError(f"{field_name} must be a non-empty string")
    return value.strip()


def _positive_finite_float(value: object, field_name: str) -> float:
    if isinstance(value, bool):
        raise FakePaperLoopError(f"{field_name} must be numeric, not bool")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise FakePaperLoopError(f"{field_name} must be numeric") from exc
    if not math.isfinite(number):
        raise FakePaperLoopError(f"{field_name} must be finite")
    if number <= 0:
        raise FakePaperLoopError(f"{field_name} must be positive")
    return number
