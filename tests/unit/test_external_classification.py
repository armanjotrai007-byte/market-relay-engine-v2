from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from market_relay_engine.ai_context.classifier import ContextClassificationAttemptResult
from market_relay_engine.context.external_classification import (
    ExternalClassificationPipeline,
)
from market_relay_engine.context.external_event_archive import (
    ConflictResolutionDecision,
    ExternalEventArchive,
    ExternalEventArchiveError,
    ExternalSourceRevision,
    LifecycleState,
    output_fingerprints,
    source_revision_id,
)
from market_relay_engine.context.external_normalization import (
    EXCERPT_VERSION,
    HTML_NORMALIZER_VERSION,
    SCOPE_RESOLVER_VERSION,
)
from market_relay_engine.context.research_projection import (
    ResearchSourceClassificationProfile,
)
from market_relay_engine.contracts.context import (
    ContextClassificationEventType,
    ContextClassificationRequest,
    ContextClassificationResponse,
    ContextClassificationStatus,
    ContextRiskLevel,
    ContextUrgency,
    ContextValidationResult,
)


T0 = datetime(2026, 7, 18, 16, 0, tzinfo=UTC)


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@dataclass(frozen=True)
class FakeResultSpec:
    status: ContextClassificationStatus
    event_type: ContextClassificationEventType = ContextClassificationEventType.OTHER
    risk_level: ContextRiskLevel = ContextRiskLevel.LOW
    urgency: ContextUrgency = ContextUrgency.LOW
    confidence: float = 0.75
    summary: str = "A bounded research-only classification."
    affected_tickers: tuple[str, ...] = ()
    affected_sectors: tuple[str, ...] = ()
    global_relevance: bool = False


class FakeClassifier:
    def __init__(
        self,
        *,
        clock: MutableClock,
        results: Iterable[FakeResultSpec],
    ) -> None:
        self._clock = clock
        self._results = list(results)
        self.calls: list[ContextClassificationRequest] = []

    def classify(
        self, request: ContextClassificationRequest
    ) -> ContextClassificationAttemptResult:
        self.calls.append(request)
        if not self._results:
            raise AssertionError("classifier was called after durable completion")
        spec = self._results.pop(0)
        attempt_id = f"fake-attempt-{len(self.calls)}"
        if spec.status is ContextClassificationStatus.VALID:
            response = ContextClassificationResponse(
                classification_request_id=request.classification_request_id,
                classification_attempt_id=attempt_id,
                classified_at=self._clock(),
                provider="gemini",
                model_version="gemini-test",
                prompt_version="context_filter_v2_scope",
                response_schema_version="context_classification_response_v2",
                status=spec.status,
                event_type=spec.event_type,
                risk_level=spec.risk_level,
                urgency=spec.urgency,
                confidence=spec.confidence,
                summary=spec.summary,
                affected_tickers=list(spec.affected_tickers),
                affected_sectors=list(spec.affected_sectors),
                global_relevance=spec.global_relevance,
                provider_latency_ms=5.0,
                provider_request_count=1,
                retry_count=0,
            )
            validation = ContextValidationResult(
                classification_request_id=request.classification_request_id,
                classification_attempt_id=attempt_id,
                validation_outcome=True,
                reason_codes=[],
                validator_version="context_filter_validator_v2_scope",
                validated_at=self._clock(),
            )
            return ContextClassificationAttemptResult(
                response=response,
                validation_result=validation,
            )
        if spec.status is ContextClassificationStatus.ABSTAINED:
            response = ContextClassificationResponse(
                classification_request_id=request.classification_request_id,
                classification_attempt_id=attempt_id,
                classified_at=self._clock(),
                provider="gemini",
                model_version="gemini-test",
                prompt_version="context_filter_v2_scope",
                response_schema_version="context_classification_response_v2",
                status=spec.status,
                global_relevance=False,
                provider_latency_ms=5.0,
                provider_request_count=1,
                retry_count=0,
            )
            validation = ContextValidationResult(
                classification_request_id=request.classification_request_id,
                classification_attempt_id=attempt_id,
                validation_outcome=True,
                reason_codes=[],
                validator_version="context_filter_validator_v2_scope",
                validated_at=self._clock(),
            )
            return ContextClassificationAttemptResult(
                response=response,
                validation_result=validation,
            )
        if spec.status is ContextClassificationStatus.PROVIDER_FAILED:
            return ContextClassificationAttemptResult(
                response=ContextClassificationResponse(
                    classification_request_id=request.classification_request_id,
                    classification_attempt_id=attempt_id,
                    classified_at=self._clock(),
                    provider="gemini",
                    model_version="gemini-test",
                    prompt_version="context_filter_v2_scope",
                    response_schema_version="context_classification_response_v2",
                    status=spec.status,
                    safe_failure_category="TRANSIENT_PROVIDER_FAILURE",
                    safe_failure_summary="retryable provider failure",
                    provider_latency_ms=5.0,
                    provider_request_count=1,
                    retry_count=0,
                )
            )
        response = ContextClassificationResponse(
            classification_request_id=request.classification_request_id,
            classification_attempt_id=attempt_id,
            classified_at=self._clock(),
            provider="gemini",
            model_version="gemini-test",
            prompt_version="context_filter_v2_scope",
            response_schema_version="context_classification_response_v2",
            status=ContextClassificationStatus.VALIDATION_REJECTED,
            provider_latency_ms=5.0,
            provider_request_count=1,
            retry_count=0,
        )
        validation = ContextValidationResult(
            classification_request_id=request.classification_request_id,
            classification_attempt_id=attempt_id,
            validation_outcome=False,
            reason_codes=["SCHEMA_REJECTED"],
            validator_version="context_filter_validator_v2_scope",
            validated_at=self._clock(),
        )
        return ContextClassificationAttemptResult(
            response=response,
            validation_result=validation,
        )


