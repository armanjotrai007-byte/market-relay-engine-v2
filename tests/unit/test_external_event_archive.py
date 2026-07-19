from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json

import pytest

from market_relay_engine.context.external_event_archive import (
    ConflictResolutionDecision,
    CoverageStatus,
    ExternalEventArchive,
    ExternalEventArchiveError,
    ExternalSourceRevision,
    LifecycleState,
    SourceCoverage,
    classification_input_fingerprint,
    output_fingerprints,
    source_revision_id,
)


T0 = datetime(2026, 7, 18, 16, 0, tzinfo=UTC)
SHA_A = sha256(b"a").hexdigest()
SHA_B = sha256(b"b").hexdigest()
SHA_C = sha256(b"c").hexdigest()


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = list(values)
        self._last = values[-1]

    def __call__(self) -> datetime:
        if self._values:
            self._last = self._values.pop(0)
        return self._last


def _revision(
    *,
    content_hash: str = SHA_A,
    revision_id: str = "revision-one",
    sequence: int = 1,
    observed_at: datetime = T0,
    supersedes: str | None = None,
) -> ExternalSourceRevision:
    return ExternalSourceRevision(
        source="veritawire_truth_social",
        source_fact_id="truth-123",
        source_revision_id=revision_id,
        revision_sequence=sequence,
        supersedes_revision_id=supersedes,
        lifecycle_state=(
            LifecycleState.ACTIVE if sequence == 1 else LifecycleState.UPDATED
        ),
        lifecycle_effective_at=observed_at,
        system_observed_at=observed_at,
        source_available_at=observed_at - timedelta(seconds=10),
        archived_at=observed_at + timedelta(milliseconds=20),
        raw_object_hash=content_hash,
        document_hash=content_hash,
        normalized_text_hash=content_hash,
        canonical_content_hash=content_hash,
        source_type="social_post",
    )


def _attempt(
    *,
    attempt_id: str,
    input_fingerprint: str,
    complete_output_fingerprint: str,
    policy_output_fingerprint: str,
    profile_hash: str,
    archived_at: datetime,
    summary: str,
) -> dict[str, object]:
    return {
        "classification_attempt_id": attempt_id,
        "classification_input_fingerprint": input_fingerprint,
        "profile_hash": profile_hash,
        "status": "VALID",
        "validation_outcome": True,
        "durably_published": True,
        "complete_output_fingerprint": complete_output_fingerprint,
        "policy_output_fingerprint": policy_output_fingerprint,
        "classification_origin": (
            "LIVE_SYSTEM" if attempt_id == "live-attempt" else "BACKFILL"
        ),
        "normalized_output": {
            "status": "VALID",
            "event_type": "OTHER",
            "risk_level": "LOW",
            "urgency": "LOW",
            "confidence": 0.7,
            "summary": summary,
            "affected_tickers": ["PLTR"],
            "affected_sectors": [],
            "global_relevance": False,
        },
    }


def _publish_conflicting_attempts(
    archive: ExternalEventArchive,
) -> tuple[str, str, str, dict[str, object]]:
    input_fingerprint = sha256(b"input").hexdigest()
    profile_hash = sha256(b"profile").hexdigest()
    first_complete, first_policy = output_fingerprints(
        {
            "status": "VALID",
            "event_type": "OTHER",
            "risk_level": "LOW",
            "urgency": "LOW",
            "confidence": 0.7,
            "summary": "first",
            "affected_tickers": ["PLTR"],
            "affected_sectors": [],
            "global_relevance": False,
        }
    )
    second_complete, second_policy = output_fingerprints(
        {
            "status": "VALID",
            "event_type": "LEGAL",
            "risk_level": "HIGH",
            "urgency": "HIGH",
            "confidence": 0.9,
            "summary": "second",
            "affected_tickers": ["PLTR"],
            "affected_sectors": [],
            "global_relevance": False,
        }
    )
    archive.publish_classification_attempt(
        classification_input_fingerprint=input_fingerprint,
        attempt_id="live-attempt",
        payload=_attempt(
            attempt_id="live-attempt",
            input_fingerprint=input_fingerprint,
            complete_output_fingerprint=first_complete,
            policy_output_fingerprint=first_policy,
            profile_hash=profile_hash,
            archived_at=T0,
            summary="first",
        ),
    )
    canonical = archive.claim_canonical_result(
        classification_input_fingerprint=input_fingerprint,
        attempt_id="live-attempt",
        complete_output_fingerprint=first_complete,
        policy_output_fingerprint=first_policy,
        profile_hash=profile_hash,
        evidence_ready_at=T0 + timedelta(seconds=1),
    )
    archive.publish_classification_attempt(
        classification_input_fingerprint=input_fingerprint,
        attempt_id="backfill-attempt",
        payload=_attempt(
            attempt_id="backfill-attempt",
            input_fingerprint=input_fingerprint,
            complete_output_fingerprint=second_complete,
            policy_output_fingerprint=second_policy,
            profile_hash=profile_hash,
            archived_at=T0 + timedelta(seconds=10),
            summary="second",
        ),
    )
    conflict = archive.detect_classification_conflict(input_fingerprint)
    assert conflict is not None
    return input_fingerprint, profile_hash, first_complete, canonical


