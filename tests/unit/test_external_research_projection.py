from __future__ import annotations

from datetime import UTC, datetime, timedelta
from dataclasses import replace
from hashlib import sha256

import pytest

from market_relay_engine.common.serialization import to_json_dict, to_json_string
from market_relay_engine.context.decision_context import DecisionContextAssembler
from market_relay_engine.context.external_event_archive import (
    ConflictResolutionDecision,
    CoverageInterval,
    CoverageStatus,
    ExternalEventArchive,
    ExternalEventArchiveError,
    ExternalSourceRevision,
    LifecycleState,
    SourceCoverage,
    output_fingerprints,
)
from market_relay_engine.context.research_projection import (
    EvidenceCategory,
    EvidenceExclusionReason,
    ResearchAvailabilityMode,
    ResearchClassificationProfile,
    ResearchEvidence,
    ResearchEvidenceCapacityError,
    ResearchEvidenceIndex,
    ResearchEvidenceRelationship,
    ResearchLifecycleRevision,
    ResearchProjectionError,
    ResearchRunDefinition,
    ResearchSourceClassificationProfile,
    ResearchSourceCoverageProfile,
    build_shadow_context_fingerprint,
    hydrate_combined_research_evidence,
    hydrate_external_research_evidence,
    _correlate_official_company_observations,
    _external_canonical_classification_owner,
    _external_exact_duplicate_fingerprint,
)
from market_relay_engine.context.sec_edgar_archive import SECEDGARArchive
from market_relay_engine.context.shadow_evaluation import (
    ShadowContextPolicy,
    ShadowContextRule,
    evaluate_shadow_context,
)
from market_relay_engine.context.state_cache import ContextStateCache
from market_relay_engine.contracts.context import (
    ContextAIEvent,
    ContextClassificationEventType,
    ContextLifecycleState,
    ContextRiskLevel,
    ContextUrgency,
    ShadowContextAction,
)
from market_relay_engine.contracts.model import ModelSignal, SignalSide


T0 = datetime(2026, 7, 18, 16, 0, tzinfo=UTC)
T1 = T0
T2 = T0 + timedelta(seconds=10)
T3 = T0 + timedelta(seconds=14)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
SOURCE = "veritawire_truth_social"


def _classification_profile() -> ResearchClassificationProfile:
    return ResearchClassificationProfile(
        extraction_version="sec_8k_items_v1",
        prompt_version="context_filter_v1",
        model_version="gemini-test",
        response_schema_version="context_classification_response_v1",
        classification_config_hash=HASH_A,
    )


def _external_profile(
    *,
    source: str = SOURCE,
    source_type: str = "social_post",
    ticker: str | None = None,
    adapter_version: str = "adapter_v1",
    extraction_version: str = "extractor_v1",
    normalization_version: str = "normalizer_v1",
) -> ResearchSourceClassificationProfile:
    return ResearchSourceClassificationProfile(
        source=source,
        source_type=source_type,
        semantic_adapter_version=adapter_version,
        extraction_version=extraction_version,
        normalization_version=normalization_version,
        excerpt_version="scope_excerpt_v1",
        scope_version="external_scope_v1",
        prompt_version="context_filter_v2_scope",
        model_version="gemini-test",
        response_schema_version="context_classification_response_v2",
        validator_version="context_validator_v1",
        classification_config_hash=HASH_A,
        ticker=ticker,
    )


def _run(
    *,
    availability_mode: ResearchAvailabilityMode | None = ResearchAvailabilityMode.LIVE_SYSTEM_READY,
    event_sources: tuple[str, ...] = (SOURCE,),
    ticker_universe: tuple[str, ...] = ("LMT", "PLTR", "XOM"),
    capacity: int = 50,
    external_profiles: tuple[ResearchSourceClassificationProfile, ...] = (),
    coverage_profiles: tuple[ResearchSourceCoverageProfile, ...] = (),
    allow_incomplete_coverage: bool = False,
    conflict_resolution_generation: int | None = None,
    conflict_resolution_manifest_hash: str | None = None,
    external_archive_generation: int | None = None,
    external_archive_manifest_hash: str | None = None,
) -> ResearchRunDefinition:
    return ResearchRunDefinition(
        ticker_universe=ticker_universe,
        event_sources=event_sources,
        evidence_categories=(EvidenceCategory.AI_EVENT,),
        hydration_start_time=T0 - timedelta(hours=1),
        hydration_end_time=T0 + timedelta(hours=2),
        capacity=capacity,
        classification_profile=_classification_profile(),
        max_age_without_valid_until=timedelta(minutes=30),
        selection_policy_version="research_selection_v1",
        availability_mode=availability_mode,
        external_classification_profiles=external_profiles,
        source_coverage_profiles=coverage_profiles,
        allow_incomplete_coverage=allow_incomplete_coverage,
        conflict_resolution_generation=conflict_resolution_generation,
        conflict_resolution_manifest_hash=conflict_resolution_manifest_hash,
        lifecycle_version=(
            None if availability_mode is None else "external_lifecycle_v1"
        ),
        correlation_version=(
            None if availability_mode is None else "external_correlation_v1"
        ),
        external_archive_generation=external_archive_generation,
        external_archive_manifest_hash=external_archive_manifest_hash,
    )


def _context(
    evaluation_time: datetime,
    *,
    ticker: str = "LMT",
    sector: str = "DEFENSE",
):
    return DecisionContextAssembler(cache=ContextStateCache()).build_for_decision(
        ticker,
        evaluation_time,
        f"trace_{ticker}_{evaluation_time.timestamp()}",
        None,
        ticker_sector=sector,
    )


def _evidence(
    evidence_id: str,
    *,
    source: str = SOURCE,
    source_record_id: str | None = None,
    tickers: tuple[str, ...] = ("LMT",),
    sectors: tuple[str, ...] = (),
    global_relevance: bool = False,
    source_available_at: datetime | None = T1,
    system_observed_at: datetime | None = T1,
    evidence_ready_at: datetime | None = T1,
    source_fact_id: str | None = None,
    source_revision_id: str | None = None,
    revision_sequence: int | None = None,
    supersedes_revision_id: str | None = None,
    lifecycle_state: str | None = None,
    lifecycle_effective_at: datetime | None = None,
    classification_input_fingerprint: str | None = None,
    canonical_classification_owner_fingerprint: str | None = None,
    exact_duplicate_fingerprint: str | None = None,
    complete_output_fingerprint: str | None = HASH_C,
    policy_output_fingerprint: str | None = HASH_D,
    lineage_ids: tuple[str, ...] | None = None,
    lineage_visibility: dict[str, str] | None = None,
    marker: str | None = None,
) -> ResearchEvidence:
    lineage = lineage_ids or (f"observation_{evidence_id}",)
    visibility = lineage_visibility or {
        lineage[0]: (system_observed_at or T1).isoformat()
    }
    return ResearchEvidence(
        evidence_id=evidence_id,
        category=EvidenceCategory.AI_EVENT,
        policy_match_key="AI_EVENT_TYPE:SOCIAL_POLITICAL_STATEMENT",
        source=source,
        source_record_id=source_record_id or f"record_{evidence_id}",
        tickers=tickers,
        sectors=sectors,
        global_relevance=global_relevance,
        available_at=evidence_ready_at,
        source_available_at=source_available_at,
        system_observed_at=system_observed_at,
        evidence_ready_at=evidence_ready_at,
        fingerprint_payload={"marker": marker or evidence_id},
        lineage_ids=lineage,
        lineage_visibility=visibility,
        source_fact_id=source_fact_id,
        source_revision_id=source_revision_id,
        revision_sequence=revision_sequence,
        supersedes_revision_id=supersedes_revision_id,
        lifecycle_state=lifecycle_state,
        lifecycle_effective_at=lifecycle_effective_at,
        classification_input_fingerprint=classification_input_fingerprint,
        canonical_classification_owner_fingerprint=(
            canonical_classification_owner_fingerprint
        ),
        exact_duplicate_fingerprint=exact_duplicate_fingerprint,
        complete_output_fingerprint=complete_output_fingerprint,
        policy_output_fingerprint=policy_output_fingerprint,
    )


def _index(
    *evidence: ResearchEvidence,
    run: ResearchRunDefinition | None = None,
    lifecycle: tuple[ResearchLifecycleRevision, ...] = (),
    relationships: tuple[ResearchEvidenceRelationship, ...] = (),
) -> ResearchEvidenceIndex:
    return ResearchEvidenceIndex.build(
        run_definition=run or _run(),
        evidence=evidence,
        lifecycle_revisions=lifecycle,
        relationships=relationships,
    )


def _selected_ids(index: ResearchEvidenceIndex, at: datetime) -> list[str]:
    return [
        value.evidence_id
        for value in index.select(_context(at)).selected_evidence
    ]


def _lifecycle_marker(
    revision_id: str,
    *,
    sequence: int,
    observed_at: datetime,
    ready_at: datetime | None,
    state: str,
    supersedes: str | None = None,
    fact_id: str = "truth-123",
) -> ResearchLifecycleRevision:
    return ResearchLifecycleRevision(
        source=SOURCE,
        source_fact_id=fact_id,
        source_revision_id=revision_id,
        revision_sequence=sequence,
        supersedes_revision_id=supersedes,
        lifecycle_state=state,
        lifecycle_effective_at=observed_at,
        system_observed_at=observed_at,
        evidence_ready_at=ready_at,
    )


def test_lifecycle_edit_hides_old_content_until_new_revision_is_ready() -> None:
    original = _evidence(
        "original",
        source_fact_id="truth-123",
        source_revision_id="truth-123-r1",
        revision_sequence=1,
        lifecycle_state="ACTIVE",
        lifecycle_effective_at=T1,
    )
    edited = _evidence(
        "edited",
        source_fact_id="truth-123",
        source_revision_id="truth-123-r2",
        revision_sequence=2,
        supersedes_revision_id="truth-123-r1",
        lifecycle_state="UPDATED",
        lifecycle_effective_at=T2,
        system_observed_at=T2,
        evidence_ready_at=T3,
    )
    index = _index(
        original,
        edited,
        lifecycle=(
            _lifecycle_marker(
                "truth-123-r1", sequence=1, observed_at=T1, ready_at=T1, state="ACTIVE"
            ),
            _lifecycle_marker(
                "truth-123-r2",
                sequence=2,
                observed_at=T2,
                ready_at=T3,
                state="UPDATED",
                supersedes="truth-123-r1",
            ),
        ),
    )

    assert _selected_ids(index, T2 - timedelta(microseconds=1)) == ["original"]
    pending = index.select(_context(T2 + timedelta(seconds=2)))
    assert pending.selected_evidence == ()
    assert EvidenceExclusionReason.LIFECYCLE_REVISION_PENDING in {
        value.reason for value in pending.exclusions
    }
    assert _selected_ids(index, T3) == ["edited"]


