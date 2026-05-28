from __future__ import annotations

from inspect import signature
from pathlib import Path

import pytest

from market_relay_engine.contracts.model import SignalSide
from market_relay_engine.contracts.risk import RiskDecisionType
from market_relay_engine.execution.order_manager import (
    OpenOrderState,
    OrderIntentSide,
    OrderManagerConfig,
    OrderManagerState,
    build_order_intent,
    release_open_order,
    reserve_order_intent,
)
from tests.fixtures.model_signals import make_model_signal
from tests.fixtures.risk_decisions import make_risk_decision


def test_approve_creates_full_size_entry_intent() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=1)
    result = build_order_intent(
        signal=signal,
        decision=_decision(signal, RiskDecisionType.APPROVE),
        risk_log_succeeded=True,
        desired_quantity=10,
        config=_config(),
    )

    assert result.allowed is True
    assert result.intent is not None
    assert result.intent.side is OrderIntentSide.BUY
    assert result.intent.quantity == 10
    assert result.effective_quantity == 10


def test_sell_signal_creates_sell_intent() -> None:
    signal = make_model_signal(signal=SignalSide.SELL, index=2)
    result = build_order_intent(
        signal=signal,
        decision=_decision(signal, RiskDecisionType.APPROVE),
        risk_log_succeeded=True,
        desired_quantity=3,
        config=_config(),
    )

    assert result.allowed is True
    assert result.intent is not None
    assert result.intent.side is OrderIntentSide.SELL
    assert result.intent.quantity == 3


@pytest.mark.parametrize(
    ("desired_quantity", "factor", "expected_quantity"),
    [(1, 0.5, 0.5), (1.5, 0.5, 0.75), (8, 0.5, 4)],
)
def test_reduce_size_preserves_fractional_quantity(
    desired_quantity: float,
    factor: float,
    expected_quantity: float,
) -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=3)
    result = build_order_intent(
        signal=signal,
        decision=_decision(
            signal,
            RiskDecisionType.REDUCE_SIZE,
            reduce_size_factor=factor,
        ),
        risk_log_succeeded=True,
        desired_quantity=desired_quantity,
        config=_config(),
    )

    assert result.allowed is True
    assert result.intent is not None
    assert result.intent.quantity == pytest.approx(expected_quantity)
    assert result.reasons == ["risk_decision_reduced_size"]


@pytest.mark.parametrize("factor", [None, 0.0, -0.1, 1.5])
def test_invalid_reduce_size_factor_blocks(factor: float | None) -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=4)
    result = build_order_intent(
        signal=signal,
        decision=_decision(
            signal,
            RiskDecisionType.REDUCE_SIZE,
            reduce_size_factor=factor,
        ),
        risk_log_succeeded=True,
        desired_quantity=10,
        config=_config(),
    )

    assert result.allowed is False
    assert result.intent is None
    assert result.reasons == ["invalid_reduce_size_factor"]


@pytest.mark.parametrize(
    ("decision_type", "expected_reason"),
    [
        (RiskDecisionType.BLOCK, "risk_decision_blocked"),
        (RiskDecisionType.DO_NOTHING, "risk_decision_do_nothing"),
    ],
)
def test_block_and_do_nothing_create_no_intent(
    decision_type: RiskDecisionType,
    expected_reason: str,
) -> None:
    signal_side = SignalSide.DO_NOTHING if decision_type is RiskDecisionType.DO_NOTHING else SignalSide.BUY
    signal = make_model_signal(signal=signal_side, index=5)
    result = build_order_intent(
        signal=signal,
        decision=_decision(signal, decision_type),
        risk_log_succeeded=True,
        desired_quantity=10,
        config=_config(),
    )

    assert result.allowed is False
    assert result.intent is None
    assert result.reasons == [expected_reason]


def test_exit_creates_close_position_without_quantity_and_allows_failed_log() -> None:
    signal = make_model_signal(signal=SignalSide.EXIT, index=6)
    state = OrderManagerState(
        open_orders=[
            _open_order(side=OrderIntentSide.BUY, order_id="buy_1"),
            _open_order(side=OrderIntentSide.SELL, order_id="sell_1"),
        ]
    )

    result = build_order_intent(
        signal=signal,
        decision=_decision(signal, RiskDecisionType.EXIT),
        risk_log_succeeded=False,
        desired_quantity=0,
        state=state,
        config=_config(max_open_orders_per_symbol=0),
    )

    assert result.allowed is True
    assert result.intent is not None
    assert result.intent.side is OrderIntentSide.CLOSE_POSITION
    assert result.intent.quantity is None
    assert result.effective_quantity is None
    assert result.reasons == ["exit_close_position_allowed"]


