from __future__ import annotations

from datetime import UTC, datetime
import sys

import pytest

from market_relay_engine.contracts.execution import FillEvent, OrderSide
from market_relay_engine.execution.order_manager import (
    OrderIntent,
    OrderIntentSide,
    OrderManagerState,
)
from market_relay_engine.execution.position_state import (
    AccountState,
    PortfolioState,
    PositionState,
    apply_fill_to_portfolio,
    build_risk_state_inputs,
    reset_consecutive_losses,
    reset_daily_account_state,
    reset_daily_loss,
    resolve_close_position_intent,
    sector_exposure,
)


FILL_TIME = datetime(2024, 1, 2, 15, 30, tzinfo=UTC)


def test_position_basics_and_open_position_count() -> None:
    long = PositionState(ticker="aapl", quantity=5, average_price=100)
    short = PositionState(ticker="MSFT", quantity=-3, average_price=50)
    flat = PositionState(ticker="TSLA", quantity=0, average_price=200)
    portfolio = PortfolioState(
        positions={
            "aapl": long,
            "msft": short,
            "tsla": flat,
        }
    )

    assert long.is_long is True
    assert short.is_short is True
    assert flat.is_flat is True
    assert long.absolute_quantity == 5
    assert portfolio.is_long("AAPL") is True
    assert portfolio.is_short("msft") is True
    assert portfolio.has_position("TSLA") is False
    assert portfolio.open_position_count() == 2


def test_account_daily_loss_uses_daily_realized_pnl_not_total() -> None:
    account = AccountState(total_realized_pnl=5000, daily_realized_pnl=-400)

    assert account.daily_loss_dollars == 400


def test_buy_opens_long_and_additional_buy_updates_weighted_average() -> None:
    portfolio = PortfolioState()

    first = apply_fill_to_portfolio(
        portfolio=portfolio,
        fill=_fill(side=OrderSide.BUY, quantity=10, fill_price=100, index=1),
    )
    second = apply_fill_to_portfolio(
        portfolio=portfolio,
        fill=_fill(side=OrderSide.BUY, quantity=10, fill_price=120, index=2),
    )

    position = portfolio.get_position("XOM")
    assert first.position_opened is True
    assert second.realized_pnl_delta == 0
    assert position is not None
    assert position.quantity == 20
    assert position.average_price == pytest.approx(110)


def test_losing_and_winning_long_closes_update_daily_and_total_pnl() -> None:
    portfolio = PortfolioState(
        positions={"XOM": PositionState(ticker="XOM", quantity=10, average_price=100)}
    )

    loss = apply_fill_to_portfolio(
        portfolio=portfolio,
        fill=_fill(side=OrderSide.SELL, quantity=4, fill_price=90, index=1),
    )
    win = apply_fill_to_portfolio(
        portfolio=portfolio,
        fill=_fill(side=OrderSide.SELL, quantity=2, fill_price=120, index=2),
    )

    assert loss.realized_pnl_delta == -40
    assert win.realized_pnl_delta == 40
    assert portfolio.account.total_realized_pnl == 0
    assert portfolio.account.daily_realized_pnl == 0
    assert portfolio.account.daily_loss_dollars == 0


def test_win_loss_and_breakeven_consecutive_loss_logic() -> None:
    portfolio = PortfolioState(
        account=AccountState(consecutive_losses=2),
        positions={"XOM": PositionState(ticker="XOM", quantity=10, average_price=100)},
    )

    loss = apply_fill_to_portfolio(
        portfolio=portfolio,
        fill=_fill(side=OrderSide.SELL, quantity=2, fill_price=90, index=1),
    )
    breakeven = apply_fill_to_portfolio(
        portfolio=portfolio,
        fill=_fill(side=OrderSide.SELL, quantity=2, fill_price=100, index=2),
    )
    win = apply_fill_to_portfolio(
        portfolio=portfolio,
        fill=_fill(side=OrderSide.SELL, quantity=2, fill_price=110, index=3),
    )

    assert loss.realized_pnl_delta < 0
    assert breakeven.realized_pnl_delta == 0
    assert win.realized_pnl_delta > 0
    assert portfolio.account.consecutive_losses == 0