@pytest.mark.parametrize("state", ["DELETED", "RETRACTED"])
def test_deleted_or_retracted_lifecycle_head_suppresses_prior_content(state: str) -> None:
    original = _evidence(
        "original",
        source_fact_id="truth-123",
        source_revision_id="truth-123-r1",
        revision_sequence=1,
        lifecycle_state="ACTIVE",
        lifecycle_effective_at=T1,
    )
    index = _index(
        original,
        lifecycle=(
            _lifecycle_marker(
                "truth-123-r1", sequence=1, observed_at=T1, ready_at=T1, state="ACTIVE"
            ),
            _lifecycle_marker(
                "truth-123-r2",
                sequence=2,
                observed_at=T2,
                ready_at=T2,
                state=state,
                supersedes="truth-123-r1",
            ),
        ),
    )

    assert _selected_ids(index, T2 - timedelta(microseconds=1)) == ["original"]
    deleted = index.select(_context(T2))
    assert deleted.selected_evidence == ()
    assert EvidenceExclusionReason.LIFECYCLE_DELETED_OR_RETRACTED in {
        value.reason for value in deleted.exclusions
    }


def test_same_time_and_sequence_lifecycle_heads_fail_closed() -> None:
    first = _evidence(
        "first",
        source_fact_id="truth-123",
        source_revision_id="truth-123-r2a",
        revision_sequence=2,
        lifecycle_state="UPDATED",
        lifecycle_effective_at=T2,
        system_observed_at=T2,
        evidence_ready_at=T2,
    )
    second = _evidence(
        "second",
        source_fact_id="truth-123",
        source_revision_id="truth-123-r2b",
        revision_sequence=2,
        lifecycle_state="UPDATED",
        lifecycle_effective_at=T2,
        system_observed_at=T2,
        evidence_ready_at=T2,
    )
    index = _index(first, second)

    selection = index.select(_context(T2))
    assert selection.selected_evidence == ()
    assert {
        value.evidence_id
        for value in selection.exclusions
        if value.reason is EvidenceExclusionReason.LIFECYCLE_ORDER_CONFLICT
    } == {"first", "second"}


def test_live_availability_waits_for_durable_evidence_readiness() -> None:
    ready_at = T1 + timedelta(seconds=4)
    evidence = _evidence(
        "slow-classification",
        system_observed_at=T1,
        source_available_at=T1,
        evidence_ready_at=ready_at,
    )
    index = _index(evidence)

    before = index.select(_context(T1 + timedelta(seconds=2)))
    assert before.selected_evidence == ()
    assert [value.reason for value in before.exclusions] == [
        EvidenceExclusionReason.FUTURE
    ]
    assert _selected_ids(index, ready_at) == ["slow-classification"]


def test_external_source_without_explicit_availability_mode_fails_closed() -> None:
    with pytest.raises(
        ResearchProjectionError,
        match="external .* require.* explicit availability mode",
    ):
        _index(
            _evidence("external-without-mode"),
            run=_run(availability_mode=None),
        )


def test_scope_union_matches_global_ticker_and_sector_without_duplication() -> None:
    evidence = _evidence(
        "multi-scope",
        tickers=("LMT",),
        sectors=("DEFENSE",),
        global_relevance=True,
    )
    index = _index(evidence)

    assert [
        item.evidence_id
        for item in index.select(_context(T1, ticker="LMT", sector="ENERGY")).selected_evidence
    ] == ["multi-scope"]
    assert [
        item.evidence_id
        for item in index.select(_context(T1, ticker="PLTR", sector="DEFENSE")).selected_evidence
    ] == ["multi-scope"]
    assert [
        item.evidence_id
        for item in index.select(_context(T1, ticker="XOM", sector="ENERGY")).selected_evidence
    ] == ["multi-scope"]


def test_multi_ticker_order_is_normalized_and_fingerprint_stable() -> None:
    first = _evidence("same", tickers=("PLTR", "LMT"), marker="same")
    second = _evidence("same", tickers=("LMT", "PLTR"), marker="same")

    assert first.tickers == second.tickers == ("LMT", "PLTR")
    assert first.to_fingerprint_payload() == second.to_fingerprint_payload()
    first_selection = _index(first).select(_context(T1, ticker="LMT"))
    second_selection = _index(second).select(_context(T1, ticker="LMT"))
    assert build_shadow_context_fingerprint(
        decision_context=_context(T1, ticker="LMT"),
        evidence_selection=first_selection,
    ) == build_shadow_context_fingerprint(
        decision_context=_context(T1, ticker="LMT"),
        evidence_selection=second_selection,
    )


def test_meaningful_duplicate_identity_uses_exact_content_not_ai_output_or_source_profile() -> None:
    profile = _external_profile()
    attempt = {
        "document_hash": HASH_A,
        "normalized_text_hash": HASH_B,
        "excerpt_hash": HASH_C,
        "normalized_output": {
            "affected_tickers": ["LMT"],
            "risk_level": "LOW",
        },
        "complete_output_fingerprint": HASH_D,
    }
    baseline = _external_exact_duplicate_fingerprint(
        attempt=attempt,
        profile=profile,
    )
    changed_ai_output = {
        **attempt,
        "normalized_output": {
            "affected_tickers": ["PLTR"],
            "risk_level": "HIGH",
        },
        "complete_output_fingerprint": "1" * 64,
    }

    assert _external_exact_duplicate_fingerprint(
        attempt=changed_ai_output,
        profile=profile,
    ) == baseline
    assert _external_exact_duplicate_fingerprint(
        attempt={**attempt, "document_hash": "2" * 64},
        profile=profile,
    ) != baseline
    assert _external_exact_duplicate_fingerprint(
        attempt=attempt,
        profile=replace(profile, excerpt_version="scope_excerpt_v2"),
    ) == baseline


def test_cross_source_owner_requires_exact_durable_trusted_input_scope() -> None:
    attempt = {
        "trusted_input_scope": {
            "affected_tickers": ["LMT"],
            "affected_sectors": [],
            "global_relevance": False,
        },
        "normalized_output": {"summary": "generated output is irrelevant"},
    }
    baseline = _external_canonical_classification_owner(
        attempt=attempt,
        classification_input_fingerprint=HASH_A,
        exact_duplicate_fingerprint=HASH_B,
    )
    assert baseline == _external_canonical_classification_owner(
        attempt={
            **attempt,
            "normalized_output": {"summary": "different generated output"},
        },
        classification_input_fingerprint=HASH_C,
        exact_duplicate_fingerprint=HASH_B,
    )
    assert baseline != _external_canonical_classification_owner(
        attempt={
            **attempt,
            "trusted_input_scope": {
                "affected_tickers": ["LMT"],
                "affected_sectors": ["DEFENSE"],
                "global_relevance": False,
            },
        },
        classification_input_fingerprint=HASH_A,
        exact_duplicate_fingerprint=HASH_B,
    )
    assert _external_canonical_classification_owner(
        attempt={},
        classification_input_fingerprint=HASH_A,
        exact_duplicate_fingerprint=HASH_B,
    ) == HASH_A


def test_contradictory_outputs_under_one_canonical_input_fail_closed() -> None:
    first = _evidence(
        "canonical-one",
        classification_input_fingerprint=HASH_A,
        exact_duplicate_fingerprint=HASH_B,
        complete_output_fingerprint=HASH_C,
    )
    second = _evidence(
        "canonical-two",
        classification_input_fingerprint=HASH_A,
        exact_duplicate_fingerprint=HASH_B,
        complete_output_fingerprint=HASH_D,
    )

    selection = _index(first, second).select(_context(T1))
    assert selection.selected_evidence == ()
    assert {
        value.evidence_id
        for value in selection.exclusions
        if value.reason is EvidenceExclusionReason.CLASSIFICATION_CONFLICT
    } == {"canonical-one", "canonical-two"}


def test_contradictory_outputs_under_shared_cross_source_owner_fail_closed() -> None:
    sec = _evidence(
        "sec-shared-owner",
        source="sec_edgar",
        classification_input_fingerprint=None,
        canonical_classification_owner_fingerprint=HASH_A,
        exact_duplicate_fingerprint=HASH_B,
        complete_output_fingerprint=HASH_C,
        policy_output_fingerprint=HASH_C,
    )
    company = _evidence(
        "company-shared-owner",
        source="company_earnings",
        classification_input_fingerprint=HASH_D,
        canonical_classification_owner_fingerprint=HASH_A,
        exact_duplicate_fingerprint=HASH_B,
        complete_output_fingerprint=HASH_D,
        policy_output_fingerprint=HASH_D,
    )
    selection = _index(
        sec,
        company,
        run=_run(event_sources=("company_earnings", "sec_edgar")),
    ).select(_context(T1))

    assert selection.selected_evidence == ()
    assert {
        value.evidence_id
        for value in selection.exclusions
        if value.reason is EvidenceExclusionReason.CLASSIFICATION_CONFLICT
    } == {"sec-shared-owner", "company-shared-owner"}


def test_exact_duplicate_collapses_once_with_only_asof_visible_lineage() -> None:
    duplicate_hash = HASH_A
    first = _evidence(
        "sec-copy",
        source="sec_edgar",
        source_record_id="sec-copy",
        system_observed_at=T1,
        evidence_ready_at=T1,
        classification_input_fingerprint=HASH_B,
        exact_duplicate_fingerprint=duplicate_hash,
        lineage_ids=("sec-observation",),
        lineage_visibility={"sec-observation": T1.isoformat()},
        marker="identical",
    )
    second = _evidence(
        "ir-copy",
        source="company_earnings",
        source_record_id="ir-copy",
        system_observed_at=T2,
        evidence_ready_at=T2,
        classification_input_fingerprint=HASH_B,
        exact_duplicate_fingerprint=duplicate_hash,
        lineage_ids=("ir-observation",),
        lineage_visibility={"ir-observation": T2.isoformat()},
        marker="identical",
    )
    run = _run(event_sources=("company_earnings", "sec_edgar"))
    index = _index(first, second, run=run)

    early = index.select(_context(T1))
    assert [value.evidence_id for value in early.selected_evidence] == ["sec-copy"]
    assert early.selected_evidence[0].lineage_ids == ("sec-observation",)
    late = index.select(_context(T2))
    assert len(late.selected_evidence) == 1
    assert set(late.selected_evidence[0].lineage_ids) == {
        "sec-observation",
        "ir-observation",
    }
    assert EvidenceExclusionReason.EXACT_DUPLICATE_COLLAPSED in {
        value.reason for value in late.exclusions
    }


