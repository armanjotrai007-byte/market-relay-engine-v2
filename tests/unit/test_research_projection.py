from __future__ import annotations

import builtins
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import socket

import pytest

from market_relay_engine.common.serialization import to_json_string
from market_relay_engine.context.decision_context import DecisionContextAssembler
from market_relay_engine.context.research_projection import (
    EvidenceCategory,
    EvidenceExclusionReason,
    ResearchClassificationProfile,
    ResearchEvidence,
    ResearchEvidenceCapacityError,
    ResearchEvidenceExclusion,
    ResearchEvidenceIndex,
    ResearchProjectionError,
    ResearchRunDefinition,
    build_shadow_context_fingerprint,
    hydrate_sec_research_evidence,
    normalize_context_flag,
    normalize_form4_research_event,
)
from market_relay_engine.context.sec_edgar import (
    Form4ReportingOwner,
    Form4ResearchEvent,
)
from market_relay_engine.context.sec_edgar_archive import SECEDGARArchive
from market_relay_engine.context.shadow_evaluation import (
    ShadowContextPolicy,
    ShadowContextRule,
    ShadowEvaluationError,
    evaluate_shadow_context,
)
from market_relay_engine.context.state_cache import (
    ContextStateCache,
    make_global_context_entry,
)
from market_relay_engine.contracts.context import (
    ContextFlag,
    DeterministicContextEventType,
    ShadowContextAction,
)
from market_relay_engine.contracts.model import ModelSignal, SignalSide
from market_relay_engine.contracts.risk import RiskDecision, RiskDecisionType
from market_relay_engine.questdb.writer import (
    QuestDBLedgerWriter,
    shadow_context_policy_evaluation_to_row,
)
from scripts import check_context_shadow_evaluation


T = datetime(2026, 7, 14, 14, 0, tzinfo=UTC)
PROFILE_HASH = "a" * 64
DOCUMENT_HASH = "b" * 64
SECTION_HASH = "c" * 64
EXCERPT_HASH = "d" * 64
RAW_HASH = "e" * 64


def _profile(**overrides: object) -> ResearchClassificationProfile:
    values: dict[str, object] = {
        "extraction_version": "sec_8k_items_v1",
        "prompt_version": "context_filter_v1",
        "model_version": "gemini-test",
        "response_schema_version": "context_classification_response_v1",
        "classification_config_hash": PROFILE_HASH,
    }
    values.update(overrides)
    return ResearchClassificationProfile(**values)  # type: ignore[arg-type]


def _run_definition(**overrides: object) -> ResearchRunDefinition:
    values: dict[str, object] = {
        "ticker_universe": ("XOM", "LMT"),
        "event_sources": ("sec_edgar",),
        "evidence_categories": (
            EvidenceCategory.AI_EVENT,
            EvidenceCategory.DETERMINISTIC_EVENT,
            EvidenceCategory.FLAG,
        ),
        "hydration_start_time": T - timedelta(days=2),
        "hydration_end_time": T + timedelta(days=2),
        "capacity": 50,
        "classification_profile": _profile(),
        "max_age_without_valid_until": timedelta(hours=2),
        "selection_policy_version": "research_selection_v1",
    }
    values.update(overrides)
    return ResearchRunDefinition(**values)  # type: ignore[arg-type]


def _decision_context(
    *,
    evaluation_time: datetime = T,
    value: str = "visible",
    ticker: str = "XOM",
    source: str = "macro_calendar_v1",
) -> object:
    cache = ContextStateCache()
    cache.update(
        make_global_context_entry(
            name="reviewed_macro",
            value=value,
            source=source,
            updated_at=evaluation_time - timedelta(minutes=1),
        )
    )
    return DecisionContextAssembler(cache=cache).build_for_decision(
        ticker,
        evaluation_time,
        "trace_pr37",
        None,
        ticker_sector="ENERGY",
    )


def _signal(
    *,
    signal_id: str = "signal_pr37",
    signal_time: datetime = T,
) -> ModelSignal:
    return ModelSignal(
        signal_time=signal_time,
        ticker="XOM",
        signal=SignalSide.BUY,
        confidence=0.7,
        raw_score=0.2,
        model_version="model_v1",
        calibration_version="calibration_v1",
        feature_version="features_v1",
        feature_snapshot_id="feature_snapshot_pr37",
        signal_id=signal_id,
        trace_id="trace_pr37",
    )


def _risk(
    *,
    risk_decision_id: str = "risk_pr37",
    signal_id: str = "signal_pr37",
) -> RiskDecision:
    return RiskDecision(
        decision_time=T + timedelta(milliseconds=10),
        ticker="XOM",
        model_signal_id=signal_id,
        decision=RiskDecisionType.APPROVE,
        approved=True,
        risk_version="risk_v1",
        risk_decision_id=risk_decision_id,
        trace_id="trace_pr37",
    )


def _evidence(
    evidence_id: str,
    *,
    category: EvidenceCategory = EvidenceCategory.AI_EVENT,
    match_value: str = "SEC_8K_RESULTS",
    available_at: datetime | None = T - timedelta(minutes=5),
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    tickers: tuple[str, ...] = ("XOM",),
    sector: str | None = None,
    global_relevance: bool = False,
    policy_eligible: bool = True,
    payload_marker: str = "base",
    source: str = "sec_edgar",
    source_record_id: str | None = None,
) -> ResearchEvidence:
    prefix = {
        EvidenceCategory.AI_EVENT: "AI_EVENT_TYPE",
        EvidenceCategory.DETERMINISTIC_EVENT: "DETERMINISTIC_EVENT_TYPE",
        EvidenceCategory.FLAG: "FLAG_TYPE",
    }[category]
    return ResearchEvidence(
        evidence_id=evidence_id,
        category=category,
        policy_match_key=f"{prefix}:{match_value}",
        source=source,
        source_record_id=source_record_id or f"source_{evidence_id}",
        tickers=tickers,
        sector=sector,
        global_relevance=global_relevance,
        available_at=available_at,
        valid_from=valid_from,
        valid_until=valid_until,
        fingerprint_payload={"marker": payload_marker},
        lineage_ids=(f"lineage_{evidence_id}",),
        policy_eligible=policy_eligible,
    )


