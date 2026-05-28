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


def test_approve_creates_full_size_buy_intent() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=1)
    decision = _decision(signal=signal, decision=RiskDecisionType.APPROVE)

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        config=_config(),
    )

    assert result.allowed is True
    assert result.intent is not None
    assert result.intent.side is OrderIntentSide.BUY
    assert result.intent.quantity == 10
    assert result.effective_quantity == 10
    assert result.reasons == ["risk_decision_approved"]


def test_approve_creates_full_size_sell_intent() -> None:
    signal = make_model_signal(signal=SignalSide.SELL, index=2)
    decision = _decision(signal=signal, decision=RiskDecisionType.APPROVE)

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=3,
        config=_config(),
    )

    assert result.allowed is True
    assert result.intent is not None
    assert result.intent.side is OrderIntentSide.SELL
    assert result.intent.quantity == 3


def test_reduce_size_creates_reduced_intent_not_full_size() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=3)
    decision = _decision(
        signal=signal,
        decision=RiskDecisionType.REDUCE_SIZE,
        reduce_size_factor=0.5,
    )

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=8,
        config=_config(),
    )

    assert result.allowed is True
    assert result.intent is not None
    assert result.intent.quantity == 4
    assert result.intent.quantity != 8
    assert result.reasons == ["risk_decision_reduced_size"]


@pytest.mark.parametrize(
    ("desired_quantity", "factor", "expected_quantity"),
    [(1, 0.5, 0.5), (1.5, 0.5, 0.75)],
)
def test_reduce_size_preserves_fractional_quantity(
    desired_quantity: float,
    factor: float,
    expected_quantity: float,
) -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=4)
    decision = _decision(
        signal=signal,
        decision=RiskDecisionType.REDUCE_SIZE,
        reduce_size_factor=factor,
    )

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=desired_quantity,
        config=_config(),
    )

    assert result.allowed is True
    assert result.intent is not None
    assert result.intent.quantity == pytest.approx(expected_quantity)


@pytest.mark.parametrize("factor", [None, 0.0, -0.1, 1.5])
def test_invalid_reduce_size_factor_blocks(factor: float | None) -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=5)
    decision = _decision(
        signal=signal,
        decision=RiskDecisionType.REDUCE_SIZE,
        reduce_size_factor=factor,
    )

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        config=_config(),
    )

    assert result.allowed is False
    assert result.intent is None
    assert result.reasons == ["invalid_reduce_size_factor"]


def test_block_creates_no_intent() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=6)
    decision = _decision(signal=signal, decision=RiskDecisionType.BLOCK)

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        config=_config(),
    )

    assert result.allowed is False
    assert result.intent is None
    assert result.reasons == ["risk_decision_blocked"]


def test_do_nothing_creates_no_intent() -> None:
    signal = make_model_signal(signal=SignalSide.DO_NOTHING, index=7)
    decision = _decision(signal=signal, decision=RiskDecisionType.DO_NOTHING)

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        config=_config(),
    )

    assert result.allowed is False
    assert result.intent is None
    assert result.reasons == ["risk_decision_do_nothing"]


def test_exit_creates_close_position_intent_with_no_quantity() -> None:
    signal = make_model_signal(signal=SignalSide.EXIT, index=8)
    decision = _decision(signal=signal, decision=RiskDecisionType.EXIT)

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=99,
        config=_config(),
    )

    assert result.allowed is True
    assert result.intent is not None
    assert result.intent.side is OrderIntentSide.CLOSE_POSITION
    assert result.intent.quantity is None
    assert result.effective_quantity is None
    assert result.reasons == ["exit_close_position_allowed"]


def test_exit_bypasses_invalid_quantity_and_failed_logging_by_default() -> None:
    signal = make_model_signal(signal=SignalSide.EXIT, index=9)
    decision = _decision(signal=signal, decision=RiskDecisionType.EXIT)

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=False,
        desired_quantity=0,
        config=_config(),
    )

    assert result.allowed is True
    assert result.intent is not None
    assert result.intent.quantity is None