def test_exact_duplicate_cross_source_observations_collapse_under_one_input_owner() -> None:
    first = _evidence(
        "sec-exact-copy",
        source="sec_edgar",
        source_record_id="sec-exact-copy",
        classification_input_fingerprint=HASH_A,
        exact_duplicate_fingerprint=HASH_C,
        lineage_ids=("sec-observation",),
        lineage_visibility={"sec-observation": T1.isoformat()},
    )
    second = _evidence(
        "ir-exact-copy",
        source="company_earnings",
        source_record_id="ir-exact-copy",
        system_observed_at=T2,
        evidence_ready_at=T2,
        classification_input_fingerprint=HASH_A,
        exact_duplicate_fingerprint=HASH_C,
        lineage_ids=("ir-observation",),
        lineage_visibility={"ir-observation": T2.isoformat()},
    )
    run = _run(event_sources=("company_earnings", "sec_edgar"))
    selection = _index(first, second, run=run).select(_context(T2))

    assert [value.evidence_id for value in selection.selected_evidence] == [
        "sec-exact-copy"
    ]
    assert set(selection.selected_evidence[0].lineage_ids) == {
        "sec-observation",
        "ir-observation",
    }
    assert any(
        value.evidence_id == "ir-exact-copy"
        and value.reason is EvidenceExclusionReason.EXACT_DUPLICATE_COLLAPSED
        for value in selection.exclusions
    )


def test_exact_content_with_distinct_input_owners_remains_separate() -> None:
    sec = _evidence(
        "sec-conflicting-copy",
        source="sec_edgar",
        classification_input_fingerprint=HASH_A,
        exact_duplicate_fingerprint=HASH_C,
        complete_output_fingerprint=HASH_A,
        policy_output_fingerprint=HASH_A,
    )
    company = _evidence(
        "ir-conflicting-copy",
        source="company_earnings",
        classification_input_fingerprint=HASH_B,
        exact_duplicate_fingerprint=HASH_C,
        complete_output_fingerprint=HASH_B,
        policy_output_fingerprint=HASH_B,
    )

    selection = _index(
        sec,
        company,
        run=_run(event_sources=("company_earnings", "sec_edgar")),
    ).select(_context(T2))

    assert {value.evidence_id for value in selection.selected_evidence} == {
        "sec-conflicting-copy",
        "ir-conflicting-copy",
    }
    assert selection.exclusions == ()


def test_related_unequal_text_remains_distinct_and_relationship_is_asof_safe() -> None:
    sec = _evidence(
        "sec-results",
        source="sec_edgar",
        source_record_id="sec-results",
        evidence_ready_at=T1,
        source_available_at=T1,
        exact_duplicate_fingerprint=HASH_A,
        marker="sec text",
    )
    company = _evidence(
        "company-results",
        source="company_earnings",
        source_record_id="company-results",
        system_observed_at=T2,
        source_available_at=T2,
        evidence_ready_at=T3,
        exact_duplicate_fingerprint=HASH_B,
        marker="richer company text",
    )
    relationship = ResearchEvidenceRelationship(
        correlation_group_id="earnings-LMT-2026-Q2",
        left_evidence_id=sec.evidence_id,
        right_evidence_id=company.evidence_id,
        relationship_type="EARNINGS_RELATED_CANDIDATE",
        correlation_version="external_correlation_v1",
        live_ready_at=T3,
        historical_ready_at=T2,
    )
    run = _run(event_sources=("company_earnings", "sec_edgar"))
    index = _index(sec, company, run=run, relationships=(relationship,))

    before_company_ready = index.select(_context(T2 + timedelta(seconds=2)))
    assert [value.evidence_id for value in before_company_ready.selected_evidence] == [
        "sec-results"
    ]
    assert before_company_ready.visible_relationships == ()
    after_company_ready = index.select(_context(T3))
    assert {value.evidence_id for value in after_company_ready.selected_evidence} == {
        "sec-results",
        "company-results",
    }
    assert after_company_ready.visible_relationships == (relationship,)


def test_sec_1601_cannot_expose_richer_company_content_until_1605() -> None:
    sec_ready = T0 + timedelta(minutes=1)
    company_ready = T0 + timedelta(minutes=5)
    sec = _evidence(
        "sec-1601",
        source="sec_edgar",
        source_record_id="sec-1601",
        source_available_at=sec_ready,
        system_observed_at=sec_ready,
        evidence_ready_at=sec_ready,
        exact_duplicate_fingerprint=HASH_A,
        marker="SEC results only",
    )
    company = _evidence(
        "company-1605",
        source="company_earnings",
        source_record_id="company-1605",
        source_available_at=company_ready,
        system_observed_at=company_ready,
        evidence_ready_at=company_ready,
        exact_duplicate_fingerprint=HASH_B,
        marker="management quote absent from SEC filing",
    )
    run = _run(event_sources=("company_earnings", "sec_edgar"))
    index = _index(sec, company, run=run)

    at_1601 = index.select(_context(sec_ready))
    assert [value.evidence_id for value in at_1601.selected_evidence] == ["sec-1601"]
    assert all(
        value.fingerprint_payload["marker"]
        != "management quote absent from SEC filing"
        for value in at_1601.selected_evidence
    )
    assert {value.evidence_id for value in index.select(_context(company_ready)).selected_evidence} == {
        "sec-1601",
        "company-1605",
    }


def test_real_combined_archives_collapse_exact_sec_and_company_input_with_visible_lineage(
    tmp_path,
) -> None:
    sec_ready = T0 + timedelta(minutes=1)
    company_ready = T0 + timedelta(minutes=5)
    document_hash = "1" * 64
    normalized_hash = "2" * 64
    excerpt_hash = "3" * 64
    sec_archive = _write_combined_sec_archive(
        tmp_path / "sec-exact",
        ready_at=sec_ready,
        document_hash=document_hash,
        normalized_text_hash=normalized_hash,
        excerpt_hash=excerpt_hash,
    )
    external_archive, profile = _write_combined_company_archive(
        tmp_path / "external-exact",
        ready_at=company_ready,
        document_hash=document_hash,
        normalized_text_hash=normalized_hash,
        excerpt_hash=excerpt_hash,
    )

    index = hydrate_combined_research_evidence(
        run_definition=_combined_archive_run(external_archive, profile),
        sec_archive=sec_archive,
        external_archive=external_archive,
    )

    assert len(index.evidence) == 2
    owners = {
        value.canonical_classification_owner_fingerprint
        for value in index.evidence
    }
    assert len(owners) == 1
    assert None not in owners
    # The source-native input fingerprints remain audit data.  The nullable
    # projection owner is what proves exact cross-source ownership.
    assert {
        value.classification_input_fingerprint for value in index.evidence
    } == {None, "6" * 64}

    at_1601 = index.select(_context(sec_ready))
    assert [value.source for value in at_1601.selected_evidence] == ["sec_edgar"]
    assert not any(
        "combined-company" in lineage
        for value in at_1601.selected_evidence
        for lineage in value.lineage_ids
    )

    at_1605 = index.select(_context(company_ready))
    assert len(at_1605.selected_evidence) == 1
    assert at_1605.selected_evidence[0].source == "sec_edgar"
    assert "raw-combined-company" in at_1605.selected_evidence[0].lineage_ids
    assert any(
        value.source == "company_earnings"
        and value.reason is EvidenceExclusionReason.EXACT_DUPLICATE_COLLAPSED
        for value in at_1605.exclusions
    )


def test_real_combined_archives_keep_richer_company_text_at_1605_not_1601(
    tmp_path,
) -> None:
    sec_ready = T0 + timedelta(minutes=1)
    company_ready = T0 + timedelta(minutes=5)
    sec_archive = _write_combined_sec_archive(
        tmp_path / "sec-related",
        ready_at=sec_ready,
        document_hash="1" * 64,
        normalized_text_hash="2" * 64,
        excerpt_hash="3" * 64,
        summary="SEC results only.",
    )
    external_archive, profile = _write_combined_company_archive(
        tmp_path / "external-related",
        ready_at=company_ready,
        document_hash="7" * 64,
        normalized_text_hash="8" * 64,
        excerpt_hash="9" * 64,
        summary="Management quote absent from the SEC filing.",
    )
    index = hydrate_combined_research_evidence(
        run_definition=_combined_archive_run(external_archive, profile),
        sec_archive=sec_archive,
        external_archive=external_archive,
    )

    at_1601 = index.select(_context(sec_ready))
    assert [value.source for value in at_1601.selected_evidence] == ["sec_edgar"]
    assert all(
        "Management quote absent"
        not in to_json_string(value.to_fingerprint_payload())
        for value in at_1601.selected_evidence
    )
    assert at_1601.visible_relationships == ()

    at_1605 = index.select(_context(company_ready))
    assert {value.source for value in at_1605.selected_evidence} == {
        "sec_edgar",
        "company_earnings",
    }
    assert len(
        {
            value.canonical_classification_owner_fingerprint
            for value in at_1605.selected_evidence
        }
    ) == 2
    assert any(
        value.relationship_type == "EARNINGS_RELATED_CANDIDATE"
        for value in at_1605.visible_relationships
    )


@pytest.mark.parametrize(
    ("source", "ticker", "official_url"),
    (
        (
            "palantir_ir",
            "PLTR",
            "https://investors.palantir.com/news-details/2026/results",
        ),
        (
            "lockheed_martin_rss",
            "LMT",
            "https://news.lockheedmartin.com/2026-results",
        ),
    ),
)
def test_ir_or_rss_and_earnings_same_url_link_without_merging_unequal_text(
    source: str,
    ticker: str,
    official_url: str,
) -> None:
    observation = replace(
        _evidence(
            f"{ticker.lower()}-index-observation",
            source=source,
            tickers=(ticker,),
            exact_duplicate_fingerprint=HASH_A,
            marker="index text revision",
        ),
        fingerprint_payload={"source_uri": official_url, "marker": "index text"},
    )
    package = replace(
        _evidence(
            f"{ticker.lower()}-earnings-observation",
            source="company_earnings",
            tickers=(ticker,),
            source_available_at=T2,
            system_observed_at=T2,
            evidence_ready_at=T3,
            exact_duplicate_fingerprint=HASH_B,
            marker="different earnings-page text revision",
        ),
        fingerprint_payload={
            "source_uri": official_url,
            "marker": "different earnings-page text",
        },
    )
    relationships = _correlate_official_company_observations(
        (observation, package),
        correlation_version="external_correlation_v1",
    )
    assert len(relationships) == 1
    assert relationships[0].relationship_type == "SAME_OFFICIAL_RELEASE_URL"
    assert relationships[0].live_ready_at == T3
    run = _run(
        event_sources=(source, "company_earnings"),
        ticker_universe=(ticker,),
    )
    index = _index(
        observation,
        package,
        run=run,
        relationships=relationships,
    )

    before_package_ready = index.select(
        _context(T2, ticker=ticker, sector="DEFENSE")
    )
    assert [
        value.evidence_id for value in before_package_ready.selected_evidence
    ] == [observation.evidence_id]
    assert before_package_ready.visible_relationships == ()
    after_package_ready = index.select(
        _context(T3, ticker=ticker, sector="DEFENSE")
    )
    assert {
        value.evidence_id for value in after_package_ready.selected_evidence
    } == {observation.evidence_id, package.evidence_id}
    assert after_package_ready.visible_relationships == relationships


