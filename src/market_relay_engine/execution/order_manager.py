"""Local order intent manager for risk-approved decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from market_relay_engine.common.config import load_yaml_config
from market_relay_engine.contracts.model import ModelSignal, SignalSide
from market_relay_engine.contracts.risk import RiskDecision, RiskDecisionType


class OrderIntentSide(str, Enum):
    """Local order intent side values."""

    BUY = "BUY"
    SELL = "SELL"
    CLOSE_POSITION = "CLOSE_POSITION"


@dataclass(frozen=True, kw_only=True)
class OrderManagerConfig:
    """Small configuration surface for local order-intent checks."""

    default_quantity: int | float
    max_open_orders_per_symbol: int
    require_logged_risk_decision_for_entries: bool = True
    allow_exit_when_risk_log_failed: bool = True

    def __post_init__(self) -> None:
        if self.default_quantity <= 0:
            raise ValueError("default_quantity must be positive")
        if self.max_open_orders_per_symbol < 0:
            raise ValueError("max_open_orders_per_symbol must be non-negative")

    @classmethod
    def from_yaml(cls) -> "OrderManagerConfig":
        """Load conservative defaults from existing repository config files."""
        execution_config = load_yaml_config("config/execution.yaml")
        risk_config = load_yaml_config("config/risk_limits.yaml")

        order_defaults = execution_config.get("order_defaults", {})
        position_limits = risk_config.get("position_limits", {})
        default_quantity = order_defaults.get(
            "default_quantity",
            position_limits.get("default_order_quantity", 1),
        )
        max_open_orders_per_symbol = position_limits.get("max_position_per_symbol", 1)

        return cls(
            default_quantity=default_quantity,
            max_open_orders_per_symbol=int(max_open_orders_per_symbol),
        )


@dataclass(frozen=True, kw_only=True)
class OpenOrderState:
    """In-memory placeholder for an order intent that has been reserved."""

    order_id: str
    ticker: str
    side: OrderIntentSide
    quantity: float | None
    source_signal_id: str | None
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "side", _coerce_side(self.side))


@dataclass(kw_only=True)
class OrderManagerState:
    """Minimal in-memory state used by the local order manager."""

    open_orders: list[OpenOrderState] = field(default_factory=list)
    used_signal_ids: set[str] = field(default_factory=set)

    def add_open_order(
        self,
        *,
        order_id: str,
        ticker: str,
        side: OrderIntentSide,
        quantity: float | None,
        source_signal_id: str | None,
        status: str = "reserved",
    ) -> None:
        """Add one in-memory open-order placeholder."""
        self.open_orders.append(
            OpenOrderState(
                order_id=order_id,
                ticker=ticker,
                side=side,
                quantity=quantity,
                source_signal_id=source_signal_id,
                status=status,
            )
        )

    def mark_signal_used(self, signal_id: str) -> None:
        """Remember a signal ID so it cannot produce another intent."""
        self.used_signal_ids.add(signal_id)


@dataclass(frozen=True, kw_only=True)
class OrderIntent:
    """Local intent to create an order later; this is not a broker order."""

    ticker: str
    side: OrderIntentSide
    quantity: float | None
    source_signal_id: str
    risk_decision_id: str | None
    reason: str
    order_type: str = "limit"
    time_in_force: str = "day"

    def __post_init__(self) -> None:
        object.__setattr__(self, "side", _coerce_side(self.side))


@dataclass(frozen=True, kw_only=True)
class OrderManagerResult:
    """Result of checking whether a local order intent is allowed."""

    allowed: bool
    intent: OrderIntent | None
    reasons: list[str]
    effective_quantity: float | None
    source_signal_id: str | None
    risk_decision_type: str | None


def build_order_intent(
    *,
    signal: ModelSignal,
    decision: RiskDecision,
    risk_log_succeeded: bool,
    desired_quantity: float | None = None,
    state: OrderManagerState | None = None,
    config: OrderManagerConfig | None = None,
) -> OrderManagerResult:
    """Return a local order intent only when the decision is safe to reserve."""
    state = state or OrderManagerState()
    config = config or OrderManagerConfig.from_yaml()

    if decision.model_signal_id != signal.signal_id:
        return _blocked(
            reason="risk_decision_signal_mismatch",
            signal=signal,
            decision=decision,
        )

    if decision.ticker.upper() != signal.ticker.upper():
        return _blocked(
            reason="risk_decision_ticker_mismatch",
            signal=signal,
            decision=decision,
        )

    if signal.signal_id in state.used_signal_ids:
        return _blocked(
            reason="duplicate_signal_id",
            signal=signal,
            decision=decision,
        )

    if decision.decision is RiskDecisionType.BLOCK:
        return _blocked(
            reason="risk_decision_blocked",
            signal=signal,
            decision=decision,
        )

    if decision.decision is RiskDecisionType.DO_NOTHING:
        return _blocked(
            reason="risk_decision_do_nothing",
            signal=signal,
            decision=decision,
        )

    if decision.decision is RiskDecisionType.EXIT:
        return _build_close_position_intent(
            signal=signal,
            decision=decision,
            risk_log_succeeded=risk_log_succeeded,
            state=state,
            config=config,
        )

    intent_side = _entry_side_for_signal(signal.signal)
    if intent_side is None:
        return _blocked(
            reason="signal_no_order",
            signal=signal,
            decision=decision,
        )

    if config.require_logged_risk_decision_for_entries and not risk_log_succeeded:
        return _blocked(
            reason="risk_log_failed",
            signal=signal,
            decision=decision,
        )

    requested_quantity = float(
        config.default_quantity if desired_quantity is None else desired_quantity
    )
    effective_quantity = requested_quantity
    if decision.decision is RiskDecisionType.REDUCE_SIZE:
        factor = decision.reduce_size_factor
        if factor is None or factor <= 0 or factor > 1:
            return _blocked(
                reason="invalid_reduce_size_factor",
                signal=signal,
                decision=decision,
            )
        effective_quantity = requested_quantity * factor
    elif decision.decision is not RiskDecisionType.APPROVE:
        return _blocked(
            reason="unsupported_risk_decision",
            signal=signal,
            decision=decision,
        )

    if effective_quantity <= 0:
        return _blocked(
            reason="invalid_quantity",
            signal=signal,
            decision=decision,
        )

    if _has_active_close_position(state, signal.ticker):
        return _blocked(
            reason="liquidation_in_progress",
            signal=signal,
            decision=decision,
            effective_quantity=effective_quantity,
        )

    if _has_same_side_open_order(state, signal.ticker, intent_side):
        return _blocked(
            reason="duplicate_open_order",
            signal=signal,
            decision=decision,
            effective_quantity=effective_quantity,
        )

    if _has_opposite_side_open_order(state, signal.ticker, intent_side):
        return _blocked(
            reason="conflicting_open_order",
            signal=signal,
            decision=decision,
            effective_quantity=effective_quantity,
        )

    if _active_order_count_for_ticker(state, signal.ticker) >= config.max_open_orders_per_symbol:
        return _blocked(
            reason="max_open_orders_per_symbol_hit",
            signal=signal,
            decision=decision,
            effective_quantity=effective_quantity,
        )

    reason = (
        "risk_decision_reduced_size"
        if decision.decision is RiskDecisionType.REDUCE_SIZE
        else "risk_decision_approved"
    )
    intent = OrderIntent(
        ticker=signal.ticker,
        side=intent_side,
        quantity=effective_quantity,
        source_signal_id=signal.signal_id,
        risk_decision_id=decision.risk_decision_id,
        reason=reason,
    )
    return _allowed(
        intent=intent,
        reason=reason,
        effective_quantity=effective_quantity,
        decision=decision,
    )


def reserve_order_intent(
    *,
    state: OrderManagerState,
    intent: OrderIntent,
) -> None:
    """Reserve an allowed intent before any downstream broker/API call."""
    state.mark_signal_used(intent.source_signal_id)
    state.add_open_order(
        order_id=_reserved_order_id(intent),
        ticker=intent.ticker,
        side=intent.side,
        quantity=intent.quantity,
        source_signal_id=intent.source_signal_id,
        status="reserved",
    )


def release_open_order(
    *,
    state: OrderManagerState,
    order_id: str,
) -> bool:
    """Release one reserved open-order placeholder by ID."""
    for index, open_order in enumerate(state.open_orders):
        if open_order.order_id == order_id:
            del state.open_orders[index]
            return True
    return False


def _build_close_position_intent(
    *,
    signal: ModelSignal,
    decision: RiskDecision,
    risk_log_succeeded: bool,
    state: OrderManagerState,
    config: OrderManagerConfig,
) -> OrderManagerResult:
    if not risk_log_succeeded and not config.allow_exit_when_risk_log_failed:
        return _blocked(
            reason="risk_log_failed",
            signal=signal,
            decision=decision,
        )

    if _has_active_close_position(state, signal.ticker):
        return _blocked(
            reason="close_position_already_in_progress",
            signal=signal,
            decision=decision,
        )

    intent = OrderIntent(
        ticker=signal.ticker,
        side=OrderIntentSide.CLOSE_POSITION,
        quantity=None,
        source_signal_id=signal.signal_id,
        risk_decision_id=decision.risk_decision_id,
        reason="exit_close_position_allowed",
    )
    return _allowed(
        intent=intent,
        reason="exit_close_position_allowed",
        effective_quantity=None,
        decision=decision,
    )


def _entry_side_for_signal(signal_side: SignalSide) -> OrderIntentSide | None:
    if signal_side is SignalSide.BUY:
        return OrderIntentSide.BUY
    if signal_side is SignalSide.SELL:
        return OrderIntentSide.SELL
    return None


def _allowed(
    *,
    intent: OrderIntent,
    reason: str,
    effective_quantity: float | None,
    decision: RiskDecision,
) -> OrderManagerResult:
    return OrderManagerResult(
        allowed=True,
        intent=intent,
        reasons=[reason],
        effective_quantity=effective_quantity,
        source_signal_id=intent.source_signal_id,
        risk_decision_type=decision.decision.value,
    )


def _blocked(
    *,
    reason: str,
    signal: ModelSignal,
    decision: RiskDecision,
    effective_quantity: float | None = 0.0,
) -> OrderManagerResult:
    return OrderManagerResult(
        allowed=False,
        intent=None,
        reasons=[reason],
        effective_quantity=effective_quantity,
        source_signal_id=signal.signal_id,
        risk_decision_type=decision.decision.value,
    )


def _coerce_side(side: OrderIntentSide | str) -> OrderIntentSide:
    if isinstance(side, OrderIntentSide):
        return side
    return OrderIntentSide(str(side).upper())


def _has_active_close_position(state: OrderManagerState, ticker: str) -> bool:
    return any(
        open_order.side is OrderIntentSide.CLOSE_POSITION
        for open_order in _active_orders_for_ticker(state, ticker)
    )


def _has_same_side_open_order(
    state: OrderManagerState,
    ticker: str,
    side: OrderIntentSide,
) -> bool:
    return any(
        open_order.side is side
        for open_order in _active_entry_orders_for_ticker(state, ticker)
    )


def _has_opposite_side_open_order(
    state: OrderManagerState,
    ticker: str,
    side: OrderIntentSide,
) -> bool:
    opposite_side = OrderIntentSide.SELL if side is OrderIntentSide.BUY else OrderIntentSide.BUY
    return any(
        open_order.side is opposite_side
        for open_order in _active_entry_orders_for_ticker(state, ticker)
    )


def _active_order_count_for_ticker(state: OrderManagerState, ticker: str) -> int:
    return len(_active_orders_for_ticker(state, ticker))


def _active_entry_orders_for_ticker(
    state: OrderManagerState,
    ticker: str,
) -> list[OpenOrderState]:
    return [
        open_order
        for open_order in _active_orders_for_ticker(state, ticker)
        if open_order.side in {OrderIntentSide.BUY, OrderIntentSide.SELL}
    ]


def _active_orders_for_ticker(state: OrderManagerState, ticker: str) -> list[OpenOrderState]:
    normalized_ticker = ticker.upper()
    return [
        open_order
        for open_order in state.open_orders
        if open_order.ticker.upper() == normalized_ticker and _is_active(open_order)
    ]


def _is_active(open_order: OpenOrderState) -> bool:
    return open_order.status.lower() not in {
        "filled",
        "canceled",
        "cancelled",
        "rejected",
        "expired",
        "closed",
        "released",
    }


def _reserved_order_id(intent: OrderIntent) -> str:
    return f"reserved_order_{intent.source_signal_id}"