def test_entries_with_failed_risk_logging_block_by_default() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=7)
    result = build_order_intent(
        signal=signal,
        decision=_decision(signal, RiskDecisionType.APPROVE),
        risk_log_succeeded=False,
        desired_quantity=10,
        config=_config(),
    )

    assert result.allowed is False
    assert result.reasons == ["risk_log_failed"]


def test_approve_decision_with_mismatched_model_signal_id_blocks() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=20)
    stale_signal = make_model_signal(signal=SignalSide.BUY, index=21)
    result = build_order_intent(
        signal=signal,
        decision=_decision(stale_signal, RiskDecisionType.APPROVE),
        risk_log_succeeded=True,
        desired_quantity=10,
        config=_config(),
    )

    assert result.allowed is False
    assert result.reasons == ["risk_decision_signal_mismatch"]


def test_approve_decision_with_mismatched_ticker_blocks() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, ticker="XOM", index=22)
    decision = make_risk_decision(
        model_signal=signal,
        ticker="LMT",
        decision=RiskDecisionType.APPROVE,
        approved=True,
    )

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        config=_config(),
    )

    assert result.allowed is False
    assert result.reasons == ["risk_decision_ticker_mismatch"]


def test_reduce_size_decision_with_mismatched_model_signal_id_blocks() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=23)
    stale_signal = make_model_signal(signal=SignalSide.BUY, index=24)
    result = build_order_intent(
        signal=signal,
        decision=_decision(
            stale_signal,
            RiskDecisionType.REDUCE_SIZE,
            reduce_size_factor=0.5,
        ),
        risk_log_succeeded=True,
        desired_quantity=10,
        config=_config(),
    )

    assert result.allowed is False
    assert result.reasons == ["risk_decision_signal_mismatch"]


def test_exit_decision_with_mismatched_model_signal_id_blocks() -> None:
    signal = make_model_signal(signal=SignalSide.EXIT, index=25)
    stale_signal = make_model_signal(signal=SignalSide.EXIT, index=26)
    result = build_order_intent(
        signal=signal,
        decision=_decision(stale_signal, RiskDecisionType.EXIT),
        risk_log_succeeded=False,
        desired_quantity=None,
        config=_config(),
    )

    assert result.allowed is False
    assert result.reasons == ["risk_decision_signal_mismatch"]


def test_matching_signal_and_decision_still_works() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=27)
    result = build_order_intent(
        signal=signal,
        decision=_decision(signal, RiskDecisionType.APPROVE),
        risk_log_succeeded=True,
        desired_quantity=10,
        config=_config(),
    )

    assert result.allowed is True
    assert result.intent is not None
    assert result.intent.source_signal_id == signal.signal_id


def test_duplicate_signal_same_side_opposite_side_max_and_invalid_quantity_block() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=8)
    decision = _decision(signal, RiskDecisionType.APPROVE)

    assert build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        state=OrderManagerState(used_signal_ids={signal.signal_id}),
        config=_config(),
    ).reasons == ["duplicate_signal_id"]

    assert build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        state=OrderManagerState(open_orders=[_open_order(side=OrderIntentSide.BUY)]),
        config=_config(max_open_orders_per_symbol=5),
    ).reasons == ["duplicate_open_order"]

    assert build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        state=OrderManagerState(open_orders=[_open_order(side=OrderIntentSide.SELL)]),
        config=_config(max_open_orders_per_symbol=5),
    ).reasons == ["conflicting_open_order"]

    assert build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        config=_config(max_open_orders_per_symbol=0),
    ).reasons == ["max_open_orders_per_symbol_hit"]

    assert build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=0,
        config=_config(),
    ).reasons == ["invalid_quantity"]


def test_close_position_reservation_blocks_entries_and_duplicate_close() -> None:
    close_signal = make_model_signal(signal=SignalSide.EXIT, index=9)
    close_result = build_order_intent(
        signal=close_signal,
        decision=_decision(close_signal, RiskDecisionType.EXIT),
        risk_log_succeeded=True,
        config=_config(),
    )
    state = OrderManagerState()
    assert close_result.intent is not None
    reserve_order_intent(state=state, intent=close_result.intent)

    assert state.open_orders[0].side is OrderIntentSide.CLOSE_POSITION
    assert state.open_orders[0].quantity is None

    entry_signal = make_model_signal(signal=SignalSide.BUY, index=10)
    entry_result = build_order_intent(
        signal=entry_signal,
        decision=_decision(entry_signal, RiskDecisionType.APPROVE),
        risk_log_succeeded=True,
        desired_quantity=1,
        state=state,
        config=_config(),
    )
    assert entry_result.allowed is False
    assert entry_result.reasons == ["liquidation_in_progress"]

    second_close_signal = make_model_signal(signal=SignalSide.EXIT, index=11)
    second_close_result = build_order_intent(
        signal=second_close_signal,
        decision=_decision(second_close_signal, RiskDecisionType.EXIT),
        risk_log_succeeded=True,
        state=state,
        config=_config(),
    )
    assert second_close_result.allowed is False
    assert second_close_result.reasons == ["close_position_already_in_progress"]