def test_related_records_still_produce_one_shadow_action_and_default_no_change() -> None:
    first = _evidence("related-one", exact_duplicate_fingerprint=HASH_A)
    second = _evidence("related-two", exact_duplicate_fingerprint=HASH_B)
    selection = _index(first, second).select(_context(T1))
    signal = ModelSignal(
        signal_time=T1,
        ticker="LMT",
        signal=SignalSide.BUY,
        confidence=0.7,
        raw_score=0.2,
        model_version="model_v1",
        calibration_version="calibration_v1",
        feature_version="features_v1",
        feature_snapshot_id="feature_projection",
        signal_id="signal_projection",
        trace_id="trace_LMT",
    )
    context = _context(T1)
    policy = ShadowContextPolicy(
        policy_version="one_action_v1",
        rules=(
            ShadowContextRule(
                rule_id="one-block",
                match_keys=("AI_EVENT_TYPE:SOCIAL_POLITICAL_STATEMENT",),
                action=ShadowContextAction.BLOCK,
                reason_code="RESEARCH_ONLY_MATCH",
            ),
        ),
    )

    evaluated = evaluate_shadow_context(
        model_signal=signal,
        decision_context=context,
        evidence_selection=selection,
        policy=policy,
    )
    assert evaluated.hypothetical_action is ShadowContextAction.BLOCK
    assert set(evaluated.matched_context_event_ids) == {
        "related-one",
        "related-two",
    }
    defaulted = evaluate_shadow_context(
        model_signal=signal,
        decision_context=context,
        evidence_selection=selection,
    )
    assert defaulted.hypothetical_action is ShadowContextAction.NO_CHANGE
    assert defaulted.matched_context_event_ids == []


def _manifest_hash(payload: object) -> str:
    return sha256(to_json_string(payload).encode("utf-8")).hexdigest()


def _archive_run(
    archive: ExternalEventArchive,
    *,
    allow_incomplete_coverage: bool = False,
    availability_mode: ResearchAvailabilityMode = (
        ResearchAvailabilityMode.LIVE_SYSTEM_READY
    ),
) -> ResearchRunDefinition:
    profile = _external_profile()
    manifest = archive.load_manifest()
    resolution_manifest = archive.load_resolution_manifest()
    coverage = archive.load_coverage(SOURCE)
    assert coverage is not None
    return _run(
        availability_mode=availability_mode,
        external_profiles=(profile,),
        coverage_profiles=(
            ResearchSourceCoverageProfile(
                source=SOURCE,
                coverage_generation=coverage.coverage_generation,
                coverage_version=coverage.coverage_version,
                semantic_adapter_version=profile.semantic_adapter_version,
            ),
        ),
        allow_incomplete_coverage=allow_incomplete_coverage,
        conflict_resolution_generation=int(resolution_manifest["generation"]),
        conflict_resolution_manifest_hash=_manifest_hash(resolution_manifest),
        external_archive_generation=int(manifest["generation"]),
        external_archive_manifest_hash=_manifest_hash(manifest),
    )


def _coverage(
    *,
    status: CoverageStatus,
    source: str = SOURCE,
    intervals: tuple[CoverageInterval, ...] = (),
    gaps: tuple[CoverageInterval, ...] = (),
    live_start: datetime | None = None,
) -> SourceCoverage:
    return SourceCoverage(
        source=source,
        coverage_start=T0 - timedelta(hours=1),
        coverage_end=T0 + timedelta(hours=2),
        coverage_status=status,
        known_gaps=gaps,
        bootstrap_time=T0,
        completed_backfill_ranges=intervals,
        live_collection_start=live_start,
        last_verification_time=T0,
        coverage_generation=3,
        coverage_version="external_coverage_v1",
    )


def _revision(
    *,
    source: str,
    fact_id: str,
    ticker: str,
    source_type: str,
    adapter_version: str,
    extractor_version: str,
    normalizer_version: str,
) -> ExternalSourceRevision:
    return ExternalSourceRevision(
        source=source,
        source_fact_id=fact_id,
        source_revision_id=f"{fact_id}-r1",
        revision_sequence=1,
        supersedes_revision_id=None,
        lifecycle_state=LifecycleState.ACTIVE,
        lifecycle_effective_at=T1,
        system_observed_at=T1,
        source_available_at=T1,
        archived_at=T1,
        raw_object_hash=HASH_A,
        document_hash=HASH_B,
        normalized_text_hash=HASH_C,
        canonical_content_hash=HASH_D,
        source_type=source_type,
        affected_tickers=(ticker,),
        adapter_version=adapter_version,
        extractor_version=extractor_version,
        normalizer_version=normalizer_version,
    )


def _pinned_external_run(
    archive: ExternalEventArchive,
    *,
    event_sources: tuple[str, ...],
    profiles: tuple[ResearchSourceClassificationProfile, ...],
    coverage_profiles: tuple[ResearchSourceCoverageProfile, ...],
) -> ResearchRunDefinition:
    manifest = archive.load_manifest()
    resolution_manifest = archive.load_resolution_manifest()
    return _run(
        event_sources=event_sources,
        external_profiles=profiles,
        coverage_profiles=coverage_profiles,
        conflict_resolution_generation=int(resolution_manifest["generation"]),
        conflict_resolution_manifest_hash=_manifest_hash(resolution_manifest),
        external_archive_generation=int(manifest["generation"]),
        external_archive_manifest_hash=_manifest_hash(manifest),
    )


def _write_combined_sec_archive(
    root,
    *,
    ready_at: datetime,
    document_hash: str,
    normalized_text_hash: str,
    excerpt_hash: str,
    summary: str = "Safe fixture classification.",
) -> SECEDGARArchive:
    archive = SECEDGARArchive(root)
    profile = _classification_profile()
    accession = "0000000000-26-000001"
    attempt_id = "attempt_combined_sec"
    saved = {
        "classification_complete": True,
        "classification_request_id": "request_combined_sec",
        "classification_attempt_id": attempt_id,
        "status": "VALID",
        "event_type": "SEC_8K_RESULTS",
        "risk_level": "MEDIUM",
        "urgency": "MEDIUM",
        "confidence": 0.7,
        "summary": summary,
        "classified_at": ready_at.isoformat(),
        "evidence_ready_at": ready_at.isoformat(),
        "provider": "gemini",
        "model_version": profile.model_version,
        "prompt_version": profile.prompt_version,
        "response_schema_version": profile.response_schema_version,
        "classification_config_hash": profile.classification_config_hash,
        "accession_number": accession,
        "document_hash": document_hash,
        "full_section_hash": normalized_text_hash,
        "excerpt_hash": excerpt_hash,
        "extraction_version": profile.extraction_version,
        "item_number": "2.02",
        "ledger_row": {
            "classification_attempt_id": attempt_id,
            "classification_request_id": "request_combined_sec",
            "raw_input_id": "raw_combined_sec",
            "source_document_id": "document_combined_sec",
            "source": "sec_edgar",
            "source_type": "sec_8k_item",
            "source_platform": "sec_edgar",
            "source_uri": "https://www.sec.gov/Archives/combined-8k.htm",
            "source_locator": f"{accession}:2.02",
            "affected_tickers_json": '["LMT"]',
            "raw_input_hash": "e" * 64,
            "document_hash": document_hash,
            "source_published_at": ready_at.isoformat(),
            "source_updated_at": None,
            "collected_at": ready_at.isoformat(),
            "normalized_at": ready_at.isoformat(),
            "classified_at": ready_at.isoformat(),
            "provider": "gemini",
            "model_version": profile.model_version,
            "prompt_version": profile.prompt_version,
            "status": "VALID",
            "event_type": "SEC_8K_RESULTS",
            "risk_level": "MEDIUM",
            "urgency": "MEDIUM",
            "confidence": 0.7,
            "summary": summary,
            "validation_result_id": "validation_combined_sec",
            "validation_outcome": True,
            "validated_at": ready_at.isoformat(),
        },
    }
    archive.save_manifest(
        {
            "schema_version": 2,
            "filings": {
                accession: {
                    "form_type": "8-K",
                    "primary_document": "combined-8k.htm",
                    "official_document_identity": "combined-8k.htm",
                    "official_document_url": (
                        "https://www.sec.gov/Archives/combined-8k.htm"
                    ),
                    "document_hash": document_hash,
                    "document_extension": ".htm",
                    "collected_at": ready_at.isoformat(),
                    "classifications": {"combined": saved},
                }
            },
        }
    )
    archive.write_filing_once(
        accession,
        {
            "ticker": "LMT",
            "issuer_cik": "0000000000",
            "accession_number": accession,
            "form_type": "8-K",
            "filing_date": ready_at.date().isoformat(),
            "acceptance_at": ready_at.isoformat(),
            "primary_document": "combined-8k.htm",
            "filing_url": "https://www.sec.gov/Archives/combined-8k.htm",
            "official_document_identity": "combined-8k.htm",
            "official_document_url": "https://www.sec.gov/Archives/combined-8k.htm",
            "amendment_of": None,
            "collected_at": ready_at.isoformat(),
            "document_hash": document_hash,
        },
    )
    return archive


