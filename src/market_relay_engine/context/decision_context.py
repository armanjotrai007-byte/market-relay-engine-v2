"""Decision-time structured context assembly."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from market_relay_engine.common.serialization import to_json_dict
from market_relay_engine.common.time import ensure_timezone_aware_utc, parse_utc_iso, to_utc_iso
from market_relay_engine.context.provenance import (
    extract_provenance,
    is_research_asof_eligible_at,
)
from market_relay_engine.context.state_cache import ContextStateCache

if TYPE_CHECKING:
    from market_relay_engine.context.refresh_coordinator import ContextRefreshRuntimeState


CONTEXT_SCHEMA_VERSION = "decision_context_v1"
DEFAULT_POLICY_VERSION = "decision_context_policy_v1_default_deny"
UNKNOWN_NOT_REFRESHED = "UNKNOWN_NOT_REFRESHED"

SUPPORTED_REFRESH_SOURCE_IDS: tuple[str, ...] = (
    "macro_calendar",
    "eia_wpsr",
    "fred",
    "usaspending",
    "yfinance_dev_only",
)

KNOWN_SOURCE_CLASSIFICATION: dict[str, dict[str, object]] = {
    "macro_calendar_v1": {
        "known_source": True,
        "refresh_source_id": "macro_calendar",
        "resource_family": "MACRO_CALENDAR",
        "source_mode": "LOCAL_REVIEWED",
        "authority_class": "RESEARCH_ONLY",
    },
    "eia_wpsr_v1": {
        "known_source": True,
        "refresh_source_id": "eia_wpsr",
        "resource_family": "EIA_WPSR",
        "source_mode": "OFFICIAL_SOURCE",
        "authority_class": "RESEARCH_ONLY",
    },
    "fred_rates_v1": {
        "known_source": True,
        "refresh_source_id": "fred",
        "resource_family": "FRED",
        "source_mode": "OFFICIAL_SOURCE",
        "authority_class": "RESEARCH_ONLY",
    },
    "usaspending_awards_v1": {
        "known_source": True,
        "refresh_source_id": "usaspending",
        "resource_family": "USASPENDING",
        "source_mode": "OFFICIAL_SOURCE",
        "authority_class": "RESEARCH_ONLY",
    },
    "yfinance_dev_raw_v1": {
        "known_source": True,
        "refresh_source_id": "yfinance_dev_only",
        "resource_family": "YFINANCE_DEV",
        "source_mode": "DEVELOPMENT_ONLY",
        "authority_class": "DEVELOPMENT_ONLY",
    },
}

_RESOURCE_FAMILIES = {
    "MACRO_CALENDAR",
    "EIA_WPSR",
    "FRED",
    "USASPENDING",
    "YFINANCE_DEV",
    "UNKNOWN",
}
_SOURCE_MODES = {
    "LOCAL_REVIEWED",
    "OFFICIAL_SOURCE",
    "DEVELOPMENT_ONLY",
    "UNKNOWN",
}
_AUTHORITY_CLASSES = {
    "RESEARCH_ONLY",
    "DEVELOPMENT_ONLY",
    "APPROVED_RISK_CONTEXT",
}
_SELECTION_SCOPES = {"GLOBAL", "SECTOR_MATCH", "TICKER_MATCH"}
_SECTOR_RESOLUTION_STATUSES = {"EXPLICIT", "INJECTED_MAPPING", "UNRESOLVED"}
_PROVENANCE_STATES = {"ASOF_ELIGIBLE", "ASOF_INELIGIBLE", "MISSING_OR_MALFORMED"}
_SCOPE_RANK = {"GLOBAL": 0, "SECTOR_MATCH": 1, "TICKER_MATCH": 2}


class DecisionContextError(ValueError):
    """Raised when decision-context assembly inputs or snapshots are invalid."""


@dataclass(frozen=True, kw_only=True)
class DecisionContextEntry:
    """One immutable selected cache entry projected for a decision."""

    entry_identity: str
    cache_scope: str
    cache_name: str
    scope_target: str | None
    value: str | int | float | bool
    severity: str
    source: str
    updated_at: datetime
    source_event_time: datetime | None
    valid_until: datetime | None
    confidence: float | None
    details: Mapping[str, object] = field(default_factory=dict)
    resource_family: str
    source_mode: str
    selection_scope: str
    authority_class: str
    provenance_state: str
    refresh_status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_identity", _required_string(self.entry_identity, "entry_identity"))
        object.__setattr__(self, "cache_scope", _required_string(self.cache_scope, "cache_scope"))
        object.__setattr__(self, "cache_name", _required_string(self.cache_name, "cache_name"))
        object.__setattr__(self, "scope_target", _optional_string(self.scope_target, "scope_target"))
        object.__setattr__(self, "value", _json_safe_scalar(self.value, "value"))
        object.__setattr__(self, "severity", _required_string(self.severity, "severity"))
        object.__setattr__(self, "source", _required_string(self.source, "source"))
        object.__setattr__(self, "updated_at", _aware_datetime(self.updated_at, "updated_at"))
        object.__setattr__(
            self,
            "source_event_time",
            _optional_aware_datetime(self.source_event_time, "source_event_time"),
        )
        object.__setattr__(
            self,
            "valid_until",
            _optional_aware_datetime(self.valid_until, "valid_until"),
        )
        object.__setattr__(self, "confidence", _optional_number(self.confidence, "confidence"))
        details = _deep_freeze_json_safe(self.details)
        if not isinstance(details, Mapping):
            raise DecisionContextError("details must be a mapping")
        object.__setattr__(self, "details", details)
        object.__setattr__(
            self,
            "resource_family",
            _required_member(self.resource_family, _RESOURCE_FAMILIES, "resource_family"),
        )
        object.__setattr__(
            self,
            "source_mode",
            _required_member(self.source_mode, _SOURCE_MODES, "source_mode"),
        )
        object.__setattr__(
            self,
            "selection_scope",
            _required_member(self.selection_scope, _SELECTION_SCOPES, "selection_scope"),
        )
        object.__setattr__(
            self,
            "authority_class",
            _required_member(self.authority_class, _AUTHORITY_CLASSES, "authority_class"),
        )
        object.__setattr__(
            self,
            "provenance_state",
            _required_member(self.provenance_state, _PROVENANCE_STATES, "provenance_state"),
        )
        object.__setattr__(self, "refresh_status", _required_string(self.refresh_status, "refresh_status"))

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-safe projection."""
        return _json_safe_mapping(
            {
                "entry_identity": self.entry_identity,
                "cache_scope": self.cache_scope,
                "cache_name": self.cache_name,
                "scope_target": self.scope_target,
                "value": self.value,
                "severity": self.severity,
                "source": self.source,
                "updated_at": self.updated_at,
                "source_event_time": self.source_event_time,
                "valid_until": self.valid_until,
                "confidence": self.confidence,
                "details": _deep_thaw_json_safe(self.details),
                "resource_family": self.resource_family,
                "source_mode": self.source_mode,
                "selection_scope": self.selection_scope,
                "authority_class": self.authority_class,
                "provenance_state": self.provenance_state,
                "refresh_status": self.refresh_status,
            },
            "decision_context_entry",
        )