def test_entry_with_failed_risk_logging_blocks_by_default() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=10)
    decision = _decision(signal=signal, decision=RiskDecisionType.APPROVE)

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=False,
        desired_quantity=10,
        config=_config(),
    )

    assert result.allowed is False
    assert result.reasons == ["risk_log_failed"]


def test_approve_with_approved_false_blocks() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=101)
    decision = make_risk_decision(
        model_signal=signal,
        ticker=signal.ticker,
        decision=RiskDecisionType.APPROVE,
        approved=False,
    )

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        config=_config(),
    )

    assert result.allowed is False
    assert result.reasons == ["risk_decision_not_approved"]


def test_reduce_size_with_approved_false_blocks() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=102)
    decision = make_risk_decision(
        model_signal=signal,
        ticker=signal.ticker,
        decision=RiskDecisionType.REDUCE_SIZE,
        approved=False,
        reduce_size_factor=0.5,
    )

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        config=_config(),
    )

    assert result.allowed is False
    assert result.reasons == ["risk_decision_not_approved"]


def test_exit_with_approved_false_blocks() -> None:
    signal = make_model_signal(signal=SignalSide.EXIT, index=103)
    decision = make_risk_decision(
        model_signal=signal,
        ticker=signal.ticker,
        decision=RiskDecisionType.EXIT,
        approved=False,
    )

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=None,
        config=_config(),
    )

    assert result.allowed is False
    assert result.reasons == ["risk_decision_not_approved"]


@pytest.mark.parametrize(
    ("decision_type", "signal_side", "reduce_size_factor", "expected_side"),
    [
        (RiskDecisionType.APPROVE, SignalSide.BUY, None, OrderIntentSide.BUY),
        (RiskDecisionType.REDUCE_SIZE, SignalSide.BUY, 0.5, OrderIntentSide.BUY),
        (RiskDecisionType.EXIT, SignalSide.EXIT, None, OrderIntentSide.CLOSE_POSITION),
    ],
)
def test_approved_true_decisions_still_create_intents(
    decision_type: RiskDecisionType,
    signal_side: SignalSide,
    reduce_size_factor: float | None,
    expected_side: OrderIntentSide,
) -> None:
    signal = make_model_signal(signal=signal_side, index=104)
    decision = make_risk_decision(
        model_signal=signal,
        ticker=signal.ticker,
        decision=decision_type,
        approved=True,
        reduce_size_factor=reduce_size_factor,
    )

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        config=_config(),
    )

    assert result.allowed is True
    assert result.intent is not None
    assert result.intent.side is expected_side


def test_approve_decision_with_mismatched_model_signal_id_blocks() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=10)
    stale_signal = make_model_signal(signal=SignalSide.BUY, index=110)
    decision = _decision(signal=stale_signal, decision=RiskDecisionType.APPROVE)

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        config=_config(),
    )

    assert result.allowed is False
    assert result.reasons == ["risk_decision_signal_mismatch"]


def test_approve_decision_with_mismatched_ticker_blocks() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, ticker="XOM", index=10)
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
    signal = make_model_signal(signal=SignalSide.BUY, index=10)
    stale_signal = make_model_signal(signal=SignalSide.BUY, index=111)
    decision = _decision(
        signal=stale_signal,
        decision=RiskDecisionType.REDUCE_SIZE,
        reduce_size_factor=0.5,
    )

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        config=_config(),
    )

    assert result.allowed is False
    assert result.reasons == ["risk_decision_signal_mismatch"]


def test_exit_decision_with_mismatched_model_signal_id_blocks() -> None:
    signal = make_model_signal(signal=SignalSide.EXIT, index=10)
    stale_signal = make_model_signal(signal=SignalSide.EXIT, index=112)
    decision = _decision(signal=stale_signal, decision=RiskDecisionType.EXIT)

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=False,
        desired_quantity=None,
        config=_config(),
    )

    assert result.allowed is False
    assert result.reasons == ["risk_decision_signal_mismatch"]