def _write_combined_company_archive(
    root,
    *,
    ready_at: datetime,
    document_hash: str,
    normalized_text_hash: str,
    excerpt_hash: str,
    summary: str = "Safe fixture classification.",
) -> tuple[ExternalEventArchive, ResearchSourceClassificationProfile]:
    current_time = {"value": ready_at}
    archive = ExternalEventArchive(root, now=lambda: current_time["value"])
    profile = _external_profile(
        source="company_earnings",
        source_type="OFFICIAL_EARNINGS_RELEASE",
        ticker="LMT",
        adapter_version="company_earnings_adapter_v1",
        extraction_version="lmt_earnings_html_v1",
        normalization_version="external_html_text_v1",
    )
    revision = ExternalSourceRevision(
        source="company_earnings",
        source_fact_id="LMT:2026:Q2:EARNINGS_RELEASE",
        source_revision_id="LMT:2026:Q2:EARNINGS_RELEASE-r1",
        revision_sequence=1,
        supersedes_revision_id=None,
        lifecycle_state=LifecycleState.ACTIVE,
        lifecycle_effective_at=ready_at,
        system_observed_at=ready_at,
        source_available_at=ready_at,
        archived_at=ready_at,
        normalized_at=ready_at,
        raw_object_hash="4" * 64,
        document_hash=document_hash,
        normalized_text_hash=normalized_text_hash,
        canonical_content_hash="5" * 64,
        source_type=profile.source_type,
        source_platform="lockheed_martin_ir",
        source_uri=(
            "https://investors.lockheedmartin.com/news-releases/"
            "2026-quarterly-results"
        ),
        source_published_at=ready_at,
        affected_tickers=("LMT",),
        affected_sectors=(),
        global_relevance=False,
        correlation_group_id="earnings:LMT:2026:Q2",
        relationship_types=("SAME_EARNINGS_PACKAGE",),
        earnings_package_id="LMT:2026:Q2",
        adapter_version=profile.semantic_adapter_version,
        extractor_version=profile.extraction_version,
        normalizer_version=profile.normalization_version,
    )
    archive.publish_revision(revision)
    input_fingerprint = "6" * 64
    output = {
        "status": "VALID",
        "event_type": "SEC_8K_RESULTS",
        "risk_level": "MEDIUM",
        "urgency": "MEDIUM",
        "confidence": 0.7,
        "summary": summary,
        "affected_tickers": ["LMT"],
        "affected_sectors": [],
        "global_relevance": False,
    }
    complete_hash, policy_hash = output_fingerprints(output)
    attempt_id = "attempt_combined_company"
    archive.publish_classification_attempt(
        classification_input_fingerprint=input_fingerprint,
        attempt_id=attempt_id,
        payload={
            "classification_attempt_id": attempt_id,
            "classification_request_id": "request_combined_company",
            "classification_input_fingerprint": input_fingerprint,
            "profile_hash": profile.profile_hash,
            "profile": profile.to_fingerprint_payload(),
            "document_hash": document_hash,
            "normalized_text_hash": normalized_text_hash,
            "excerpt_hash": excerpt_hash,
            "trusted_input_scope": {
                "affected_tickers": ["LMT"],
                "affected_sectors": [],
                "global_relevance": False,
            },
            "status": "VALID",
            "classified_at": ready_at.isoformat(),
            "validated_at": ready_at.isoformat(),
            "validation_outcome": True,
            "validator_version": profile.validator_version,
            "complete_output_fingerprint": complete_hash,
            "policy_output_fingerprint": policy_hash,
            "normalized_output": output,
            "durably_published": True,
            "classification_origin": "LIVE_SYSTEM",
        },
    )
    archive.claim_canonical_result(
        classification_input_fingerprint=input_fingerprint,
        attempt_id=attempt_id,
        complete_output_fingerprint=complete_hash,
        policy_output_fingerprint=policy_hash,
        profile_hash=profile.profile_hash,
        evidence_ready_at=ready_at,
    )
    event = ContextAIEvent(
        context_event_id="context-event-combined-company",
        event_time=ready_at,
        source="company_earnings",
        source_id=revision.source_fact_id,
        affected_tickers=["LMT"],
        affected_sectors=[],
        global_relevance=False,
        event_type=ContextClassificationEventType.SEC_8K_RESULTS,
        urgency=ContextUrgency.MEDIUM,
        risk_level=ContextRiskLevel.MEDIUM,
        confidence=0.7,
        summary=summary,
        prompt_version=profile.prompt_version,
        model_version=profile.model_version,
        raw_input_hash=revision.raw_object_hash,
        raw_input_id="raw-combined-company",
        source_document_id="document-combined-company",
        classification_request_id="request_combined_company",
        classification_attempt_id=attempt_id,
        validation_result_id="validation-combined-company",
        source_type=revision.source_type,
        source_platform=revision.source_platform,
        source_uri=revision.source_uri,
        source_locator=revision.source_fact_id,
        document_hash=revision.document_hash,
        source_published_at=ready_at,
        collected_at=ready_at,
        normalized_at=ready_at,
        classified_at=ready_at,
        available_at=ready_at,
        validated_at=ready_at,
        provider="gemini",
        source_available_at=ready_at,
        system_observed_at=ready_at,
        archived_at=ready_at,
        evidence_ready_at=ready_at,
        source_fact_id=revision.source_fact_id,
        source_revision_id=revision.source_revision_id,
        revision_sequence=1,
        lifecycle_state=ContextLifecycleState.ACTIVE,
        lifecycle_effective_at=ready_at,
        classification_input_fingerprint=input_fingerprint,
        complete_output_fingerprint=complete_hash,
        policy_output_fingerprint=policy_hash,
        canonical_classification_attempt_id=attempt_id,
        correlation_group_id=revision.correlation_group_id,
        relationship_types=list(revision.relationship_types),
    )
    archive.publish_materialized_event(
        source_revision_id=revision.source_revision_id,
        classification_input_fingerprint=input_fingerprint,
        payload=to_json_dict(event),
    )
    archive.publish_readiness(
        source_revision_id=revision.source_revision_id,
        classification_input_fingerprint=input_fingerprint,
        canonical_classification_attempt_id=attempt_id,
        complete_output_fingerprint=complete_hash,
        policy_output_fingerprint=policy_hash,
        profile_hash=profile.profile_hash,
        classification_profile=profile.to_fingerprint_payload(),
        classification_status="VALID",
        policy_eligible=True,
        context_event=None,
        evidence_ready_at=ready_at,
    )
    coverage_source = "company_earnings:LMT"
    archive.save_coverage(
        _coverage(
            source=coverage_source,
            status=CoverageStatus.COMPLETE_FOR_RANGE,
        )
    )
    return archive, profile


def _combined_archive_run(
    archive: ExternalEventArchive,
    profile: ResearchSourceClassificationProfile,
) -> ResearchRunDefinition:
    coverage = archive.load_coverage("company_earnings:LMT")
    assert coverage is not None
    manifest = archive.load_manifest()
    resolution_manifest = archive.load_resolution_manifest()
    return _run(
        event_sources=("company_earnings", "sec_edgar"),
        ticker_universe=("LMT",),
        external_profiles=(profile,),
        coverage_profiles=(
            ResearchSourceCoverageProfile(
                source="company_earnings",
                ticker="LMT",
                semantic_adapter_version=profile.semantic_adapter_version,
                coverage_manifest_source="company_earnings:LMT",
                coverage_generation=coverage.coverage_generation,
                coverage_version=coverage.coverage_version,
            ),
        ),
        conflict_resolution_generation=int(resolution_manifest["generation"]),
        conflict_resolution_manifest_hash=_manifest_hash(resolution_manifest),
        external_archive_generation=int(manifest["generation"]),
        external_archive_manifest_hash=_manifest_hash(manifest),
    )


def test_source_specific_earnings_profiles_select_pltr_and_lmt_extractors(
    tmp_path,
) -> None:
    source = "company_earnings"
    adapter = "company_earnings_adapter_v1"
    source_type = "OFFICIAL_EARNINGS_RELEASE"
    archive = ExternalEventArchive(tmp_path)
    archive.publish_revision(
        _revision(
            source=source,
            fact_id="PLTR:2026:Q2:EARNINGS_RELEASE",
            ticker="PLTR",
            source_type=source_type,
            adapter_version=adapter,
            extractor_version="palantir_ir_json_v1",
            normalizer_version="external_html_text_v1",
        )
    )
    archive.publish_revision(
        _revision(
            source=source,
            fact_id="LMT:2026:Q2:EARNINGS_RELEASE",
            ticker="LMT",
            source_type=source_type,
            adapter_version=adapter,
            extractor_version="external_pdf_text_v2_bounded",
            normalizer_version="external_pdf_text_v2_bounded",
        )
    )
    profiles = (
        _external_profile(
            source=source,
            source_type=source_type,
            ticker="PLTR",
            adapter_version=adapter,
            extraction_version="palantir_ir_json_v1",
            normalization_version="external_html_text_v1",
        ),
        _external_profile(
            source=source,
            source_type=source_type,
            ticker="LMT",
            adapter_version=adapter,
            extraction_version="external_pdf_text_v2_bounded",
            normalization_version="external_pdf_text_v2_bounded",
        ),
        _external_profile(
            source=source,
            source_type=source_type,
            ticker="LMT",
            adapter_version=adapter,
            extraction_version="lmt_earnings_html_v1",
            normalization_version="external_html_text_v1",
        ),
    )
    coverage_profiles: list[ResearchSourceCoverageProfile] = []
    for ticker in ("LMT", "PLTR"):
        manifest_source = f"{source}:{ticker}"
        archive.save_coverage(
            _coverage(
                source=manifest_source,
                status=CoverageStatus.COMPLETE_FOR_RANGE,
            )
        )
        coverage_profiles.append(
            ResearchSourceCoverageProfile(
                source=source,
                ticker=ticker,
                semantic_adapter_version=adapter,
                coverage_manifest_source=manifest_source,
                coverage_generation=3,
                coverage_version="external_coverage_v1",
            )
        )
    run = _pinned_external_run(
        archive,
        event_sources=(source,),
        profiles=profiles,
        coverage_profiles=tuple(coverage_profiles),
    )

    index = hydrate_external_research_evidence(
        archive=archive,
        run_definition=run,
    )
    assert {value.source_revision_id for value in index.lifecycle_revisions} == {
        "PLTR:2026:Q2:EARNINGS_RELEASE-r1",
        "LMT:2026:Q2:EARNINGS_RELEASE-r1",
    }
    assert {
        (value.ticker, value.semantic_adapter_version)
        for value in index.coverage_assessments
    } == {("LMT", adapter), ("PLTR", adapter)}


