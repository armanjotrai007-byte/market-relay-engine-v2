"""Research-only event hydration and leak-free in-memory selection."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from market_relay_engine.common.serialization import to_json_dict, to_json_string
from market_relay_engine.common.time import ensure_timezone_aware_utc, to_utc_iso
from market_relay_engine.context.decision_context import (
    KNOWN_SOURCE_CLASSIFICATION,
    DecisionContext,
)
from market_relay_engine.context.sec_edgar import (
    Form4ReportingOwner,
    Form4ResearchEvent,
)
from market_relay_engine.context.sec_edgar_archive import SECEDGARArchive
from market_relay_engine.context.external_event_archive import (
    ConflictResolutionDecision,
    ExternalEventArchive,
    ExternalEventArchiveError,
    output_fingerprints,
    validate_external_event_lineage_chronology,
)
from market_relay_engine.contracts.context import (
    ContextAIEvent,
    ContextClassificationEventType,
    ContextFlag,
    ContextLifecycleState,
    ContextRiskLevel,
    ContextUrgency,
    DeterministicContextEventType,
)


SEC_EVENT_SOURCE = "sec_edgar"
_KNOWN_EXTERNAL_EVENT_SOURCES = frozenset(
    {
        "veritawire_truth_social",
        "lockheed_martin_rss",
        "palantir_ir",
        "company_earnings",
    }
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_POLICY_KEY_RE = re.compile(
    r"(?:AI_EVENT_TYPE|DETERMINISTIC_EVENT_TYPE|FLAG_TYPE):[A-Z0-9_]+"
)
_STRUCTURED_SOURCE_NAMES = frozenset(KNOWN_SOURCE_CLASSIFICATION)
_CONTEXT_AI_EVENT_EXTERNAL_FIELDS = frozenset(
    {
        "affected_sectors",
        "global_relevance",
        "source_available_at",
        "system_observed_at",
        "archived_at",
        "evidence_ready_at",
        "source_fact_id",
        "source_revision_id",
        "revision_sequence",
        "supersedes_revision_id",
        "lifecycle_state",
        "lifecycle_effective_at",
        "classification_input_fingerprint",
        "complete_output_fingerprint",
        "policy_output_fingerprint",
        "canonical_classification_attempt_id",
        "correlation_group_id",
        "related_event_ids",
        "relationship_types",
        "classification_conflict_id",
        "conflict_resolution_id",
        "conflict_resolution_generation",
    }
)


class ResearchProjectionError(ValueError):
    """Raised when research evidence cannot be hydrated safely."""


class ResearchEvidenceCapacityError(ResearchProjectionError):
    """Raised before publication when a bounded index would overflow."""

    def __init__(
        self,
        *,
        attempted_record_count: int,
        capacity: int,
        ticker_universe: tuple[str, ...],
        hydration_start_time: datetime,
        hydration_end_time: datetime,
    ) -> None:
        self.attempted_record_count = attempted_record_count
        self.capacity = capacity
        self.ticker_universe = ticker_universe
        self.hydration_start_time = hydration_start_time
        self.hydration_end_time = hydration_end_time
        super().__init__(
            "research evidence capacity exceeded: "
            f"attempted_record_count={attempted_record_count} "
            f"capacity={capacity} universe={list(ticker_universe)} "
            f"window={to_utc_iso(hydration_start_time)}..{to_utc_iso(hydration_end_time)}"
        )


class EvidenceCategory(str, Enum):
    AI_EVENT = "AI_EVENT"
    DETERMINISTIC_EVENT = "DETERMINISTIC_EVENT"
    FLAG = "FLAG"


class EvidenceExclusionReason(str, Enum):
    FUTURE = "FUTURE"
    EXPIRED = "EXPIRED"
    OUTSIDE_LOOKBACK = "OUTSIDE_LOOKBACK"
    MISSING_AVAILABILITY = "MISSING_AVAILABILITY"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    CLASSIFICATION_PROFILE_MISMATCH = "CLASSIFICATION_PROFILE_MISMATCH"
    MALFORMED = "MALFORMED"
    POLICY_INELIGIBLE = "POLICY_INELIGIBLE"
    MISSING_EVIDENCE_READY_AT = "MISSING_EVIDENCE_READY_AT"
    MISSING_SOURCE_AVAILABILITY = "MISSING_SOURCE_AVAILABILITY"
    SUPERSEDED_BY_LIFECYCLE_REVISION = "SUPERSEDED_BY_LIFECYCLE_REVISION"
    LIFECYCLE_REVISION_PENDING = "LIFECYCLE_REVISION_PENDING"
    LIFECYCLE_DELETED_OR_RETRACTED = "LIFECYCLE_DELETED_OR_RETRACTED"
    LIFECYCLE_ORDER_CONFLICT = "LIFECYCLE_ORDER_CONFLICT"
    EXACT_DUPLICATE_COLLAPSED = "EXACT_DUPLICATE_COLLAPSED"
    CLASSIFICATION_CONFLICT = "CLASSIFICATION_CONFLICT"
    INCOMPLETE_COVERAGE = "INCOMPLETE_COVERAGE"


class ResearchAvailabilityMode(str, Enum):
    """The one leak model used by an explicit external-source research run."""

    LIVE_SYSTEM_READY = "LIVE_SYSTEM_READY"
    HISTORICAL_SOURCE_TIME = "HISTORICAL_SOURCE_TIME"


@dataclass(frozen=True, kw_only=True)
class ResearchSourceClassificationProfile:
    """Exact source-specific and semantic profile for external classifications."""

    source: str
    source_type: str
    semantic_adapter_version: str
    extraction_version: str
    normalization_version: str
    excerpt_version: str
    scope_version: str
    prompt_version: str
    model_version: str
    response_schema_version: str
    validator_version: str
    classification_config_hash: str
    ticker: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "source",
            "source_type",
            "semantic_adapter_version",
            "extraction_version",
            "normalization_version",
            "excerpt_version",
            "scope_version",
            "prompt_version",
            "model_version",
            "response_schema_version",
            "validator_version",
        ):
            object.__setattr__(self, name, _required_string(getattr(self, name), name))
        object.__setattr__(
            self,
            "classification_config_hash",
            _required_sha256(self.classification_config_hash, "classification_config_hash"),
        )
        if self.ticker is not None:
            object.__setattr__(self, "ticker", _normalize_symbol(self.ticker, "ticker"))

    def to_fingerprint_payload(self) -> dict[str, str]:
        payload = {
            "source": self.source,
            "source_type": self.source_type,
            "semantic_adapter_version": self.semantic_adapter_version,
            "extraction_version": self.extraction_version,
            "normalization_version": self.normalization_version,
            "excerpt_version": self.excerpt_version,
            "scope_version": self.scope_version,
            "prompt_version": self.prompt_version,
            "model_version": self.model_version,
            "response_schema_version": self.response_schema_version,
            "validator_version": self.validator_version,
            "classification_config_hash": self.classification_config_hash,
        }
        if self.ticker is not None:
            payload["ticker"] = self.ticker
        return payload

    @property
    def revision_profile_key(self) -> tuple[str, str, str | None, str, str, str]:
        """Return the one semantic extractor owner for a source revision."""

        return (
            self.source,
            self.source_type,
            self.ticker,
            self.semantic_adapter_version,
            self.extraction_version,
            self.normalization_version,
        )

    @property
    def coverage_owner_key(self) -> tuple[str, str | None, str]:
        return (self.source, self.ticker, self.semantic_adapter_version)

    @property
    def profile_hash(self) -> str:
        return _sha256_payload(self.to_fingerprint_payload())


@dataclass(frozen=True, kw_only=True)
class ResearchSourceCoverageProfile:
    source: str
    coverage_generation: int
    coverage_version: str
    semantic_adapter_version: str | None = None
    ticker: str | None = None
    coverage_manifest_source: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _required_string(self.source, "source"))
        object.__setattr__(self, "coverage_version", _required_string(self.coverage_version, "coverage_version"))
        if isinstance(self.coverage_generation, bool) or not isinstance(self.coverage_generation, int) or self.coverage_generation < 0:
            raise ResearchProjectionError("coverage_generation must be non-negative")
        if self.semantic_adapter_version is not None:
            object.__setattr__(
                self,
                "semantic_adapter_version",
                _required_string(
                    self.semantic_adapter_version,
                    "semantic_adapter_version",
                ),
            )
        if self.ticker is not None:
            object.__setattr__(self, "ticker", _normalize_symbol(self.ticker, "ticker"))
        if self.coverage_manifest_source is not None:
            object.__setattr__(
                self,
                "coverage_manifest_source",
                _required_string(
                    self.coverage_manifest_source,
                    "coverage_manifest_source",
                ),
            )

    def to_fingerprint_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source": self.source,
            "coverage_generation": self.coverage_generation,
            "coverage_version": self.coverage_version,
        }
        if self.semantic_adapter_version is not None:
            payload["semantic_adapter_version"] = self.semantic_adapter_version
        if self.ticker is not None:
            payload["ticker"] = self.ticker
        if self.coverage_manifest_source is not None:
            payload["coverage_manifest_source"] = self.coverage_manifest_source
        return payload

    @property
    def owner_key(self) -> tuple[str, str | None, str | None]:
        return (self.source, self.ticker, self.semantic_adapter_version)

    @property
    def manifest_source(self) -> str:
        return self.coverage_manifest_source or self.source


@dataclass(frozen=True, kw_only=True)
class ResearchClassificationProfile:
    """Exact SEC 8-K classification profile pinned for one research run."""

    extraction_version: str
    prompt_version: str
    model_version: str
    response_schema_version: str
    classification_config_hash: str

    def __post_init__(self) -> None:
        for name in (
            "extraction_version",
            "prompt_version",
            "model_version",
            "response_schema_version",
        ):
            object.__setattr__(self, name, _required_string(getattr(self, name), name))
        object.__setattr__(
            self,
            "classification_config_hash",
            _required_sha256(
                self.classification_config_hash,
                "classification_config_hash",
            ),
        )

    def to_fingerprint_payload(self) -> dict[str, str]:
        return {
            "extraction_version": self.extraction_version,
            "prompt_version": self.prompt_version,
            "model_version": self.model_version,
            "response_schema_version": self.response_schema_version,
            "classification_config_hash": self.classification_config_hash,
        }


@dataclass(frozen=True, kw_only=True)
class ResearchRunDefinition:
    """Explicit bounded universe and selection semantics for one research run."""

    ticker_universe: tuple[str, ...]
    event_sources: tuple[str, ...]
    evidence_categories: tuple[EvidenceCategory, ...]
    hydration_start_time: datetime
    hydration_end_time: datetime
    capacity: int
    classification_profile: ResearchClassificationProfile
    max_age_without_valid_until: timedelta
    selection_policy_version: str
    availability_mode: ResearchAvailabilityMode | None = None
    external_classification_profiles: tuple[ResearchSourceClassificationProfile, ...] = field(default_factory=tuple)
    source_coverage_profiles: tuple[ResearchSourceCoverageProfile, ...] = field(default_factory=tuple)
    allow_incomplete_coverage: bool = False
    conflict_resolution_generation: int | None = None
    conflict_resolution_manifest_hash: str | None = None
    lifecycle_version: str | None = None
    correlation_version: str | None = None
    external_archive_generation: int | None = None
    external_archive_manifest_hash: str | None = None

    def __post_init__(self) -> None:
        tickers = tuple(
            sorted({_normalize_symbol(value, "ticker_universe") for value in self.ticker_universe})
        )
        if not tickers:
            raise ResearchProjectionError("ticker_universe must not be empty")
        object.__setattr__(self, "ticker_universe", tickers)
        sources = tuple(sorted({_required_string(value, "event_sources") for value in self.event_sources}))
        if not sources:
            raise ResearchProjectionError("event_sources must not be empty")
        structured = set(sources).intersection(_STRUCTURED_SOURCE_NAMES)
        if structured:
            raise ResearchProjectionError(
                "structured-owned sources cannot enter the event index: "
                + ", ".join(sorted(structured))
            )
        object.__setattr__(self, "event_sources", sources)
        categories = tuple(self.evidence_categories)
        if not categories or not all(
            isinstance(value, EvidenceCategory) for value in categories
        ):
            raise ResearchProjectionError(
                "evidence_categories must contain EvidenceCategory values"
            )
        if len(set(categories)) != len(categories):
            raise ResearchProjectionError("evidence_categories must be unique")
        object.__setattr__(
            self,
            "evidence_categories",
            tuple(sorted(categories, key=lambda value: value.value)),
        )
        start = _aware_datetime(self.hydration_start_time, "hydration_start_time")
        end = _aware_datetime(self.hydration_end_time, "hydration_end_time")
        if end < start:
            raise ResearchProjectionError(
                "hydration_end_time must not precede hydration_start_time"
            )
        object.__setattr__(self, "hydration_start_time", start)
        object.__setattr__(self, "hydration_end_time", end)
        if isinstance(self.capacity, bool) or not isinstance(self.capacity, int) or self.capacity <= 0:
            raise ResearchProjectionError("capacity must be a positive integer")
        if not isinstance(self.classification_profile, ResearchClassificationProfile):
            raise ResearchProjectionError(
                "classification_profile must be a ResearchClassificationProfile"
            )
        age = self.max_age_without_valid_until
        if not isinstance(age, timedelta) or age <= timedelta(0):
            raise ResearchProjectionError(
                "max_age_without_valid_until must be a positive timedelta"
            )
        if not math.isfinite(age.total_seconds()):
            raise ResearchProjectionError(
                "max_age_without_valid_until must be finite"
            )
        object.__setattr__(
            self,
            "selection_policy_version",
            _required_string(
                self.selection_policy_version,
                "selection_policy_version",
            ),
        )
        if self.availability_mode is not None and not isinstance(
            self.availability_mode, ResearchAvailabilityMode
        ):
            raise ResearchProjectionError(
                "availability_mode must be a ResearchAvailabilityMode or None"
            )
        external_profiles = tuple(self.external_classification_profiles)
        if not all(
            isinstance(value, ResearchSourceClassificationProfile)
            for value in external_profiles
        ):
            raise ResearchProjectionError(
                "external_classification_profiles has an invalid value"
            )
        profile_keys = [value.revision_profile_key for value in external_profiles]
        if len(set(profile_keys)) != len(profile_keys):
            raise ResearchProjectionError(
                "external classification profiles must have unique revision ownership"
            )
        for source in {value.source for value in external_profiles}:
            source_profiles = [
                value for value in external_profiles if value.source == source
            ]
            if len(source_profiles) > 1 and any(
                value.ticker is None for value in source_profiles
            ):
                raise ResearchProjectionError(
                    "multiple source profiles require explicit ticker ownership"
                )
        object.__setattr__(
            self,
            "external_classification_profiles",
            tuple(
                sorted(
                    external_profiles,
                    key=lambda value: (
                        value.source,
                        value.source_type,
                        value.ticker or "",
                        value.semantic_adapter_version,
                        value.extraction_version,
                        value.normalization_version,
                    ),
                )
            ),
        )
        coverage_profiles = tuple(self.source_coverage_profiles)
        if not all(
            isinstance(value, ResearchSourceCoverageProfile)
            for value in coverage_profiles
        ):
            raise ResearchProjectionError("source_coverage_profiles has an invalid value")
        coverage_keys = [value.owner_key for value in coverage_profiles]
        if len(set(coverage_keys)) != len(coverage_profiles):
            raise ResearchProjectionError(
                "source coverage profiles must have unique source/ticker/adapter ownership"
            )
        if len({value.manifest_source for value in coverage_profiles}) != len(
            coverage_profiles
        ):
            raise ResearchProjectionError(
                "coverage owners must pin distinct archive coverage manifests"
            )
        object.__setattr__(
            self,
            "source_coverage_profiles",
            tuple(
                sorted(
                    coverage_profiles,
                    key=lambda value: (
                        value.source,
                        value.ticker or "",
                        value.semantic_adapter_version or "",
                        value.manifest_source,
                    ),
                )
            ),
        )
        if not isinstance(self.allow_incomplete_coverage, bool):
            raise ResearchProjectionError("allow_incomplete_coverage must be bool")
        if external_profiles and self.availability_mode is None:
            raise ResearchProjectionError(
                "external classification profiles require an explicit availability mode"
            )
        if (
            set(sources).intersection(_KNOWN_EXTERNAL_EVENT_SOURCES)
            and self.availability_mode is None
        ):
            raise ResearchProjectionError(
                "external event sources require an explicit availability mode"
            )
        if external_profiles:
            expected_coverage_owners = {
                value.coverage_owner_key for value in external_profiles
            }
            actual_coverage_owners = {value.owner_key for value in coverage_profiles}
            if expected_coverage_owners != actual_coverage_owners:
                raise ResearchProjectionError(
                    "external coverage ownership must exactly match source/ticker/adapter profiles"
                )
        if external_profiles and (
            self.conflict_resolution_generation is None
            or self.conflict_resolution_manifest_hash is None
        ):
            raise ResearchProjectionError(
                "external research requires a pinned conflict-resolution manifest"
            )
        if external_profiles and (
            self.external_archive_generation is None
            or self.external_archive_manifest_hash is None
        ):
            raise ResearchProjectionError(
                "external research requires a pinned external archive manifest"
            )
        if (
            self.availability_mode is ResearchAvailabilityMode.HISTORICAL_SOURCE_TIME
            and self.allow_incomplete_coverage
        ):
            raise ResearchProjectionError(
                "historical source-time runs cannot allow incomplete coverage"
            )
        if self.conflict_resolution_generation is not None:
            if (
                isinstance(self.conflict_resolution_generation, bool)
                or not isinstance(self.conflict_resolution_generation, int)
                or self.conflict_resolution_generation < 0
            ):
                raise ResearchProjectionError(
                    "conflict_resolution_generation must be non-negative"
                )
        if self.conflict_resolution_manifest_hash is not None:
            object.__setattr__(
                self,
                "conflict_resolution_manifest_hash",
                _required_sha256(
                    self.conflict_resolution_manifest_hash,
                    "conflict_resolution_manifest_hash",
                ),
            )
        for name in ("lifecycle_version", "correlation_version"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required_string(value, name))
        if self.external_archive_generation is not None:
            if (
                isinstance(self.external_archive_generation, bool)
                or not isinstance(self.external_archive_generation, int)
                or self.external_archive_generation < 0
            ):
                raise ResearchProjectionError(
                    "external_archive_generation must be non-negative"
                )
            if self.external_archive_manifest_hash is None:
                raise ResearchProjectionError(
                    "external archive generation requires its manifest hash"
                )
        if self.external_archive_manifest_hash is not None:
            object.__setattr__(
                self,
                "external_archive_manifest_hash",
                _required_sha256(
                    self.external_archive_manifest_hash,
                    "external_archive_manifest_hash",
                ),
            )

    def to_fingerprint_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ticker_universe": list(self.ticker_universe),
            "event_sources": list(self.event_sources),
            "evidence_categories": [value.value for value in self.evidence_categories],
            "hydration_start_time": to_utc_iso(self.hydration_start_time),
            "hydration_end_time": to_utc_iso(self.hydration_end_time),
            "classification_profile": self.classification_profile.to_fingerprint_payload(),
            "max_age_without_valid_until_seconds": self.max_age_without_valid_until.total_seconds(),
            "selection_policy_version": self.selection_policy_version,
        }
        # New fields are conditional so unchanged PR37 SEC-only runs retain their
        # exact historical fingerprint payload.
        if self.availability_mode is not None:
            payload["availability_mode"] = self.availability_mode.value
        if self.external_classification_profiles:
            payload["external_classification_profiles"] = [
                value.to_fingerprint_payload()
                for value in self.external_classification_profiles
            ]
        if self.source_coverage_profiles:
            payload["source_coverage_profiles"] = [
                value.to_fingerprint_payload() for value in self.source_coverage_profiles
            ]
            payload["allow_incomplete_coverage"] = self.allow_incomplete_coverage
        if self.conflict_resolution_generation is not None:
            payload["conflict_resolution_generation"] = self.conflict_resolution_generation
            payload["conflict_resolution_manifest_hash"] = self.conflict_resolution_manifest_hash
        if self.lifecycle_version is not None:
            payload["lifecycle_version"] = self.lifecycle_version
        if self.correlation_version is not None:
            payload["correlation_version"] = self.correlation_version
        if self.external_archive_generation is not None:
            payload["external_archive_generation"] = self.external_archive_generation
            payload["external_archive_manifest_hash"] = self.external_archive_manifest_hash
        return payload


@dataclass(frozen=True, kw_only=True)
class ResearchEvidence:
    """Internal normalized selection view; not a public source contract."""

    evidence_id: str
    category: EvidenceCategory
    policy_match_key: str
    source: str
    source_record_id: str
    tickers: tuple[str, ...] = field(default_factory=tuple)
    sector: str | None = None
    sectors: tuple[str, ...] = field(default_factory=tuple)
    global_relevance: bool = False
    available_at: datetime | None = None
    source_available_at: datetime | None = None
    system_observed_at: datetime | None = None
    evidence_ready_at: datetime | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    fingerprint_payload: Mapping[str, object] = field(default_factory=dict)
    lineage_ids: tuple[str, ...] = field(default_factory=tuple)
    lineage_visibility: Mapping[str, str] = field(default_factory=dict)
    policy_eligible: bool = True
    source_fact_id: str | None = None
    source_revision_id: str | None = None
    revision_sequence: int | None = None
    supersedes_revision_id: str | None = None
    lifecycle_state: str | None = None
    lifecycle_effective_at: datetime | None = None
    classification_input_fingerprint: str | None = None
    canonical_classification_owner_fingerprint: str | None = None
    exact_duplicate_fingerprint: str | None = None
    complete_output_fingerprint: str | None = None
    policy_output_fingerprint: str | None = None
    classification_conflict_id: str | None = None
    conflict_resolution_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evidence_id",
            _required_string(self.evidence_id, "evidence_id"),
        )
        if not isinstance(self.category, EvidenceCategory):
            raise ResearchProjectionError("category must be an EvidenceCategory")
        key = _required_string(self.policy_match_key, "policy_match_key")
        if _POLICY_KEY_RE.fullmatch(key) is None:
            raise ResearchProjectionError("policy_match_key has an unsupported taxonomy")
        expected_prefix = {
            EvidenceCategory.AI_EVENT: "AI_EVENT_TYPE:",
            EvidenceCategory.DETERMINISTIC_EVENT: "DETERMINISTIC_EVENT_TYPE:",
            EvidenceCategory.FLAG: "FLAG_TYPE:",
        }[self.category]
        if not key.startswith(expected_prefix):
            raise ResearchProjectionError(
                "policy_match_key prefix must match evidence category"
            )
        object.__setattr__(self, "policy_match_key", key)
        object.__setattr__(self, "source", _required_string(self.source, "source"))
        object.__setattr__(
            self,
            "source_record_id",
            _required_string(self.source_record_id, "source_record_id"),
        )
        tickers = tuple(
            sorted({_normalize_symbol(value, "tickers") for value in self.tickers})
        )
        sector = (
            None if self.sector is None else _normalize_symbol(self.sector, "sector")
        )
        sectors = tuple(
            sorted({_normalize_symbol(value, "sectors") for value in self.sectors})
        )
        if not isinstance(self.global_relevance, bool):
            raise ResearchProjectionError("global_relevance must be bool")
        if not self.global_relevance and not tickers and sector is None and not sectors:
            raise ResearchProjectionError(
                "evidence must declare ticker, sector, or global relevance"
            )
        object.__setattr__(self, "tickers", tickers)
        object.__setattr__(self, "sector", sector)
        object.__setattr__(self, "sectors", sectors)
        for name in (
            "available_at",
            "source_available_at",
            "system_observed_at",
            "evidence_ready_at",
            "lifecycle_effective_at",
            "valid_from",
            "valid_until",
        ):
            value = getattr(self, name)
            object.__setattr__(
                self,
                name,
                None if value is None else _aware_datetime(value, name),
            )
        if self.valid_from is not None and self.valid_until is not None:
            if self.valid_until < self.valid_from:
                raise ResearchProjectionError(
                    "valid_until must not precede valid_from"
                )
        if not isinstance(self.fingerprint_payload, Mapping):
            raise ResearchProjectionError("fingerprint_payload must be a mapping")
        payload = _json_mapping_copy(self.fingerprint_payload, "fingerprint_payload")
        object.__setattr__(self, "fingerprint_payload", _deep_freeze_json(payload))
        lineage = tuple(_required_string(value, "lineage_ids") for value in self.lineage_ids)
        if len(set(lineage)) != len(lineage):
            raise ResearchProjectionError("lineage_ids must not contain duplicates")
        object.__setattr__(self, "lineage_ids", lineage)
        if not isinstance(self.lineage_visibility, Mapping):
            raise ResearchProjectionError("lineage_visibility must be a mapping")
        visibility: dict[str, str] = {}
        for lineage_id, raw_time in self.lineage_visibility.items():
            identity = _required_string(lineage_id, "lineage_visibility key")
            timestamp = _aware_datetime(
                datetime.fromisoformat(str(raw_time).replace("Z", "+00:00")),
                "lineage_visibility value",
            )
            visibility[identity] = to_utc_iso(timestamp)
        unknown_visibility = set(visibility).difference(lineage)
        if unknown_visibility:
            raise ResearchProjectionError(
                "lineage_visibility contains an identity absent from lineage_ids"
            )
        object.__setattr__(self, "lineage_visibility", MappingProxyType(dict(sorted(visibility.items()))))
        if not isinstance(self.policy_eligible, bool):
            raise ResearchProjectionError("policy_eligible must be bool")
        for name in (
            "source_fact_id",
            "source_revision_id",
            "supersedes_revision_id",
            "lifecycle_state",
            "classification_conflict_id",
            "conflict_resolution_id",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required_string(value, name))
        if self.revision_sequence is not None:
            if isinstance(self.revision_sequence, bool) or not isinstance(self.revision_sequence, int) or self.revision_sequence < 1:
                raise ResearchProjectionError("revision_sequence must be positive")
        for name in (
            "classification_input_fingerprint",
            "canonical_classification_owner_fingerprint",
            "exact_duplicate_fingerprint",
            "complete_output_fingerprint",
            "policy_output_fingerprint",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required_sha256(value, name))
        if self.source_revision_id is not None and self.source_fact_id is None:
            raise ResearchProjectionError("source_revision_id requires source_fact_id")

    def to_fingerprint_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "evidence_id": self.evidence_id,
            "category": self.category.value,
            "policy_match_key": self.policy_match_key,
            "source": self.source,
            "source_record_id": self.source_record_id,
            "tickers": list(self.tickers),
            "sector": self.sector,
            "global_relevance": self.global_relevance,
            "available_at": None if self.available_at is None else to_utc_iso(self.available_at),
            "valid_from": None if self.valid_from is None else to_utc_iso(self.valid_from),
            "valid_until": None if self.valid_until is None else to_utc_iso(self.valid_until),
            "fingerprint_payload": _deep_thaw_json(self.fingerprint_payload),
            "lineage_ids": list(self.lineage_ids),
            "policy_eligible": self.policy_eligible,
        }
        if self.sectors:
            payload["sectors"] = list(self.sectors)
        for name in (
            "source_available_at",
            "system_observed_at",
            "evidence_ready_at",
            "lifecycle_effective_at",
        ):
            value = getattr(self, name)
            if value is not None:
                payload[name] = to_utc_iso(value)
        if self.lineage_visibility:
            payload["lineage_visibility"] = dict(self.lineage_visibility)
        for name in (
            "source_fact_id",
            "source_revision_id",
            "revision_sequence",
            "supersedes_revision_id",
            "lifecycle_state",
            "classification_input_fingerprint",
            "canonical_classification_owner_fingerprint",
            "exact_duplicate_fingerprint",
            "complete_output_fingerprint",
            "policy_output_fingerprint",
            "classification_conflict_id",
            "conflict_resolution_id",
        ):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload

    def effective_available_at(
        self, mode: ResearchAvailabilityMode | None
    ) -> datetime | None:
        if mode is ResearchAvailabilityMode.LIVE_SYSTEM_READY:
            return self.evidence_ready_at
        if mode is ResearchAvailabilityMode.HISTORICAL_SOURCE_TIME:
            return self.source_available_at
        return self.available_at

    @property
    def effective_sectors(self) -> tuple[str, ...]:
        values = set(self.sectors)
        if self.sector is not None:
            values.add(self.sector)
        return tuple(sorted(values))


@dataclass(frozen=True, kw_only=True)
class ResearchEvidenceExclusion:
    evidence_id: str
    reason: EvidenceExclusionReason
    source: str
    tickers: tuple[str, ...] = field(default_factory=tuple)
    sector: str | None = None
    sectors: tuple[str, ...] = field(default_factory=tuple)
    global_relevance: bool = False
    available_at: datetime | None = None
    source_available_at: datetime | None = None
    evidence_ready_at: datetime | None = None
    source_fact_id: str | None = None
    source_revision_id: str | None = None
    safe_detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", _required_string(self.evidence_id, "evidence_id"))
        if not isinstance(self.reason, EvidenceExclusionReason):
            raise ResearchProjectionError("reason must be an EvidenceExclusionReason")
        object.__setattr__(self, "source", _required_string(self.source, "source"))
        object.__setattr__(
            self,
            "tickers",
            tuple(sorted({_normalize_symbol(value, "tickers") for value in self.tickers})),
        )
        object.__setattr__(
            self,
            "sector",
            None if self.sector is None else _normalize_symbol(self.sector, "sector"),
        )
        object.__setattr__(
            self,
            "sectors",
            tuple(sorted({_normalize_symbol(value, "sectors") for value in self.sectors})),
        )
        if not isinstance(self.global_relevance, bool):
            raise ResearchProjectionError("global_relevance must be bool")
        for name in ("available_at", "source_available_at", "evidence_ready_at"):
            value = getattr(self, name)
            object.__setattr__(
                self,
                name,
                None if value is None else _aware_datetime(value, name),
            )
        for name in ("source_fact_id", "source_revision_id", "safe_detail"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required_string(value, name))

    @property
    def effective_sectors(self) -> tuple[str, ...]:
        values = set(self.sectors)
        if self.sector is not None:
            values.add(self.sector)
        return tuple(sorted(values))


@dataclass(frozen=True, kw_only=True)
class ResearchLifecycleRevision:
    """Source-fact lifecycle head metadata, including revisions still pending AI."""

    source: str
    source_fact_id: str
    source_revision_id: str
    revision_sequence: int
    supersedes_revision_id: str | None
    lifecycle_state: str
    lifecycle_effective_at: datetime
    system_observed_at: datetime
    evidence_ready_at: datetime | None

    def __post_init__(self) -> None:
        for name in ("source", "source_fact_id", "source_revision_id", "lifecycle_state"):
            object.__setattr__(self, name, _required_string(getattr(self, name), name))
        if self.supersedes_revision_id is not None:
            object.__setattr__(
                self,
                "supersedes_revision_id",
                _required_string(self.supersedes_revision_id, "supersedes_revision_id"),
            )
        if (
            isinstance(self.revision_sequence, bool)
            or not isinstance(self.revision_sequence, int)
            or self.revision_sequence < 1
        ):
            raise ResearchProjectionError("revision_sequence must be positive")
        if self.lifecycle_state not in {"ACTIVE", "UPDATED", "DELETED", "RETRACTED"}:
            raise ResearchProjectionError("lifecycle_state is unsupported")
        for name in ("lifecycle_effective_at", "system_observed_at"):
            object.__setattr__(self, name, _aware_datetime(getattr(self, name), name))
        if self.evidence_ready_at is not None:
            object.__setattr__(
                self,
                "evidence_ready_at",
                _aware_datetime(self.evidence_ready_at, "evidence_ready_at"),
            )

    def effective_at(self, mode: ResearchAvailabilityMode | None) -> datetime:
        if mode is ResearchAvailabilityMode.LIVE_SYSTEM_READY:
            return self.system_observed_at
        return self.lifecycle_effective_at

    def to_fingerprint_payload(self) -> dict[str, object]:
        return {
            "source": self.source,
            "source_fact_id": self.source_fact_id,
            "source_revision_id": self.source_revision_id,
            "revision_sequence": self.revision_sequence,
            "supersedes_revision_id": self.supersedes_revision_id,
            "lifecycle_state": self.lifecycle_state,
            "lifecycle_effective_at": to_utc_iso(self.lifecycle_effective_at),
            "system_observed_at": to_utc_iso(self.system_observed_at),
            "evidence_ready_at": (
                None if self.evidence_ready_at is None else to_utc_iso(self.evidence_ready_at)
            ),
        }


@dataclass(frozen=True, kw_only=True)
class ResearchEvidenceRelationship:
    correlation_group_id: str
    left_evidence_id: str
    right_evidence_id: str
    relationship_type: str
    correlation_version: str
    live_ready_at: datetime
    historical_ready_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in (
            "correlation_group_id",
            "left_evidence_id",
            "right_evidence_id",
            "relationship_type",
            "correlation_version",
        ):
            object.__setattr__(self, name, _required_string(getattr(self, name), name))
        if self.left_evidence_id == self.right_evidence_id:
            raise ResearchProjectionError("relationship members must differ")
        object.__setattr__(self, "live_ready_at", _aware_datetime(self.live_ready_at, "live_ready_at"))
        if self.historical_ready_at is not None:
            object.__setattr__(self, "historical_ready_at", _aware_datetime(self.historical_ready_at, "historical_ready_at"))

    def effective_at(self, mode: ResearchAvailabilityMode | None) -> datetime | None:
        if mode is ResearchAvailabilityMode.HISTORICAL_SOURCE_TIME:
            return self.historical_ready_at
        return self.live_ready_at

    def to_fingerprint_payload(self) -> dict[str, object]:
        return {
            "correlation_group_id": self.correlation_group_id,
            "left_evidence_id": self.left_evidence_id,
            "right_evidence_id": self.right_evidence_id,
            "relationship_type": self.relationship_type,
            "correlation_version": self.correlation_version,
            "live_ready_at": to_utc_iso(self.live_ready_at),
            "historical_ready_at": None if self.historical_ready_at is None else to_utc_iso(self.historical_ready_at),
        }


@dataclass(frozen=True, kw_only=True)
class ResearchCoverageAssessment:
    source: str
    coverage_generation: int
    status: str
    complete: bool
    known_gaps: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    ticker: str | None = None
    semantic_adapter_version: str | None = None
    coverage_manifest_source: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _required_string(self.source, "source"))
        object.__setattr__(self, "status", _required_string(self.status, "status"))
        if isinstance(self.coverage_generation, bool) or not isinstance(self.coverage_generation, int) or self.coverage_generation < 0:
            raise ResearchProjectionError("coverage_generation must be non-negative")
        if not isinstance(self.complete, bool):
            raise ResearchProjectionError("coverage complete must be bool")
        if self.ticker is not None:
            object.__setattr__(self, "ticker", _normalize_symbol(self.ticker, "ticker"))
        for name in ("semantic_adapter_version", "coverage_manifest_source"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required_string(value, name))

    def to_fingerprint_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source": self.source,
            "coverage_generation": self.coverage_generation,
            "status": self.status,
            "complete": self.complete,
            "known_gaps": [list(value) for value in self.known_gaps],
        }
        if self.ticker is not None:
            payload["ticker"] = self.ticker
        if self.semantic_adapter_version is not None:
            payload["semantic_adapter_version"] = self.semantic_adapter_version
        if self.coverage_manifest_source is not None:
            payload["coverage_manifest_source"] = self.coverage_manifest_source
        return payload


@dataclass(frozen=True, kw_only=True)
class ResearchEvidenceSelection:
    decision_time: datetime
    ticker: str
    sector: str | None
    selected_evidence: tuple[ResearchEvidence, ...]
    exclusions: tuple[ResearchEvidenceExclusion, ...]
    run_definition: ResearchRunDefinition
    visible_relationships: tuple[ResearchEvidenceRelationship, ...] = field(default_factory=tuple)
    coverage_assessments: tuple[ResearchCoverageAssessment, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_time", _aware_datetime(self.decision_time, "decision_time"))
        object.__setattr__(self, "ticker", _normalize_symbol(self.ticker, "ticker"))
        object.__setattr__(
            self,
            "sector",
            None if self.sector is None else _normalize_symbol(self.sector, "sector"),
        )
        object.__setattr__(self, "selected_evidence", tuple(self.selected_evidence))
        object.__setattr__(self, "exclusions", tuple(self.exclusions))
        object.__setattr__(self, "visible_relationships", tuple(self.visible_relationships))
        object.__setattr__(self, "coverage_assessments", tuple(self.coverage_assessments))
        if not isinstance(self.run_definition, ResearchRunDefinition):
            raise ResearchProjectionError("run_definition has an invalid type")


@dataclass(frozen=True, kw_only=True)
class ResearchEvidenceIndex:
    """Immutable bounded event index hydrated before any signal evaluation."""

    run_definition: ResearchRunDefinition
    evidence: tuple[ResearchEvidence, ...]
    hydration_exclusions: tuple[ResearchEvidenceExclusion, ...]
    attempted_record_count: int
    lifecycle_revisions: tuple[ResearchLifecycleRevision, ...] = field(default_factory=tuple)
    relationships: tuple[ResearchEvidenceRelationship, ...] = field(default_factory=tuple)
    coverage_assessments: tuple[ResearchCoverageAssessment, ...] = field(default_factory=tuple)

    @classmethod
    def build(
        cls,
        *,
        run_definition: ResearchRunDefinition,
        evidence: Iterable[ResearchEvidence],
        hydration_exclusions: Iterable[ResearchEvidenceExclusion] = (),
        attempted_record_count: int | None = None,
        lifecycle_revisions: Iterable[ResearchLifecycleRevision] = (),
        relationships: Iterable[ResearchEvidenceRelationship] = (),
        coverage_assessments: Iterable[ResearchCoverageAssessment] = (),
    ) -> "ResearchEvidenceIndex":
        if not isinstance(run_definition, ResearchRunDefinition):
            raise ResearchProjectionError("run_definition has an invalid type")
        values = tuple(evidence)
        exclusions = tuple(hydration_exclusions)
        lifecycle_values = tuple(lifecycle_revisions)
        relationship_values = tuple(relationships)
        coverage_values = tuple(coverage_assessments)
        if not all(isinstance(value, ResearchEvidence) for value in values):
            raise ResearchProjectionError("evidence must contain ResearchEvidence values")
        if not all(isinstance(value, ResearchEvidenceExclusion) for value in exclusions):
            raise ResearchProjectionError(
                "hydration_exclusions must contain ResearchEvidenceExclusion values"
            )
        if not all(isinstance(value, ResearchLifecycleRevision) for value in lifecycle_values):
            raise ResearchProjectionError("lifecycle_revisions contain an invalid value")
        if not all(isinstance(value, ResearchEvidenceRelationship) for value in relationship_values):
            raise ResearchProjectionError("relationships contain an invalid value")
        if not all(isinstance(value, ResearchCoverageAssessment) for value in coverage_values):
            raise ResearchProjectionError("coverage_assessments contain an invalid value")
        if run_definition.availability_mode is None and any(
            value.source_available_at is not None
            or value.system_observed_at is not None
            or value.evidence_ready_at is not None
            or value.source_fact_id is not None
            or value.classification_input_fingerprint is not None
            for value in values
        ):
            raise ResearchProjectionError(
                "external evidence requires an explicit availability mode"
            )
        attempted = len(values) + len(exclusions) if attempted_record_count is None else attempted_record_count
        stored_record_count = len(values) + len(exclusions)
        if (
            isinstance(attempted, bool)
            or not isinstance(attempted, int)
            or attempted < stored_record_count
        ):
            raise ResearchProjectionError("attempted_record_count is invalid")
        if attempted > run_definition.capacity:
            raise ResearchEvidenceCapacityError(
                attempted_record_count=attempted,
                capacity=run_definition.capacity,
                ticker_universe=run_definition.ticker_universe,
                hydration_start_time=run_definition.hydration_start_time,
                hydration_end_time=run_definition.hydration_end_time,
            )
        seen: dict[str, str] = {}
        seen_source_records: dict[tuple[str, str], str] = {}
        for value in values:
            if value.source not in run_definition.event_sources:
                raise ResearchProjectionError(
                    f"event source is not admitted by this research run: {value.source}"
                )
            if value.source in _STRUCTURED_SOURCE_NAMES:
                raise ResearchProjectionError(
                    "structured-owned evidence cannot enter the research event index"
                )
            if value.category not in run_definition.evidence_categories:
                raise ResearchProjectionError(
                    f"evidence category is not admitted: {value.category.value}"
                )
            outside_universe = set(value.tickers).difference(
                run_definition.ticker_universe
            )
            if outside_universe:
                raise ResearchProjectionError(
                    "event evidence is outside the hydrated ticker universe: "
                    + ", ".join(sorted(outside_universe))
                )
            canonical = to_json_string(value.to_fingerprint_payload())
            previous = seen.get(value.evidence_id)
            if previous is not None:
                raise ResearchProjectionError(
                    f"duplicate evidence identity is ambiguous: {value.evidence_id}"
                )
            seen[value.evidence_id] = canonical
            source_record_key = (
                value.source,
                value.source_revision_id or value.source_record_id,
            )
            prior_evidence_id = seen_source_records.get(source_record_key)
            if prior_evidence_id is not None:
                raise ResearchProjectionError(
                    "multiple event records claim the same source fact: "
                    f"{value.source}:{value.source_record_id} "
                    f"({prior_evidence_id}, {value.evidence_id})"
                )
            seen_source_records[source_record_key] = value.evidence_id
        ordered = tuple(sorted(values, key=_evidence_sort_key))
        ordered_exclusions = tuple(sorted(exclusions, key=_exclusion_sort_key))
        evidence_ids = {value.evidence_id for value in ordered}
        lifecycle_ids: set[tuple[str, str, str]] = set()
        for lifecycle in lifecycle_values:
            if lifecycle.source not in run_definition.event_sources:
                raise ResearchProjectionError("lifecycle source is not admitted")
            identity = (
                lifecycle.source,
                lifecycle.source_fact_id,
                lifecycle.source_revision_id,
            )
            if identity in lifecycle_ids:
                raise ResearchProjectionError("duplicate lifecycle revision identity")
            lifecycle_ids.add(identity)
        for relationship in relationship_values:
            if relationship.left_evidence_id not in evidence_ids or relationship.right_evidence_id not in evidence_ids:
                raise ResearchProjectionError("relationship references unknown evidence")
        return cls(
            run_definition=run_definition,
            evidence=ordered,
            hydration_exclusions=ordered_exclusions,
            attempted_record_count=attempted,
            lifecycle_revisions=tuple(
                sorted(
                    lifecycle_values,
                    key=lambda value: (
                        value.source,
                        value.source_fact_id,
                        value.system_observed_at,
                        value.revision_sequence,
                        value.source_revision_id,
                    ),
                )
            ),
            relationships=tuple(sorted(relationship_values, key=lambda value: (value.correlation_group_id, value.left_evidence_id, value.right_evidence_id))),
            coverage_assessments=tuple(
                sorted(
                    coverage_values,
                    key=lambda value: (
                        value.source,
                        value.ticker or "",
                        value.semantic_adapter_version or "",
                        value.coverage_manifest_source or "",
                    ),
                )
            ),
        )

    def select(self, decision_context: DecisionContext) -> ResearchEvidenceSelection:
        """Select applicable evidence entirely from immutable in-memory state."""
        if not isinstance(decision_context, DecisionContext):
            raise ResearchProjectionError("decision_context must be a DecisionContext")
        decision_time = decision_context.evaluation_time
        ticker = decision_context.ticker
        if ticker not in self.run_definition.ticker_universe:
            raise ResearchProjectionError(
                "decision ticker is outside the hydrated ticker universe"
            )
        structured_sources = {
            value.source for value in decision_context.all_structured_context
        }
        ambiguous_sources = structured_sources.intersection(
            value.source for value in self.evidence
        )
        if ambiguous_sources:
            raise ResearchProjectionError(
                "evidence ownership is ambiguous between structured and event paths: "
                + ", ".join(sorted(ambiguous_sources))
            )
        coverage_start = (
            self.run_definition.hydration_start_time
            + self.run_definition.max_age_without_valid_until
        )
        if not coverage_start <= decision_time <= self.run_definition.hydration_end_time:
            raise ResearchProjectionError(
                "decision time is outside complete hydrated evidence coverage"
            )
        selected: list[ResearchEvidence] = []
        exclusions = [
            value
            for value in self.hydration_exclusions
            if _hydration_exclusion_matches_decision(
                value,
                ticker=ticker,
                sector=decision_context.ticker_sector,
                decision_time=decision_time,
                max_age=self.run_definition.max_age_without_valid_until,
            )
        ]
        lifecycle_candidates, lifecycle_exclusions = _resolve_lifecycle_as_of(
            self.evidence,
            lifecycle_revisions=self.lifecycle_revisions,
            decision_time=decision_time,
            mode=self.run_definition.availability_mode,
        )
        exclusions.extend(lifecycle_exclusions)
        for value in lifecycle_candidates:
            reason = _selection_exclusion_reason(
                value,
                ticker=ticker,
                sector=decision_context.ticker_sector,
                decision_time=decision_time,
                max_age=self.run_definition.max_age_without_valid_until,
                availability_mode=self.run_definition.availability_mode,
            )
            if reason is None:
                selected.append(value)
            else:
                exclusions.append(
                    ResearchEvidenceExclusion(
                        evidence_id=value.evidence_id,
                        reason=reason,
                        source=value.source,
                        tickers=value.tickers,
                        sector=value.sector,
                        sectors=value.sectors,
                        global_relevance=value.global_relevance,
                        available_at=value.effective_available_at(
                            self.run_definition.availability_mode
                        ),
                        source_available_at=value.source_available_at,
                        evidence_ready_at=value.evidence_ready_at,
                        source_fact_id=value.source_fact_id,
                        source_revision_id=value.source_revision_id,
                    )
                )
        visible_before_duplicates = tuple(selected)
        selected, duplicate_exclusions = _collapse_exact_duplicates_as_of(
            selected,
            decision_time=decision_time,
            mode=self.run_definition.availability_mode,
        )
        exclusions.extend(duplicate_exclusions)
        eligible_ids = {value.evidence_id for value in visible_before_duplicates}
        visible_relationships = tuple(
            value
            for value in self.relationships
            if value.left_evidence_id in eligible_ids
            and value.right_evidence_id in eligible_ids
            and value.effective_at(self.run_definition.availability_mode) is not None
            and value.effective_at(self.run_definition.availability_mode) <= decision_time  # type: ignore[operator]
        )
        return ResearchEvidenceSelection(
            decision_time=decision_time,
            ticker=ticker,
            sector=decision_context.ticker_sector,
            selected_evidence=tuple(sorted(selected, key=_evidence_sort_key)),
            exclusions=tuple(sorted(exclusions, key=_exclusion_sort_key)),
            run_definition=self.run_definition,
            visible_relationships=visible_relationships,
            coverage_assessments=self.coverage_assessments,
        )


def hydrate_sec_research_evidence(
    *,
    archive: SECEDGARArchive,
    run_definition: ResearchRunDefinition,
) -> ResearchEvidenceIndex:
    """Read PR36 durable SEC outputs once and publish an atomic in-memory index."""
    if not isinstance(archive, SECEDGARArchive):
        raise ResearchProjectionError("archive must be a SECEDGARArchive")
    if SEC_EVENT_SOURCE not in run_definition.event_sources:
        raise ResearchProjectionError(
            "SEC hydration requires sec_edgar in event_sources"
        )
    evidence: list[ResearchEvidence] = []
    exclusions: list[ResearchEvidenceExclusion] = []
    attempted = 0
    section_claims: dict[str, str] = {}
    manifest = archive.load_manifest()
    filings = manifest.get("filings", {})
    if not isinstance(filings, Mapping):
        raise ResearchProjectionError("SEC manifest filings have an invalid shape")

    if EvidenceCategory.AI_EVENT in run_definition.evidence_categories:
        for accession, raw_state in sorted(filings.items(), key=lambda item: str(item[0])):
            if not isinstance(raw_state, Mapping):
                attempted += 1
                exclusions.append(_malformed_exclusion(f"sec_filing_{accession}"))
                continue
            try:
                filing_state = _merge_sec_filing_metadata(
                    archive=archive,
                    accession=str(accession),
                    manifest_state=raw_state,
                )
            except ResearchProjectionError:
                raw_classifications = raw_state.get("classifications", {})
                count = (
                    len(raw_classifications)
                    if isinstance(raw_classifications, Mapping)
                    else 1
                )
                attempted += count
                exclusions.extend(
                    _malformed_exclusion(f"sec_filing_{accession}_{index}")
                    for index in range(count)
                )
                continue
            ticker = _optional_symbol(filing_state.get("ticker"))
            classifications = raw_state.get("classifications", {})
            if ticker is None:
                count = len(classifications) if isinstance(classifications, Mapping) else 1
                attempted += count
                exclusions.extend(
                    _malformed_exclusion(f"sec_filing_{accession}_{index}")
                    for index in range(count)
                )
                continue
            if ticker not in run_definition.ticker_universe:
                continue
            if not _archived_filing_in_window(filing_state, run_definition):
                continue
            filing_available_at = _optional_datetime(
                filing_state.get("acceptance_at"),
                "acceptance_at",
            )
            if not isinstance(classifications, Mapping):
                attempted += 1
                exclusions.append(
                    _malformed_exclusion(
                        f"sec_filing_{accession}",
                        ticker=ticker,
                        available_at=filing_available_at,
                    )
                )
                continue
            for classification_key, raw_saved in sorted(
                classifications.items(), key=lambda item: str(item[0])
            ):
                attempted += 1
                fallback_id = f"classification_{classification_key}"
                if not isinstance(raw_saved, Mapping):
                    exclusions.append(
                        _malformed_exclusion(
                            fallback_id,
                            ticker=ticker,
                            available_at=filing_available_at,
                        )
                    )
                    continue
                evidence_id = _classification_evidence_id(raw_saved, fallback_id)
                if not _classification_matches_profile(
                    raw_saved,
                    run_definition.classification_profile,
                ):
                    exclusions.append(
                        ResearchEvidenceExclusion(
                            evidence_id=evidence_id,
                            reason=EvidenceExclusionReason.CLASSIFICATION_PROFILE_MISMATCH,
                            source=SEC_EVENT_SOURCE,
                            tickers=(ticker,),
                            available_at=filing_available_at,
                        )
                    )
                    continue
                try:
                    section_identity = _saved_8k_section_identity(
                        accession=str(accession),
                        state=filing_state,
                        saved=raw_saved,
                    )
                except (ResearchProjectionError, TypeError, ValueError, KeyError):
                    exclusions.append(
                        _malformed_exclusion(
                            evidence_id,
                            ticker=ticker,
                            available_at=filing_available_at,
                        )
                    )
                    continue
                prior = section_claims.get(section_identity)
                if prior is not None:
                    raise ResearchProjectionError(
                        "multiple classifications under the pinned profile "
                        f"claim source section {section_identity}: {prior}, {evidence_id}"
                    )
                section_claims[section_identity] = evidence_id
                if not _classification_is_semantically_valid(raw_saved):
                    exclusions.append(
                        ResearchEvidenceExclusion(
                            evidence_id=evidence_id,
                            reason=EvidenceExclusionReason.POLICY_INELIGIBLE,
                            source=SEC_EVENT_SOURCE,
                            tickers=(ticker,),
                            available_at=filing_available_at,
                        )
                    )
                    continue
                if filing_available_at is None:
                    exclusions.append(
                        ResearchEvidenceExclusion(
                            evidence_id=evidence_id,
                            reason=EvidenceExclusionReason.MISSING_AVAILABILITY,
                            source=SEC_EVENT_SOURCE,
                            tickers=(ticker,),
                        )
                    )
                    continue
                try:
                    normalized = _normalize_saved_8k_classification(
                        accession=str(accession),
                        state=filing_state,
                        saved=raw_saved,
                        run_definition=run_definition,
                        section_identity=section_identity,
                        available_at=filing_available_at,
                    )
                except (ResearchProjectionError, TypeError, ValueError, KeyError):
                    exclusions.append(
                        _malformed_exclusion(
                            evidence_id,
                            ticker=ticker,
                            available_at=filing_available_at,
                        )
                    )
                    continue
                evidence.append(normalized)

    if EvidenceCategory.DETERMINISTIC_EVENT in run_definition.evidence_categories:
        for path in _form4_paths(archive):
            try:
                payload = _read_json_mapping(path)
            except ResearchProjectionError:
                attempted += 1
                exclusions.append(_malformed_exclusion(f"form4_file_{path.stem}"))
                continue
            filing = payload.get("filing")
            if not isinstance(filing, Mapping):
                attempted += 1
                exclusions.append(_malformed_exclusion(f"form4_file_{path.stem}"))
                continue
            values = payload.get("research_events", [])
            ticker = _optional_symbol(payload.get("issuer_ticker"))
            if ticker is None:
                count = len(values) if isinstance(values, list) else 1
                attempted += count
                exclusions.extend(
                    _malformed_exclusion(f"form4_{path.stem}_{index}")
                    for index in range(count)
                )
                continue
            if ticker not in run_definition.ticker_universe:
                continue
            if not _archived_filing_in_window(filing, run_definition):
                continue
            filing_available_at = _optional_datetime(
                filing.get("acceptance_at"),
                "acceptance_at",
            )
            if not isinstance(values, list):
                attempted += 1
                exclusions.append(
                    _malformed_exclusion(
                        f"form4_file_{path.stem}",
                        ticker=ticker,
                        available_at=filing_available_at,
                    )
                )
                continue
            for ordinal, raw_event in enumerate(values):
                attempted += 1
                fallback_id = f"form4_{path.stem}_{ordinal}"
                if not isinstance(raw_event, Mapping):
                    exclusions.append(
                        _malformed_exclusion(
                            fallback_id,
                            ticker=ticker,
                            available_at=filing_available_at,
                        )
                    )
                    continue
                if filing_available_at is None:
                    exclusions.append(
                        ResearchEvidenceExclusion(
                            evidence_id=fallback_id,
                            reason=EvidenceExclusionReason.MISSING_AVAILABILITY,
                            source=SEC_EVENT_SOURCE,
                            tickers=(ticker,),
                        )
                    )
                    continue
                try:
                    evidence.append(
                        _normalize_archived_form4_event(
                            event=raw_event,
                            filing=filing,
                            ordinal=ordinal,
                            archive_accession=path.stem,
                            payload_ticker=ticker,
                            payload_issuer_cik=_required_string(
                                payload.get("issuer_cik"),
                                "issuer_cik",
                            ),
                            payload_is_amendment=_required_bool(
                                payload.get("is_amendment"),
                                "is_amendment",
                            ),
                            payload_amends_accession=_optional_string(
                                payload.get("amends_accession"),
                                "amends_accession",
                            ),
                        )
                    )
                except (ResearchProjectionError, TypeError, ValueError, KeyError):
                    exclusions.append(
                        _malformed_exclusion(
                            fallback_id,
                            ticker=ticker,
                            available_at=filing_available_at,
                        )
                    )

    return ResearchEvidenceIndex.build(
        run_definition=run_definition,
        evidence=evidence,
        hydration_exclusions=exclusions,
        attempted_record_count=attempted,
    )


def hydrate_external_research_evidence(
    *,
    archive: ExternalEventArchive,
    run_definition: ResearchRunDefinition,
) -> ResearchEvidenceIndex:
    """Hydrate exact external profiles once; signal-time selection stays memory-only."""
    if not isinstance(archive, ExternalEventArchive):
        raise ResearchProjectionError("archive must be an ExternalEventArchive")
    profiles = tuple(run_definition.external_classification_profiles)
    if not profiles:
        raise ResearchProjectionError("external hydration requires source profiles")
    if run_definition.availability_mode is None:
        raise ResearchProjectionError(
            "external hydration requires an explicit availability mode"
        )
    external_sources = {value.source for value in profiles}
    admitted_external_sources = set(run_definition.event_sources).difference(
        {SEC_EVENT_SOURCE}
    )
    if external_sources != admitted_external_sources:
        raise ResearchProjectionError(
            "every admitted external source must have an exact classification profile"
        )
    _verify_external_archive_pin(archive, run_definition)
    coverage_assessments = _verify_external_coverage(
        archive=archive,
        run_definition=run_definition,
        profiles=profiles,
    )
    evidence: list[ResearchEvidence] = []
    exclusions: list[ResearchEvidenceExclusion] = []
    lifecycle_revisions: list[ResearchLifecycleRevision] = []
    correlated: list[tuple[ResearchEvidence, str, tuple[str, ...]]] = []
    attempted = 0
    for revision in archive.iter_revisions(sources=external_sources):
        lifecycle_time = (
            revision.system_observed_at
            if run_definition.availability_mode
            is ResearchAvailabilityMode.LIVE_SYSTEM_READY
            else revision.lifecycle_effective_at
        )
        if not (
            run_definition.hydration_start_time
            <= lifecycle_time
            <= run_definition.hydration_end_time
        ):
            continue
        profile = _profile_for_external_revision(
            revision=revision,
            profiles=profiles,
        )
        attempted += 1
        revision_resolution = _validate_revision_classification_conflicts(
            archive=archive,
            source_revision_id=revision.source_revision_id,
            selected_profile=profile,
            run_definition=run_definition,
        )
        selected_readiness = _select_external_readiness(
            archive=archive,
            source_revision_id=revision.source_revision_id,
            profile=profile,
            run_definition=run_definition,
        )
        ready_at = (
            None
            if selected_readiness is None
            else _required_datetime(
                selected_readiness.get("evidence_ready_at"),
                "evidence_ready_at",
            )
        )
        lifecycle_revisions.append(
            ResearchLifecycleRevision(
                source=revision.source,
                source_fact_id=revision.source_fact_id,
                source_revision_id=revision.source_revision_id,
                revision_sequence=revision.revision_sequence,
                supersedes_revision_id=revision.supersedes_revision_id,
                lifecycle_state=revision.lifecycle_state.value,
                lifecycle_effective_at=revision.lifecycle_effective_at,
                system_observed_at=revision.system_observed_at,
                evidence_ready_at=ready_at,
            )
        )
        if selected_readiness is None:
            continue
        status = str(selected_readiness.get("classification_status", ""))
        policy_eligible = selected_readiness.get("policy_eligible") is True
        input_fingerprint = _required_sha256(
            selected_readiness.get("classification_input_fingerprint"),
            "classification_input_fingerprint",
        )
        resolution = _resolved_classification_conflict(
            archive=archive,
            classification_input_fingerprint=input_fingerprint,
            run_definition=run_definition,
        )
        if resolution is None:
            resolution = revision_resolution
        if resolution is not None and resolution.get("decision") == (
            ConflictResolutionDecision.ABSTAIN_INPUT.value
        ):
            exclusions.append(
                _revision_exclusion(
                    revision,
                    EvidenceExclusionReason.CLASSIFICATION_CONFLICT,
                    available_at=ready_at,
                    safe_detail="reviewed conflict resolution abstains this input",
                )
            )
            continue
        if status != "VALID" or not policy_eligible:
            exclusions.append(
                _revision_exclusion(
                    revision,
                    EvidenceExclusionReason.POLICY_INELIGIBLE,
                    available_at=ready_at,
                    safe_detail=f"classification status={status or 'UNKNOWN'}",
                )
            )
            continue
        event_payload = archive.read_materialized_event(
            revision.source_revision_id,
            classification_input_fingerprint=input_fingerprint,
        )
        if event_payload is None:
            exclusions.append(
                _revision_exclusion(
                    revision,
                    EvidenceExclusionReason.MALFORMED,
                    available_at=ready_at,
                    safe_detail="policy-eligible readiness lacks materialized event",
                )
            )
            continue
        try:
            event = _context_ai_event_from_payload(event_payload)
            attempt = _canonical_external_attempt(
                archive=archive,
                classification_input_fingerprint=input_fingerprint,
                canonical_attempt_id=str(
                    selected_readiness.get(
                        "canonical_classification_attempt_id"
                    )
                ),
            )
            canonical_claim = archive.read_canonical_claim(input_fingerprint)
            if canonical_claim is None:
                raise ResearchProjectionError(
                    "external readiness lacks a canonical claim"
                )
            _validate_external_event_lineage(
                event=event,
                revision=revision,
                readiness=selected_readiness,
                profile=profile,
                attempt=attempt,
                canonical_claim=canonical_claim,
            )
        except (
            ExternalEventArchiveError,
            ResearchProjectionError,
            TypeError,
            ValueError,
            KeyError,
        ):
            exclusions.append(
                _revision_exclusion(
                    revision,
                    EvidenceExclusionReason.MALFORMED,
                    available_at=ready_at,
                    safe_detail="materialized event lineage validation failed",
                )
            )
            continue
        event = replace(
            event,
            available_at=ready_at,
            evidence_ready_at=ready_at,
            classification_conflict_id=(
                None
                if resolution is None
                else str(resolution.get("conflict_id"))
            ),
            conflict_resolution_id=(
                None
                if resolution is None
                else str(resolution.get("classification_resolution_id"))
            ),
            conflict_resolution_generation=(
                None
                if resolution is None
                else int(resolution.get("manifest_generation", 0))
            ),
        )
        normalized = normalize_context_ai_event(event)
        exact_duplicate_fingerprint = _external_exact_duplicate_fingerprint(
            attempt=attempt,
            profile=profile,
        )
        observation_visibility = archive.observation_lineage(
            revision.source_revision_id
        )
        lineage = list(normalized.lineage_ids)
        visibility = dict(normalized.lineage_visibility)
        for observation_id, observed_at in sorted(observation_visibility.items()):
            if observation_id not in lineage:
                lineage.append(observation_id)
            visibility[observation_id] = observed_at
        normalized = replace(
            normalized,
            lineage_ids=tuple(lineage),
            lineage_visibility=visibility,
            exact_duplicate_fingerprint=exact_duplicate_fingerprint,
            canonical_classification_owner_fingerprint=(
                _external_canonical_classification_owner(
                    attempt=attempt,
                    classification_input_fingerprint=input_fingerprint,
                    exact_duplicate_fingerprint=exact_duplicate_fingerprint,
                )
            ),
        )
        evidence.append(normalized)
        if revision.correlation_group_id is not None:
            correlated.append(
                (
                    normalized,
                    revision.correlation_group_id,
                    revision.relationship_types
                    or ("RELATED_EXTERNAL_EVENT",),
                )
            )
    relationships = _external_relationships(
        correlated,
        correlation_version=run_definition.correlation_version
        or "external_correlation_v1",
    )
    # Recheck after every immutable classification/readiness read.  The archive
    # manifest owns those publications, so a concurrent append aborts before an
    # index can be published under stale run pins.
    _verify_external_archive_pin(archive, run_definition)
    return ResearchEvidenceIndex.build(
        run_definition=run_definition,
        evidence=evidence,
        hydration_exclusions=exclusions,
        attempted_record_count=attempted,
        lifecycle_revisions=lifecycle_revisions,
        relationships=relationships,
        coverage_assessments=coverage_assessments,
    )


def hydrate_combined_research_evidence(
    *,
    run_definition: ResearchRunDefinition,
    sec_archive: SECEDGARArchive | None = None,
    external_archive: ExternalEventArchive | None = None,
) -> ResearchEvidenceIndex:
    """Atomically combine legacy SEC and external event evidence in one index."""
    indexes: list[ResearchEvidenceIndex] = []
    admitted_external_sources = set(run_definition.event_sources).difference(
        {SEC_EVENT_SOURCE}
    )
    profile_sources = {
        value.source for value in run_definition.external_classification_profiles
    }
    if admitted_external_sources != profile_sources:
        raise ResearchProjectionError(
            "every admitted external source must have an exact classification profile"
        )
    if SEC_EVENT_SOURCE in run_definition.event_sources:
        if sec_archive is None:
            raise ResearchProjectionError("combined preparation requires SEC archive")
        indexes.append(
            hydrate_sec_research_evidence(
                archive=sec_archive,
                run_definition=run_definition,
            )
        )
    if run_definition.external_classification_profiles:
        if external_archive is None:
            raise ResearchProjectionError("combined preparation requires external archive")
        indexes.append(
            hydrate_external_research_evidence(
                archive=external_archive,
                run_definition=run_definition,
            )
        )
    if not indexes:
        raise ResearchProjectionError("combined preparation has no admitted archive")
    combined_evidence = tuple(
        value for index in indexes for value in index.evidence
    )
    combined_relationships = tuple(
        value for index in indexes for value in index.relationships
    ) + _correlate_sec_company_earnings(
        combined_evidence,
        correlation_version=run_definition.correlation_version
        or "external_correlation_v1",
    ) + _correlate_official_company_observations(
        combined_evidence,
        correlation_version=run_definition.correlation_version
        or "external_correlation_v1",
    )
    return ResearchEvidenceIndex.build(
        run_definition=run_definition,
        evidence=combined_evidence,
        hydration_exclusions=(
            value for index in indexes for value in index.hydration_exclusions
        ),
        attempted_record_count=sum(index.attempted_record_count for index in indexes),
        lifecycle_revisions=(
            value for index in indexes for value in index.lifecycle_revisions
        ),
        relationships=combined_relationships,
        coverage_assessments=(
            value for index in indexes for value in index.coverage_assessments
        ),
    )


def normalize_context_ai_event(
    event: ContextAIEvent,
    *,
    policy_eligible: bool = True,
) -> ResearchEvidence:
    """Adapt a validated/materialized AI event; raw text is never accepted."""
    if not isinstance(event, ContextAIEvent):
        raise ResearchProjectionError("event must be a ContextAIEvent")
    if event.source in _STRUCTURED_SOURCE_NAMES:
        raise ResearchProjectionError(
            "structured-owned ContextAIEvent cannot enter event evidence"
        )
    payload = to_json_dict(event)
    if not isinstance(payload, dict):
        raise ResearchProjectionError("ContextAIEvent serialization is invalid")
    explicit_external_contract = any(
        (
            event.affected_sectors,
            event.global_relevance is not None,
            event.source_fact_id is not None,
            event.source_revision_id is not None,
            event.source_available_at is not None,
            event.system_observed_at is not None,
            event.evidence_ready_at is not None,
            event.classification_input_fingerprint is not None,
            event.correlation_group_id is not None,
            event.classification_conflict_id is not None,
        )
    )
    if not explicit_external_contract:
        # ContextAIEvent gained additive external-source fields after PR37.  Do
        # not let their empty defaults perturb historical SEC-only/legacy event
        # fingerprints.
        for name in _CONTEXT_AI_EVENT_EXTERNAL_FIELDS:
            payload.pop(name, None)
    sectors = set(event.affected_sectors)
    if event.affected_sector is not None:
        sectors.add(event.affected_sector)
    global_relevance = (
        event.global_relevance
        if event.global_relevance is not None
        else not event.affected_tickers and not sectors
    )
    lineage_ids = tuple(
        value
        for value in (
            event.raw_input_id,
            event.source_document_id,
            event.classification_request_id,
            event.classification_attempt_id,
            event.validation_result_id,
        )
        if value is not None
    )
    lineage_visibility = (
        {}
        if event.system_observed_at is None
        else {
            value: to_utc_iso(event.system_observed_at)
            for value in lineage_ids
        }
    )
    return ResearchEvidence(
        evidence_id=event.context_event_id,
        category=EvidenceCategory.AI_EVENT,
        policy_match_key=f"AI_EVENT_TYPE:{event.event_type.value}",
        source=event.source,
        source_record_id=event.source_id,
        tickers=tuple(event.affected_tickers),
        sector=event.affected_sector,
        sectors=tuple(sectors),
        global_relevance=global_relevance,
        available_at=event.available_at,
        source_available_at=event.source_available_at,
        system_observed_at=event.system_observed_at,
        evidence_ready_at=event.evidence_ready_at,
        valid_from=event.valid_from,
        valid_until=event.valid_until,
        fingerprint_payload=payload,
        lineage_ids=lineage_ids,
        lineage_visibility=lineage_visibility,
        policy_eligible=(
            policy_eligible
            and not (
                event.classification_conflict_id is not None
                and event.conflict_resolution_id is None
            )
        ),
        source_fact_id=event.source_fact_id,
        source_revision_id=event.source_revision_id,
        revision_sequence=event.revision_sequence,
        supersedes_revision_id=event.supersedes_revision_id,
        lifecycle_state=(
            None if event.lifecycle_state is None else event.lifecycle_state.value
        ),
        lifecycle_effective_at=event.lifecycle_effective_at,
        classification_input_fingerprint=event.classification_input_fingerprint,
        complete_output_fingerprint=event.complete_output_fingerprint,
        policy_output_fingerprint=event.policy_output_fingerprint,
        classification_conflict_id=event.classification_conflict_id,
        conflict_resolution_id=event.conflict_resolution_id,
    )


def normalize_context_flag(
    flag: ContextFlag,
    *,
    policy_eligible: bool = True,
) -> ResearchEvidence:
    """Adapt an explicitly event-owned validated flag."""
    if not isinstance(flag, ContextFlag):
        raise ResearchProjectionError("flag must be a ContextFlag")
    if flag.source in _STRUCTURED_SOURCE_NAMES:
        raise ResearchProjectionError(
            "structured-owned ContextFlag cannot enter event evidence"
        )
    payload = to_json_dict(flag)
    if not isinstance(payload, dict):
        raise ResearchProjectionError("ContextFlag serialization is invalid")
    return ResearchEvidence(
        evidence_id=flag.context_flag_id,
        category=EvidenceCategory.FLAG,
        policy_match_key=f"FLAG_TYPE:{flag.flag_type.upper()}",
        source=flag.source,
        source_record_id=flag.source_id or flag.context_flag_id,
        tickers=() if flag.ticker is None else (flag.ticker,),
        sector=flag.sector,
        global_relevance=flag.ticker is None and flag.sector is None,
        available_at=flag.available_at,
        valid_from=flag.valid_from,
        valid_until=flag.valid_until,
        fingerprint_payload=payload,
        lineage_ids=tuple(
            value
            for value in (
                flag.raw_input_id,
                flag.source_document_id,
                flag.classification_request_id,
                flag.classification_attempt_id,
                flag.validation_result_id,
                flag.context_event_id,
            )
            if value is not None
        ),
        policy_eligible=policy_eligible,
    )


def normalize_form4_research_event(
    event: Form4ResearchEvent,
    *,
    document_hash: str,
    source_uri: str,
    ordinal: int,
) -> ResearchEvidence:
    """Adapt PR36's existing deterministic Form 4 record without a new contract."""
    if not isinstance(event, Form4ResearchEvent):
        raise ResearchProjectionError("event must be a Form4ResearchEvent")
    document_hash = _required_sha256(document_hash, "document_hash")
    source_uri = _required_string(source_uri, "source_uri")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise ResearchProjectionError("ordinal must be a non-negative integer")
    payload = {
        "event_type": event.event_type.value,
        "issuer_ticker": event.issuer_ticker,
        "issuer_cik": event.issuer_cik,
        "accession_number": event.accession_number,
        "reporting_owners": [
            {
                "cik": owner.cik,
                "name": owner.name,
                "roles": list(owner.roles),
                "officer_title": owner.officer_title,
                "other_relationship_text": owner.other_relationship_text,
            }
            for owner in event.reporting_owners
        ],
        "transaction_date": (
            None if event.transaction_date is None else event.transaction_date.isoformat()
        ),
        "available_at": (
            None if event.available_at is None else to_utc_iso(event.available_at)
        ),
        "transaction_code": event.transaction_code,
        "shares": event.shares,
        "price_per_share": event.price_per_share,
        "approximate_value": event.approximate_value,
        "direct_or_indirect": event.direct_or_indirect,
        "shares_owned_following": event.shares_owned_following,
        "is_amendment": event.is_amendment,
        "amends_accession": event.amends_accession,
        "aggregate_eligibility": event.aggregate_eligibility,
        "plan_10b5_1": event.plan_10b5_1,
        "document_hash": document_hash,
        "source_uri": source_uri,
    }
    event_id = _stable_id(
        "context_event_sec_form4",
        {
            "accession_number": event.accession_number,
            "ordinal": ordinal,
            "event": payload,
        },
    )
    return ResearchEvidence(
        evidence_id=event_id,
        category=EvidenceCategory.DETERMINISTIC_EVENT,
        policy_match_key=f"DETERMINISTIC_EVENT_TYPE:{event.event_type.value}",
        source=SEC_EVENT_SOURCE,
        source_record_id=f"{event.accession_number}:{ordinal}",
        tickers=(event.issuer_ticker,),
        available_at=event.available_at,
        fingerprint_payload=payload,
        lineage_ids=tuple(
            dict.fromkeys(
                [
                    event.accession_number,
                    document_hash,
                    *(
                        owner.cik
                        for owner in event.reporting_owners
                        if owner.cik is not None
                    ),
                ]
            )
        ),
        policy_eligible=event.aggregate_eligibility == "ELIGIBLE",
    )


