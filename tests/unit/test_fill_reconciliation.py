from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from market_relay_engine.contracts.execution import FillEvent, OrderSide
from market_relay_engine.execution.execution_metrics import OrderSubmissionResult
from market_relay_engine.execution.fill_reconciliation import (
    BrokerPositionSnapshot,
    FillReconciliationError,
    apply_fill_and_reconcile,
    broker_position_snapshot_from_alpaca_payload,
    build_position_reconciliation_health_event,
    fill_event_from_alpaca_fill_payload,
    reconcile_position,
)
from market_relay_engine.execution.position_state import PortfolioState, PositionState
from market_relay_engine.questdb.writer import TABLE_COLUMNS, fill_event_to_row


STARTED_AT = datetime(2026, 1, 2, 14, 30, 0, tzinfo=UTC)
COMPLETED_AT = STARTED_AT + timedelta(milliseconds=100)
FILL_TIME = datetime(2026, 1, 2, 14, 31, 0, tzinfo=UTC)


def test_execution_level_buy_fill_converts_to_fill_event() -> None:
    event = fill_event_from_alpaca_fill_payload(
        payload=_payload(side="buy", price="101.25"),
        order_result=_order_result(),
        expected_price=100.0,
    )

    assert event.fill_id == "execution_1"
    assert event.broker_fill_id == "execution_1"
    assert event.order_id == "local_order_1"
    assert event.ticker == "AAPL"
    assert event.side is OrderSide.BUY
    assert event.quantity == 2.0
    assert event.fill_price == 101.25
    assert event.expected_price == 100.0
    assert event.slippage == 1.25
    assert event.slippage_bps == 125.0
    assert event.model_signal_id == "signal_1"
    assert event.risk_decision_id == "risk_1"
    assert event.trace_id == "trace_1"


def test_execution_level_sell_fill_converts_to_fill_event() -> None:
    event = fill_event_from_alpaca_fill_payload(
        payload=_payload(side="sell", price="99.50"),
        order_result=_order_result(),
        expected_price=100.0,
    )

    assert event.side is OrderSide.SELL
    assert event.slippage == 0.5
    assert event.slippage_bps == 50.0


@pytest.mark.parametrize("id_key", ["execution_id", "activity_id", "id", "trade_id"])
def test_execution_fill_id_keys_are_accepted(id_key: str) -> None:
    payload = _payload()
    payload.pop("execution_id")
    payload[id_key] = f"{id_key}_1"

    event = fill_event_from_alpaca_fill_payload(payload=payload, order_result=_order_result())

    assert event.fill_id == f"{id_key}_1"
    assert event.broker_fill_id == f"{id_key}_1"


def test_missing_unique_execution_fill_id_rejects() -> None:
    payload = _payload()
    payload.pop("execution_id")

    with pytest.raises(FillReconciliationError, match="fill_id"):
        fill_event_from_alpaca_fill_payload(payload=payload, order_result=_order_result())


def test_aggregate_order_payload_without_execution_id_rejects() -> None:
    payload = {
        "order_id": "broker_order_1",
        "symbol": "AAPL",
        "side": "buy",
        "filled_qty": "2",
        "avg_price": "100.0",
        "filled_at": FILL_TIME,
    }

    with pytest.raises(FillReconciliationError, match="fill_id"):
        fill_event_from_alpaca_fill_payload(payload=payload, order_result=_order_result())


def test_broker_order_id_is_not_used_as_fill_id_or_field() -> None:
    event = fill_event_from_alpaca_fill_payload(
        payload=_payload(order_id="broker_order_1"),
        order_result=_order_result(),
    )

    assert event.fill_id == "execution_1"
    assert not hasattr(event, "broker_order_id")


def test_order_result_supplies_order_correlation_fallbacks() -> None:
    assert (
        fill_event_from_alpaca_fill_payload(
            payload=_payload(),
            order_result=_order_result(local_order_id="local", client_order_id="client"),
        ).order_id
        == "local"
    )
    assert (
        fill_event_from_alpaca_fill_payload(
            payload=_payload(),
            order_result=_order_result(local_order_id=None, client_order_id="client"),
        ).order_id
        == "client"
    )
    assert (
        fill_event_from_alpaca_fill_payload(
            payload=_payload(),
            order_result=_order_result(
                local_order_id=None,
                client_order_id=None,
                source_signal_id="signal_fallback",
            ),
        ).order_id
        == "signal_fallback"
    )


def test_missing_order_correlation_rejects() -> None:
    with pytest.raises(FillReconciliationError, match="order correlation"):
        fill_event_from_alpaca_fill_payload(
            payload=_payload(),
            order_result=_order_result(
                local_order_id=None,
                client_order_id=None,
                source_signal_id=None,
            ),
        )