@dataclass(frozen=True, kw_only=True)
class SourceReadiness:
    """Decision-time as-of projection of one PR31 source state."""

    source_id: str
    refresh_status: str
    last_attempted_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_usable_at: datetime | None = None
    last_full_success_at: datetime | None = None
    next_due_at: datetime | None = None
    last_status_observed_at: datetime | None = None
    consecutive_failure_count: int | None = None
    consecutive_non_usable_count: int | None = None
    readiness_age_seconds: float | None = None

    def __post_init__(self) -> None:
        source_id = _required_string(self.source_id, "source_id")
        if source_id not in SUPPORTED_REFRESH_SOURCE_IDS:
            raise DecisionContextError(f"unsupported source_id: {source_id}")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "refresh_status", _required_string(self.refresh_status, "refresh_status"))
        object.__setattr__(
            self,
            "last_attempted_at",
            _optional_aware_datetime(self.last_attempted_at, "last_attempted_at"),
        )
        object.__setattr__(
            self,
            "last_completed_at",
            _optional_aware_datetime(self.last_completed_at, "last_completed_at"),
        )
        object.__setattr__(
            self,
            "last_usable_at",
            _optional_aware_datetime(self.last_usable_at, "last_usable_at"),
        )
        object.__setattr__(
            self,
            "last_full_success_at",
            _optional_aware_datetime(self.last_full_success_at, "last_full_success_at"),
        )
        object.__setattr__(
            self,
            "next_due_at",
            _optional_aware_datetime(self.next_due_at, "next_due_at"),
        )
        object.__setattr__(
            self,
            "last_status_observed_at",
            _optional_aware_datetime(self.last_status_observed_at, "last_status_observed_at"),
        )
        object.__setattr__(
            self,
            "consecutive_failure_count",
            _optional_non_negative_int(self.consecutive_failure_count, "consecutive_failure_count"),
        )
        object.__setattr__(
            self,
            "consecutive_non_usable_count",
            _optional_non_negative_int(
                self.consecutive_non_usable_count,
                "consecutive_non_usable_count",
            ),
        )
        object.__setattr__(
            self,
            "readiness_age_seconds",
            _optional_non_negative_number(self.readiness_age_seconds, "readiness_age_seconds"),
        )

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-safe projection."""
        return _json_safe_mapping(to_json_dict(self), "source_readiness")


@dataclass(frozen=True, kw_only=True)
class DecisionContextPolicy:
    """Default-deny policy boundary for future approved risk context."""

    policy_version: str = DEFAULT_POLICY_VERSION
    approved_entry_rules: tuple[Mapping[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_version", _required_string(self.policy_version, "policy_version"))
        rules: list[Mapping[str, object]] = []
        for rule in self.approved_entry_rules:
            if not isinstance(rule, Mapping):
                raise DecisionContextError("approved_entry_rules entries must be mappings")
            expected = {"source", "cache_scope", "cache_name"}
            if set(rule) != expected:
                raise DecisionContextError("approval rules must contain source, cache_scope, and cache_name")
            source = _required_string(rule["source"], "approval.source")
            classification = _classify_source(source)
            if classification.get("known_source") is not True:
                raise DecisionContextError("unknown source cannot be approved for risk context")
            if not _is_risk_approval_eligible(classification):
                raise DecisionContextError("development-only source cannot be approved for risk context")
            frozen_rule = _deep_freeze_json_safe(
                {
                    "source": source,
                    "cache_scope": _required_string(rule["cache_scope"], "approval.cache_scope"),
                    "cache_name": _required_string(rule["cache_name"], "approval.cache_name"),
                }
            )
            if not isinstance(frozen_rule, Mapping):
                raise DecisionContextError("approval rule must be a mapping")
            rules.append(frozen_rule)
        object.__setattr__(self, "approved_entry_rules", tuple(rules))

    def approves(self, *, source: str, cache_scope: str, cache_name: str) -> bool:
        """Return whether the exact source/scope/name tuple is approved."""
        candidate = {
            "source": source,
            "cache_scope": cache_scope,
            "cache_name": cache_name,
        }
        return any(_deep_thaw_json_safe(rule) == candidate for rule in self.approved_entry_rules)

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-safe projection."""
        return _json_safe_mapping(
            {
                "policy_version": self.policy_version,
                "approved_entry_rules": [
                    _deep_thaw_json_safe(rule)
                    for rule in self.approved_entry_rules
                ],
            },
            "decision_context_policy",
        )


