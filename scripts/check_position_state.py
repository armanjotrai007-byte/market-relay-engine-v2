"""Validate local position/account state behavior without external services."""

from __future__ import annotations

from datetime import UTC, datetime
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.contracts.execution import FillEvent, OrderSide  # noqa: E402
from market_relay_engine.execution.order_manager import (  # noqa: E402
    OrderIntent,
    OrderIntentSide,
)
from market_relay_engine.execution.position_state import (  # noqa: E402
    PortfolioState,
    apply_fill_to_portfolio,
    build_risk_state_inputs,
    resolve_close_position_intent,
)


def main() -> int:
    portfolio = PortfolioState()

    buy = _fill(
        fill_id="fill_aapl_buy",
        order_id="order_aapl_buy",
        ticker="AAPL",
        side=OrderSide.BUY,
        quantity=10,
        fill_price=100,
    )
    buy_result = apply_fill_to_portfolio(portfolio=portfolio, fill=buy)
    assert buy_result.position_opened is True
    assert portfolio.is_long("AAPL") is True

    partial_sell = _fill(
        fill_id="fill_aapl_sell",
        order_id="order_aapl_sell",
        ticker="AAPL",
        side=OrderSide.SELL,
        quantity=4,
        fill_price=110,
    )
    sell_result = apply_fill_to_portfolio(portfolio=portfolio, fill=partial_sell)
    assert sell_result.realized_pnl_delta == 40
    assert portfolio.get_position("AAPL") is not None
    assert portfolio.get_position("AAPL").quantity == 6  # type: ignore[union-attr]

    close_long = resolve_close_position_intent(
        intent=_close_intent("AAPL", index=1),
        portfolio=portfolio,
    )
    assert close_long.side is OrderIntentSide.SELL
    assert close_long.quantity == 6

    short = _fill(
        fill_id="fill_msft_short",
        order_id="order_msft_short",
        ticker="MSFT",
        side=OrderSide.SELL,
        quantity=5,
        fill_price=50,
    )
    apply_fill_to_portfolio(portfolio=portfolio, fill=short)
    close_short = resolve_close_position_intent(
        intent=_close_intent("MSFT", index=2),
        portfolio=portfolio,
    )
    assert close_short.side is OrderIntentSide.BUY
    assert close_short.quantity == 5

    account_input, portfolio_input = build_risk_state_inputs(
        portfolio=portfolio,
        ticker="AAPL",
    )
    assert account_input.daily_loss_dollars == 0
    assert account_input.consecutive_losses == 0
    assert portfolio_input.open_positions == 2
    assert portfolio_input.symbol_position_exists is True

    print("Position state check PASS")
    return 0


def _fill(
    *,
    fill_id: str,
    order_id: str,
    ticker: str,
    side: OrderSide,
    quantity: float,
    fill_price: float,
) -> FillEvent:
    return FillEvent(
        fill_time=datetime(2024, 1, 2, 15, 30, tzinfo=UTC),
        order_id=order_id,
        ticker=ticker,
        side=side,
        quantity=quantity,
        fill_price=fill_price,
        fill_id=fill_id,
    )


def _close_intent(ticker: str, *, index: int) -> OrderIntent:
    return OrderIntent(
        ticker=ticker,
        side=OrderIntentSide.CLOSE_POSITION,
        quantity=None,
        source_signal_id=f"signal_{index}",
        risk_decision_id=f"risk_decision_{index}",
        reason="exit_close_position_allowed",
    )


if __name__ == "__main__":
    raise SystemExit(main())