def test_loss_increments_and_breakeven_leaves_consecutive_losses_unchanged() -> None:
    portfolio = PortfolioState(
        positions={"XOM": PositionState(ticker="XOM", quantity=10, average_price=100)}
    )

    apply_fill_to_portfolio(
        portfolio=portfolio,
        fill=_fill(side=OrderSide.SELL, quantity=2, fill_price=90, index=1),
    )
    assert portfolio.account.consecutive_losses == 1

    apply_fill_to_portfolio(
        portfolio=portfolio,
        fill=_fill(side=OrderSide.SELL, quantity=2, fill_price=100, index=2),
    )
    assert portfolio.account.consecutive_losses == 1


def test_daily_reset_helpers_clear_daily_state_without_erasing_total() -> None:
    account = AccountState(
        total_realized_pnl=5000,
        daily_realized_pnl=-400,
        consecutive_losses=3,
    )

    reset_daily_loss(account)
    assert account.total_realized_pnl == 5000
    assert account.daily_realized_pnl == 0
    assert account.daily_loss_dollars == 0
    assert account.consecutive_losses == 3

    account.daily_realized_pnl = -100
    account.daily_loss_dollars = 100
    reset_daily_account_state(account)
    assert account.total_realized_pnl == 5000
    assert account.daily_realized_pnl == 0
    assert account.daily_loss_dollars == 0
    assert account.consecutive_losses == 3

    reset_consecutive_losses(account)
    assert account.consecutive_losses == 0


def test_partial_long_close_keeps_average_price_unchanged() -> None:
    portfolio = PortfolioState(
        positions={"XOM": PositionState(ticker="XOM", quantity=10, average_price=100)}
    )

    result = apply_fill_to_portfolio(
        portfolio=portfolio,
        fill=_fill(side=OrderSide.SELL, quantity=4, fill_price=110),
    )

    position = portfolio.get_position("XOM")
    assert result.realized_pnl_delta == 40
    assert position is not None
    assert position.quantity == 6
    assert position.average_price == 100


def test_sell_fully_closes_long() -> None:
    portfolio = PortfolioState(
        positions={"XOM": PositionState(ticker="XOM", quantity=10, average_price=100)}
    )

    result = apply_fill_to_portfolio(
        portfolio=portfolio,
        fill=_fill(side=OrderSide.SELL, quantity=10, fill_price=110),
    )

    assert result.position_closed is True
    assert portfolio.get_position("XOM") is None
    assert portfolio.account.total_realized_pnl == 100


def test_sell_opens_short_and_additional_sell_updates_weighted_average() -> None:
    portfolio = PortfolioState()

    first = apply_fill_to_portfolio(
        portfolio=portfolio,
        fill=_fill(side=OrderSide.SELL, quantity=10, fill_price=100, index=1),
    )
    second = apply_fill_to_portfolio(
        portfolio=portfolio,
        fill=_fill(side=OrderSide.SELL, quantity=10, fill_price=80, index=2),
    )

    position = portfolio.get_position("XOM")
    assert first.position_opened is True
    assert second.realized_pnl_delta == 0
    assert position is not None
    assert position.quantity == -20
    assert position.average_price == pytest.approx(90)


def test_partial_short_cover_keeps_average_price_unchanged() -> None:
    portfolio = PortfolioState(
        positions={"XOM": PositionState(ticker="XOM", quantity=-10, average_price=100)}
    )

    result = apply_fill_to_portfolio(
        portfolio=portfolio,
        fill=_fill(side=OrderSide.BUY, quantity=4, fill_price=90),
    )

    position = portfolio.get_position("XOM")
    assert result.realized_pnl_delta == 40
    assert position is not None
    assert position.quantity == -6
    assert position.average_price == 100


def test_buy_fully_covers_short() -> None:
    portfolio = PortfolioState(
        positions={"XOM": PositionState(ticker="XOM", quantity=-10, average_price=100)}
    )

    result = apply_fill_to_portfolio(
        portfolio=portfolio,
        fill=_fill(side=OrderSide.BUY, quantity=10, fill_price=90),
    )

    assert result.position_closed is True
    assert portfolio.get_position("XOM") is None
    assert portfolio.account.total_realized_pnl == 100