@dataclass(frozen=True, kw_only=True)
class DecisionContextAuditPayload:
    """JSON-safe audit payload returned by a decision context."""

    context_schema_version: str
    context_snapshot_id: str
    context_fingerprint: str
    ticker: str
    ticker_sector: str | None
    sector_resolution_status: str
    trace_id: str
    evaluation_time: datetime
    all_structured_context: tuple[DecisionContextEntry, ...]
    approved_risk_context: tuple[DecisionContextEntry, ...]
    source_readiness: tuple[SourceReadiness, ...]
    future_entry_exclusion_count: int
    policy_version: str

    def __post_init__(self) -> None:
        _validate_context_common(self)

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-safe audit payload."""
        return _json_safe_mapping(
            {
                "context_schema_version": self.context_schema_version,
                "context_snapshot_id": self.context_snapshot_id,
                "context_fingerprint": self.context_fingerprint,
                "ticker": self.ticker,
                "ticker_sector": self.ticker_sector,
                "sector_resolution_status": self.sector_resolution_status,
                "trace_id": self.trace_id,
                "evaluation_time": self.evaluation_time,
                "all_structured_context": [
                    entry.to_json_dict()
                    for entry in self.all_structured_context
                ],
                "approved_risk_context": [
                    entry.to_json_dict()
                    for entry in self.approved_risk_context
                ],
                "source_readiness": [
                    item.to_json_dict()
                    for item in self.source_readiness
                ],
                "future_entry_exclusion_count": self.future_entry_exclusion_count,
                "policy_version": self.policy_version,
            },
            "decision_context_audit_payload",
        )


@dataclass(frozen=True, kw_only=True)
class DecisionContext:
    """Immutable decision-scoped context projection."""

    context_schema_version: str
    context_snapshot_id: str
    context_fingerprint: str
    ticker: str
    ticker_sector: str | None
    sector_resolution_status: str
    trace_id: str
    evaluation_time: datetime
    all_structured_context: tuple[DecisionContextEntry, ...]
    approved_risk_context: tuple[DecisionContextEntry, ...]
    source_readiness: tuple[SourceReadiness, ...]
    future_entry_exclusion_count: int
    policy_version: str

    def __post_init__(self) -> None:
        _validate_context_common(self)

    def to_audit_payload(self) -> DecisionContextAuditPayload:
        """Return the full JSON-safe audit payload without persistence side effects."""
        return DecisionContextAuditPayload(
            context_schema_version=self.context_schema_version,
            context_snapshot_id=self.context_snapshot_id,
            context_fingerprint=self.context_fingerprint,
            ticker=self.ticker,
            ticker_sector=self.ticker_sector,
            sector_resolution_status=self.sector_resolution_status,
            trace_id=self.trace_id,
            evaluation_time=self.evaluation_time,
            all_structured_context=self.all_structured_context,
            approved_risk_context=self.approved_risk_context,
            source_readiness=self.source_readiness,
            future_entry_exclusion_count=self.future_entry_exclusion_count,
            policy_version=self.policy_version,
        )

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-safe projection."""
        return _json_safe_mapping(
            {
                "context_schema_version": self.context_schema_version,
                "context_snapshot_id": self.context_snapshot_id,
                "context_fingerprint": self.context_fingerprint,
                "ticker": self.ticker,
                "ticker_sector": self.ticker_sector,
                "sector_resolution_status": self.sector_resolution_status,
                "trace_id": self.trace_id,
                "evaluation_time": self.evaluation_time,
                "all_structured_context": [
                    entry.to_json_dict()
                    for entry in self.all_structured_context
                ],
                "approved_risk_context": [
                    entry.to_json_dict()
                    for entry in self.approved_risk_context
                ],
                "source_readiness": [
                    item.to_json_dict()
                    for item in self.source_readiness
                ],
                "future_entry_exclusion_count": self.future_entry_exclusion_count,
                "policy_version": self.policy_version,
            },
            "decision_context",
        )


