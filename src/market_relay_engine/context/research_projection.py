"""Research-only event hydration and leak-free in-memory selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping

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
from market_relay_engine.contracts.context import (
    ContextAIEvent,
    ContextClassificationEventType,
    ContextFlag,
    ContextRiskLevel,
    ContextUrgency,
    DeterministicContextEventType,
)


SEC_EVENT_SOURCE = "sec_edgar"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_POLICY_KEY_RE = re.compile(
    r"(?:AI_EVENT_TYPE|DETERMINISTIC_EVENT_TYPE|FLAG_TYPE):[A-Z0-9_]+"
)
_STRUCTURED_SOURCE_NAMES = frozenset(KNOWN_SOURCE_CLASSIFICATION)


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

    def to_fingerprint_payload(self) -> dict[str, object]:
        return {
            "ticker_universe": list(self.ticker_universe),
            "event_sources": list(self.event_sources),
            "evidence_categories": [value.value for value in self.evidence_categories],
            "hydration_start_time": to_utc_iso(self.hydration_start_time),
            "hydration_end_time": to_utc_iso(self.hydration_end_time),
            "classification_profile": self.classification_profile.to_fingerprint_payload(),
            "max_age_without_valid_until_seconds": self.max_age_without_valid_until.total_seconds(),
            "selection_policy_version": self.selection_policy_version,
        }


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
    global_relevance: bool = False
    available_at: datetime | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    fingerprint_payload: Mapping[str, object] = field(default_factory=dict)
    lineage_ids: tuple[str, ...] = field(default_factory=tuple)
    policy_eligible: bool = True

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
        if not isinstance(self.global_relevance, bool):
            raise ResearchProjectionError("global_relevance must be bool")
        if self.global_relevance and (tickers or sector is not None):
            raise ResearchProjectionError(
                "global evidence cannot also declare ticker or sector scope"
            )
        if not self.global_relevance and not tickers and sector is None:
            raise ResearchProjectionError(
                "evidence must declare ticker, sector, or global relevance"
            )
        object.__setattr__(self, "tickers", tickers)
        object.__setattr__(self, "sector", sector)
        for name in ("available_at", "valid_from", "valid_until"):
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
        if not isinstance(self.policy_eligible, bool):
            raise ResearchProjectionError("policy_eligible must be bool")

    def to_fingerprint_payload(self) -> dict[str, object]:
        return {
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


@dataclass(frozen=True, kw_only=True)
class ResearchEvidenceExclusion:
    evidence_id: str
    reason: EvidenceExclusionReason
    source: str
    tickers: tuple[str, ...] = field(default_factory=tuple)
    sector: str | None = None
    global_relevance: bool = False
    available_at: datetime | None = None

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
        if not isinstance(self.global_relevance, bool):
            raise ResearchProjectionError("global_relevance must be bool")
        if self.global_relevance and (self.tickers or self.sector is not None):
            raise ResearchProjectionError(
                "global exclusion cannot also declare ticker or sector scope"
            )
        object.__setattr__(
            self,
            "available_at",
            (
                None
                if self.available_at is None
                else _aware_datetime(self.available_at, "available_at")
            ),
        )


@dataclass(frozen=True, kw_only=True)
class ResearchEvidenceSelection:
    decision_time: datetime
    ticker: str
    sector: str | None
    selected_evidence: tuple[ResearchEvidence, ...]
    exclusions: tuple[ResearchEvidenceExclusion, ...]
    run_definition: ResearchRunDefinition

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
        if not isinstance(self.run_definition, ResearchRunDefinition):
            raise ResearchProjectionError("run_definition has an invalid type")


@dataclass(frozen=True, kw_only=True)
class ResearchEvidenceIndex:
    """Immutable bounded event index hydrated before any signal evaluation."""

    run_definition: ResearchRunDefinition
    evidence: tuple[ResearchEvidence, ...]
    hydration_exclusions: tuple[ResearchEvidenceExclusion, ...]
    attempted_record_count: int

    @classmethod
    def build(
        cls,
        *,
        run_definition: ResearchRunDefinition,
        evidence: Iterable[ResearchEvidence],
        hydration_exclusions: Iterable[ResearchEvidenceExclusion] = (),
        attempted_record_count: int | None = None,
    ) -> "ResearchEvidenceIndex":
        if not isinstance(run_definition, ResearchRunDefinition):
            raise ResearchProjectionError("run_definition has an invalid type")
        values = tuple(evidence)
        exclusions = tuple(hydration_exclusions)
        if not all(isinstance(value, ResearchEvidence) for value in values):
            raise ResearchProjectionError("evidence must contain ResearchEvidence values")
        if not all(isinstance(value, ResearchEvidenceExclusion) for value in exclusions):
            raise ResearchProjectionError(
                "hydration_exclusions must contain ResearchEvidenceExclusion values"
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
            source_record_key = (value.source, value.source_record_id)
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
        return cls(
            run_definition=run_definition,
            evidence=ordered,
            hydration_exclusions=ordered_exclusions,
            attempted_record_count=attempted,
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
        for value in self.evidence:
            reason = _selection_exclusion_reason(
                value,
                ticker=ticker,
                sector=decision_context.ticker_sector,
                decision_time=decision_time,
                max_age=self.run_definition.max_age_without_valid_until,
            )
            if reason is None:
                selected.append(value)
            else:
                exclusions.append(
                    ResearchEvidenceExclusion(
                        evidence_id=value.evidence_id,
                        reason=reason,
                        source=value.source,
                    )
                )
        return ResearchEvidenceSelection(
            decision_time=decision_time,
            ticker=ticker,
            sector=decision_context.ticker_sector,
            selected_evidence=tuple(selected),
            exclusions=tuple(sorted(exclusions, key=_exclusion_sort_key)),
            run_definition=self.run_definition,
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
    return ResearchEvidence(
        evidence_id=event.context_event_id,
        category=EvidenceCategory.AI_EVENT,
        policy_match_key=f"AI_EVENT_TYPE:{event.event_type.value}",
        source=event.source,
        source_record_id=event.source_id,
        tickers=tuple(event.affected_tickers),
        sector=event.affected_sector,
        global_relevance=not event.affected_tickers and event.affected_sector is None,
        available_at=event.available_at,
        valid_from=event.valid_from,
        valid_until=event.valid_until,
        fingerprint_payload=payload,
        lineage_ids=tuple(
            value
            for value in (
                event.raw_input_id,
                event.source_document_id,
                event.classification_request_id,
                event.classification_attempt_id,
                event.validation_result_id,
            )
            if value is not None
        ),
        policy_eligible=policy_eligible,
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
    return _sha256_payload(payload)


def _selection_exclusion_reason(
    value: ResearchEvidence,
    *,
    ticker: str,
    sector: str | None,
    decision_time: datetime,
    max_age: timedelta,
) -> EvidenceExclusionReason | None:
    if not value.policy_eligible:
        return EvidenceExclusionReason.POLICY_INELIGIBLE
    if value.available_at is None:
        return EvidenceExclusionReason.MISSING_AVAILABILITY
    if not _scope_matches(value, ticker=ticker, sector=sector):
        return EvidenceExclusionReason.SCOPE_MISMATCH
    if value.available_at > decision_time:
        return EvidenceExclusionReason.FUTURE
    if value.valid_from is not None and value.valid_from > decision_time:
        return EvidenceExclusionReason.FUTURE
    if value.valid_until is not None and decision_time > value.valid_until:
        return EvidenceExclusionReason.EXPIRED
    if value.valid_until is None and decision_time - value.available_at > max_age:
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
    has_scope = value.global_relevance or bool(value.tickers) or value.sector is not None
    if not has_scope:
        return False
    if not (
        value.global_relevance
        or ticker in value.tickers
        or (sector is not None and value.sector == sector)
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
    if value.global_relevance:
        return True
    if ticker in value.tickers:
        return True
    return sector is not None and value.sector == sector


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
        encoded = to_json_string(dict(value))
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
    "ResearchClassificationProfile",
    "ResearchEvidenceCapacityError",
    "ResearchEvidenceExclusion",
    "ResearchEvidenceIndex",
    "ResearchEvidenceSelection",
    "ResearchProjectionError",
    "ResearchRunDefinition",
    "build_shadow_context_fingerprint",
    "hydrate_sec_research_evidence",
    "normalize_context_ai_event",
    "normalize_context_flag",
    "normalize_form4_research_event",
]