def test_admitted_source_or_revision_without_exact_profile_fails_closed(
    tmp_path,
) -> None:
    archive = ExternalEventArchive(tmp_path / "missing-source")
    archive.save_coverage(_coverage(status=CoverageStatus.COMPLETE_FOR_RANGE))
    with pytest.raises(
        ResearchProjectionError,
        match="every admitted external source",
    ):
        hydrate_external_research_evidence(
            archive=archive,
            run_definition=_run(
                event_sources=(SOURCE, "palantir_ir"),
                external_profiles=(_external_profile(),),
                coverage_profiles=(
                    ResearchSourceCoverageProfile(
                        source=SOURCE,
                        semantic_adapter_version="adapter_v1",
                        coverage_generation=3,
                        coverage_version="external_coverage_v1",
                    ),
                ),
                conflict_resolution_generation=0,
                conflict_resolution_manifest_hash=_manifest_hash(
                    archive.load_resolution_manifest()
                ),
                external_archive_generation=0,
                external_archive_manifest_hash=_manifest_hash(
                    archive.load_manifest()
                ),
            ),
        )

    mismatched = ExternalEventArchive(tmp_path / "missing-revision")
    mismatched.publish_revision(
        _revision(
            source=SOURCE,
            fact_id="truth-unprofiled",
            ticker="LMT",
            source_type="changed_source_schema",
            adapter_version="adapter_v1",
            extractor_version="extractor_v1",
            normalizer_version="normalizer_v1",
        )
    )
    mismatched.save_coverage(
        _coverage(status=CoverageStatus.COMPLETE_FOR_RANGE)
    )
    with pytest.raises(
        ResearchProjectionError,
        match="does not have exactly one pinned",
    ):
        hydrate_external_research_evidence(
            archive=mismatched,
            run_definition=_archive_run(mismatched),
        )


def test_live_only_or_partial_gap_coverage_fails_closed(tmp_path) -> None:
    live_only = ExternalEventArchive(tmp_path / "live-only")
    live_only.save_coverage(
        _coverage(status=CoverageStatus.LIVE_ONLY, live_start=T0)
    )
    with pytest.raises(ResearchProjectionError, match="coverage is incomplete"):
        hydrate_external_research_evidence(
            archive=live_only,
            run_definition=_archive_run(live_only),
        )

    gap = CoverageInterval(
        start=T0 - timedelta(minutes=5),
        end=T0 + timedelta(minutes=5),
    )
    partial = ExternalEventArchive(tmp_path / "partial")
    partial.save_coverage(
        _coverage(
            status=CoverageStatus.PARTIAL,
            intervals=(
                CoverageInterval(
                    start=T0 - timedelta(hours=1),
                    end=T0 + timedelta(hours=2),
                ),
            ),
            gaps=(gap,),
        )
    )
    with pytest.raises(ResearchProjectionError, match="coverage is incomplete"):
        hydrate_external_research_evidence(
            archive=partial,
            run_definition=_archive_run(partial),
        )


def test_recovered_coverage_publication_invalidates_older_run_pin(
    tmp_path,
    monkeypatch,
) -> None:
    archive = ExternalEventArchive(tmp_path, now=lambda: T0)
    original = _coverage(status=CoverageStatus.COMPLETE_FOR_RANGE)
    archive.save_coverage(original)
    pinned_run = _archive_run(archive)
    register = archive._register_mutable_artifact

    def fail_before_manifest_registration(*_args, **_kwargs) -> None:
        raise ExternalEventArchiveError("simulated manifest interruption")

    monkeypatch.setattr(
        archive,
        "_register_mutable_artifact",
        fail_before_manifest_registration,
    )
    with pytest.raises(ExternalEventArchiveError, match="manifest interruption"):
        archive.save_coverage(
            replace(
                original,
                coverage_generation=original.coverage_generation + 1,
                last_verification_time=T0 + timedelta(seconds=1),
            )
        )
    monkeypatch.setattr(archive, "_register_mutable_artifact", register)

    with pytest.raises(
        ResearchProjectionError,
        match="external archive generation does not match the run",
    ):
        hydrate_external_research_evidence(
            archive=archive,
            run_definition=pinned_run,
        )

    recovered_run = _archive_run(archive)
    recovered = hydrate_external_research_evidence(
        archive=archive,
        run_definition=recovered_run,
    )
    assert recovered.evidence == ()
    assert recovered.coverage_assessments[0].complete is True


def test_malformed_recovered_coverage_fails_at_projection_boundary(
    tmp_path,
) -> None:
    archive = ExternalEventArchive(tmp_path, now=lambda: T0)
    archive._atomic_replace(
        archive.coverage_dir / f"{SOURCE}.json",
        b"{}\n",
    )
    run = _run(
        external_profiles=(_external_profile(),),
        coverage_profiles=(
            ResearchSourceCoverageProfile(
                source=SOURCE,
                semantic_adapter_version="adapter_v1",
                coverage_generation=0,
                coverage_version="external_coverage_v1",
            ),
        ),
        conflict_resolution_generation=0,
        conflict_resolution_manifest_hash=_manifest_hash(
            archive.load_resolution_manifest()
        ),
        external_archive_generation=0,
        external_archive_manifest_hash=_manifest_hash(archive.load_manifest()),
    )

    with pytest.raises(
        ResearchProjectionError,
        match="external artifact reconciliation failed",
    ):
        hydrate_external_research_evidence(
            archive=archive,
            run_definition=run,
        )


def test_live_only_status_cannot_claim_historical_backfill_coverage(
    tmp_path,
) -> None:
    archive = ExternalEventArchive(tmp_path, now=lambda: T0)
    archive.save_coverage(
        _coverage(
            status=CoverageStatus.LIVE_ONLY,
            intervals=(
                CoverageInterval(
                    start=T0 - timedelta(hours=1),
                    end=T0 + timedelta(hours=2),
                ),
            ),
            live_start=T0 + timedelta(hours=1),
        )
    )

    with pytest.raises(ResearchProjectionError, match="coverage is incomplete"):
        hydrate_external_research_evidence(
            archive=archive,
            run_definition=_archive_run(
                archive,
                availability_mode=ResearchAvailabilityMode.HISTORICAL_SOURCE_TIME,
            ),
        )


def test_final_archive_pin_recheck_rejects_mid_hydration_coverage_change(
    tmp_path,
    monkeypatch,
) -> None:
    archive = ExternalEventArchive(tmp_path, now=lambda: T0)
    original = _coverage(status=CoverageStatus.COMPLETE_FOR_RANGE)
    archive.save_coverage(original)
    run = _archive_run(archive)

    def mutate_coverage(*, sources):
        assert sources == {SOURCE}
        archive.save_coverage(
            replace(
                original,
                coverage_generation=original.coverage_generation + 1,
                last_verification_time=T0 + timedelta(seconds=1),
            )
        )
        return ()

    monkeypatch.setattr(archive, "iter_revisions", mutate_coverage)

    with pytest.raises(
        ResearchProjectionError,
        match="external archive generation does not match the run",
    ):
        hydrate_external_research_evidence(
            archive=archive,
            run_definition=run,
        )


def test_complete_bounded_coverage_succeeds_and_override_changes_fingerprint(tmp_path) -> None:
    complete = ExternalEventArchive(tmp_path / "complete")
    complete.save_coverage(
        _coverage(status=CoverageStatus.COMPLETE_FOR_RANGE)
    )
    complete_run = _archive_run(complete)
    index = hydrate_external_research_evidence(
        archive=complete,
        run_definition=complete_run,
    )
    assert index.evidence == ()
    assert index.coverage_assessments[0].complete is True

    partial = ExternalEventArchive(tmp_path / "allowed-partial")
    partial.save_coverage(
        _coverage(status=CoverageStatus.LIVE_ONLY, live_start=T0)
    )
    strict = _archive_run(partial)
    allowed = _archive_run(partial, allow_incomplete_coverage=True)
    assert strict.to_fingerprint_payload() != allowed.to_fingerprint_payload()
    allowed_index = hydrate_external_research_evidence(
        archive=partial,
        run_definition=allowed,
    )
    assert allowed_index.coverage_assessments[0].complete is False


def test_historical_source_time_requires_complete_coverage_and_changes_selection(
    tmp_path,
) -> None:
    incomplete = ExternalEventArchive(tmp_path / "historical-incomplete")
    incomplete.save_coverage(
        _coverage(status=CoverageStatus.LIVE_ONLY, live_start=T2)
    )
    with pytest.raises(ResearchProjectionError, match="coverage is incomplete"):
        hydrate_external_research_evidence(
            archive=incomplete,
            run_definition=_archive_run(
                incomplete,
                availability_mode=ResearchAvailabilityMode.HISTORICAL_SOURCE_TIME,
            ),
        )

    complete = ExternalEventArchive(tmp_path / "historical-complete")
    complete.save_coverage(_coverage(status=CoverageStatus.COMPLETE_FOR_RANGE))
    historical_run = _archive_run(
        complete,
        availability_mode=ResearchAvailabilityMode.HISTORICAL_SOURCE_TIME,
    )
    assert hydrate_external_research_evidence(
        archive=complete,
        run_definition=historical_run,
    ).coverage_assessments[0].complete is True

    delayed = _evidence(
        "historical-source-time",
        source_available_at=T1,
        system_observed_at=T2,
        evidence_ready_at=T2,
    )
    live_run = _run(availability_mode=ResearchAvailabilityMode.LIVE_SYSTEM_READY)
    source_time_run = _run(
        availability_mode=ResearchAvailabilityMode.HISTORICAL_SOURCE_TIME
    )
    evaluation = _context(T1 + timedelta(seconds=2))
    live_selection = _index(delayed, run=live_run).select(evaluation)
    historical_selection = _index(delayed, run=source_time_run).select(evaluation)

    assert live_selection.selected_evidence == ()
    assert [value.evidence_id for value in historical_selection.selected_evidence] == [
        "historical-source-time"
    ]
    assert live_run.to_fingerprint_payload() != source_time_run.to_fingerprint_payload()


def test_pinned_run_rejects_later_classification_archive_publication(tmp_path) -> None:
    archive = ExternalEventArchive(tmp_path)
    archive.save_coverage(_coverage(status=CoverageStatus.COMPLETE_FOR_RANGE))
    pinned = _archive_run(archive)
    archive.publish_classification_attempt(
        classification_input_fingerprint="8" * 64,
        attempt_id="later-attempt",
        payload={
            "classification_attempt_id": "later-attempt",
            "durably_published": True,
            "validation_outcome": True,
            "complete_output_fingerprint": "9" * 64,
            "policy_output_fingerprint": "9" * 64,
            "profile_hash": _external_profile().profile_hash,
        },
    )

    with pytest.raises(
        ResearchProjectionError,
        match="external archive generation does not match the run",
    ):
        hydrate_external_research_evidence(
            archive=archive,
            run_definition=pinned,
        )