def test_matching_signal_and_decision_still_works() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=10)
    decision = _decision(signal=signal, decision=RiskDecisionType.APPROVE)

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        config=_config(),
    )

    assert result.allowed is True
    assert result.intent is not None
    assert result.intent.source_signal_id == signal.signal_id


def test_duplicate_signal_id_blocks() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=11)
    decision = _decision(signal=signal, decision=RiskDecisionType.APPROVE)
    state = OrderManagerState(used_signal_ids={signal.signal_id})

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        state=state,
        config=_config(),
    )

    assert result.allowed is False
    assert result.reasons == ["duplicate_signal_id"]


def test_duplicate_same_side_open_order_blocks() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=12)
    decision = _decision(signal=signal, decision=RiskDecisionType.APPROVE)
    state = OrderManagerState(open_orders=[_open_order(side=OrderIntentSide.BUY)])

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        state=state,
        config=_config(max_open_orders_per_symbol=5),
    )

    assert result.allowed is False
    assert result.reasons == ["duplicate_open_order"]


def test_conflicting_opposite_side_open_order_blocks() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=13)
    decision = _decision(signal=signal, decision=RiskDecisionType.APPROVE)
    state = OrderManagerState(open_orders=[_open_order(side=OrderIntentSide.SELL)])

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        state=state,
        config=_config(max_open_orders_per_symbol=5),
    )

    assert result.allowed is False
    assert result.reasons == ["conflicting_open_order"]


def test_max_open_orders_per_symbol_blocks() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=14)
    decision = _decision(signal=signal, decision=RiskDecisionType.APPROVE)

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=10,
        config=_config(max_open_orders_per_symbol=0),
    )

    assert result.allowed is False
    assert result.reasons == ["max_open_orders_per_symbol_hit"]


def test_invalid_entry_quantity_blocks() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=15)
    decision = _decision(signal=signal, decision=RiskDecisionType.APPROVE)

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=0,
        config=_config(),
    )

    assert result.allowed is False
    assert result.reasons == ["invalid_quantity"]


def test_exit_bypasses_buy_sell_open_order_conflicts_and_limits() -> None:
    signal = make_model_signal(signal=SignalSide.EXIT, index=16)
    decision = _decision(signal=signal, decision=RiskDecisionType.EXIT)
    state = OrderManagerState(
        open_orders=[
            _open_order(side=OrderIntentSide.BUY, order_id="buy_1"),
            _open_order(side=OrderIntentSide.SELL, order_id="sell_1"),
        ]
    )

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=False,
        desired_quantity=0,
        state=state,
        config=_config(max_open_orders_per_symbol=0),
    )

    assert result.allowed is True
    assert result.intent is not None
    assert result.intent.side is OrderIntentSide.CLOSE_POSITION


def test_duplicate_close_position_blocks() -> None:
    signal = make_model_signal(signal=SignalSide.EXIT, index=17)
    decision = _decision(signal=signal, decision=RiskDecisionType.EXIT)
    state = OrderManagerState(open_orders=[_open_order(side=OrderIntentSide.CLOSE_POSITION)])

    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=None,
        state=state,
        config=_config(),
    )

    assert result.allowed is False
    assert result.reasons == ["close_position_already_in_progress"]


def test_liquidation_in_progress_blocks_new_entries_until_release() -> None:
    close_signal = make_model_signal(signal=SignalSide.EXIT, index=18)
    close_decision = _decision(signal=close_signal, decision=RiskDecisionType.EXIT)
    close_result = build_order_intent(
        signal=close_signal,
        decision=close_decision,
        risk_log_succeeded=True,
        state=OrderManagerState(),
        config=_config(),
    )
    state = OrderManagerState()
    assert close_result.intent is not None
    reserve_order_intent(state=state, intent=close_result.intent)

    entry_signal = make_model_signal(signal=SignalSide.BUY, index=19)
    entry_decision = _decision(signal=entry_signal, decision=RiskDecisionType.APPROVE)
    blocked = build_order_intent(
        signal=entry_signal,
        decision=entry_decision,
        risk_log_succeeded=True,
        desired_quantity=1,
        state=state,
        config=_config(),
    )

    assert blocked.allowed is False
    assert blocked.reasons == ["liquidation_in_progress"]
    assert release_open_order(state=state, order_id=state.open_orders[0].order_id) is True

    allowed = build_order_intent(
        signal=entry_signal,
        decision=entry_decision,
        risk_log_succeeded=True,
        desired_quantity=1,
        state=state,
        config=_config(),
    )
    assert allowed.allowed is True