def _profile() -> ResearchSourceClassificationProfile:
    return ResearchSourceClassificationProfile(
        source="veritawire_truth_social",
        source_type="social_post",
        semantic_adapter_version="veritawire_adapter_v1",
        extraction_version="veritawire_html_v1",
        normalization_version=HTML_NORMALIZER_VERSION,
        excerpt_version=EXCERPT_VERSION,
        scope_version=SCOPE_RESOLVER_VERSION,
        prompt_version="context_filter_v2_scope",
        model_version="gemini-test",
        response_schema_version="context_classification_response_v2",
        validator_version="context_filter_validator_v2_scope",
        classification_config_hash=sha256(b"test-classifier-config").hexdigest(),
    )


def _archive_revision(
    archive: ExternalEventArchive,
    *,
    text: str | None,
    fact_id: str = "truth-123",
    observed_at: datetime = T0,
    fixed_tickers: tuple[str, ...] = (),
    collection_mode: str = "LIVE_SYSTEM",
    source_title: str | None = None,
) -> ExternalSourceRevision:
    raw = ("<p>" + (text or "") + "</p>").encode("utf-8")
    raw_hash = archive.archive_object(
        raw,
        extension="json",
        content_type="application/json",
    )
    normalized_hash = None if text is None else archive.archive_normalized_text(text)
    content_hash = normalized_hash or raw_hash
    revision = ExternalSourceRevision(
        source="veritawire_truth_social",
        source_fact_id=fact_id,
        source_revision_id=source_revision_id(
            source="veritawire_truth_social",
            source_fact_id=fact_id,
            canonical_content_hash=content_hash,
            lifecycle_state=LifecycleState.ACTIVE,
            adapter_version="veritawire_adapter_v1",
        ),
        revision_sequence=1,
        supersedes_revision_id=None,
        lifecycle_state=LifecycleState.ACTIVE,
        lifecycle_effective_at=observed_at,
        system_observed_at=observed_at,
        source_available_at=observed_at - timedelta(seconds=10),
        archived_at=observed_at + timedelta(milliseconds=25),
        raw_object_hash=raw_hash,
        document_hash=content_hash,
        normalized_text_hash=normalized_hash,
        canonical_content_hash=content_hash,
        source_type="social_post",
        source_title=source_title,
        source_platform="truth_social_via_veritawire",
        affected_tickers=fixed_tickers,
        adapter_version="veritawire_adapter_v1",
        extractor_version="veritawire_html_v1",
        normalizer_version=HTML_NORMALIZER_VERSION,
        collection_mode=collection_mode,
    )
    archive.publish_revision(revision)
    return revision


def _pipeline(
    archive: ExternalEventArchive,
    classifier: FakeClassifier,
    clock: MutableClock,
    *,
    questdb_writer: object | None = None,
) -> ExternalClassificationPipeline:
    return ExternalClassificationPipeline(
        archive=archive,
        classifier=classifier,
        profile=_profile(),
        approved_tickers=("PLTR", "LMT", "RTX"),
        approved_sectors=("DEFENSE", "ENERGY"),
        ticker_sector_hints={
            "LMT": "DEFENSE",
            "PLTR": "DEFENSE",
            "RTX": "DEFENSE",
        },
        questdb_writer=questdb_writer,  # type: ignore[arg-type]
        now=clock,
    )


def _publish_conflicting_backfill_attempt(
    archive: ExternalEventArchive,
    *,
    fingerprint: str,
    profile: ResearchSourceClassificationProfile,
) -> tuple[str, str]:
    """Publish a safe synthetic contradictory backfill for conflict tests."""

    contradictory_output = {
        "status": "VALID",
        "event_type": "LEGAL",
        "risk_level": "HIGH",
        "urgency": "HIGH",
        "confidence": 0.95,
        "summary": "Synthetic contradictory classification.",
        "affected_tickers": ["PLTR"],
        "affected_sectors": [],
        "global_relevance": False,
    }
    complete_hash, policy_hash = output_fingerprints(contradictory_output)
    archive.publish_classification_attempt(
        classification_input_fingerprint=fingerprint,
        attempt_id="imported-backfill-attempt",
        payload={
            "classification_attempt_id": "imported-backfill-attempt",
            "classification_input_fingerprint": fingerprint,
            "profile_hash": profile.profile_hash,
            "profile": profile.to_fingerprint_payload(),
            "status": "VALID",
            "validation_outcome": True,
            "durably_published": True,
            "complete_output_fingerprint": complete_hash,
            "policy_output_fingerprint": policy_hash,
            "normalized_output": contradictory_output,
            "classification_origin": "BACKFILL",
        },
    )
    return complete_hash, policy_hash


