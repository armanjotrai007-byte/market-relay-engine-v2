from dataclasses import replace
from datetime import UTC, datetime

import pytest

from market_relay_engine.common.serialization import to_json_dict
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
    DeterministicContextEventType,
    ShadowContextAction,
    ShadowContextPolicyEvaluation,
)
from tests.fixtures.context import (
    make_context_ai_event,
    make_context_classification_request,
    make_context_classification_response,
    make_context_raw_input,
    make_context_source_document,
    make_context_validation_result,
    make_shadow_context_policy_evaluation,
)
from tests.unit.contract_test_utils import assert_contract_serializes, example_for


def test_context_indicator_snapshot_serializes() -> None:
    parsed = assert_contract_serializes(example_for(ContextIndicatorSnapshot))

    assert parsed["indicator_name"] == "eia_window"
    assert parsed["snapshot_time"].endswith("Z")
    assert parsed["source_event_time"].endswith("Z")


def test_context_ai_event_serializes_with_id_and_validity_window() -> None:
    parsed = assert_contract_serializes(example_for(ContextAIEvent))

    assert parsed["context_event_id"]
    assert parsed["affected_tickers"] == ["XOM"]
    assert parsed["valid_from"].endswith("Z")
    assert parsed["valid_until"].endswith("Z")


def test_context_flag_serializes_with_id() -> None:
    parsed = assert_contract_serializes(example_for(ContextFlag))

    assert parsed["context_flag_id"]
    assert parsed["flag_type"] == "context_risk"
    assert parsed["valid_until"].endswith("Z")


def test_context_state_snapshot_serializes_with_id_lists_and_validity() -> None:
    parsed = assert_contract_serializes(example_for(ContextStateSnapshot))

    assert parsed["context_snapshot_id"]
    assert parsed["ticker"] == "XOM"
    assert parsed["active_indicator_ids"] == ["context_indicator_example"]
    assert parsed["context_summary"] == {"summary": "example_only"}
    assert parsed["valid_until"].endswith("Z")


def test_phase7_enum_value_sets_are_exact() -> None:
    assert [item.value for item in ContextClassificationEventType] == [
        "UNKNOWN",
        "OTHER",
        "GOVERNMENT_CONTRACT",
        "REGULATORY_POLICY",
        "GEOPOLITICAL",
        "SUPPLY_DISRUPTION",
        "EARNINGS_GUIDANCE",
        "LEGAL",
        "CYBERSECURITY",
        "MANAGEMENT_CHANGE",
        "SOCIAL_POLITICAL_STATEMENT",
        "SEC_8K_MATERIAL_AGREEMENT",
        "SEC_8K_TERMINATION_OF_MATERIAL_AGREEMENT",
        "SEC_8K_BANKRUPTCY",
        "SEC_8K_CYBERSECURITY_INCIDENT",
        "SEC_8K_ACQUISITION",
        "SEC_8K_RESULTS",
        "SEC_8K_DIRECT_FINANCIAL_OBLIGATION",
        "SEC_8K_DEBT_DEFAULT",
        "SEC_8K_EXIT_OR_DISPOSAL_COSTS",
        "SEC_8K_MATERIAL_IMPAIRMENT",
        "SEC_8K_DELISTING",
        "SEC_8K_AUDITOR_CHANGE",
        "SEC_8K_NON_RELIANCE",
        "SEC_8K_CHANGE_IN_CONTROL",
        "SEC_8K_EXECUTIVE_OR_DIRECTOR_CHANGE",
        "SEC_8K_REGULATION_FD",
        "SEC_8K_OTHER_EVENT",
    ]
    assert [item.value for item in DeterministicContextEventType] == [
        "SEC_FORM4_PURCHASE",
        "SEC_FORM4_SALE",
    ]
    assert [item.value for item in ContextRiskLevel] == [
        "UNKNOWN",
        "LOW",
        "MEDIUM",
        "HIGH",
        "CRITICAL",
    ]
    assert [item.value for item in ContextUrgency] == [
        "UNKNOWN",
        "LOW",
        "MEDIUM",
        "HIGH",
    ]
    assert [item.value for item in ContextClassificationStatus] == [
        "VALID",
        "ABSTAINED",
        "VALIDATION_REJECTED",
        "PROVIDER_FAILED",
    ]
    assert [item.value for item in ShadowContextAction] == [
        "NO_CHANGE",
        "BLOCK",
        "REDUCE_SIZE",
        "DELAY",
        "WARN_ONLY",
    ]