def build_shadow_context_fingerprint(
    *,
    decision_context: DecisionContext,
    evidence_selection: ResearchEvidenceSelection,
) -> str:
    """Fingerprint exact structured and selected event evidence without policy output."""
    if decision_context.evaluation_time != evidence_selection.decision_time:
        raise ResearchProjectionError(
            "DecisionContext and evidence selection times must match"
        )
    if decision_context.ticker != evidence_selection.ticker:
        raise ResearchProjectionError(
            "DecisionContext and evidence selection tickers must match"
        )
    payload = {
        "decision_context_fingerprint": decision_context.context_fingerprint,
        "selected_event_evidence": [
            value.to_fingerprint_payload()
            for value in evidence_selection.selected_evidence
        ],
        "research_run_selection": evidence_selection.run_definition.to_fingerprint_payload(),
    }
    if evidence_selection.visible_relationships:
        payload["visible_evidence_relationships"] = [
            value.to_fingerprint_payload()
            for value in evidence_selection.visible_relationships
        ]
    if evidence_selection.coverage_assessments:
        payload["research_coverage_assessments"] = [
            value.to_fingerprint_payload()
            for value in evidence_selection.coverage_assessments
        ]
    return _sha256_payload(payload)


def _verify_external_archive_pin(
    archive: ExternalEventArchive,
    run_definition: ResearchRunDefinition,
) -> None:
    try:
        archive.reconcile_classification_artifacts()
        archive.reconcile_conflict_resolutions()
        archive.reconcile_mutable_artifacts()
    except ExternalEventArchiveError as exc:
        raise ResearchProjectionError(
            "external artifact reconciliation failed"
        ) from exc
    manifest = archive.load_manifest()
    if int(manifest.get("generation", -1)) != run_definition.external_archive_generation:
        raise ResearchProjectionError("external archive generation does not match the run")
    if _sha256_payload(manifest) != run_definition.external_archive_manifest_hash:
        raise ResearchProjectionError("external archive manifest hash does not match the run")
    resolution_manifest = archive.load_resolution_manifest()
    if int(resolution_manifest.get("generation", -1)) != (
        run_definition.conflict_resolution_generation
    ):
        raise ResearchProjectionError(
            "classification-resolution generation does not match the run"
        )
    if _sha256_payload(resolution_manifest) != (
        run_definition.conflict_resolution_manifest_hash
    ):
        raise ResearchProjectionError(
            "classification-resolution manifest hash does not match the run"
        )