def _index(
    *values: ResearchEvidence,
    run_definition: ResearchRunDefinition | None = None,
    hydration_exclusions: tuple[ResearchEvidenceExclusion, ...] = (),
) -> ResearchEvidenceIndex:
    return ResearchEvidenceIndex.build(
        run_definition=run_definition or _run_definition(),
        evidence=values,
        hydration_exclusions=hydration_exclusions,
    )


def _saved_classification(
    attempt_id: str,
    *,
    profile: ResearchClassificationProfile | None = None,
    status: str = "VALID",
    complete: bool = True,
    validation_outcome: bool | None = True,
    section_hash: str = SECTION_HASH,
    item_number: str = "2.02",
) -> dict[str, object]:
    profile = profile or _profile()
    return {
        "classification_complete": complete,
        "classification_request_id": f"request_{attempt_id}",
        "classification_attempt_id": attempt_id,
        "status": status,
        "event_type": "SEC_8K_RESULTS" if status == "VALID" else "UNKNOWN",
        "risk_level": "MEDIUM" if status == "VALID" else "UNKNOWN",
        "urgency": "MEDIUM" if status == "VALID" else "UNKNOWN",
        "confidence": 0.7 if status == "VALID" else None,
        "summary": "Safe fixture classification.",
        "classified_at": (T + timedelta(minutes=2)).isoformat(),
        "provider": "gemini",
        "model_version": profile.model_version,
        "prompt_version": profile.prompt_version,
        "response_schema_version": profile.response_schema_version,
        "classification_config_hash": profile.classification_config_hash,
        "accession_number": "0000000000-26-000001",
        "document_hash": DOCUMENT_HASH,
        "full_section_hash": section_hash,
        "excerpt_hash": EXCERPT_HASH,
        "extraction_version": profile.extraction_version,
        "item_number": item_number,
        "ledger_row": {
            "classification_attempt_id": attempt_id,
            "classification_request_id": f"request_{attempt_id}",
            "raw_input_id": "raw_sec_fixture",
            "source_document_id": "document_sec_fixture",
            "source": "sec_edgar",
            "source_type": "sec_8k_item",
            "source_platform": "sec_edgar",
            "source_uri": "https://www.sec.gov/Archives/fixture",
            "source_locator": "0000000000-26-000001:2.02",
            "affected_tickers_json": '["XOM"]',
            "raw_input_hash": RAW_HASH,
            "document_hash": DOCUMENT_HASH,
            "source_published_at": T.isoformat(),
            "source_updated_at": None,
            "collected_at": (T + timedelta(minutes=1)).isoformat(),
            "normalized_at": (T + timedelta(minutes=1)).isoformat(),
            "classified_at": (
                T + timedelta(minutes=2)
            ).isoformat().replace("+00:00", "Z"),
            "provider": "gemini",
            "model_version": profile.model_version,
            "prompt_version": profile.prompt_version,
            "status": status,
            "event_type": "SEC_8K_RESULTS" if status == "VALID" else "UNKNOWN",
            "risk_level": "MEDIUM" if status == "VALID" else "UNKNOWN",
            "urgency": "MEDIUM" if status == "VALID" else "UNKNOWN",
            "confidence": 0.7 if status == "VALID" else None,
            "summary": "Safe fixture classification.",
            "validation_result_id": f"validation_{attempt_id}",
            "validation_outcome": validation_outcome,
            "validated_at": (T + timedelta(minutes=2)).isoformat(),
        },
    }


def _write_sec_archive(
    tmp_path: Path,
    *,
    classifications: dict[str, object],
    form4_events: list[dict[str, object]] | None = None,
    form4_has_acceptance: bool = True,
) -> SECEDGARArchive:
    archive = SECEDGARArchive(tmp_path / "sec")
    archive.save_manifest(
        {
            "schema_version": 2,
            "filings": {
                "0000000000-26-000001": {
                    "form_type": "8-K",
                    "primary_document": "fixture-8k.htm",
                    "official_document_identity": "fixture-8k.htm",
                    "official_document_url": "https://www.sec.gov/Archives/fixture-8k.htm",
                    "document_hash": DOCUMENT_HASH,
                    "document_extension": ".htm",
                    "collected_at": (T + timedelta(minutes=1)).isoformat(),
                    "classifications": classifications,
                }
            },
        }
    )
    archive.write_filing_once(
        "0000000000-26-000001",
        {
            "ticker": "XOM",
            "issuer_cik": "0000000000",
            "accession_number": "0000000000-26-000001",
            "form_type": "8-K",
            "filing_date": T.date().isoformat(),
            "acceptance_at": T.isoformat(),
            "primary_document": "fixture-8k.htm",
            "filing_url": "https://www.sec.gov/Archives/fixture-8k.htm",
            "official_document_identity": "fixture-8k.htm",
            "official_document_url": "https://www.sec.gov/Archives/fixture-8k.htm",
            "amendment_of": None,
            "collected_at": (T + timedelta(minutes=1)).isoformat(),
            "document_hash": DOCUMENT_HASH,
        },
    )
    if form4_events is not None:
        form4_is_amendment = bool(form4_events and form4_events[0]["is_amendment"])
        form4_amends_accession = (
            None if not form4_events else form4_events[0]["amends_accession"]
        )
        if any(
            value["is_amendment"] != form4_is_amendment
            or value["amends_accession"] != form4_amends_accession
            for value in form4_events
        ):
            raise AssertionError("fixture Form 4 events must share one filing lineage")
        archive.write_form4_once(
            "0000000000-26-000002",
            {
                "filing": {
                    "ticker": "XOM",
                    "issuer_cik": "0000000000",
                    "accession_number": "0000000000-26-000002",
                    "form_type": "4/A" if form4_is_amendment else "4",
                    "filing_date": T.date().isoformat(),
                    "acceptance_at": T.isoformat() if form4_has_acceptance else None,
                    "primary_document": "form4.xml",
                    "filing_url": "https://www.sec.gov/Archives/form4-index.htm",
                    "official_document_identity": "form4.xml",
                    "official_document_url": "https://www.sec.gov/Archives/form4.xml",
                    "amendment_of": form4_amends_accession,
                    "collected_at": (T + timedelta(minutes=1)).isoformat(),
                    "document_hash": DOCUMENT_HASH,
                },
                "issuer_ticker": "XOM",
                "issuer_cik": "0000000000",
                "filing_plan_10b5_1": False,
                "reporting_owners": [],
                "is_amendment": form4_is_amendment,
                "amends_accession": form4_amends_accession,
                "normalized_transactions": [],
                "research_events": form4_events,
            },
        )
    return archive


