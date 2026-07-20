"""Durable PR35 classification flow for archived external source revisions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
import json
from typing import Any, Callable, Mapping, Protocol

from market_relay_engine.ai_context.classifier import (
    ContextClassificationAttemptResult,
    ContextClassifier,
    merge_classification_scope,
)
from market_relay_engine.common.ids import new_record_id
from market_relay_engine.common.serialization import to_json_dict
from market_relay_engine.common.time import parse_utc_iso, to_utc_iso, utc_now
from market_relay_engine.context.external_event_archive import (
    ExternalEventArchive,
    ExternalEventArchiveError,
    ExternalSourceRevision,
    classification_input_fingerprint,
    output_fingerprints,
)
from market_relay_engine.context.external_normalization import (
    EXCERPT_VERSION,
    SCOPE_RESOLVER_VERSION,
    ResolvedScope,
    ScopeAwareExcerpt,
    build_scope_aware_excerpt,
    resolve_explicit_scope,
    union_scope,
)
from market_relay_engine.context.research_projection import (
    ResearchSourceClassificationProfile,
)
from market_relay_engine.contracts.context import (
    ContextAIEvent,
    ContextClassificationEventType,
    ContextClassificationRequest,
    ContextClassificationResponse,
    ContextClassificationStatus,
    ContextLifecycleState,
    ContextRawInput,
    ContextRiskLevel,
    ContextSourceDocument,
    ContextUrgency,
    ContextValidationResult,
)


class ExternalClassificationError(RuntimeError):
    """Raised when external classification cannot be published safely."""


class MetadataWriter(Protocol):
    """Optional existing QuestDB writer boundary; never receives source text."""

    def write(self, record: object) -> object: ...


@dataclass(frozen=True, kw_only=True)
class ExternalClassificationOutcome:
    source_revision_id: str
    classification_input_fingerprint: str | None
    status: str
    provider_called: bool
    reused_canonical_result: bool
    policy_eligible: bool
    evidence_ready_at: datetime | None
    context_event: ContextAIEvent | None = None


@dataclass(frozen=True, kw_only=True)
class PreparedExternalClassification:
    raw_input: ContextRawInput
    source_document: ContextSourceDocument
    request: ContextClassificationRequest
    deterministic_scope: ResolvedScope
    excerpt: ScopeAwareExcerpt
    profile: ResearchSourceClassificationProfile


class ExternalClassificationPipeline:
    """Archive-first, restart-safe adapter into the existing PR35 classifier."""

    def __init__(
        self,
        *,
        archive: ExternalEventArchive,
        classifier: ContextClassifier,
        profile: ResearchSourceClassificationProfile,
        approved_tickers: tuple[str, ...],
        approved_sectors: tuple[str, ...],
        ticker_sector_hints: Mapping[str, str],
        max_input_characters: int = 12_000,
        questdb_writer: MetadataWriter | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if not isinstance(archive, ExternalEventArchive):
            raise TypeError("archive must be an ExternalEventArchive")
        if not isinstance(profile, ResearchSourceClassificationProfile):
            raise TypeError("profile must be a ResearchSourceClassificationProfile")
        if max_input_characters < 1_000:
            raise ValueError("max_input_characters must be at least 1000")
        self._archive = archive
        self._classifier = classifier
        self._profile = profile
        self._approved_tickers = tuple(sorted({value.upper() for value in approved_tickers}))
        self._approved_sectors = tuple(sorted({value.upper() for value in approved_sectors}))
        canonical_hints = {
            str(ticker).upper(): str(sector).upper()
            for ticker, sector in ticker_sector_hints.items()
        }
        if any(
            ticker not in self._approved_tickers
            or sector not in self._approved_sectors
            for ticker, sector in canonical_hints.items()
        ):
            raise ValueError(
                "ticker_sector_hints must use the approved ticker and sector universes"
            )
        self._ticker_sector_hints = dict(sorted(canonical_hints.items()))
        self._max_input_characters = max_input_characters
        self._questdb_writer = questdb_writer
        self._now = now

    def prepare(
        self,
        revision: ExternalSourceRevision,
        *,
        title: str | None = None,
        earnings: bool = False,
    ) -> PreparedExternalClassification | None:
        """Build the existing contracts from already durable source objects."""
        self._validate_revision_profile(revision)
        if title is None:
            title = revision.source_title
        if revision.normalized_text_hash is None:
            return None
        try:
            normalized_text = self._archive.read_object(
                revision.normalized_text_hash,
                filename="normalized.txt",
            ).decode("utf-8")
        except (UnicodeDecodeError, ExternalEventArchiveError) as exc:
            raise ExternalClassificationError(
                "archived normalized source text is unavailable"
            ) from exc
        if not normalized_text.strip():
            return None
        canonical_title = "" if not title else " ".join(title.split())
        classification_text = (
            normalized_text
            if not canonical_title
            else f"[TITLE]\n{canonical_title}\n\n[BODY]\n{normalized_text}"
        )
        deterministic_scope = resolve_explicit_scope(
            classification_text,
            approved_tickers=self._approved_tickers,
        )
        deterministic_scope = union_scope(
            fixed_tickers=revision.affected_tickers,
            deterministic=deterministic_scope,
            approved_tickers=self._approved_tickers,
            approved_sectors=self._approved_sectors,
        )
        excerpt = build_scope_aware_excerpt(
            classification_text,
            title=None,
            scope=deterministic_scope,
            max_characters=self._max_input_characters,
            earnings=earnings,
        )
        archived_excerpt_hash = self._archive.archive_excerpt(excerpt.text)
        if archived_excerpt_hash != excerpt.excerpt_hash:
            raise ExternalClassificationError("archived excerpt hash changed")
        raw_input = ContextRawInput(
            raw_input_id=_stable_id(
                "raw_input_external",
                revision.source,
                revision.source_revision_id,
            ),
            source=revision.source,
            source_type=revision.source_type,
            source_locator=revision.source_fact_id,
            raw_input_hash=revision.raw_object_hash,
            affected_tickers=list(deterministic_scope.tickers),
            affected_sectors=list(deterministic_scope.sectors),
            global_relevance=deterministic_scope.global_relevance,
            collected_at=revision.system_observed_at,
            source_platform=revision.source_platform,
            source_uri=revision.source_uri,
            source_published_at=revision.source_published_at,
            source_updated_at=revision.source_updated_at,
            source_fact_id=revision.source_fact_id,
            source_revision_id=revision.source_revision_id,
            revision_sequence=revision.revision_sequence,
            supersedes_revision_id=revision.supersedes_revision_id,
            lifecycle_state=ContextLifecycleState(revision.lifecycle_state.value),
            lifecycle_effective_at=revision.lifecycle_effective_at,
            source_available_at=revision.source_available_at,
            system_observed_at=revision.system_observed_at,
            archived_at=revision.archived_at,
            trace_id=revision.trace_id,
        )
        source_document = ContextSourceDocument(
            source_document_id=_stable_id(
                "source_document_external",
                revision.source,
                revision.source_revision_id,
                revision.normalized_text_hash,
            ),
            raw_input_id=raw_input.raw_input_id,
            source=revision.source,
            source_type=revision.source_type,
            source_locator=revision.source_fact_id,
            raw_input_hash=revision.raw_object_hash,
            document_hash=revision.document_hash,
            affected_tickers=list(deterministic_scope.tickers),
            affected_sectors=list(deterministic_scope.sectors),
            global_relevance=deterministic_scope.global_relevance,
            collected_at=revision.system_observed_at,
            normalized_at=revision.normalized_at or revision.archived_at,
            source_platform=revision.source_platform,
            source_uri=revision.source_uri,
            source_published_at=revision.source_published_at,
            source_updated_at=revision.source_updated_at,
            source_fact_id=revision.source_fact_id,
            source_revision_id=revision.source_revision_id,
            revision_sequence=revision.revision_sequence,
            supersedes_revision_id=revision.supersedes_revision_id,
            lifecycle_state=ContextLifecycleState(revision.lifecycle_state.value),
            lifecycle_effective_at=revision.lifecycle_effective_at,
            source_available_at=revision.source_available_at,
            system_observed_at=revision.system_observed_at,
            archived_at=revision.archived_at,
            trace_id=revision.trace_id,
        )
        semantic_request = {
            "source": revision.source,
            "source_type": revision.source_type,
            "source_platform": revision.source_platform,
            "document_hash": revision.document_hash,
            "normalized_text_hash": revision.normalized_text_hash,
            "classification_text_hash": excerpt.full_hash,
            "normalized_character_count": len(normalized_text),
            "excerpt_hash": excerpt.excerpt_hash,
            "full_character_count": excerpt.full_character_count,
            "excerpt_character_count": excerpt.excerpt_character_count,
            "truncated": excerpt.truncated,
            "allowed_tickers": list(self._approved_tickers),
            "allowed_sectors": list(self._approved_sectors),
            "ticker_sector_hints": self._ticker_sector_hints,
            "sector_hints": sorted(
                {
                    self._ticker_sector_hints[ticker]
                    for ticker in deterministic_scope.tickers
                    if ticker in self._ticker_sector_hints
                }
            ),
            "trusted_scope": {
                "affected_tickers": list(deterministic_scope.tickers),
                "affected_sectors": list(deterministic_scope.sectors),
                "global_relevance": deterministic_scope.global_relevance,
                "supporting_spans": [_scope_span_payload(value) for value in excerpt.included_spans],
            },
        }
        profile_payload = self._profile.to_fingerprint_payload()
        input_fingerprint = classification_input_fingerprint(
            semantic_request,
            profile_payload,
        )
        request = ContextClassificationRequest(
            classification_request_id=_stable_id(
                "classification_request_external",
                input_fingerprint,
            ),
            requested_at=self._now(),
            source=revision.source,
            source_type=revision.source_type,
            source_locator=revision.source_fact_id,
            raw_input_id=raw_input.raw_input_id,
            source_document_id=source_document.source_document_id,
            raw_input_hash=revision.raw_object_hash,
            document_hash=revision.document_hash,
            affected_tickers=list(deterministic_scope.tickers),
            affected_sectors=list(deterministic_scope.sectors),
            global_relevance=deterministic_scope.global_relevance,
            input_text=excerpt.text,
            prompt_version=self._profile.prompt_version,
            response_schema_version=self._profile.response_schema_version,
            excerpt_hash=excerpt.excerpt_hash,
            classification_input_fingerprint=input_fingerprint,
            collected_at=revision.system_observed_at,
            normalized_at=revision.normalized_at or revision.archived_at,
            source_platform=revision.source_platform,
            source_uri=revision.source_uri,
            source_published_at=revision.source_published_at,
            source_updated_at=revision.source_updated_at,
            source_fact_id=revision.source_fact_id,
            source_revision_id=revision.source_revision_id,
            revision_sequence=revision.revision_sequence,
            supersedes_revision_id=revision.supersedes_revision_id,
            lifecycle_state=ContextLifecycleState(revision.lifecycle_state.value),
            lifecycle_effective_at=revision.lifecycle_effective_at,
            source_available_at=revision.source_available_at,
            system_observed_at=revision.system_observed_at,
            archived_at=revision.archived_at,
            trace_id=revision.trace_id,
        )
        self._archive.publish_observation(
            source=revision.source,
            payload={
                "observation_type": "classification_contract_bundle",
                "source_revision_id": revision.source_revision_id,
                "raw_input": _without_text(to_json_dict(raw_input)),
                "source_document": _without_text(to_json_dict(source_document)),
                "classification_request": {
                    **_without_text(to_json_dict(request)),
                    "input_text": None,
                },
                "normalization": {
                    "normalized_text_hash": revision.normalized_text_hash,
                    "normalized_character_count": len(normalized_text),
                    "classification_text_hash": excerpt.full_hash,
                    "full_character_count": excerpt.full_character_count,
                    "excerpt_character_count": excerpt.excerpt_character_count,
                    "full_hash": excerpt.full_hash,
                    "excerpt_hash": excerpt.excerpt_hash,
                    "truncated": excerpt.truncated,
                    "selected_spans": [_scope_span_payload(value) for value in excerpt.included_spans],
                    "excerpt_version": excerpt.excerpt_version,
                    "scope_version": deterministic_scope.resolver_version,
                },
                "profile": profile_payload,
                "profile_hash": self._profile.profile_hash,
                "classification_input_fingerprint": input_fingerprint,
            },
        )
        return PreparedExternalClassification(
            raw_input=raw_input,
            source_document=source_document,
            request=request,
            deterministic_scope=deterministic_scope,
            excerpt=excerpt,
            profile=self._profile,
        )

    def process_revision(
        self,
        revision: ExternalSourceRevision,
        *,
        title: str | None = None,
        earnings: bool = False,
    ) -> ExternalClassificationOutcome:
        prepared = self.prepare(revision, title=title, earnings=earnings)
        if prepared is None:
            return ExternalClassificationOutcome(
                source_revision_id=revision.source_revision_id,
                classification_input_fingerprint=None,
                status="NO_TEXT",
                provider_called=False,
                reused_canonical_result=False,
                policy_eligible=False,
                evidence_ready_at=None,
            )
        fingerprint = prepared.request.classification_input_fingerprint
        if fingerprint is None:  # The request contract already prevents this.
            raise ExternalClassificationError("classification input fingerprint is missing")
        canonical = self._archive.read_canonical_claim(fingerprint)
        provider_called = False
        reused = canonical is not None
        if canonical is None:
            with self._archive.classification_lease(
                fingerprint,
                owner_id=new_record_id("external_classifier"),
            ) as acquired:
                canonical = self._archive.read_canonical_claim(fingerprint)
                if canonical is None and acquired:
                    canonical, recovery_conflict = (
                        self._recover_canonical_attempt_before_provider(
                            revision=revision,
                            prepared=prepared,
                        )
                    )
                    if recovery_conflict:
                        return ExternalClassificationOutcome(
                            source_revision_id=revision.source_revision_id,
                            classification_input_fingerprint=fingerprint,
                            status="CLASSIFICATION_CONFLICT",
                            provider_called=False,
                            reused_canonical_result=False,
                            policy_eligible=False,
                            evidence_ready_at=None,
                        )
                    if canonical is None:
                        result = self._classifier.classify(prepared.request)
                        provider_called = result.response.provider_request_count > 0
                        mismatch_fields = _pinned_profile_mismatch_fields(
                            prepared=prepared,
                            response=result.response,
                            validation=result.validation_result,
                        )
                        if mismatch_fields:
                            self._publish_profile_mismatch_observation(
                                revision=revision,
                                prepared=prepared,
                                mismatch_fields=mismatch_fields,
                                classification_status=result.response.status.value,
                            )
                            return ExternalClassificationOutcome(
                                source_revision_id=revision.source_revision_id,
                                classification_input_fingerprint=fingerprint,
                                status="PROFILE_MISMATCH",
                                provider_called=provider_called,
                                reused_canonical_result=False,
                                policy_eligible=False,
                                evidence_ready_at=None,
                            )
                        canonical = self._publish_attempt(revision, prepared, result)
                    else:
                        reused = True
                elif canonical is None:
                    return ExternalClassificationOutcome(
                        source_revision_id=revision.source_revision_id,
                        classification_input_fingerprint=fingerprint,
                        status="PENDING_CANONICAL_OWNER",
                        provider_called=False,
                        reused_canonical_result=False,
                        policy_eligible=False,
                        evidence_ready_at=None,
                    )
                else:
                    reused = True
        if canonical is None:
            # Provider/validation failures are deliberately retryable and never
            # claim completion.
            attempts = self._archive.iter_classification_attempts(fingerprint)
            status = str(attempts[-1].get("status", "PENDING")) if attempts else "PENDING"
            return ExternalClassificationOutcome(
                source_revision_id=revision.source_revision_id,
                classification_input_fingerprint=fingerprint,
                status=status,
                provider_called=provider_called,
                reused_canonical_result=False,
                policy_eligible=False,
                evidence_ready_at=None,
            )
        outcome = self._materialize_canonical(
            revision=revision,
            prepared=prepared,
            canonical=canonical,
            provider_called=provider_called,
            reused=reused,
        )
        return outcome

    def _recover_canonical_attempt_before_provider(
        self,
        *,
        revision: ExternalSourceRevision,
        prepared: PreparedExternalClassification,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Adopt a validated attempt left by a crash before canonical claim."""

        fingerprint = prepared.request.classification_input_fingerprint
        if fingerprint is None:
            raise ExternalClassificationError("classification fingerprint is missing")
        candidates: list[dict[str, Any]] = []
        for attempt in self._archive.iter_classification_attempts(fingerprint):
            if (
                attempt.get("durably_published") is not True
                or attempt.get("validation_outcome") is not True
                or attempt.get("status")
                not in {
                    ContextClassificationStatus.VALID.value,
                    ContextClassificationStatus.ABSTAINED.value,
                }
                or _pinned_profile_mismatch_fields(
                    prepared=prepared,
                    attempt=attempt,
                )
                or attempt.get("classification_input_fingerprint") != fingerprint
                or attempt.get("document_hash") != prepared.request.document_hash
                or attempt.get("normalized_text_hash")
                != revision.normalized_text_hash
                or attempt.get("classification_text_hash")
                != prepared.excerpt.full_hash
                or attempt.get("excerpt_hash") != prepared.excerpt.excerpt_hash
                or not isinstance(attempt.get("normalized_output"), Mapping)
            ):
                continue
            try:
                parse_utc_iso(str(attempt["classified_at"]))
                parse_utc_iso(str(attempt["validated_at"]))
                parse_utc_iso(str(attempt["archive_published_at"]))
                complete_hash = str(attempt["complete_output_fingerprint"])
                policy_hash = str(attempt["policy_output_fingerprint"])
                if len(complete_hash) != 64 or len(policy_hash) != 64:
                    raise ValueError("invalid output hash")
            except (KeyError, ValueError):
                continue
            candidates.append(attempt)
        if not candidates:
            return None, False
        output_identities = {
            (
                str(value["complete_output_fingerprint"]),
                str(value["policy_output_fingerprint"]),
            )
            for value in candidates
        }
        if len(output_identities) != 1:
            self._archive.detect_classification_conflict(fingerprint)
            return None, True
        candidates.sort(
            key=lambda value: (
                str(value["archive_published_at"]),
                str(value["classification_attempt_id"]),
            )
        )
        chosen = candidates[0]
        canonical = self._archive.claim_canonical_result(
            classification_input_fingerprint=fingerprint,
            attempt_id=str(chosen["classification_attempt_id"]),
            complete_output_fingerprint=str(
                chosen["complete_output_fingerprint"]
            ),
            policy_output_fingerprint=str(chosen["policy_output_fingerprint"]),
            profile_hash=prepared.profile.profile_hash,
            evidence_ready_at=max(
                self._now(),
                revision.system_observed_at,
                revision.archived_at,
                revision.normalized_at or revision.archived_at,
                parse_utc_iso(str(chosen["classified_at"])),
                parse_utc_iso(str(chosen["validated_at"])),
                parse_utc_iso(str(chosen["archive_published_at"])),
            ),
        )
        return canonical, False

    def _publish_attempt(
        self,
        revision: ExternalSourceRevision,
        prepared: PreparedExternalClassification,
        result: ContextClassificationAttemptResult,
    ) -> dict[str, Any] | None:
        response = result.response
        validation = result.validation_result
        mismatch_fields = _pinned_profile_mismatch_fields(
            prepared=prepared,
            response=response,
            validation=validation,
        )
        if mismatch_fields:
            raise ExternalClassificationError(
                "classifier result does not match the pinned profile"
            )
        output = _normalized_response_output(response)
        if response.status in {
            ContextClassificationStatus.VALID,
            ContextClassificationStatus.ABSTAINED,
        } and (validation is None or not validation.validation_outcome):
            raise ExternalClassificationError("completed classification lacks validation")
        if response.status is ContextClassificationStatus.VALID:
            tickers, sectors, global_relevance = merge_classification_scope(
                prepared.request,
                response,
            )
        else:
            tickers, sectors, global_relevance = ([], [], False)
        policy_output = {
            **output,
            "affected_tickers": tickers,
            "affected_sectors": sectors,
            "global_relevance": global_relevance,
        }
        complete_output_hash, policy_output_hash = output_fingerprints(
            output,
            policy_output=policy_output,
        )
        attempt_payload = {
            "classification_attempt_id": response.classification_attempt_id,
            "classification_request_id": response.classification_request_id,
            "classification_input_fingerprint": prepared.request.classification_input_fingerprint,
            "profile_hash": prepared.profile.profile_hash,
            "profile": prepared.profile.to_fingerprint_payload(),
            "document_hash": prepared.request.document_hash,
            "normalized_text_hash": revision.normalized_text_hash,
            "classification_text_hash": prepared.excerpt.full_hash,
            "excerpt_hash": prepared.excerpt.excerpt_hash,
            "trusted_input_scope": {
                "affected_tickers": list(prepared.request.affected_tickers),
                "affected_sectors": list(prepared.request.affected_sectors),
                "global_relevance": prepared.request.global_relevance,
            },
            "status": response.status.value,
            "classified_at": to_utc_iso(response.classified_at),
            "validated_at": None if validation is None else to_utc_iso(validation.validated_at),
            "validation_outcome": None if validation is None else validation.validation_outcome,
            "validator_version": None if validation is None else validation.validator_version,
            "complete_output_fingerprint": complete_output_hash,
            "policy_output_fingerprint": policy_output_hash,
            "normalized_output": policy_output,
            "provider_request_count": response.provider_request_count,
            "deduplicated_in_process": response.deduplicated,
            "durably_published": True,
            "classification_origin": revision.collection_mode,
            "safe_failure_category": response.safe_failure_category,
        }
        fingerprint = prepared.request.classification_input_fingerprint
        if fingerprint is None:
            raise ExternalClassificationError("classification fingerprint is missing")
        self._archive.publish_classification_attempt(
            classification_input_fingerprint=fingerprint,
            attempt_id=response.classification_attempt_id,
            payload=attempt_payload,
        )
        if response.status not in {
            ContextClassificationStatus.VALID,
            ContextClassificationStatus.ABSTAINED,
        }:
            return None
        if validation is None or not validation.validation_outcome:
            return None
        return self._archive.claim_canonical_result(
            classification_input_fingerprint=fingerprint,
            attempt_id=response.classification_attempt_id,
            complete_output_fingerprint=complete_output_hash,
            policy_output_fingerprint=policy_output_hash,
            profile_hash=prepared.profile.profile_hash,
            evidence_ready_at=max(
                self._now(),
                revision.system_observed_at,
                revision.archived_at,
                revision.normalized_at or revision.archived_at,
                response.classified_at,
                validation.validated_at,
            ),
        )

    def _materialize_canonical(
        self,
        *,
        revision: ExternalSourceRevision,
        prepared: PreparedExternalClassification,
        canonical: Mapping[str, Any],
        provider_called: bool,
        reused: bool,
    ) -> ExternalClassificationOutcome:
        fingerprint = prepared.request.classification_input_fingerprint
        if fingerprint is None:
            raise ExternalClassificationError("classification fingerprint is missing")
        attempt_id = str(canonical.get("canonical_classification_attempt_id", ""))
        attempts = self._archive.iter_classification_attempts(fingerprint)
        attempt = next(
            (value for value in attempts if value.get("classification_attempt_id") == attempt_id),
            None,
        )
        if attempt is None:
            raise ExternalClassificationError("canonical classification attempt is missing")
        if _pinned_profile_mismatch_fields(
            prepared=prepared,
            attempt=attempt,
            canonical=canonical,
        ):
            raise ExternalClassificationError(
                "canonical classification profile does not match this materialization"
            )
        conflict = self._archive.detect_classification_conflict(fingerprint)
        if conflict is not None:
            resolution_status = self._reviewed_conflict_materialization_status(
                fingerprint=fingerprint,
                canonical=canonical,
                conflict=conflict,
                profile=prepared.profile,
            )
            if resolution_status is not None:
                return ExternalClassificationOutcome(
                    source_revision_id=revision.source_revision_id,
                    classification_input_fingerprint=fingerprint,
                    status=resolution_status,
                    provider_called=provider_called,
                    reused_canonical_result=reused,
                    policy_eligible=False,
                    evidence_ready_at=None,
                )
        status = str(attempt.get("status"))
        output = attempt.get("normalized_output")
        if not isinstance(output, Mapping):
            raise ExternalClassificationError("canonical classification output is malformed")
        context_event: ContextAIEvent | None = None
        policy_eligible = status == ContextClassificationStatus.VALID.value and _has_scope(output)
        if status == ContextClassificationStatus.VALID.value:
            context_event = _materialized_event(
                revision=revision,
                prepared=prepared,
                attempt=attempt,
                canonical=canonical,
                output=output,
                policy_eligible=policy_eligible,
            )
            self._archive.publish_materialized_event(
                source_revision_id=revision.source_revision_id,
                classification_input_fingerprint=fingerprint,
                payload=to_json_dict(context_event),
            )
        readiness = self._archive.read_readiness(
            revision.source_revision_id,
            classification_input_fingerprint=fingerprint,
        )
        if readiness is None:
            readiness = self._archive.publish_readiness(
                source_revision_id=revision.source_revision_id,
                classification_input_fingerprint=fingerprint,
                canonical_classification_attempt_id=attempt_id,
                complete_output_fingerprint=str(canonical["complete_output_fingerprint"]),
                policy_output_fingerprint=str(canonical["policy_output_fingerprint"]),
                profile_hash=prepared.profile.profile_hash,
                classification_profile=prepared.profile.to_fingerprint_payload(),
                classification_status=status,
                policy_eligible=policy_eligible,
                context_event=None,
                evidence_ready_at=max(
                    self._now(),
                    revision.system_observed_at,
                    revision.archived_at,
                    revision.normalized_at or revision.archived_at,
                    parse_utc_iso(str(attempt["classified_at"])),
                    parse_utc_iso(str(attempt["validated_at"])),
                ),
            )
        else:
            expected_readiness = {
                "canonical_classification_attempt_id": attempt_id,
                "complete_output_fingerprint": str(canonical["complete_output_fingerprint"]),
                "policy_output_fingerprint": str(canonical["policy_output_fingerprint"]),
                "profile_hash": prepared.profile.profile_hash,
                "classification_status": status,
                "policy_eligible": policy_eligible,
            }
            if any(
                readiness.get(name) != value
                for name, value in expected_readiness.items()
            ):
                raise ExternalClassificationError(
                    "durable readiness differs from its canonical result"
                )
        ready_at = parse_utc_iso(str(readiness["evidence_ready_at"]))
        if context_event is not None:
            context_event = replace(
                context_event,
                available_at=ready_at,
                evidence_ready_at=ready_at,
            )
        # QuestDB is explicitly optional and happens after reusable durable
        # classification state.  Its failure can never cause another AI call.
        if self._questdb_writer is not None and context_event is not None:
            self._questdb_writer.write(context_event)
        return ExternalClassificationOutcome(
            source_revision_id=revision.source_revision_id,
            classification_input_fingerprint=fingerprint,
            status=status,
            provider_called=provider_called,
            reused_canonical_result=reused,
            policy_eligible=policy_eligible,
            evidence_ready_at=ready_at,
            context_event=context_event,
        )

    def _reviewed_conflict_materialization_status(
        self,
        *,
        fingerprint: str,
        canonical: Mapping[str, Any],
        conflict: Mapping[str, Any],
        profile: ResearchSourceClassificationProfile,
    ) -> str | None:
        """Return a fail-closed status unless review authorizes this owner.

        A replacement profile has a distinct classification-input fingerprint,
        so an old conflicted profile can never silently consume it.  The normal
        canonical/profile checks above govern the replacement input itself.
        """

        resolution = self._archive.load_conflict_resolution(fingerprint)
        if resolution is None:
            return "CLASSIFICATION_CONFLICT"
        if resolution.get("conflict_id") != conflict.get(
            "classification_conflict_id"
        ):
            raise ExternalClassificationError(
                "classification resolution references a different conflict"
            )
        decision = str(resolution.get("decision"))
        if decision == "ABSTAIN_INPUT":
            return "ABSTAIN_INPUT"
        if decision == "KEEP_FIRST_DURABLY_PUBLISHED":
            required_matches = (
                (
                    "chosen_attempt_id",
                    "canonical_classification_attempt_id",
                ),
                (
                    "chosen_complete_output_fingerprint",
                    "complete_output_fingerprint",
                ),
                (
                    "chosen_policy_output_fingerprint",
                    "policy_output_fingerprint",
                ),
            )
            if any(
                resolution.get(resolution_name) != canonical.get(canonical_name)
                for resolution_name, canonical_name in required_matches
            ):
                raise ExternalClassificationError(
                    "KEEP_FIRST resolution does not authorize the canonical result"
                )
            if canonical.get("profile_hash") != profile.profile_hash:
                raise ExternalClassificationError(
                    "KEEP_FIRST resolution profile does not match materialization"
                )
            return None
        if decision == "RECLASSIFY_UNDER_NEW_PROFILE":
            if resolution.get("new_profile_hash") != profile.profile_hash:
                return "RECLASSIFY_UNDER_NEW_PROFILE_REQUIRED"
            if canonical.get("profile_hash") != profile.profile_hash:
                raise ExternalClassificationError(
                    "replacement profile does not own the canonical result"
                )
            return None
        raise ExternalClassificationError("classification resolution decision is invalid")

    def _publish_profile_mismatch_observation(
        self,
        *,
        revision: ExternalSourceRevision,
        prepared: PreparedExternalClassification,
        mismatch_fields: tuple[str, ...],
        classification_status: str,
    ) -> None:
        """Persist only safe mismatch metadata; never retain provider output."""

        self._archive.publish_observation(
            source=revision.source,
            payload={
                "observation_type": "classification_profile_mismatch",
                "source_revision_id": revision.source_revision_id,
                "classification_input_fingerprint": (
                    prepared.request.classification_input_fingerprint
                ),
                "profile_hash": prepared.profile.profile_hash,
                "classification_status": classification_status,
                "mismatch_fields": list(mismatch_fields),
            },
        )

    def _validate_revision_profile(self, revision: ExternalSourceRevision) -> None:
        if revision.source != self._profile.source or revision.source_type != self._profile.source_type:
            raise ExternalClassificationError("revision does not match the source profile")
        if (
            self._profile.ticker is not None
            and revision.affected_tickers != (self._profile.ticker,)
        ):
            raise ExternalClassificationError(
                "ticker-owned profile does not own this source revision"
            )
        expected = {
            "adapter": self._profile.semantic_adapter_version,
            "extractor": self._profile.extraction_version,
            "normalizer": self._profile.normalization_version,
        }
        actual = {
            "adapter": revision.adapter_version,
            "extractor": revision.extractor_version,
            "normalizer": revision.normalizer_version,
        }
        if expected != actual:
            raise ExternalClassificationError("revision versions do not match the pinned profile")