def _verify_external_coverage(
    *,
    archive: ExternalEventArchive,
    run_definition: ResearchRunDefinition,
    profiles: tuple[ResearchSourceClassificationProfile, ...],
) -> tuple[ResearchCoverageAssessment, ...]:
    pinned = {
        value.owner_key: value for value in run_definition.source_coverage_profiles
    }
    expected_owners = {value.coverage_owner_key for value in profiles}
    if set(pinned) != expected_owners:
        raise ResearchProjectionError(
            "external run must pin coverage for every source/ticker/adapter owner"
        )
    assessments: list[ResearchCoverageAssessment] = []
    for owner_key in sorted(
        expected_owners,
        key=lambda value: (value[0], value[1] or "", value[2]),
    ):
        profile = pinned[owner_key]
        coverage = archive.load_coverage(profile.manifest_source)
        if coverage is None:
            raise ResearchProjectionError(
                f"coverage manifest is missing for {profile.manifest_source}"
            )
        if coverage.source != profile.manifest_source:
            raise ResearchProjectionError("coverage manifest ownership changed")
        if (
            coverage.coverage_generation != profile.coverage_generation
            or coverage.coverage_version != profile.coverage_version
        ):
            raise ResearchProjectionError(
                f"coverage profile changed for {profile.manifest_source}"
            )
        complete = coverage.covers(
            run_definition.hydration_start_time,
            run_definition.hydration_end_time,
        )
        if not complete and not run_definition.allow_incomplete_coverage:
            raise ResearchProjectionError(
                "external source coverage is incomplete for "
                f"{profile.manifest_source}"
            )
        assessments.append(
            ResearchCoverageAssessment(
                source=profile.source,
                coverage_generation=coverage.coverage_generation,
                status=coverage.coverage_status.value,
                complete=complete,
                known_gaps=tuple(
                    (to_utc_iso(value.start), to_utc_iso(value.end))
                    for value in coverage.known_gaps
                ),
                ticker=profile.ticker,
                semantic_adapter_version=profile.semantic_adapter_version,
                coverage_manifest_source=profile.manifest_source,
            )
        )
    return tuple(assessments)


