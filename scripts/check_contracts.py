"""Validate that PR 3 contracts instantiate and serialize locally."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.common.ids import new_trace_id  # noqa: E402
from market_relay_engine.common.serialization import (  # noqa: E402
    from_json_string,
    to_json_dict,
    to_json_string,
)
from market_relay_engine.contracts.context import (  # noqa: E402
    ContextAIEvent,
    ContextFlag,
    ContextIndicatorSnapshot,
    ContextStateSnapshot,
)
from market_relay_engine.contracts.execution import (  # noqa: E402
    FillEvent,
    OrderEvent,
    OrderSide,
    OrderStatus,
    OrderType,
)
from market_relay_engine.contracts.features import FeatureSnapshot  # noqa: E402
from market_relay_engine.contracts.ledger import LatencyMetric, TradeOutcome  # noqa: E402
from market_relay_engine.contracts.market import MarketRecord  # noqa: E402
from market_relay_engine.contracts.model import ModelSignal, SignalSide  # noqa: E402
from market_relay_engine.contracts.risk import RiskDecision, RiskDecisionType  # noqa: E402
from market_relay_engine.contracts.system import SystemHealthEvent  # noqa: E402


EXAMPLE_TIME = datetime(2026, 5, 18, 14, 30, 0, tzinfo=UTC)


def build_contract_examples() -> list[Any]:
    """Return one representative instance of every PR 3 contract."""
    trace_id = new_trace_id()
    feature_snapshot = FeatureSnapshot(
        snapshot_time=EXAMPLE_TIME,
        ticker="XOM",
        feature_version="feature_v0_placeholder",
        features={"midprice": 100.25, "spread": 0.02, "is_open": True},
        source_record_count=3,
        lookback_window_seconds=60,
        trace_id=trace_id,
    )
    model_signal = ModelSignal(
        signal_time=EXAMPLE_TIME,
        ticker="XOM",
        signal=SignalSide.BUY,
        confidence=0.62,
        raw_score=0.24,
        model_version="model_v0_placeholder",
        calibration_version="calibration_v0_placeholder",
        feature_version=feature_snapshot.feature_version,
        feature_snapshot_id=feature_snapshot.feature_snapshot_id,
        trace_id=trace_id,
    )
    context_state = ContextStateSnapshot(
        snapshot_time=EXAMPLE_TIME,
        ticker="XOM",
        sector="oil",
        active_indicator_ids=["context_indicator_example"],
        active_context_event_ids=["context_event_example"],
        active_context_flag_ids=["context_flag_example"],
        context_summary={"summary": "example_only"},
        highest_severity="normal",
        risk_level="normal",
        valid_until=EXAMPLE_TIME + timedelta(minutes=30),
        trace_id=trace_id,
    )
    risk_decision = RiskDecision(
        decision_time=EXAMPLE_TIME,
        ticker="XOM",
        model_signal_id=model_signal.signal_id,
        decision=RiskDecisionType.BLOCK,
        approved=False,
        reduce_size_factor=None,
        reasons=["example_only"],
        thresholds_used={"max_spread_bps": 10},
        cost_estimate_id="cost_estimate_example",
        context_snapshot_id=context_state.context_snapshot_id,
        risk_version="risk_v0_placeholder",
        trace_id=trace_id,
    )
    order = OrderEvent(
        order_time=EXAMPLE_TIME,
        ticker="XOM",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=1,
        expected_price=100.25,
        submitted_price=100.24,
        status=OrderStatus.SUBMITTED,
        broker="alpaca",
        paper_trading=True,
        trace_id=trace_id,
    )
    fill = FillEvent(
        fill_time=EXAMPLE_TIME + timedelta(seconds=1),
        order_id=order.order_id,
        ticker=order.ticker,
        side=order.side,
        quantity=1,
        fill_price=100.26,
        expected_price=100.25,
        slippage=0.01,
        broker_status="filled",
        trace_id=trace_id,
    )

    return [
        MarketRecord(
            event_time=EXAMPLE_TIME,
            ticker="XOM",
            raw_symbol="XOM",
            source="databento_future_adapter",
            record_type="quote",
            bid_price=100.24,
            ask_price=100.26,
            bid_size=100,
            ask_size=100,
            spread=0.02,
            midprice=100.25,
            source_event_time=EXAMPLE_TIME,
            local_receive_time=EXAMPLE_TIME + timedelta(milliseconds=5),
            trace_id=trace_id,
        ),
        feature_snapshot,
        model_signal,
        context_state,
        risk_decision,
        ContextIndicatorSnapshot(
            snapshot_time=EXAMPLE_TIME,
            source="calendar_events",
            ticker_or_sector="oil",
            indicator_name="eia_window",
            value=False,
            window="intraday",
            units="boolean",
            freshness_seconds=30,
            source_event_time=EXAMPLE_TIME,
            trace_id=trace_id,
        ),
        ContextAIEvent(
            event_time=EXAMPLE_TIME,
            source="ai_context_filter",
            source_id="example_article_1",
            affected_tickers=["XOM"],
            affected_sector="oil",
            event_type="headline",
            sentiment="neutral",
            urgency="low",
            risk_level="normal",
            confidence=0.7,
            valid_from=EXAMPLE_TIME,
            valid_until=EXAMPLE_TIME + timedelta(minutes=30),
            summary="Example structured context event.",
            prompt_version="context_filter_v1",
            model_version="model_placeholder",
            raw_input_hash="abc123",
            trace_id=trace_id,
        ),
        ContextFlag(
            event_time=EXAMPLE_TIME,
            source="ai_context_filter",
            ticker="XOM",
            sector="oil",
            flag_type="context_risk",
            severity="normal",
            confidence=0.7,
            valid_until=EXAMPLE_TIME + timedelta(minutes=30),
            trace_id=trace_id,
        ),
        order,
        fill,
        TradeOutcome(
            signal_id=model_signal.signal_id,
            order_id=order.order_id,
            ticker="XOM",
            entry_time=EXAMPLE_TIME,
            exit_time=EXAMPLE_TIME + timedelta(minutes=5),
            realized_pnl=1.25,
            return_1m=0.001,
            return_5m=0.002,
            return_15m=None,
            max_favorable_excursion=0.003,
            max_adverse_excursion=-0.001,
            result="example_closed",
            trace_id=trace_id,
        ),
        LatencyMetric(
            measured_time=EXAMPLE_TIME,
            component="feature_builder",
            latency_ms=12.5,
            source="local_timer",
            trace_id=trace_id,
        ),
        SystemHealthEvent(
            event_time=EXAMPLE_TIME,
            component="local_validation",
            status="ok",
            message="Example health record.",
            cpu_percent=None,
            memory_percent=None,
            clock_offset_ms=0.0,
            feed_delay_ms=None,
            reconnect_count=0,
            trace_id=trace_id,
        ),
    ]


def _record(results: list[tuple[bool, str]], ok: bool, message: str) -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {message}")
    results.append((ok, message))


def main() -> int:
    results: list[tuple[bool, str]] = []

    for example in build_contract_examples():
        name = type(example).__name__
        try:
            json_dict = to_json_dict(example)
            json_string = to_json_string(example)
            parsed = from_json_string(json_string)
            ok = isinstance(json_dict, dict) and isinstance(parsed, dict)
            _record(results, ok, f"{name} serializes to dict/string and parses to dict")
        except Exception as exc:  # noqa: BLE001 - check script should report all failures.
            _record(results, False, f"{name} serialization failed: {exc}")

    failures = [message for ok, message in results if not ok]
    print()
    if failures:
        print(f"Contract validation FAILED with {len(failures)} failure(s).")
        return 1

    print("Contract validation PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