def test_source_signal_maps_to_model_signal_and_risk_decision_maps_through() -> None:
    event = fill_event_from_alpaca_fill_payload(
        payload=_payload(),
        order_result=_order_result(
            source_signal_id="signal_capture",
            risk_decision_id="risk_capture",
        ),
    )

    assert event.model_signal_id == "signal_capture"
    assert event.risk_decision_id == "risk_capture"


def test_expected_price_explicit_argument_wins_over_arrival_midprice() -> None:
    event = fill_event_from_alpaca_fill_payload(
        payload=_payload(price=101.0),
        order_result=_order_result(arrival_midprice=99.0),
        expected_price=100.0,
    )

    assert event.expected_price == 100.0
    assert event.slippage == 1.0


def test_expected_price_falls_back_to_arrival_midprice() -> None:
    event = fill_event_from_alpaca_fill_payload(
        payload=_payload(price=101.0),
        order_result=_order_result(arrival_midprice=100.0),
    )

    assert event.expected_price == 100.0
    assert event.slippage == 1.0


def test_missing_expected_price_does_not_crash() -> None:
    event = fill_event_from_alpaca_fill_payload(
        payload=_payload(price=101.0),
        order_result=_order_result(arrival_midprice=None),
    )

    assert event.expected_price is None
    assert event.slippage is None
    assert event.slippage_bps is None