class DecisionContextAssembler:
    """Build deterministic ticker-specific decision context from cache state."""

    def __init__(
        self,
        *,
        cache: ContextStateCache,
        policy: DecisionContextPolicy | None = None,
        ticker_sector_by_ticker: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(cache, ContextStateCache):
            raise DecisionContextError("cache must be a ContextStateCache")
        self.cache = cache
        self.policy = DecisionContextPolicy() if policy is None else policy
        if not isinstance(self.policy, DecisionContextPolicy):
            raise DecisionContextError("policy must be a DecisionContextPolicy")
        self.ticker_sector_by_ticker = _normalize_sector_mapping(ticker_sector_by_ticker)

    def build_for_decision(
        self,
        ticker: str,
        evaluation_time: datetime,
        trace_id: str,
        refresh_runtime_state: "ContextRefreshRuntimeState | None",
        *,
        ticker_sector: str | None = None,
    ) -> DecisionContext:
        """Assemble the context visible for one ticker at one evaluation time."""
        normalized_ticker = _normalize_symbol(ticker, "ticker")
        normalized_trace_id = _required_string(trace_id, "trace_id")
        decision_time = _aware_datetime(evaluation_time, "evaluation_time")
        resolved_sector, sector_status = self._resolve_sector(normalized_ticker, ticker_sector)
        snapshot = self.cache.snapshot(now=decision_time)
        readiness = _build_source_readiness(refresh_runtime_state, decision_time)
        readiness_by_source = {item.source_id: item for item in readiness}
        selected, future_exclusion_count = _select_entries(
            snapshot=snapshot,
            ticker=normalized_ticker,
            sector=resolved_sector,
            evaluation_time=decision_time,
            policy=self.policy,
            readiness_by_source=readiness_by_source,
        )
        approved = tuple(
            entry for entry in selected if entry.authority_class == "APPROVED_RISK_CONTEXT"
        )
        fingerprint_payload = {
            "context_schema_version": CONTEXT_SCHEMA_VERSION,
            "ticker": normalized_ticker,
            "ticker_sector": resolved_sector,
            "sector_resolution_status": sector_status,
            "evaluation_time": to_utc_iso(decision_time),
            "all_structured_context": [entry.to_json_dict() for entry in selected],
            "source_readiness": [item.to_json_dict() for item in readiness],
            "future_entry_exclusion_count": future_exclusion_count,
            "policy_version": self.policy.policy_version,
        }
        context_fingerprint = f"decision_context_{_sha256_payload(fingerprint_payload)}"
        context_snapshot_id = f"context_snapshot_{_sha256_payload({
            'trace_id': normalized_trace_id,
            'ticker': normalized_ticker,
            'evaluation_time': to_utc_iso(decision_time),
            'context_fingerprint': context_fingerprint,
            'context_schema_version': CONTEXT_SCHEMA_VERSION,
        })}"
        return DecisionContext(
            context_schema_version=CONTEXT_SCHEMA_VERSION,
            context_snapshot_id=context_snapshot_id,
            context_fingerprint=context_fingerprint,
            ticker=normalized_ticker,
            ticker_sector=resolved_sector,
            sector_resolution_status=sector_status,
            trace_id=normalized_trace_id,
            evaluation_time=decision_time,
            all_structured_context=selected,
            approved_risk_context=approved,
            source_readiness=readiness,
            future_entry_exclusion_count=future_exclusion_count,
            policy_version=self.policy.policy_version,
        )

    def _resolve_sector(self, ticker: str, explicit_sector: str | None) -> tuple[str | None, str]:
        if explicit_sector is not None:
            return _normalize_symbol(explicit_sector, "ticker_sector"), "EXPLICIT"
        mapped = self.ticker_sector_by_ticker.get(ticker)
        if mapped is not None:
            return mapped, "INJECTED_MAPPING"
        return None, "UNRESOLVED"