def test_prepare_builds_existing_contract_chain_and_semantic_input_identity(
    tmp_path,
) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    archive = ExternalEventArchive(tmp_path, now=clock)
    first = _archive_revision(
        archive,
        text="Palantir announced a new program.",
        fact_id="truth-123",
        observed_at=T0,
    )
    second = _archive_revision(
        archive,
        text="Palantir announced a new program.",
        fact_id="truth-456",
        observed_at=T0 + timedelta(hours=1),
    )
    classifier = FakeClassifier(clock=clock, results=[])
    pipeline = _pipeline(archive, classifier, clock)

    prepared_first = pipeline.prepare(first)
    prepared_second = pipeline.prepare(second)

    assert prepared_first is not None
    assert prepared_second is not None
    assert prepared_first.raw_input.source_revision_id != (
        prepared_second.raw_input.source_revision_id
    )
    assert prepared_first.source_document.raw_input_id != (
        prepared_second.source_document.raw_input_id
    )
    assert prepared_first.request.classification_input_fingerprint == (
        prepared_second.request.classification_input_fingerprint
    )
    assert prepared_first.request.input_text == "Palantir announced a new program."
    assert prepared_first.request.affected_tickers == ["PLTR"]


def test_allowed_scope_universe_changes_canonical_classification_input(
    tmp_path,
) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    archive = ExternalEventArchive(tmp_path, now=clock)
    revision = _archive_revision(
        archive,
        text="Palantir announced a new program.",
    )
    classifier = FakeClassifier(clock=clock, results=[])
    baseline = _pipeline(archive, classifier, clock).prepare(revision)
    expanded = ExternalClassificationPipeline(
        archive=archive,
        classifier=classifier,
        profile=_profile(),
        approved_tickers=("LMT", "PLTR", "RTX", "XOM"),
        approved_sectors=("DEFENSE", "ENERGY"),
        ticker_sector_hints={
            "LMT": "DEFENSE",
            "PLTR": "DEFENSE",
            "RTX": "DEFENSE",
            "XOM": "ENERGY",
        },
        now=clock,
    ).prepare(revision)

    assert baseline is not None
    assert expanded is not None
    assert baseline.request.classification_input_fingerprint != (
        expanded.request.classification_input_fingerprint
    )


def test_short_document_title_is_in_classifier_input_scope_and_identity(
    tmp_path,
) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    archive = ExternalEventArchive(tmp_path, now=clock)
    lmt = _archive_revision(
        archive,
        text="Official program announcement.",
        fact_id="release-lmt",
        source_title="Lockheed Martin wins a program award",
    )
    pltr = _archive_revision(
        archive,
        text="Official program announcement.",
        fact_id="release-pltr",
        source_title="Palantir wins a program award",
    )
    pipeline = _pipeline(archive, FakeClassifier(clock=clock, results=[]), clock)

    prepared_lmt = pipeline.prepare(lmt)
    prepared_pltr = pipeline.prepare(pltr)

    assert prepared_lmt is not None
    assert prepared_pltr is not None
    assert prepared_lmt.request.input_text.startswith("[TITLE]\nLockheed Martin")
    assert prepared_lmt.request.affected_tickers == ["LMT"]
    assert prepared_pltr.request.affected_tickers == ["PLTR"]
    assert prepared_lmt.request.classification_input_fingerprint != (
        prepared_pltr.request.classification_input_fingerprint
    )


def test_every_text_bearing_trump_post_reaches_classifier_without_keyword_gate(
    tmp_path,
) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    archive = ExternalEventArchive(tmp_path, now=clock)
    revision = _archive_revision(
        archive,
        text="A statement with no approved company or sector aliases.",
    )
    classifier = FakeClassifier(
        clock=clock,
        results=[FakeResultSpec(status=ContextClassificationStatus.VALID)],
    )

    outcome = _pipeline(archive, classifier, clock).process_revision(revision)

    assert len(classifier.calls) == 1
    assert outcome.status == "VALID"
    assert outcome.policy_eligible is False


def test_scope_union_preserves_global_ticker_and_multiple_sectors(tmp_path) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    archive = ExternalEventArchive(tmp_path, now=clock)
    revision = _archive_revision(
        archive,
        text="Lockheed Martin supports the defense industrial base.",
        fixed_tickers=("PLTR",),
    )
    classifier = FakeClassifier(
        clock=clock,
        results=[
            FakeResultSpec(
                status=ContextClassificationStatus.VALID,
                affected_tickers=("RTX",),
                affected_sectors=("ENERGY",),
                global_relevance=True,
            )
        ],
    )

    outcome = _pipeline(archive, classifier, clock).process_revision(revision)

    assert outcome.context_event is not None
    assert outcome.context_event.affected_tickers == ["LMT", "PLTR", "RTX"]
    assert outcome.context_event.affected_sectors == ["DEFENSE", "ENERGY"]
    assert outcome.context_event.global_relevance is True