def test_phase7_enums_serialize_to_stable_string_values() -> None:
    response = make_context_classification_response()
    shadow = make_shadow_context_policy_evaluation()

    response_json = to_json_dict(response)
    shadow_json = to_json_dict(shadow)

    assert response_json["status"] == "VALID"
    assert response_json["event_type"] == "SEC_8K_RESULTS"
    assert response_json["risk_level"] == "MEDIUM"
    assert response_json["urgency"] == "MEDIUM"
    assert shadow_json["hypothetical_action"] == "WARN_ONLY"


@pytest.mark.parametrize(
    "contract_type",
    [
        ContextRawInput,
        ContextSourceDocument,
        ContextClassificationRequest,
        ContextClassificationResponse,
        ContextValidationResult,
        ShadowContextPolicyEvaluation,
    ],
)
def test_new_contracts_serialize_with_generated_ids(contract_type: type[object]) -> None:
    parsed = assert_contract_serializes(example_for(contract_type))
    id_field = {
        ContextRawInput: "raw_input_id",
        ContextSourceDocument: "source_document_id",
        ContextClassificationRequest: "classification_request_id",
        ContextClassificationResponse: "classification_attempt_id",
        ContextValidationResult: "validation_result_id",
        ShadowContextPolicyEvaluation: "shadow_evaluation_id",
    }[contract_type]

    assert parsed[id_field]


def test_raw_input_defensively_copies_tickers_and_normalizes_utc() -> None:
    tickers = ["XOM"]
    raw_input = ContextRawInput(
        source="test",
        source_type="document",
        source_locator="local/item",
        raw_input_hash="a" * 64,
        affected_tickers=tickers,
        collected_at=datetime.fromisoformat("2026-07-01T10:00:00-04:00"),
    )
    tickers.append("CVX")

    assert raw_input.affected_tickers == ["XOM"]
    assert raw_input.collected_at == datetime(2026, 7, 1, 14, 0, tzinfo=UTC)
    assert raw_input.raw_input_id.startswith("raw_input_")


def test_evolved_context_contracts_defensively_copy_every_mutable_collection() -> None:
    document_tickers = ["XOM"]
    request_tickers = ["XOM"]
    event_tickers = ["XOM"]
    flag_reasons = ["RESEARCH_ONLY"]

    document = replace(
        make_context_source_document(),
        affected_tickers=document_tickers,
    )
    request = replace(
        make_context_classification_request(),
        affected_tickers=request_tickers,
    )
    event = replace(make_context_ai_event(), affected_tickers=event_tickers)
    flag = ContextFlag(
        event_time=datetime(2026, 7, 1, 14, 0, tzinfo=UTC),
        source="test",
        flag_type="test",
        severity="NORMAL",
        reason_codes=flag_reasons,
    )
    document_tickers.append("CVX")
    request_tickers.append("CVX")
    event_tickers.append("CVX")
    flag_reasons.append("MUTATED")

    assert document.affected_tickers == ["XOM"]
    assert request.affected_tickers == ["XOM"]
    assert event.affected_tickers == ["XOM"]
    assert flag.reason_codes == ["RESEARCH_ONLY"]


@pytest.mark.parametrize("bad_hash", ["abc", "A" * 64, "g" * 64, "a" * 63])
def test_phase7_hashes_require_canonical_lowercase_sha256(bad_hash: str) -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        replace(make_context_raw_input(), raw_input_hash=bad_hash)


@pytest.mark.parametrize(
    "record",
    [
        make_context_raw_input(),
        make_context_source_document(),
        make_context_classification_request(),
        make_context_classification_response(),
        make_context_validation_result(),
        make_shadow_context_policy_evaluation(),
    ],
)
def test_phase7_contracts_reject_naive_required_timestamps(record: object) -> None:
    datetime_field = {
        ContextRawInput: "collected_at",
        ContextSourceDocument: "normalized_at",
        ContextClassificationRequest: "requested_at",
        ContextClassificationResponse: "classified_at",
        ContextValidationResult: "validated_at",
        ShadowContextPolicyEvaluation: "decision_evaluation_time",
    }[type(record)]

    with pytest.raises(ValueError, match="timezone-aware"):
        replace(record, **{datetime_field: datetime(2026, 7, 1, 12, 0)})