def test_reserve_and_release_open_order_state() -> None:
    first_signal = make_model_signal(signal=SignalSide.BUY, index=12)
    first_result = build_order_intent(
        signal=first_signal,
        decision=_decision(first_signal, RiskDecisionType.APPROVE),
        risk_log_succeeded=True,
        desired_quantity=2,
        config=_config(),
    )
    state = OrderManagerState()
    assert first_result.intent is not None
    reserve_order_intent(state=state, intent=first_result.intent)

    order_id = state.open_orders[0].order_id
    assert state.open_orders[0].side is OrderIntentSide.BUY
    assert state.open_orders[0].quantity == 2
    assert first_signal.signal_id in state.used_signal_ids
    assert release_open_order(state=state, order_id="missing") is False
    assert release_open_order(state=state, order_id=order_id) is True
    assert state.open_orders == []
    assert first_signal.signal_id in state.used_signal_ids

    next_signal = make_model_signal(signal=SignalSide.BUY, index=13)
    next_result = build_order_intent(
        signal=next_signal,
        decision=_decision(next_signal, RiskDecisionType.APPROVE),
        risk_log_succeeded=True,
        desired_quantity=1,
        state=state,
        config=_config(max_open_orders_per_symbol=1),
    )
    assert next_result.allowed is True


def test_release_close_position_placeholder_allows_new_entries() -> None:
    close_signal = make_model_signal(signal=SignalSide.EXIT, index=14)
    close_result = build_order_intent(
        signal=close_signal,
        decision=_decision(close_signal, RiskDecisionType.EXIT),
        risk_log_succeeded=True,
        config=_config(),
    )
    state = OrderManagerState()
    assert close_result.intent is not None
    reserve_order_intent(state=state, intent=close_result.intent)
    assert release_open_order(state=state, order_id=state.open_orders[0].order_id) is True

    entry_signal = make_model_signal(signal=SignalSide.BUY, index=15)
    entry_result = build_order_intent(
        signal=entry_signal,
        decision=_decision(entry_signal, RiskDecisionType.APPROVE),
        risk_log_succeeded=True,
        desired_quantity=1,
        state=state,
        config=_config(),
    )
    assert entry_result.allowed is True


def test_order_manager_signature_and_dependencies() -> None:
    parameters = signature(build_order_intent).parameters
    source = Path("src/market_relay_engine/execution/order_manager.py").read_text(
        encoding="utf-8"
    )

    assert "decision" in parameters
    assert "risk_log_succeeded" in parameters
    assert "risk_log_result" not in parameters
    assert "floor(" not in source
    assert "quantity_rounds_to_zero" not in source
    assert "alpaca" not in source.lower()
    assert "market_relay_engine.questdb" not in source


def _config(
    *,
    default_quantity: float = 1,
    max_open_orders_per_symbol: int = 5,
) -> OrderManagerConfig:
    return OrderManagerConfig(
        default_quantity=default_quantity,
        max_open_orders_per_symbol=max_open_orders_per_symbol,
    )


def _decision(
    signal,
    decision: RiskDecisionType,
    *,
    reduce_size_factor: float | None = None,
):
    return make_risk_decision(
        model_signal=signal,
        ticker=signal.ticker,
        decision=decision,
        approved=decision in {
            RiskDecisionType.APPROVE,
            RiskDecisionType.REDUCE_SIZE,
            RiskDecisionType.EXIT,
        },
        reduce_size_factor=reduce_size_factor,
    )


def _open_order(
    *,
    side: OrderIntentSide,
    ticker: str = "XOM",
    order_id: str = "existing_order",
    quantity: float | None = 1,
) -> OpenOrderState:
    return OpenOrderState(
        order_id=order_id,
        ticker=ticker,
        side=side,
        quantity=quantity,
        source_signal_id="existing_signal",
        status="reserved",
    )