def _profile_for_external_revision(
    *,
    revision: object,
    profiles: tuple[ResearchSourceClassificationProfile, ...],
) -> ResearchSourceClassificationProfile:
    revision_tickers = tuple(getattr(revision, "affected_tickers"))
    candidates = [
        value
        for value in profiles
        if value.source == getattr(revision, "source")
        and value.source_type == getattr(revision, "source_type")
        and value.semantic_adapter_version == getattr(revision, "adapter_version")
        and value.extraction_version == getattr(revision, "extractor_version")
        and value.normalization_version == getattr(revision, "normalizer_version")
        and (
            value.ticker is None
            or revision_tickers == (value.ticker,)
        )
    ]
    if len(candidates) != 1:
        raise ResearchProjectionError(
            "external revision does not have exactly one pinned source/ticker/extractor profile"
        )
    return candidates[0]


def _select_external_readiness(
    *,
    archive: ExternalEventArchive,
    source_revision_id: str,
    profile: ResearchSourceClassificationProfile,
    run_definition: ResearchRunDefinition,
) -> dict[str, Any] | None:
    expected = profile.to_fingerprint_payload()
    candidates: list[dict[str, Any]] = []
    for value in archive.iter_readiness(source_revision_id):
        saved_profile = value.get("classification_profile")
        if saved_profile == expected and value.get("profile_hash") == profile.profile_hash:
            candidates.append(value)
    if len(candidates) > 1:
        raise ResearchProjectionError(
            "one source revision has multiple classifications under one exact profile"
        )
    return None if not candidates else candidates[0]


