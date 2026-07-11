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
    ContextClassificationEventType,
    ContextClassificationRequest,
    ContextClassificationResponse,
    ContextClassificationStatus,
    ContextFlag,
    ContextIndicatorSnapshot,
    ContextRawInput,
    ContextRiskLevel,
    ContextSourceDocument,
    ContextStateSnapshot,
    ContextUrgency,
    ContextValidationResult,
    ShadowContextAction,
    ShadowContextPolicyEvaluation,
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
    """Return one representative instance of every current contract."""
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
    raw_input = ContextRawInput(
        source="manual_test_source",
        source_type="local_document",
        source_locator="inbox/example.json",
        source_uri="https://example.invalid/source/1",
        raw_input_hash="a" * 64,
        affected_tickers=["XOM"],
        source_published_at=EXAMPLE_TIME - timedelta(minutes=5),
        collected_at=EXAMPLE_TIME,
        trace_id=trace_id,
    )
    source_document = ContextSourceDocument(
        raw_input_id=raw_input.raw_input_id,
        source=raw_input.source,
        source_type=raw_input.source_type,
        source_locator=raw_input.source_locator,
        source_uri=raw_input.source_uri,
        raw_input_hash=raw_input.raw_input_hash,
        document_hash="b" * 64,
        affected_tickers=["XOM"],
        source_published_at=raw_input.source_published_at,
        collected_at=raw_input.collected_at,
        normalized_at=EXAMPLE_TIME + timedelta(seconds=1),
        trace_id=trace_id,
    )
    classification_request = ContextClassificationRequest(
        requested_at=EXAMPLE_TIME + timedelta(seconds=2),
        source=source_document.source,
        source_type=source_document.source_type,
        source_locator=source_document.source_locator,
        source_uri=source_document.source_uri,
        raw_input_id=source_document.raw_input_id,
        source_document_id=source_document.source_document_id,
        raw_input_hash=source_document.raw_input_hash,
        document_hash=source_document.document_hash,
        affected_tickers=["XOM"],
        input_text="Bounded example excerpt.",
        prompt_version="context_prompt_v1",
        source_published_at=source_document.source_published_at,
        collected_at=source_document.collected_at,
        normalized_at=source_document.normalized_at,
        trace_id=trace_id,
    )
    classification_response = ContextClassificationResponse(
        classification_request_id=classification_request.classification_request_id,
        classified_at=EXAMPLE_TIME + timedelta(seconds=3),
        provider="provider_placeholder",
        model_version="model_placeholder",
        prompt_version=classification_request.prompt_version,
        status=ContextClassificationStatus.VALID,
        provider_latency_ms=125.0,
        event_type=ContextClassificationEventType.SEC_8K_RESULTS,
        risk_level=ContextRiskLevel.MEDIUM,
        urgency=ContextUrgency.MEDIUM,
        confidence=0.7,
        summary="Example structured context event.",
        trace_id=trace_id,
    )
    validation_result = ContextValidationResult(
        classification_request_id=classification_request.classification_request_id,
        classification_attempt_id=classification_response.classification_attempt_id,
        validation_outcome=True,
        reason_codes=[],
        validator_version="context_validator_v1",
        validated_at=EXAMPLE_TIME + timedelta(seconds=4),
        trace_id=trace_id,
    )
    shadow_evaluation = ShadowContextPolicyEvaluation(
        model_signal_id=model_signal.signal_id,
        risk_decision_id=risk_decision.risk_decision_id,
        decision_evaluation_time=EXAMPLE_TIME + timedelta(seconds=5),
        matched_context_event_ids=["context_event_example"],
        matched_context_flag_ids=["context_flag_example"],
        shadow_context_fingerprint="c" * 64,
        policy_version="shadow_policy_v1",
        policy_config_hash="d" * 64,
        hypothetical_action=ShadowContextAction.WARN_ONLY,
        reason_codes=["EXAMPLE_ONLY"],
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
        raw_input,
        source_document,
        classification_request,
        classification_response,
        validation_result,
        ContextAIEvent(
            event_time=EXAMPLE_TIME,
            source="ai_context_filter",
            source_id="example_article_1",
            affected_tickers=["XOM"],
            affected_sector="oil",
            event_type=classification_response.event_type,
            sentiment="neutral",
            urgency=classification_response.urgency,
            risk_level=classification_response.risk_level,
            confidence=0.7,
            valid_from=EXAMPLE_TIME,
            valid_until=EXAMPLE_TIME + timedelta(minutes=30),
            summary="Example structured context event.",
            prompt_version="context_filter_v1",
            model_version="model_placeholder",
            raw_input_hash=classification_request.raw_input_hash,
            raw_input_id=classification_request.raw_input_id,
            source_document_id=classification_request.source_document_id,
            classification_request_id=classification_request.classification_request_id,
            classification_attempt_id=classification_response.classification_attempt_id,
            validation_result_id=validation_result.validation_result_id,
            document_hash=classification_request.document_hash,
            classified_at=classification_response.classified_at,
            validated_at=validation_result.validated_at,
            available_at=raw_input.source_published_at,
            provider=classification_response.provider,
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
        shadow_evaluation,
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