@pytest.mark.parametrize(
    ("record", "field_name"),
    [
        (make_context_raw_input(), "raw_input_id"),
        (make_context_raw_input(), "source"),
        (make_context_source_document(), "source_document_id"),
        (make_context_source_document(), "raw_input_id"),
        (make_context_classification_request(), "classification_request_id"),
        (make_context_classification_request(), "source_document_id"),
        (make_context_classification_response(), "classification_attempt_id"),
        (make_context_classification_response(), "classification_request_id"),
        (make_context_validation_result(), "validation_result_id"),
        (make_context_validation_result(), "classification_attempt_id"),
        (make_shadow_context_policy_evaluation(), "shadow_evaluation_id"),
        (make_shadow_context_policy_evaluation(), "model_signal_id"),
    ],
)
def test_new_contracts_reject_empty_required_identities(
    record: object,
    field_name: str,
) -> None:
    with pytest.raises(ValueError, match=field_name):
        replace(record, **{field_name: "  "})


@pytest.mark.parametrize("confidence", [-0.01, 1.01, float("nan"), float("inf"), True])
def test_classification_response_rejects_invalid_confidence(confidence: object) -> None:
    with pytest.raises((TypeError, ValueError), match="confidence"):
        replace(make_context_classification_response(), confidence=confidence)


@pytest.mark.parametrize("latency", [-0.01, float("nan"), float("inf"), True])
def test_classification_response_rejects_invalid_provider_latency(
    latency: object,
) -> None:
    with pytest.raises((TypeError, ValueError), match="provider_latency_ms"):
        replace(make_context_classification_response(), provider_latency_ms=latency)


def test_classification_response_defaults_preserve_attempt_accounting_compatibility() -> None:
    response = ContextClassificationResponse(
        classification_request_id="classification_request_legacy",
        classified_at=datetime(2026, 7, 1, 14, 0, tzinfo=UTC),
        provider="legacy_provider",
        model_version="legacy_model_v1",
        prompt_version="legacy_prompt_v1",
        status=ContextClassificationStatus.PROVIDER_FAILED,
        provider_latency_ms=0.0,
        safe_failure_category="LOCAL_FAILURE",
    )

    assert response.provider_request_count == 0
    assert response.retry_count == 0
    assert response.deduplicated is False
    assert response.reused_classification_attempt_id is None


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"provider_request_count": -1}, "provider_request_count"),
        ({"provider_request_count": True}, "provider_request_count"),
        ({"provider_request_count": 1.0}, "provider_request_count"),
        ({"retry_count": -1}, "retry_count"),
        ({"retry_count": True}, "retry_count"),
        ({"provider_request_count": 1, "retry_count": 1}, "retry_count"),
        ({"provider_request_count": 2, "retry_count": 0}, "retry_count"),
        ({"provider_request_count": 2, "retry_count": 2}, "retry_count"),
        ({"provider_request_count": 3, "retry_count": 1}, "retry_count"),
        ({"deduplicated": "false"}, "deduplicated"),
        (
            {
                "provider_request_count": 0,
                "retry_count": 0,
                "deduplicated": True,
            },
            "reused_classification_attempt_id",
        ),
        (
            {
                "deduplicated": True,
                "reused_classification_attempt_id": "classification_attempt_original",
                "provider_request_count": 1,
            },
            "deduplicated responses",
        ),
        (
            {"reused_classification_attempt_id": "classification_attempt_original"},
            "only valid for deduplicated",
        ),
    ],
)
def test_classification_response_rejects_invalid_attempt_accounting(
    changes: dict[str, object],
    match: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=match):
        replace(make_context_classification_response(), **changes)