def _form4_event(
    *,
    event_type: str = "SEC_FORM4_PURCHASE",
    eligibility: str = "ELIGIBLE",
) -> dict[str, object]:
    amends_accession = (
        "0000000000-26-000000"
        if eligibility == "AMENDMENT_RESOLVED"
        else None
    )
    return {
        "event_type": event_type,
        "issuer_ticker": "XOM",
        "issuer_cik": "0000000000",
        "accession_number": "0000000000-26-000002",
        "reporting_owners": [
            {
                "cik": "0000000001",
                "name": "Fixture Owner",
                "roles": ["OFFICER"],
                "officer_title": "CFO",
                "other_relationship_text": None,
            }
        ],
        "transaction_date": T.date().isoformat(),
        "available_at": T.isoformat(),
        "transaction_code": "P" if event_type.endswith("PURCHASE") else "S",
        "shares": 10.0,
        "price_per_share": 100.0,
        "approximate_value": 1000.0,
        "direct_or_indirect": "D",
        "shares_owned_following": 110.0,
        "is_amendment": eligibility != "ELIGIBLE",
        "amends_accession": amends_accession,
        "aggregate_eligibility": eligibility,
        "plan_10b5_1": True,
    }


def test_structured_context_is_reused_once_and_not_event_evidence() -> None:
    context = _decision_context()
    index = _index(_evidence("event_1"))
    selection = index.select(context)  # type: ignore[arg-type]

    assert len(context.all_structured_context) == 1
    assert [value.evidence_id for value in selection.selected_evidence] == ["event_1"]
    assert all(
        value.source != context.all_structured_context[0].source
        for value in selection.selected_evidence
    )

    structured_flag = ContextFlag(
        event_time=T,
        source="eia_wpsr_v1",
        flag_type="eia_window",
        severity="HIGH",
        context_flag_id="flag_structured",
        ticker="XOM",
        available_at=T,
    )
    with pytest.raises(ResearchProjectionError, match="structured-owned"):
        normalize_context_flag(structured_flag)
    with pytest.raises(ResearchProjectionError, match="structured-owned"):
        _run_definition(event_sources=("eia_wpsr_v1",))


def test_event_ownership_and_source_fact_identity_fail_closed() -> None:
    duplicate_fact = (
        _evidence("event_one", source_record_id="same_fact"),
        _evidence("event_two", source_record_id="same_fact"),
    )
    with pytest.raises(ResearchProjectionError, match="same source fact"):
        _index(*duplicate_fact)

    unknown_source = "fixture_ambiguous_source"
    index = _index(
        _evidence("ambiguous", source=unknown_source),
        run_definition=_run_definition(event_sources=(unknown_source,)),
    )
    with pytest.raises(ResearchProjectionError, match="ownership is ambiguous"):
        index.select(_decision_context(source=unknown_source))  # type: ignore[arg-type]

    with pytest.raises(ResearchProjectionError, match="outside the hydrated"):
        _index(_evidence("outside", tickers=("CVX",)))


def test_asof_selection_reports_each_exclusion_reason_and_exact_boundary() -> None:
    profile_exclusion = ResearchEvidenceExclusion(
        evidence_id="profile_other",
        reason=EvidenceExclusionReason.CLASSIFICATION_PROFILE_MISMATCH,
        source="sec_edgar",
        tickers=("XOM",),
        available_at=T,
    )
    index = _index(
        _evidence("at_boundary", available_at=T),
        _evidence("future", available_at=T + timedelta(microseconds=1)),
        _evidence("expired", valid_until=T - timedelta(microseconds=1)),
        _evidence("old", available_at=T - timedelta(hours=3)),
        _evidence("unknown", available_at=None),
        _evidence("wrong_scope", tickers=("LMT",)),
        _evidence("ineligible", policy_eligible=False),
        hydration_exclusions=(profile_exclusion,),
    )
    selection = index.select(_decision_context())  # type: ignore[arg-type]

    assert [value.evidence_id for value in selection.selected_evidence] == [
        "at_boundary"
    ]
    reasons = {value.evidence_id: value.reason for value in selection.exclusions}
    assert reasons == {
        "expired": EvidenceExclusionReason.EXPIRED,
        "future": EvidenceExclusionReason.FUTURE,
        "ineligible": EvidenceExclusionReason.POLICY_INELIGIBLE,
        "old": EvidenceExclusionReason.OUTSIDE_LOOKBACK,
        "profile_other": EvidenceExclusionReason.CLASSIFICATION_PROFILE_MISMATCH,
        "unknown": EvidenceExclusionReason.MISSING_AVAILABILITY,
        "wrong_scope": EvidenceExclusionReason.SCOPE_MISMATCH,
    }