def _validate_revision_classification_conflicts(
    *,
    archive: ExternalEventArchive,
    source_revision_id: str,
    selected_profile: ResearchSourceClassificationProfile,
    run_definition: ResearchRunDefinition,
) -> dict[str, Any] | None:
    applicable_resolutions: dict[str, dict[str, Any]] = {}
    for readiness in archive.iter_readiness(source_revision_id):
        raw_fingerprint = readiness.get("classification_input_fingerprint")
        if raw_fingerprint is None:
            continue
        fingerprint = _required_sha256(
            raw_fingerprint, "classification_input_fingerprint"
        )
        resolution = _resolved_classification_conflict(
            archive=archive,
            classification_input_fingerprint=fingerprint,
            run_definition=run_definition,
        )
        if resolution is None:
            continue
        conflict_profile_hashes = tuple(
            str(value) for value in resolution.get("profile_hashes", ())
        )
        readiness_profile_hash = str(readiness.get("profile_hash", ""))
        if (
            len(set(conflict_profile_hashes)) != 1
            or not conflict_profile_hashes
            or conflict_profile_hashes[0] != readiness_profile_hash
        ):
            raise ResearchProjectionError(
                "classification conflict profile ownership is ambiguous"
            )
        decision = str(resolution.get("decision"))
        if decision == ConflictResolutionDecision.RECLASSIFY_UNDER_NEW_PROFILE.value:
            if resolution.get("new_profile_hash") != selected_profile.profile_hash:
                raise ResearchProjectionError(
                    "classification conflict requires the run to pin its reviewed new profile"
                )
        elif selected_profile.profile_hash != readiness_profile_hash:
            raise ResearchProjectionError(
                "classification conflict resolution does not authorize a different profile"
            )
        resolution_id = _required_string(
            resolution.get("classification_resolution_id"),
            "classification_resolution_id",
        )
        applicable_resolutions[resolution_id] = resolution
    if len(applicable_resolutions) > 1:
        raise ResearchProjectionError(
            "multiple classification conflict resolutions claim one source revision"
        )
    return next(iter(applicable_resolutions.values()), None)


def _resolved_classification_conflict(
    *,
    archive: ExternalEventArchive,
    classification_input_fingerprint: str,
    run_definition: ResearchRunDefinition,
) -> dict[str, Any] | None:
    conflict = archive.detect_classification_conflict(
        classification_input_fingerprint
    )
    if conflict is None:
        return None
    resolution = archive.load_conflict_resolution(classification_input_fingerprint)
    if resolution is None:
        raise ResearchProjectionError("unresolved classification conflict blocks preparation")
    if resolution.get("conflict_id") != conflict.get("classification_conflict_id"):
        raise ResearchProjectionError("conflict resolution references a different conflict")
    if resolution.get("decision") == (
        ConflictResolutionDecision.KEEP_FIRST_DURABLY_PUBLISHED.value
    ):
        canonical = conflict.get("canonical_claim")
        if not isinstance(canonical, Mapping):
            raise ResearchProjectionError(
                "KEEP_FIRST resolution lacks proven canonical chronology"
            )
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
            raise ResearchProjectionError(
                "KEEP_FIRST resolution does not pin the complete canonical result"
            )
    generation = int(resolution.get("manifest_generation", -1))
    if generation > int(run_definition.conflict_resolution_generation or -1):
        raise ResearchProjectionError("conflict resolution is newer than the pinned run")
    return {
        **resolution,
        "conflict_id": conflict["classification_conflict_id"],
        "profile_hashes": tuple(conflict.get("profile_hashes", ())),
    }


def _revision_exclusion(
    revision: object,
    reason: EvidenceExclusionReason,
    *,
    available_at: datetime | None,
    safe_detail: str | None = None,
) -> ResearchEvidenceExclusion:
    return ResearchEvidenceExclusion(
        evidence_id=f"external:{getattr(revision, 'source_revision_id')}",
        reason=reason,
        source=str(getattr(revision, "source")),
        tickers=tuple(getattr(revision, "affected_tickers")),
        sectors=tuple(getattr(revision, "affected_sectors")),
        global_relevance=bool(getattr(revision, "global_relevance")),
        available_at=available_at,
        source_available_at=getattr(revision, "source_available_at"),
        evidence_ready_at=available_at,
        source_fact_id=str(getattr(revision, "source_fact_id")),
        source_revision_id=str(getattr(revision, "source_revision_id")),
        safe_detail=safe_detail,
    )


def _context_ai_event_from_payload(payload: Mapping[str, Any]) -> ContextAIEvent:
    values = {
        item.name: payload[item.name]
        for item in fields(ContextAIEvent)
        if item.name in payload
    }
    for name in (
        "event_time",
        "valid_from",
        "valid_until",
        "source_published_at",
        "source_updated_at",
        "collected_at",
        "normalized_at",
        "classified_at",
        "available_at",
        "validated_at",
        "source_available_at",
        "system_observed_at",
        "archived_at",
        "evidence_ready_at",
        "lifecycle_effective_at",
    ):
        if values.get(name) is not None:
            values[name] = _required_datetime(values[name], name)
    values["event_type"] = ContextClassificationEventType(str(values["event_type"]))
    if values.get("urgency") is not None:
        values["urgency"] = ContextUrgency(str(values["urgency"]))
    if values.get("risk_level") is not None:
        values["risk_level"] = ContextRiskLevel(str(values["risk_level"]))
    if values.get("lifecycle_state") is not None:
        values["lifecycle_state"] = ContextLifecycleState(
            str(values["lifecycle_state"])
        )
    return ContextAIEvent(**values)


def _validate_external_event_lineage(
    *,
    event: ContextAIEvent,
    revision: object,
    readiness: Mapping[str, Any],
    profile: ResearchSourceClassificationProfile,
    attempt: Mapping[str, Any],
    canonical_claim: Mapping[str, Any],
) -> None:
    input_fingerprint = readiness.get("classification_input_fingerprint")
    attempt_id = readiness.get("canonical_classification_attempt_id")
    expected = {
        "source": getattr(revision, "source"),
        "source_id": getattr(revision, "source_fact_id"),
        "source_fact_id": getattr(revision, "source_fact_id"),
        "source_revision_id": getattr(revision, "source_revision_id"),
        "document_hash": getattr(revision, "document_hash"),
        "classification_input_fingerprint": input_fingerprint,
        "complete_output_fingerprint": readiness.get(
            "complete_output_fingerprint"
        ),
        "policy_output_fingerprint": readiness.get("policy_output_fingerprint"),
        "prompt_version": profile.prompt_version,
        "model_version": profile.model_version,
    }
    for name, value in expected.items():
        if getattr(event, name) != value:
            raise ResearchProjectionError(f"external event lineage differs on {name}")
    if event.system_observed_at != getattr(revision, "system_observed_at"):
        raise ResearchProjectionError("external event observation time changed")
    if event.source_available_at != getattr(revision, "source_available_at"):
        raise ResearchProjectionError("external event source availability changed")
    if event.archived_at != getattr(revision, "archived_at"):
        raise ResearchProjectionError("external event archive time changed")

    expected_profile = profile.to_fingerprint_payload()
    if readiness.get("classification_profile") != expected_profile:
        raise ResearchProjectionError("readiness classification profile changed")
    if readiness.get("profile_hash") != profile.profile_hash:
        raise ResearchProjectionError("readiness profile hash changed")
    if attempt.get("profile") != expected_profile:
        raise ResearchProjectionError("attempt classification profile changed")
    if attempt.get("profile_hash") != profile.profile_hash:
        raise ResearchProjectionError("attempt profile hash changed")
    trusted_scope = _trusted_input_scope_from_attempt(attempt)
    if trusted_scope is not None:
        trusted_tickers, trusted_sectors, trusted_global = trusted_scope
        revision_tickers = set(getattr(revision, "affected_tickers"))
        revision_sectors = set(getattr(revision, "affected_sectors"))
        if not revision_tickers.issubset(trusted_tickers) or not revision_sectors.issubset(
            trusted_sectors
        ):
            raise ResearchProjectionError(
                "trusted classification scope dropped fixed source scope"
            )
        if getattr(revision, "global_relevance") is True and not trusted_global:
            raise ResearchProjectionError(
                "trusted classification scope dropped fixed global scope"
            )
        if not set(trusted_tickers).issubset(event.affected_tickers) or not set(
            trusted_sectors
        ).issubset(event.affected_sectors):
            raise ResearchProjectionError(
                "materialized event dropped trusted classification scope"
            )
        if trusted_global and event.global_relevance is not True:
            raise ResearchProjectionError(
                "materialized event dropped trusted global scope"
            )
    if attempt.get("classification_input_fingerprint") != input_fingerprint:
        raise ResearchProjectionError("attempt classification input changed")
    if attempt.get("classification_attempt_id") != attempt_id:
        raise ResearchProjectionError("readiness canonical attempt changed")
    if canonical_claim.get("classification_input_fingerprint") != input_fingerprint:
        raise ResearchProjectionError("canonical claim input changed")
    if canonical_claim.get("canonical_classification_attempt_id") != attempt_id:
        raise ResearchProjectionError("canonical claim attempt changed")
    if canonical_claim.get("profile_hash") != profile.profile_hash:
        raise ResearchProjectionError("canonical claim profile changed")
    for name in (
        "complete_output_fingerprint",
        "policy_output_fingerprint",
    ):
        value = readiness.get(name)
        if attempt.get(name) != value or canonical_claim.get(name) != value:
            raise ResearchProjectionError(
                f"canonical classification lineage differs on {name}"
            )

    classified_at = _required_datetime(attempt.get("classified_at"), "classified_at")
    validated_at = _required_datetime(attempt.get("validated_at"), "validated_at")
    if event.classified_at != classified_at or event.validated_at != validated_at:
        raise ResearchProjectionError(
            "materialized event classification timestamps changed"
        )
    validate_external_event_lineage_chronology(
        system_observed_at=getattr(revision, "system_observed_at"),
        archived_at=getattr(revision, "archived_at"),
        normalized_at=event.normalized_at,
        classified_at=classified_at,
        validated_at=validated_at,
        attempt_published_at=attempt.get("archive_published_at"),
        canonical_published_at=canonical_claim.get("durably_published_at"),
        readiness_published_at=readiness.get("archive_published_at"),
        evidence_ready_at=readiness.get("evidence_ready_at"),
    )