def test_valid_result_is_reused_after_restart_without_another_provider_call(
    tmp_path,
) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    first_archive = ExternalEventArchive(tmp_path, now=clock)
    revision = _archive_revision(first_archive, text="Palantir announced results.")
    first_classifier = FakeClassifier(
        clock=clock,
        results=[
            FakeResultSpec(
                status=ContextClassificationStatus.VALID,
                affected_tickers=("PLTR",),
            )
        ],
    )
    first = _pipeline(first_archive, first_classifier, clock).process_revision(
        revision
    )

    clock.value = T0 + timedelta(hours=1)
    restarted_archive = ExternalEventArchive(tmp_path, now=clock)
    no_call_classifier = FakeClassifier(clock=clock, results=[])
    second = _pipeline(
        restarted_archive,
        no_call_classifier,
        clock,
    ).process_revision(revision)

    assert first.status == second.status == "VALID"
    assert first.classification_input_fingerprint == (
        second.classification_input_fingerprint
    )
    assert first.evidence_ready_at == second.evidence_ready_at
    assert first.context_event is not None
    assert second.context_event is not None
    assert first.context_event.available_at == first.evidence_ready_at
    assert second.context_event.available_at == second.evidence_ready_at
    assert first.context_event.source_available_at == revision.source_available_at
    assert len(first_classifier.calls) == 1
    assert no_call_classifier.calls == []
    assert second.reused_canonical_result is True


def test_questdb_failure_after_durable_publication_does_not_repeat_provider_call(
    tmp_path,
) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    first_archive = ExternalEventArchive(tmp_path, now=clock)
    revision = _archive_revision(first_archive, text="Palantir announced results.")
    first_classifier = FakeClassifier(
        clock=clock,
        results=[
            FakeResultSpec(
                status=ContextClassificationStatus.VALID,
                affected_tickers=("PLTR",),
            )
        ],
    )

    class FailingWriter:
        def write(self, _record: object) -> None:
            raise RuntimeError("synthetic metadata writer outage")

    with pytest.raises(RuntimeError, match="metadata writer outage"):
        _pipeline(
            first_archive,
            first_classifier,
            clock,
            questdb_writer=FailingWriter(),
        ).process_revision(revision)

    clock.value = T0 + timedelta(hours=1)
    persisted: list[object] = []

    class RecordingWriter:
        def write(self, record: object) -> None:
            persisted.append(record)

    no_call_classifier = FakeClassifier(clock=clock, results=[])
    restarted = _pipeline(
        ExternalEventArchive(tmp_path, now=clock),
        no_call_classifier,
        clock,
        questdb_writer=RecordingWriter(),
    ).process_revision(revision)

    assert restarted.status == "VALID"
    assert restarted.provider_called is False
    assert restarted.reused_canonical_result is True
    assert len(first_classifier.calls) == 1
    assert no_call_classifier.calls == []
    assert len(persisted) == 1


