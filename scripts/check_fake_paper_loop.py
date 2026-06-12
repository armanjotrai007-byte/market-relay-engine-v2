"""Validate the deterministic fake paper loop without broker or QuestDB I/O."""

from __future__ import annotations

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.execution.fake_paper_loop import (  # noqa: E402
    FakePaperLoopConfig,
    run_fake_paper_cycle,
)
from market_relay_engine.execution.fill_reconciliation import (  # noqa: E402
    BrokerPositionSnapshot,
    apply_fill_and_reconcile,
    reconcile_position,
)


def main() -> int:
    result = run_fake_paper_cycle()
    position = result.final_portfolio.get_position("AAPL")
    assert position is not None
    assert position.quantity == 1.0
    assert result.fill_event.slippage is not None
    assert result.fill_event.slippage > 0
    assert result.reconciliation is not None
    assert result.reconciliation.matched is True
    assert result.order_state.open_orders == []
    assert "signal_fake_pr23" in result.order_state.used_signal_ids

    duplicate = apply_fill_and_reconcile(
        portfolio=result.final_portfolio,
        fill_event=result.fill_event,
        broker_position=BrokerPositionSnapshot(ticker="AAPL", quantity=1.0),
    )
    assert duplicate.position_update.duplicate_fill is True
    assert duplicate.position_update.new_quantity == 1.0
    assert result.final_portfolio.get_position("AAPL") is not None
    assert result.final_portfolio.get_position("AAPL").quantity == 1.0  # type: ignore[union-attr]

    mismatch = reconcile_position(
        portfolio=result.final_portfolio,
        broker_position=BrokerPositionSnapshot(ticker="AAPL", quantity=2.0),
    )
    assert mismatch.matched is False

    sell_result = run_fake_paper_cycle(FakePaperLoopConfig(side="SELL"))
    sell_position = sell_result.final_portfolio.get_position("AAPL")
    assert sell_position is not None
    assert sell_position.quantity == -1.0

    print("Fake paper loop check PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