def test_asof_boundaries_sector_global_and_hydration_coverage_are_explicit() -> None:
    exact_lookback = T - timedelta(hours=2)
    index = _index(
        _evidence(
            "validity_boundary",
            available_at=T,
            valid_from=T,
            valid_until=T,
        ),
        _evidence("lookback_boundary", available_at=exact_lookback),
        _evidence(
            "native_expiry_old",
            available_at=T - timedelta(days=1),
            valid_until=T,
        ),
        _evidence("sector", tickers=(), sector="ENERGY"),
        _evidence("global", tickers=(), global_relevance=True),
    )
    selection = index.select(_decision_context())  # type: ignore[arg-type]
    assert {value.evidence_id for value in selection.selected_evidence} == {
        "validity_boundary",
        "lookback_boundary",
        "native_expiry_old",
        "sector",
        "global",
    }

    earliest_complete = (
        index.run_definition.hydration_start_time
        + index.run_definition.max_age_without_valid_until
    )
    index.select(  # exact complete-coverage boundary is accepted
        _decision_context(evaluation_time=earliest_complete)  # type: ignore[arg-type]
    )
    with pytest.raises(ResearchProjectionError, match="complete hydrated evidence"):
        index.select(
            _decision_context(
                evaluation_time=earliest_complete - timedelta(microseconds=1)
            )  # type: ignore[arg-type]
        )


def test_hydration_exclusions_are_scoped_to_the_decision() -> None:
    exclusions = (
        ResearchEvidenceExclusion(
            evidence_id="xom_profile",
            reason=EvidenceExclusionReason.CLASSIFICATION_PROFILE_MISMATCH,
            source="sec_edgar",
            tickers=("XOM",),
            available_at=T,
        ),
        ResearchEvidenceExclusion(
            evidence_id="lmt_profile",
            reason=EvidenceExclusionReason.CLASSIFICATION_PROFILE_MISMATCH,
            source="sec_edgar",
            tickers=("LMT",),
            available_at=T,
        ),
        ResearchEvidenceExclusion(
            evidence_id="future_xom_profile",
            reason=EvidenceExclusionReason.CLASSIFICATION_PROFILE_MISMATCH,
            source="sec_edgar",
            tickers=("XOM",),
            available_at=T + timedelta(seconds=1),
        ),
        ResearchEvidenceExclusion(
            evidence_id="old_xom_profile",
            reason=EvidenceExclusionReason.CLASSIFICATION_PROFILE_MISMATCH,
            source="sec_edgar",
            tickers=("XOM",),
            available_at=T - timedelta(hours=3),
        ),
        ResearchEvidenceExclusion(
            evidence_id="run_level_malformed",
            reason=EvidenceExclusionReason.MALFORMED,
            source="sec_edgar",
        ),
    )
    index = _index(hydration_exclusions=exclusions)
    selection = index.select(_decision_context())  # type: ignore[arg-type]
    assert [value.evidence_id for value in selection.exclusions] == [
        "xom_profile"
    ]
    assert len(index.hydration_exclusions) == 5