def test_restart_claims_validated_attempt_left_before_canonical_publication(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    first_archive = ExternalEventArchive(tmp_path, now=clock)
    revision = _archive_revision(first_archive, text="Palantir announced results.")
    first_classifier = FakeClassifier(
        clock=clock,
        results=[
            FakeResultSpec(
                status=ContextClassificationStatus.VALID,
                affected_tickers=("PLTR",),
            )
        ],
    )

    def crash_before_claim(**_kwargs: object) -> dict[str, object]:
        raise ExternalEventArchiveError("simulated crash before canonical claim")

    monkeypatch.setattr(
        first_archive,
        "claim_canonical_result",
        crash_before_claim,
    )
    with pytest.raises(
        ExternalEventArchiveError,
        match="simulated crash before canonical claim",
    ):
        _pipeline(first_archive, first_classifier, clock).process_revision(revision)
    assert len(first_classifier.calls) == 1

    clock.value = T0 + timedelta(minutes=5)
    restarted = ExternalEventArchive(tmp_path, now=clock)
    no_call_classifier = FakeClassifier(clock=clock, results=[])
    recovered = _pipeline(restarted, no_call_classifier, clock).process_revision(
        revision
    )

    assert recovered.status == "VALID"
    assert recovered.reused_canonical_result is True
    assert recovered.evidence_ready_at == clock.value
    assert no_call_classifier.calls == []
    assert restarted.read_canonical_claim(
        recovered.classification_input_fingerprint or ""
    ) is not None


def test_restart_adopts_attempt_written_before_manifest_registration_without_provider_call(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    first_archive = ExternalEventArchive(tmp_path, now=clock)
    revision = _archive_revision(
        first_archive, text="Palantir announced results."
    )
    first_classifier = FakeClassifier(
        clock=clock,
        results=[
            FakeResultSpec(
                status=ContextClassificationStatus.VALID,
                affected_tickers=("PLTR",),
            )
        ],
    )
    original_register = first_archive._register_classification_artifact

    def crash_attempt_registration(
        category: str,
        identities: tuple[str, ...],
        path: object,
    ) -> None:
        if category == "attempts":
            raise ExternalEventArchiveError(
                "simulated crash after attempt file publication"
            )
        original_register(category, identities, path)  # type: ignore[arg-type]

    monkeypatch.setattr(
        first_archive,
        "_register_classification_artifact",
        crash_attempt_registration,
    )
    with pytest.raises(
        ExternalEventArchiveError,
        match="simulated crash after attempt file publication",
    ):
        _pipeline(first_archive, first_classifier, clock).process_revision(
            revision
        )
    assert len(first_classifier.calls) == 1

    clock.value = T0 + timedelta(minutes=5)
    restarted = ExternalEventArchive(tmp_path, now=clock)
    no_call_classifier = FakeClassifier(clock=clock, results=[])
    recovered = _pipeline(restarted, no_call_classifier, clock).process_revision(
        revision
    )

    assert recovered.status == "VALID"
    assert recovered.reused_canonical_result is True
    assert no_call_classifier.calls == []
    attempts = restarted.iter_classification_attempts(
        recovered.classification_input_fingerprint or ""
    )
    assert [item["classification_attempt_id"] for item in attempts] == [
        "fake-attempt-1"
    ]


def test_restart_adopts_canonical_claim_written_before_manifest_registration_without_provider_call(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    first_archive = ExternalEventArchive(tmp_path, now=clock)
    revision = _archive_revision(
        first_archive, text="Palantir announced results."
    )
    first_classifier = FakeClassifier(
        clock=clock,
        results=[
            FakeResultSpec(
                status=ContextClassificationStatus.VALID,
                affected_tickers=("PLTR",),
            )
        ],
    )
    original_register = first_archive._register_classification_artifact

    def crash_claim_registration(
        category: str,
        identities: tuple[str, ...],
        path: object,
    ) -> None:
        if category == "canonical_claims":
            raise ExternalEventArchiveError(
                "simulated crash after canonical claim publication"
            )
        original_register(category, identities, path)  # type: ignore[arg-type]

    monkeypatch.setattr(
        first_archive,
        "_register_classification_artifact",
        crash_claim_registration,
    )
    with pytest.raises(
        ExternalEventArchiveError,
        match="simulated crash after canonical claim publication",
    ):
        _pipeline(first_archive, first_classifier, clock).process_revision(
            revision
        )
    assert len(first_classifier.calls) == 1

    clock.value = T0 + timedelta(minutes=5)
    restarted = ExternalEventArchive(tmp_path, now=clock)
    no_call_classifier = FakeClassifier(clock=clock, results=[])
    recovered = _pipeline(restarted, no_call_classifier, clock).process_revision(
        revision
    )

    assert recovered.status == "VALID"
    assert recovered.reused_canonical_result is True
    # The provider result and canonical ownership are reused, but the event was
    # not materially ready until restart completed durable publication.
    assert recovered.evidence_ready_at == T0 + timedelta(minutes=5)
    assert no_call_classifier.calls == []
    canonical = restarted.read_canonical_claim(
        recovered.classification_input_fingerprint or ""
    )
    assert canonical is not None
    assert canonical["canonical_classification_attempt_id"] == "fake-attempt-1"


def test_restart_adopts_materialized_event_written_before_manifest_registration_without_provider_call(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    first_archive = ExternalEventArchive(tmp_path, now=clock)
    revision = _archive_revision(
        first_archive, text="Palantir announced results."
    )
    first_classifier = FakeClassifier(
        clock=clock,
        results=[
            FakeResultSpec(
                status=ContextClassificationStatus.VALID,
                affected_tickers=("PLTR",),
            )
        ],
    )
    original_register = first_archive._register_classification_artifact

    def crash_event_registration(
        category: str,
        identities: tuple[str, ...],
        path: object,
    ) -> None:
        if category == "events":
            raise ExternalEventArchiveError(
                "simulated crash after materialized event publication"
            )
        original_register(category, identities, path)  # type: ignore[arg-type]

    monkeypatch.setattr(
        first_archive,
        "_register_classification_artifact",
        crash_event_registration,
    )
    with pytest.raises(
        ExternalEventArchiveError,
        match="simulated crash after materialized event publication",
    ):
        _pipeline(first_archive, first_classifier, clock).process_revision(
            revision
        )
    assert len(first_classifier.calls) == 1

    clock.value = T0 + timedelta(minutes=5)
    restarted = ExternalEventArchive(tmp_path, now=clock)
    no_call_classifier = FakeClassifier(clock=clock, results=[])
    recovered = _pipeline(restarted, no_call_classifier, clock).process_revision(
        revision
    )

    assert recovered.status == "VALID"
    assert recovered.reused_canonical_result is True
    assert recovered.evidence_ready_at == T0 + timedelta(minutes=5)
    assert no_call_classifier.calls == []
    assert restarted.read_materialized_event(
        revision.source_revision_id,
        classification_input_fingerprint=(
            recovered.classification_input_fingerprint or ""
        ),
    ) is not None


def test_orphan_reconciliation_reuses_older_canonical_result_for_later_observation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later revision need not place normalization before old classification."""

    clock = MutableClock(T0 + timedelta(seconds=4))
    archive = ExternalEventArchive(tmp_path, now=clock)
    original = _archive_revision(
        archive, text="Palantir announced results.", fact_id="truth-original"
    )
    classifier = FakeClassifier(
        clock=clock,
        results=[
            FakeResultSpec(
                status=ContextClassificationStatus.VALID,
                affected_tickers=("PLTR",),
            )
        ],
    )
    _pipeline(archive, classifier, clock).process_revision(original)

    clock.value = T0 + timedelta(minutes=5)
    later = _archive_revision(
        archive,
        text="Palantir announced results.",
        fact_id="truth-later-observation",
        observed_at=clock.value,
    )
    original_register = archive._register_classification_artifact

    def crash_after_event_write(
        category: str, identities: tuple[str, ...], path: object
    ) -> None:
        if category == "events":
            raise ExternalEventArchiveError("simulated crash after event write")
        original_register(category, identities, path)  # type: ignore[arg-type]

    monkeypatch.setattr(
        archive, "_register_classification_artifact", crash_after_event_write
    )
    no_call_classifier = FakeClassifier(clock=clock, results=[])
    with pytest.raises(ExternalEventArchiveError, match="simulated crash"):
        _pipeline(archive, no_call_classifier, clock).process_revision(later)
    assert no_call_classifier.calls == []

    clock.value = T0 + timedelta(minutes=10)
    restarted = ExternalEventArchive(tmp_path, now=clock)
    recovered_classifier = FakeClassifier(clock=clock, results=[])
    recovered = _pipeline(
        restarted, recovered_classifier, clock
    ).process_revision(later)

    assert recovered.status == "VALID"
    assert recovered.reused_canonical_result is True
    assert recovered.evidence_ready_at == clock.value
    assert recovered_classifier.calls == []
    assert restarted.read_materialized_event(
        later.source_revision_id,
        classification_input_fingerprint=(
            recovered.classification_input_fingerprint or ""
        ),
    ) is not None


def test_restart_adopts_readiness_written_before_manifest_registration_without_provider_call(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    first_archive = ExternalEventArchive(tmp_path, now=clock)
    revision = _archive_revision(
        first_archive, text="Palantir announced results."
    )
    first_classifier = FakeClassifier(
        clock=clock,
        results=[
            FakeResultSpec(
                status=ContextClassificationStatus.VALID,
                affected_tickers=("PLTR",),
            )
        ],
    )
    original_register = first_archive._register_classification_artifact

    def crash_readiness_registration(
        category: str,
        identities: tuple[str, ...],
        path: object,
    ) -> None:
        if category == "readiness":
            raise ExternalEventArchiveError(
                "simulated crash after readiness publication"
            )
        original_register(category, identities, path)  # type: ignore[arg-type]

    monkeypatch.setattr(
        first_archive,
        "_register_classification_artifact",
        crash_readiness_registration,
    )
    with pytest.raises(
        ExternalEventArchiveError,
        match="simulated crash after readiness publication",
    ):
        _pipeline(first_archive, first_classifier, clock).process_revision(
            revision
        )
    assert len(first_classifier.calls) == 1

    clock.value = T0 + timedelta(minutes=5)
    restarted = ExternalEventArchive(tmp_path, now=clock)
    no_call_classifier = FakeClassifier(clock=clock, results=[])
    recovered = _pipeline(restarted, no_call_classifier, clock).process_revision(
        revision
    )

    assert recovered.status == "VALID"
    assert recovered.reused_canonical_result is True
    assert recovered.evidence_ready_at == T0 + timedelta(seconds=4)
    assert no_call_classifier.calls == []
    readiness = restarted.read_readiness(
        revision.source_revision_id,
        classification_input_fingerprint=(
            recovered.classification_input_fingerprint or ""
        ),
    )
    assert readiness is not None
    assert readiness["evidence_ready_at"] == "2026-07-18T16:00:04Z"


def test_backfill_reuses_canonical_live_input_without_calling_provider(
    tmp_path,
) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    archive = ExternalEventArchive(tmp_path, now=clock)
    live_revision = _archive_revision(
        archive,
        text="Palantir announced results.",
        fact_id="truth-live",
        collection_mode="LIVE_SYSTEM",
    )
    live_classifier = FakeClassifier(
        clock=clock,
        results=[
            FakeResultSpec(
                status=ContextClassificationStatus.VALID,
                affected_tickers=("PLTR",),
            )
        ],
    )
    live = _pipeline(archive, live_classifier, clock).process_revision(live_revision)

    clock.value = T0 + timedelta(days=1)
    backfill_revision = _archive_revision(
        archive,
        text="Palantir announced results.",
        fact_id="truth-backfill-observation",
        observed_at=clock.value,
        collection_mode="BACKFILL",
    )
    no_call_classifier = FakeClassifier(clock=clock, results=[])
    backfill = _pipeline(
        ExternalEventArchive(tmp_path, now=clock),
        no_call_classifier,
        clock,
    ).process_revision(backfill_revision)

    assert live.classification_input_fingerprint == (
        backfill.classification_input_fingerprint
    )
    assert live.evidence_ready_at is not None
    assert backfill.evidence_ready_at is not None
    assert backfill.evidence_ready_at > live.evidence_ready_at
    assert len(live_classifier.calls) == 1
    assert no_call_classifier.calls == []
    assert backfill.reused_canonical_result is True
    assert live.context_event is not None
    assert backfill.context_event is not None
    assert live.context_event.available_at == live.evidence_ready_at
    assert backfill.context_event.available_at == backfill.evidence_ready_at
    assert backfill.context_event.available_at > live.context_event.available_at
    stored_backfill_event = archive.read_materialized_event(
        backfill_revision.source_revision_id,
        classification_input_fingerprint=(
            backfill.classification_input_fingerprint or ""
        ),
    )
    assert stored_backfill_event is not None
    assert stored_backfill_event["available_at"] is None
    assert stored_backfill_event["evidence_ready_at"] is None


def test_abstained_result_is_durably_suppressed_after_restart(tmp_path) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    first_archive = ExternalEventArchive(tmp_path, now=clock)
    revision = _archive_revision(first_archive, text="A text-bearing social post.")
    first_classifier = FakeClassifier(
        clock=clock,
        results=[FakeResultSpec(status=ContextClassificationStatus.ABSTAINED)],
    )
    first = _pipeline(first_archive, first_classifier, clock).process_revision(
        revision
    )

    clock.value = T0 + timedelta(hours=1)
    no_call_classifier = FakeClassifier(clock=clock, results=[])
    second = _pipeline(
        ExternalEventArchive(tmp_path, now=clock),
        no_call_classifier,
        clock,
    ).process_revision(revision)

    assert first.status == second.status == "ABSTAINED"
    assert first.context_event is second.context_event is None
    assert first.evidence_ready_at == second.evidence_ready_at
    assert no_call_classifier.calls == []


def test_exact_semantic_input_across_observations_reuses_one_classification(
    tmp_path,
) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    archive = ExternalEventArchive(tmp_path, now=clock)
    first_revision = _archive_revision(
        archive,
        text="Palantir announced results.",
        fact_id="truth-first",
        observed_at=T0,
    )
    second_revision = _archive_revision(
        archive,
        text="Palantir announced results.",
        fact_id="truth-replay-observation",
        observed_at=T0 + timedelta(minutes=1),
    )
    classifier = FakeClassifier(
        clock=clock,
        results=[
            FakeResultSpec(
                status=ContextClassificationStatus.VALID,
                affected_tickers=("PLTR",),
            )
        ],
    )
    pipeline = _pipeline(archive, classifier, clock)

    first = pipeline.process_revision(first_revision)
    clock.value = T0 + timedelta(minutes=2)
    second = pipeline.process_revision(second_revision)

    assert len(classifier.calls) == 1
    assert first.classification_input_fingerprint == (
        second.classification_input_fingerprint
    )
    assert second.reused_canonical_result is True
    assert first.context_event is not None
    assert second.context_event is not None
    assert first.context_event.source_fact_id == "truth-first"
    assert second.context_event.source_fact_id == "truth-replay-observation"


def test_provider_failure_is_retryable_and_does_not_claim_canonical_input(
    tmp_path,
) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    archive = ExternalEventArchive(tmp_path, now=clock)
    revision = _archive_revision(archive, text="Palantir announced results.")
    classifier = FakeClassifier(
        clock=clock,
        results=[
            FakeResultSpec(status=ContextClassificationStatus.PROVIDER_FAILED),
            FakeResultSpec(
                status=ContextClassificationStatus.VALID,
                affected_tickers=("PLTR",),
            ),
        ],
    )
    pipeline = _pipeline(archive, classifier, clock)

    failed = pipeline.process_revision(revision)
    assert failed.status == "PROVIDER_FAILED"
    assert failed.classification_input_fingerprint is not None
    assert archive.read_canonical_claim(failed.classification_input_fingerprint) is None

    clock.value = T0 + timedelta(seconds=8)
    completed = pipeline.process_revision(revision)
    assert completed.status == "VALID"
    assert len(classifier.calls) == 2
    assert archive.read_canonical_claim(
        completed.classification_input_fingerprint or ""
    ) is not None


def test_validation_rejection_is_retryable_and_never_claims_completion(
    tmp_path,
) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    archive = ExternalEventArchive(tmp_path, now=clock)
    revision = _archive_revision(archive, text="Palantir announced results.")
    classifier = FakeClassifier(
        clock=clock,
        results=[
            FakeResultSpec(
                status=ContextClassificationStatus.VALIDATION_REJECTED
            ),
            FakeResultSpec(
                status=ContextClassificationStatus.VALID,
                affected_tickers=("PLTR",),
            ),
        ],
    )
    pipeline = _pipeline(archive, classifier, clock)

    rejected = pipeline.process_revision(revision)
    assert rejected.status == "VALIDATION_REJECTED"
    assert rejected.classification_input_fingerprint is not None
    assert archive.read_canonical_claim(rejected.classification_input_fingerprint) is None

    clock.value = T0 + timedelta(seconds=8)
    completed = pipeline.process_revision(revision)
    assert completed.status == "VALID"
    assert len(classifier.calls) == 2


def test_contradictory_import_blocks_materialization_without_another_call(
    tmp_path,
) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    archive = ExternalEventArchive(tmp_path, now=clock)
    revision = _archive_revision(archive, text="Palantir announced results.")
    classifier = FakeClassifier(
        clock=clock,
        results=[
            FakeResultSpec(
                status=ContextClassificationStatus.VALID,
                affected_tickers=("PLTR",),
            )
        ],
    )
    completed = _pipeline(archive, classifier, clock).process_revision(revision)
    fingerprint = completed.classification_input_fingerprint
    assert fingerprint is not None
    canonical = archive.read_canonical_claim(fingerprint)
    assert canonical is not None

    contradictory_output = {
        "status": "VALID",
        "event_type": "LEGAL",
        "risk_level": "HIGH",
        "urgency": "HIGH",
        "confidence": 0.95,
        "summary": "Contradictory imported classification.",
        "affected_tickers": ["PLTR"],
        "affected_sectors": [],
        "global_relevance": False,
    }
    complete_hash, policy_hash = output_fingerprints(contradictory_output)
    archive.publish_classification_attempt(
        classification_input_fingerprint=fingerprint,
        attempt_id="imported-backfill-attempt",
        payload={
            "classification_attempt_id": "imported-backfill-attempt",
            "classification_input_fingerprint": fingerprint,
            "profile_hash": _profile().profile_hash,
            "status": "VALID",
            "validation_outcome": True,
            "durably_published": True,
            "complete_output_fingerprint": complete_hash,
            "policy_output_fingerprint": policy_hash,
            "first_archived_at": "2026-07-18T17:00:00Z",
            "normalized_output": contradictory_output,
        },
    )

    no_call_classifier = FakeClassifier(clock=clock, results=[])
    conflicted = _pipeline(archive, no_call_classifier, clock).process_revision(
        revision
    )

    assert conflicted.status == "CLASSIFICATION_CONFLICT"
    assert conflicted.policy_eligible is False
    assert conflicted.context_event is None
    assert no_call_classifier.calls == []
    assert archive.detect_classification_conflict(fingerprint) is not None


def test_reviewed_keep_first_conflict_reuses_canonical_result_for_new_observation(
    tmp_path,
) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    archive = ExternalEventArchive(tmp_path, now=clock)
    original = _archive_revision(
        archive, text="Palantir announced results.", fact_id="truth-live"
    )
    live_classifier = FakeClassifier(
        clock=clock,
        results=[
            FakeResultSpec(
                status=ContextClassificationStatus.VALID,
                affected_tickers=("PLTR",),
            )
        ],
    )
    live = _pipeline(archive, live_classifier, clock).process_revision(original)
    fingerprint = live.classification_input_fingerprint
    assert fingerprint is not None
    canonical = archive.read_canonical_claim(fingerprint)
    assert canonical is not None

    clock.value = T0 + timedelta(minutes=1)
    _publish_conflicting_backfill_attempt(
        archive, fingerprint=fingerprint, profile=_profile()
    )
    conflict = archive.detect_classification_conflict(fingerprint)
    assert conflict is not None
    archive.publish_conflict_resolution(
        conflict_id=str(conflict["classification_conflict_id"]),
        decision=ConflictResolutionDecision.KEEP_FIRST_DURABLY_PUBLISHED,
        reviewer="fixture-reviewer",
        reason="The live canonical attempt was durably first.",
        chosen_attempt_id=str(canonical["canonical_classification_attempt_id"]),
        chosen_complete_output_fingerprint=str(
            canonical["complete_output_fingerprint"]
        ),
        chosen_policy_output_fingerprint=str(
            canonical["policy_output_fingerprint"]
        ),
    )

    clock.value = T0 + timedelta(minutes=5)
    replay = _archive_revision(
        archive,
        text="Palantir announced results.",
        fact_id="truth-replayed-observation",
        observed_at=clock.value,
    )
    no_call_classifier = FakeClassifier(clock=clock, results=[])
    replayed = _pipeline(archive, no_call_classifier, clock).process_revision(
        replay
    )

    assert replayed.status == "VALID"
    assert replayed.reused_canonical_result is True
    assert replayed.context_event is not None
    assert replayed.evidence_ready_at == clock.value + timedelta(milliseconds=25)
    assert no_call_classifier.calls == []
    readiness = archive.read_readiness(
        replay.source_revision_id,
        classification_input_fingerprint=fingerprint,
    )
    assert readiness is not None
    assert readiness["canonical_classification_attempt_id"] == canonical[
        "canonical_classification_attempt_id"
    ]


def test_reviewed_abstain_conflict_does_not_materialize_a_replayed_observation(
    tmp_path,
) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    archive = ExternalEventArchive(tmp_path, now=clock)
    original = _archive_revision(
        archive, text="Palantir announced results.", fact_id="truth-live"
    )
    completed = _pipeline(
        archive,
        FakeClassifier(
            clock=clock,
            results=[
                FakeResultSpec(
                    status=ContextClassificationStatus.VALID,
                    affected_tickers=("PLTR",),
                )
            ],
        ),
        clock,
    ).process_revision(original)
    fingerprint = completed.classification_input_fingerprint
    assert fingerprint is not None

    clock.value = T0 + timedelta(minutes=1)
    _publish_conflicting_backfill_attempt(
        archive, fingerprint=fingerprint, profile=_profile()
    )
    conflict = archive.detect_classification_conflict(fingerprint)
    assert conflict is not None
    archive.publish_conflict_resolution(
        conflict_id=str(conflict["classification_conflict_id"]),
        decision=ConflictResolutionDecision.ABSTAIN_INPUT,
        reviewer="fixture-reviewer",
        reason="The contradictory output is deliberately abstained.",
    )

    clock.value = T0 + timedelta(minutes=5)
    replay = _archive_revision(
        archive,
        text="Palantir announced results.",
        fact_id="truth-replayed-observation",
        observed_at=clock.value,
    )
    no_call_classifier = FakeClassifier(clock=clock, results=[])
    outcome = _pipeline(archive, no_call_classifier, clock).process_revision(replay)

    assert outcome.status == "ABSTAIN_INPUT"
    assert outcome.context_event is None
    assert outcome.evidence_ready_at is None
    assert no_call_classifier.calls == []
    assert archive.read_readiness(
        replay.source_revision_id,
        classification_input_fingerprint=fingerprint,
    ) is None


def test_empty_or_media_only_revision_is_archived_without_classifier_call(
    tmp_path,
) -> None:
    clock = MutableClock(T0 + timedelta(seconds=4))
    archive = ExternalEventArchive(tmp_path, now=clock)
    revision = _archive_revision(archive, text=None)
    classifier = FakeClassifier(clock=clock, results=[])

    outcome = _pipeline(archive, classifier, clock).process_revision(revision)

    assert outcome.status == "NO_TEXT"
    assert outcome.classification_input_fingerprint is None
    assert outcome.evidence_ready_at is None
    assert classifier.calls == []
    assert tuple(archive.iter_revisions()) == (revision,)