def _canonical_external_attempt(
    *,
    archive: ExternalEventArchive,
    classification_input_fingerprint: str,
    canonical_attempt_id: str,
) -> Mapping[str, Any]:
    for value in archive.iter_classification_attempts(
        classification_input_fingerprint
    ):
        if value.get("classification_attempt_id") == canonical_attempt_id:
            return value
    raise ResearchProjectionError("canonical external classification attempt is missing")


def _external_exact_duplicate_fingerprint(
    *,
    attempt: Mapping[str, Any],
    profile: ResearchSourceClassificationProfile,
) -> str:
    """Hash exact provider-visible content across official observations.

    ``classification_input_fingerprint`` remains the strict owner of one
    request/profile/result.  This second identity handles the separate case in
    which the same immutable document is observed through two official source
    paths (for example an IR index and an earnings page).  It excludes
    generated output and source-specific profile fields.  A change to any
    document, normalized-text, or excerpt hash keeps the records distinct.
    """

    # Requiring the resolved profile at this boundary ensures callers have
    # already validated source/profile ownership.  Its source-specific fields
    # do not change byte-for-byte content equivalence.
    del profile

    return _sha256_payload(
        {
            "document_hash": _required_sha256(
                attempt.get("document_hash"), "document_hash"
            ),
            "normalized_text_hash": _required_sha256(
                attempt.get("normalized_text_hash"), "normalized_text_hash"
            ),
            "excerpt_hash": _required_sha256(
                attempt.get("excerpt_hash"), "excerpt_hash"
            ),
        }
    )


def _canonical_exact_input_owner_fingerprint(
    *,
    exact_duplicate_fingerprint: str,
    trusted_tickers: Iterable[str],
    trusted_sectors: Iterable[str],
    trusted_global_relevance: bool,
) -> str:
    """Own one exact semantic input independently of its observation path.

    Source-specific classifier fingerprints remain untouched for audit and
    profile validation.  This projection identity is deliberately narrower:
    byte-for-byte document, normalized-text, and excerpt equality (already
    represented by ``exact_duplicate_fingerprint``) plus trusted input scope.
    Generated model output, source IDs, timestamps, and source adapter names
    are excluded.  Consequently two official observations can share one
    canonical owner only when the meaningful provider input is exact.
    """

    duplicate = _required_sha256(
        exact_duplicate_fingerprint,
        "exact_duplicate_fingerprint",
    )
    if not isinstance(trusted_global_relevance, bool):
        raise ResearchProjectionError("trusted_global_relevance must be bool")
    return _sha256_payload(
        {
            "owner_version": "canonical_exact_input_owner_v1",
            "exact_duplicate_fingerprint": duplicate,
            "trusted_scope": {
                "affected_tickers": sorted(
                    {
                        _normalize_symbol(value, "trusted_tickers")
                        for value in trusted_tickers
                    }
                ),
                "affected_sectors": sorted(
                    {
                        _normalize_symbol(value, "trusted_sectors")
                        for value in trusted_sectors
                    }
                ),
                "global_relevance": trusted_global_relevance,
            },
        }
    )


def _external_canonical_classification_owner(
    *,
    attempt: Mapping[str, Any],
    classification_input_fingerprint: str,
    exact_duplicate_fingerprint: str,
) -> str:
    """Return a cross-source owner only when trusted request scope is durable.

    Archives created before ``trusted_input_scope`` remain readable, but keep
    their original source-specific classification owner.  Silently inferring
    trusted scope from generated output would make duplicate identity depend
    on the model and could collapse semantically different requests.
    """

    input_fingerprint = _required_sha256(
        classification_input_fingerprint,
        "classification_input_fingerprint",
    )
    trusted_scope = _trusted_input_scope_from_attempt(attempt)
    if trusted_scope is None:
        return input_fingerprint
    trusted_tickers, trusted_sectors, trusted_global = trusted_scope
    return _canonical_exact_input_owner_fingerprint(
        exact_duplicate_fingerprint=exact_duplicate_fingerprint,
        trusted_tickers=trusted_tickers,
        trusted_sectors=trusted_sectors,
        trusted_global_relevance=trusted_global,
    )


def _trusted_input_scope_from_attempt(
    attempt: Mapping[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...], bool] | None:
    raw_scope = attempt.get("trusted_input_scope")
    if raw_scope is None:
        return None
    if not isinstance(raw_scope, Mapping):
        raise ResearchProjectionError("trusted_input_scope must be a mapping")
    raw_tickers = raw_scope.get("affected_tickers")
    raw_sectors = raw_scope.get("affected_sectors")
    raw_global = raw_scope.get("global_relevance")
    if (
        not isinstance(raw_tickers, (list, tuple))
        or not all(isinstance(value, str) for value in raw_tickers)
        or not isinstance(raw_sectors, (list, tuple))
        or not all(isinstance(value, str) for value in raw_sectors)
        or not isinstance(raw_global, bool)
    ):
        raise ResearchProjectionError("trusted_input_scope has an invalid shape")
    return (
        tuple(sorted({_normalize_symbol(value, "trusted_tickers") for value in raw_tickers})),
        tuple(sorted({_normalize_symbol(value, "trusted_sectors") for value in raw_sectors})),
        raw_global,
    )


def _external_relationships(
    values: Iterable[tuple[ResearchEvidence, str, tuple[str, ...]]],
    *,
    correlation_version: str,
) -> tuple[ResearchEvidenceRelationship, ...]:
    groups: dict[str, list[tuple[ResearchEvidence, tuple[str, ...]]]] = {}
    for evidence, group_id, relationship_types in values:
        groups.setdefault(group_id, []).append((evidence, relationship_types))
    relationships: list[ResearchEvidenceRelationship] = []
    for group_id, members in sorted(groups.items()):
        members.sort(key=lambda item: item[0].evidence_id)
        for left_index, (left, left_types) in enumerate(members):
            for right, right_types in members[left_index + 1 :]:
                if left.evidence_ready_at is None or right.evidence_ready_at is None:
                    continue
                historical_times = [
                    value
                    for value in (left.source_available_at, right.source_available_at)
                    if value is not None
                ]
                for relationship_type in sorted(set(left_types) | set(right_types)):
                    relationships.append(
                        ResearchEvidenceRelationship(
                            correlation_group_id=group_id,
                            left_evidence_id=left.evidence_id,
                            right_evidence_id=right.evidence_id,
                            relationship_type=relationship_type,
                            correlation_version=correlation_version,
                            live_ready_at=max(
                                left.evidence_ready_at,
                                right.evidence_ready_at,
                            ),
                            historical_ready_at=(
                                max(historical_times)
                                if len(historical_times) == 2
                                else None
                            ),
                        )
                    )
    return tuple(relationships)


def _correlate_sec_company_earnings(
    evidence: Iterable[ResearchEvidence],
    *,
    correlation_version: str,
) -> tuple[ResearchEvidenceRelationship, ...]:
    """Link, but never merge, deterministic SEC/company earnings candidates."""
    values = tuple(evidence)
    sec_results = [
        value
        for value in values
        if value.source == SEC_EVENT_SOURCE
        and value.policy_match_key == "AI_EVENT_TYPE:SEC_8K_RESULTS"
    ]
    company_results = [
        value for value in values if value.source == "company_earnings"
    ]
    relationships: list[ResearchEvidenceRelationship] = []
    for sec in sec_results:
        for company in company_results:
            if not set(sec.tickers).intersection(company.tickers):
                continue
            sec_time = sec.source_available_at or sec.available_at
            company_time = company.source_available_at or company.available_at
            if sec_time is None or company_time is None:
                continue
            if abs(company_time - sec_time) > timedelta(hours=48):
                continue
            if sec.evidence_ready_at is None or company.evidence_ready_at is None:
                continue
            ticker = sorted(set(sec.tickers).intersection(company.tickers))[0]
            group_id = "earnings_candidate_" + _sha256_payload(
                {
                    "ticker": ticker,
                    "sec_evidence_id": sec.evidence_id,
                    "company_evidence_id": company.evidence_id,
                    "correlation_version": correlation_version,
                }
            )
            relationships.append(
                ResearchEvidenceRelationship(
                    correlation_group_id=group_id,
                    left_evidence_id=sec.evidence_id,
                    right_evidence_id=company.evidence_id,
                    relationship_type="EARNINGS_RELATED_CANDIDATE",
                    correlation_version=correlation_version,
                    live_ready_at=max(sec.evidence_ready_at, company.evidence_ready_at),
                    historical_ready_at=max(sec_time, company_time),
                )
            )
    return tuple(relationships)


def _correlate_official_company_observations(
    evidence: Iterable[ResearchEvidence],
    *,
    correlation_version: str,
) -> tuple[ResearchEvidenceRelationship, ...]:
    """Link an IR/RSS observation to its earnings-page observation exactly.

    A pair is related only when the configured company ticker agrees and the
    archives expose either the exact canonical official URL or the exact
    meaningful classification-content fingerprint.  URL equality may link
    unequal text revisions, but never makes them duplicates; content equality
    can collapse only later under the separate canonical-owner rule.
    """

    values = tuple(evidence)
    source_tickers = {
        "palantir_ir": "PLTR",
        "lockheed_martin_rss": "LMT",
    }
    earnings = [value for value in values if value.source == "company_earnings"]
    relationships: list[ResearchEvidenceRelationship] = []
    for observation in values:
        expected_ticker = source_tickers.get(observation.source)
        if expected_ticker is None or expected_ticker not in observation.tickers:
            continue
        for package in earnings:
            if expected_ticker not in package.tickers:
                continue
            observation_url = _canonical_official_evidence_url(
                observation,
                ticker=expected_ticker,
            )
            package_url = _canonical_official_evidence_url(
                package,
                ticker=expected_ticker,
            )
            same_url = (
                observation_url is not None
                and observation_url == package_url
            )
            same_content = (
                observation.exact_duplicate_fingerprint is not None
                and observation.exact_duplicate_fingerprint
                == package.exact_duplicate_fingerprint
            )
            if not same_url and not same_content:
                continue
            if (
                observation.evidence_ready_at is None
                or package.evidence_ready_at is None
            ):
                continue
            relationship_type = (
                "EXACT_OFFICIAL_CONTENT_OBSERVATION"
                if same_content
                else "SAME_OFFICIAL_RELEASE_URL"
            )
            relationship_identity = (
                observation.exact_duplicate_fingerprint
                if same_content
                else observation_url
            )
            if relationship_identity is None:  # Defensive for type narrowing.
                continue
            historical_times = (
                observation.source_available_at,
                package.source_available_at,
            )
            relationships.append(
                ResearchEvidenceRelationship(
                    correlation_group_id="official_release_" + _sha256_payload(
                        {
                            "ticker": expected_ticker,
                            "relationship_type": relationship_type,
                            "relationship_identity": relationship_identity,
                            "correlation_version": correlation_version,
                        }
                    ),
                    left_evidence_id=observation.evidence_id,
                    right_evidence_id=package.evidence_id,
                    relationship_type=relationship_type,
                    correlation_version=correlation_version,
                    live_ready_at=max(
                        observation.evidence_ready_at,
                        package.evidence_ready_at,
                    ),
                    historical_ready_at=(
                        max(historical_times)  # type: ignore[arg-type]
                        if all(value is not None for value in historical_times)
                        else None
                    ),
                )
            )
    return tuple(relationships)


def _canonical_official_evidence_url(
    value: ResearchEvidence,
    *,
    ticker: str,
) -> str | None:
    payload = value.fingerprint_payload
    raw_uri = payload.get("source_uri")
    if raw_uri is None:
        nested = payload.get("context_ai_event")
        if isinstance(nested, Mapping):
            raw_uri = nested.get("source_uri")
    if not isinstance(raw_uri, str) or not raw_uri.strip():
        return None
    try:
        parts = urlsplit(raw_uri.strip())
    except ValueError:
        return None
    if (
        parts.scheme.lower() != "https"
        or parts.username is not None
        or parts.password is not None
        or parts.hostname is None
    ):
        return None
    host = parts.hostname.lower().rstrip(".")
    allowed = (
        host == "investors.palantir.com"
        if ticker == "PLTR"
        else host == "lockheedmartin.com" or host.endswith(".lockheedmartin.com")
    )
    if not allowed:
        return None
    netloc = host if parts.port in (None, 443) else f"{host}:{parts.port}"
    return urlunsplit(
        (
            "https",
            netloc,
            parts.path or "/",
            parts.query,
            "",
        )
    )


def _selection_exclusion_reason(
    value: ResearchEvidence,
    *,
    ticker: str,
    sector: str | None,
    decision_time: datetime,
    max_age: timedelta,
    availability_mode: ResearchAvailabilityMode | None = None,
) -> EvidenceExclusionReason | None:
    if not value.policy_eligible:
        if value.classification_conflict_id is not None and value.conflict_resolution_id is None:
            return EvidenceExclusionReason.CLASSIFICATION_CONFLICT
        return EvidenceExclusionReason.POLICY_INELIGIBLE
    effective_available_at = value.effective_available_at(availability_mode)
    if effective_available_at is None:
        if availability_mode is ResearchAvailabilityMode.LIVE_SYSTEM_READY:
            return EvidenceExclusionReason.MISSING_EVIDENCE_READY_AT
        if availability_mode is ResearchAvailabilityMode.HISTORICAL_SOURCE_TIME:
            return EvidenceExclusionReason.MISSING_SOURCE_AVAILABILITY
        return EvidenceExclusionReason.MISSING_AVAILABILITY
    if not _scope_matches(value, ticker=ticker, sector=sector):
        return EvidenceExclusionReason.SCOPE_MISMATCH
    if effective_available_at > decision_time:
        return EvidenceExclusionReason.FUTURE
    if value.valid_from is not None and value.valid_from > decision_time:
        return EvidenceExclusionReason.FUTURE
    if value.valid_until is not None and decision_time > value.valid_until:
        return EvidenceExclusionReason.EXPIRED
    if value.valid_until is None and decision_time - effective_available_at > max_age:
        return EvidenceExclusionReason.OUTSIDE_LOOKBACK
    return None


def _hydration_exclusion_matches_decision(
    value: ResearchEvidenceExclusion,
    *,
    ticker: str,
    sector: str | None,
    decision_time: datetime,
    max_age: timedelta,
) -> bool:
    has_scope = value.global_relevance or bool(value.tickers) or bool(value.effective_sectors)
    if not has_scope:
        return False
    if not (
        value.global_relevance
        or ticker in value.tickers
        or (sector is not None and sector in value.effective_sectors)
    ):
        return False
    if value.reason is EvidenceExclusionReason.MISSING_AVAILABILITY:
        return True
    if value.available_at is None or value.available_at > decision_time:
        return False
    return decision_time - value.available_at <= max_age


def _scope_matches(
    value: ResearchEvidence,
    *,
    ticker: str,
    sector: str | None,
) -> bool:
    return (
        value.global_relevance
        or ticker in value.tickers
        or (sector is not None and sector in value.effective_sectors)
    )