def test_classification_response_accepts_retry_and_deduplication_accounting() -> None:
    retried = replace(
        make_context_classification_response(),
        provider_request_count=3,
        retry_count=2,
    )
    deduplicated = replace(
        make_context_classification_response(),
        provider_request_count=0,
        retry_count=0,
        deduplicated=True,
        reused_classification_attempt_id="classification_attempt_original",
    )

    assert retried.provider_request_count == 3
    assert retried.retry_count == 2
    assert deduplicated.deduplicated is True
    assert (
        deduplicated.reused_classification_attempt_id
        == "classification_attempt_original"
    )


@pytest.mark.parametrize("confidence", [-0.01, 1.01, float("nan"), float("inf"), True])
def test_existing_context_records_reject_invalid_confidence(confidence: object) -> None:
    with pytest.raises((TypeError, ValueError), match="confidence"):
        replace(make_context_ai_event(), confidence=confidence)
    with pytest.raises((TypeError, ValueError), match="confidence"):
        ContextFlag(
            event_time=datetime(2026, 7, 1, 14, 0, tzinfo=UTC),
            source="test",
            flag_type="test",
            severity="NORMAL",
            confidence=confidence,
        )


def test_classification_response_accepts_valid_abstained_shape() -> None:
    response = replace(
        make_context_classification_response(),
        status=ContextClassificationStatus.ABSTAINED,
        event_type=ContextClassificationEventType.UNKNOWN,
        risk_level=ContextRiskLevel.UNKNOWN,
        urgency=ContextUrgency.UNKNOWN,
        confidence=None,
        summary="Provider abstained due to insufficient context.",
    )

    assert response.status is ContextClassificationStatus.ABSTAINED


def test_classification_response_accepts_validation_rejected_shape() -> None:
    response = replace(
        make_context_classification_response(),
        status=ContextClassificationStatus.VALIDATION_REJECTED,
        event_type=ContextClassificationEventType.UNKNOWN,
        risk_level=ContextRiskLevel.UNKNOWN,
        urgency=ContextUrgency.UNKNOWN,
        confidence=None,
        summary=None,
    )

    assert response.status is ContextClassificationStatus.VALIDATION_REJECTED


def test_classification_response_accepts_safe_provider_failure_shape() -> None:
    response = replace(
        make_context_classification_response(),
        status=ContextClassificationStatus.PROVIDER_FAILED,
        event_type=ContextClassificationEventType.UNKNOWN,
        risk_level=ContextRiskLevel.UNKNOWN,
        urgency=ContextUrgency.UNKNOWN,
        confidence=None,
        summary=None,
        safe_failure_category="TIMEOUT",
        safe_failure_summary="Provider call exceeded the configured timeout.",
    )

    assert response.safe_failure_category == "TIMEOUT"
    assert not hasattr(response, "exception")
    assert not hasattr(response, "traceback")