def test_content_objects_and_revisions_are_immutable_and_content_addressed(
    tmp_path,
) -> None:
    archive = ExternalEventArchive(tmp_path)
    payload = b'{"post_id":"truth-123","content":"original"}'
    digest = archive.archive_object(
        payload,
        extension="json",
        content_type="application/json",
    )

    assert digest == sha256(payload).hexdigest()
    assert archive.read_object(digest, filename="original.json") == payload
    assert archive.archive_object(
        payload,
        extension="json",
        content_type="application/json",
    ) == digest
    assert archive.archive_object(payload, extension="txt") == digest

    first = _revision()
    archive.publish_revision(first)
    archive.publish_revision(first)
    assert tuple(archive.iter_revisions()) == (first,)

    with pytest.raises(
        ExternalEventArchiveError,
        match="immutable source revision identity changed",
    ):
        archive.publish_revision(
            _revision(content_hash=SHA_B, revision_id=first.source_revision_id)
        )


def test_failed_revision_publication_does_not_advance_checkpoint(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = ExternalEventArchive(tmp_path)

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise ExternalEventArchiveError("simulated archive failure")

    monkeypatch.setattr(archive, "_write_json_once", fail_write)
    with pytest.raises(ExternalEventArchiveError, match="simulated archive failure"):
        archive.publish_revision(_revision())

    assert archive.get_checkpoint("veritawire_truth_social") is None


def test_restart_adopts_revision_published_before_manifest_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = ExternalEventArchive(tmp_path, now=lambda: T0)
    first = _revision(observed_at=T0)

    def fail_manifest(_manifest: object) -> None:
        raise ExternalEventArchiveError("simulated manifest replace failure")

    monkeypatch.setattr(archive, "save_manifest", fail_manifest)
    with pytest.raises(
        ExternalEventArchiveError,
        match="simulated manifest replace failure",
    ):
        archive.publish_revision(first)

    restarted = ExternalEventArchive(
        tmp_path,
        now=lambda: T0 + timedelta(seconds=5),
    )
    replay = _revision(observed_at=T0 + timedelta(seconds=5))
    adopted = restarted.publish_revision(replay)

    assert adopted == first
    assert tuple(restarted.iter_revisions()) == (first,)


def test_restart_reclaims_manifest_lock_owned_by_dead_process(tmp_path) -> None:
    archive = ExternalEventArchive(tmp_path, now=lambda: T0)
    lock_dir = tmp_path / "locks" / "external-events.lock"
    lock_dir.mkdir(parents=True)
    (lock_dir / "owner.json").write_text(
        json.dumps(
            {
                "pid": 2_147_483_647,
                "owner_token": "abandoned-owner",
                "acquired_at": T0.isoformat(),
            }
        ),
        encoding="utf-8",
    )

    generation = archive.update_checkpoint("veritawire_truth_social", {"id": "1"})

    assert generation == 1
    assert not lock_dir.exists()


def test_exact_duplicate_revision_identity_is_stable_but_changed_content_revises() -> None:
    first = source_revision_id(
        source="veritawire_truth_social",
        source_fact_id="truth-123",
        canonical_content_hash=SHA_A,
        lifecycle_state=LifecycleState.ACTIVE,
        adapter_version="veritawire_adapter_v1",
    )
    replay = source_revision_id(
        source="veritawire_truth_social",
        source_fact_id="truth-123",
        canonical_content_hash=SHA_A,
        lifecycle_state=LifecycleState.ACTIVE,
        adapter_version="veritawire_adapter_v1",
    )
    edited = source_revision_id(
        source="veritawire_truth_social",
        source_fact_id="truth-123",
        canonical_content_hash=SHA_B,
        lifecycle_state=LifecycleState.UPDATED,
        adapter_version="veritawire_adapter_v1",
    )

    assert replay == first
    assert edited != first


def test_superseding_revision_cannot_move_lifecycle_effective_time_backward(
    tmp_path,
) -> None:
    archive = ExternalEventArchive(tmp_path)
    first = _revision(observed_at=T0)
    archive.publish_revision(first)
    updated = _revision(
        content_hash=SHA_B,
        revision_id="revision-two",
        sequence=2,
        observed_at=T0 + timedelta(seconds=10),
        supersedes=first.source_revision_id,
    )
    invalid = replace(
        updated,
        lifecycle_effective_at=T0 - timedelta(seconds=1),
    )

    with pytest.raises(ExternalEventArchiveError, match="lifecycle time moved backwards"):
        archive.publish_revision(invalid)


def test_classification_input_fingerprint_is_semantic_and_excludes_runtime_identity() -> None:
    semantic_request = {
        "source": "palantir_ir",
        "document_hash": SHA_A,
        "normalized_text_hash": SHA_B,
        "excerpt_hash": SHA_C,
        "trusted_scope": {
            "affected_tickers": ["PLTR"],
            "affected_sectors": [],
            "global_relevance": False,
        },
    }
    profile = {
        "prompt_version": "context_filter_v2_scope",
        "model_version": "gemini-test",
        "validator_version": "context_filter_validator_v2_scope",
    }

    first = classification_input_fingerprint(dict(semantic_request), dict(profile))
    assert first == classification_input_fingerprint(
        dict(reversed(list(semantic_request.items()))),
        dict(reversed(list(profile.items()))),
    )

    for forbidden_name, forbidden_value in (
        ("classification_request_id", "request-1"),
        ("classification_attempt_id", "attempt-1"),
        ("requested_at", T0.isoformat()),
        ("classified_at", T0.isoformat()),
        ("validated_at", T0.isoformat()),
        ("provider_latency_ms", 4.2),
        ("output", {"summary": "generated"}),
    ):
        with pytest.raises(
            ExternalEventArchiveError,
            match="non-semantic identity fields",
        ):
            classification_input_fingerprint(
                {**semantic_request, forbidden_name: forbidden_value}, profile
            )


def test_first_writer_canonical_claim_cannot_be_overwritten(tmp_path) -> None:
    archive = ExternalEventArchive(tmp_path, now=lambda: T0)
    input_fingerprint = SHA_A
    first = archive.claim_canonical_result(
        classification_input_fingerprint=input_fingerprint,
        attempt_id="first",
        complete_output_fingerprint=SHA_B,
        policy_output_fingerprint=SHA_C,
        profile_hash=sha256(b"profile").hexdigest(),
        evidence_ready_at=T0,
    )
    second = archive.claim_canonical_result(
        classification_input_fingerprint=input_fingerprint,
        attempt_id="later",
        complete_output_fingerprint=SHA_C,
        policy_output_fingerprint=SHA_B,
        profile_hash=sha256(b"profile").hexdigest(),
        evidence_ready_at=T0 + timedelta(minutes=1),
    )

    assert second == first
    assert second["canonical_classification_attempt_id"] == "first"
    assert second["evidence_ready_at"] == "2026-07-18T16:00:00Z"


def test_contradictory_import_creates_safe_classification_conflict(tmp_path) -> None:
    archive = ExternalEventArchive(
        tmp_path,
        now=SequenceClock(
            T0 + timedelta(seconds=1),
            T0 + timedelta(seconds=20),
        ),
    )
    input_fingerprint, profile_hash, _, _ = _publish_conflicting_attempts(archive)
    conflict = next(tmp_path.joinpath("conflicts").glob("*.json"))
    raw_conflict = conflict.read_text(encoding="utf-8")
    detected = archive.load_conflict_resolution(input_fingerprint)

    assert detected is None
    assert "live-attempt" in raw_conflict
    assert "backfill-attempt" in raw_conflict
    assert profile_hash in raw_conflict
    assert "first" not in raw_conflict
    assert "second" not in raw_conflict


def test_policy_only_output_contradiction_creates_conflict(tmp_path) -> None:
    archive = ExternalEventArchive(tmp_path, now=lambda: T0)
    input_fingerprint = sha256(b"policy-only-input").hexdigest()
    profile_hash = sha256(b"policy-only-profile").hexdigest()
    complete_hash = sha256(b"same-complete-output").hexdigest()
    for attempt_id, policy_hash in (
        ("live-attempt", sha256(b"policy-a").hexdigest()),
        ("backfill-attempt", sha256(b"policy-b").hexdigest()),
    ):
        archive.publish_classification_attempt(
            classification_input_fingerprint=input_fingerprint,
            attempt_id=attempt_id,
            payload=_attempt(
                attempt_id=attempt_id,
                input_fingerprint=input_fingerprint,
                complete_output_fingerprint=complete_hash,
                policy_output_fingerprint=policy_hash,
                profile_hash=profile_hash,
                archived_at=T0,
                summary="same generated output",
            ),
        )

    conflict = archive.detect_classification_conflict(input_fingerprint)

    assert conflict is not None
    assert conflict["complete_output_fingerprints"] == [complete_hash]
    assert len(conflict["policy_output_fingerprints"]) == 2


def test_conflict_detection_is_idempotent_and_preserves_first_detection_time(
    tmp_path,
) -> None:
    archive = ExternalEventArchive(
        tmp_path,
        now=SequenceClock(
            T0 + timedelta(seconds=1),
            T0 + timedelta(seconds=20),
            T0 + timedelta(hours=1),
        ),
    )
    input_fingerprint, _, _, _ = _publish_conflicting_attempts(archive)
    first_conflict_path = next(tmp_path.joinpath("conflicts").glob("*.json"))
    first_bytes = first_conflict_path.read_bytes()

    repeated = archive.detect_classification_conflict(input_fingerprint)

    assert repeated is not None
    assert first_conflict_path.read_bytes() == first_bytes
    assert repeated["detected_at"] == json.loads(first_bytes)["detected_at"]


def test_keep_first_requires_proven_live_chronology(tmp_path) -> None:
    archive = ExternalEventArchive(
        tmp_path,
        now=SequenceClock(
            *(T0 + timedelta(seconds=value) for value in range(1, 12)),
        ),
    )
    input_fingerprint, _, first_output, canonical = _publish_conflicting_attempts(
        archive
    )
    conflict_id = next(tmp_path.joinpath("conflicts").glob("*.json")).stem
    resolution = archive.publish_conflict_resolution(
        conflict_id=conflict_id,
        decision=ConflictResolutionDecision.KEEP_FIRST_DURABLY_PUBLISHED,
        reviewer="research-reviewer",
        reason="Archive chronology proves the live result owned the input first.",
        chosen_attempt_id="live-attempt",
        chosen_complete_output_fingerprint=first_output,
        chosen_policy_output_fingerprint=str(
            canonical["policy_output_fingerprint"]
        ),
    )

    assert resolution["decision"] == "KEEP_FIRST_DURABLY_PUBLISHED"
    assert resolution["chosen_attempt_id"] == canonical[
        "canonical_classification_attempt_id"
    ]
    assert archive.load_conflict_resolution(input_fingerprint) == resolution

    with pytest.raises(ExternalEventArchiveError, match="proven canonical result"):
        archive.publish_conflict_resolution(
            conflict_id=conflict_id,
            decision=ConflictResolutionDecision.KEEP_FIRST_DURABLY_PUBLISHED,
            reviewer="research-reviewer",
            reason="Wrong result must not replace the live owner.",
            chosen_attempt_id="backfill-attempt",
            chosen_complete_output_fingerprint=SHA_A,
            chosen_policy_output_fingerprint=SHA_A,
        )

    with pytest.raises(ExternalEventArchiveError, match="proven canonical result"):
        archive.publish_conflict_resolution(
            conflict_id=conflict_id,
            decision=ConflictResolutionDecision.KEEP_FIRST_DURABLY_PUBLISHED,
            reviewer="research-reviewer",
            reason="A policy hash mismatch cannot retain the canonical output.",
            chosen_attempt_id="live-attempt",
            chosen_complete_output_fingerprint=first_output,
            chosen_policy_output_fingerprint=SHA_A,
        )

    assert resolution["chosen_policy_output_fingerprint"] == canonical[
        "policy_output_fingerprint"
    ]


def test_restart_adopts_resolution_written_before_manifest_save(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = ExternalEventArchive(
        tmp_path,
        now=SequenceClock(
            *(T0 + timedelta(seconds=value) for value in range(1, 14)),
        ),
    )
    input_fingerprint, _, first_output, canonical = (
        _publish_conflicting_attempts(archive)
    )
    conflict_id = next(tmp_path.joinpath("conflicts").glob("*.json")).stem

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
            conflict_id=conflict_id,
            decision=(
                ConflictResolutionDecision.KEEP_FIRST_DURABLY_PUBLISHED
            ),
            reviewer="research-reviewer",
            reason="The durable live result owned the input first.",
            chosen_attempt_id="live-attempt",
            chosen_complete_output_fingerprint=first_output,
            chosen_policy_output_fingerprint=str(
                canonical["policy_output_fingerprint"]
            ),
        )

    assert list(tmp_path.joinpath("resolutions").glob("*.json"))
    assert archive.load_resolution_manifest()["generation"] == 0

    restarted = ExternalEventArchive(
        tmp_path, now=lambda: T0 + timedelta(hours=1)
    )
    recovered = restarted.load_conflict_resolution(input_fingerprint)

    assert recovered is not None
    assert recovered["decision"] == "KEEP_FIRST_DURABLY_PUBLISHED"
    assert recovered["chosen_complete_output_fingerprint"] == first_output
    assert recovered["chosen_policy_output_fingerprint"] == canonical[
        "policy_output_fingerprint"
    ]
    assert restarted.load_resolution_manifest()["generation"] == 1


def test_abstain_and_reclassify_conflict_resolutions_are_explicit(tmp_path) -> None:
    archive = ExternalEventArchive(
        tmp_path,
        now=SequenceClock(
            T0 + timedelta(seconds=1),
            T0 + timedelta(seconds=20),
            T0 + timedelta(seconds=30),
            T0 + timedelta(seconds=40),
        ),
    )
    input_fingerprint, old_profile_hash, _, _ = _publish_conflicting_attempts(
        archive
    )
    conflict_id = next(tmp_path.joinpath("conflicts").glob("*.json")).stem

    abstain = archive.publish_conflict_resolution(
        conflict_id=conflict_id,
        decision=ConflictResolutionDecision.ABSTAIN_INPUT,
        reviewer="research-reviewer",
        reason="Canonical ownership could not be proven.",
    )
    assert abstain["decision"] == "ABSTAIN_INPUT"

    with pytest.raises(
        ExternalEventArchiveError,
        match="does not choose an attempt or profile",
    ):
        archive.publish_conflict_resolution(
            conflict_id=conflict_id,
            decision=ConflictResolutionDecision.ABSTAIN_INPUT,
            reviewer="research-reviewer",
            reason="An abstention cannot retain a selected output.",
            chosen_attempt_id="live-attempt",
        )

    with pytest.raises(ExternalEventArchiveError, match="genuinely new profile"):
        archive.publish_conflict_resolution(
            conflict_id=conflict_id,
            decision=ConflictResolutionDecision.RECLASSIFY_UNDER_NEW_PROFILE,
            reviewer="research-reviewer",
            reason="The profile was under-specified.",
            new_profile_hash=old_profile_hash,
        )

    new_profile_hash = sha256(b"new-pinned-profile").hexdigest()
    with pytest.raises(ExternalEventArchiveError, match="does not choose an old attempt"):
        archive.publish_conflict_resolution(
            conflict_id=conflict_id,
            decision=ConflictResolutionDecision.RECLASSIFY_UNDER_NEW_PROFILE,
            reviewer="research-reviewer",
            reason="A new profile cannot select an old result.",
            chosen_attempt_id="live-attempt",
            new_profile_hash=new_profile_hash,
        )
    reclassify = archive.publish_conflict_resolution(
        conflict_id=conflict_id,
        decision=ConflictResolutionDecision.RECLASSIFY_UNDER_NEW_PROFILE,
        reviewer="research-reviewer",
        reason="The old profile omitted a semantic setting.",
        new_profile_hash=new_profile_hash,
    )
    assert reclassify["new_profile_hash"] == new_profile_hash
    assert reclassify["manifest_generation"] > abstain["manifest_generation"]
    assert archive.load_conflict_resolution(input_fingerprint) == reclassify


def test_classification_artifacts_advance_pinned_archive_generation(tmp_path) -> None:
    archive = ExternalEventArchive(tmp_path, now=lambda: T0)
    input_fingerprint = sha256(b"snapshot-input").hexdigest()
    profile_hash = sha256(b"snapshot-profile").hexdigest()
    complete_hash, policy_hash = output_fingerprints(
        {
            "status": "VALID",
            "event_type": "OTHER",
            "risk_level": "LOW",
            "urgency": "LOW",
            "confidence": 0.7,
            "summary": "safe metadata only",
        }
    )
    generations = [archive.load_manifest()["generation"]]

    archive.publish_classification_attempt(
        classification_input_fingerprint=input_fingerprint,
        attempt_id="attempt-one",
        payload={
            "classification_attempt_id": "attempt-one",
            "classification_input_fingerprint": input_fingerprint,
            "profile_hash": profile_hash,
            "status": "VALID",
            "validation_outcome": True,
            "durably_published": True,
            "complete_output_fingerprint": complete_hash,
            "policy_output_fingerprint": policy_hash,
            "normalized_output": {"event_type": "OTHER"},
            "classification_origin": "LIVE_SYSTEM",
        },
    )
    generations.append(archive.load_manifest()["generation"])
    archive.claim_canonical_result(
        classification_input_fingerprint=input_fingerprint,
        attempt_id="attempt-one",
        complete_output_fingerprint=complete_hash,
        policy_output_fingerprint=policy_hash,
        profile_hash=profile_hash,
        evidence_ready_at=T0,
    )
    generations.append(archive.load_manifest()["generation"])
    archive.publish_materialized_event(
        source_revision_id="revision-one",
        classification_input_fingerprint=input_fingerprint,
        payload={"context_event_id": "event-one"},
    )
    generations.append(archive.load_manifest()["generation"])
    archive.publish_readiness(
        source_revision_id="revision-one",
        classification_input_fingerprint=input_fingerprint,
        canonical_classification_attempt_id="attempt-one",
        complete_output_fingerprint=complete_hash,
        policy_output_fingerprint=policy_hash,
        profile_hash=profile_hash,
        classification_profile={"profile_hash": profile_hash},
        classification_status="VALID",
        policy_eligible=True,
        context_event=None,
        evidence_ready_at=T0,
    )
    generations.append(archive.load_manifest()["generation"])

    assert all(later > earlier for earlier, later in zip(generations, generations[1:]))


def test_coverage_reconciliation_recovers_atomic_file_before_manifest_update(
    tmp_path,
    monkeypatch,
) -> None:
    archive = ExternalEventArchive(tmp_path, now=lambda: T0)
    coverage = SourceCoverage(
        source="lockheed_martin_rss",
        coverage_start=T0,
        coverage_end=T0,
        coverage_status=CoverageStatus.LIVE_ONLY,
        bootstrap_time=T0,
        live_collection_start=T0,
        last_verification_time=T0,
        coverage_generation=1,
    )

    def fail_before_manifest_registration(*_args, **_kwargs) -> None:
        raise ExternalEventArchiveError("simulated manifest interruption")

    monkeypatch.setattr(
        archive,
        "_register_mutable_artifact",
        fail_before_manifest_registration,
    )
    with pytest.raises(ExternalEventArchiveError, match="manifest interruption"):
        archive.save_coverage(coverage)

    recovered = ExternalEventArchive(tmp_path, now=lambda: T0 + timedelta(seconds=1))
    before_generation = int(recovered.load_manifest()["generation"])
    assert recovered.load_coverage(coverage.source) == coverage
    manifest = recovered.load_manifest()

    assert int(manifest["generation"]) == before_generation + 1
    assert (
        manifest["mutable_artifacts"]["coverage"][coverage.source]
        == sha256(
            (recovered.coverage_dir / "lockheed_martin_rss.json").read_bytes()
        ).hexdigest()
    )


@pytest.mark.parametrize(
    "mutation",
    (
        {"source": 123},
        {"coverage_generation": "1"},
        {"coverage_generation": True},
        {"coverage_version": None},
        {"coverage_status": 1},
    ),
)
def test_coverage_reconciliation_rejects_noncanonical_field_types(
    tmp_path,
    mutation,
) -> None:
    archive = ExternalEventArchive(tmp_path, now=lambda: T0)
    coverage = SourceCoverage(
        source="lockheed_martin_rss",
        coverage_start=T0,
        coverage_end=T0,
        coverage_status=CoverageStatus.LIVE_ONLY,
        bootstrap_time=T0,
        live_collection_start=T0,
        last_verification_time=T0,
        coverage_generation=1,
    )
    payload = {**coverage.to_payload(), **mutation}
    path = archive.coverage_dir / "lockheed_martin_rss.json"
    archive._atomic_replace(
        path,
        (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"),
    )
    before = archive.load_manifest()

    with pytest.raises(ExternalEventArchiveError):
        archive.reconcile_mutable_artifacts()

    assert archive.load_manifest() == before


def test_coverage_reconciliation_wraps_malformed_payload(tmp_path) -> None:
    archive = ExternalEventArchive(tmp_path, now=lambda: T0)
    path = archive.coverage_dir / "lockheed_martin_rss.json"
    archive._atomic_replace(path, b"{}\n")

    with pytest.raises(ExternalEventArchiveError, match="coverage manifest"):
        archive.reconcile_mutable_artifacts()

    assert archive.load_manifest()["mutable_artifacts"] == {}


def test_coverage_reconciliation_does_not_register_swapped_unvalidated_bytes(
    tmp_path,
    monkeypatch,
) -> None:
    archive = ExternalEventArchive(tmp_path, now=lambda: T0)
    coverage = SourceCoverage(
        source="lockheed_martin_rss",
        coverage_start=T0,
        coverage_end=T0,
        coverage_status=CoverageStatus.LIVE_ONLY,
        bootstrap_time=T0,
        live_collection_start=T0,
        last_verification_time=T0,
        coverage_generation=1,
    )
    path = archive.coverage_dir / "lockheed_martin_rss.json"
    archive._atomic_replace(
        path,
        (json.dumps(coverage.to_payload(), sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )
    register = archive._register_mutable_artifact

    def swap_then_register(*args, **kwargs) -> None:
        archive._atomic_replace(path, b"{}\n")
        register(*args, **kwargs)

    monkeypatch.setattr(
        archive,
        "_register_mutable_artifact",
        swap_then_register,
    )
    with pytest.raises(
        ExternalEventArchiveError,
        match="changed during publication",
    ):
        archive.reconcile_mutable_artifacts()

    assert archive.load_manifest()["mutable_artifacts"] == {}


def test_earnings_package_a_b_a_is_three_ordered_immutable_occurrences(
    tmp_path,
) -> None:
    archive = ExternalEventArchive(tmp_path, now=lambda: T0)
    base = {
        "package_id": "LMT:2026:Q1",
        "package_state": "PRIMARY_ARCHIVED",
    }

    first = archive.publish_earnings_package(
        ticker="LMT",
        fiscal_year=2026,
        fiscal_quarter=1,
        payload={**base, "primary_document": {"document_hash": SHA_A}},
    )
    second = archive.publish_earnings_package(
        ticker="LMT",
        fiscal_year=2026,
        fiscal_quarter=1,
        payload={**base, "primary_document": {"document_hash": SHA_B}},
    )
    reverted = archive.publish_earnings_package(
        ticker="LMT",
        fiscal_year=2026,
        fiscal_quarter=1,
        payload={**base, "primary_document": {"document_hash": SHA_A}},
    )

    assert len({first, second, reverted}) == 3
    revisions = archive.iter_earnings_package_revisions(
        ticker="LMT",
        fiscal_year=2026,
        fiscal_quarter=1,
    )
    assert [value["revision_sequence"] for value in revisions] == [1, 2, 3]
    assert revisions[0]["supersedes_revision_id"] is None
    assert revisions[1]["supersedes_revision_id"] == first
    assert revisions[2]["supersedes_revision_id"] == second
    assert revisions[2]["package_content_hash"] == revisions[0][
        "package_content_hash"
    ]
    state = archive.load_manifest()["earnings_packages"]["LMT:2026:Q1"]
    assert state["current_revision_id"] == reverted


@pytest.mark.parametrize(
    "override",
    [
        {"package_id": "PLTR:2026:Q1"},
        {"ticker": "PLTR"},
        {"fiscal_year": 2025},
        {"fiscal_quarter": 2},
        {"revision_sequence": 99},
    ],
)
def test_earnings_package_rejects_caller_owned_identity_or_revision_fields(
    tmp_path, override: dict[str, object]
) -> None:
    archive = ExternalEventArchive(tmp_path, now=lambda: T0)

    with pytest.raises(ExternalEventArchiveError):
        archive.publish_earnings_package(
            ticker="LMT",
            fiscal_year=2026,
            fiscal_quarter=1,
            payload={"package_state": "DISCOVERED", **override},
        )