def _resolve_lifecycle_as_of(
    evidence: Iterable[ResearchEvidence],
    *,
    lifecycle_revisions: Iterable[ResearchLifecycleRevision] = (),
    decision_time: datetime,
    mode: ResearchAvailabilityMode | None,
) -> tuple[list[ResearchEvidence], list[ResearchEvidenceExclusion]]:
    all_evidence = tuple(evidence)
    legacy: list[ResearchEvidence] = [
        value for value in all_evidence if value.source_fact_id is None
    ]
    evidence_groups: dict[tuple[str, str], list[ResearchEvidence]] = {}
    evidence_by_revision: dict[tuple[str, str, str], ResearchEvidence] = {}
    for value in all_evidence:
        if value.source_fact_id is None:
            continue
        evidence_groups.setdefault((value.source, value.source_fact_id), []).append(value)
        if value.source_revision_id is not None:
            evidence_by_revision[(value.source, value.source_fact_id, value.source_revision_id)] = value
    groups: dict[tuple[str, str], list[ResearchLifecycleRevision]] = {}
    for value in lifecycle_revisions:
        groups.setdefault((value.source, value.source_fact_id), []).append(value)
    # Backward-compatible callers may provide lifecycle fields only on the
    # evidence itself.  Synthesize markers when no explicit archive marker was
    # supplied for that fact.
    for group_key, values in evidence_groups.items():
        if group_key in groups:
            continue
        groups[group_key] = [
            ResearchLifecycleRevision(
                source=value.source,
                source_fact_id=value.source_fact_id or value.source_record_id,
                source_revision_id=value.source_revision_id or value.source_record_id,
                revision_sequence=value.revision_sequence or 1,
                supersedes_revision_id=value.supersedes_revision_id,
                lifecycle_state=value.lifecycle_state or "ACTIVE",
                lifecycle_effective_at=(
                    value.lifecycle_effective_at
                    or value.system_observed_at
                    or value.available_at
                    or datetime.min.replace(tzinfo=UTC)
                ),
                system_observed_at=(
                    value.system_observed_at
                    or value.lifecycle_effective_at
                    or value.available_at
                    or datetime.min.replace(tzinfo=UTC)
                ),
                evidence_ready_at=(value.evidence_ready_at or value.available_at),
            )
            for value in values
        ]
    selected = list(legacy)
    exclusions: list[ResearchEvidenceExclusion] = []
    for (source, fact_id), revisions in sorted(groups.items()):
        visible: list[tuple[datetime, int, ResearchLifecycleRevision]] = []
        for revision in revisions:
            effective = revision.effective_at(mode)
            if effective > decision_time:
                continue
            visible.append((effective, revision.revision_sequence, revision))
        if not visible:
            continue
        visible.sort(key=lambda item: (item[0], item[1], item[2].source_revision_id))
        latest_time, latest_sequence, latest = visible[-1]
        tied = [
            item
            for item in visible
            if item[0] == latest_time and item[1] == latest_sequence
        ]
        if len(tied) > 1:
            for _effective, _sequence, marker in tied:
                value = evidence_by_revision.get(
                    (source, fact_id, marker.source_revision_id)
                )
                exclusions.append(
                    _lifecycle_marker_exclusion(
                        marker,
                        value,
                        EvidenceExclusionReason.LIFECYCLE_ORDER_CONFLICT,
                        safe_detail=f"ambiguous lifecycle head for {fact_id}",
                    )
                )
            for value in evidence_groups.get((source, fact_id), ()):
                if value.source_revision_id not in {item[2].source_revision_id for item in tied}:
                    exclusions.append(
                        _evidence_exclusion(value, EvidenceExclusionReason.SUPERSEDED_BY_LIFECYCLE_REVISION)
                    )
            continue
        for value in evidence_groups.get((source, fact_id), ()):
            if value.source_revision_id == latest.source_revision_id:
                continue
            exclusions.append(
                _evidence_exclusion(value, EvidenceExclusionReason.SUPERSEDED_BY_LIFECYCLE_REVISION)
            )
        if latest.lifecycle_state in {"DELETED", "RETRACTED"}:
            exclusions.append(
                _lifecycle_marker_exclusion(
                    latest,
                    evidence_by_revision.get((source, fact_id, latest.source_revision_id)),
                    EvidenceExclusionReason.LIFECYCLE_DELETED_OR_RETRACTED,
                )
            )
            continue
        if latest.evidence_ready_at is None or (
            mode is ResearchAvailabilityMode.LIVE_SYSTEM_READY
            and latest.evidence_ready_at > decision_time
        ):
            exclusions.append(
                _lifecycle_marker_exclusion(
                    latest,
                    evidence_by_revision.get((source, fact_id, latest.source_revision_id)),
                    EvidenceExclusionReason.LIFECYCLE_REVISION_PENDING,
                )
            )
            continue
        current = evidence_by_revision.get((source, fact_id, latest.source_revision_id))
        if current is not None:
            selected.append(current)
    return selected, exclusions


def _lifecycle_effective_for_mode(
    value: ResearchEvidence, mode: ResearchAvailabilityMode | None
) -> datetime | None:
    if mode is ResearchAvailabilityMode.LIVE_SYSTEM_READY:
        return value.system_observed_at
    if mode is ResearchAvailabilityMode.HISTORICAL_SOURCE_TIME:
        return value.lifecycle_effective_at
    return value.lifecycle_effective_at or value.available_at


def _collapse_exact_duplicates_as_of(
    evidence: Iterable[ResearchEvidence],
    *,
    decision_time: datetime,
    mode: ResearchAvailabilityMode | None,
) -> tuple[list[ResearchEvidence], list[ResearchEvidenceExclusion]]:
    values = tuple(evidence)
    exclusions: list[ResearchEvidenceExclusion] = []
    conflicted_evidence_ids: set[str] = set()
    canonical_inputs: dict[str, list[ResearchEvidence]] = {}
    for value in values:
        canonical_owner = (
            value.canonical_classification_owner_fingerprint
            or value.classification_input_fingerprint
        )
        if canonical_owner is not None:
            canonical_inputs.setdefault(
                canonical_owner, []
            ).append(value)
    for fingerprint, members in sorted(canonical_inputs.items()):
        output_identities = {
            (
                value.complete_output_fingerprint,
                value.policy_output_fingerprint,
            )
            for value in members
        }
        if len(output_identities) <= 1:
            continue
        for value in members:
            conflicted_evidence_ids.add(value.evidence_id)
            exclusions.append(
                _evidence_exclusion(
                    value,
                    EvidenceExclusionReason.CLASSIFICATION_CONFLICT,
                    safe_detail=(
                        "conflicting canonical outputs for "
                        f"{fingerprint[:12]}"
                    ),
                )
            )

    unkeyed: list[ResearchEvidence] = []
    groups: dict[tuple[str, str, str], list[ResearchEvidence]] = {}
    for value in values:
        if value.evidence_id in conflicted_evidence_ids:
            continue
        canonical_owner = (
            value.canonical_classification_owner_fingerprint
            or value.classification_input_fingerprint
        )
        if value.exact_duplicate_fingerprint is not None:
            groups.setdefault(
                (
                    "MEANINGFUL_INPUT",
                    value.exact_duplicate_fingerprint,
                    # Exact duplicate collapse never crosses canonical
                    # classification owners.  The nullable projection owner
                    # permits SEC and company observations with exact semantic
                    # input to share one result without rewriting either
                    # source-specific classifier fingerprint.
                    canonical_owner or value.exact_duplicate_fingerprint,
                ),
                [],
            ).append(value)
        elif canonical_owner is not None:
            groups.setdefault(
                (
                    "CANONICAL_INPUT",
                    canonical_owner,
                    canonical_owner,
                ),
                [],
            ).append(value)
        else:
            unkeyed.append(value)
    selected = list(unkeyed)
    for (_group_kind, _fingerprint, _canonical_input), members in sorted(
        groups.items()
    ):
        members.sort(
            key=lambda value: (
                value.effective_available_at(mode) or datetime.max.replace(tzinfo=UTC),
                value.source,
                value.source_record_id,
                value.evidence_id,
            )
        )
        canonical = members[0]
        if len(members) > 1:
            visible_lineage: list[str] = []
            visibility: dict[str, str] = {}
            for member in members:
                for lineage_id in member.lineage_ids:
                    raw_time = member.lineage_visibility.get(lineage_id)
                    if raw_time is not None:
                        parsed = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                        if parsed > decision_time:
                            continue
                        visibility[lineage_id] = raw_time
                    if lineage_id not in visible_lineage:
                        visible_lineage.append(lineage_id)
            canonical = replace(
                canonical,
                lineage_ids=tuple(visible_lineage),
                lineage_visibility=visibility,
            )
            for duplicate in members[1:]:
                exclusions.append(
                    _evidence_exclusion(duplicate, EvidenceExclusionReason.EXACT_DUPLICATE_COLLAPSED)
                )
        selected.append(canonical)
    return selected, exclusions


def _evidence_exclusion(
    value: ResearchEvidence,
    reason: EvidenceExclusionReason,
    *,
    safe_detail: str | None = None,
) -> ResearchEvidenceExclusion:
    return ResearchEvidenceExclusion(
        evidence_id=value.evidence_id,
        reason=reason,
        source=value.source,
        tickers=value.tickers,
        sector=value.sector,
        sectors=value.sectors,
        global_relevance=value.global_relevance,
        available_at=value.available_at,
        source_available_at=value.source_available_at,
        evidence_ready_at=value.evidence_ready_at,
        source_fact_id=value.source_fact_id,
        source_revision_id=value.source_revision_id,
        safe_detail=safe_detail,
    )


def _lifecycle_marker_exclusion(
    marker: ResearchLifecycleRevision,
    evidence: ResearchEvidence | None,
    reason: EvidenceExclusionReason,
    *,
    safe_detail: str | None = None,
) -> ResearchEvidenceExclusion:
    if evidence is not None:
        return _evidence_exclusion(evidence, reason, safe_detail=safe_detail)
    return ResearchEvidenceExclusion(
        evidence_id=f"lifecycle:{marker.source}:{marker.source_revision_id}",
        reason=reason,
        source=marker.source,
        source_fact_id=marker.source_fact_id,
        source_revision_id=marker.source_revision_id,
        evidence_ready_at=marker.evidence_ready_at,
        safe_detail=safe_detail,
    )


def _classification_matches_profile(
    saved: Mapping[str, Any],
    profile: ResearchClassificationProfile,
) -> bool:
    expected = profile.to_fingerprint_payload()
    return all(saved.get(name) == value for name, value in expected.items())


def _classification_is_semantically_valid(saved: Mapping[str, Any]) -> bool:
    row = saved.get("ledger_row")
    return (
        saved.get("classification_complete") is True
        and saved.get("status") == "VALID"
        and isinstance(row, Mapping)
        and row.get("status") == "VALID"
        and row.get("validation_outcome") is True
    )


def _saved_8k_section_identity(
    *,
    accession: str,
    state: Mapping[str, Any],
    saved: Mapping[str, Any],
) -> str:
    row = _mapping(saved.get("ledger_row"), "ledger_row")
    if _required_string(state.get("accession_number"), "accession_number") != accession:
        raise ResearchProjectionError("archived filing accession identity is inconsistent")
    if _required_string(saved.get("accession_number"), "saved.accession_number") != accession:
        raise ResearchProjectionError("saved classification accession is inconsistent")
    official_identity = _required_string(
        state.get("official_document_identity"),
        "official_document_identity",
    )
    document_hash = _required_sha256(saved.get("document_hash"), "document_hash")
    state_document_hash = _required_sha256(
        state.get("document_hash"),
        "state.document_hash",
    )
    row_document_hash = _required_sha256(
        row.get("document_hash"),
        "ledger_row.document_hash",
    )
    if document_hash != state_document_hash or document_hash != row_document_hash:
        raise ResearchProjectionError(
            "classification document hash disagrees with archived SEC filing"
        )
    full_section_hash = _required_sha256(
        saved.get("full_section_hash"),
        "full_section_hash",
    )
    item_number = _required_string(saved.get("item_number"), "item_number")
    return _sha256_payload(
        {
            "source": SEC_EVENT_SOURCE,
            "accession_number": accession,
            "official_document_identity": official_identity,
            "document_hash": document_hash,
            "item_number": item_number,
            "full_section_hash": full_section_hash,
        }
    )


def _normalize_saved_8k_classification(
    *,
    accession: str,
    state: Mapping[str, Any],
    saved: Mapping[str, Any],
    run_definition: ResearchRunDefinition,
    section_identity: str,
    available_at: datetime,
) -> ResearchEvidence:
    row = _mapping(saved.get("ledger_row"), "ledger_row")
    source = _required_string(row.get("source"), "ledger_row.source")
    if source not in {SEC_EVENT_SOURCE, "sec_edgar_v1"}:
        raise ResearchProjectionError("saved classification source is not SEC EDGAR")
    event_type = ContextClassificationEventType(
        _required_string(row.get("event_type"), "ledger_row.event_type")
    )
    if event_type is ContextClassificationEventType.UNKNOWN:
        raise ResearchProjectionError("VALID classification cannot use UNKNOWN event type")
    risk_level = ContextRiskLevel(
        _required_string(row.get("risk_level"), "ledger_row.risk_level")
    )
    urgency = ContextUrgency(
        _required_string(row.get("urgency"), "ledger_row.urgency")
    )
    if risk_level is ContextRiskLevel.UNKNOWN or urgency is ContextUrgency.UNKNOWN:
        raise ResearchProjectionError(
            "VALID classification cannot use UNKNOWN risk or urgency"
        )
    confidence = _required_unit_interval(row.get("confidence"), "ledger_row.confidence")
    summary = _required_string(row.get("summary"), "ledger_row.summary")
    profile = run_definition.classification_profile
    if row.get("model_version") != profile.model_version:
        raise ResearchProjectionError("ledger model version disagrees with pinned profile")
    if row.get("prompt_version") != profile.prompt_version:
        raise ResearchProjectionError("ledger prompt version disagrees with pinned profile")
    for field_name in (
        "status",
        "event_type",
        "risk_level",
        "urgency",
        "confidence",
        "summary",
        "provider",
        "model_version",
        "prompt_version",
    ):
        if saved.get(field_name) != row.get(field_name):
            raise ResearchProjectionError(
                f"saved and ledger classification values disagree on {field_name}"
            )
    if _required_datetime(
        saved.get("classified_at"),
        "saved.classified_at",
    ) != _required_datetime(row.get("classified_at"), "ledger_row.classified_at"):
        raise ResearchProjectionError(
            "saved and ledger classification values disagree on classified_at"
        )
    if saved.get("classification_attempt_id") != row.get("classification_attempt_id"):
        raise ResearchProjectionError("saved and ledger attempt identities disagree")
    if saved.get("classification_request_id") != row.get("classification_request_id"):
        raise ResearchProjectionError("saved and ledger request identities disagree")
    tickers = _json_string_list(row.get("affected_tickers_json"), "affected_tickers_json")
    if any(value not in run_definition.ticker_universe for value in tickers):
        raise ResearchProjectionError("classification ticker is outside run universe")
    row_available_at = _optional_datetime(
        row.get("source_published_at"),
        "source_published_at",
    )
    if row_available_at is not None and row_available_at != available_at:
        raise ResearchProjectionError(
            "classification availability disagrees with archived SEC acceptance"
        )
    official_identity = _required_string(
        state.get("official_document_identity"),
        "official_document_identity",
    )
    official_document_url = _required_string(
        state.get("official_document_url"),
        "official_document_url",
    )
    document_hash = _required_sha256(saved.get("document_hash"), "document_hash")
    full_section_hash = _required_sha256(
        saved.get("full_section_hash"),
        "full_section_hash",
    )
    excerpt_hash = _required_sha256(saved.get("excerpt_hash"), "excerpt_hash")
    item_number = _required_string(saved.get("item_number"), "item_number")
    if section_identity != _saved_8k_section_identity(
        accession=accession,
        state=state,
        saved=saved,
    ):
        raise ResearchProjectionError("source section identity changed during hydration")
    classification_attempt_id = _required_string(
        row.get("classification_attempt_id"),
        "classification_attempt_id",
    )
    evidence_id = _stable_id(
        "context_event_sec_8k",
        {
            "section_identity": section_identity,
            "classification_attempt_id": classification_attempt_id,
            "profile": run_definition.classification_profile.to_fingerprint_payload(),
        },
    )
    external_time_fields: dict[str, datetime] = {}
    if run_definition.external_classification_profiles:
        external_time_fields = {
            "source_available_at": available_at,
            "system_observed_at": _required_datetime(
                row.get("collected_at"), "collected_at"
            ),
        }
        saved_ready_at = _optional_datetime(
            saved.get("evidence_ready_at"), "evidence_ready_at"
        )
        if saved_ready_at is not None:
            external_time_fields["evidence_ready_at"] = saved_ready_at
    materialized = ContextAIEvent(
        context_event_id=evidence_id,
        event_time=available_at,
        source=SEC_EVENT_SOURCE,
        source_id=section_identity,
        affected_tickers=tickers,
        event_type=event_type,
        urgency=urgency,
        risk_level=risk_level,
        confidence=confidence,
        summary=summary,
        prompt_version=profile.prompt_version,
        model_version=profile.model_version,
        raw_input_hash=_required_sha256(
            row.get("raw_input_hash"),
            "ledger_row.raw_input_hash",
        ),
        raw_input_id=_required_string(row.get("raw_input_id"), "raw_input_id"),
        source_document_id=_required_string(
            row.get("source_document_id"),
            "source_document_id",
        ),
        classification_request_id=_required_string(
            row.get("classification_request_id"),
            "classification_request_id",
        ),
        classification_attempt_id=classification_attempt_id,
        validation_result_id=_required_string(
            row.get("validation_result_id"),
            "validation_result_id",
        ),
        source_type=_required_string(row.get("source_type"), "source_type"),
        source_platform=_required_string(
            row.get("source_platform"),
            "source_platform",
        ),
        source_uri=_required_string(row.get("source_uri"), "source_uri"),
        source_locator=_required_string(
            row.get("source_locator"),
            "source_locator",
        ),
        document_hash=document_hash,
        source_published_at=available_at,
        source_updated_at=_optional_datetime(
            row.get("source_updated_at"),
            "source_updated_at",
        ),
        collected_at=_required_datetime(row.get("collected_at"), "collected_at"),
        normalized_at=_required_datetime(
            row.get("normalized_at"),
            "normalized_at",
        ),
        classified_at=_required_datetime(
            row.get("classified_at"),
            "classified_at",
        ),
        available_at=available_at,
        validated_at=_required_datetime(
            row.get("validated_at"),
            "validated_at",
        ),
        provider=_required_string(row.get("provider"), "provider"),
        **external_time_fields,
        trace_id=_optional_string(row.get("trace_id"), "trace_id"),
    )
    normalized = normalize_context_ai_event(materialized)
    safe_payload = {
        "context_ai_event": to_json_dict(materialized),
        "sec_source_section": {
            "source_section_identity": section_identity,
            "accession_number": accession,
            "official_document_identity": official_identity,
            "official_document_url": official_document_url,
            "item_number": item_number,
            "document_hash": document_hash,
            "full_section_hash": full_section_hash,
            "excerpt_hash": excerpt_hash,
        },
        "classification_profile": profile.to_fingerprint_payload(),
    }
    exact_duplicate_fingerprint = None
    canonical_owner_fingerprint = None
    complete_output_fingerprint = None
    policy_output_fingerprint = None
    if run_definition.external_classification_profiles:
        exact_duplicate_fingerprint = _sha256_payload(
            {
                "document_hash": document_hash,
                "normalized_text_hash": full_section_hash,
                "excerpt_hash": excerpt_hash,
            }
        )
        canonical_owner_fingerprint = _canonical_exact_input_owner_fingerprint(
            exact_duplicate_fingerprint=exact_duplicate_fingerprint,
            trusted_tickers=tickers,
            trusted_sectors=(),
            trusted_global_relevance=False,
        )
        canonical_output = {
            "status": "VALID",
            "event_type": event_type.value,
            "risk_level": risk_level.value,
            "urgency": urgency.value,
            "confidence": confidence,
            "summary": summary,
            "affected_tickers": list(tickers),
            "affected_sectors": [],
            "global_relevance": False,
        }
        complete_output_fingerprint, policy_output_fingerprint = (
            output_fingerprints(canonical_output)
        )
    return ResearchEvidence(
        evidence_id=normalized.evidence_id,
        category=normalized.category,
        policy_match_key=normalized.policy_match_key,
        source=normalized.source,
        source_record_id=normalized.source_record_id,
        tickers=normalized.tickers,
        sector=normalized.sector,
        global_relevance=normalized.global_relevance,
        available_at=normalized.available_at,
        source_available_at=normalized.source_available_at,
        system_observed_at=normalized.system_observed_at,
        evidence_ready_at=normalized.evidence_ready_at,
        valid_from=normalized.valid_from,
        valid_until=normalized.valid_until,
        fingerprint_payload=safe_payload,
        lineage_ids=tuple(
            dict.fromkeys(
                (
                    *normalized.lineage_ids,
                    accession,
                    document_hash,
                    full_section_hash,
                )
            )
        ),
        classification_input_fingerprint=None,
        canonical_classification_owner_fingerprint=canonical_owner_fingerprint,
        exact_duplicate_fingerprint=exact_duplicate_fingerprint,
        complete_output_fingerprint=complete_output_fingerprint,
        policy_output_fingerprint=policy_output_fingerprint,
        policy_eligible=True,
    )


