"""Local position and account state helpers for filled orders."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping

from market_relay_engine.contracts.execution import FillEvent, OrderSide
from market_relay_engine.execution.order_manager import (
    OrderIntent,
    OrderIntentSide,
    OrderManagerState,
)
from market_relay_engine.risk.risk_filter import AccountRiskInput, PortfolioRiskInput


UNKNOWN_SECTOR = "UNKNOWN"
_FLAT_EPSILON = 1e-12
_INACTIVE_ORDER_STATUSES = {
    "filled",
    "canceled",
    "cancelled",
    "rejected",
    "expired",
    "closed",
    "released",
}


@dataclass(kw_only=True)
class PositionState:
    """Local signed position state for one ticker."""

    ticker: str
    quantity: float
    average_price: float
    realized_pnl: float = 0.0

    def __post_init__(self) -> None:
        self.ticker = _normalize_ticker(self.ticker)
        self.quantity = _finite_float(self.quantity, "quantity")
        self.average_price = _non_negative_float(self.average_price, "average_price")
        self.realized_pnl = _finite_float(self.realized_pnl, "realized_pnl")

    @property
    def is_long(self) -> bool:
        """Return true when signed quantity is positive."""
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        """Return true when signed quantity is negative."""
        return self.quantity < 0

    @property
    def is_flat(self) -> bool:
        """Return true when signed quantity is zero."""
        return abs(self.quantity) <= _FLAT_EPSILON

    @property
    def absolute_quantity(self) -> float:
        """Return absolute position size."""
        return abs(self.quantity)


@dataclass(kw_only=True)
class AccountState:
    """Local account-level realized PnL placeholders."""

    total_realized_pnl: float = 0.0
    daily_realized_pnl: float = 0.0
    daily_loss_dollars: float = 0.0
    consecutive_losses: int = 0

    def __post_init__(self) -> None:
        self.total_realized_pnl = _finite_float(
            self.total_realized_pnl,
            "total_realized_pnl",
        )
        self.daily_realized_pnl = _finite_float(
            self.daily_realized_pnl,
            "daily_realized_pnl",
        )
        self.daily_loss_dollars = max(0.0, -self.daily_realized_pnl)
        self.consecutive_losses = _non_negative_int(
            self.consecutive_losses,
            "consecutive_losses",
        )


@dataclass(kw_only=True)
class PortfolioState:
    """Local in-memory portfolio state derived from fills."""

    positions: dict[str, PositionState] = field(default_factory=dict)
    account: AccountState = field(default_factory=AccountState)
    applied_fill_ids: set[str] = field(default_factory=set)
    ticker_to_sector: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.positions = {
            _normalize_ticker(ticker): position
            for ticker, position in self.positions.items()
        }
        self.ticker_to_sector = {
            _normalize_ticker(ticker): _normalize_sector(sector)
            for ticker, sector in self.ticker_to_sector.items()
        }

    def get_position(self, ticker: str) -> PositionState | None:
        """Return the active position for ticker, if one exists."""
        position = self.positions.get(_normalize_ticker(ticker))
        if position is None or position.is_flat:
            return None
        return position

    def has_position(self, ticker: str) -> bool:
        """Return true when ticker has a non-flat position."""
        return self.get_position(ticker) is not None

    def is_long(self, ticker: str) -> bool:
        """Return true when ticker is long."""
        position = self.get_position(ticker)
        return position is not None and position.is_long

    def is_short(self, ticker: str) -> bool:
        """Return true when ticker is short."""
        position = self.get_position(ticker)
        return position is not None and position.is_short

    def open_position_count(self) -> int:
        """Return count of non-flat positions."""
        return sum(1 for position in self.positions.values() if not position.is_flat)

    def gross_exposure(self, mark_prices: Mapping[str, float] | None = None) -> float:
        """Return gross exposure using marks when provided, otherwise average price."""
        normalized_marks = _normalize_mark_prices(mark_prices or {})
        exposure = 0.0
        for ticker, position in self.positions.items():
            if position.is_flat:
                continue
            mark_price = normalized_marks.get(ticker, position.average_price)
            exposure += position.absolute_quantity * mark_price
        return exposure


@dataclass(frozen=True, kw_only=True)
class PositionUpdateResult:
    """Result of applying one fill to local position state."""

    ticker: str
    previous_quantity: float
    new_quantity: float
    realized_pnl_delta: float
    position_closed: bool
    position_opened: bool
    position_flipped: bool
    duplicate_fill: bool = False


@dataclass(frozen=True, kw_only=True)
class ResolvedOrderIntent:
    """Order intent after resolving local close-position semantics."""

    ticker: str
    side: OrderIntentSide
    quantity: float
    source_signal_id: str
    risk_decision_id: str | None
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", _normalize_ticker(self.ticker))
        object.__setattr__(self, "side", _coerce_intent_side(self.side))
        quantity = _non_negative_float(self.quantity, "quantity")
        object.__setattr__(self, "quantity", quantity)


def apply_fill_to_portfolio(
    *,
    portfolio: PortfolioState,
    fill: FillEvent,
) -> PositionUpdateResult:
    """Apply one fill to local position/account state."""
    ticker = _normalize_ticker(fill.ticker)
    position = portfolio.get_position(ticker)
    previous_quantity = position.quantity if position is not None else 0.0

    if fill.fill_id in portfolio.applied_fill_ids:
        return PositionUpdateResult(
            ticker=ticker,
            previous_quantity=previous_quantity,
            new_quantity=previous_quantity,
            realized_pnl_delta=0.0,
            position_closed=False,
            position_opened=False,
            position_flipped=False,
            duplicate_fill=True,
        )

    side = OrderSide(fill.side)
    fill_quantity = _positive_float(fill.quantity, "fill.quantity")
    fill_price = _non_negative_float(fill.fill_price, "fill.fill_price")
    fill_delta = fill_quantity if side is OrderSide.BUY else -fill_quantity
    realized_pnl_delta = 0.0
    position_opened = False
    position_closed = False
    position_flipped = False

    if position is None:
        portfolio.positions[ticker] = PositionState(
            ticker=ticker,
            quantity=fill_delta,
            average_price=fill_price,
        )
        new_quantity = fill_delta
        position_opened = True
    elif _same_side(position.quantity, fill_delta):
        new_quantity = position.quantity + fill_delta
        position.average_price = _weighted_average_price(
            current_quantity=position.quantity,
            current_average_price=position.average_price,
            added_quantity=fill_quantity,
            added_price=fill_price,
        )
        position.quantity = new_quantity
    else:
        closed_quantity = min(abs(position.quantity), fill_quantity)
        realized_pnl_delta = _realized_pnl_for_close(
            previous_quantity=position.quantity,
            average_price=position.average_price,
            fill_price=fill_price,
            closed_quantity=closed_quantity,
        )
        new_quantity = _normalize_flat_quantity(position.quantity + fill_delta)
        position.realized_pnl += realized_pnl_delta
        _update_account_for_realized_pnl(portfolio.account, realized_pnl_delta)

        if abs(new_quantity) <= _FLAT_EPSILON:
            del portfolio.positions[ticker]
            position_closed = True
            new_quantity = 0.0
        elif _same_side(position.quantity, new_quantity):
            position.quantity = new_quantity
        else:
            portfolio.positions[ticker] = PositionState(
                ticker=ticker,
                quantity=new_quantity,
                average_price=fill_price,
                realized_pnl=position.realized_pnl,
            )
            position_closed = True
            position_opened = True
            position_flipped = True

    portfolio.applied_fill_ids.add(fill.fill_id)
    return PositionUpdateResult(
        ticker=ticker,
        previous_quantity=previous_quantity,
        new_quantity=new_quantity,
        realized_pnl_delta=realized_pnl_delta,
        position_closed=position_closed,
        position_opened=position_opened,
        position_flipped=position_flipped,
        duplicate_fill=False,
    )


def resolve_close_position_intent(
    *,
    intent: OrderIntent,
    portfolio: PortfolioState,
) -> ResolvedOrderIntent:
    """Resolve local CLOSE_POSITION intent into a concrete buy/sell direction."""
    side = _coerce_intent_side(intent.side)

    if side in {OrderIntentSide.BUY, OrderIntentSide.SELL}:
        if intent.quantity is None:
            raise ValueError("BUY/SELL intents must have a quantity")
        return ResolvedOrderIntent(
            ticker=intent.ticker,
            side=side,
            quantity=intent.quantity,
            source_signal_id=intent.source_signal_id,
            risk_decision_id=intent.risk_decision_id,
            reason=intent.reason,
        )

    position = portfolio.get_position(intent.ticker)
    if position is None:
        return ResolvedOrderIntent(
            ticker=intent.ticker,
            side=OrderIntentSide.SELL,
            quantity=0.0,
            source_signal_id=intent.source_signal_id,
            risk_decision_id=intent.risk_decision_id,
            reason="no_position_to_close",
        )

    close_side = OrderIntentSide.SELL if position.is_long else OrderIntentSide.BUY
    return ResolvedOrderIntent(
        ticker=intent.ticker,
        side=close_side,
        quantity=position.absolute_quantity,
        source_signal_id=intent.source_signal_id,
        risk_decision_id=intent.risk_decision_id,
        reason=intent.reason,
    )


def build_risk_state_inputs(
    *,
    portfolio: PortfolioState,
    ticker: str,
    order_state: OrderManagerState | None = None,
) -> tuple[AccountRiskInput, PortfolioRiskInput]:
    """Build the account and portfolio placeholders consumed by Risk Filter V1."""
    return (
        AccountRiskInput(
            daily_loss_dollars=portfolio.account.daily_loss_dollars,
            consecutive_losses=portfolio.account.consecutive_losses,
        ),
        PortfolioRiskInput(
            duplicate_or_conflicting_order=_has_active_order_for_ticker(
                order_state,
                ticker,
            ),
            open_positions=portfolio.open_position_count(),
            symbol_position_exists=portfolio.has_position(ticker),
        ),
    )


def sector_exposure(
    portfolio: PortfolioState,
    mark_prices: Mapping[str, float],
) -> dict[str, float]:
    """Return absolute exposure grouped by lightweight sector labels."""
    normalized_marks = _normalize_mark_prices(mark_prices)
    exposure_by_sector: dict[str, float] = {}
    for ticker, position in portfolio.positions.items():
        if position.is_flat:
            continue
        if ticker not in normalized_marks:
            raise ValueError(f"Missing mark price for {ticker}")
        sector = portfolio.ticker_to_sector.get(ticker, UNKNOWN_SECTOR)
        exposure_by_sector[sector] = exposure_by_sector.get(sector, 0.0) + (
            position.absolute_quantity * normalized_marks[ticker]
        )
    return exposure_by_sector


def reset_daily_loss(account: AccountState) -> None:
    """Reset daily PnL and loss tracking without erasing total realized PnL."""
    account.daily_realized_pnl = 0.0
    account.daily_loss_dollars = 0.0


def reset_consecutive_losses(account: AccountState) -> None:
    """Reset consecutive realized losing fills explicitly."""
    account.consecutive_losses = 0


def reset_daily_account_state(account: AccountState) -> None:
    """Reset daily account counters at a new trading session boundary."""
    reset_daily_loss(account)


def _update_account_for_realized_pnl(
    account: AccountState,
    realized_pnl_delta: float,
) -> None:
    account.total_realized_pnl += realized_pnl_delta
    account.daily_realized_pnl += realized_pnl_delta
    account.daily_loss_dollars = max(0.0, -account.daily_realized_pnl)
    if realized_pnl_delta < 0:
        account.consecutive_losses += 1
    elif realized_pnl_delta > 0:
        account.consecutive_losses = 0


def _realized_pnl_for_close(
    *,
    previous_quantity: float,
    average_price: float,
    fill_price: float,
    closed_quantity: float,
) -> float:
    if previous_quantity > 0:
        return (fill_price - average_price) * closed_quantity
    return (average_price - fill_price) * closed_quantity


def _weighted_average_price(
    *,
    current_quantity: float,
    current_average_price: float,
    added_quantity: float,
    added_price: float,
) -> float:
    current_abs_quantity = abs(current_quantity)
    new_abs_quantity = current_abs_quantity + added_quantity
    return (
        (current_abs_quantity * current_average_price)
        + (added_quantity * added_price)
    ) / new_abs_quantity


def _has_active_order_for_ticker(
    order_state: OrderManagerState | None,
    ticker: str,
) -> bool:
    if order_state is None:
        return False
    normalized_ticker = _normalize_ticker(ticker)
    return any(
        _normalize_ticker(open_order.ticker) == normalized_ticker
        and open_order.status.lower() not in _INACTIVE_ORDER_STATUSES
        for open_order in order_state.open_orders
    )


def _normalize_mark_prices(mark_prices: Mapping[str, float]) -> dict[str, float]:
    return {
        _normalize_ticker(ticker): _non_negative_float(price, f"mark_prices[{ticker}]")
        for ticker, price in mark_prices.items()
    }


def _same_side(first_quantity: float, second_quantity: float) -> bool:
    return (first_quantity > 0 and second_quantity > 0) or (
        first_quantity < 0 and second_quantity < 0
    )


def _normalize_flat_quantity(quantity: float) -> float:
    if abs(quantity) <= _FLAT_EPSILON:
        return 0.0
    return quantity


def _coerce_intent_side(side: OrderIntentSide | str) -> OrderIntentSide:
    if isinstance(side, OrderIntentSide):
        return side
    return OrderIntentSide(str(side).upper())


def _normalize_ticker(ticker: str) -> str:
    if not isinstance(ticker, str) or not ticker.strip():
        raise ValueError("ticker must be a non-empty string")
    return ticker.strip().upper()


def _normalize_sector(sector: str) -> str:
    if not isinstance(sector, str) or not sector.strip():
        return UNKNOWN_SECTOR
    return sector.strip()


def _finite_float(value: float, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric, not bool")
    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not math.isfinite(numeric_value):
        raise ValueError(f"{field_name} must be finite")
    return numeric_value


def _non_negative_float(value: float, field_name: str) -> float:
    numeric_value = _finite_float(value, field_name)
    if numeric_value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return numeric_value


def _positive_float(value: float, field_name: str) -> float:
    numeric_value = _finite_float(value, field_name)
    if numeric_value <= 0:
        raise ValueError(f"{field_name} must be positive")
    return numeric_value


def _non_negative_int(value: int, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer, not bool")
    try:
        integer_value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if integer_value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    if not isinstance(value, int) and str(integer_value) != str(value).strip():
        raise ValueError(f"{field_name} must be an integer")
    return integer_value
