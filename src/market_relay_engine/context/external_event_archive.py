"""Immutable external-event archive and canonical classification registry.

The archive is the source of truth for news and social inputs.  Mutable files are
limited to atomically replaced manifests; source bytes, revisions, attempts,
conflicts, resolutions, and readiness receipts are immutable publications.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Iterator, Mapping

from market_relay_engine.common.time import ensure_timezone_aware_utc, parse_utc_iso, to_utc_iso, utc_now


class ExternalEventArchiveError(RuntimeError):
    """Raised when immutable external-event state cannot be proven safe."""


class LifecycleState(str, Enum):
    ACTIVE = "ACTIVE"
    UPDATED = "UPDATED"
    DELETED = "DELETED"
    RETRACTED = "RETRACTED"


class CoverageStatus(str, Enum):
    LIVE_ONLY = "LIVE_ONLY"
    PARTIAL = "PARTIAL"
    COMPLETE_FOR_RANGE = "COMPLETE_FOR_RANGE"
    UNKNOWN = "UNKNOWN"


class ConflictResolutionDecision(str, Enum):
    KEEP_FIRST_DURABLY_PUBLISHED = "KEEP_FIRST_DURABLY_PUBLISHED"
    ABSTAIN_INPUT = "ABSTAIN_INPUT"
    RECLASSIFY_UNDER_NEW_PROFILE = "RECLASSIFY_UNDER_NEW_PROFILE"


@dataclass(frozen=True, kw_only=True)
class ExternalSourceRevision:
    """One immutable lifecycle revision of a source-native fact."""

    source: str
    source_fact_id: str
    source_revision_id: str
    revision_sequence: int
    supersedes_revision_id: str | None
    lifecycle_state: LifecycleState
    lifecycle_effective_at: datetime
    system_observed_at: datetime
    source_available_at: datetime | None
    archived_at: datetime
    raw_object_hash: str
    document_hash: str
    normalized_text_hash: str | None
    canonical_content_hash: str
    source_type: str
    source_platform: str | None = None
    source_uri: str | None = None
    source_published_at: datetime | None = None
    source_updated_at: datetime | None = None
    source_title: str | None = None
    affected_tickers: tuple[str, ...] = field(default_factory=tuple)
    affected_sectors: tuple[str, ...] = field(default_factory=tuple)
    global_relevance: bool | None = None
    correlation_group_id: str | None = None
    relationship_types: tuple[str, ...] = field(default_factory=tuple)
    earnings_package_id: str | None = None
    adapter_version: str = "external_adapter_v1"
    extractor_version: str = "external_extractor_v1"
    normalizer_version: str = "external_normalizer_v1"
    normalized_at: datetime | None = None
    collection_mode: str = "LIVE_SYSTEM"
    trace_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "source",
            "source_fact_id",
            "source_revision_id",
            "source_type",
            "adapter_version",
            "extractor_version",
            "normalizer_version",
            "collection_mode",
        ):
            _required_string(getattr(self, name), name)
        for name in (
            "raw_object_hash",
            "document_hash",
            "canonical_content_hash",
        ):
            _sha256_value(getattr(self, name), name)
        if self.normalized_text_hash is not None:
            _sha256_value(self.normalized_text_hash, "normalized_text_hash")
        if isinstance(self.revision_sequence, bool) or not isinstance(self.revision_sequence, int) or self.revision_sequence < 1:
            raise ExternalEventArchiveError("revision_sequence must be a positive integer")
        if self.collection_mode not in {"LIVE_SYSTEM", "BACKFILL"}:
            raise ExternalEventArchiveError(
                "collection_mode must be LIVE_SYSTEM or BACKFILL"
            )
        if not isinstance(self.lifecycle_state, LifecycleState):
            raise ExternalEventArchiveError("lifecycle_state must be a LifecycleState")
        for name in (
            "lifecycle_effective_at",
            "system_observed_at",
            "archived_at",
        ):
            ensure_timezone_aware_utc(getattr(self, name))
        if self.normalized_at is None:
            object.__setattr__(self, "normalized_at", self.archived_at)
        else:
            object.__setattr__(
                self,
                "normalized_at",
                ensure_timezone_aware_utc(self.normalized_at),
            )
        for name in ("source_available_at", "source_published_at", "source_updated_at"):
            value = getattr(self, name)
            if value is not None:
                ensure_timezone_aware_utc(value)
        object.__setattr__(self, "affected_tickers", _symbols(self.affected_tickers))
        object.__setattr__(self, "affected_sectors", _symbols(self.affected_sectors))
        if self.global_relevance is not None and not isinstance(self.global_relevance, bool):
            raise ExternalEventArchiveError("global_relevance must be bool or None")
        for name in ("correlation_group_id", "earnings_package_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required_string(value, name))
        if self.source_title is not None:
            object.__setattr__(
                self,
                "source_title",
                _required_string(self.source_title, "source_title"),
            )
        object.__setattr__(
            self,
            "relationship_types",
            tuple(sorted({_required_string(value, "relationship_type") for value in self.relationship_types})),
        )

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["lifecycle_state"] = self.lifecycle_state.value
        for name in (
            "lifecycle_effective_at",
            "system_observed_at",
            "source_available_at",
            "archived_at",
            "source_published_at",
            "source_updated_at",
            "normalized_at",
        ):
            value = getattr(self, name)
            payload[name] = None if value is None else to_utc_iso(value)
        payload["affected_tickers"] = list(self.affected_tickers)
        payload["affected_sectors"] = list(self.affected_sectors)
        payload["relationship_types"] = list(self.relationship_types)
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ExternalSourceRevision":
        values = dict(payload)
        values["lifecycle_state"] = LifecycleState(str(values["lifecycle_state"]))
        for name in ("lifecycle_effective_at", "system_observed_at", "archived_at"):
            values[name] = parse_utc_iso(str(values[name]))
        for name in ("source_available_at", "source_published_at", "source_updated_at"):
            if values.get(name) is not None:
                values[name] = parse_utc_iso(str(values[name]))
        if values.get("normalized_at") is not None:
            values["normalized_at"] = parse_utc_iso(str(values["normalized_at"]))
        values["affected_tickers"] = tuple(values.get("affected_tickers", ()))
        values["affected_sectors"] = tuple(values.get("affected_sectors", ()))
        values["relationship_types"] = tuple(values.get("relationship_types", ()))
        return cls(**values)


@dataclass(frozen=True, kw_only=True)
class CoverageInterval:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = ensure_timezone_aware_utc(self.start)
        end = ensure_timezone_aware_utc(self.end)
        if end < start:
            raise ExternalEventArchiveError("coverage interval end precedes start")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    def to_payload(self) -> dict[str, str]:
        return {"start": to_utc_iso(self.start), "end": to_utc_iso(self.end)}

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "CoverageInterval":
        try:
            return cls(
                start=_required_archive_publication_time(
                    value["start"], "coverage interval start"
                ),
                end=_required_archive_publication_time(
                    value["end"], "coverage interval end"
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ExternalEventArchiveError(
                "coverage interval has invalid values"
            ) from exc


@dataclass(frozen=True, kw_only=True)
class SourceCoverage:
    source: str
    coverage_start: datetime | None
    coverage_end: datetime | None
    coverage_status: CoverageStatus
    known_gaps: tuple[CoverageInterval, ...] = field(default_factory=tuple)
    bootstrap_time: datetime | None = None
    completed_backfill_ranges: tuple[CoverageInterval, ...] = field(default_factory=tuple)
    live_collection_start: datetime | None = None
    last_verification_time: datetime | None = None
    coverage_generation: int = 0
    coverage_version: str = "external_coverage_v1"

    def __post_init__(self) -> None:
        _required_string(self.source, "source")
        _required_string(self.coverage_version, "coverage_version")
        if not isinstance(self.coverage_status, CoverageStatus):
            raise ExternalEventArchiveError("coverage_status must be a CoverageStatus")
        if isinstance(self.coverage_generation, bool) or not isinstance(self.coverage_generation, int) or self.coverage_generation < 0:
            raise ExternalEventArchiveError("coverage_generation must be non-negative")
        for name in (
            "coverage_start",
            "coverage_end",
            "bootstrap_time",
            "live_collection_start",
            "last_verification_time",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, ensure_timezone_aware_utc(value))
        if self.coverage_start is not None and self.coverage_end is not None and self.coverage_end < self.coverage_start:
            raise ExternalEventArchiveError("coverage_end precedes coverage_start")
        object.__setattr__(self, "known_gaps", tuple(self.known_gaps))
        object.__setattr__(self, "completed_backfill_ranges", tuple(self.completed_backfill_ranges))

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "coverage_start": _optional_iso(self.coverage_start),
            "coverage_end": _optional_iso(self.coverage_end),
            "coverage_status": self.coverage_status.value,
            "known_gaps": [value.to_payload() for value in self.known_gaps],
            "bootstrap_time": _optional_iso(self.bootstrap_time),
            "completed_backfill_ranges": [value.to_payload() for value in self.completed_backfill_ranges],
            "live_collection_start": _optional_iso(self.live_collection_start),
            "last_verification_time": _optional_iso(self.last_verification_time),
            "coverage_generation": self.coverage_generation,
            "coverage_version": self.coverage_version,
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "SourceCoverage":
        try:
            source = _required_string(value["source"], "source")
            status_value = value["coverage_status"]
            if not isinstance(status_value, str):
                raise ExternalEventArchiveError(
                    "coverage_status must be a string"
                )
            version = value.get("coverage_version", "external_coverage_v1")
            version = _required_string(version, "coverage_version")
            generation = _non_negative_integer(
                value.get("coverage_generation", 0),
                "coverage_generation",
            )
            return cls(
                source=source,
                coverage_start=_parse_optional_iso(value.get("coverage_start")),
                coverage_end=_parse_optional_iso(value.get("coverage_end")),
                coverage_status=CoverageStatus(status_value),
                known_gaps=tuple(
                    CoverageInterval.from_payload(item)
                    for item in _mapping_list(value.get("known_gaps", []))
                ),
                bootstrap_time=_parse_optional_iso(value.get("bootstrap_time")),
                completed_backfill_ranges=tuple(
                    CoverageInterval.from_payload(item)
                    for item in _mapping_list(
                        value.get("completed_backfill_ranges", [])
                    )
                ),
                live_collection_start=_parse_optional_iso(
                    value.get("live_collection_start")
                ),
                last_verification_time=_parse_optional_iso(
                    value.get("last_verification_time")
                ),
                coverage_generation=generation,
                coverage_version=version,
            )
        except ExternalEventArchiveError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ExternalEventArchiveError(
                "coverage manifest has invalid values"
            ) from exc

    def covers(self, start: datetime, end: datetime) -> bool:
        start = ensure_timezone_aware_utc(start)
        end = ensure_timezone_aware_utc(end)
        if self.coverage_status is CoverageStatus.UNKNOWN:
            return False
        intervals = (
            []
            if self.coverage_status is CoverageStatus.LIVE_ONLY
            else list(self.completed_backfill_ranges)
        )
        if self.live_collection_start is not None and self.coverage_end is not None:
            intervals.append(CoverageInterval(start=self.live_collection_start, end=self.coverage_end))
        if self.coverage_status is CoverageStatus.COMPLETE_FOR_RANGE and self.coverage_start is not None and self.coverage_end is not None:
            intervals.append(CoverageInterval(start=self.coverage_start, end=self.coverage_end))
        covered = _interval_union_covers(intervals, start, end)
        if not covered:
            return False
        return not any(_intervals_overlap(gap.start, gap.end, start, end) for gap in self.known_gaps)


class ExternalEventArchive:
    """Content-addressed external source archive with atomic mutable manifests."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        root: str | Path,
        *,
        now: Callable[[], datetime] = utc_now,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.records = self.root / "records"
        self.observations = self.root / "observations"
        self.readiness = self.root / "readiness"
        self.events = self.root / "events"
        self.earnings = self.root / "earnings"
        self.classifications = self.root / "classifications" / "by_input"
        self.conflicts = self.root / "conflicts"
        self.resolutions = self.root / "resolutions"
        self.coverage_dir = self.root / "coverage"
        self.manifest_path = self.root / "manifests" / "external_events.json"
        self.resolution_manifest_path = self.root / "manifests" / "classification_resolutions.json"
        self._now = now
        self._sleeper = sleeper

    def archive_object(self, content: bytes, *, extension: str, content_type: str | None = None) -> str:
        if not isinstance(content, bytes):
            raise ExternalEventArchiveError("archive content must be bytes")
        digest = sha256(content).hexdigest()
        extension = _safe_extension(extension)
        path = self.objects / digest / f"original.{extension}"
        self._write_bytes_once(path, content)
        metadata = {
            "sha256": digest,
            "size_bytes": len(content),
            "content_type": content_type,
            "extension": extension,
        }
        # The same byte sequence can legitimately be observed with different
        # source extensions/content types.  Keep each metadata assertion as an
        # immutable observation without making the content object conflict.
        metadata_hash = _hash_payload(metadata)
        self._write_json_once(
            self.objects
            / digest
            / f"object.{extension}.{metadata_hash}.json",
            metadata,
        )
        return digest

    def archive_normalized_text(self, text: str) -> str:
        if not isinstance(text, str):
            raise ExternalEventArchiveError("normalized text must be a string")
        payload = text.encode("utf-8")
        digest = sha256(payload).hexdigest()
        self._write_bytes_once(self.objects / digest / "normalized.txt", payload)
        return digest

    def archive_excerpt(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            raise ExternalEventArchiveError("classification excerpt must be non-empty")
        payload = text.encode("utf-8")
        digest = sha256(payload).hexdigest()
        self._write_bytes_once(self.objects / digest / "excerpt.txt", payload)
        return digest

    def read_object(self, digest: str, *, filename: str) -> bytes:
        _sha256_value(digest, "digest")
        path = self.objects / digest / filename
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ExternalEventArchiveError("archive object is missing") from exc
        if sha256(payload).hexdigest() != digest:
            raise ExternalEventArchiveError("archive object content hash mismatch")
        return payload

    def publish_observation(
        self,
        *,
        source: str,
        payload: Mapping[str, Any],
        source_revision_id: str | None = None,
        observed_at: datetime | None = None,
    ) -> str:
        _required_string(source, "source")
        encoded = _json_bytes(payload)
        observation_id = sha256(encoded).hexdigest()
        self._write_bytes_once(self.observations / _safe_component(source) / f"{observation_id}.json", encoded)
        if source_revision_id is not None:
            self.record_observation_lineage(
                source_revision_id=source_revision_id,
                observation_id=observation_id,
                observed_at=observed_at or self._now(),
            )
        return observation_id

    def record_observation_lineage(
        self,
        *,
        source_revision_id: str,
        observation_id: str,
        observed_at: datetime,
    ) -> None:
        _required_string(source_revision_id, "source_revision_id")
        _sha256_value(observation_id, "observation_id")
        observed_at = ensure_timezone_aware_utc(observed_at)
        with self.manifest_lock():
            manifest = self.load_manifest()
            lineage = manifest.setdefault("observation_lineage", {})
            values = lineage.setdefault(source_revision_id, {})
            existing = values.get(observation_id)
            timestamp = to_utc_iso(observed_at)
            if existing is not None and existing != timestamp:
                raise ExternalEventArchiveError("observation lineage timestamp changed")
            if existing is None:
                values[observation_id] = timestamp
                manifest["generation"] = int(manifest.get("generation", 0)) + 1
                manifest["updated_at"] = to_utc_iso(self._now())
                self.save_manifest(manifest)

    def observation_lineage(self, source_revision_id: str) -> dict[str, str]:
        manifest = self.load_manifest()
        lineage = manifest.get("observation_lineage", {})
        if not isinstance(lineage, Mapping):
            raise ExternalEventArchiveError("observation lineage has invalid shape")
        values = lineage.get(source_revision_id, {})
        if not isinstance(values, Mapping):
            raise ExternalEventArchiveError("revision observation lineage is invalid")
        return {str(key): str(value) for key, value in values.items()}

    def publish_earnings_package(
        self,
        *,
        ticker: str,
        fiscal_year: int,
        fiscal_quarter: int,
        payload: Mapping[str, Any],
    ) -> str:
        ticker = _required_string(ticker, "ticker").upper()
        if not (2000 <= fiscal_year <= 9999):
            raise ExternalEventArchiveError("fiscal_year is invalid")
        if fiscal_quarter not in {1, 2, 3, 4}:
            raise ExternalEventArchiveError("fiscal_quarter is invalid")
        package_id = f"{ticker}:{fiscal_year}:Q{fiscal_quarter}"
        expected_identity = {
            "package_id": package_id,
            "ticker": ticker,
            "fiscal_year": fiscal_year,
            "fiscal_quarter": fiscal_quarter,
        }
        for field_name, expected_value in expected_identity.items():
            supplied_value = payload.get(field_name)
            if supplied_value is not None and supplied_value != expected_value:
                raise ExternalEventArchiveError(
                    "earnings package payload identity changed"
                )
        derived_fields = {
            "package_revision_id",
            "package_content_hash",
            "revision_sequence",
            "supersedes_revision_id",
        }
        if derived_fields.intersection(payload):
            raise ExternalEventArchiveError(
                "earnings package payload contains archive-owned fields"
            )
        package_payload = {
            "package_id": package_id,
            "ticker": ticker,
            "fiscal_year": fiscal_year,
            "fiscal_quarter": fiscal_quarter,
            **dict(payload),
        }
        content_hash = _hash_payload(package_payload)
        package_directory = (
            self.earnings / ticker / str(fiscal_year) / f"Q{fiscal_quarter}"
        )
        with self.manifest_lock():
            manifest = self.load_manifest()
            packages = manifest.setdefault("earnings_packages", {})
            state = packages.setdefault(
                package_id, {"revision_ids": [], "current_revision_id": None}
            )
            revision_ids = state.get("revision_ids")
            if not isinstance(revision_ids, list):
                legacy = state.get("revision_hashes", [])
                if not isinstance(legacy, list):
                    raise ExternalEventArchiveError(
                        "earnings package revision manifest is invalid"
                    )
                revision_ids = list(legacy)
                state["revision_ids"] = revision_ids
            current_id = state.get("current_revision_id")
            if current_id is None:
                current_id = state.get("current_revision_hash")
            if current_id is not None:
                current_path = package_directory / f"package.{current_id}.json"
                current_payload = self._read_json_object(
                    current_path, "earnings package"
                )
                current_content = {
                    key: value
                    for key, value in current_payload.items()
                    if key
                    not in {
                        "package_revision_id",
                        "package_content_hash",
                        "revision_sequence",
                        "supersedes_revision_id",
                    }
                }
                if current_content == package_payload:
                    return str(current_id)
            sequence = len(revision_ids) + 1
            revision_id = _hash_payload(
                {
                    "package_id": package_id,
                    "package_content_hash": content_hash,
                    "revision_sequence": sequence,
                    "supersedes_revision_id": current_id,
                }
            )
            stored_payload = {
                **package_payload,
                "package_revision_id": revision_id,
                "package_content_hash": content_hash,
                "revision_sequence": sequence,
                "supersedes_revision_id": current_id,
            }
            self._write_json_once(
                package_directory / f"package.{revision_id}.json",
                stored_payload,
            )
            revision_ids.append(revision_id)
            state["current_revision_id"] = revision_id
            # Preserve legacy keys when an existing local archive has them,
            # but make the occurrence-aware fields authoritative.
            manifest["generation"] = int(manifest.get("generation", 0)) + 1
            manifest["updated_at"] = to_utc_iso(self._now())
            self.save_manifest(manifest)
        return revision_id

    def iter_earnings_package_revisions(
        self, *, ticker: str, fiscal_year: int, fiscal_quarter: int
    ) -> tuple[dict[str, Any], ...]:
        ticker = _required_string(ticker, "ticker").upper()
        path = self.earnings / ticker / str(fiscal_year) / f"Q{fiscal_quarter}"
        if not path.exists():
            return ()
        values = [
            self._read_json_object(item, "earnings package")
            for item in sorted(path.glob("package.*.json"))
        ]
        return tuple(
            sorted(
                values,
                key=lambda value: (
                    int(value.get("revision_sequence", 0)),
                    str(
                        value.get("package_revision_id")
                        or value.get("package_content_hash")
                        or ""
                    ),
                ),
            )
        )

    def publish_revision(
        self, revision: ExternalSourceRevision
    ) -> ExternalSourceRevision:
        if not isinstance(revision, ExternalSourceRevision):
            raise ExternalEventArchiveError("revision must be ExternalSourceRevision")
        path = self._revision_path(revision.source, revision.source_fact_id, revision.source_revision_id)
        with self.manifest_lock():
            manifest = self.load_manifest()
            facts = manifest.setdefault("facts", {})
            source_facts = facts.setdefault(revision.source, {})
            state = source_facts.setdefault(
                revision.source_fact_id,
                {"revision_ids": [], "current_revision_id": None},
            )
            revision_ids = state.setdefault("revision_ids", [])
            current_id = state.get("current_revision_id")
            if revision.source_revision_id in revision_ids:
                existing = self.read_revision(
                    revision.source,
                    revision.source_fact_id,
                    revision.source_revision_id,
                )
                if existing != revision:
                    raise ExternalEventArchiveError(
                        "immutable source revision identity changed"
                    )
                return existing
            if current_id is None:
                if revision.supersedes_revision_id is not None:
                    raise ExternalEventArchiveError(
                        "first source revision cannot supersede another revision"
                    )
            else:
                current = self.read_revision(revision.source, revision.source_fact_id, str(current_id))
                if revision.supersedes_revision_id != current.source_revision_id:
                    raise ExternalEventArchiveError(
                        "source revision must supersede the current lifecycle head"
                    )
                if revision.revision_sequence <= current.revision_sequence:
                    raise ExternalEventArchiveError(
                        "source revision sequence is not monotonic"
                    )
                if revision.system_observed_at < current.system_observed_at:
                    raise ExternalEventArchiveError(
                        "source revision observation time moved backwards"
                    )
                if revision.lifecycle_effective_at < current.lifecycle_effective_at:
                    raise ExternalEventArchiveError(
                        "source revision lifecycle time moved backwards"
                    )
                if any(
                    self.read_revision(
                        revision.source,
                        revision.source_fact_id,
                        str(revision_id),
                    ).revision_sequence
                    == revision.revision_sequence
                    for revision_id in revision_ids
                ):
                    raise ExternalEventArchiveError(
                        "source revision sequence is ambiguous"
                    )
            canonical_revision = revision
            if path.exists():
                # Recover a source record durably published before a process
                # crash that occurred prior to manifest replacement.  Runtime
                # receipt/archive timestamps belong to that first immutable
                # publication and must not be replaced by replay timestamps.
                orphan = self.read_revision(
                    revision.source,
                    revision.source_fact_id,
                    revision.source_revision_id,
                )
                if not _same_revision_semantic_identity(orphan, revision):
                    raise ExternalEventArchiveError(
                        "orphan source revision semantic identity changed"
                    )
                canonical_revision = orphan
            else:
                self._write_json_once(path, revision.to_payload())
            revision_ids.append(canonical_revision.source_revision_id)
            revision_ids.sort()
            state["current_revision_id"] = canonical_revision.source_revision_id
            manifest["generation"] = int(manifest.get("generation", 0)) + 1
            manifest["updated_at"] = to_utc_iso(self._now())
            self.save_manifest(manifest)
            return canonical_revision

    def read_revision(self, source: str, source_fact_id: str, source_revision_id: str) -> ExternalSourceRevision:
        path = self._revision_path(source, source_fact_id, source_revision_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExternalEventArchiveError("external revision could not be read") from exc
        if not isinstance(value, dict):
            raise ExternalEventArchiveError("external revision has invalid shape")
        return ExternalSourceRevision.from_payload(value)

    def iter_revisions(self, *, sources: Iterable[str] | None = None) -> Iterator[ExternalSourceRevision]:
        allowed = None if sources is None else set(sources)
        manifest = self.load_manifest()
        facts = manifest.get("facts", {})
        if not isinstance(facts, Mapping):
            raise ExternalEventArchiveError("external manifest facts have invalid shape")
        for source, source_facts in sorted(facts.items(), key=lambda item: str(item[0])):
            if allowed is not None and str(source) not in allowed:
                continue
            if not isinstance(source_facts, Mapping):
                raise ExternalEventArchiveError("external manifest source facts have invalid shape")
            for fact_id, raw_state in sorted(source_facts.items(), key=lambda item: str(item[0])):
                if not isinstance(raw_state, Mapping) or not isinstance(raw_state.get("revision_ids"), list):
                    raise ExternalEventArchiveError("external manifest fact state has invalid shape")
                for revision_id in raw_state["revision_ids"]:
                    yield self.read_revision(str(source), str(fact_id), str(revision_id))

    def publish_readiness(
        self,
        *,
        source_revision_id: str,
        classification_input_fingerprint: str | None,
        canonical_classification_attempt_id: str | None,
        complete_output_fingerprint: str | None,
        policy_output_fingerprint: str | None,
        profile_hash: str | None,
        classification_profile: Mapping[str, Any] | None,
        classification_status: str | None,
        policy_eligible: bool,
        context_event: Mapping[str, Any] | None,
        evidence_ready_at: datetime | None = None,
    ) -> dict[str, Any]:
        _required_string(source_revision_id, "source_revision_id")
        for value, name in (
            (classification_input_fingerprint, "classification_input_fingerprint"),
            (complete_output_fingerprint, "complete_output_fingerprint"),
            (policy_output_fingerprint, "policy_output_fingerprint"),
            (profile_hash, "profile_hash"),
        ):
            if value is not None:
                _sha256_value(value, name)
        if classification_input_fingerprint is not None and (
            profile_hash is None or classification_profile is None
        ):
            raise ExternalEventArchiveError(
                "classified readiness requires an exact profile and profile hash"
            )
        archive_published_at = ensure_timezone_aware_utc(self._now())
        ready_at = max(
            archive_published_at,
            ensure_timezone_aware_utc(
                archive_published_at
                if evidence_ready_at is None
                else evidence_ready_at
            ),
        )
        payload = {
            "source_revision_id": source_revision_id,
            "evidence_ready_at": to_utc_iso(ready_at),
            "archive_published_at": to_utc_iso(archive_published_at),
            "classification_input_fingerprint": classification_input_fingerprint,
            "canonical_classification_attempt_id": canonical_classification_attempt_id,
            "complete_output_fingerprint": complete_output_fingerprint,
            "policy_output_fingerprint": policy_output_fingerprint,
            "profile_hash": profile_hash,
            "classification_profile": (
                None if classification_profile is None else dict(classification_profile)
            ),
            "classification_status": classification_status,
            "policy_eligible": bool(policy_eligible),
            "context_event": None if context_event is None else dict(context_event),
        }
        readiness_key = classification_input_fingerprint or "no-classification"
        path = (
            self.readiness
            / _safe_component(source_revision_id)
            / f"{_safe_component(readiness_key)}.json"
        )
        self._write_json_once(path, payload)
        self._register_classification_artifact(
            "readiness",
            (source_revision_id, readiness_key),
            path,
        )
        return payload

    def publish_materialized_event(
        self,
        *,
        source_revision_id: str,
        classification_input_fingerprint: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Durably publish a safe materialized event before its readiness receipt."""
        _required_string(source_revision_id, "source_revision_id")
        _sha256_value(
            classification_input_fingerprint,
            "classification_input_fingerprint",
        )
        path = (
            self.events
            / _safe_component(source_revision_id)
            / f"{classification_input_fingerprint}.json"
        )
        if path.exists():
            self._reconcile_materialized_event(
                source_revision_id=source_revision_id,
                classification_input_fingerprint=(
                    classification_input_fingerprint
                ),
                path=path,
            )
        self._write_json_once(path, payload)
        self._register_classification_artifact(
            "events",
            (source_revision_id, classification_input_fingerprint),
            path,
        )

    def read_materialized_event(
        self,
        source_revision_id: str,
        *,
        classification_input_fingerprint: str,
    ) -> dict[str, Any] | None:
        _sha256_value(
            classification_input_fingerprint,
            "classification_input_fingerprint",
        )
        path = (
            self.events
            / _safe_component(source_revision_id)
            / f"{classification_input_fingerprint}.json"
        )
        if not path.exists():
            return None
        self._reconcile_materialized_event(
            source_revision_id=source_revision_id,
            classification_input_fingerprint=classification_input_fingerprint,
            path=path,
        )
        self._require_registered_classification_artifact(
            "events",
            (source_revision_id, classification_input_fingerprint),
            path,
        )
        return self._read_json_object(path, "materialized event")

    def iter_readiness(self, source_revision_id: str) -> tuple[dict[str, Any], ...]:
        path = self.readiness / _safe_component(source_revision_id)
        if not path.exists():
            return ()
        self._reconcile_readiness_directory(
            source_revision_id=source_revision_id,
            path=path,
        )
        state = self._classification_artifact_state("readiness", source_revision_id)
        values: list[dict[str, Any]] = []
        for readiness_key in sorted(state):
            item = path / f"{_safe_component(readiness_key)}.json"
            self._require_registered_classification_artifact(
                "readiness", (source_revision_id, readiness_key), item
            )
            values.append(self._read_json_object(item, "readiness receipt"))
        return tuple(values)

    def read_readiness(
        self,
        source_revision_id: str,
        *,
        classification_input_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        values = self.iter_readiness(source_revision_id)
        if classification_input_fingerprint is not None:
            _sha256_value(
                classification_input_fingerprint,
                "classification_input_fingerprint",
            )
            values = tuple(
                value
                for value in values
                if value.get("classification_input_fingerprint")
                == classification_input_fingerprint
            )
        if not values:
            return None
        if len(values) != 1:
            raise ExternalEventArchiveError(
                "readiness profile is ambiguous; select an exact classification input"
            )
        return values[0]

    @contextmanager
    def classification_lease(
        self,
        classification_input_fingerprint: str,
        *,
        owner_id: str,
        timeout_seconds: float = 5.0,
        stale_after: timedelta = timedelta(minutes=10),
    ) -> Iterator[bool]:
        _sha256_value(classification_input_fingerprint, "classification_input_fingerprint")
        _required_string(owner_id, "owner_id")
        base = self.classifications / classification_input_fingerprint
        lease_dir = base / ".lease"
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        acquired = False
        while True:
            base.mkdir(parents=True, exist_ok=True)
            try:
                lease_dir.mkdir()
                self._atomic_replace(
                    lease_dir / "lease.json",
                    _json_bytes({"owner_id": owner_id, "acquired_at": to_utc_iso(self._now())}),
                )
                acquired = True
                break
            except FileExistsError:
                canonical = self.read_canonical_claim(classification_input_fingerprint)
                if canonical is not None:
                    break
                if self._lease_is_stale(lease_dir, stale_after):
                    # A stale lease is removed only while the global archive lock is held
                    # and only after proving a canonical claim is still absent.
                    with self.manifest_lock(name=f"classification-{classification_input_fingerprint[:16]}"):
                        if self.read_canonical_claim(classification_input_fingerprint) is None and self._lease_is_stale(lease_dir, stale_after):
                            for child in lease_dir.glob("*"):
                                child.unlink(missing_ok=True)
                            try:
                                lease_dir.rmdir()
                            except OSError:
                                pass
                    continue
                if time.monotonic() >= deadline:
                    break
                self._sleeper(min(0.05, max(0.0, deadline - time.monotonic())))
            except OSError as exc:
                raise ExternalEventArchiveError("classification lease acquisition failed") from exc
        try:
            yield acquired
        finally:
            if acquired:
                for child in lease_dir.glob("*"):
                    child.unlink(missing_ok=True)
                try:
                    lease_dir.rmdir()
                except OSError:
                    pass

    def publish_classification_attempt(
        self,
        *,
        classification_input_fingerprint: str,
        attempt_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        _sha256_value(classification_input_fingerprint, "classification_input_fingerprint")
        _required_string(attempt_id, "attempt_id")
        payload_attempt_id = payload.get("classification_attempt_id")
        if payload_attempt_id != attempt_id:
            raise ExternalEventArchiveError(
                "classification attempt payload identity changed"
            )
        payload_input_fingerprint = payload.get(
            "classification_input_fingerprint"
        )
        if (
            payload_input_fingerprint is not None
            and payload_input_fingerprint != classification_input_fingerprint
        ):
            raise ExternalEventArchiveError(
                "classification attempt input identity changed"
            )
        if "archive_published_at" in payload:
            raise ExternalEventArchiveError(
                "classification attempt cannot supply archive publication time"
            )
        stored_payload = {
            **dict(payload),
            "archive_published_at": to_utc_iso(self._now()),
        }
        path = (
            self.classifications
            / classification_input_fingerprint
            / "attempts"
            / f"{_safe_component(attempt_id)}.json"
        )
        self._write_json_once(path, stored_payload)
        self._register_classification_artifact(
            "attempts",
            (classification_input_fingerprint, attempt_id),
            path,
        )

    def iter_classification_attempts(self, classification_input_fingerprint: str) -> tuple[dict[str, Any], ...]:
        _sha256_value(classification_input_fingerprint, "classification_input_fingerprint")
        path = self.classifications / classification_input_fingerprint / "attempts"
        if not path.exists():
            return ()
        self.reconcile_classification_artifacts(
            classification_input_fingerprint=classification_input_fingerprint
        )
        state = self._classification_artifact_state(
            "attempts", classification_input_fingerprint
        )
        values: list[dict[str, Any]] = []
        for attempt_id in sorted(state):
            item = path / f"{_safe_component(attempt_id)}.json"
            self._require_registered_classification_artifact(
                "attempts", (classification_input_fingerprint, attempt_id), item
            )
            values.append(self._read_json_object(item, "classification attempt"))
        return tuple(values)

    def claim_canonical_result(
        self,
        *,
        classification_input_fingerprint: str,
        attempt_id: str,
        complete_output_fingerprint: str,
        policy_output_fingerprint: str,
        profile_hash: str,
        evidence_ready_at: datetime,
    ) -> dict[str, Any]:
        for value, name in (
            (classification_input_fingerprint, "classification_input_fingerprint"),
            (complete_output_fingerprint, "complete_output_fingerprint"),
            (policy_output_fingerprint, "policy_output_fingerprint"),
            (profile_hash, "profile_hash"),
        ):
            _sha256_value(value, name)
        _required_string(attempt_id, "attempt_id")
        payload = {
            "classification_input_fingerprint": classification_input_fingerprint,
            "canonical_classification_attempt_id": attempt_id,
            "complete_output_fingerprint": complete_output_fingerprint,
            "policy_output_fingerprint": policy_output_fingerprint,
            "profile_hash": profile_hash,
            "evidence_ready_at": to_utc_iso(ensure_timezone_aware_utc(evidence_ready_at)),
            "durably_published_at": to_utc_iso(self._now()),
        }
        path = self.classifications / classification_input_fingerprint / "canonical_claim.json"
        encoded = _json_bytes(payload)
        self._publish_first_writer(path, encoded)
        self._register_classification_artifact(
            "canonical_claims",
            (classification_input_fingerprint,),
            path,
        )
        return self._read_json_object(path, "canonical classification claim")

    def read_canonical_claim(self, classification_input_fingerprint: str) -> dict[str, Any] | None:
        _sha256_value(classification_input_fingerprint, "classification_input_fingerprint")
        path = self.classifications / classification_input_fingerprint / "canonical_claim.json"
        if not path.exists():
            return None
        self.reconcile_classification_artifacts(
            classification_input_fingerprint=classification_input_fingerprint
        )
        self._require_registered_classification_artifact(
            "canonical_claims", (classification_input_fingerprint,), path
        )
        return self._read_json_object(path, "canonical classification claim")

    def reconcile_classification_artifacts(
        self,
        *,
        classification_input_fingerprint: str | None = None,
    ) -> None:
        """Adopt immutable files left between durable write and manifest update.

        Publication deliberately writes and fsyncs an immutable artifact before
        making it visible through the mutable manifest.  A process failure in
        that narrow interval must not cause a paid classification to be issued
        again.  Recovery validates every identity from the artifact itself,
        then idempotently publishes the missing manifest entry.
        """

        if classification_input_fingerprint is None:
            input_directories = tuple(
                path
                for path in (
                    sorted(self.classifications.iterdir())
                    if self.classifications.exists()
                    else ()
                )
                if path.is_dir()
            )
        else:
            _sha256_value(
                classification_input_fingerprint,
                "classification_input_fingerprint",
            )
            input_directory = (
                self.classifications / classification_input_fingerprint
            )
            if not input_directory.exists():
                return
            input_directories = (input_directory,)

        for input_directory in input_directories:
            input_fingerprint = input_directory.name
            _sha256_value(
                input_fingerprint,
                "classification_input_fingerprint",
            )
            attempts_directory = input_directory / "attempts"
            registered_attempts = self._classification_artifact_state(
                "attempts", input_fingerprint
            )
            if attempts_directory.exists():
                for attempt_path in sorted(attempts_directory.glob("*.json")):
                    attempt = self._read_json_object(
                        attempt_path, "classification attempt"
                    )
                    attempt_id = _required_string(
                        attempt.get("classification_attempt_id"),
                        "classification_attempt_id",
                    )
                    if _safe_component(attempt_id) != attempt_path.stem:
                        raise ExternalEventArchiveError(
                            "classification attempt path identity changed"
                        )
                    payload_input = attempt.get(
                        "classification_input_fingerprint"
                    )
                    if (
                        payload_input is not None
                        and payload_input != input_fingerprint
                    ):
                        raise ExternalEventArchiveError(
                            "classification attempt input identity changed"
                        )
                    _required_archive_publication_time(
                        attempt.get("archive_published_at"),
                        "classification attempt",
                    )
                    if attempt_id not in registered_attempts:
                        self._register_classification_artifact(
                            "attempts",
                            (input_fingerprint, attempt_id),
                            attempt_path,
                        )

            canonical_path = input_directory / "canonical_claim.json"
            if canonical_path.exists():
                canonical = self._read_json_object(
                    canonical_path, "canonical classification claim"
                )
                if (
                    canonical.get("classification_input_fingerprint")
                    != input_fingerprint
                ):
                    raise ExternalEventArchiveError(
                        "canonical classification claim input identity changed"
                    )
                _required_string(
                    canonical.get("canonical_classification_attempt_id"),
                    "canonical_classification_attempt_id",
                )
                for name in (
                    "complete_output_fingerprint",
                    "policy_output_fingerprint",
                    "profile_hash",
                ):
                    _sha256_value(canonical.get(name), name)
                _required_archive_publication_time(
                    canonical.get("evidence_ready_at"),
                    "canonical evidence readiness",
                )
                _required_archive_publication_time(
                    canonical.get("durably_published_at"),
                    "canonical publication",
                )
                registered_claims = self._classification_artifact_state(
                    "canonical_claims"
                )
                if input_fingerprint not in registered_claims:
                    self._register_classification_artifact(
                        "canonical_claims",
                        (input_fingerprint,),
                        canonical_path,
                    )

        if classification_input_fingerprint is None:
            if self.events.exists():
                for source_directory in sorted(self.events.iterdir()):
                    if not source_directory.is_dir():
                        continue
                    for event_path in sorted(source_directory.glob("*.json")):
                        event = self._read_json_object(
                            event_path, "materialized event"
                        )
                        source_revision_id = _required_string(
                            event.get("source_revision_id"),
                            "source_revision_id",
                        )
                        if _safe_component(source_revision_id) != (
                            source_directory.name
                        ):
                            raise ExternalEventArchiveError(
                                "materialized event path identity changed"
                            )
                        self._reconcile_materialized_event(
                            source_revision_id=source_revision_id,
                            classification_input_fingerprint=event_path.stem,
                            path=event_path,
                            payload=event,
                        )
            if self.readiness.exists():
                for readiness_directory in sorted(self.readiness.iterdir()):
                    if not readiness_directory.is_dir():
                        continue
                    self._reconcile_readiness_directory(
                        source_revision_id=None,
                        path=readiness_directory,
                    )

    def _reconcile_materialized_event(
        self,
        *,
        source_revision_id: str,
        classification_input_fingerprint: str,
        path: Path,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        _required_string(source_revision_id, "source_revision_id")
        _sha256_value(
            classification_input_fingerprint,
            "classification_input_fingerprint",
        )
        state = self._classification_artifact_state(
            "events", source_revision_id
        )
        if classification_input_fingerprint in state:
            return
        event = (
            dict(payload)
            if payload is not None
            else self._read_json_object(path, "materialized event")
        )
        if event.get("source_revision_id") != source_revision_id or event.get(
            "classification_input_fingerprint"
        ) != classification_input_fingerprint:
            raise ExternalEventArchiveError(
                "materialized event embedded identity changed"
            )
        if path.parent.name != _safe_component(source_revision_id) or (
            path.stem != classification_input_fingerprint
        ):
            raise ExternalEventArchiveError(
                "materialized event path identity changed"
            )
        _required_string(event.get("context_event_id"), "context_event_id")
        canonical, attempt = self._canonical_artifact_lineage(
            classification_input_fingerprint
        )
        for event_name, canonical_name in (
            (
                "canonical_classification_attempt_id",
                "canonical_classification_attempt_id",
            ),
            ("complete_output_fingerprint", "complete_output_fingerprint"),
            ("policy_output_fingerprint", "policy_output_fingerprint"),
        ):
            if event.get(event_name) != canonical.get(canonical_name):
                raise ExternalEventArchiveError(
                    "materialized event canonical ownership changed"
                )
        revision = self._read_revision_by_id(source_revision_id)
        if (
            event.get("source") != revision.source
            or event.get("source_fact_id") != revision.source_fact_id
            or event.get("source_id") != revision.source_fact_id
            or event.get("document_hash") != revision.document_hash
        ):
            raise ExternalEventArchiveError(
                "materialized event source revision lineage changed"
            )
        event_times = {
            name: _required_archive_publication_time(
                event.get(name), f"materialized event {name}"
            )
            for name in (
                "system_observed_at",
                "archived_at",
                "normalized_at",
                "classified_at",
                "validated_at",
                "lifecycle_effective_at",
            )
        }
        if (
            event_times["system_observed_at"] != revision.system_observed_at
            or event_times["archived_at"] != revision.archived_at
            or event_times["normalized_at"] != revision.normalized_at
        ):
            raise ExternalEventArchiveError(
                "materialized event source timestamps changed"
            )
        for name in ("classified_at", "validated_at"):
            if event_times[name] != _required_archive_publication_time(
                attempt.get(name), f"canonical attempt {name}"
            ):
                raise ExternalEventArchiveError(
                    "materialized event attempt timestamp changed"
                )
        validate_external_event_lineage_chronology(
            system_observed_at=revision.system_observed_at,
            archived_at=revision.archived_at,
            normalized_at=revision.normalized_at,
            classified_at=event_times["classified_at"],
            validated_at=event_times["validated_at"],
            attempt_published_at=attempt.get("archive_published_at"),
            canonical_published_at=canonical.get("durably_published_at"),
            readiness_published_at=None,
            evidence_ready_at=event.get("evidence_ready_at"),
        )
        self._register_classification_artifact(
            "events",
            (source_revision_id, classification_input_fingerprint),
            path,
        )

    def _reconcile_readiness_directory(
        self,
        *,
        source_revision_id: str | None,
        path: Path,
    ) -> None:
        state_root = self._classification_artifact_state("readiness")
        for readiness_path in sorted(path.glob("*.json")):
            receipt = self._read_json_object(
                readiness_path, "readiness receipt"
            )
            embedded_revision_id = _required_string(
                receipt.get("source_revision_id"), "source_revision_id"
            )
            if source_revision_id is not None and (
                embedded_revision_id != source_revision_id
            ):
                raise ExternalEventArchiveError(
                    "readiness receipt embedded identity changed"
                )
            if path.name != _safe_component(embedded_revision_id):
                raise ExternalEventArchiveError(
                    "readiness receipt path identity changed"
                )
            raw_input_fingerprint = receipt.get(
                "classification_input_fingerprint"
            )
            readiness_key = (
                "no-classification"
                if raw_input_fingerprint is None
                else _sha256_value(
                    raw_input_fingerprint,
                    "classification_input_fingerprint",
                )
            )
            if readiness_path.stem != _safe_component(readiness_key):
                raise ExternalEventArchiveError(
                    "readiness receipt key identity changed"
                )
            registered_for_revision = state_root.get(embedded_revision_id, {})
            if not isinstance(registered_for_revision, Mapping):
                raise ExternalEventArchiveError(
                    "classification artifact manifest has invalid identity shape"
                )
            if readiness_key in registered_for_revision:
                continue
            archive_published_at = _required_archive_publication_time(
                receipt.get("archive_published_at"),
                "readiness archive publication",
            )
            evidence_ready_at = _required_archive_publication_time(
                receipt.get("evidence_ready_at"),
                "evidence readiness",
            )
            if evidence_ready_at < archive_published_at:
                raise ExternalEventArchiveError(
                    "evidence readiness predates archive publication"
                )
            if not isinstance(receipt.get("policy_eligible"), bool):
                raise ExternalEventArchiveError(
                    "readiness policy_eligible must be bool"
                )
            if raw_input_fingerprint is None:
                if any(
                    receipt.get(name) is not None
                    for name in (
                        "canonical_classification_attempt_id",
                        "complete_output_fingerprint",
                        "policy_output_fingerprint",
                        "profile_hash",
                        "classification_profile",
                    )
                ):
                    raise ExternalEventArchiveError(
                        "unclassified readiness contains classification identity"
                    )
                revision = self._read_revision_by_id(embedded_revision_id)
                validate_external_event_lineage_chronology(
                    system_observed_at=revision.system_observed_at,
                    archived_at=revision.archived_at,
                    normalized_at=revision.normalized_at,
                    classified_at=None,
                    validated_at=None,
                    attempt_published_at=None,
                    canonical_published_at=None,
                    readiness_published_at=archive_published_at,
                    evidence_ready_at=evidence_ready_at,
                )
            else:
                canonical, attempt = self._canonical_artifact_lineage(
                    readiness_key
                )
                profile_hash = _sha256_value(
                    receipt.get("profile_hash"), "profile_hash"
                )
                profile = receipt.get("classification_profile")
                if not isinstance(profile, Mapping) or (
                    _hash_payload(dict(profile)) != profile_hash
                ):
                    raise ExternalEventArchiveError(
                        "readiness classification profile hash changed"
                    )
                for receipt_name, canonical_name in (
                    (
                        "canonical_classification_attempt_id",
                        "canonical_classification_attempt_id",
                    ),
                    (
                        "complete_output_fingerprint",
                        "complete_output_fingerprint",
                    ),
                    (
                        "policy_output_fingerprint",
                        "policy_output_fingerprint",
                    ),
                    ("profile_hash", "profile_hash"),
                ):
                    if receipt.get(receipt_name) != canonical.get(
                        canonical_name
                    ):
                        raise ExternalEventArchiveError(
                            "readiness canonical ownership changed"
                        )
                if receipt.get("classification_status") != attempt.get(
                    "status"
                ):
                    raise ExternalEventArchiveError(
                        "readiness classification status changed"
                    )
                revision = self._read_revision_by_id(embedded_revision_id)
                validate_external_event_lineage_chronology(
                    system_observed_at=revision.system_observed_at,
                    archived_at=revision.archived_at,
                    normalized_at=revision.normalized_at,
                    classified_at=attempt.get("classified_at"),
                    validated_at=attempt.get("validated_at"),
                    attempt_published_at=attempt.get("archive_published_at"),
                    canonical_published_at=canonical.get("durably_published_at"),
                    readiness_published_at=archive_published_at,
                    evidence_ready_at=evidence_ready_at,
                )
            self._register_classification_artifact(
                "readiness",
                (embedded_revision_id, readiness_key),
                readiness_path,
            )

    def _canonical_artifact_lineage(
        self,
        classification_input_fingerprint: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        canonical = self.read_canonical_claim(
            classification_input_fingerprint
        )
        if canonical is None:
            raise ExternalEventArchiveError(
                "classification artifact lacks a canonical claim"
            )
        attempt_id = _required_string(
            canonical.get("canonical_classification_attempt_id"),
            "canonical_classification_attempt_id",
        )
        attempts = self.iter_classification_attempts(
            classification_input_fingerprint
        )
        attempt = next(
            (
                value
                for value in attempts
                if value.get("classification_attempt_id") == attempt_id
            ),
            None,
        )
        if attempt is None:
            raise ExternalEventArchiveError(
                "classification artifact lacks its canonical attempt"
            )
        for name in (
            "complete_output_fingerprint",
            "policy_output_fingerprint",
            "profile_hash",
        ):
            canonical_value = _sha256_value(canonical.get(name), name)
            if attempt.get(name) != canonical_value:
                raise ExternalEventArchiveError(
                    "canonical attempt lineage changed"
                )
        profile = attempt.get("profile")
        if not isinstance(profile, Mapping) or _hash_payload(
            dict(profile)
        ) != canonical.get("profile_hash"):
            raise ExternalEventArchiveError(
                "canonical attempt profile hash changed"
            )
        return canonical, attempt

    def _read_revision_by_id(
        self, source_revision_id: str
    ) -> ExternalSourceRevision:
        """Find one registered source revision without inferring its owner."""

        matches = [
            revision
            for revision in self.iter_revisions()
            if revision.source_revision_id == source_revision_id
        ]
        if len(matches) != 1:
            raise ExternalEventArchiveError(
                "materialized artifact source revision ownership is ambiguous"
            )
        return matches[0]

    def detect_classification_conflict(self, classification_input_fingerprint: str) -> dict[str, Any] | None:
        attempts = self.iter_classification_attempts(classification_input_fingerprint)
        successful = [value for value in attempts if value.get("durably_published") is True and value.get("validation_outcome") is True]
        output_pairs = sorted(
            {
                (
                    str(value.get("complete_output_fingerprint")),
                    str(value.get("policy_output_fingerprint")),
                )
                for value in successful
            }
        )
        if len(output_pairs) <= 1:
            return None
        output_hashes = sorted({value[0] for value in output_pairs})
        policy_output_hashes = sorted({value[1] for value in output_pairs})
        field_values: dict[str, set[str]] = {}
        for attempt in successful:
            normalized = attempt.get("normalized_output")
            if not isinstance(normalized, Mapping):
                continue
            for name, value in normalized.items():
                field_values.setdefault(str(name), set()).add(_stable_json(value))
        conflicting_fields = sorted(name for name, values in field_values.items() if len(values) > 1)
        attempt_ids = sorted(str(value.get("classification_attempt_id")) for value in successful)
        profile_hashes = sorted({str(value.get("profile_hash")) for value in successful})
        conflict_id = "classification_conflict_" + _hash_payload(
            {
                "input": classification_input_fingerprint,
                "attempt_ids": attempt_ids,
                "output_hashes": output_hashes,
                "policy_output_hashes": policy_output_hashes,
                "profile_hashes": profile_hashes,
            }
        )
        conflict_path = self.conflicts / f"{conflict_id}.json"
        if conflict_path.exists():
            existing = self._read_json_object(
                conflict_path, "classification conflict"
            )
            if (
                existing.get("classification_input_fingerprint")
                != classification_input_fingerprint
                or existing.get("attempt_ids") != attempt_ids
                or existing.get("complete_output_fingerprints") != output_hashes
                or existing.get("policy_output_fingerprints")
                != policy_output_hashes
            ):
                raise ExternalEventArchiveError(
                    "immutable classification conflict identity changed"
                )
            return existing
        payload = {
            "classification_conflict_id": conflict_id,
            "classification_input_fingerprint": classification_input_fingerprint,
            "attempt_ids": attempt_ids,
            "complete_output_fingerprints": output_hashes,
            "policy_output_fingerprints": policy_output_hashes,
            "profile_hashes": profile_hashes,
            "conflicting_fields": conflicting_fields,
            "canonical_claim": self.read_canonical_claim(classification_input_fingerprint),
            "detected_at": to_utc_iso(self._now()),
        }
        self._write_json_once(conflict_path, payload)
        return payload

    def publish_conflict_resolution(
        self,
        *,
        conflict_id: str,
        decision: ConflictResolutionDecision,
        reviewer: str,
        reason: str,
        chosen_attempt_id: str | None = None,
        chosen_complete_output_fingerprint: str | None = None,
        chosen_policy_output_fingerprint: str | None = None,
        new_profile_hash: str | None = None,
    ) -> dict[str, Any]:
        _required_string(conflict_id, "conflict_id")
        _required_string(reviewer, "reviewer")
        _required_string(reason, "reason")
        conflict_path = self.conflicts / f"{_safe_component(conflict_id)}.json"
        conflict = self._read_json_object(conflict_path, "classification conflict")
        if conflict.get("classification_conflict_id") != conflict_id:
            raise ExternalEventArchiveError("classification conflict identity changed")
        if not isinstance(decision, ConflictResolutionDecision):
            raise ExternalEventArchiveError("decision must be a ConflictResolutionDecision")
        if decision is ConflictResolutionDecision.KEEP_FIRST_DURABLY_PUBLISHED:
            if new_profile_hash is not None:
                raise ExternalEventArchiveError(
                    "KEEP_FIRST does not accept a replacement profile"
                )
            canonical = conflict.get("canonical_claim")
            if not isinstance(canonical, Mapping):
                raise ExternalEventArchiveError("KEEP_FIRST requires proven canonical chronology")
            canonical_attempt = canonical.get("canonical_classification_attempt_id")
            canonical_output = canonical.get("complete_output_fingerprint")
            canonical_policy_output = canonical.get("policy_output_fingerprint")
            if (
                chosen_attempt_id != canonical_attempt
                or chosen_complete_output_fingerprint != canonical_output
                or chosen_policy_output_fingerprint != canonical_policy_output
            ):
                raise ExternalEventArchiveError("KEEP_FIRST must choose the proven canonical result")
            attempts = self.iter_classification_attempts(str(conflict["classification_input_fingerprint"]))
            chosen = next((item for item in attempts if item.get("classification_attempt_id") == chosen_attempt_id), None)
            later = [item for item in attempts if item.get("classification_attempt_id") != chosen_attempt_id]
            if (
                chosen is None
                or chosen.get("complete_output_fingerprint") != canonical_output
                or chosen.get("policy_output_fingerprint")
                != canonical_policy_output
                or chosen.get("classification_origin") != "LIVE_SYSTEM"
                or any(
                    item.get("classification_origin") != "BACKFILL"
                    or str(item.get("archive_published_at", ""))
                    <= str(canonical.get("durably_published_at", ""))
                    for item in later
                )
            ):
                raise ExternalEventArchiveError("KEEP_FIRST chronology is ambiguous")
        elif decision is ConflictResolutionDecision.ABSTAIN_INPUT:
            if any(
                value is not None
                for value in (
                    chosen_attempt_id,
                    chosen_complete_output_fingerprint,
                    chosen_policy_output_fingerprint,
                    new_profile_hash,
                )
            ):
                raise ExternalEventArchiveError(
                    "ABSTAIN_INPUT does not choose an attempt or profile"
                )
        elif decision is ConflictResolutionDecision.RECLASSIFY_UNDER_NEW_PROFILE:
            if (
                chosen_attempt_id is not None
                or chosen_complete_output_fingerprint is not None
                or chosen_policy_output_fingerprint is not None
            ):
                raise ExternalEventArchiveError(
                    "RECLASSIFY does not choose an old attempt"
                )
            if new_profile_hash is None:
                raise ExternalEventArchiveError("RECLASSIFY requires new_profile_hash")
            _sha256_value(new_profile_hash, "new_profile_hash")
            if new_profile_hash in set(conflict.get("profile_hashes", [])):
                raise ExternalEventArchiveError("RECLASSIFY requires a genuinely new profile")
        identity = {
            "conflict_id": conflict_id,
            "decision": decision.value,
            "reviewer": reviewer,
            "reason": reason,
            "chosen_attempt_id": chosen_attempt_id,
            "chosen_complete_output_fingerprint": chosen_complete_output_fingerprint,
            "chosen_policy_output_fingerprint": chosen_policy_output_fingerprint,
            "new_profile_hash": new_profile_hash,
        }
        resolution_id = "classification_resolution_" + _hash_payload(identity)
        self.reconcile_conflict_resolutions()
        with self.manifest_lock(name="classification-resolutions"):
            manifest = self.load_resolution_manifest()
            previous_generation = int(manifest.get("generation", 0))
            existing_resolution_id = manifest.get("resolutions", {}).get(
                str(conflict["classification_input_fingerprint"])
            )
            if existing_resolution_id == resolution_id:
                existing_path = self.resolutions / f"{resolution_id}.json"
                return self._validate_conflict_resolution_artifact(
                    self._read_json_object(
                        existing_path, "classification resolution"
                    ),
                    existing_path,
                )
            payload = {
                "classification_resolution_id": resolution_id,
                **identity,
                "classification_input_fingerprint": conflict["classification_input_fingerprint"],
                "created_at": to_utc_iso(self._now()),
                "previous_manifest_generation": previous_generation,
                "manifest_generation": previous_generation + 1,
            }
            self._write_json_once(self.resolutions / f"{resolution_id}.json", payload)
            resolutions = manifest.setdefault("resolutions", {})
            resolutions[str(conflict["classification_input_fingerprint"])] = resolution_id
            manifest["generation"] = previous_generation + 1
            manifest["updated_at"] = payload["created_at"]
            self.save_resolution_manifest(manifest)
        return payload

    def load_conflict_resolution(self, classification_input_fingerprint: str) -> dict[str, Any] | None:
        _sha256_value(
            classification_input_fingerprint,
            "classification_input_fingerprint",
        )
        self.reconcile_conflict_resolutions()
        manifest = self.load_resolution_manifest()
        resolutions = manifest.get("resolutions", {})
        if not isinstance(resolutions, Mapping):
            raise ExternalEventArchiveError("resolution manifest has invalid shape")
        resolution_id = resolutions.get(classification_input_fingerprint)
        if resolution_id is None:
            return None
        resolution_path = (
            self.resolutions / f"{_safe_component(str(resolution_id))}.json"
        )
        payload = self._validate_conflict_resolution_artifact(
            self._read_json_object(
                resolution_path,
                "classification resolution",
            ),
            resolution_path,
        )
        if (
            payload.get("classification_input_fingerprint")
            != classification_input_fingerprint
        ):
            raise ExternalEventArchiveError(
                "classification resolution input identity changed"
            )
        return payload

    def reconcile_conflict_resolutions(self) -> None:
        """Adopt a complete immutable resolution left before manifest save."""

        if not self.resolutions.exists():
            return
        with self.manifest_lock(name="classification-resolutions"):
            while True:
                manifest = self.load_resolution_manifest()
                current_generation = _non_negative_integer(
                    manifest.get("generation"),
                    "resolution manifest generation",
                )
                mappings = manifest.get("resolutions")
                if not isinstance(mappings, dict):
                    raise ExternalEventArchiveError(
                        "resolution manifest has invalid shape"
                    )
                visible_ids = {str(value) for value in mappings.values()}
                candidates: list[tuple[dict[str, Any], Path]] = []
                future_generations: list[int] = []
                for resolution_path in sorted(self.resolutions.glob("*.json")):
                    raw = self._read_json_object(
                        resolution_path, "classification resolution"
                    )
                    resolution_id = _required_string(
                        raw.get("classification_resolution_id"),
                        "classification_resolution_id",
                    )
                    if resolution_id in visible_ids:
                        continue
                    previous_generation = _non_negative_integer(
                        raw.get("previous_manifest_generation"),
                        "previous_manifest_generation",
                    )
                    manifest_generation = _non_negative_integer(
                        raw.get("manifest_generation"),
                        "manifest_generation",
                    )
                    if manifest_generation != previous_generation + 1:
                        raise ExternalEventArchiveError(
                            "classification resolution generation chain changed"
                        )
                    if previous_generation == current_generation:
                        candidates.append(
                            (
                                self._validate_conflict_resolution_artifact(
                                    raw, resolution_path
                                ),
                                resolution_path,
                            )
                        )
                    elif previous_generation > current_generation:
                        future_generations.append(previous_generation)
                if len(candidates) > 1:
                    raise ExternalEventArchiveError(
                        "ambiguous orphan classification resolutions"
                    )
                if not candidates:
                    if future_generations:
                        raise ExternalEventArchiveError(
                            "classification resolution generation gap"
                        )
                    return
                payload, _path = candidates[0]
                input_fingerprint = _sha256_value(
                    payload.get("classification_input_fingerprint"),
                    "classification_input_fingerprint",
                )
                mappings[input_fingerprint] = payload[
                    "classification_resolution_id"
                ]
                manifest["generation"] = payload["manifest_generation"]
                manifest["updated_at"] = payload["created_at"]
                self.save_resolution_manifest(manifest)

    def _validate_conflict_resolution_artifact(
        self,
        payload: Mapping[str, Any],
        path: Path,
    ) -> dict[str, Any]:
        value = dict(payload)
        input_fingerprint = _sha256_value(
            value.get("classification_input_fingerprint"),
            "classification_input_fingerprint",
        )
        conflict_id = _required_string(value.get("conflict_id"), "conflict_id")
        reviewer = _required_string(value.get("reviewer"), "reviewer")
        reason = _required_string(value.get("reason"), "reason")
        try:
            decision = ConflictResolutionDecision(str(value.get("decision")))
        except ValueError as exc:
            raise ExternalEventArchiveError(
                "classification resolution decision is invalid"
            ) from exc
        _required_archive_publication_time(
            value.get("created_at"), "classification resolution creation"
        )
        previous_generation = _non_negative_integer(
            value.get("previous_manifest_generation"),
            "previous_manifest_generation",
        )
        manifest_generation = _non_negative_integer(
            value.get("manifest_generation"), "manifest_generation"
        )
        if manifest_generation != previous_generation + 1:
            raise ExternalEventArchiveError(
                "classification resolution generation chain changed"
            )
        identity = {
            "conflict_id": conflict_id,
            "decision": decision.value,
            "reviewer": reviewer,
            "reason": reason,
            "chosen_attempt_id": value.get("chosen_attempt_id"),
            "chosen_complete_output_fingerprint": value.get(
                "chosen_complete_output_fingerprint"
            ),
            "chosen_policy_output_fingerprint": value.get(
                "chosen_policy_output_fingerprint"
            ),
            "new_profile_hash": value.get("new_profile_hash"),
        }
        expected_id = "classification_resolution_" + _hash_payload(identity)
        if (
            value.get("classification_resolution_id") != expected_id
            or path.stem != expected_id
        ):
            raise ExternalEventArchiveError(
                "classification resolution identity changed"
            )
        conflict_path = self.conflicts / f"{_safe_component(conflict_id)}.json"
        conflict = self._read_json_object(
            conflict_path, "classification conflict"
        )
        if (
            conflict.get("classification_conflict_id") != conflict_id
            or conflict.get("classification_input_fingerprint")
            != input_fingerprint
        ):
            raise ExternalEventArchiveError(
                "classification resolution conflict identity changed"
            )
        profile_hashes = conflict.get("profile_hashes")
        if not isinstance(profile_hashes, list):
            raise ExternalEventArchiveError(
                "classification conflict profile hashes are invalid"
            )
        for profile_hash in profile_hashes:
            _sha256_value(profile_hash, "profile_hash")
        chosen_attempt_id = value.get("chosen_attempt_id")
        chosen_complete = value.get("chosen_complete_output_fingerprint")
        chosen_policy = value.get("chosen_policy_output_fingerprint")
        new_profile_hash = value.get("new_profile_hash")
        if decision is ConflictResolutionDecision.KEEP_FIRST_DURABLY_PUBLISHED:
            canonical = conflict.get("canonical_claim")
            if not isinstance(canonical, Mapping):
                raise ExternalEventArchiveError(
                    "KEEP_FIRST requires proven canonical chronology"
                )
            for name in (
                "complete_output_fingerprint",
                "policy_output_fingerprint",
                "profile_hash",
            ):
                _sha256_value(canonical.get(name), name)
            if (
                chosen_attempt_id
                != canonical.get("canonical_classification_attempt_id")
                or chosen_complete
                != canonical.get("complete_output_fingerprint")
                or chosen_policy != canonical.get("policy_output_fingerprint")
                or new_profile_hash is not None
            ):
                raise ExternalEventArchiveError(
                    "KEEP_FIRST must choose the proven canonical result"
                )
            attempts = self.iter_classification_attempts(input_fingerprint)
            chosen = next(
                (
                    item
                    for item in attempts
                    if item.get("classification_attempt_id")
                    == chosen_attempt_id
                ),
                None,
            )
            canonical_published_at = _required_archive_publication_time(
                canonical.get("durably_published_at"),
                "canonical durable publication",
            )
            later = [
                item
                for item in attempts
                if item.get("classification_attempt_id") != chosen_attempt_id
            ]
            if (
                chosen is None
                or chosen.get("classification_origin") != "LIVE_SYSTEM"
                or chosen.get("complete_output_fingerprint") != chosen_complete
                or chosen.get("policy_output_fingerprint") != chosen_policy
                or any(
                    item.get("classification_origin") != "BACKFILL"
                    or _required_archive_publication_time(
                        item.get("archive_published_at"),
                        "classification attempt publication",
                    )
                    <= canonical_published_at
                    for item in later
                )
            ):
                raise ExternalEventArchiveError(
                    "KEEP_FIRST chronology is ambiguous"
                )
        elif decision is ConflictResolutionDecision.ABSTAIN_INPUT:
            if any(
                item is not None
                for item in (
                    chosen_attempt_id,
                    chosen_complete,
                    chosen_policy,
                    new_profile_hash,
                )
            ):
                raise ExternalEventArchiveError(
                    "ABSTAIN_INPUT does not choose an attempt or profile"
                )
        else:
            if any(
                item is not None
                for item in (
                    chosen_attempt_id,
                    chosen_complete,
                    chosen_policy,
                )
            ):
                raise ExternalEventArchiveError(
                    "RECLASSIFY does not choose an old attempt"
                )
            replacement_profile = _sha256_value(
                new_profile_hash, "new_profile_hash"
            )
            if replacement_profile in set(profile_hashes):
                raise ExternalEventArchiveError(
                    "RECLASSIFY requires a genuinely new profile"
                )
        return value

    def _register_classification_artifact(
        self,
        category: str,
        identities: tuple[str, ...],
        path: Path,
    ) -> None:
        """Make an immutable classification artifact visible in a pinned snapshot."""

        if not identities:
            raise ExternalEventArchiveError(
                "classification artifact identity must not be empty"
            )
        try:
            digest = sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ExternalEventArchiveError(
                "classification artifact is unavailable for publication"
            ) from exc
        with self.manifest_lock():
            manifest = self.load_manifest()
            artifacts = manifest.setdefault("classification_artifacts", {})
            state = artifacts.setdefault(category, {})
            if not isinstance(state, dict):
                raise ExternalEventArchiveError(
                    "classification artifact manifest has invalid shape"
                )
            cursor = state
            for identity in identities[:-1]:
                child = cursor.setdefault(identity, {})
                if not isinstance(child, dict):
                    raise ExternalEventArchiveError(
                        "classification artifact manifest has invalid identity shape"
                    )
                cursor = child
            leaf = identities[-1]
            existing = cursor.get(leaf)
            if existing is not None:
                if existing != digest:
                    raise ExternalEventArchiveError(
                        "immutable classification artifact hash changed"
                    )
                return
            cursor[leaf] = digest
            manifest["generation"] = int(manifest.get("generation", 0)) + 1
            manifest["updated_at"] = to_utc_iso(self._now())
            self.save_manifest(manifest)

    def _classification_artifact_state(
        self,
        category: str,
        *prefix: str,
    ) -> dict[str, Any]:
        artifacts = self.load_manifest().get("classification_artifacts", {})
        if not isinstance(artifacts, Mapping):
            raise ExternalEventArchiveError(
                "classification artifact manifest has invalid shape"
            )
        state: Any = artifacts.get(category, {})
        for identity in prefix:
            if not isinstance(state, Mapping):
                raise ExternalEventArchiveError(
                    "classification artifact manifest has invalid identity shape"
                )
            state = state.get(identity, {})
        if not isinstance(state, Mapping):
            raise ExternalEventArchiveError(
                "classification artifact manifest has invalid leaf shape"
            )
        return dict(state)

    def _require_registered_classification_artifact(
        self,
        category: str,
        identities: tuple[str, ...],
        path: Path,
    ) -> None:
        if not identities:
            raise ExternalEventArchiveError(
                "classification artifact identity must not be empty"
            )
        state: Any = self.load_manifest().get("classification_artifacts", {})
        if not isinstance(state, Mapping):
            raise ExternalEventArchiveError(
                "classification artifact manifest has invalid shape"
            )
        state = state.get(category, {})
        for identity in identities:
            if not isinstance(state, Mapping) or identity not in state:
                raise ExternalEventArchiveError(
                    "classification artifact is not part of the pinned archive"
                )
            state = state[identity]
        if not isinstance(state, str):
            raise ExternalEventArchiveError(
                "classification artifact manifest hash is invalid"
            )
        try:
            actual = sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ExternalEventArchiveError(
                "registered classification artifact is missing"
            ) from exc
        if actual != state:
            raise ExternalEventArchiveError(
                "registered classification artifact hash mismatch"
            )

    def save_coverage(self, coverage: SourceCoverage) -> None:
        if not isinstance(coverage, SourceCoverage):
            raise ExternalEventArchiveError("coverage must be SourceCoverage")
        path = self.coverage_dir / f"{_safe_component(coverage.source)}.json"
        payload = _json_bytes(coverage.to_payload())
        self._atomic_replace(path, payload)
        self._register_mutable_artifact(
            "coverage",
            coverage.source,
            path,
            expected_digest=sha256(payload).hexdigest(),
        )

    def load_coverage(self, source: str) -> SourceCoverage | None:
        path = self.coverage_dir / f"{_safe_component(source)}.json"
        if not path.exists():
            return None
        self.reconcile_mutable_artifacts()
        coverage, digest = self._read_coverage_file(path)
        self._require_registered_mutable_artifact(
            "coverage", source, path, actual_digest=digest
        )
        if coverage.source != source:
            raise ExternalEventArchiveError(
                "coverage manifest source identity changed"
            )
        return coverage

    def reconcile_mutable_artifacts(self) -> None:
        """Validate and adopt atomic coverage files after interrupted publication.

        Coverage is mutable but each replacement is atomic.  A process can stop
        after replacing the validated coverage file and before publishing its
        new digest in the archive manifest.  Reconciliation treats the
        self-identifying, schema-validated file as the recovery source, updates
        the digest/generation, and therefore invalidates every older research
        run pin before hydration can read the changed coverage.
        """

        if not self.coverage_dir.exists():
            return
        for path in sorted(self.coverage_dir.glob("*.json")):
            coverage, digest = self._read_coverage_file(path)
            if _safe_component(coverage.source) != path.stem:
                raise ExternalEventArchiveError(
                    "coverage manifest path identity changed"
                )
            self._register_mutable_artifact(
                "coverage",
                coverage.source,
                path,
                expected_digest=digest,
            )

    def _read_coverage_file(
        self, path: Path
    ) -> tuple[SourceCoverage, str]:
        try:
            payload = path.read_bytes()
            value = json.loads(payload.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExternalEventArchiveError(
                "coverage manifest could not be read"
            ) from exc
        if not isinstance(value, Mapping):
            raise ExternalEventArchiveError(
                "coverage manifest has invalid shape"
            )
        return SourceCoverage.from_payload(value), sha256(payload).hexdigest()

    def _register_mutable_artifact(
        self,
        category: str,
        identity: str,
        path: Path,
        *,
        expected_digest: str | None = None,
    ) -> None:
        with self.manifest_lock():
            try:
                digest = sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                raise ExternalEventArchiveError(
                    "mutable archive artifact is unavailable"
                ) from exc
            if expected_digest is not None and digest != expected_digest:
                raise ExternalEventArchiveError(
                    "mutable archive artifact changed during publication"
                )
            manifest = self.load_manifest()
            artifacts = manifest.setdefault("mutable_artifacts", {})
            state = artifacts.setdefault(category, {})
            if not isinstance(state, dict):
                raise ExternalEventArchiveError(
                    "mutable archive artifact manifest has invalid shape"
                )
            if state.get(identity) == digest:
                return
            state[identity] = digest
            manifest["generation"] = int(manifest.get("generation", 0)) + 1
            manifest["updated_at"] = to_utc_iso(self._now())
            self.save_manifest(manifest)

    def _require_registered_mutable_artifact(
        self,
        category: str,
        identity: str,
        path: Path,
        *,
        actual_digest: str | None = None,
    ) -> None:
        artifacts = self.load_manifest().get("mutable_artifacts", {})
        if not isinstance(artifacts, Mapping):
            raise ExternalEventArchiveError(
                "mutable archive artifact manifest has invalid shape"
            )
        state = artifacts.get(category, {})
        if not isinstance(state, Mapping) or not isinstance(state.get(identity), str):
            raise ExternalEventArchiveError(
                "mutable archive artifact is not part of the pinned archive"
            )
        if actual_digest is None:
            try:
                actual = sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                raise ExternalEventArchiveError(
                    "registered mutable archive artifact is missing"
                ) from exc
        else:
            actual = _sha256_value(actual_digest, "mutable artifact digest")
        if actual != state[identity]:
            raise ExternalEventArchiveError(
                "registered mutable archive artifact hash mismatch"
            )

    def load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {
                "schema_version": self.SCHEMA_VERSION,
                "generation": 0,
                "facts": {},
                "checkpoints": {},
                "observation_lineage": {},
                "earnings_packages": {},
                "classification_artifacts": {},
                "mutable_artifacts": {},
            }
        value = self._read_json_object(self.manifest_path, "external manifest")
        if not isinstance(value.get("facts"), dict) or not isinstance(value.get("checkpoints"), dict):
            raise ExternalEventArchiveError("external manifest has invalid shape")
        value.setdefault("observation_lineage", {})
        value.setdefault("earnings_packages", {})
        value.setdefault("classification_artifacts", {})
        value.setdefault("mutable_artifacts", {})
        return value

    def save_manifest(self, manifest: Mapping[str, Any]) -> None:
        self._atomic_replace(self.manifest_path, _json_bytes(manifest))

    def update_checkpoint(self, source: str, payload: Mapping[str, Any]) -> int:
        with self.manifest_lock():
            manifest = self.load_manifest()
            checkpoints = manifest.setdefault("checkpoints", {})
            current = checkpoints.get(source, {})
            generation = int(current.get("generation", 0)) + 1 if isinstance(current, Mapping) else 1
            checkpoints[source] = {**dict(payload), "generation": generation, "updated_at": to_utc_iso(self._now())}
            manifest["generation"] = int(manifest.get("generation", 0)) + 1
            manifest["updated_at"] = to_utc_iso(self._now())
            self.save_manifest(manifest)
            return generation

    def get_checkpoint(self, source: str) -> dict[str, Any] | None:
        value = self.load_manifest().get("checkpoints", {}).get(source)
        return None if value is None else dict(value)

    def load_resolution_manifest(self) -> dict[str, Any]:
        if not self.resolution_manifest_path.exists():
            return {"schema_version": 1, "generation": 0, "resolutions": {}}
        value = self._read_json_object(self.resolution_manifest_path, "resolution manifest")
        if not isinstance(value.get("resolutions"), dict):
            raise ExternalEventArchiveError("resolution manifest has invalid shape")
        return value

    def save_resolution_manifest(self, manifest: Mapping[str, Any]) -> None:
        self._atomic_replace(self.resolution_manifest_path, _json_bytes(manifest))

    @contextmanager
    def manifest_lock(
        self,
        *,
        name: str = "external-events",
        timeout_seconds: float = 10.0,
        incomplete_owner_grace_seconds: float = 1.0,
    ) -> Iterator[None]:
        lock_dir = self.root / "locks" / f"{_safe_component(name)}.lock"
        deadline = time.monotonic() + timeout_seconds
        owner_token = os.urandom(16).hex()
        while True:
            lock_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                lock_dir.mkdir()
                self._atomic_replace(
                    lock_dir / "owner.json",
                    _json_bytes(
                        {
                            "pid": os.getpid(),
                            "owner_token": owner_token,
                            "acquired_at": to_utc_iso(self._now()),
                        }
                    ),
                )
                break
            except FileExistsError:
                if self._reclaim_dead_manifest_lock(
                    lock_dir,
                    incomplete_owner_grace_seconds=incomplete_owner_grace_seconds,
                ):
                    continue
                if time.monotonic() >= deadline:
                    raise ExternalEventArchiveError("archive manifest lock timed out")
                self._sleeper(0.02)
            except OSError as exc:
                raise ExternalEventArchiveError("archive manifest lock failed") from exc
        try:
            yield
        finally:
            try:
                owner = self._read_json_object(lock_dir / "owner.json", "archive lock")
                if owner.get("owner_token") != owner_token:
                    raise ExternalEventArchiveError(
                        "archive manifest lock ownership changed"
                    )
                (lock_dir / "owner.json").unlink()
                lock_dir.rmdir()
            except ExternalEventArchiveError:
                raise
            except OSError as exc:
                raise ExternalEventArchiveError("archive manifest lock release failed") from exc

    def _reclaim_dead_manifest_lock(
        self,
        lock_dir: Path,
        *,
        incomplete_owner_grace_seconds: float,
    ) -> bool:
        owner_path = lock_dir / "owner.json"
        try:
            owner = self._read_json_object(owner_path, "archive lock")
            pid = owner.get("pid")
            reclaimable = (
                isinstance(pid, int)
                and not isinstance(pid, bool)
                and pid > 0
                and not _process_is_alive(pid)
            )
        except ExternalEventArchiveError:
            try:
                age = max(0.0, time.time() - lock_dir.stat().st_mtime)
            except OSError:
                return False
            reclaimable = age >= incomplete_owner_grace_seconds
        if not reclaimable:
            return False
        abandoned = lock_dir.with_name(
            f".{lock_dir.name}.{os.getpid()}.{time.monotonic_ns()}.abandoned"
        )
        try:
            lock_dir.rename(abandoned)
        except (FileNotFoundError, FileExistsError):
            return True
        except OSError:
            return False
        try:
            for child in abandoned.iterdir():
                if child.is_file() and not child.is_symlink():
                    child.unlink()
                else:
                    raise ExternalEventArchiveError(
                        "abandoned archive lock has unexpected contents"
                    )
            abandoned.rmdir()
        except OSError as exc:
            raise ExternalEventArchiveError(
                "abandoned archive lock cleanup failed"
            ) from exc
        return True

    def _revision_path(self, source: str, fact_id: str, revision_id: str) -> Path:
        return self.records / _safe_component(source) / _safe_component(fact_id) / f"{_safe_component(revision_id)}.json"

    def _lease_is_stale(self, lease_dir: Path, stale_after: timedelta) -> bool:
        path = lease_dir / "lease.json"
        try:
            value = self._read_json_object(path, "classification lease")
            acquired = parse_utc_iso(str(value["acquired_at"]))
        except (ExternalEventArchiveError, KeyError, ValueError):
            return False
        return ensure_timezone_aware_utc(self._now()) - acquired > stale_after

    def _read_json_object(self, path: Path, label: str) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExternalEventArchiveError(f"{label} could not be read") from exc
        if not isinstance(value, dict):
            raise ExternalEventArchiveError(f"{label} has invalid shape")
        return value

    def _write_json_once(self, path: Path, payload: Mapping[str, Any]) -> None:
        self._write_bytes_once(path, _json_bytes(payload))

    @staticmethod
    def _write_bytes_once(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != payload:
                raise ExternalEventArchiveError("immutable archive target contains different content")
            return
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != payload:
                    raise ExternalEventArchiveError("immutable archive publication conflicted")
        except ExternalEventArchiveError:
            raise
        except OSError as exc:
            raise ExternalEventArchiveError("immutable archive write failed") from exc
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _publish_first_writer(path: Path, payload: bytes) -> bool:
        """Atomically publish once and report whether this process won.

        Unlike a normal immutable write, a losing canonical claimant must read
        and reuse the winner instead of treating different output as an
        overwrite attempt.  Hydration detects the contradictory attempt later.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
        )
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
                return True
            except FileExistsError:
                return False
        except OSError as exc:
            raise ExternalEventArchiveError(
                "canonical classification claim publication failed"
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _atomic_replace(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise ExternalEventArchiveError("atomic manifest update failed") from exc
        finally:
            temporary.unlink(missing_ok=True)


def classification_input_fingerprint(payload: Mapping[str, Any], profile: Mapping[str, Any]) -> str:
    """Hash only canonical semantic request data and its exact pinned profile."""
    forbidden = {
        "classification_request_id",
        "classification_attempt_id",
        "requested_at",
        "classified_at",
        "validated_at",
        "provider_latency_ms",
        "output",
    }
    if forbidden.intersection(payload):
        raise ExternalEventArchiveError("classification input payload contains non-semantic identity fields")
    return _hash_payload({"semantic_request": dict(payload), "profile": dict(profile)})


def validate_external_event_lineage_chronology(
    *,
    system_observed_at: datetime | str,
    archived_at: datetime | str,
    normalized_at: datetime | str,
    classified_at: datetime | str | None,
    validated_at: datetime | str | None,
    attempt_published_at: datetime | str | None,
    canonical_published_at: datetime | str | None,
    readiness_published_at: datetime | str | None,
    evidence_ready_at: datetime | str | None,
) -> None:
    """Validate independent source and canonical-result chronology.

    A later observation may reuse a canonical result classified earlier.  The
    source-revision and canonical-classification chains are therefore checked
    independently; only a per-revision readiness receipt must dominate both.
    """

    def chronology_time(value: datetime | str, label: str) -> datetime:
        if isinstance(value, datetime):
            return ensure_timezone_aware_utc(value)
        return _required_archive_publication_time(value, label)

    observed = chronology_time(system_observed_at, "system_observed_at")
    archived = chronology_time(archived_at, "archived_at")
    normalized = chronology_time(normalized_at, "normalized_at")
    if not observed <= archived <= normalized:
        raise ExternalEventArchiveError(
            "external source revision chronology is non-monotonic"
        )

    classification_values = (
        classified_at,
        validated_at,
        attempt_published_at,
        canonical_published_at,
    )
    if any(value is not None for value in classification_values) and any(
        value is None for value in classification_values
    ):
        raise ExternalEventArchiveError(
            "external canonical classification chronology is incomplete"
        )
    required_artifacts = [observed, archived, normalized]
    if all(value is not None for value in classification_values):
        classified = chronology_time(classified_at, "classified_at")
        validated = chronology_time(validated_at, "validated_at")
        attempt_published = chronology_time(
            attempt_published_at, "attempt.archive_published_at"
        )
        canonical_published = chronology_time(
            canonical_published_at, "canonical.durably_published_at"
        )
        if not classified <= validated <= attempt_published <= canonical_published:
            raise ExternalEventArchiveError(
                "external canonical classification chronology is non-monotonic"
            )
        required_artifacts.extend(
            [classified, validated, attempt_published, canonical_published]
        )

    if readiness_published_at is not None:
        readiness_published = chronology_time(
            readiness_published_at, "readiness.archive_published_at"
        )
        required_artifacts.append(readiness_published)
    elif evidence_ready_at is not None:
        raise ExternalEventArchiveError(
            "evidence readiness lacks its durable readiness receipt"
        )

    if evidence_ready_at is not None:
        evidence_ready = chronology_time(evidence_ready_at, "evidence_ready_at")
        if evidence_ready < max(required_artifacts):
            raise ExternalEventArchiveError(
                "external evidence readiness predates a durable artifact"
            )


def output_fingerprints(
    normalized_output: Mapping[str, Any],
    *,
    policy_output: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    complete = _hash_payload(dict(normalized_output))
    policy_source = normalized_output if policy_output is None else policy_output
    policy_names = (
        "status",
        "event_type",
        "risk_level",
        "urgency",
        "confidence",
        "affected_tickers",
        "affected_sectors",
        "global_relevance",
        "valid_from",
        "valid_until",
    )
    policy = _hash_payload({name: policy_source.get(name) for name in policy_names})
    return complete, policy


def source_revision_id(
    *,
    source: str,
    source_fact_id: str,
    canonical_content_hash: str,
    lifecycle_state: LifecycleState,
    adapter_version: str,
) -> str:
    _sha256_value(canonical_content_hash, "canonical_content_hash")
    return "revision_" + _hash_payload(
        {
            "source": source,
            "source_fact_id": source_fact_id,
            "canonical_content_hash": canonical_content_hash,
            "lifecycle_state": lifecycle_state.value,
            "adapter_version": adapter_version,
        }
    )


def _revision_order_key(value: ExternalSourceRevision) -> tuple[datetime, int]:
    return (ensure_timezone_aware_utc(value.system_observed_at), value.revision_sequence)


def _hash_payload(value: Mapping[str, Any]) -> str:
    return sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExternalEventArchiveError(f"{name} must be a non-empty string")
    return value.strip()


def _sha256_value(value: object, name: str) -> str:
    text = _required_string(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ExternalEventArchiveError(f"{name} must be a lowercase SHA-256 value")
    return text


def _required_archive_publication_time(value: object, label: str) -> datetime:
    text = _required_string(value, f"{label} timestamp")
    try:
        return ensure_timezone_aware_utc(parse_utc_iso(text))
    except (TypeError, ValueError) as exc:
        raise ExternalEventArchiveError(
            f"{label} timestamp is invalid"
        ) from exc


def _non_negative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExternalEventArchiveError(
            f"{name} must be a non-negative integer"
        )
    return value


def _symbols(values: Iterable[str]) -> tuple[str, ...]:
    result = tuple(sorted({_required_string(value, "scope").upper() for value in values}))
    return result


def _safe_component(value: str) -> str:
    text = _required_string(value, "path component")
    safe = "".join(character if character.isalnum() or character in "._-" else "_" for character in text)
    if safe in {"", ".", ".."}:
        raise ExternalEventArchiveError("archive path component is unsafe")
    return safe[:180]


def _safe_extension(value: str) -> str:
    extension = _required_string(value, "extension").lower().lstrip(".")
    return extension if extension in {"json", "html", "htm", "xml", "txt", "pdf", "bin"} else "bin"


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else to_utc_iso(value)


def _parse_optional_iso(value: object) -> datetime | None:
    return (
        None
        if value is None
        else _required_archive_publication_time(value, "coverage")
    )


def _mapping_list(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ExternalEventArchiveError("coverage interval list has invalid shape")
    return value


def _interval_union_covers(intervals: Iterable[CoverageInterval], start: datetime, end: datetime) -> bool:
    ordered = sorted(intervals, key=lambda value: (value.start, value.end))
    cursor = start
    for interval in ordered:
        if interval.end < cursor:
            continue
        if interval.start > cursor:
            return False
        if interval.end >= end:
            return True
        cursor = interval.end
    return False


def _intervals_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start <= b_end and b_start <= a_end


def _same_revision_semantic_identity(
    first: ExternalSourceRevision,
    second: ExternalSourceRevision,
) -> bool:
    fields = (
        "source",
        "source_fact_id",
        "source_revision_id",
        "revision_sequence",
        "supersedes_revision_id",
        "lifecycle_state",
        "raw_object_hash",
        "document_hash",
        "normalized_text_hash",
        "canonical_content_hash",
        "source_type",
        "source_platform",
        "source_uri",
        "source_title",
        "affected_tickers",
        "affected_sectors",
        "global_relevance",
        "correlation_group_id",
        "relationship_types",
        "earnings_package_id",
        "adapter_version",
        "extractor_version",
        "normalizer_version",
        "collection_mode",
    )
    return all(getattr(first, name) == getattr(second, name) for name in fields)


def _process_is_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


__all__ = [
    "ConflictResolutionDecision",
    "CoverageInterval",
    "CoverageStatus",
    "ExternalEventArchive",
    "ExternalEventArchiveError",
    "ExternalSourceRevision",
    "LifecycleState",
    "SourceCoverage",
    "classification_input_fingerprint",
    "output_fingerprints",
    "source_revision_id",
]