def _select_entries(
    *,
    snapshot: Mapping[str, object],
    ticker: str,
    sector: str | None,
    evaluation_time: datetime,
    policy: DecisionContextPolicy,
    readiness_by_source: Mapping[str, SourceReadiness],
) -> tuple[tuple[DecisionContextEntry, ...], int]:
    snapshot = _snapshot_mapping(snapshot)
    future_exclusion_count = 0
    selected: list[DecisionContextEntry] = []

    for raw_entry in _iter_snapshot_entries(snapshot):
        parsed = _parse_snapshot_entry(raw_entry)
        selection_scope = _selection_scope(parsed, ticker=ticker, sector=sector)
        if selection_scope is None:
            continue
        if parsed["expired"] is True:
            raise DecisionContextError("snapshot entries must not be expired")
        if parsed["updated_at"] > evaluation_time:
            future_exclusion_count += 1
            continue
        source = parsed["source"]
        cache_scope = parsed["cache_scope"]
        cache_name = parsed["cache_name"]
        classification = _classify_source(source)
        refresh_source_id = classification.get("refresh_source_id")
        if refresh_source_id is None:
            refresh_status = UNKNOWN_NOT_REFRESHED
        else:
            refresh_status = readiness_by_source[refresh_source_id].refresh_status
        authority_class = classification["authority_class"]
        if _is_risk_approval_eligible(classification) and policy.approves(
            source=source,
            cache_scope=cache_scope,
            cache_name=cache_name,
        ):
            authority_class = "APPROVED_RISK_CONTEXT"
        selected.append(
            DecisionContextEntry(
                entry_identity=_entry_identity(
                    cache_scope=cache_scope,
                    scope_target=parsed["scope_target"],
                    cache_name=cache_name,
                    source=source,
                ),
                cache_scope=cache_scope,
                cache_name=cache_name,
                scope_target=parsed["scope_target"],
                value=parsed["value"],
                severity=parsed["severity"],
                source=source,
                updated_at=parsed["updated_at"],
                source_event_time=parsed["source_event_time"],
                valid_until=parsed["valid_until"],
                confidence=parsed["confidence"],
                details=parsed["details"],
                resource_family=classification["resource_family"],
                source_mode=classification["source_mode"],
                selection_scope=selection_scope,
                authority_class=authority_class,
                provenance_state=_provenance_state(parsed["details"], evaluation_time),
                refresh_status=refresh_status,
            )
        )

    return tuple(sorted(selected, key=_entry_sort_key)), future_exclusion_count


def _iter_snapshot_entries(snapshot: Mapping[str, object]) -> list[Mapping[str, object]]:
    entries: list[Mapping[str, object]] = []
    global_entries = _nested_mapping(snapshot.get("global"), "snapshot.global")
    for value in global_entries.values():
        entries.append(_snapshot_entry_mapping(value))
    tickers = _nested_mapping(snapshot.get("tickers"), "snapshot.tickers")
    for ticker_entries in tickers.values():
        for value in _nested_mapping(ticker_entries, "snapshot.tickers[]").values():
            entries.append(_snapshot_entry_mapping(value))
    sectors = _nested_mapping(snapshot.get("sectors"), "snapshot.sectors")
    for sector_entries in sectors.values():
        for value in _nested_mapping(sector_entries, "snapshot.sectors[]").values():
            entries.append(_snapshot_entry_mapping(value))
    return entries