def test_ordering_and_combined_fingerprint_are_deterministic_and_content_sensitive() -> None:
    context = _decision_context()
    late = _evidence("late", available_at=T - timedelta(minutes=1))
    early = _evidence("early", available_at=T - timedelta(minutes=2))
    first = _index(late, early).select(context)  # type: ignore[arg-type]
    second = _index(early, late).select(context)  # type: ignore[arg-type]
    assert [value.evidence_id for value in first.selected_evidence] == [
        "early",
        "late",
    ]
    assert build_shadow_context_fingerprint(
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=first,
    ) == build_shadow_context_fingerprint(
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=second,
    )

    changed = _index(
        early,
        _evidence(
            "late",
            available_at=T - timedelta(minutes=1),
            payload_marker="changed",
        ),
    ).select(context)  # type: ignore[arg-type]
    assert build_shadow_context_fingerprint(
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=changed,
    ) != build_shadow_context_fingerprint(
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=first,
    )

    changed_settings = _index(
        late,
        early,
        run_definition=_run_definition(
            max_age_without_valid_until=timedelta(hours=3)
        ),
    ).select(context)  # type: ignore[arg-type]
    assert build_shadow_context_fingerprint(
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=changed_settings,
    ) != build_shadow_context_fingerprint(
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=first,
    )

    immutable = ResearchEvidence(
        evidence_id="immutable",
        category=EvidenceCategory.AI_EVENT,
        policy_match_key="AI_EVENT_TYPE:SEC_8K_RESULTS",
        source="sec_edgar",
        source_record_id="immutable_source",
        tickers=("XOM",),
        available_at=T,
        fingerprint_payload={"nested": {"values": [1, 2]}},
    )
    with pytest.raises(TypeError):
        immutable.fingerprint_payload["changed"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        immutable.fingerprint_payload["nested"]["changed"] = True  # type: ignore[index]
    assert immutable.fingerprint_payload["nested"]["values"] == (1, 2)  # type: ignore[index]


def test_capacity_overflow_aborts_with_complete_recovery_metadata() -> None:
    definition = _run_definition(capacity=1)
    with pytest.raises(ResearchEvidenceCapacityError) as captured:
        ResearchEvidenceIndex.build(
            run_definition=definition,
            evidence=(_evidence("one"), _evidence("two")),
            attempted_record_count=2,
        )
    error = captured.value
    assert error.attempted_record_count == 2
    assert error.capacity == 1
    assert error.ticker_universe == ("LMT", "XOM")
    assert error.hydration_start_time == T - timedelta(days=2)
    assert "attempted_record_count=2" in str(error)

    exclusions = tuple(
        ResearchEvidenceExclusion(
            evidence_id=f"excluded_{index}",
            reason=EvidenceExclusionReason.MALFORMED,
            source="sec_edgar",
        )
        for index in range(2)
    )
    with pytest.raises(ResearchProjectionError, match="attempted_record_count"):
        ResearchEvidenceIndex.build(
            run_definition=definition,
            evidence=(),
            hydration_exclusions=exclusions,
            attempted_record_count=0,
        )
    with pytest.raises(ResearchEvidenceCapacityError):
        ResearchEvidenceIndex.build(
            run_definition=definition,
            evidence=(),
            hydration_exclusions=exclusions,
        )


def test_sec_hydration_capacity_overflow_publishes_no_partial_index(
    tmp_path: Path,
) -> None:
    archive = _write_sec_archive(
        tmp_path,
        classifications={"matching": _saved_classification("attempt_matching")},
        form4_events=[_form4_event()],
    )
    published: ResearchEvidenceIndex | None = None
    with pytest.raises(ResearchEvidenceCapacityError) as captured:
        published = hydrate_sec_research_evidence(
            archive=archive,
            run_definition=_run_definition(capacity=1),
        )
    assert published is None
    assert captured.value.attempted_record_count == 2
    assert captured.value.capacity == 1


def test_sec_hydration_pins_one_profile_and_preserves_form4_semantics(
    tmp_path: Path,
) -> None:
    other_profile = _profile(model_version="other-model")
    archive = _write_sec_archive(
        tmp_path,
        classifications={
            "matching": _saved_classification("attempt_matching"),
            "other": _saved_classification(
                "attempt_other",
                profile=other_profile,
            ),
            "abstained": _saved_classification(
                "attempt_abstained",
                status="ABSTAINED",
                section_hash="1" * 64,
                item_number="1.01",
            ),
            "incomplete": _saved_classification(
                "attempt_incomplete",
                complete=False,
                section_hash="2" * 64,
                item_number="8.01",
            ),
        },
        form4_events=[_form4_event()],
    )
    index = hydrate_sec_research_evidence(
        archive=archive,
        run_definition=_run_definition(),
    )
    selection = index.select(_decision_context())  # type: ignore[arg-type]

    assert [value.category for value in selection.selected_evidence] == [
        EvidenceCategory.AI_EVENT,
        EvidenceCategory.DETERMINISTIC_EVENT,
    ]
    form4 = selection.selected_evidence[1]
    assert form4.policy_match_key == (
        "DETERMINISTIC_EVENT_TYPE:SEC_FORM4_PURCHASE"
    )
    assert form4.fingerprint_payload["plan_10b5_1"] is True
    assert form4.fingerprint_payload["reporting_owners"][0]["name"] == (
        "Fixture Owner"
    )
    assert form4.fingerprint_payload["source_uri"] == (
        "https://www.sec.gov/Archives/form4.xml"
    )
    ai_event = selection.selected_evidence[0]
    assert ai_event.fingerprint_payload["context_ai_event"]["collected_at"] == (
        "2026-07-14T14:01:00Z"
    )
    assert ai_event.fingerprint_payload["context_ai_event"]["classified_at"] == (
        "2026-07-14T14:02:00Z"
    )
    reasons = [value.reason for value in selection.exclusions]
    assert reasons.count(EvidenceExclusionReason.CLASSIFICATION_PROFILE_MISMATCH) == 1
    assert reasons.count(EvidenceExclusionReason.POLICY_INELIGIBLE) == 2


def test_duplicate_valid_same_profile_section_fails_closed(tmp_path: Path) -> None:
    archive = _write_sec_archive(
        tmp_path,
        classifications={
            "first": _saved_classification("attempt_first"),
            "second": _saved_classification("attempt_second"),
        },
    )
    with pytest.raises(ResearchProjectionError, match="multiple classifications"):
        hydrate_sec_research_evidence(
            archive=archive,
            run_definition=_run_definition(),
        )


def test_same_profile_conflict_is_independent_of_classification_status(
    tmp_path: Path,
) -> None:
    archive = _write_sec_archive(
        tmp_path,
        classifications={
            "valid": _saved_classification("attempt_valid"),
            "abstained": _saved_classification(
                "attempt_abstained",
                status="ABSTAINED",
            ),
        },
    )
    with pytest.raises(ResearchProjectionError, match="multiple classifications"):
        hydrate_sec_research_evidence(
            archive=archive,
            run_definition=_run_definition(),
        )


@pytest.mark.parametrize("corruption", ["accession", "document_hash", "event_type"])
def test_corrupt_8k_lineage_or_semantics_never_becomes_active(
    tmp_path: Path,
    corruption: str,
) -> None:
    saved = _saved_classification("attempt_corrupt")
    row = saved["ledger_row"]
    assert isinstance(row, dict)
    if corruption == "accession":
        saved["accession_number"] = "0000000000-26-999999"
    elif corruption == "document_hash":
        saved["document_hash"] = "9" * 64
        row["document_hash"] = "9" * 64
    else:
        saved["event_type"] = "INVENTED_EVENT"
        row["event_type"] = "INVENTED_EVENT"
    archive = _write_sec_archive(
        tmp_path,
        classifications={"corrupt": saved},
    )
    index = hydrate_sec_research_evidence(
        archive=archive,
        run_definition=_run_definition(),
    )
    selection = index.select(_decision_context())  # type: ignore[arg-type]
    assert selection.selected_evidence == ()
    assert [value.reason for value in selection.exclusions] == [
        EvidenceExclusionReason.MALFORMED
    ]


def test_corrupt_form4_issuer_lineage_never_becomes_active(tmp_path: Path) -> None:
    event = _form4_event()
    event["issuer_ticker"] = "LMT"
    archive = _write_sec_archive(
        tmp_path,
        classifications={},
        form4_events=[event],
    )
    index = hydrate_sec_research_evidence(
        archive=archive,
        run_definition=_run_definition(),
    )
    selection = index.select(_decision_context())  # type: ignore[arg-type]
    assert selection.selected_evidence == ()
    assert [value.reason for value in selection.exclusions] == [
        EvidenceExclusionReason.MALFORMED
    ]


def test_unresolved_form4_amendment_is_preserved_but_policy_ineligible(
    tmp_path: Path,
) -> None:
    archive = _write_sec_archive(
        tmp_path,
        classifications={},
        form4_events=[
            _form4_event(
                event_type="SEC_FORM4_SALE",
                eligibility="AMENDMENT_UNRESOLVED",
            )
        ],
    )
    index = hydrate_sec_research_evidence(
        archive=archive,
        run_definition=_run_definition(),
    )
    assert len(index.evidence) == 1
    assert index.evidence[0].fingerprint_payload["is_amendment"] is True
    assert index.evidence[0].fingerprint_payload["aggregate_eligibility"] == (
        "AMENDMENT_UNRESOLVED"
    )
    selection = index.select(_decision_context())  # type: ignore[arg-type]
    assert selection.selected_evidence == ()
    assert [value.reason for value in selection.exclusions] == [
        EvidenceExclusionReason.POLICY_INELIGIBLE
    ]


def test_form4_collection_fallback_never_substitutes_for_canonical_availability(
    tmp_path: Path,
) -> None:
    event = _form4_event()
    event["available_at"] = (T + timedelta(minutes=1)).isoformat()
    archive = _write_sec_archive(
        tmp_path,
        classifications={},
        form4_events=[event],
        form4_has_acceptance=False,
    )
    index = hydrate_sec_research_evidence(
        archive=archive,
        run_definition=_run_definition(),
    )
    assert index.evidence == ()
    selection = index.select(_decision_context())  # type: ignore[arg-type]
    assert [value.reason for value in selection.exclusions] == [
        EvidenceExclusionReason.MISSING_AVAILABILITY
    ]


def test_existing_form4_record_adapts_without_a_new_public_contract() -> None:
    event = Form4ResearchEvent(
        event_type=DeterministicContextEventType.SEC_FORM4_PURCHASE,
        issuer_ticker="XOM",
        issuer_cik="0000000000",
        accession_number="0000000000-26-000002",
        reporting_owners=(
            Form4ReportingOwner(
                cik="0000000001",
                name="Fixture Owner",
                roles=("OFFICER",),
                officer_title="CFO",
                other_relationship_text=None,
            ),
        ),
        transaction_date=T.date(),
        available_at=T,
        transaction_code="P",
        shares=10.0,
        price_per_share=100.0,
        approximate_value=1000.0,
        direct_or_indirect="D",
        shares_owned_following=110.0,
        is_amendment=False,
        amends_accession=None,
        aggregate_eligibility="ELIGIBLE",
        plan_10b5_1=True,
    )
    normalized = normalize_form4_research_event(
        event,
        document_hash=DOCUMENT_HASH,
        source_uri="https://www.sec.gov/Archives/form4.xml",
        ordinal=0,
    )
    assert normalized.category is EvidenceCategory.DETERMINISTIC_EVENT
    assert normalized.policy_match_key == (
        "DETERMINISTIC_EVENT_TYPE:SEC_FORM4_PURCHASE"
    )
    assert normalized.fingerprint_payload["transaction_date"] == "2026-07-14"
    assert normalized.fingerprint_payload["reporting_owners"][0]["name"] == (
        "Fixture Owner"
    )
    assert normalized.fingerprint_payload["plan_10b5_1"] is True


def test_profile_is_explicit_and_changes_combined_fingerprint() -> None:
    with pytest.raises(ResearchProjectionError, match="classification_profile"):
        _run_definition(classification_profile=None)
    context = _decision_context()
    base = _index(_evidence("event")).select(context)  # type: ignore[arg-type]
    changed = _index(
        _evidence("event"),
        run_definition=_run_definition(
            classification_profile=_profile(model_version="other-model")
        ),
    ).select(context)  # type: ignore[arg-type]
    assert build_shadow_context_fingerprint(
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=base,
    ) != build_shadow_context_fingerprint(
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=changed,
    )


def test_form4_matches_synthetic_rule_and_global_priority_records_all_winners() -> None:
    context = _decision_context()
    purchase_one = _evidence(
        "purchase_1",
        category=EvidenceCategory.DETERMINISTIC_EVENT,
        match_value="SEC_FORM4_PURCHASE",
    )
    purchase_two = _evidence(
        "purchase_2",
        category=EvidenceCategory.DETERMINISTIC_EVENT,
        match_value="SEC_FORM4_PURCHASE",
        available_at=T - timedelta(minutes=4),
    )
    selected_non_winner = _evidence(
        "ai_block_candidate",
        category=EvidenceCategory.AI_EVENT,
        match_value="SEC_8K_CYBERSECURITY_INCIDENT",
    )
    selection = _index(
        selected_non_winner,
        purchase_one,
        purchase_two,
    ).select(context)  # type: ignore[arg-type]
    policy = ShadowContextPolicy(
        policy_version="synthetic_priority_v1",
        rules=(
            ShadowContextRule(
                rule_id="form4_warn",
                match_keys=(
                    "DETERMINISTIC_EVENT_TYPE:SEC_FORM4_PURCHASE",
                ),
                action=ShadowContextAction.WARN_ONLY,
                reason_code="SYNTHETIC_FORM4_WARNING",
            ),
            ShadowContextRule(
                rule_id="cyber_block",
                match_keys=(
                    "AI_EVENT_TYPE:SEC_8K_CYBERSECURITY_INCIDENT",
                ),
                action=ShadowContextAction.BLOCK,
                reason_code="SYNTHETIC_CYBER_BLOCK",
            ),
        ),
    )
    evaluation = evaluate_shadow_context(
        model_signal=_signal(),
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=selection,
        policy=policy,
    )

    assert evaluation.hypothetical_action is ShadowContextAction.WARN_ONLY
    assert evaluation.matched_context_event_ids == ["purchase_1", "purchase_2"]
    assert evaluation.matched_context_flag_ids == []
    assert evaluation.reason_codes == ["SYNTHETIC_FORM4_WARNING"]
    assert "ai_block_candidate" not in evaluation.matched_context_event_ids

    changed_non_winner = _index(
        _evidence(
            "ai_block_candidate",
            category=EvidenceCategory.AI_EVENT,
            match_value="SEC_8K_CYBERSECURITY_INCIDENT",
            payload_marker="changed",
        ),
        purchase_one,
        purchase_two,
    ).select(context)  # type: ignore[arg-type]
    changed_evaluation = evaluate_shadow_context(
        model_signal=_signal(),
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=changed_non_winner,
        policy=policy,
    )
    assert changed_evaluation.hypothetical_action is ShadowContextAction.WARN_ONLY
    assert changed_evaluation.matched_context_event_ids == (
        evaluation.matched_context_event_ids
    )
    assert changed_evaluation.shadow_context_fingerprint != (
        evaluation.shadow_context_fingerprint
    )


def test_form4_sale_and_event_owned_flag_are_reachable_by_rules(
    tmp_path: Path,
) -> None:
    archive = _write_sec_archive(
        tmp_path,
        classifications={},
        form4_events=[_form4_event(event_type="SEC_FORM4_SALE")],
    )
    sale_index = hydrate_sec_research_evidence(
        archive=archive,
        run_definition=_run_definition(),
    )
    context = _decision_context()
    sale_evaluation = evaluate_shadow_context(
        model_signal=_signal(),
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=sale_index.select(context),  # type: ignore[arg-type]
        policy=ShadowContextPolicy(
            policy_version="synthetic_sale_v1",
            rules=(
                ShadowContextRule(
                    rule_id="sale_delay",
                    match_keys=(
                        "DETERMINISTIC_EVENT_TYPE:SEC_FORM4_SALE",
                    ),
                    action=ShadowContextAction.DELAY,
                    reason_code="SYNTHETIC_FORM4_SALE",
                ),
            ),
        ),
    )
    assert sale_evaluation.hypothetical_action is ShadowContextAction.DELAY
    assert len(sale_evaluation.matched_context_event_ids) == 1

    flag = ContextFlag(
        event_time=T,
        source="validated_news_fixture",
        flag_type="headline_risk",
        severity="HIGH",
        context_flag_id="flag_event_owned",
        ticker="XOM",
        available_at=T,
    )
    normalized_flag = normalize_context_flag(flag)
    flag_index = _index(
        normalized_flag,
        run_definition=_run_definition(
            event_sources=("validated_news_fixture",),
            evidence_categories=(EvidenceCategory.FLAG,),
        ),
    )
    flag_policy = ShadowContextPolicy(
        policy_version="synthetic_flag_v1",
        rules=(
            ShadowContextRule(
                rule_id="flag_warn",
                match_keys=("FLAG_TYPE:HEADLINE_RISK",),
                action=ShadowContextAction.WARN_ONLY,
                reason_code="SYNTHETIC_FLAG_WARNING",
            ),
        ),
    )
    flag_evaluation = evaluate_shadow_context(
        model_signal=_signal(),
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=flag_index.select(context),  # type: ignore[arg-type]
        policy=flag_policy,
    )
    assert flag_evaluation.matched_context_event_ids == []
    assert flag_evaluation.matched_context_flag_ids == ["flag_event_owned"]

    changed_policy = ShadowContextPolicy(
        policy_version=flag_policy.policy_version,
        rules=(
            ShadowContextRule(
                rule_id="flag_block",
                match_keys=("FLAG_TYPE:HEADLINE_RISK",),
                action=ShadowContextAction.BLOCK,
                reason_code="SYNTHETIC_FLAG_BLOCK",
            ),
        ),
    )
    assert changed_policy.policy_config_hash != flag_policy.policy_config_hash


@pytest.mark.parametrize(
    ("action", "factor"),
    [
        (ShadowContextAction.WARN_ONLY, None),
        (ShadowContextAction.DELAY, None),
        (ShadowContextAction.BLOCK, None),
        (ShadowContextAction.REDUCE_SIZE, 0.5),
    ],
)
def test_synthetic_policies_produce_each_supported_action(
    action: ShadowContextAction,
    factor: float | None,
) -> None:
    context = _decision_context()
    selection = _index(_evidence("event")).select(context)  # type: ignore[arg-type]
    policy = ShadowContextPolicy(
        policy_version=f"synthetic_{action.value.lower()}_v1",
        rules=(
            ShadowContextRule(
                rule_id="synthetic",
                match_keys=("AI_EVENT_TYPE:SEC_8K_RESULTS",),
                action=action,
                reason_code="SYNTHETIC_RULE",
                proposed_size_factor=factor,
            ),
        ),
    )
    evaluation = evaluate_shadow_context(
        model_signal=_signal(),
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=selection,
        policy=policy,
    )
    assert evaluation.hypothetical_action is action
    assert evaluation.proposed_size_factor == factor


def test_default_policy_returns_no_change_and_no_matched_ids() -> None:
    context = _decision_context()
    selection = _index(_evidence("event")).select(context)  # type: ignore[arg-type]
    evaluation = evaluate_shadow_context(
        model_signal=_signal(),
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=selection,
    )
    assert evaluation.hypothetical_action is ShadowContextAction.NO_CHANGE
    assert evaluation.matched_context_event_ids == []
    assert evaluation.matched_context_flag_ids == []
    assert evaluation.reason_codes == []


def test_complete_evaluation_identity_changes_for_every_logical_input() -> None:
    context = _decision_context()
    selection = _index(_evidence("event")).select(context)  # type: ignore[arg-type]
    base_policy = ShadowContextPolicy()
    base = evaluate_shadow_context(
        model_signal=_signal(),
        risk_decision=_risk(),
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=selection,
        policy=base_policy,
    )
    repeated = evaluate_shadow_context(
        model_signal=_signal(),
        risk_decision=_risk(),
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=selection,
        policy=base_policy,
    )
    assert repeated.shadow_evaluation_id == base.shadow_evaluation_id

    changed_signal = evaluate_shadow_context(
        model_signal=_signal(signal_id="signal_other"),
        risk_decision=_risk(signal_id="signal_other"),
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=selection,
        policy=base_policy,
    )
    changed_risk = evaluate_shadow_context(
        model_signal=_signal(),
        risk_decision=_risk(risk_decision_id="risk_other"),
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=selection,
        policy=base_policy,
    )
    changed_context_obj = _decision_context(value="changed")
    changed_context_selection = _index(_evidence("event")).select(
        changed_context_obj  # type: ignore[arg-type]
    )
    changed_context = evaluate_shadow_context(
        model_signal=_signal(),
        risk_decision=_risk(),
        decision_context=changed_context_obj,  # type: ignore[arg-type]
        evidence_selection=changed_context_selection,
        policy=base_policy,
    )
    changed_event_selection = _index(
        _evidence("event", payload_marker="changed_event")
    ).select(context)  # type: ignore[arg-type]
    changed_event_context = evaluate_shadow_context(
        model_signal=_signal(),
        risk_decision=_risk(),
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=changed_event_selection,
        policy=base_policy,
    )
    changed_policy_version = evaluate_shadow_context(
        model_signal=_signal(),
        risk_decision=_risk(),
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=selection,
        policy=ShadowContextPolicy(policy_version="shadow_no_change_v2"),
    )
    changed_policy_config = evaluate_shadow_context(
        model_signal=_signal(),
        risk_decision=_risk(),
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=selection,
        policy=ShadowContextPolicy(
            policy_version="shadow_no_change_v1",
            rules=(
                ShadowContextRule(
                    rule_id="warn",
                    match_keys=("AI_EVENT_TYPE:SEC_8K_OTHER_EVENT",),
                    action=ShadowContextAction.WARN_ONLY,
                    reason_code="SYNTHETIC_WARN",
                ),
            ),
        ),
    )
    later = T + timedelta(minutes=1)
    later_context = _decision_context(evaluation_time=later)
    later_selection = _index(_evidence("event")).select(
        later_context  # type: ignore[arg-type]
    )
    changed_time = evaluate_shadow_context(
        model_signal=_signal(signal_time=later),
        decision_context=later_context,  # type: ignore[arg-type]
        evidence_selection=later_selection,
        policy=base_policy,
    )
    ids = {
        changed_signal.shadow_evaluation_id,
        changed_risk.shadow_evaluation_id,
        changed_context.shadow_evaluation_id,
        changed_event_context.shadow_evaluation_id,
        changed_policy_version.shadow_evaluation_id,
        changed_policy_config.shadow_evaluation_id,
        changed_time.shadow_evaluation_id,
    }
    assert base.shadow_evaluation_id not in ids
    assert len(ids) == 7


def test_evaluation_reuses_existing_questdb_converter_and_preserves_real_inputs(
    tmp_path: Path,
) -> None:
    context = _decision_context()
    archive = _write_sec_archive(
        tmp_path,
        classifications={"matching": _saved_classification("attempt_matching")},
    )
    selection = hydrate_sec_research_evidence(
        archive=archive,
        run_definition=_run_definition(),
    ).select(context)  # type: ignore[arg-type]
    assert selection.selected_evidence[0].fingerprint_payload[
        "context_ai_event"
    ]["summary"] == "Safe fixture classification."  # type: ignore[index]
    model = _signal()
    risk = _risk()
    model_before = to_json_string(model)
    risk_before = to_json_string(risk)
    evaluation = evaluate_shadow_context(
        model_signal=model,
        risk_decision=risk,
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=selection,
    )
    row = shadow_context_policy_evaluation_to_row(evaluation, write_time=T)

    assert row["shadow_evaluation_id"] == evaluation.shadow_evaluation_id
    assert row["model_signal_id"] == model.signal_id
    assert row["risk_decision_id"] == risk.risk_decision_id
    encoded = json.dumps(row, default=str, sort_keys=True)
    assert "Safe fixture classification" not in encoded
    assert "source text" not in encoded
    assert to_json_string(model) == model_before
    assert to_json_string(risk) == risk_before


def test_selection_and_evaluation_perform_no_file_or_socket_io(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context = _decision_context()
    archive = _write_sec_archive(
        tmp_path,
        classifications={"matching": _saved_classification("attempt_matching")},
        form4_events=[_form4_event()],
    )
    index = hydrate_sec_research_evidence(
        archive=archive,
        run_definition=_run_definition(),
    )

    def blocked(*args: object, **kwargs: object) -> object:
        raise AssertionError("post-hydration evaluation attempted external I/O")

    monkeypatch.setattr(builtins, "open", blocked)
    monkeypatch.setattr(Path, "open", blocked)
    monkeypatch.setattr(Path, "read_text", blocked)
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(SECEDGARArchive, "load_manifest", blocked)
    monkeypatch.setattr(SECEDGARArchive, "read_filing_metadata", blocked)
    monkeypatch.setattr(
        QuestDBLedgerWriter,
        "write_shadow_context_policy_evaluation",
        blocked,
    )
    selection = index.select(context)  # type: ignore[arg-type]
    evaluation = evaluate_shadow_context(
        model_signal=_signal(),
        decision_context=context,  # type: ignore[arg-type]
        evidence_selection=selection,
    )
    assert evaluation.hypothetical_action is ShadowContextAction.NO_CHANGE


def test_evaluator_rejects_mismatched_signal_context_and_risk() -> None:
    context = _decision_context()
    selection = _index(_evidence("event")).select(context)  # type: ignore[arg-type]
    with pytest.raises(ShadowEvaluationError, match="model_signal_id"):
        evaluate_shadow_context(
            model_signal=_signal(),
            risk_decision=_risk(signal_id="different"),
            decision_context=context,  # type: ignore[arg-type]
            evidence_selection=selection,
        )
    with pytest.raises(ShadowEvaluationError, match="evaluation_time"):
        evaluate_shadow_context(
            model_signal=_signal(signal_time=T + timedelta(seconds=1)),
            decision_context=context,  # type: ignore[arg-type]
            evidence_selection=selection,
        )


def test_offline_checker_uses_no_network_or_questdb_by_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def blocked_socket(*args: object, **kwargs: object) -> object:
        raise AssertionError("offline checker attempted network access")

    def blocked_questdb(*args: object, **kwargs: object) -> object:
        raise AssertionError("offline checker attempted a QuestDB write")

    monkeypatch.setattr(socket, "socket", blocked_socket)
    monkeypatch.setattr(
        check_context_shadow_evaluation.QuestDBLedgerWriter,
        "write_shadow_context_policy_evaluation",
        blocked_questdb,
    )

    assert check_context_shadow_evaluation.main([]) == 0
    output = capsys.readouterr().out
    assert "PR37 context shadow check PASS" in output
    assert "questdb_write=disabled" in output