def _normalize_archived_form4_event(
    *,
    event: Mapping[str, Any],
    filing: Mapping[str, Any],
    ordinal: int,
    archive_accession: str,
    payload_ticker: str,
    payload_issuer_cik: str,
    payload_is_amendment: bool,
    payload_amends_accession: str | None,
) -> ResearchEvidence:
    event_type = DeterministicContextEventType(
        _required_string(event.get("event_type"), "event_type")
    )
    ticker = _normalize_symbol(event.get("issuer_ticker"), "issuer_ticker")
    accession = _required_string(event.get("accession_number"), "accession_number")
    filing_accession = _required_string(
        filing.get("accession_number"),
        "filing.accession_number",
    )
    if filing_accession != accession or archive_accession != accession:
        raise ResearchProjectionError("Form 4 event accession disagrees with filing")
    filing_ticker = _normalize_symbol(filing.get("ticker"), "filing.ticker")
    filing_issuer_cik = _required_string(
        filing.get("issuer_cik"),
        "filing.issuer_cik",
    )
    event_issuer_cik = _required_string(event.get("issuer_cik"), "issuer_cik")
    if ticker != payload_ticker or ticker != filing_ticker:
        raise ResearchProjectionError("Form 4 issuer ticker lineage is inconsistent")
    if event_issuer_cik != payload_issuer_cik or event_issuer_cik != filing_issuer_cik:
        raise ResearchProjectionError("Form 4 issuer CIK lineage is inconsistent")
    document_hash = _required_sha256(filing.get("document_hash"), "document_hash")
    source_uri = _required_string(
        filing.get("official_document_url"),
        "official_document_url",
    )
    available_at = _optional_datetime(event.get("available_at"), "available_at")
    acceptance_at = _optional_datetime(filing.get("acceptance_at"), "acceptance_at")
    if available_at is not None and acceptance_at is not None and available_at != acceptance_at:
        raise ResearchProjectionError(
            "Form 4 event availability disagrees with archived SEC acceptance"
        )
    owners = _form4_owners_from_payload(event.get("reporting_owners"))
    transaction_date = _optional_date(event.get("transaction_date"), "transaction_date")
    transaction_code = _required_string(
        event.get("transaction_code"),
        "transaction_code",
    )
    expected_code = {
        DeterministicContextEventType.SEC_FORM4_PURCHASE: "P",
        DeterministicContextEventType.SEC_FORM4_SALE: "S",
    }[event_type]
    if transaction_code != expected_code:
        raise ResearchProjectionError(
            "Form 4 deterministic event type disagrees with transaction code"
        )
    is_amendment = _required_bool(event.get("is_amendment"), "is_amendment")
    amends_accession = _optional_string(
        event.get("amends_accession"),
        "amends_accession",
    )
    filing_form_type = _required_string(filing.get("form_type"), "filing.form_type")
    filing_amends_accession = _optional_string(
        filing.get("amendment_of"),
        "filing.amendment_of",
    )
    if (
        is_amendment != payload_is_amendment
        or is_amendment != (filing_form_type == "4/A")
        or amends_accession != payload_amends_accession
        or amends_accession != filing_amends_accession
    ):
        raise ResearchProjectionError("Form 4 amendment lineage is inconsistent")
    aggregate_eligibility = _required_string(
        event.get("aggregate_eligibility"),
        "aggregate_eligibility",
    )
    expected_eligibility = (
        "ELIGIBLE"
        if not is_amendment
        else (
            "AMENDMENT_UNRESOLVED"
            if amends_accession is None
            else "AMENDMENT_RESOLVED"
        )
    )
    if aggregate_eligibility != expected_eligibility:
        raise ResearchProjectionError(
            "Form 4 aggregate eligibility disagrees with amendment lineage"
        )
    record = Form4ResearchEvent(
        event_type=event_type,
        issuer_ticker=ticker,
        issuer_cik=event_issuer_cik,
        accession_number=accession,
        reporting_owners=owners,
        transaction_date=transaction_date,
        available_at=available_at,
        transaction_code=transaction_code,
        shares=_optional_number(event.get("shares"), "shares"),
        price_per_share=_optional_number(event.get("price_per_share"), "price_per_share"),
        approximate_value=_optional_number(event.get("approximate_value"), "approximate_value"),
        direct_or_indirect=_optional_string(event.get("direct_or_indirect"), "direct_or_indirect"),
        shares_owned_following=_optional_number(
            event.get("shares_owned_following"),
            "shares_owned_following",
        ),
        is_amendment=is_amendment,
        amends_accession=amends_accession,
        aggregate_eligibility=aggregate_eligibility,
        plan_10b5_1=_optional_bool(event.get("plan_10b5_1"), "plan_10b5_1"),
    )
    return normalize_form4_research_event(
        record,
        document_hash=document_hash,
        source_uri=source_uri,
        ordinal=ordinal,
    )


def _form4_owners_from_payload(value: object) -> tuple[Form4ReportingOwner, ...]:
    if not isinstance(value, list):
        raise ResearchProjectionError("reporting_owners must be a list")
    owners: list[Form4ReportingOwner] = []
    for raw_owner in value:
        if not isinstance(raw_owner, Mapping):
            raise ResearchProjectionError("reporting owner must be a mapping")
        raw_roles = raw_owner.get("roles")
        if not isinstance(raw_roles, list) or not all(
            isinstance(role, str) and role.strip() for role in raw_roles
        ):
            raise ResearchProjectionError("reporting owner roles are invalid")
        owners.append(
            Form4ReportingOwner(
                cik=_optional_string(raw_owner.get("cik"), "owner.cik"),
                name=_optional_string(raw_owner.get("name"), "owner.name"),
                roles=tuple(role.strip() for role in raw_roles),
                officer_title=_optional_string(
                    raw_owner.get("officer_title"),
                    "owner.officer_title",
                ),
                other_relationship_text=_optional_string(
                    raw_owner.get("other_relationship_text"),
                    "owner.other_relationship_text",
                ),
            )
        )
    return tuple(owners)


def _archived_filing_in_window(
    state: Mapping[str, Any],
    run_definition: ResearchRunDefinition,
) -> bool:
    acceptance = _optional_datetime(state.get("acceptance_at"), "acceptance_at")
    if acceptance is not None:
        return (
            run_definition.hydration_start_time
            <= acceptance
            <= run_definition.hydration_end_time
        )
    raw_date = state.get("filing_date")
    if not isinstance(raw_date, str):
        return True
    try:
        filing_date = date.fromisoformat(raw_date)
    except ValueError:
        return True
    return (
        run_definition.hydration_start_time.date()
        <= filing_date
        <= run_definition.hydration_end_time.date()
    )


def _merge_sec_filing_metadata(
    *,
    archive: SECEDGARArchive,
    accession: str,
    manifest_state: Mapping[str, Any],
) -> dict[str, Any]:
    immutable = archive.read_filing_metadata(accession)
    if not isinstance(immutable, Mapping):
        raise ResearchProjectionError("SEC immutable filing metadata is missing")
    if immutable.get("accession_number") != accession:
        raise ResearchProjectionError("SEC filing accession identity is inconsistent")
    for field_name in (
        "form_type",
        "primary_document",
        "official_document_identity",
        "official_document_url",
        "document_hash",
        "collected_at",
    ):
        manifest_value = manifest_state.get(field_name)
        immutable_value = immutable.get(field_name)
        if manifest_value is not None and manifest_value != immutable_value:
            raise ResearchProjectionError(
                f"SEC manifest and immutable filing metadata disagree on {field_name}"
            )
    merged = dict(immutable)
    merged.update(dict(manifest_state))
    return merged


def _form4_paths(archive: SECEDGARArchive) -> tuple[Path, ...]:
    if not archive.form4.exists():
        return ()
    return tuple(sorted(archive.form4.glob("*.json"), key=lambda path: path.name))


def _read_json_mapping(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchProjectionError("SEC Form 4 archive payload is unreadable") from exc
    if not isinstance(value, dict):
        raise ResearchProjectionError("SEC Form 4 archive payload has invalid shape")
    return value


def _classification_evidence_id(saved: Mapping[str, Any], fallback: str) -> str:
    row = saved.get("ledger_row")
    if isinstance(row, Mapping):
        attempt = row.get("classification_attempt_id")
        if isinstance(attempt, str) and attempt.strip():
            return f"context_event_candidate_{attempt.strip()}"
    return fallback


def _malformed_exclusion(
    evidence_id: str,
    *,
    ticker: str | None = None,
    available_at: datetime | None = None,
) -> ResearchEvidenceExclusion:
    return ResearchEvidenceExclusion(
        evidence_id=evidence_id,
        reason=EvidenceExclusionReason.MALFORMED,
        source=SEC_EVENT_SOURCE,
        tickers=() if ticker is None else (ticker,),
        available_at=available_at,
    )


def _evidence_sort_key(value: ResearchEvidence) -> tuple[object, ...]:
    return (
        datetime.max.replace(tzinfo=UTC) if value.available_at is None else value.available_at,
        value.category.value,
        value.source,
        value.source_record_id,
        value.evidence_id,
    )


def _exclusion_sort_key(value: ResearchEvidenceExclusion) -> tuple[str, str, str]:
    return (value.reason.value, value.source, value.evidence_id)


def _stable_id(prefix: str, payload: object) -> str:
    return f"{prefix}_{_sha256_payload(payload)}"


def _sha256_payload(payload: object) -> str:
    return sha256(to_json_string(payload).encode("utf-8")).hexdigest()


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchProjectionError(f"{field_name} must be a mapping")
    return value


def _json_mapping_copy(value: Mapping[str, object], field_name: str) -> dict[str, object]:
    try:
        # ``dataclasses.replace`` re-validates an already deep-frozen
        # ResearchEvidence payload.  Thaw nested MappingProxyType/tuple values
        # before passing them back through the canonical JSON validator.
        encoded = to_json_string(_deep_thaw_json(value))
        copied = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ResearchProjectionError(f"{field_name} must be JSON safe") from exc
    if not isinstance(copied, dict):
        raise ResearchProjectionError(f"{field_name} must be a mapping")
    return copied


def _deep_freeze_json(value: object) -> object:
    """Return an immutable copy of an already validated JSON value."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _deep_freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze_json(item) for item in value)
    return value


def _deep_thaw_json(value: object) -> object:
    """Return a JSON-serializable copy of a frozen fingerprint payload."""

    if isinstance(value, Mapping):
        return {str(key): _deep_thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw_json(item) for item in value]
    return value


def _json_string_list(value: object, field_name: str) -> list[str]:
    if not isinstance(value, str):
        raise ResearchProjectionError(f"{field_name} must be JSON text")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ResearchProjectionError(f"{field_name} is invalid JSON") from exc
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) and item.strip() for item in parsed
    ):
        raise ResearchProjectionError(f"{field_name} must contain strings")
    return [_normalize_symbol(item, field_name) for item in parsed]


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchProjectionError(f"{field_name} must be a non-empty string")
    return value.strip()


def _required_sha256(value: object, field_name: str) -> str:
    text = _required_string(value, field_name)
    if _SHA256_RE.fullmatch(text) is None:
        raise ResearchProjectionError(
            f"{field_name} must be a lowercase SHA-256 hex string"
        )
    return text


def _normalize_symbol(value: object, field_name: str) -> str:
    return _required_string(value, field_name).upper()


def _optional_symbol(value: object) -> str | None:
    if value is None:
        return None
    try:
        return _normalize_symbol(value, "symbol")
    except ResearchProjectionError:
        return None


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, field_name)


def _required_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ResearchProjectionError(f"{field_name} must be bool")
    return value


def _optional_bool(value: object, field_name: str) -> bool | None:
    if value is None:
        return None
    return _required_bool(value, field_name)


def _optional_number(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchProjectionError(f"{field_name} must be numeric")
    converted = float(value)
    if not math.isfinite(converted):
        raise ResearchProjectionError(f"{field_name} must be finite")
    return converted


def _required_unit_interval(value: object, field_name: str) -> float:
    converted = _optional_number(value, field_name)
    if converted is None or not 0.0 <= converted <= 1.0:
        raise ResearchProjectionError(f"{field_name} must be between 0 and 1")
    return converted


def _optional_date(value: object, field_name: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ResearchProjectionError(f"{field_name} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ResearchProjectionError(f"{field_name} is invalid") from exc


def _aware_datetime(value: object, field_name: str) -> datetime:
    try:
        return ensure_timezone_aware_utc(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ResearchProjectionError(
            f"{field_name} must be a timezone-aware datetime"
        ) from exc


def _optional_datetime(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ResearchProjectionError(f"{field_name} is invalid") from exc
        return _aware_datetime(parsed, field_name)
    return _aware_datetime(value, field_name)


def _required_datetime(value: object, field_name: str) -> datetime:
    converted = _optional_datetime(value, field_name)
    if converted is None:
        raise ResearchProjectionError(f"{field_name} must be provided")
    return converted


__all__ = [
    "EvidenceCategory",
    "EvidenceExclusionReason",
    "ResearchAvailabilityMode",
    "ResearchClassificationProfile",
    "ResearchCoverageAssessment",
    "ResearchEvidenceCapacityError",
    "ResearchEvidenceExclusion",
    "ResearchEvidenceIndex",
    "ResearchEvidenceRelationship",
    "ResearchEvidenceSelection",
    "ResearchLifecycleRevision",
    "ResearchProjectionError",
    "ResearchRunDefinition",
    "ResearchSourceClassificationProfile",
    "ResearchSourceCoverageProfile",
    "build_shadow_context_fingerprint",
    "hydrate_combined_research_evidence",
    "hydrate_external_research_evidence",
    "hydrate_sec_research_evidence",
    "normalize_context_ai_event",
    "normalize_context_flag",
    "normalize_form4_research_event",
]