def _parse_snapshot_entry(raw: Mapping[str, object]) -> dict[str, Any]:
    cache_scope = _required_string(raw.get("scope"), "entry.scope")
    if cache_scope not in {"GLOBAL", "TICKER", "SECTOR"}:
        raise DecisionContextError("entry.scope must be GLOBAL, TICKER, or SECTOR")
    cache_name = _required_string(raw.get("name"), "entry.name")
    source = _required_string(raw.get("source"), "entry.source")
    ticker = _optional_symbol(raw.get("ticker"), "entry.ticker")
    sector = _optional_symbol(raw.get("sector"), "entry.sector")
    if cache_scope == "GLOBAL":
        scope_target = None
        if ticker is not None or sector is not None:
            raise DecisionContextError("GLOBAL snapshot entry cannot include ticker or sector")
    elif cache_scope == "TICKER":
        if ticker is None or sector is not None:
            raise DecisionContextError("TICKER snapshot entry must include only ticker")
        scope_target = ticker
    else:
        if sector is None or ticker is not None:
            raise DecisionContextError("SECTOR snapshot entry must include only sector")
        scope_target = sector
    expired = raw.get("expired")
    if not isinstance(expired, bool):
        raise DecisionContextError("entry.expired must be bool")
    return {
        "cache_scope": cache_scope,
        "cache_name": cache_name,
        "scope_target": scope_target,
        "value": _json_safe_scalar(raw.get("value"), "entry.value"),
        "severity": _required_string(raw.get("severity"), "entry.severity"),
        "source": source,
        "updated_at": _parse_required_datetime(raw.get("updated_at"), "entry.updated_at"),
        "source_event_time": _parse_optional_datetime(raw.get("source_event_time"), "entry.source_event_time"),
        "valid_until": _parse_optional_datetime(raw.get("valid_until"), "entry.valid_until"),
        "confidence": _optional_number(raw.get("confidence"), "entry.confidence"),
        "details": _json_safe_mapping(raw.get("details"), "entry.details"),
        "expired": expired,
    }


def _selection_scope(
    entry: Mapping[str, Any],
    *,
    ticker: str,
    sector: str | None,
) -> str | None:
    if entry["cache_scope"] == "GLOBAL":
        return "GLOBAL"
    if entry["cache_scope"] == "TICKER" and entry["scope_target"] == ticker:
        return "TICKER_MATCH"
    if sector is not None and entry["cache_scope"] == "SECTOR" and entry["scope_target"] == sector:
        return "SECTOR_MATCH"
    return None


def _build_source_readiness(
    refresh_runtime_state: object | None,
    evaluation_time: datetime,
) -> tuple[SourceReadiness, ...]:
    if refresh_runtime_state is None:
        return tuple(_unknown_readiness(source_id) for source_id in SUPPORTED_REFRESH_SOURCE_IDS)
    sources = getattr(refresh_runtime_state, "sources", None)
    if not isinstance(sources, Mapping):
        raise DecisionContextError("refresh_runtime_state.sources must be a mapping")
    readiness: list[SourceReadiness] = []
    for source_id in SUPPORTED_REFRESH_SOURCE_IDS:
        state = sources.get(source_id)
        if state is None:
            readiness.append(_unknown_readiness(source_id))
            continue
        readiness.append(_source_readiness_from_state(source_id, state, evaluation_time))
    return tuple(readiness)


def _source_readiness_from_state(
    source_id: str,
    state: object,
    evaluation_time: datetime,
) -> SourceReadiness:
    status = getattr(state, "last_status", None)
    if status is None:
        return _unknown_readiness(source_id)
    status_observed_at = _state_datetime(state, "last_status_observed_at")
    if status_observed_at is None or status_observed_at > evaluation_time:
        return _unknown_readiness(source_id)

    attempted = _state_datetime(state, "last_attempted_at")
    completed = _state_datetime(state, "last_completed_at")
    usable = _state_datetime(state, "last_usable_at")
    full_success = _state_datetime(state, "last_full_success_at")
    observed_timestamps = (
        status_observed_at,
        attempted,
        completed,
        usable,
        full_success,
    )
    if any(timestamp is not None and timestamp > evaluation_time for timestamp in observed_timestamps):
        return _unknown_readiness(source_id)

    status_value = getattr(status, "value", status)
    if not isinstance(status_value, str):
        return _unknown_readiness(source_id)
    refresh_status = status_value
    next_due = _state_datetime(state, "next_due_at")
    age = None
    if completed is not None and completed <= evaluation_time:
        age = (evaluation_time - completed).total_seconds()
    return SourceReadiness(
        source_id=source_id,
        refresh_status=refresh_status,
        last_attempted_at=attempted,
        last_completed_at=completed,
        last_usable_at=usable,
        last_full_success_at=full_success,
        next_due_at=next_due,
        last_status_observed_at=status_observed_at,
        consecutive_failure_count=_state_count(state, "consecutive_failure_count"),
        consecutive_non_usable_count=_state_count(state, "consecutive_non_usable_count"),
        readiness_age_seconds=age,
    )


def _unknown_readiness(source_id: str) -> SourceReadiness:
    return SourceReadiness(source_id=source_id, refresh_status=UNKNOWN_NOT_REFRESHED)


def _state_datetime(state: object, field_name: str) -> datetime | None:
    return _optional_aware_datetime(getattr(state, field_name, None), field_name)


def _state_count(state: object, field_name: str) -> int | None:
    return _optional_non_negative_int(getattr(state, field_name, None), field_name)