def _materialized_event(
    *,
    revision: ExternalSourceRevision,
    prepared: PreparedExternalClassification,
    attempt: Mapping[str, Any],
    canonical: Mapping[str, Any],
    output: Mapping[str, Any],
    policy_eligible: bool,
) -> ContextAIEvent:
    classified_at = parse_utc_iso(str(attempt["classified_at"]))
    validated_at = parse_utc_iso(str(attempt["validated_at"]))
    source_time = revision.source_published_at or revision.source_available_at
    return ContextAIEvent(
        context_event_id=_stable_id(
            "context_event_external",
            revision.source,
            revision.source_revision_id,
            str(prepared.request.classification_input_fingerprint),
        ),
        event_time=source_time or revision.system_observed_at,
        source=revision.source,
        source_id=revision.source_fact_id,
        affected_tickers=list(output.get("affected_tickers", [])),
        affected_sectors=list(output.get("affected_sectors", [])),
        global_relevance=bool(output.get("global_relevance", False)),
        event_type=ContextClassificationEventType(str(output["event_type"])),
        urgency=ContextUrgency(str(output["urgency"])),
        risk_level=ContextRiskLevel(str(output["risk_level"])),
        confidence=float(output["confidence"]),
        summary=str(output["summary"]),
        prompt_version=prepared.profile.prompt_version,
        model_version=prepared.profile.model_version,
        raw_input_hash=revision.raw_object_hash,
        raw_input_id=prepared.raw_input.raw_input_id,
        source_document_id=prepared.source_document.source_document_id,
        classification_request_id=str(attempt["classification_request_id"]),
        classification_attempt_id=str(attempt["classification_attempt_id"]),
        validation_result_id=_stable_id(
            "validation_result_external",
            str(attempt["classification_attempt_id"]),
        ),
        source_type=revision.source_type,
        source_platform=revision.source_platform,
        source_uri=revision.source_uri,
        source_locator=revision.source_fact_id,
        document_hash=revision.document_hash,
        source_published_at=revision.source_published_at,
        source_updated_at=revision.source_updated_at,
        collected_at=revision.system_observed_at,
        normalized_at=revision.normalized_at or revision.archived_at,
        classified_at=classified_at,
        # This immutable event payload is published before the authoritative
        # per-revision readiness receipt.  Leave compatibility availability
        # unset here so reusing an older canonical classification can never
        # transfer that earlier attempt's readiness to a later observation.
        # The returned event and PR37 hydration overlay the final receipt time.
        available_at=None,
        validated_at=validated_at,
        provider="gemini",
        source_available_at=revision.source_available_at,
        system_observed_at=revision.system_observed_at,
        archived_at=revision.archived_at,
        source_fact_id=revision.source_fact_id,
        source_revision_id=revision.source_revision_id,
        revision_sequence=revision.revision_sequence,
        supersedes_revision_id=revision.supersedes_revision_id,
        lifecycle_state=ContextLifecycleState(revision.lifecycle_state.value),
        lifecycle_effective_at=revision.lifecycle_effective_at,
        classification_input_fingerprint=prepared.request.classification_input_fingerprint,
        complete_output_fingerprint=str(canonical["complete_output_fingerprint"]),
        policy_output_fingerprint=str(canonical["policy_output_fingerprint"]),
        canonical_classification_attempt_id=str(canonical["canonical_classification_attempt_id"]),
        correlation_group_id=revision.correlation_group_id,
        relationship_types=list(revision.relationship_types),
        trace_id=revision.trace_id,
    )