def test_build_order_intent_signature_is_decoupled_from_risk_log_result() -> None:
    parameters = signature(build_order_intent).parameters

    assert "decision" in parameters
    assert "risk_log_succeeded" in parameters
    assert "risk_log_result" not in parameters


def test_reserve_order_intent_marks_signal_used_and_adds_buy_placeholder() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=20)
    decision = _decision(signal=signal, decision=RiskDecisionType.APPROVE)
    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=5,
        config=_config(),
    )
    state = OrderManagerState()

    assert result.intent is not None
    reserve_order_intent(state=state, intent=result.intent)

    assert signal.signal_id in state.used_signal_ids
    assert len(state.open_orders) == 1
    assert state.open_orders[0].order_id == f"reserved_order_{signal.signal_id}"
    assert state.open_orders[0].side is OrderIntentSide.BUY
    assert state.open_orders[0].quantity == 5


def test_reserve_order_intent_adds_close_position_placeholder_without_quantity() -> None:
    signal = make_model_signal(signal=SignalSide.EXIT, index=21)
    decision = _decision(signal=signal, decision=RiskDecisionType.EXIT)
    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        config=_config(),
    )
    state = OrderManagerState()

    assert result.intent is not None
    reserve_order_intent(state=state, intent=result.intent)

    assert signal.signal_id in state.used_signal_ids
    assert len(state.open_orders) == 1
    assert state.open_orders[0].side is OrderIntentSide.CLOSE_POSITION
    assert state.open_orders[0].quantity is None


def test_release_open_order_removes_placeholder_but_keeps_signal_used() -> None:
    signal = make_model_signal(signal=SignalSide.BUY, index=22)
    decision = _decision(signal=signal, decision=RiskDecisionType.APPROVE)
    result = build_order_intent(
        signal=signal,
        decision=decision,
        risk_log_succeeded=True,
        desired_quantity=1,
        config=_config(),
    )
    state = OrderManagerState()
    assert result.intent is not None
    reserve_order_intent(state=state, intent=result.intent)
    order_id = state.open_orders[0].order_id

    assert release_open_order(state=state, order_id="missing_order") is False
    assert release_open_order(state=state, order_id=order_id) is True
    assert state.open_orders == []
    assert signal.signal_id in state.used_signal_ids


def test_release_open_order_frees_symbol_for_different_signal() -> None:
    first_signal = make_model_signal(signal=SignalSide.BUY, index=23)
    first_decision = _decision(signal=first_signal, decision=RiskDecisionType.APPROVE)
    first_result = build_order_intent(
        signal=first_signal,
        decision=first_decision,
        risk_log_succeeded=True,
        desired_quantity=1,
        config=_config(),
    )
    state = OrderManagerState()
    assert first_result.intent is not None
    reserve_order_intent(state=state, intent=first_result.intent)

    second_signal = make_model_signal(signal=SignalSide.BUY, index=24)
    second_decision = _decision(signal=second_signal, decision=RiskDecisionType.APPROVE)
    assert release_open_order(state=state, order_id=state.open_orders[0].order_id) is True

    second_result = build_order_intent(
        signal=second_signal,
        decision=second_decision,
        risk_log_succeeded=True,
        desired_quantity=1,
        state=state,
        config=_config(max_open_orders_per_symbol=1),
    )

    assert second_result.allowed is True


def test_order_manager_has_no_broker_or_questdb_dependency() -> None:
    source = Path("src/market_relay_engine/execution/order_manager.py").read_text(
        encoding="utf-8"
    )

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
    *,
    signal,
    decision: RiskDecisionType,
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