@pytest.mark.parametrize(
    "changes",
    [
        {"event_type": ContextClassificationEventType.UNKNOWN},
        {
            "status": ContextClassificationStatus.ABSTAINED,
            "event_type": ContextClassificationEventType.UNKNOWN,
            "risk_level": ContextRiskLevel.UNKNOWN,
            "urgency": ContextUrgency.UNKNOWN,
            "confidence": 0.2,
        },
        {
            "status": ContextClassificationStatus.VALIDATION_REJECTED,
            "event_type": ContextClassificationEventType.UNKNOWN,
            "risk_level": ContextRiskLevel.UNKNOWN,
            "urgency": ContextUrgency.UNKNOWN,
            "confidence": None,
            "summary": "not allowed",
        },
        {
            "status": ContextClassificationStatus.PROVIDER_FAILED,
            "event_type": ContextClassificationEventType.UNKNOWN,
            "risk_level": ContextRiskLevel.UNKNOWN,
            "urgency": ContextUrgency.UNKNOWN,
            "confidence": None,
            "summary": None,
            "safe_failure_category": None,
        },
    ],
)
def test_classification_response_rejects_incoherent_status_shapes(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        replace(make_context_classification_response(), **changes)


def test_classification_contracts_reject_raw_strings_and_form4_event_types() -> None:
    with pytest.raises(TypeError, match="status"):
        replace(make_context_classification_response(), status="VALID")
    with pytest.raises(TypeError, match="event_type"):
        replace(make_context_classification_response(), event_type="SEC_8K_RESULTS")
    with pytest.raises(TypeError, match="risk_level"):
        replace(make_context_classification_response(), risk_level="MEDIUM")
    with pytest.raises(TypeError, match="urgency"):
        replace(make_context_classification_response(), urgency="MEDIUM")
    with pytest.raises(TypeError, match="event_type"):
        replace(
            make_context_classification_response(),
            event_type=DeterministicContextEventType.SEC_FORM4_PURCHASE,
        )
    with pytest.raises(TypeError, match="event_type"):
        replace(
            make_context_ai_event(),
            event_type=DeterministicContextEventType.SEC_FORM4_SALE,
        )
    with pytest.raises(TypeError, match="risk_level"):
        replace(make_context_ai_event(), risk_level="MEDIUM")
    with pytest.raises(TypeError, match="urgency"):
        replace(make_context_ai_event(), urgency="MEDIUM")
    with pytest.raises(TypeError, match="hypothetical_action"):
        replace(
            make_shadow_context_policy_evaluation(),
            hypothetical_action="WARN_ONLY",
        )


def test_validation_result_enforces_reason_code_coherence_and_copying() -> None:
    reasons = ["BAD_SCHEMA"]
    failed = replace(
        make_context_validation_result(),
        validation_outcome=False,
        reason_codes=reasons,
        safe_detail="Classification payload did not match the contract.",
    )
    reasons.append("MUTATED")

    assert failed.reason_codes == ["BAD_SCHEMA"]
    with pytest.raises(ValueError, match="cannot include reason"):
        replace(make_context_validation_result(), reason_codes=["UNEXPECTED"])
    with pytest.raises(ValueError, match="requires at least one"):
        replace(
            make_context_validation_result(),
            validation_outcome=False,
            reason_codes=[],
        )


@pytest.mark.parametrize(
    "action",
    [
        ShadowContextAction.NO_CHANGE,
        ShadowContextAction.BLOCK,
        ShadowContextAction.DELAY,
        ShadowContextAction.WARN_ONLY,
    ],
)
def test_shadow_non_size_actions_reject_size_factor(action: ShadowContextAction) -> None:
    valid = replace(
        make_shadow_context_policy_evaluation(),
        hypothetical_action=action,
        proposed_size_factor=None,
    )
    assert valid.proposed_size_factor is None
    with pytest.raises(ValueError, match="only valid"):
        replace(valid, proposed_size_factor=0.5)


def test_shadow_reduce_size_requires_valid_factor_and_copies_ids() -> None:
    event_ids = ["event-1"]
    flag_ids = ["flag-1"]
    evaluation = replace(
        make_shadow_context_policy_evaluation(),
        hypothetical_action=ShadowContextAction.REDUCE_SIZE,
        proposed_size_factor=0.5,
        matched_context_event_ids=event_ids,
        matched_context_flag_ids=flag_ids,
    )
    event_ids.append("mutated")
    flag_ids.append("mutated")

    assert evaluation.proposed_size_factor == 0.5
    assert evaluation.matched_context_event_ids == ["event-1"]
    assert evaluation.matched_context_flag_ids == ["flag-1"]
    for bad_factor in (None, 0.0, -0.1, 1.01, float("nan"), True):
        with pytest.raises((TypeError, ValueError), match="proposed_size_factor|REDUCE_SIZE"):
            replace(evaluation, proposed_size_factor=bad_factor)


def test_legacy_context_flag_constructor_remains_compatible() -> None:
    flag = ContextFlag(
        event_time=datetime(2026, 7, 1, 14, 0, tzinfo=UTC),
        source="eia_wpsr_v1",
        flag_type="eia_wpsr_event_window",
        severity="NORMAL",
        ticker="XOM",
    )

    assert flag.available_at is None
    assert flag.reason_codes == []


def test_context_flag_available_at_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ContextFlag(
            event_time=datetime(2026, 7, 1, 14, 0, tzinfo=UTC),
            source="test",
            flag_type="test",
            severity="NORMAL",
            available_at=datetime(2026, 7, 1, 14, 0),
        )


def test_context_ai_event_rejects_naive_optional_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(
            make_context_ai_event(),
            available_at=datetime(2026, 7, 1, 14, 0),
        )