@pytest.mark.parametrize(
    ("readiness_offset_seconds", "expected_evidence_count"),
    ((1, 0), (5, 1)),
)
def test_hydration_enforces_complete_readiness_chronology(
    tmp_path,
    readiness_offset_seconds: int,
    expected_evidence_count: int,
) -> None:
    current_time = {"value": T1}
    archive = ExternalEventArchive(
        tmp_path,
        now=lambda: current_time["value"],
    )
    profile = _external_profile()
    revision = _revision(
        source=SOURCE,
        fact_id="truth-invalid-readiness",
        ticker="LMT",
        source_type="social_post",
        adapter_version=profile.semantic_adapter_version,
        extractor_version=profile.extraction_version,
        normalizer_version=profile.normalization_version,
    )
    archive.publish_revision(revision)
    input_fingerprint = "4" * 64
    complete_output = "5" * 64
    policy_output = "6" * 64
    classified_at = T1 + timedelta(seconds=1)
    validated_at = T1 + timedelta(seconds=2)
    current_time["value"] = T1 + timedelta(seconds=3)
    archive.publish_classification_attempt(
        classification_input_fingerprint=input_fingerprint,
        attempt_id="canonical-attempt",
        payload={
            "classification_attempt_id": "canonical-attempt",
            "classification_request_id": "classification-request",
            "classification_input_fingerprint": input_fingerprint,
            "profile_hash": profile.profile_hash,
            "profile": profile.to_fingerprint_payload(),
            "document_hash": revision.document_hash,
            "normalized_text_hash": revision.normalized_text_hash,
            "excerpt_hash": "7" * 64,
            "status": "VALID",
            "classified_at": classified_at.isoformat(),
            "validated_at": validated_at.isoformat(),
            "validation_outcome": True,
            "validator_version": profile.validator_version,
            "complete_output_fingerprint": complete_output,
            "policy_output_fingerprint": policy_output,
            "normalized_output": {"event_type": "SOCIAL_POLITICAL_STATEMENT"},
            "durably_published": True,
        },
    )
    current_time["value"] = T1 + timedelta(seconds=4)
    archive.claim_canonical_result(
        classification_input_fingerprint=input_fingerprint,
        attempt_id="canonical-attempt",
        complete_output_fingerprint=complete_output,
        policy_output_fingerprint=policy_output,
        profile_hash=profile.profile_hash,
        evidence_ready_at=current_time["value"],
    )
    event = ContextAIEvent(
        context_event_id="context-event-invalid-readiness",
        event_time=T1,
        source=SOURCE,
        source_id=revision.source_fact_id,
        affected_tickers=["LMT"],
        affected_sectors=["DEFENSE"],
        global_relevance=False,
        event_type=ContextClassificationEventType.SOCIAL_POLITICAL_STATEMENT,
        urgency=ContextUrgency.MEDIUM,
        risk_level=ContextRiskLevel.MEDIUM,
        confidence=0.8,
        summary="Synthetic chronology fixture.",
        prompt_version=profile.prompt_version,
        model_version=profile.model_version,
        raw_input_hash=revision.raw_object_hash,
        raw_input_id="raw-input",
        source_document_id="source-document",
        classification_request_id="classification-request",
        classification_attempt_id="canonical-attempt",
        validation_result_id="validation-result",
        source_type=revision.source_type,
        source_platform="truth_social",
        source_locator=revision.source_fact_id,
        document_hash=revision.document_hash,
        collected_at=revision.system_observed_at,
        normalized_at=revision.archived_at,
        classified_at=classified_at,
        available_at=revision.source_available_at,
        validated_at=validated_at,
        provider="gemini",
        source_available_at=revision.source_available_at,
        system_observed_at=revision.system_observed_at,
        archived_at=revision.archived_at,
        source_fact_id=revision.source_fact_id,
        source_revision_id=revision.source_revision_id,
        revision_sequence=revision.revision_sequence,
        lifecycle_state=ContextLifecycleState.ACTIVE,
        lifecycle_effective_at=revision.lifecycle_effective_at,
        classification_input_fingerprint=input_fingerprint,
        complete_output_fingerprint=complete_output,
        policy_output_fingerprint=policy_output,
        canonical_classification_attempt_id="canonical-attempt",
    )
    archive.publish_materialized_event(
        source_revision_id=revision.source_revision_id,
        classification_input_fingerprint=input_fingerprint,
        payload=to_json_dict(event),
    )
    # The one-second case simulates a corrupt/non-monotonic publisher.  The
    # five-second case proves the same complete lineage hydrates when ordered.
    current_time["value"] = T1 + timedelta(
        seconds=readiness_offset_seconds
    )
    archive.publish_readiness(
        source_revision_id=revision.source_revision_id,
        classification_input_fingerprint=input_fingerprint,
        canonical_classification_attempt_id="canonical-attempt",
        complete_output_fingerprint=complete_output,
        policy_output_fingerprint=policy_output,
        profile_hash=profile.profile_hash,
        classification_profile=profile.to_fingerprint_payload(),
        classification_status="VALID",
        policy_eligible=True,
        context_event=None,
        evidence_ready_at=current_time["value"],
    )
    current_time["value"] = T1 + timedelta(seconds=6)
    archive.save_coverage(_coverage(status=CoverageStatus.COMPLETE_FOR_RANGE))

    index = hydrate_external_research_evidence(
        archive=archive,
        run_definition=_archive_run(archive),
    )
    assert len(index.evidence) == expected_evidence_count
    if expected_evidence_count == 0:
        assert [value.reason for value in index.hydration_exclusions] == [
            EvidenceExclusionReason.MALFORMED
        ]
        assert index.hydration_exclusions[0].safe_detail == (
            "materialized event lineage validation failed"
        )
    else:
        assert index.hydration_exclusions == ()


def _publish_conflicting_revision(
    archive: ExternalEventArchive,
    *,
    set_time=None,
) -> None:
    profile = _external_profile()
    revision = ExternalSourceRevision(
        source=SOURCE,
        source_fact_id="truth-conflict",
        source_revision_id="truth-conflict-r1",
        revision_sequence=1,
        supersedes_revision_id=None,
        lifecycle_state=LifecycleState.ACTIVE,
        lifecycle_effective_at=T1,
        system_observed_at=T1,
        source_available_at=T1,
        archived_at=T1,
        raw_object_hash=HASH_A,
        document_hash=HASH_B,
        normalized_text_hash=HASH_C,
        canonical_content_hash=HASH_D,
        source_type="social_post",
        source_platform="truth_social",
        affected_tickers=("LMT",),
        adapter_version="adapter_v1",
        extractor_version="extractor_v1",
        normalizer_version="normalizer_v1",
    )
    archive.publish_revision(revision)
    input_fingerprint = "1" * 64
    if set_time is not None:
        set_time(T1 + timedelta(seconds=3))
    archive.publish_classification_attempt(
        classification_input_fingerprint=input_fingerprint,
        attempt_id="live-attempt",
        payload={
            "classification_attempt_id": "live-attempt",
            "classification_request_id": "classification-request-conflict",
            "classification_input_fingerprint": input_fingerprint,
            "durably_published": True,
            "validation_outcome": True,
            "complete_output_fingerprint": "2" * 64,
            "policy_output_fingerprint": "2" * 64,
            "profile_hash": profile.profile_hash,
            "profile": profile.to_fingerprint_payload(),
            "document_hash": revision.document_hash,
            "normalized_text_hash": revision.normalized_text_hash,
            "excerpt_hash": "4" * 64,
            "classified_at": (T1 + timedelta(seconds=1)).isoformat(),
            "validated_at": (T1 + timedelta(seconds=2)).isoformat(),
            "classification_origin": "LIVE_SYSTEM",
            "normalized_output": {"risk_level": "LOW"},
        },
    )
    if set_time is not None:
        set_time(T1 + timedelta(seconds=4))
    archive.claim_canonical_result(
        classification_input_fingerprint=input_fingerprint,
        attempt_id="live-attempt",
        complete_output_fingerprint="2" * 64,
        policy_output_fingerprint="2" * 64,
        profile_hash=profile.profile_hash,
        evidence_ready_at=T1 + timedelta(seconds=7),
    )
    event = ContextAIEvent(
        context_event_id="context-event-conflict",
        event_time=T1,
        source=SOURCE,
        source_id=revision.source_fact_id,
        affected_tickers=["LMT"],
        affected_sectors=["DEFENSE"],
        global_relevance=False,
        event_type=ContextClassificationEventType.SOCIAL_POLITICAL_STATEMENT,
        urgency=ContextUrgency.MEDIUM,
        risk_level=ContextRiskLevel.LOW,
        confidence=0.8,
        summary="Canonical live classification.",
        prompt_version=profile.prompt_version,
        model_version=profile.model_version,
        raw_input_hash=revision.raw_object_hash,
        raw_input_id="raw-input-conflict",
        source_document_id="source-document-conflict",
        classification_request_id="classification-request-conflict",
        classification_attempt_id="live-attempt",
        validation_result_id="validation-result-conflict",
        source_type=revision.source_type,
        source_platform="truth_social",
        source_locator=revision.source_fact_id,
        document_hash=revision.document_hash,
        collected_at=revision.system_observed_at,
        normalized_at=revision.archived_at,
        classified_at=T1 + timedelta(seconds=1),
        available_at=revision.source_available_at,
        validated_at=T1 + timedelta(seconds=2),
        provider="gemini",
        source_available_at=revision.source_available_at,
        system_observed_at=revision.system_observed_at,
        archived_at=revision.archived_at,
        evidence_ready_at=T1 + timedelta(seconds=7),
        source_fact_id=revision.source_fact_id,
        source_revision_id=revision.source_revision_id,
        revision_sequence=revision.revision_sequence,
        lifecycle_state=ContextLifecycleState.ACTIVE,
        lifecycle_effective_at=revision.lifecycle_effective_at,
        classification_input_fingerprint=input_fingerprint,
        complete_output_fingerprint="2" * 64,
        policy_output_fingerprint="2" * 64,
        canonical_classification_attempt_id="live-attempt",
    )
    if set_time is not None:
        set_time(T1 + timedelta(seconds=5))
    archive.publish_materialized_event(
        source_revision_id=revision.source_revision_id,
        classification_input_fingerprint=input_fingerprint,
        payload=to_json_dict(event),
    )
    if set_time is not None:
        set_time(T1 + timedelta(seconds=6))
    archive.publish_readiness(
        source_revision_id=revision.source_revision_id,
        classification_input_fingerprint=input_fingerprint,
        canonical_classification_attempt_id="live-attempt",
        complete_output_fingerprint="2" * 64,
        policy_output_fingerprint="2" * 64,
        profile_hash=profile.profile_hash,
        classification_profile=profile.to_fingerprint_payload(),
        classification_status="VALID",
        policy_eligible=True,
        context_event=None,
        evidence_ready_at=T1 + timedelta(seconds=7),
    )
    if set_time is not None:
        set_time(T2)
    archive.publish_classification_attempt(
        classification_input_fingerprint=input_fingerprint,
        attempt_id="backfill-attempt",
        payload={
            "classification_attempt_id": "backfill-attempt",
            "classification_input_fingerprint": input_fingerprint,
            "durably_published": True,
            "validation_outcome": True,
            "complete_output_fingerprint": "3" * 64,
            "policy_output_fingerprint": "3" * 64,
            "profile_hash": profile.profile_hash,
            "profile": profile.to_fingerprint_payload(),
            "document_hash": revision.document_hash,
            "normalized_text_hash": revision.normalized_text_hash,
            "excerpt_hash": "4" * 64,
            "classified_at": T2.isoformat(),
            "validated_at": T2.isoformat(),
            "classification_origin": "BACKFILL",
            "normalized_output": {"risk_level": "HIGH"},
        },
    )


