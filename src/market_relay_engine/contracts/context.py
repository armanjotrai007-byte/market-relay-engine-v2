"""Context contract shapes.

The Phase 7 contracts in this module are provider-neutral data records.  They do
not authorize, block, resize, delay, or submit trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import json
import math
import re
from typing import Any, TypeVar

from market_relay_engine.common.ids import new_record_id
from market_relay_engine.common.serialization import to_json_string
from market_relay_engine.contracts.base import (
    DEFAULT_SCHEMA_VERSION,
    optional_utc_datetime,
    require_non_empty_string,
    require_optional_non_empty_string,
    utc_datetime,
)


class ContextClassificationEventType(str, Enum):
    """Event vocabulary permitted at the AI-classification boundary."""

    UNKNOWN = "UNKNOWN"
    OTHER = "OTHER"
    GOVERNMENT_CONTRACT = "GOVERNMENT_CONTRACT"
    REGULATORY_POLICY = "REGULATORY_POLICY"
    GEOPOLITICAL = "GEOPOLITICAL"
    SUPPLY_DISRUPTION = "SUPPLY_DISRUPTION"
    EARNINGS_GUIDANCE = "EARNINGS_GUIDANCE"
    LEGAL = "LEGAL"
    CYBERSECURITY = "CYBERSECURITY"
    MANAGEMENT_CHANGE = "MANAGEMENT_CHANGE"
    SOCIAL_POLITICAL_STATEMENT = "SOCIAL_POLITICAL_STATEMENT"
    SEC_8K_MATERIAL_AGREEMENT = "SEC_8K_MATERIAL_AGREEMENT"
    SEC_8K_TERMINATION_OF_MATERIAL_AGREEMENT = (
        "SEC_8K_TERMINATION_OF_MATERIAL_AGREEMENT"
    )
    SEC_8K_BANKRUPTCY = "SEC_8K_BANKRUPTCY"
    SEC_8K_CYBERSECURITY_INCIDENT = "SEC_8K_CYBERSECURITY_INCIDENT"
    SEC_8K_ACQUISITION = "SEC_8K_ACQUISITION"
    SEC_8K_RESULTS = "SEC_8K_RESULTS"
    SEC_8K_DIRECT_FINANCIAL_OBLIGATION = "SEC_8K_DIRECT_FINANCIAL_OBLIGATION"
    SEC_8K_DEBT_DEFAULT = "SEC_8K_DEBT_DEFAULT"
    SEC_8K_EXIT_OR_DISPOSAL_COSTS = "SEC_8K_EXIT_OR_DISPOSAL_COSTS"
    SEC_8K_MATERIAL_IMPAIRMENT = "SEC_8K_MATERIAL_IMPAIRMENT"
    SEC_8K_DELISTING = "SEC_8K_DELISTING"
    SEC_8K_AUDITOR_CHANGE = "SEC_8K_AUDITOR_CHANGE"
    SEC_8K_NON_RELIANCE = "SEC_8K_NON_RELIANCE"
    SEC_8K_CHANGE_IN_CONTROL = "SEC_8K_CHANGE_IN_CONTROL"
    SEC_8K_EXECUTIVE_OR_DIRECTOR_CHANGE = "SEC_8K_EXECUTIVE_OR_DIRECTOR_CHANGE"
    SEC_8K_REGULATION_FD = "SEC_8K_REGULATION_FD"
    SEC_8K_OTHER_EVENT = "SEC_8K_OTHER_EVENT"


class DeterministicContextEventType(str, Enum):
    """Event vocabulary reserved for deterministic parsers, never Gemini."""

    SEC_FORM4_PURCHASE = "SEC_FORM4_PURCHASE"
    SEC_FORM4_SALE = "SEC_FORM4_SALE"


class ContextRiskLevel(str, Enum):
    """Bounded research-only context risk levels."""

    UNKNOWN = "UNKNOWN"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ContextUrgency(str, Enum):
    """Bounded research-only context urgency levels."""

    UNKNOWN = "UNKNOWN"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ContextClassificationStatus(str, Enum):
    """Outcome of one logical classification attempt."""

    VALID = "VALID"
    ABSTAINED = "ABSTAINED"
    VALIDATION_REJECTED = "VALIDATION_REJECTED"
    PROVIDER_FAILED = "PROVIDER_FAILED"


class ShadowContextAction(str, Enum):
    """Hypothetical action emitted only by a future shadow evaluator."""

    NO_CHANGE = "NO_CHANGE"
    BLOCK = "BLOCK"
    REDUCE_SIZE = "REDUCE_SIZE"
    DELAY = "DELAY"
    WARN_ONLY = "WARN_ONLY"


_EnumT = TypeVar("_EnumT", bound=Enum)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _require_enum(value: object, enum_type: type[_EnumT], field_name: str) -> _EnumT:
    if not isinstance(value, enum_type):
        raise TypeError(f"{field_name} must be a {enum_type.__name__}")
    return value


def _optional_enum(
    value: object | None,
    enum_type: type[_EnumT],
    field_name: str,
) -> _EnumT | None:
    if value is None:
        return None
    return _require_enum(value, enum_type, field_name)


def _copy_string_list(value: object, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list")
    copied: list[str] = []
    for item in value:
        require_non_empty_string(item, f"{field_name} item")
        copied.append(item)
    return copied


def _require_sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(
            f"{field_name} must be a 64-character lowercase SHA-256 hex string"
        )
    return value


def _optional_sha256(value: object | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_sha256(value, field_name)


def _optional_unit_interval(value: object | None, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a finite number")
    converted = float(value)
    if not math.isfinite(converted) or not 0.0 <= converted <= 1.0:
        raise ValueError(f"{field_name} must be between 0 and 1 inclusive")
    return converted


def _non_negative_finite(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a finite number")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0.0:
        raise ValueError(f"{field_name} must be non-negative and finite")
    return converted


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


def _validate_common_record_fields(
    *,
    schema_version: str,
    trace_id: str | None,
) -> None:
    require_non_empty_string(schema_version, "schema_version")
    require_optional_non_empty_string(trace_id, "trace_id")


@dataclass(frozen=True, kw_only=True)
class ContextRawInput:
    """Trusted metadata envelope for one collected raw context input.

    Raw source text is deliberately not part of this durable metadata contract.
    """

    source: str
    source_type: str
    source_locator: str
    raw_input_hash: str
    affected_tickers: list[str]
    collected_at: datetime
    raw_input_id: str = field(default_factory=lambda: new_record_id("raw_input"))
    source_platform: str | None = None
    source_uri: str | None = None
    source_published_at: datetime | None = None
    source_updated_at: datetime | None = None
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        require_non_empty_string(self.raw_input_id, "raw_input_id")
        require_non_empty_string(self.source, "source")
        require_non_empty_string(self.source_type, "source_type")
        require_non_empty_string(self.source_locator, "source_locator")
        require_optional_non_empty_string(self.source_platform, "source_platform")
        require_optional_non_empty_string(self.source_uri, "source_uri")
        object.__setattr__(
            self, "raw_input_hash", _require_sha256(self.raw_input_hash, "raw_input_hash")
        )
        object.__setattr__(
            self,
            "affected_tickers",
            _copy_string_list(self.affected_tickers, "affected_tickers"),
        )
        object.__setattr__(self, "collected_at", utc_datetime(self.collected_at))
        object.__setattr__(
            self,
            "source_published_at",
            optional_utc_datetime(self.source_published_at),
        )
        object.__setattr__(
            self,
            "source_updated_at",
            optional_utc_datetime(self.source_updated_at),
        )
        _validate_common_record_fields(
            schema_version=self.schema_version,
            trace_id=self.trace_id,
        )


@dataclass(frozen=True, kw_only=True)
class ContextSourceDocument:
    """Normalized source-document metadata linked to a trusted raw input."""

    raw_input_id: str
    source: str
    source_type: str
    source_locator: str
    raw_input_hash: str
    document_hash: str
    affected_tickers: list[str]
    collected_at: datetime
    normalized_at: datetime
    source_document_id: str = field(
        default_factory=lambda: new_record_id("source_document")
    )
    source_platform: str | None = None
    source_uri: str | None = None
    source_published_at: datetime | None = None
    source_updated_at: datetime | None = None
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("source_document_id", "raw_input_id", "source", "source_type", "source_locator"):
            require_non_empty_string(getattr(self, field_name), field_name)
        require_optional_non_empty_string(self.source_platform, "source_platform")
        require_optional_non_empty_string(self.source_uri, "source_uri")
        object.__setattr__(
            self, "raw_input_hash", _require_sha256(self.raw_input_hash, "raw_input_hash")
        )
        object.__setattr__(
            self, "document_hash", _require_sha256(self.document_hash, "document_hash")
        )
        object.__setattr__(
            self,
            "affected_tickers",
            _copy_string_list(self.affected_tickers, "affected_tickers"),
        )
        object.__setattr__(self, "collected_at", utc_datetime(self.collected_at))
        object.__setattr__(self, "normalized_at", utc_datetime(self.normalized_at))
        object.__setattr__(
            self,
            "source_published_at",
            optional_utc_datetime(self.source_published_at),
        )
        object.__setattr__(
            self,
            "source_updated_at",
            optional_utc_datetime(self.source_updated_at),
        )
        _validate_common_record_fields(
            schema_version=self.schema_version,
            trace_id=self.trace_id,
        )


@dataclass(frozen=True, kw_only=True)
class ContextClassificationRequest:
    """Bounded in-memory provider request assembled from trusted metadata."""

    requested_at: datetime
    source: str
    source_type: str
    source_locator: str
    raw_input_id: str
    source_document_id: str
    raw_input_hash: str
    document_hash: str
    affected_tickers: list[str]
    input_text: str
    prompt_version: str
    collected_at: datetime
    normalized_at: datetime
    classification_request_id: str = field(
        default_factory=lambda: new_record_id("classification_request")
    )
    source_platform: str | None = None
    source_uri: str | None = None
    source_published_at: datetime | None = None
    source_updated_at: datetime | None = None
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "classification_request_id",
            "raw_input_id",
            "source_document_id",
            "source",
            "source_type",
            "source_locator",
            "input_text",
            "prompt_version",
        ):
            require_non_empty_string(getattr(self, field_name), field_name)
        require_optional_non_empty_string(self.source_platform, "source_platform")
        require_optional_non_empty_string(self.source_uri, "source_uri")
        object.__setattr__(
            self, "raw_input_hash", _require_sha256(self.raw_input_hash, "raw_input_hash")
        )
        object.__setattr__(
            self, "document_hash", _require_sha256(self.document_hash, "document_hash")
        )
        object.__setattr__(
            self,
            "affected_tickers",
            _copy_string_list(self.affected_tickers, "affected_tickers"),
        )
        object.__setattr__(self, "requested_at", utc_datetime(self.requested_at))
        object.__setattr__(self, "collected_at", utc_datetime(self.collected_at))
        object.__setattr__(self, "normalized_at", utc_datetime(self.normalized_at))
        object.__setattr__(
            self,
            "source_published_at",
            optional_utc_datetime(self.source_published_at),
        )
        object.__setattr__(
            self,
            "source_updated_at",
            optional_utc_datetime(self.source_updated_at),
        )
        _validate_common_record_fields(
            schema_version=self.schema_version,
            trace_id=self.trace_id,
        )


@dataclass(frozen=True, kw_only=True)
class ContextClassificationResponse:
    """Safe, bounded result of one logical classification attempt."""

    classification_request_id: str
    classified_at: datetime
    provider: str
    model_version: str
    prompt_version: str
    status: ContextClassificationStatus
    provider_latency_ms: float
    classification_attempt_id: str = field(
        default_factory=lambda: new_record_id("classification_attempt")
    )
    event_type: ContextClassificationEventType = ContextClassificationEventType.UNKNOWN
    risk_level: ContextRiskLevel = ContextRiskLevel.UNKNOWN
    urgency: ContextUrgency = ContextUrgency.UNKNOWN
    confidence: float | None = None
    summary: str | None = None
    safe_failure_category: str | None = None
    safe_failure_summary: str | None = None
    provider_request_count: int = 0
    retry_count: int = 0
    deduplicated: bool = False
    reused_classification_attempt_id: str | None = None
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "classification_attempt_id",
            "classification_request_id",
            "provider",
            "model_version",
            "prompt_version",
        ):
            require_non_empty_string(getattr(self, field_name), field_name)
        _require_enum(self.status, ContextClassificationStatus, "status")
        _require_enum(self.event_type, ContextClassificationEventType, "event_type")
        _require_enum(self.risk_level, ContextRiskLevel, "risk_level")
        _require_enum(self.urgency, ContextUrgency, "urgency")
        require_optional_non_empty_string(self.summary, "summary")
        require_optional_non_empty_string(
            self.safe_failure_category,
            "safe_failure_category",
        )
        require_optional_non_empty_string(
            self.safe_failure_summary,
            "safe_failure_summary",
        )
        require_optional_non_empty_string(
            self.reused_classification_attempt_id,
            "reused_classification_attempt_id",
        )
        _non_negative_int(self.provider_request_count, "provider_request_count")
        _non_negative_int(self.retry_count, "retry_count")
        if not isinstance(self.deduplicated, bool):
            raise TypeError("deduplicated must be bool")
        if self.retry_count != max(self.provider_request_count - 1, 0):
            raise ValueError(
                "retry_count must equal provider_request_count minus the original request"
            )
        if self.deduplicated:
            if self.provider_request_count != 0 or self.retry_count != 0:
                raise ValueError(
                    "deduplicated responses cannot include provider requests or retries"
                )
            if self.reused_classification_attempt_id is None:
                raise ValueError(
                    "deduplicated responses require reused_classification_attempt_id"
                )
        elif self.reused_classification_attempt_id is not None:
            raise ValueError(
                "reused_classification_attempt_id is only valid for deduplicated responses"
            )
        object.__setattr__(self, "classified_at", utc_datetime(self.classified_at))
        object.__setattr__(
            self,
            "provider_latency_ms",
            _non_negative_finite(self.provider_latency_ms, "provider_latency_ms"),
        )
        object.__setattr__(
            self,
            "confidence",
            _optional_unit_interval(self.confidence, "confidence"),
        )
        self._validate_status_shape()
        _validate_common_record_fields(
            schema_version=self.schema_version,
            trace_id=self.trace_id,
        )

    def _validate_status_shape(self) -> None:
        classification_is_unknown = (
            self.event_type is ContextClassificationEventType.UNKNOWN
            and self.risk_level is ContextRiskLevel.UNKNOWN
            and self.urgency is ContextUrgency.UNKNOWN
        )
        if self.status is ContextClassificationStatus.VALID:
            if (
                self.event_type is ContextClassificationEventType.UNKNOWN
                or self.risk_level is ContextRiskLevel.UNKNOWN
                or self.urgency is ContextUrgency.UNKNOWN
                or self.confidence is None
                or self.summary is None
            ):
                raise ValueError("VALID responses require complete classification fields")
            if self.safe_failure_category is not None or self.safe_failure_summary is not None:
                raise ValueError("VALID responses cannot include provider failure fields")
            return
        if not classification_is_unknown or self.confidence is not None:
            raise ValueError(f"{self.status.value} responses cannot include classification fields")
        if self.status is ContextClassificationStatus.ABSTAINED:
            if self.safe_failure_category is not None or self.safe_failure_summary is not None:
                raise ValueError("ABSTAINED responses cannot include provider failure fields")
            return
        if self.status is ContextClassificationStatus.VALIDATION_REJECTED:
            if (
                self.summary is not None
                or self.safe_failure_category is not None
                or self.safe_failure_summary is not None
            ):
                raise ValueError("VALIDATION_REJECTED responses cannot include result payloads")
            return
        if self.status is ContextClassificationStatus.PROVIDER_FAILED:
            if self.summary is not None or self.safe_failure_category is None:
                raise ValueError(
                    "PROVIDER_FAILED responses require a safe failure category and no summary"
                )
            return
        raise AssertionError("unhandled classification status")


@dataclass(frozen=True, kw_only=True)
class ContextValidationResult:
    """Validation outcome for a provider response, with only safe detail."""

    classification_request_id: str
    classification_attempt_id: str
    validation_outcome: bool
    reason_codes: list[str]
    validator_version: str
    validated_at: datetime
    validation_result_id: str = field(
        default_factory=lambda: new_record_id("validation_result")
    )
    safe_detail: str | None = None
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "validation_result_id",
            "classification_request_id",
            "classification_attempt_id",
            "validator_version",
        ):
            require_non_empty_string(getattr(self, field_name), field_name)
        if not isinstance(self.validation_outcome, bool):
            raise TypeError("validation_outcome must be bool")
        reasons = _copy_string_list(self.reason_codes, "reason_codes")
        if self.validation_outcome and reasons:
            raise ValueError("successful validation cannot include reason codes")
        if not self.validation_outcome and not reasons:
            raise ValueError("failed validation requires at least one reason code")
        object.__setattr__(self, "reason_codes", reasons)
        require_optional_non_empty_string(self.safe_detail, "safe_detail")
        object.__setattr__(self, "validated_at", utc_datetime(self.validated_at))
        _validate_common_record_fields(
            schema_version=self.schema_version,
            trace_id=self.trace_id,
        )


@dataclass(frozen=True, kw_only=True)
class ContextIndicatorSnapshot:
    """Structured context indicator snapshot for future risk inputs."""

    snapshot_time: datetime
    source: str
    ticker_or_sector: str
    indicator_name: str
    value: Any
    context_indicator_id: str = field(
        default_factory=lambda: new_record_id("context_indicator")
    )
    window: str | None = None
    units: str | None = None
    freshness_seconds: float | None = None
    source_event_time: datetime | None = None
    details: dict[str, object] = field(default_factory=dict)
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_time", utc_datetime(self.snapshot_time))
        object.__setattr__(
            self,
            "source_event_time",
            optional_utc_datetime(self.source_event_time),
        )
        require_non_empty_string(self.context_indicator_id, "context_indicator_id")
        require_optional_non_empty_string(self.trace_id, "trace_id")
        if not isinstance(self.details, dict):
            raise TypeError("details must be a dictionary")
        copied_details = json.loads(to_json_string(self.details))
        if not isinstance(copied_details, dict):
            raise TypeError("details must be a dictionary")
        object.__setattr__(self, "details", copied_details)
        from market_relay_engine.context.provenance import (
            validate_snapshot_provenance_alignment,
        )

        validate_snapshot_provenance_alignment(self)


@dataclass(frozen=True, kw_only=True)
class ContextAIEvent:
    """Validated research-only AI classification event.

    This record is non-authoritative and cannot directly affect a real risk
    decision.  Deterministic Form 4 events use a separate type boundary.
    """

    event_time: datetime
    source: str
    source_id: str
    affected_tickers: list[str]
    event_type: ContextClassificationEventType
    context_event_id: str = field(default_factory=lambda: new_record_id("context_event"))
    affected_sector: str | None = None
    sentiment: str | None = None
    urgency: ContextUrgency | None = None
    risk_level: ContextRiskLevel | None = None
    confidence: float | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    summary: str | None = None
    prompt_version: str | None = None
    model_version: str | None = None
    raw_input_hash: str | None = None
    raw_input_id: str | None = None
    source_document_id: str | None = None
    classification_request_id: str | None = None
    classification_attempt_id: str | None = None
    validation_result_id: str | None = None
    source_type: str | None = None
    source_platform: str | None = None
    source_uri: str | None = None
    source_locator: str | None = None
    document_hash: str | None = None
    source_published_at: datetime | None = None
    source_updated_at: datetime | None = None
    collected_at: datetime | None = None
    normalized_at: datetime | None = None
    classified_at: datetime | None = None
    available_at: datetime | None = None
    validated_at: datetime | None = None
    provider: str | None = None
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_time", utc_datetime(self.event_time))
        for field_name in (
            "valid_from",
            "valid_until",
            "source_published_at",
            "source_updated_at",
            "collected_at",
            "normalized_at",
            "classified_at",
            "available_at",
            "validated_at",
        ):
            object.__setattr__(
                self,
                field_name,
                optional_utc_datetime(getattr(self, field_name)),
            )
        for field_name in ("context_event_id", "source", "source_id"):
            require_non_empty_string(getattr(self, field_name), field_name)
        for field_name in (
            "affected_sector",
            "sentiment",
            "summary",
            "prompt_version",
            "model_version",
            "raw_input_id",
            "source_document_id",
            "classification_request_id",
            "classification_attempt_id",
            "validation_result_id",
            "source_type",
            "source_platform",
            "source_uri",
            "source_locator",
            "provider",
        ):
            require_optional_non_empty_string(getattr(self, field_name), field_name)
        _require_enum(self.event_type, ContextClassificationEventType, "event_type")
        _optional_enum(self.urgency, ContextUrgency, "urgency")
        _optional_enum(self.risk_level, ContextRiskLevel, "risk_level")
        object.__setattr__(
            self,
            "affected_tickers",
            _copy_string_list(self.affected_tickers, "affected_tickers"),
        )
        object.__setattr__(
            self,
            "confidence",
            _optional_unit_interval(self.confidence, "confidence"),
        )
        object.__setattr__(
            self,
            "raw_input_hash",
            _optional_sha256(self.raw_input_hash, "raw_input_hash"),
        )
        object.__setattr__(
            self,
            "document_hash",
            _optional_sha256(self.document_hash, "document_hash"),
        )
        _validate_common_record_fields(
            schema_version=self.schema_version,
            trace_id=self.trace_id,
        )


@dataclass(frozen=True, kw_only=True)
class ContextFlag:
    """Structured flag compatible with the existing deterministic risk adapter.

    ``available_at`` is the earliest trusted, demonstrable public-availability
    time of the underlying source fact.  It is not collection time, source event
    time, or an event-window activation time.
    """

    event_time: datetime
    source: str
    flag_type: str
    severity: str
    context_flag_id: str = field(default_factory=lambda: new_record_id("context_flag"))
    ticker: str | None = None
    sector: str | None = None
    confidence: float | None = None
    valid_until: datetime | None = None
    context_event_id: str | None = None
    raw_input_id: str | None = None
    source_document_id: str | None = None
    classification_request_id: str | None = None
    classification_attempt_id: str | None = None
    validation_result_id: str | None = None
    source_type: str | None = None
    source_id: str | None = None
    source_platform: str | None = None
    source_uri: str | None = None
    source_locator: str | None = None
    document_hash: str | None = None
    raw_input_hash: str | None = None
    valid_from: datetime | None = None
    available_at: datetime | None = None
    validated_at: datetime | None = None
    reason_codes: list[str] = field(default_factory=list)
    summary: str | None = None
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_time", utc_datetime(self.event_time))
        for field_name in ("valid_until", "valid_from", "available_at", "validated_at"):
            object.__setattr__(
                self,
                field_name,
                optional_utc_datetime(getattr(self, field_name)),
            )
        for field_name in ("context_flag_id", "source", "flag_type", "severity"):
            require_non_empty_string(getattr(self, field_name), field_name)
        for field_name in (
            "ticker",
            "sector",
            "context_event_id",
            "raw_input_id",
            "source_document_id",
            "classification_request_id",
            "classification_attempt_id",
            "validation_result_id",
            "source_type",
            "source_id",
            "source_platform",
            "source_uri",
            "source_locator",
            "summary",
        ):
            require_optional_non_empty_string(getattr(self, field_name), field_name)
        object.__setattr__(
            self,
            "confidence",
            _optional_unit_interval(self.confidence, "confidence"),
        )
        object.__setattr__(
            self,
            "document_hash",
            _optional_sha256(self.document_hash, "document_hash"),
        )
        object.__setattr__(
            self,
            "raw_input_hash",
            _optional_sha256(self.raw_input_hash, "raw_input_hash"),
        )
        object.__setattr__(
            self,
            "reason_codes",
            _copy_string_list(self.reason_codes, "reason_codes"),
        )
        _validate_common_record_fields(
            schema_version=self.schema_version,
            trace_id=self.trace_id,
        )


@dataclass(frozen=True, kw_only=True)
class ShadowContextPolicyEvaluation:
    """Research-only record of a hypothetical context-policy result."""

    model_signal_id: str
    decision_evaluation_time: datetime
    shadow_context_fingerprint: str
    policy_version: str
    policy_config_hash: str
    hypothetical_action: ShadowContextAction
    shadow_evaluation_id: str = field(
        default_factory=lambda: new_record_id("shadow_evaluation")
    )
    risk_decision_id: str | None = None
    matched_context_event_ids: list[str] = field(default_factory=list)
    matched_context_flag_ids: list[str] = field(default_factory=list)
    proposed_size_factor: float | None = None
    reason_codes: list[str] = field(default_factory=list)
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "shadow_evaluation_id",
            "model_signal_id",
            "policy_version",
        ):
            require_non_empty_string(getattr(self, field_name), field_name)
        require_optional_non_empty_string(self.risk_decision_id, "risk_decision_id")
        _require_enum(self.hypothetical_action, ShadowContextAction, "hypothetical_action")
        object.__setattr__(
            self,
            "decision_evaluation_time",
            utc_datetime(self.decision_evaluation_time),
        )
        object.__setattr__(
            self,
            "shadow_context_fingerprint",
            _require_sha256(
                self.shadow_context_fingerprint,
                "shadow_context_fingerprint",
            ),
        )
        object.__setattr__(
            self,
            "policy_config_hash",
            _require_sha256(self.policy_config_hash, "policy_config_hash"),
        )
        for field_name in (
            "matched_context_event_ids",
            "matched_context_flag_ids",
            "reason_codes",
        ):
            object.__setattr__(
                self,
                field_name,
                _copy_string_list(getattr(self, field_name), field_name),
            )
        if self.hypothetical_action is ShadowContextAction.REDUCE_SIZE:
            factor = _optional_unit_interval(
                self.proposed_size_factor,
                "proposed_size_factor",
            )
            if factor is None or factor <= 0.0:
                raise ValueError(
                    "REDUCE_SIZE requires proposed_size_factor greater than 0 and at most 1"
                )
            object.__setattr__(self, "proposed_size_factor", factor)
        elif self.proposed_size_factor is not None:
            raise ValueError("proposed_size_factor is only valid for REDUCE_SIZE")
        _validate_common_record_fields(
            schema_version=self.schema_version,
            trace_id=self.trace_id,
        )


@dataclass(frozen=True, kw_only=True)
class ContextStateSnapshot:
    """Context state snapshot consumed by the deterministic risk gate."""

    snapshot_time: datetime
    ticker: str
    context_snapshot_id: str = field(
        default_factory=lambda: new_record_id("context_snapshot")
    )
    sector: str | None = None
    active_indicator_ids: list[str] = field(default_factory=list)
    active_context_event_ids: list[str] = field(default_factory=list)
    active_context_flag_ids: list[str] = field(default_factory=list)
    context_summary: dict[str, Any] = field(default_factory=dict)
    highest_severity: str | None = None
    risk_level: str | None = None
    valid_until: datetime | None = None
    schema_version: str = DEFAULT_SCHEMA_VERSION
    trace_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_time", utc_datetime(self.snapshot_time))
        object.__setattr__(self, "valid_until", optional_utc_datetime(self.valid_until))
        require_non_empty_string(self.context_snapshot_id, "context_snapshot_id")
        require_non_empty_string(self.ticker, "ticker")
        require_optional_non_empty_string(self.trace_id, "trace_id")


__all__ = [
    "ContextAIEvent",
    "ContextClassificationEventType",
    "ContextClassificationRequest",
    "ContextClassificationResponse",
    "ContextClassificationStatus",
    "ContextFlag",
    "ContextIndicatorSnapshot",
    "ContextRawInput",
    "ContextRiskLevel",
    "ContextSourceDocument",
    "ContextStateSnapshot",
    "ContextUrgency",
    "ContextValidationResult",
    "DeterministicContextEventType",
    "ShadowContextAction",
    "ShadowContextPolicyEvaluation",
]
