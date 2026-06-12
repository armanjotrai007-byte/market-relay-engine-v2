from __future__ import annotations

import dataclasses
import inspect

import pytest

from market_relay_engine.execution import fake_paper_loop
from market_relay_engine.execution.alpaca_paper import AlpacaPaperClient
from market_relay_engine.execution.fake_paper_loop import (
    FIXED_FILL_TIME,
    FIXED_SUBMIT_COMPLETED_AT,
    FIXED_SUBMIT_STARTED_AT,
    FakePaperLoopConfig,
    FakePaperLoopError,
    FakePaperLoopResult,
    run_fake_paper_cycle,
)
from market_relay_engine.execution.fill_reconciliation import (
    BrokerPositionSnapshot,
    reconcile_position,
)
from market_relay_engine.execution.order_manager import OrderIntent, OrderIntentSide
from market_relay_engine.execution.position_state import PortfolioState, PositionState


def test_default_fake_paper_cycle_completes_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_submit_order(*args: object, **kwargs: object) -> object:
        raise AssertionError("AlpacaPaperClient.submit_order must not be called")

    monkeypatch.setattr(AlpacaPaperClient, "submit_order", fail_submit_order)

    result = run_fake_paper_cycle()

    assert isinstance(result, FakePaperLoopResult)
    assert result.order_submission_result.success is True
    assert result.fill_event.fill_id == "fill_fake_pr23"
    assert result.position_update.duplicate_fill is False
    position = result.final_portfolio.get_position("AAPL")
    assert position is not None
    assert position.quantity == 1.0
    assert result.reconciliation is not None
    assert result.reconciliation.matched is True
    assert result.order_state.open_orders == []
    assert "signal_fake_pr23" in result.order_state.used_signal_ids
    assert "questdb" not in inspect.getsource(fake_paper_loop).lower()


def test_frozen_intent_customization_uses_dataclasses_replace() -> None:
    original = OrderIntent(
        ticker="AAPL",
        side=OrderIntentSide.BUY,
        quantity=1.0,
        source_signal_id="signal_replace_test",
        risk_decision_id="risk_replace_test",
        reason="risk_decision_approved",
    )
    customized = dataclasses.replace(
        original,
        order_type="MARKET",
        time_in_force="day",
    )

    assert original.order_type == "limit"
    assert original.time_in_force == "day"
    assert customized.order_type == "MARKET"
    assert "dataclasses.replace" in inspect.getsource(fake_paper_loop.run_fake_paper_cycle)
    assert run_fake_paper_cycle().order_intent.order_type == "MARKET"


def test_reserve_order_intent_is_called_before_mocked_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    original_reserve = fake_paper_loop.reserve_order_intent
    original_capture = fake_paper_loop.capture_order_submission_result

    def spy_reserve(*, state: object, intent: object) -> None:
        calls.append("reserve")
        original_reserve(state=state, intent=intent)  # type: ignore[arg-type]

    def spy_capture(**kwargs: object) -> object:
        calls.append("capture")
        return original_capture(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fake_paper_loop, "reserve_order_intent", spy_reserve)
    monkeypatch.setattr(fake_paper_loop, "capture_order_submission_result", spy_capture)

    run_fake_paper_cycle()

    assert calls.index("reserve") < calls.index("capture")


def test_release_open_order_is_called_after_fake_fill_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    original_apply = fake_paper_loop.apply_fill_and_reconcile
    original_release = fake_paper_loop.release_open_order

    def spy_apply(**kwargs: object) -> object:
        calls.append("apply_fill")
        return original_apply(**kwargs)  # type: ignore[arg-type]

    def spy_release(*, state: object, order_id: str) -> bool:
        calls.append("release")
        return original_release(state=state, order_id=order_id)  # type: ignore[arg-type]

    monkeypatch.setattr(fake_paper_loop, "apply_fill_and_reconcile", spy_apply)
    monkeypatch.setattr(fake_paper_loop, "release_open_order", spy_release)

    result = run_fake_paper_cycle()

    assert calls.index("apply_fill") < calls.index("release")
    assert result.order_state.open_orders == []


def test_exact_fake_fill_payload_is_used_for_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_payload: dict[str, object] = {}
    original_convert = fake_paper_loop.fill_event_from_alpaca_fill_payload
    config = FakePaperLoopConfig(
        ticker="MSFT",
        quantity=2.5,
        fill_price=101.25,
        execution_id="execution_exact_payload",
        broker_order_id="broker_order_not_fill_id",
    )

    def spy_convert(**kwargs: object) -> object:
        captured_payload.update(kwargs["payload"])  # type: ignore[arg-type]
        return original_convert(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fake_paper_loop, "fill_event_from_alpaca_fill_payload", spy_convert)

    result = run_fake_paper_cycle(config)

    assert captured_payload == {
        "execution_id": "execution_exact_payload",
        "symbol": "MSFT",
        "side": "buy",
        "qty": "2.5",
        "price": "101.25",
        "transaction_time": FIXED_FILL_TIME.isoformat().replace("+00:00", "Z"),
        "status": "filled",
    }
    assert result.fill_event.fill_id == "execution_exact_payload"
    assert result.fill_event.fill_id != "broker_order_not_fill_id"