@pytest.mark.parametrize("expected_price", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_expected_price_disables_slippage(expected_price: float) -> None:
    event = fill_event_from_alpaca_fill_payload(
        payload=_payload(price=101.0),
        order_result=_order_result(arrival_midprice=100.0),
        expected_price=expected_price,
    )

    assert event.expected_price is None
    assert event.slippage is None
    assert event.slippage_bps is None


@pytest.mark.parametrize("quantity", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_quantity_rejects(quantity: float) -> None:
    with pytest.raises(FillReconciliationError, match="quantity"):
        fill_event_from_alpaca_fill_payload(
            payload=_payload(qty=quantity),
            order_result=_order_result(),
        )


@pytest.mark.parametrize("fill_price", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_fill_price_rejects(fill_price: float) -> None:
    with pytest.raises(FillReconciliationError, match="fill_price"):
        fill_event_from_alpaca_fill_payload(
            payload=_payload(price=fill_price),
            order_result=_order_result(),
        )


def test_invalid_side_rejects() -> None:
    with pytest.raises(FillReconciliationError, match="side"):
        fill_event_from_alpaca_fill_payload(
            payload=_payload(side="hold"),
            order_result=_order_result(),
        )


def test_naive_timestamp_rejects() -> None:
    with pytest.raises(FillReconciliationError, match="timezone-aware"):
        fill_event_from_alpaca_fill_payload(
            payload=_payload(transaction_time=datetime(2026, 1, 2, 14, 31, 0)),
            order_result=_order_result(),
        )


@pytest.mark.parametrize(
    ("side", "fill_price", "expected_price", "expected_slippage"),
    [
        ("buy", 101.0, 100.0, 1.0),
        ("buy", 99.0, 100.0, -1.0),
        ("sell", 99.0, 100.0, 1.0),
        ("sell", 101.0, 100.0, -1.0),
    ],
)
def test_slippage_signs(
    side: str,
    fill_price: float,
    expected_price: float,
    expected_slippage: float,
) -> None:
    event = fill_event_from_alpaca_fill_payload(
        payload=_payload(side=side, price=fill_price),
        order_result=_order_result(),
        expected_price=expected_price,
    )

    assert event.slippage == expected_slippage
    assert event.slippage_bps == expected_slippage / expected_price * 10000.0


def test_slippage_bps_never_divides_by_zero() -> None:
    event = fill_event_from_alpaca_fill_payload(
        payload=_payload(price=101.0),
        order_result=_order_result(),
        expected_price=0.0,
    )

    assert event.slippage_bps is None


def test_buy_fill_opens_long_through_apply_fill_and_reconcile() -> None:
    portfolio = PortfolioState()
    result = apply_fill_and_reconcile(
        portfolio=portfolio,
        fill_event=_fill_event(side=OrderSide.BUY, quantity=5, fill_price=100, fill_id="buy_1"),
    )

    assert result.position_update.position_opened is True
    assert result.position_update.new_quantity == 5
    assert portfolio.get_position("AAPL") is not None


def test_sell_fill_opens_short_through_apply_fill_and_reconcile() -> None:
    portfolio = PortfolioState()
    result = apply_fill_and_reconcile(
        portfolio=portfolio,
        fill_event=_fill_event(side=OrderSide.SELL, quantity=5, fill_price=100, fill_id="sell_1"),
    )

    assert result.position_update.position_opened is True
    assert result.position_update.new_quantity == -5


def test_closing_fill_updates_realized_pnl_through_pr19() -> None:
    portfolio = PortfolioState(
        positions={"AAPL": PositionState(ticker="AAPL", quantity=5, average_price=100)}
    )
    result = apply_fill_and_reconcile(
        portfolio=portfolio,
        fill_event=_fill_event(side=OrderSide.SELL, quantity=5, fill_price=110, fill_id="close_1"),
    )

    assert result.position_update.realized_pnl_delta == 50
    assert portfolio.account.total_realized_pnl == 50
    assert portfolio.get_position("AAPL") is None


def test_duplicate_fill_id_is_ignored_by_pr19() -> None:
    portfolio = PortfolioState()
    fill = _fill_event(side=OrderSide.BUY, quantity=5, fill_price=100, fill_id="duplicate_1")

    first = apply_fill_and_reconcile(portfolio=portfolio, fill_event=fill)
    duplicate = apply_fill_and_reconcile(portfolio=portfolio, fill_event=fill)

    assert first.position_update.new_quantity == 5
    assert duplicate.position_update.duplicate_fill is True
    assert duplicate.position_update.new_quantity == 5
    assert portfolio.get_position("AAPL").quantity == 5  # type: ignore[union-attr]


def test_broker_position_parses_long_short_and_signed_quantities() -> None:
    assert broker_position_snapshot_from_alpaca_payload(
        {"symbol": "aapl", "qty": "5", "side": "long"}
    ).quantity == 5
    assert broker_position_snapshot_from_alpaca_payload(
        {"symbol": "AAPL", "qty": "5", "side": "short"}
    ).quantity == -5
    assert broker_position_snapshot_from_alpaca_payload(
        {"symbol": "AAPL", "qty": "-3"}
    ).quantity == -3


def test_broker_position_validation_rejects_bad_inputs() -> None:
    with pytest.raises(FillReconciliationError, match="ticker"):
        broker_position_snapshot_from_alpaca_payload({"qty": "5"})
    with pytest.raises(FillReconciliationError, match="quantity"):
        broker_position_snapshot_from_alpaca_payload({"symbol": "AAPL", "qty": float("inf")})
    with pytest.raises(FillReconciliationError, match="side"):
        broker_position_snapshot_from_alpaca_payload(
            {"symbol": "AAPL", "qty": "5", "side": "flat-ish"}
        )


def test_reconciliation_match_mismatch_tolerance_and_absent_local_position() -> None:
    portfolio = PortfolioState(
        positions={"AAPL": PositionState(ticker="AAPL", quantity=5, average_price=100)}
    )

    matched = reconcile_position(
        portfolio=portfolio,
        broker_position=BrokerPositionSnapshot(ticker="AAPL", quantity=5),
    )
    mismatch = reconcile_position(
        portfolio=portfolio,
        broker_position=BrokerPositionSnapshot(ticker="AAPL", quantity=4),
    )
    tolerated = reconcile_position(
        portfolio=portfolio,
        broker_position=BrokerPositionSnapshot(ticker="AAPL", quantity=5.0001),
        tolerance=0.001,
    )
    flat_match = reconcile_position(
        portfolio=PortfolioState(),
        broker_position=BrokerPositionSnapshot(ticker="MSFT", quantity=0),
    )
    flat_mismatch = reconcile_position(
        portfolio=PortfolioState(),
        broker_position=BrokerPositionSnapshot(ticker="MSFT", quantity=1),
    )

    assert matched.matched is True
    assert matched.reasons == ["position_quantity_match"]
    assert mismatch.matched is False
    assert mismatch.reasons == ["position_quantity_mismatch"]
    assert tolerated.matched is True
    assert flat_match.matched is True
    assert flat_mismatch.matched is False


def test_apply_fill_and_reconcile_only_reconciles_when_snapshot_is_provided() -> None:
    portfolio = PortfolioState()

    without_snapshot = apply_fill_and_reconcile(
        portfolio=portfolio,
        fill_event=_fill_event(side=OrderSide.BUY, quantity=5, fill_price=100, fill_id="snap_1"),
    )
    with_stale_snapshot = apply_fill_and_reconcile(
        portfolio=portfolio,
        fill_event=_fill_event(side=OrderSide.BUY, quantity=1, fill_price=100, fill_id="snap_2"),
        broker_position=BrokerPositionSnapshot(ticker="AAPL", quantity=5),
    )

    assert without_snapshot.reconciliation is None
    assert with_stale_snapshot.reconciliation is not None
    assert with_stale_snapshot.reconciliation.matched is False


def test_position_reconciliation_health_event_statuses() -> None:
    matched = reconcile_position(
        portfolio=PortfolioState(),
        broker_position=BrokerPositionSnapshot(ticker="AAPL", quantity=0),
    )
    mismatch = reconcile_position(
        portfolio=PortfolioState(),
        broker_position=BrokerPositionSnapshot(ticker="AAPL", quantity=1),
    )

    ok_event = build_position_reconciliation_health_event(matched, event_time=FILL_TIME)
    warning_event = build_position_reconciliation_health_event(
        mismatch,
        event_time=FILL_TIME,
        trace_id="trace_health",
    )

    assert ok_event.component == "position_reconciliation"
    assert ok_event.status == "OK"
    assert warning_event.status == "WARNING"
    assert warning_event.trace_id == "trace_health"
    assert "position_quantity_mismatch" in (warning_event.message or "")


def test_fill_event_to_row_reads_fill_event_optional_attributes_by_default() -> None:
    event = _fill_event(
        side=OrderSide.BUY,
        quantity=2,
        fill_price=101,
        fill_id="row_1",
        slippage_bps=100.0,
        broker_fill_id="broker_fill_row_1",
        model_signal_id="signal_row_1",
        risk_decision_id="risk_row_1",
    )

    row = fill_event_to_row(event, write_time=FILL_TIME)

    assert row["slippage_bps"] == 100.0
    assert row["broker_fill_id"] == "broker_fill_row_1"
    assert row["model_signal_id"] == "signal_row_1"
    assert row["risk_decision_id"] == "risk_row_1"
    assert "broker_order_id" not in row
    assert set(row).issubset(set(TABLE_COLUMNS["fill_events"]))


def test_fill_event_to_row_explicit_kwargs_override_record_attributes() -> None:
    event = _fill_event(
        side=OrderSide.BUY,
        quantity=2,
        fill_price=101,
        fill_id="row_2",
        slippage_bps=100.0,
        broker_fill_id="broker_fill_row_2",
        model_signal_id="signal_row_2",
        risk_decision_id="risk_row_2",
    )

    row = fill_event_to_row(
        event,
        write_time=FILL_TIME,
        slippage_bps=25.0,
        broker_fill_id="explicit_broker_fill",
        model_signal_id="explicit_signal",
        risk_decision_id="explicit_risk",
    )

    assert row["slippage_bps"] == 25.0
    assert row["broker_fill_id"] == "explicit_broker_fill"
    assert row["model_signal_id"] == "explicit_signal"
    assert row["risk_decision_id"] == "explicit_risk"


def test_fill_reconciliation_source_keeps_pr22_scope_small() -> None:
    source = Path("src/market_relay_engine/execution/fill_reconciliation.py").read_text(
        encoding="utf-8"
    )

    assert "requests" not in source
    assert "AlpacaPaperClient" not in source
    assert "market_relay_engine.questdb" not in source
    assert "market_relay_engine.model" not in source
    assert "market_relay_engine.ai_context" not in source
    assert "market_relay_engine.context" not in source
    assert "async def" not in source
    assert "retry" not in source.lower()


def _order_result(**overrides: object) -> OrderSubmissionResult:
    values = {
        "local_order_id": "local_order_1",
        "client_order_id": "client_order_1",
        "broker_order_id": "broker_order_1",
        "ticker": "AAPL",
        "side": "BUY",
        "quantity": 2.0,
        "order_type": "MARKET",
        "time_in_force": "day",
        "submit_started_at": STARTED_AT,
        "submit_completed_at": COMPLETED_AT,
        "latency_ms": 100.0,
        "success": True,
        "source_signal_id": "signal_1",
        "risk_decision_id": "risk_1",
        "trace_id": "trace_1",
        "arrival_midprice": 100.0,
    }
    values.update(overrides)
    return OrderSubmissionResult(**values)


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "execution_id": "execution_1",
        "order_id": "broker_order_1",
        "symbol": "AAPL",
        "side": "buy",
        "qty": "2",
        "price": "101.0",
        "transaction_time": FILL_TIME,
        "status": "filled",
    }
    payload.update(overrides)
    return payload


def _fill_event(
    *,
    side: OrderSide,
    quantity: float,
    fill_price: float,
    fill_id: str,
    slippage_bps: float | None = None,
    broker_fill_id: str | None = None,
    model_signal_id: str | None = None,
    risk_decision_id: str | None = None,
) -> FillEvent:
    return FillEvent(
        fill_time=FILL_TIME,
        order_id="local_order_1",
        ticker="AAPL",
        side=side,
        quantity=quantity,
        fill_price=fill_price,
        fill_id=fill_id,
        expected_price=100.0,
        slippage=fill_price - 100.0 if side is OrderSide.BUY else 100.0 - fill_price,
        slippage_bps=slippage_bps,
        broker_status="filled",
        broker_fill_id=broker_fill_id,
        model_signal_id=model_signal_id,
        risk_decision_id=risk_decision_id,
        trace_id="trace_1",
    )
