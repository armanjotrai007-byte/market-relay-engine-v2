"""Validate fill reconciliation without broker or QuestDB I/O."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.execution.execution_metrics import (  # noqa: E402
    OrderSubmissionResult,
)
from market_relay_engine.execution.fill_reconciliation import (  # noqa: E402
    BrokerPositionSnapshot,
    FillReconciliationError,
    apply_fill_and_reconcile,
    build_position_reconciliation_health_event,
    fill_event_from_alpaca_fill_payload,
    reconcile_position,
)
from market_relay_engine.execution.position_state import PortfolioState  # noqa: E402


def main() -> int:
    started_at = datetime(2026, 1, 2, 14, 30, 0, tzinfo=UTC)
    completed_at = started_at + timedelta(milliseconds=100)
    order_result = OrderSubmissionResult(
        local_order_id="local_order_check_fill_reconciliation",
        client_order_id="client_order_check_fill_reconciliation",
        broker_order_id="broker_order_check_fill_reconciliation",
        ticker="AAPL",
        side="BUY",
        quantity=2.0,
        order_type="MARKET",
        time_in_force="day",
        submit_started_at=started_at,
        submit_completed_at=completed_at,
        latency_ms=100.0,
        success=True,
        source_signal_id="signal_check_fill_reconciliation",
        risk_decision_id="risk_check_fill_reconciliation",
        trace_id="trace_check_fill_reconciliation",
        arrival_midprice=100.0,
    )
    fill_event = fill_event_from_alpaca_fill_payload(
        payload={
            "execution_id": "execution_check_fill_reconciliation_1",
            "order_id": "broker_order_check_fill_reconciliation",
            "symbol": "AAPL",
            "side": "buy",
            "qty": "2",
            "price": "100.05",
            "transaction_time": completed_at.isoformat().replace("+00:00", "Z"),
            "status": "filled",
        },
        order_result=order_result,
    )
    assert fill_event.fill_id == "execution_check_fill_reconciliation_1"
    assert fill_event.broker_fill_id == "execution_check_fill_reconciliation_1"
    assert fill_event.order_id == "local_order_check_fill_reconciliation"
    assert fill_event.model_signal_id == "signal_check_fill_reconciliation"
    assert fill_event.risk_decision_id == "risk_check_fill_reconciliation"
    assert fill_event.slippage is not None
    assert abs(fill_event.slippage - 0.05) < 1e-12

    portfolio = PortfolioState()
    result = apply_fill_and_reconcile(
        portfolio=portfolio,
        fill_event=fill_event,
        broker_position=BrokerPositionSnapshot(ticker="AAPL", quantity=2.0),
    )
    assert result.position_update.new_quantity == 2.0
    assert result.reconciliation is not None
    assert result.reconciliation.matched is True

    mismatch = reconcile_position(
        portfolio=portfolio,
        broker_position=BrokerPositionSnapshot(ticker="AAPL", quantity=1.0),
    )
    assert mismatch.matched is False
    health = build_position_reconciliation_health_event(mismatch)
    assert health.component == "position_reconciliation"
    assert health.status == "WARNING"

    try:
        fill_event_from_alpaca_fill_payload(
            payload={
                "order_id": "broker_order_aggregate",
                "symbol": "AAPL",
                "side": "buy",
                "filled_qty": "2",
                "avg_price": "100.05",
                "filled_at": completed_at,
            },
            order_result=order_result,
        )
    except FillReconciliationError:
        pass
    else:
        raise AssertionError("aggregate order payload without unique fill id should reject")

    print("Fill reconciliation check PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