def test_id_propagation_through_order_result_and_fill_event() -> None:
    config = FakePaperLoopConfig(
        source_signal_id="signal_custom",
        risk_decision_id="risk_custom",
        local_order_id="local_custom",
        client_order_id="client_custom",
        broker_order_id="broker_custom",
        execution_id="execution_custom",
        trace_id="trace_custom",
    )

    result = run_fake_paper_cycle(config)

    assert result.order_submission_result.source_signal_id == "signal_custom"
    assert result.order_submission_result.risk_decision_id == "risk_custom"
    assert result.order_submission_result.local_order_id == "local_custom"
    assert result.order_submission_result.client_order_id == "client_custom"
    assert result.order_submission_result.broker_order_id == "broker_custom"
    assert result.order_submission_result.trace_id == "trace_custom"
    assert result.fill_event.model_signal_id == "signal_custom"
    assert result.fill_event.risk_decision_id == "risk_custom"
    assert result.fill_event.trace_id == "trace_custom"
    assert result.fill_event.order_id == "local_custom"


def test_slippage_direction_and_bps_for_buy_and_sell() -> None:
    buy = run_fake_paper_cycle(
        FakePaperLoopConfig(side="BUY", arrival_midprice=100.0, fill_price=100.02)
    )
    sell = run_fake_paper_cycle(
        FakePaperLoopConfig(side="SELL", arrival_midprice=100.0, fill_price=99.98)
    )

    assert buy.fill_event.slippage == pytest.approx(0.02)
    assert buy.fill_event.slippage_bps == pytest.approx(2.0)
    assert sell.fill_event.slippage == pytest.approx(0.02)
    assert sell.fill_event.slippage_bps == pytest.approx(2.0)


def test_independent_reconciliation_uses_starting_quantity_plus_signed_delta() -> None:
    portfolio = PortfolioState(
        positions={"AAPL": PositionState(ticker="AAPL", quantity=5.0, average_price=99.0)}
    )

    result = run_fake_paper_cycle(FakePaperLoopConfig(quantity=2.0), portfolio=portfolio)

    assert result.reconciliation is not None
    assert result.reconciliation.broker_quantity == 7.0
    assert result.reconciliation.local_quantity == 7.0
    assert result.reconciliation.matched is True

    mismatch = reconcile_position(
        portfolio=result.final_portfolio,
        broker_position=BrokerPositionSnapshot(ticker="AAPL", quantity=8.0),
    )
    assert mismatch.matched is False


def test_buy_cycle_opens_long_and_sell_cycle_opens_short() -> None:
    buy_result = run_fake_paper_cycle(FakePaperLoopConfig(side="BUY"))
    sell_result = run_fake_paper_cycle(FakePaperLoopConfig(side="SELL"))

    buy_position = buy_result.final_portfolio.get_position("AAPL")
    sell_position = sell_result.final_portfolio.get_position("AAPL")
    assert buy_position is not None
    assert sell_position is not None
    assert buy_position.quantity > 0
    assert sell_position.quantity < 0
    assert buy_result.resolved_intent.side is OrderIntentSide.BUY
    assert sell_result.resolved_intent.side is OrderIntentSide.SELL


def test_fixed_timestamps_are_deterministic_constants() -> None:
    first = run_fake_paper_cycle()
    second = run_fake_paper_cycle()

    assert first.order_submission_result.submit_started_at == FIXED_SUBMIT_STARTED_AT
    assert first.order_submission_result.submit_completed_at == FIXED_SUBMIT_COMPLETED_AT
    assert first.fill_event.fill_time == FIXED_FILL_TIME
    assert second.order_submission_result.submit_started_at == FIXED_SUBMIT_STARTED_AT
    assert second.order_submission_result.submit_completed_at == FIXED_SUBMIT_COMPLETED_AT
    assert second.fill_event.fill_time == FIXED_FILL_TIME


def test_no_round_trip_helper_is_added() -> None:
    assert not hasattr(fake_paper_loop, "run_fake_round_trip_cycle")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"side": "HOLD"},
        {"quantity": 0.0},
        {"quantity": float("inf")},
        {"arrival_midprice": 0.0},
        {"fill_price": -1.0},
        {"order_type": "LIMIT"},
        {"ticker": ""},
        {"source_signal_id": ""},
    ],
)
def test_config_validation_rejects_invalid_inputs(kwargs: dict[str, object]) -> None:
    with pytest.raises(FakePaperLoopError):
        FakePaperLoopConfig(**kwargs)
