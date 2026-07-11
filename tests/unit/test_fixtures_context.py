from market_relay_engine.common.serialization import from_json_string, to_json_string
from market_relay_engine.contracts.context import (
    ContextAIEvent,
    ContextClassificationRequest,
    ContextClassificationResponse,
    ContextFlag,
    ContextIndicatorSnapshot,
    ContextRawInput,
    ContextSourceDocument,
    ContextValidationResult,
    ShadowContextPolicyEvaluation,
)
from tests.fixtures.context import (
    build_context_examples,
    make_context_ai_event,
    make_context_classification_request,
    make_context_classification_response,
    make_context_raw_input,
    make_context_source_document,
    make_context_validation_result,
    make_eia_window_indicator,
    make_shadow_context_policy_evaluation,
    make_social_context_flag,
)


def test_context_fixtures_include_indicator_event_and_flag_records() -> None:
    examples = build_context_examples()

    assert any(isinstance(example, ContextIndicatorSnapshot) for example in examples)
    assert any(isinstance(example, ContextAIEvent) for example in examples)
    assert any(isinstance(example, ContextFlag) for example in examples)
    assert any(isinstance(example, ContextRawInput) for example in examples)
    assert any(isinstance(example, ContextSourceDocument) for example in examples)
    assert any(isinstance(example, ContextClassificationRequest) for example in examples)
    assert any(isinstance(example, ContextClassificationResponse) for example in examples)
    assert any(isinstance(example, ContextValidationResult) for example in examples)
    assert any(isinstance(example, ShadowContextPolicyEvaluation) for example in examples)


def test_context_indicator_examples_are_source_aware_without_api_calls() -> None:
    indicator = make_eia_window_indicator()

    assert indicator.source == "fake_eia_calendar_fixture"
    assert indicator.indicator_name == "eia_window"
    assert indicator.freshness_seconds is not None


def test_context_event_and_flag_serialize_with_validity_windows() -> None:
    event = make_context_ai_event()
    flag = make_social_context_flag()

    parsed_event = from_json_string(to_json_string(event))
    parsed_flag = from_json_string(to_json_string(flag))

    assert parsed_event["context_event_id"] == "FIXTURE-CONTEXT-EVENT-0001"
    assert parsed_event["valid_until"].endswith("Z")
    assert parsed_flag["context_flag_id"] == "FIXTURE-CONTEXT-FLAG-0001"
    assert parsed_flag["valid_until"].endswith("Z")


def test_phase7_fixture_lineage_is_deterministic_and_consistent() -> None:
    raw_input = make_context_raw_input()
    document = make_context_source_document(raw_input=raw_input)
    request = make_context_classification_request(document=document)
    response = make_context_classification_response(request=request)
    validation = make_context_validation_result(response=response)
    shadow = make_shadow_context_policy_evaluation()

    assert document.raw_input_id == raw_input.raw_input_id
    assert request.source_document_id == document.source_document_id
    assert request.raw_input_hash == raw_input.raw_input_hash
    assert response.classification_request_id == request.classification_request_id
    assert validation.classification_attempt_id == response.classification_attempt_id
    assert len(raw_input.raw_input_hash) == 64
    assert len(document.document_hash) == 64
    assert len(shadow.shadow_context_fingerprint) == 64