def test_unresolved_classification_conflict_blocks_preparation(tmp_path) -> None:
    archive = ExternalEventArchive(tmp_path, now=lambda: T3)
    _publish_conflicting_revision(archive)
    archive.save_coverage(_coverage(status=CoverageStatus.COMPLETE_FOR_RANGE))

    with pytest.raises(
        ResearchProjectionError,
        match="unresolved classification conflict blocks preparation",
    ):
        hydrate_external_research_evidence(
            archive=archive,
            run_definition=_archive_run(archive),
        )


def test_resolution_orphan_adoption_invalidates_an_older_run_pin(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = ExternalEventArchive(tmp_path, now=lambda: T3)
    _publish_conflicting_revision(archive)
    archive.save_coverage(
        _coverage(status=CoverageStatus.COMPLETE_FOR_RANGE)
    )
    conflict = archive.detect_classification_conflict("1" * 64)
    assert conflict is not None

    def crash_manifest_save(_manifest: object) -> None:
        raise ExternalEventArchiveError(
            "simulated crash before resolution manifest save"
        )

    monkeypatch.setattr(
        archive, "save_resolution_manifest", crash_manifest_save
    )
    with pytest.raises(
        ExternalEventArchiveError,
        match="simulated crash before resolution manifest save",
    ):
        archive.publish_conflict_resolution(
            conflict_id=str(conflict["classification_conflict_id"]),
            decision=ConflictResolutionDecision.ABSTAIN_INPUT,
            reviewer="fixture-reviewer",
            reason="Ownership is intentionally abstained.",
        )
    stale_run = _archive_run(archive)

    with pytest.raises(
        ResearchProjectionError,
        match="classification-resolution generation does not match the run",
    ):
        hydrate_external_research_evidence(
            archive=ExternalEventArchive(tmp_path, now=lambda: T3),
            run_definition=stale_run,
        )


def test_abstain_input_resolution_admits_neither_conflicting_result(tmp_path) -> None:
    archive = ExternalEventArchive(tmp_path, now=lambda: T3)
    _publish_conflicting_revision(archive)
    conflict = archive.detect_classification_conflict("1" * 64)
    assert conflict is not None
    archive.publish_conflict_resolution(
        conflict_id=str(conflict["classification_conflict_id"]),
        decision=ConflictResolutionDecision.ABSTAIN_INPUT,
        reviewer="fixture-reviewer",
        reason="Canonical live ownership cannot be proven by this fixture.",
    )
    archive.save_coverage(_coverage(status=CoverageStatus.COMPLETE_FOR_RANGE))

    index = hydrate_external_research_evidence(
        archive=archive,
        run_definition=_archive_run(archive),
    )

    assert index.evidence == ()
    assert [value.reason for value in index.hydration_exclusions] == [
        EvidenceExclusionReason.CLASSIFICATION_CONFLICT
    ]


def test_keep_first_resolution_preserves_proven_live_result_and_readiness(
    tmp_path,
) -> None:
    current_time = {"value": T1}
    archive = ExternalEventArchive(
        tmp_path,
        now=lambda: current_time["value"],
    )

    def set_time(value: datetime) -> None:
        current_time["value"] = value

    _publish_conflicting_revision(archive, set_time=set_time)
    conflict = archive.detect_classification_conflict("1" * 64)
    assert conflict is not None
    archive.publish_conflict_resolution(
        conflict_id=str(conflict["classification_conflict_id"]),
        decision=ConflictResolutionDecision.KEEP_FIRST_DURABLY_PUBLISHED,
        reviewer="fixture-reviewer",
        reason="The immutable archive proves live ownership before backfill.",
        chosen_attempt_id="live-attempt",
        chosen_complete_output_fingerprint="2" * 64,
        chosen_policy_output_fingerprint="2" * 64,
    )
    archive.save_coverage(_coverage(status=CoverageStatus.COMPLETE_FOR_RANGE))

    index = hydrate_external_research_evidence(
        archive=archive,
        run_definition=_archive_run(archive),
    )

    assert len(index.evidence) == 1
    evidence = index.evidence[0]
    assert evidence.evidence_ready_at == T1 + timedelta(seconds=7)
    assert evidence.complete_output_fingerprint == "2" * 64
    assert evidence.conflict_resolution_id is not None
    assert index.hydration_exclusions == ()


def test_reclassify_resolution_requires_the_reviewed_new_profile(tmp_path) -> None:
    archive = ExternalEventArchive(tmp_path, now=lambda: T3)
    _publish_conflicting_revision(archive)
    archive.save_coverage(_coverage(status=CoverageStatus.COMPLETE_FOR_RANGE))
    conflict = archive.detect_classification_conflict("1" * 64)
    assert conflict is not None
    old_profile = _external_profile()
    new_profile = replace(old_profile, prompt_version="context_filter_v3_reviewed")
    archive.publish_conflict_resolution(
        conflict_id=str(conflict["classification_conflict_id"]),
        decision=ConflictResolutionDecision.RECLASSIFY_UNDER_NEW_PROFILE,
        reviewer="fixture-reviewer",
        reason="The prior profile did not pin enough semantics.",
        new_profile_hash=new_profile.profile_hash,
    )

    with pytest.raises(
        ResearchProjectionError,
        match="requires the run to pin its reviewed new profile",
    ):
        hydrate_external_research_evidence(
            archive=archive,
            run_definition=_archive_run(archive),
        )

    coverage = archive.load_coverage(SOURCE)
    assert coverage is not None
    new_run = _pinned_external_run(
        archive,
        event_sources=(SOURCE,),
        profiles=(new_profile,),
        coverage_profiles=(
            ResearchSourceCoverageProfile(
                source=SOURCE,
                semantic_adapter_version=new_profile.semantic_adapter_version,
                coverage_generation=coverage.coverage_generation,
                coverage_version=coverage.coverage_version,
            ),
        ),
    )
    reclassified = hydrate_external_research_evidence(
        archive=archive,
        run_definition=new_run,
    )
    assert reclassified.evidence == ()
    assert len(reclassified.lifecycle_revisions) == 1


def test_conflict_resolution_generation_changes_research_fingerprint() -> None:
    profile = _external_profile()
    coverage = (
        ResearchSourceCoverageProfile(
            source=SOURCE,
            coverage_generation=3,
            coverage_version="external_coverage_v1",
            semantic_adapter_version=profile.semantic_adapter_version,
        ),
    )
    first = _run(
        external_profiles=(profile,),
        coverage_profiles=coverage,
        conflict_resolution_generation=1,
        conflict_resolution_manifest_hash="1" * 64,
        external_archive_generation=4,
        external_archive_manifest_hash="4" * 64,
    )
    second = _run(
        external_profiles=(profile,),
        coverage_profiles=coverage,
        conflict_resolution_generation=2,
        conflict_resolution_manifest_hash="2" * 64,
        external_archive_generation=4,
        external_archive_manifest_hash="4" * 64,
    )
    evidence = _evidence("resolution-sensitive")
    first_selection = _index(evidence, run=first).select(_context(T1))
    second_selection = _index(evidence, run=second).select(_context(T1))

    assert build_shadow_context_fingerprint(
        decision_context=_context(T1),
        evidence_selection=first_selection,
    ) != build_shadow_context_fingerprint(
        decision_context=_context(T1),
        evidence_selection=second_selection,
    )


def test_capacity_failure_does_not_mutate_an_already_published_index() -> None:
    published = _index(_evidence("published"), run=_run(capacity=1))
    with pytest.raises(ResearchEvidenceCapacityError):
        _index(
            _evidence("one"),
            _evidence("two"),
            run=_run(capacity=1),
        )

    assert _selected_ids(published, T1) == ["published"]


def test_legacy_sec_only_payload_selection_and_fingerprint_are_stable() -> None:
    legacy_run = ResearchRunDefinition(
        ticker_universe=("LMT",),
        event_sources=("sec_edgar",),
        evidence_categories=(EvidenceCategory.AI_EVENT,),
        hydration_start_time=T0 - timedelta(hours=1),
        hydration_end_time=T0 + timedelta(hours=1),
        capacity=10,
        classification_profile=_classification_profile(),
        max_age_without_valid_until=timedelta(minutes=30),
        selection_policy_version="research_selection_v1",
    )
    legacy = ResearchEvidence(
        evidence_id="legacy-sec-event",
        category=EvidenceCategory.AI_EVENT,
        policy_match_key="AI_EVENT_TYPE:SEC_8K_RESULTS",
        source="sec_edgar",
        source_record_id="0000000000-26-000001:2.02",
        tickers=("LMT",),
        available_at=T1,
        fingerprint_payload={"legacy": True},
        lineage_ids=("sec-lineage",),
    )
    context = _context(T1)
    selection = _index(legacy, run=legacy_run).select(context)

    assert [value.evidence_id for value in selection.selected_evidence] == [
        "legacy-sec-event"
    ]
    assert legacy_run.to_fingerprint_payload() == {
        "ticker_universe": ["LMT"],
        "event_sources": ["sec_edgar"],
        "evidence_categories": ["AI_EVENT"],
        "hydration_start_time": "2026-07-18T15:00:00Z",
        "hydration_end_time": "2026-07-18T17:00:00Z",
        "classification_profile": {
            "extraction_version": "sec_8k_items_v1",
            "prompt_version": "context_filter_v1",
            "model_version": "gemini-test",
            "response_schema_version": "context_classification_response_v1",
            "classification_config_hash": HASH_A,
        },
        "max_age_without_valid_until_seconds": 1800.0,
        "selection_policy_version": "research_selection_v1",
    }
    assert build_shadow_context_fingerprint(
        decision_context=context,
        evidence_selection=selection,
    ) == "f643af6b38c5186686849c55ee8929512feb7dbda1dbc5e93ba3060df008332a"