def _pinned_profile_mismatch_fields(
    *,
    prepared: PreparedExternalClassification,
    response: ContextClassificationResponse | None = None,
    validation: ContextValidationResult | None = None,
    attempt: Mapping[str, Any] | None = None,
    canonical: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Return exact pinned-profile disagreements without trusting injection."""

    profile = prepared.profile
    mismatches: list[str] = []
    if response is not None:
        for field_name, actual, expected in (
            ("response.model_version", response.model_version, profile.model_version),
            ("response.prompt_version", response.prompt_version, profile.prompt_version),
            (
                "response.response_schema_version",
                response.response_schema_version,
                profile.response_schema_version,
            ),
        ):
            if actual != expected:
                mismatches.append(field_name)
    if validation is not None and validation.validator_version != profile.validator_version:
        mismatches.append("validation.validator_version")
    if attempt is not None:
        if attempt.get("profile_hash") != profile.profile_hash:
            mismatches.append("attempt.profile_hash")
        if attempt.get("profile") != profile.to_fingerprint_payload():
            mismatches.append("attempt.profile")
        if (
            attempt.get("status")
            in {
                ContextClassificationStatus.VALID.value,
                ContextClassificationStatus.ABSTAINED.value,
            }
            and attempt.get("validator_version") != profile.validator_version
        ):
            mismatches.append("attempt.validator_version")
    if canonical is not None and canonical.get("profile_hash") != profile.profile_hash:
        mismatches.append("canonical.profile_hash")
    return tuple(sorted(set(mismatches)))


def _normalized_response_output(response: ContextClassificationResponse) -> dict[str, Any]:
    return {
        "status": response.status.value,
        "event_type": response.event_type.value,
        "risk_level": response.risk_level.value,
        "urgency": response.urgency.value,
        "confidence": response.confidence,
        "summary": response.summary,
        "affected_tickers": list(response.affected_tickers),
        "affected_sectors": list(response.affected_sectors),
        "global_relevance": response.global_relevance,
    }


def _has_scope(output: Mapping[str, Any]) -> bool:
    return bool(
        output.get("global_relevance")
        or output.get("affected_tickers")
        or output.get("affected_sectors")
    )


def _scope_span_payload(value: object) -> dict[str, Any]:
    return {
        "kind": getattr(value, "kind"),
        "value": getattr(value, "value"),
        "alias": getattr(value, "alias"),
        "start": getattr(value, "start"),
        "end": getattr(value, "end"),
    }


def _without_text(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExternalClassificationError("contract serialization is invalid")
    copied = dict(value)
    copied.pop("input_text", None)
    return copied


def canonical_manifest_hash(value: Mapping[str, Any]) -> str:
    return sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _stable_id(prefix: str, *parts: object) -> str:
    encoded = json.dumps(
        [str(value) for value in parts],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"{prefix}_{sha256(encoded).hexdigest()}"


__all__ = [
    "ExternalClassificationError",
    "ExternalClassificationOutcome",
    "ExternalClassificationPipeline",
    "PreparedExternalClassification",
    "canonical_manifest_hash",
]
