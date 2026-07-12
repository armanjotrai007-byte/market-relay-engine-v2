"""Fake context record fixtures for tests."""

from __future__ import annotations

from market_relay_engine.contracts.context import (
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
from tests.fixtures.ids import TRACE_ID_APPROVED_OIL, stable_record_id, stable_sha256
from tests.fixtures.times import minutes_after_market_open, seconds_after_market_open


def make_context_indicator(
    *,
    source: str,
    ticker_or_sector: str,
    indicator_name: str,
    value: object,
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
    window: str | None = "intraday",
    units: str | None = None,
    freshness_seconds: float | None = 60.0,
    stale: bool = False,
) -> ContextIndicatorSnapshot:
    """Return a fake structured context indicator."""
    snapshot_time = seconds_after_market_open(index + 6)
    source_event_time = minutes_after_market_open(-45) if stale else snapshot_time
    return ContextIndicatorSnapshot(
        snapshot_time=snapshot_time,
        source=source,
        ticker_or_sector=ticker_or_sector,
        indicator_name=indicator_name,
        value=value,
        window=window,
        units=units,
        freshness_seconds=7200.0 if stale else freshness_seconds,
        source_event_time=source_event_time,
        trace_id=trace_id,
    )


def make_eia_window_indicator(**overrides: object) -> ContextIndicatorSnapshot:
    """Return a fake EIA window indicator."""
    return make_context_indicator(
        source="fake_eia_calendar_fixture",
        ticker_or_sector="oil",
        indicator_name="eia_window",
        value=True,
        units="boolean",
        **overrides,
    )


def make_sector_proxy_move_indicator(**overrides: object) -> ContextIndicatorSnapshot:
    """Return a fake sector proxy move indicator."""
    return make_context_indicator(
        source="fake_sector_proxy_fixture",
        ticker_or_sector="XLE",
        indicator_name="sector_proxy_move",
        value={"proxy": "XLE", "return_5m": 0.012},
        units="return",
        **overrides,
    )


def make_fred_rate_context_indicator(**overrides: object) -> ContextIndicatorSnapshot:
    """Return a fake FRED/rate context indicator."""
    return make_context_indicator(
        source="fake_fred_fixture",
        ticker_or_sector="rates",
        indicator_name="rate_context",
        value={"ten_year_yield_change_bps": 4.2},
        units="basis_points",
        **overrides,
    )


def make_usaspending_defense_award_indicator(**overrides: object) -> ContextIndicatorSnapshot:
    """Return a fake USAspending defense award context indicator."""
    return make_context_indicator(
        source="fake_usaspending_fixture",
        ticker_or_sector="defense",
        indicator_name="defense_award_context",
        value={"award_ticker": "LMT", "award_size_usd": 125000000},
        units="usd",
        **overrides,
    )


def make_context_raw_input(
    *,
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
) -> ContextRawInput:
    """Return trusted source metadata without embedding source text."""
    return ContextRawInput(
        raw_input_id=stable_record_id("raw_input", index),
        source="fake_sec_fixture",
        source_type="sec_filing",
        source_platform="sec_edgar",
        source_uri=f"https://example.invalid/filing/{index}",
        source_locator=f"fixture/sec/{index}.txt",
        raw_input_hash=stable_sha256("raw_input", index),
        affected_tickers=["XOM"],
        source_published_at=minutes_after_market_open(-10),
        collected_at=seconds_after_market_open(index),
        trace_id=trace_id,
    )


def make_context_source_document(
    *,
    raw_input: ContextRawInput | None = None,
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
) -> ContextSourceDocument:
    """Return normalized document metadata without a document body."""
    raw_input = raw_input or make_context_raw_input(index=index, trace_id=trace_id)
    return ContextSourceDocument(
        source_document_id=stable_record_id("source_document", index),
        raw_input_id=raw_input.raw_input_id,
        source=raw_input.source,
        source_type=raw_input.source_type,
        source_platform=raw_input.source_platform,
        source_uri=raw_input.source_uri,
        source_locator=raw_input.source_locator,
        raw_input_hash=raw_input.raw_input_hash,
        document_hash=stable_sha256("source_document", index),
        affected_tickers=list(raw_input.affected_tickers),
        source_published_at=raw_input.source_published_at,
        source_updated_at=raw_input.source_updated_at,
        collected_at=raw_input.collected_at,
        normalized_at=seconds_after_market_open(index + 1),
        trace_id=trace_id,
    )


def make_context_classification_request(
    *,
    document: ContextSourceDocument | None = None,
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
) -> ContextClassificationRequest:
    """Return a bounded in-memory classification request fixture."""
    document = document or make_context_source_document(index=index, trace_id=trace_id)
    return ContextClassificationRequest(
        classification_request_id=stable_record_id("classification_request", index),
        requested_at=seconds_after_market_open(index + 2),
        source=document.source,
        source_type=document.source_type,
        source_platform=document.source_platform,
        source_uri=document.source_uri,
        source_locator=document.source_locator,
        raw_input_id=document.raw_input_id,
        source_document_id=document.source_document_id,
        raw_input_hash=document.raw_input_hash,
        document_hash=document.document_hash,
        affected_tickers=list(document.affected_tickers),
        input_text="Bounded fake 8-K excerpt for local contract tests only.",
        prompt_version="fixture_prompt_v1",
        source_published_at=document.source_published_at,
        source_updated_at=document.source_updated_at,
        collected_at=document.collected_at,
        normalized_at=document.normalized_at,
        trace_id=trace_id,
    )


def make_context_classification_response(
    *,
    request: ContextClassificationRequest | None = None,
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
) -> ContextClassificationResponse:
    """Return a valid research-only classification response fixture."""
    request = request or make_context_classification_request(
        index=index,
        trace_id=trace_id,
    )
    return ContextClassificationResponse(
        classification_attempt_id=stable_record_id("classification_attempt", index),
        classification_request_id=request.classification_request_id,
        classified_at=seconds_after_market_open(index + 3),
        provider="fake_provider",
        model_version="fixture_context_model_v1",
        prompt_version=request.prompt_version,
        status=ContextClassificationStatus.VALID,
        provider_latency_ms=12.5,
        provider_request_count=1,
        event_type=ContextClassificationEventType.SEC_8K_RESULTS,
        risk_level=ContextRiskLevel.MEDIUM,
        urgency=ContextUrgency.MEDIUM,
        confidence=0.68,
        summary="Fake context classification for fixture tests.",
        trace_id=trace_id,
    )


def make_context_validation_result(
    *,
    response: ContextClassificationResponse | None = None,
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
) -> ContextValidationResult:
    """Return a successful local validation-result fixture."""
    response = response or make_context_classification_response(
        index=index,
        trace_id=trace_id,
    )
    return ContextValidationResult(
        validation_result_id=stable_record_id("validation_result", index),
        classification_request_id=response.classification_request_id,
        classification_attempt_id=response.classification_attempt_id,
        validation_outcome=True,
        reason_codes=[],
        validator_version="fixture_validator_v1",
        validated_at=seconds_after_market_open(index + 4),
        trace_id=trace_id,
    )


def make_shadow_context_policy_evaluation(
    *,
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
) -> ShadowContextPolicyEvaluation:
    """Return a non-authoritative shadow-evaluation fixture."""
    return ShadowContextPolicyEvaluation(
        shadow_evaluation_id=stable_record_id("shadow_evaluation", index),
        model_signal_id=stable_record_id("signal", index),
        risk_decision_id=stable_record_id("risk_decision", index),
        decision_evaluation_time=seconds_after_market_open(index + 5),
        matched_context_event_ids=[stable_record_id("context_event", index)],
        matched_context_flag_ids=[stable_record_id("context_flag", index)],
        shadow_context_fingerprint=stable_sha256("shadow_context", index),
        policy_version="fixture_shadow_policy_v1",
        policy_config_hash=stable_sha256("shadow_policy_config", index),
        hypothetical_action=ShadowContextAction.WARN_ONLY,
        reason_codes=["FIXTURE_WARNING"],
        trace_id=trace_id,
    )


def make_context_ai_event(
    *,
    source: str = "fake_ai_context_fixture",
    source_id: str = "fixture-ai-news-0001",
    affected_tickers: list[str] | None = None,
    affected_sector: str | None = "oil",
    event_type: ContextClassificationEventType = ContextClassificationEventType.OTHER,
    risk_level: ContextRiskLevel | str = ContextRiskLevel.LOW,
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
    expired: bool = False,
) -> ContextAIEvent:
    """Return a fake AI context event without making AI calls."""
    legacy_risk_levels = {
        "normal": ContextRiskLevel.LOW,
        "elevated": ContextRiskLevel.MEDIUM,
        "high": ContextRiskLevel.HIGH,
        "unknown": ContextRiskLevel.UNKNOWN,
    }
    if isinstance(risk_level, str) and not isinstance(risk_level, ContextRiskLevel):
        try:
            risk_level = legacy_risk_levels[risk_level.strip().lower()]
        except KeyError as exc:
            raise ValueError("Unsupported fixture context risk level") from exc
    event_time = minutes_after_market_open(-30) if expired else seconds_after_market_open(index + 7)
    valid_from = event_time
    valid_until = minutes_after_market_open(-1) if expired else minutes_after_market_open(20)
    return ContextAIEvent(
        event_time=event_time,
        source=source,
        source_id=source_id,
        affected_tickers=affected_tickers or ["XOM"],
        event_type=event_type,
        context_event_id=stable_record_id("context_event", index),
        affected_sector=affected_sector,
        sentiment="neutral",
        urgency=ContextUrgency.MEDIUM,
        risk_level=risk_level,
        confidence=0.68,
        valid_from=valid_from,
        valid_until=valid_until,
        summary="Fake context event for fixture tests.",
        prompt_version="fixture_prompt_v1",
        model_version="fixture_context_model_v1",
        raw_input_hash=stable_sha256("raw_input", index),
        trace_id=trace_id,
    )


def make_context_flag(
    *,
    source: str = "fake_ai_context_fixture",
    flag_type: str = "ai_context_high_risk",
    severity: str = "warning",
    ticker: str | None = "XOM",
    sector: str | None = "oil",
    confidence: float | None = 0.72,
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
    expired: bool = False,
) -> ContextFlag:
    """Return a fake context flag for risk fixtures."""
    return ContextFlag(
        event_time=minutes_after_market_open(-20) if expired else seconds_after_market_open(index + 8),
        source=source,
        flag_type=flag_type,
        severity=severity,
        context_flag_id=stable_record_id("context_flag", index),
        ticker=ticker,
        sector=sector,
        confidence=confidence,
        valid_until=minutes_after_market_open(-1) if expired else minutes_after_market_open(15),
        trace_id=trace_id,
    )


def make_ai_news_context_flag(**overrides: object) -> ContextFlag:
    """Return a fake AI news context flag."""
    return make_context_flag(
        source="fake_ai_news_fixture",
        flag_type="ai_news_context",
        **overrides,
    )


def make_sec_context_flag(**overrides: object) -> ContextFlag:
    """Return a fake SEC context flag."""
    return make_context_flag(
        source="fake_sec_fixture",
        flag_type="sec_context",
        **overrides,
    )


def make_social_context_flag(**overrides: object) -> ContextFlag:
    """Return a fake social context flag."""
    return make_context_flag(
        source="fake_social_fixture",
        flag_type="social_context",
        **overrides,
    )


def make_context_state_snapshot(
    *,
    ticker: str = "XOM",
    sector: str | None = "oil",
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
) -> ContextStateSnapshot:
    """Return a fake context state snapshot for risk-decision ledger joins."""
    return ContextStateSnapshot(
        snapshot_time=seconds_after_market_open(index + 9),
        ticker=ticker,
        sector=sector,
        active_indicator_ids=[stable_record_id("context_indicator", index)],
        active_context_event_ids=[stable_record_id("context_event", index)],
        active_context_flag_ids=[stable_record_id("context_flag", index)],
        context_summary={"fixture": "context_state"},
        highest_severity="normal",
        risk_level="normal",
        valid_until=minutes_after_market_open(15),
        trace_id=trace_id,
    )


def build_context_examples() -> list[object]:
    """Return representative fake context records."""
    return [
        make_eia_window_indicator(),
        make_sector_proxy_move_indicator(index=2),
        make_fred_rate_context_indicator(index=3),
        make_usaspending_defense_award_indicator(index=4),
        make_context_raw_input(index=1),
        make_context_source_document(index=1),
        make_context_classification_request(index=1),
        make_context_classification_response(index=1),
        make_context_validation_result(index=1),
        make_context_ai_event(index=1),
        make_ai_news_context_flag(index=1),
        make_sec_context_flag(index=2),
        make_social_context_flag(index=3),
        make_context_state_snapshot(index=1),
        make_shadow_context_policy_evaluation(index=1),
    ]