def _classify_source(source: str) -> dict[str, object]:
    classification = KNOWN_SOURCE_CLASSIFICATION.get(source)
    if classification is None:
        return {
            "known_source": False,
            "refresh_source_id": None,
            "resource_family": "UNKNOWN",
            "source_mode": "UNKNOWN",
            "authority_class": "RESEARCH_ONLY",
        }
    return dict(classification)


def _is_risk_approval_eligible(classification: Mapping[str, object]) -> bool:
    return (
        classification.get("known_source") is True
        and classification.get("source_mode") != "DEVELOPMENT_ONLY"
        and classification.get("authority_class") != "DEVELOPMENT_ONLY"
    )


def _provenance_state(details: Mapping[str, object], evaluation_time: datetime) -> str:
    provenance = extract_provenance(details)
    if provenance is None:
        return "MISSING_OR_MALFORMED"
    if is_research_asof_eligible_at(details, evaluation_time):
        return "ASOF_ELIGIBLE"
    return "ASOF_INELIGIBLE"


def _entry_identity(
    *,
    cache_scope: str,
    scope_target: str | None,
    cache_name: str,
    source: str,
) -> str:
    return "|".join((source, cache_scope, scope_target or "", cache_name))


def _entry_sort_key(entry: DecisionContextEntry) -> tuple[int, str, str, str, str, str]:
    return (
        _SCOPE_RANK[entry.selection_scope],
        entry.cache_scope,
        entry.scope_target or "",
        entry.cache_name,
        entry.source,
        to_utc_iso(entry.updated_at),
    )


def _validate_context_common(value: object) -> None:
    object.__setattr__(
        value,
        "context_schema_version",
        _required_string(getattr(value, "context_schema_version"), "context_schema_version"),
    )
    object.__setattr__(
        value,
        "context_snapshot_id",
        _required_string(getattr(value, "context_snapshot_id"), "context_snapshot_id"),
    )
    object.__setattr__(
        value,
        "context_fingerprint",
        _required_string(getattr(value, "context_fingerprint"), "context_fingerprint"),
    )
    object.__setattr__(value, "ticker", _normalize_symbol(getattr(value, "ticker"), "ticker"))
    object.__setattr__(
        value,
        "ticker_sector",
        _optional_symbol(getattr(value, "ticker_sector"), "ticker_sector"),
    )
    object.__setattr__(
        value,
        "sector_resolution_status",
        _required_member(
            getattr(value, "sector_resolution_status"),
            _SECTOR_RESOLUTION_STATUSES,
            "sector_resolution_status",
        ),
    )
    object.__setattr__(value, "trace_id", _required_string(getattr(value, "trace_id"), "trace_id"))
    object.__setattr__(
        value,
        "evaluation_time",
        _aware_datetime(getattr(value, "evaluation_time"), "evaluation_time"),
    )
    object.__setattr__(
        value,
        "all_structured_context",
        _entry_tuple(getattr(value, "all_structured_context"), "all_structured_context"),
    )
    object.__setattr__(
        value,
        "approved_risk_context",
        _entry_tuple(getattr(value, "approved_risk_context"), "approved_risk_context"),
    )
    object.__setattr__(
        value,
        "source_readiness",
        _readiness_tuple(getattr(value, "source_readiness")),
    )
    object.__setattr__(
        value,
        "future_entry_exclusion_count",
        _non_negative_int(getattr(value, "future_entry_exclusion_count"), "future_entry_exclusion_count"),
    )
    object.__setattr__(value, "policy_version", _required_string(getattr(value, "policy_version"), "policy_version"))


def _entry_tuple(value: object, field_name: str) -> tuple[DecisionContextEntry, ...]:
    try:
        entries = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise DecisionContextError(f"{field_name} must be a sequence") from exc
    if not all(isinstance(entry, DecisionContextEntry) for entry in entries):
        raise DecisionContextError(f"{field_name} entries must be DecisionContextEntry")
    return entries


def _readiness_tuple(value: object) -> tuple[SourceReadiness, ...]:
    try:
        entries = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise DecisionContextError("source_readiness must be a sequence") from exc
    if not all(isinstance(entry, SourceReadiness) for entry in entries):
        raise DecisionContextError("source_readiness entries must be SourceReadiness")
    return entries