def test_long_to_short_flip_splits_close_and_open() -> None:
    portfolio = PortfolioState(
        positions={"XOM": PositionState(ticker="XOM", quantity=10, average_price=100)}
    )

    result = apply_fill_to_portfolio(
        portfolio=portfolio,
        fill=_fill(side=OrderSide.SELL, quantity=15, fill_price=110),
    )

    position = portfolio.get_position("XOM")
    assert result.position_flipped is True
    assert result.position_closed is True
    assert result.position_opened is True
    assert result.realized_pnl_delta == 100
    assert position is not None
    assert position.quantity == -5
    assert position.average_price == 110


def test_short_to_long_flip_splits_close_and_open() -> None:
    portfolio = PortfolioState(
        positions={"XOM": PositionState(ticker="XOM", quantity=-10, average_price=100)}
    )

    result = apply_fill_to_portfolio(
        portfolio=portfolio,
        fill=_fill(side=OrderSide.BUY, quantity=15, fill_price=90),
    )

    position = portfolio.get_position("XOM")
    assert result.position_flipped is True
    assert result.position_closed is True
    assert result.position_opened is True
    assert result.realized_pnl_delta == 100
    assert position is not None
    assert position.quantity == 5
    assert position.average_price == 90


def test_close_position_long_resolves_sell_exact_quantity() -> None:
    portfolio = PortfolioState(
        positions={"XOM": PositionState(ticker="XOM", quantity=6.5, average_price=100)}
    )

    resolved = resolve_close_position_intent(
        intent=_intent(side=OrderIntentSide.CLOSE_POSITION, quantity=None),
        portfolio=portfolio,
    )

    assert resolved.side is OrderIntentSide.SELL
    assert resolved.quantity == 6.5


def test_close_position_short_resolves_buy_exact_quantity() -> None:
    portfolio = PortfolioState(
        positions={"XOM": PositionState(ticker="XOM", quantity=-4, average_price=100)}
    )

    resolved = resolve_close_position_intent(
        intent=_intent(side=OrderIntentSide.CLOSE_POSITION, quantity=None),
        portfolio=portfolio,
    )

    assert resolved.side is OrderIntentSide.BUY
    assert resolved.quantity == 4


def test_close_position_flat_resolves_quantity_zero() -> None:
    portfolio = PortfolioState()

    resolved = resolve_close_position_intent(
        intent=_intent(side=OrderIntentSide.CLOSE_POSITION, quantity=None),
        portfolio=portfolio,
    )

    assert resolved.quantity == 0
    assert resolved.reason == "no_position_to_close"
    assert resolved.side in {OrderIntentSide.BUY, OrderIntentSide.SELL}


def test_normal_buy_and_sell_intents_pass_through_with_quantity() -> None:
    portfolio = PortfolioState()

    buy = resolve_close_position_intent(
        intent=_intent(side=OrderIntentSide.BUY, quantity=3, reason="entry"),
        portfolio=portfolio,
    )
    sell = resolve_close_position_intent(
        intent=_intent(side=OrderIntentSide.SELL, quantity=2, reason="entry"),
        portfolio=portfolio,
    )

    assert buy.side is OrderIntentSide.BUY
    assert buy.quantity == 3
    assert buy.reason == "entry"
    assert sell.side is OrderIntentSide.SELL
    assert sell.quantity == 2


def test_duplicate_fill_id_is_ignored_without_mutation() -> None:
    portfolio = PortfolioState()
    fill = _fill(side=OrderSide.BUY, quantity=10, fill_price=100)

    first = apply_fill_to_portfolio(portfolio=portfolio, fill=fill)
    duplicate = apply_fill_to_portfolio(portfolio=portfolio, fill=fill)

    position = portfolio.get_position("XOM")
    assert first.duplicate_fill is False
    assert duplicate.duplicate_fill is True
    assert duplicate.previous_quantity == 10
    assert duplicate.new_quantity == 10
    assert position is not None
    assert position.quantity == 10
    assert portfolio.account.total_realized_pnl == 0