def _normalize_sector_mapping(value: Mapping[str, str] | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DecisionContextError("ticker_sector_by_ticker must be a mapping")
    return {
        _normalize_symbol(ticker, "ticker_sector_by_ticker key"): _normalize_symbol(
            sector,
            "ticker_sector_by_ticker value",
        )
        for ticker, sector in value.items()
    }


def _snapshot_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DecisionContextError("cache snapshot must be a mapping")
    for key in ("global", "tickers", "sectors"):
        if key not in value:
            raise DecisionContextError(f"cache snapshot missing {key}")
    return value


def _nested_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DecisionContextError(f"{field_name} must be a mapping")
    for key in value:
        if not isinstance(key, str):
            raise DecisionContextError(f"{field_name} keys must be strings")
    return value


def _snapshot_entry_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DecisionContextError("snapshot entry must be a mapping")
    return value


def _parse_required_datetime(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return _aware_datetime(value, field_name)
    if isinstance(value, str):
        try:
            return parse_utc_iso(value)
        except ValueError as exc:
            raise DecisionContextError(f"{field_name} must be an offset-aware ISO datetime") from exc
    raise DecisionContextError(f"{field_name} must be a datetime or ISO string")


def _parse_optional_datetime(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _parse_required_datetime(value, field_name)


def _aware_datetime(value: object, field_name: str) -> datetime:
    try:
        return ensure_timezone_aware_utc(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise DecisionContextError(f"{field_name} must be a timezone-aware datetime") from exc


def _optional_aware_datetime(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _aware_datetime(value, field_name)


def _normalize_symbol(value: object, field_name: str) -> str:
    return _required_string(value, field_name).upper()


def _optional_symbol(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _normalize_symbol(value, field_name)


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DecisionContextError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, field_name)


def _required_member(value: object, allowed: set[str], field_name: str) -> str:
    text = _required_string(value, field_name)
    if text not in allowed:
        raise DecisionContextError(f"{field_name} must be one of {sorted(allowed)}")
    return text


def _json_safe_scalar(value: object, field_name: str) -> str | int | float | bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _required_string(value, field_name)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        json.dumps(value, allow_nan=False)
        return value
    raise DecisionContextError(f"{field_name} must be a JSON-safe scalar")


def _optional_number(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecisionContextError(f"{field_name} must be numeric")
    number = float(value)
    json.dumps(number, allow_nan=False)
    return number


def _optional_non_negative_number(value: object, field_name: str) -> float | None:
    number = _optional_number(value, field_name)
    if number is not None and number < 0.0:
        raise DecisionContextError(f"{field_name} must be non-negative")
    return number


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DecisionContextError(f"{field_name} must be a non-negative integer")
    return value


def _optional_non_negative_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    return _non_negative_int(value, field_name)


def _copy_json_container(value: object) -> object:
    if isinstance(value, Mapping):
        copied: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise DecisionContextError("JSON object keys must be strings")
            copied[key] = _copy_json_container(child)
        return copied
    if isinstance(value, (list, tuple)):
        return [_copy_json_container(child) for child in value]
    return value


def _freeze_loaded_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {
                key: _freeze_loaded_json(child)
                for key, child in value.items()
            }
        )
    if isinstance(value, list):
        return tuple(_freeze_loaded_json(child) for child in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        json.dumps(value, allow_nan=False)
        return value
    raise DecisionContextError("value must be JSON-safe")


def _deep_freeze_json_safe(value: object) -> object:
    """Return a deeply immutable JSON-safe copy of a JSON-like value."""
    try:
        prepared = _copy_json_container(value)
        safe_value = to_json_dict(prepared)
        encoded = json.dumps(
            safe_value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        loaded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise DecisionContextError("value must be JSON-safe") from exc
    return _freeze_loaded_json(loaded)


def _deep_thaw_json_safe(value: object) -> object:
    """Return a fresh mutable JSON-safe copy of a frozen JSON-like value."""
    if isinstance(value, Mapping):
        return {
            key: _deep_thaw_json_safe(child)
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_deep_thaw_json_safe(child) for child in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        json.dumps(value, allow_nan=False)
        return value
    raise DecisionContextError("value must be JSON-safe")


def _json_safe_mapping(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise DecisionContextError(f"{field_name} must be a mapping")
    try:
        frozen = _deep_freeze_json_safe(value)
        loaded = _deep_thaw_json_safe(frozen)
    except DecisionContextError as exc:
        raise DecisionContextError(f"{field_name} must be JSON-safe") from exc
    if not isinstance(loaded, dict):
        raise DecisionContextError(f"{field_name} must be a mapping")
    return loaded


def _sha256_payload(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(payload: object) -> str:
    return json.dumps(
        to_json_dict(payload),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = [
    "CONTEXT_SCHEMA_VERSION",
    "DEFAULT_POLICY_VERSION",
    "KNOWN_SOURCE_CLASSIFICATION",
    "SUPPORTED_REFRESH_SOURCE_IDS",
    "DecisionContext",
    "DecisionContextAssembler",
    "DecisionContextAuditPayload",
    "DecisionContextEntry",
    "DecisionContextError",
    "DecisionContextPolicy",
    "SourceReadiness",
    "UNKNOWN_NOT_REFRESHED",
]