def test_sector_exposure_groups_by_sector_with_unknown_fallback() -> None:
    portfolio = PortfolioState(
        positions={
            "AAPL": PositionState(ticker="AAPL", quantity=10, average_price=100),
            "XOM": PositionState(ticker="XOM", quantity=-5, average_price=80),
        },
        ticker_to_sector={"AAPL": "Technology"},
    )

    exposure = sector_exposure(portfolio, {"AAPL": 120, "XOM": 90})

    assert exposure == {"Technology": 1200, "UNKNOWN": 450}


def test_build_risk_state_inputs_uses_order_state_and_daily_loss() -> None:
    portfolio = PortfolioState(
        account=AccountState(total_realized_pnl=5000, daily_realized_pnl=-400),
        positions={"XOM": PositionState(ticker="XOM", quantity=10, average_price=100)},
    )
    order_state = OrderManagerState()
    order_state.add_open_order(
        order_id="order_1",
        ticker="XOM",
        side=OrderIntentSide.BUY,
        quantity=1,
        source_signal_id="signal_1",
        status="reserved",
    )

    account_input, portfolio_input = build_risk_state_inputs(
        portfolio=portfolio,
        ticker="XOM",
        order_state=order_state,
    )
    _, portfolio_without_orders = build_risk_state_inputs(
        portfolio=portfolio,
        ticker="XOM",
    )

    assert account_input.daily_loss_dollars == 400
    assert account_input.consecutive_losses == 0
    assert portfolio_input.open_positions == 1
    assert portfolio_input.symbol_position_exists is True
    assert portfolio_input.duplicate_or_conflicting_order is True
    assert portfolio_without_orders.duplicate_or_conflicting_order is False


@pytest.mark.parametrize("quantity", [float("nan"), float("inf")])
def test_position_rejects_non_finite_quantity(quantity: float) -> None:
    with pytest.raises(ValueError, match="quantity must be finite"):
        PositionState(ticker="XOM", quantity=quantity, average_price=100)


@pytest.mark.parametrize("price", [float("nan"), float("inf")])
def test_position_rejects_non_finite_price(price: float) -> None:
    with pytest.raises(ValueError, match="average_price must be finite"):
        PositionState(ticker="XOM", quantity=1, average_price=price)


def test_position_rejects_negative_price() -> None:
    with pytest.raises(ValueError, match="average_price must be non-negative"):
        PositionState(ticker="XOM", quantity=1, average_price=-1)


def test_apply_fill_rejects_non_finite_fill_values() -> None:
    portfolio = PortfolioState()

    with pytest.raises(ValueError, match="fill.quantity must be finite"):
        apply_fill_to_portfolio(
            portfolio=portfolio,
            fill=_fill(side=OrderSide.BUY, quantity=float("nan"), fill_price=100),
        )

    with pytest.raises(ValueError, match="fill.fill_price must be finite"):
        apply_fill_to_portfolio(
            portfolio=portfolio,
            fill=_fill(side=OrderSide.BUY, quantity=1, fill_price=float("inf")),
        )


def test_position_state_has_no_external_service_dependencies() -> None:
    import market_relay_engine.execution.position_state as position_state

    source_path = position_state.__file__
    assert source_path is not None
    source = open(source_path, encoding="utf-8").read()

    assert "alpaca" not in source.lower()
    assert "questdb" not in source.lower()
    assert "requests" not in source.lower()
    assert "aiohttp" not in source.lower()
    assert "alpaca" not in sys.modules


def _fill(
    *,
    side: OrderSide,
    quantity: float,
    fill_price: float,
    ticker: str = "XOM",
    index: int = 1,
) -> FillEvent:
    return FillEvent(
        fill_time=FILL_TIME,
        order_id=f"order_{index}",
        ticker=ticker,
        side=side,
        quantity=quantity,
        fill_price=fill_price,
        fill_id=f"fill_{index}",
    )


def _intent(
    *,
    side: OrderIntentSide,
    quantity: float | None,
    reason: str = "exit_close_position_allowed",
) -> OrderIntent:
    return OrderIntent(
        ticker="XOM",
        side=side,
        quantity=quantity,
        source_signal_id="signal_1",
        risk_decision_id="risk_decision_1",
        reason=reason,
    )
