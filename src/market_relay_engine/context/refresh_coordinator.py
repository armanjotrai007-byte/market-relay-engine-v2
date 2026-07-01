"""One-shot coordinator for context collector refresh attempts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
import json
import re
from pathlib import Path
from typing import Any, Protocol

from market_relay_engine.common.config import load_yaml_config
from market_relay_engine.common.serialization import to_json_dict as _repo_to_json_dict
from market_relay_engine.common.time import (
    ensure_timezone_aware_utc,
    parse_utc_iso,
    to_utc_iso,
)
from market_relay_engine.context.macro_calendar import (
    MacroCalendar,
    MacroCalendarCollector,
    load_macro_calendar,
)
from market_relay_engine.context.yfinance_proxy import (
    INTERVAL_SECONDS,
    YFinanceProxyCollector,
)

SUPPORTED_SOURCE_IDS: tuple[str, ...] = (
    "macro_calendar",
    "eia_wpsr",
    "fred",
    "usaspending",
    "yfinance_dev_only",
)
DEFAULT_CONFIG_PATH = "config/context_refresh.yaml"
MAX_ERROR_MESSAGE_LENGTH = 300


class ContextRefreshError(ValueError):
    """Raised when coordinator inputs or configuration are invalid."""


class ContextRefreshStatus(str, Enum):
    """Coordinator-level source outcome status."""

    DISABLED = "DISABLED"
    SKIPPED_NOT_DUE = "SKIPPED_NOT_DUE"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    NO_FRESH_DATA = "NO_FRESH_DATA"
    DATA_DELAYED = "DATA_DELAYED"
    NO_ACTIVE_EVENTS = "NO_ACTIVE_EVENTS"
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, kw_only=True)
class ContextRefreshIssue:
    """Coordinator issue safe for logs and JSON serialization."""

    issue_type: str
    message: str
    source_id: str | None = None
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "issue_type", _required_string(self.issue_type, "issue_type"))
        object.__setattr__(self, "message", _bounded_text(self.message))
        object.__setattr__(self, "source_id", _optional_source_id(self.source_id))
        object.__setattr__(self, "details", _json_safe_mapping(self.details, "details"))


@dataclass(frozen=True, kw_only=True)
class ContextRefreshSourceState:
    """In-memory coordinator state for one source."""

    last_attempted_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_usable_at: datetime | None = None
    last_full_success_at: datetime | None = None
    last_status: ContextRefreshStatus | None = None
    next_due_at: datetime | None = None
    consecutive_failure_count: int = 0
    consecutive_non_usable_count: int = 0
    last_error_type: str | None = None
    last_error_message: str | None = None
    adapter_state: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
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
        status = None if self.last_status is None else ContextRefreshStatus(self.last_status)
        object.__setattr__(self, "last_status", status)
        object.__setattr__(
            self,
            "next_due_at",
            _optional_aware_datetime(self.next_due_at, "next_due_at"),
        )
        object.__setattr__(
            self,
            "consecutive_failure_count",
            _non_negative_int(self.consecutive_failure_count, "consecutive_failure_count"),
        )
        object.__setattr__(
            self,
            "consecutive_non_usable_count",
            _non_negative_int(self.consecutive_non_usable_count, "consecutive_non_usable_count"),
        )
        object.__setattr__(
            self,
            "last_error_type",
            _optional_error_string(self.last_error_type, "last_error_type"),
        )
        object.__setattr__(
            self,
            "last_error_message",
            None if self.last_error_message is None else _bounded_text(self.last_error_message),
        )
        object.__setattr__(
            self,
            "adapter_state",
            _json_safe_mapping(self.adapter_state, "adapter_state"),
        )

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-safe state projection with no native collector objects."""
        return _json_safe_mapping(_repo_to_json_dict(self), "source_state")


@dataclass(frozen=True, kw_only=True)
class ContextRefreshRuntimeState:
    """Complete in-memory coordinator runtime state."""

    sources: Mapping[str, ContextRefreshSourceState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.sources, Mapping):
            raise ContextRefreshError("sources must be a mapping")
        copied: dict[str, ContextRefreshSourceState] = {}
        for source_id, state in self.sources.items():
            source_id = _required_string(source_id, "source_id")
            if not isinstance(state, ContextRefreshSourceState):
                raise ContextRefreshError("runtime source values must be ContextRefreshSourceState")
            copied[source_id] = state
        object.__setattr__(self, "sources", copied)

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-safe state projection."""
        return {
            "sources": {
                source_id: state.to_json_dict()
                for source_id, state in sorted(self.sources.items())
            }
        }


@dataclass(frozen=True, kw_only=True)
class ContextRefreshAdapterResult:
    """Normalized result returned by a source adapter."""

    status: ContextRefreshStatus
    usable_context: bool
    next_due_at: datetime | None = None
    adapter_state: Mapping[str, object] = field(default_factory=dict)
    native_result: object | None = None
    issues: tuple[ContextRefreshIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ContextRefreshStatus(self.status))
        if not isinstance(self.usable_context, bool):
            raise ContextRefreshError("usable_context must be bool")
        object.__setattr__(
            self,
            "adapter_state",
            _json_safe_mapping(self.adapter_state, "adapter_state"),
        )
        object.__setattr__(self, "issues", tuple(self.issues))


class ContextRefreshAdapter(Protocol):
    """Adapter protocol for one existing context collector."""

    source_id: str

    def is_enabled(self) -> bool:
        """Return whether the source should be considered for a run."""

    def run_once(
        self,
        evaluation_time: datetime,
        source_state: ContextRefreshSourceState,
        *,
        write_questdb: bool,
        questdb_required: bool,
        run_id: str | None,
        session_id: str | None,
    ) -> ContextRefreshAdapterResult:
        """Run the source once and return a normalized coordinator result."""


@dataclass(frozen=True, kw_only=True)
class ContextRefreshSourcePolicy:
    """Coordinator fallback policy for one source."""

    source_id: str
    fallback_interval_seconds: int

    def __post_init__(self) -> None:
        source_id = _required_string(self.source_id, "source_id")
        if source_id not in SUPPORTED_SOURCE_IDS:
            raise ContextRefreshError(f"unsupported source id: {source_id}")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(
            self,
            "fallback_interval_seconds",
            _positive_int(self.fallback_interval_seconds, "fallback_interval_seconds"),
        )


@dataclass(frozen=True, kw_only=True)
class ContextRefreshPolicy:
    """Validated coordinator policy."""

    schema_version: int
    source_order: tuple[str, ...]
    sources: Mapping[str, ContextRefreshSourcePolicy]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContextRefreshError("context refresh schema_version must be 1")
        order = tuple(_required_string(item, "source_order[]") for item in self.source_order)
        _validate_exact_source_order(order)
        policies: dict[str, ContextRefreshSourcePolicy] = {}
        if not isinstance(self.sources, Mapping):
            raise ContextRefreshError("sources must be a mapping")
        for source_id, policy in self.sources.items():
            source_id = _required_string(source_id, "source_id")
            if not isinstance(policy, ContextRefreshSourcePolicy):
                raise ContextRefreshError("sources values must be ContextRefreshSourcePolicy")
            if source_id != policy.source_id:
                raise ContextRefreshError(f"source policy key mismatch: {source_id}")
            policies[source_id] = policy
        if set(policies) != set(SUPPORTED_SOURCE_IDS):
            raise ContextRefreshError("sources must contain exactly the supported source IDs")
        object.__setattr__(self, "source_order", order)
        object.__setattr__(self, "sources", policies)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "ContextRefreshPolicy":
        """Validate and build policy from a YAML mapping."""
        if not isinstance(mapping, Mapping):
            raise ContextRefreshError("context refresh config must be a mapping")
        expected_keys = {"schema_version", "source_order", "sources"}
        unexpected = sorted(set(mapping).difference(expected_keys))
        if unexpected:
            raise ContextRefreshError(f"unexpected context refresh config key: {unexpected[0]}")
        missing = sorted(expected_keys.difference(mapping))
        if missing:
            raise ContextRefreshError(f"missing context refresh config key: {missing[0]}")
        raw_order = mapping["source_order"]
        if not isinstance(raw_order, Sequence) or isinstance(raw_order, (str, bytes, bytearray)):
            raise ContextRefreshError("source_order must be a sequence")
        order = tuple(str(item) for item in raw_order)
        _validate_exact_source_order(order)
        raw_sources = mapping["sources"]
        if not isinstance(raw_sources, Mapping):
            raise ContextRefreshError("sources must be a mapping")
        unexpected_sources = sorted(set(raw_sources).difference(SUPPORTED_SOURCE_IDS))
        if unexpected_sources:
            raise ContextRefreshError(f"unsupported source id: {unexpected_sources[0]}")
        missing_sources = sorted(set(SUPPORTED_SOURCE_IDS).difference(raw_sources))
        if missing_sources:
            raise ContextRefreshError(f"missing source config: {missing_sources[0]}")
        sources: dict[str, ContextRefreshSourcePolicy] = {}
        for source_id in order:
            raw_source = raw_sources[source_id]
            if not isinstance(raw_source, Mapping):
                raise ContextRefreshError(f"sources.{source_id} must be a mapping")
            expected_source_keys = {"fallback_interval_seconds"}
            unexpected_source_keys = sorted(set(raw_source).difference(expected_source_keys))
            if unexpected_source_keys:
                raise ContextRefreshError(
                    f"unexpected sources.{source_id} key: {unexpected_source_keys[0]}"
                )
            if "fallback_interval_seconds" not in raw_source:
                raise ContextRefreshError(f"sources.{source_id}.fallback_interval_seconds is required")
            sources[source_id] = ContextRefreshSourcePolicy(
                source_id=source_id,
                fallback_interval_seconds=raw_source["fallback_interval_seconds"],
            )
        return cls(
            schema_version=mapping["schema_version"],
            source_order=order,
            sources=sources,
        )

    @classmethod
    def from_yaml(
        cls,
        path: str | Path = DEFAULT_CONFIG_PATH,
        *,
        base_dir: str | Path | None = None,
    ) -> "ContextRefreshPolicy":
        """Load coordinator policy from YAML."""
        return cls.from_mapping(load_yaml_config(path, base_dir=base_dir))


@dataclass(frozen=True, kw_only=True)
class ContextRefreshSourceOutcome:
    """Outcome for one source in one coordinator run."""

    source_id: str
    status: ContextRefreshStatus
    due_at_start: datetime | None
    attempted: bool
    next_due_at: datetime | None
    usable_context: bool = False
    native_result: object | None = None
    error_type: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _required_string(self.source_id, "source_id"))
        object.__setattr__(self, "status", ContextRefreshStatus(self.status))
        object.__setattr__(
            self,
            "due_at_start",
            _optional_aware_datetime(self.due_at_start, "due_at_start"),
        )
        if not isinstance(self.attempted, bool):
            raise ContextRefreshError("attempted must be bool")
        object.__setattr__(
            self,
            "next_due_at",
            _optional_aware_datetime(self.next_due_at, "next_due_at"),
        )
        if not isinstance(self.usable_context, bool):
            raise ContextRefreshError("usable_context must be bool")
        object.__setattr__(
            self,
            "error_type",
            _optional_error_string(self.error_type, "error_type"),
        )
        object.__setattr__(
            self,
            "error_message",
            None if self.error_message is None else _bounded_text(self.error_message),
        )

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-safe outcome projection without the native result object."""
        result = {
            "source_id": self.source_id,
            "status": self.status.value,
            "due_at_start": _optional_iso(self.due_at_start),
            "attempted": self.attempted,
            "next_due_at": _optional_iso(self.next_due_at),
            "usable_context": self.usable_context,
            "native_result_summary": _native_result_summary(self.native_result),
            "error_type": self.error_type,
            "error_message": self.error_message,
        }
        return _json_safe_mapping(result, "source_outcome")


@dataclass(frozen=True, kw_only=True)
class ContextRefreshRunResult:
    """Structured result from one coordinator pass."""

    evaluation_time: datetime
    updated_runtime_state: ContextRefreshRuntimeState
    source_outcomes: tuple[ContextRefreshSourceOutcome, ...]
    sources_run: tuple[str, ...]
    sources_skipped_not_due: tuple[str, ...]
    sources_disabled: tuple[str, ...]
    issues: tuple[ContextRefreshIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evaluation_time",
            ensure_timezone_aware_utc(self.evaluation_time),
        )
        if not isinstance(self.updated_runtime_state, ContextRefreshRuntimeState):
            raise ContextRefreshError("updated_runtime_state must be ContextRefreshRuntimeState")
        object.__setattr__(self, "source_outcomes", tuple(self.source_outcomes))
        object.__setattr__(self, "sources_run", tuple(self.sources_run))
        object.__setattr__(self, "sources_skipped_not_due", tuple(self.sources_skipped_not_due))
        object.__setattr__(self, "sources_disabled", tuple(self.sources_disabled))
        object.__setattr__(self, "issues", tuple(self.issues))

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-safe run projection that excludes native result internals."""
        return _json_safe_mapping(
            {
                "evaluation_time": to_utc_iso(self.evaluation_time),
                "updated_runtime_state": self.updated_runtime_state.to_json_dict(),
                "source_outcomes": [
                    outcome.to_json_dict() for outcome in self.source_outcomes
                ],
                "sources_run": list(self.sources_run),
                "sources_skipped_not_due": list(self.sources_skipped_not_due),
                "sources_disabled": list(self.sources_disabled),
                "issues": _repo_to_json_dict(self.issues),
            },
            "run_result",
        )


class ContextRefreshCoordinator:
    """Deterministic one-shot source refresh coordinator."""

    def __init__(
        self,
        *,
        adapters: Sequence[ContextRefreshAdapter],
        policy: ContextRefreshPolicy,
    ) -> None:
        self.policy = policy
        adapter_map: dict[str, ContextRefreshAdapter] = {}
        for adapter in adapters:
            source_id = _required_string(adapter.source_id, "adapter.source_id")
            if source_id in adapter_map:
                raise ContextRefreshError(f"duplicate adapter source id: {source_id}")
            adapter_map[source_id] = adapter
        missing = sorted(set(self.policy.source_order).difference(adapter_map))
        if missing:
            raise ContextRefreshError(f"missing adapter for source: {missing[0]}")
        unexpected = sorted(set(adapter_map).difference(self.policy.source_order))
        if unexpected:
            raise ContextRefreshError(f"unexpected adapter source id: {unexpected[0]}")
        self._adapters = adapter_map

    def run_due_once(
        self,
        evaluation_time: datetime,
        runtime_state: ContextRefreshRuntimeState | None,
        *,
        write_questdb: bool = False,
        questdb_required: bool = False,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> ContextRefreshRunResult:
        """Run every enabled due source once in deterministic source order."""
        now = ensure_timezone_aware_utc(evaluation_time)
        state = self._complete_runtime_state(runtime_state)
        updated_sources = dict(state.sources)
        outcomes: list[ContextRefreshSourceOutcome] = []
        issues: list[ContextRefreshIssue] = []
        sources_run: list[str] = []
        sources_skipped_not_due: list[str] = []
        sources_disabled: list[str] = []

        for source_id in self.policy.source_order:
            adapter = self._adapters[source_id]
            policy = self.policy.sources[source_id]
            previous = updated_sources[source_id]
            due_at_start = previous.next_due_at

            if not adapter.is_enabled():
                updated_sources[source_id] = _copy_source_state(
                    previous,
                    last_status=ContextRefreshStatus.DISABLED,
                )
                sources_disabled.append(source_id)
                outcomes.append(
                    ContextRefreshSourceOutcome(
                        source_id=source_id,
                        status=ContextRefreshStatus.DISABLED,
                        due_at_start=due_at_start,
                        attempted=False,
                        next_due_at=previous.next_due_at,
                    )
                )
                continue

            if not _source_is_due(previous, now):
                updated_sources[source_id] = _copy_source_state(
                    previous,
                    last_status=ContextRefreshStatus.SKIPPED_NOT_DUE,
                )
                sources_skipped_not_due.append(source_id)
                outcomes.append(
                    ContextRefreshSourceOutcome(
                        source_id=source_id,
                        status=ContextRefreshStatus.SKIPPED_NOT_DUE,
                        due_at_start=due_at_start,
                        attempted=False,
                        next_due_at=previous.next_due_at,
                    )
                )
                continue

            attempted_state = _copy_source_state(previous, last_attempted_at=now)
            try:
                adapter_result = adapter.run_once(
                    now,
                    attempted_state,
                    write_questdb=write_questdb,
                    questdb_required=questdb_required,
                    run_id=run_id,
                    session_id=session_id,
                )
            except Exception as exc:  # noqa: BLE001 - adapter boundary; BaseException is not caught.
                error_type = type(exc).__name__
                error_message = _bounded_text(str(exc) or error_type)
                next_due = now + timedelta(seconds=policy.fallback_interval_seconds)
                updated_sources[source_id] = _copy_source_state(
                    previous,
                    last_attempted_at=now,
                    last_status=ContextRefreshStatus.FAILED,
                    next_due_at=next_due,
                    consecutive_failure_count=previous.consecutive_failure_count + 1,
                    consecutive_non_usable_count=previous.consecutive_non_usable_count + 1,
                    last_error_type=error_type,
                    last_error_message=error_message,
                )
                sources_run.append(source_id)
                issues.append(
                    ContextRefreshIssue(
                        issue_type="ADAPTER_EXCEPTION",
                        source_id=source_id,
                        message=error_message,
                        details={"error_type": error_type},
                    )
                )
                outcomes.append(
                    ContextRefreshSourceOutcome(
                        source_id=source_id,
                        status=ContextRefreshStatus.FAILED,
                        due_at_start=due_at_start,
                        attempted=True,
                        next_due_at=next_due,
                        error_type=error_type,
                        error_message=error_message,
                    )
                )
                continue

            issues.extend(adapter_result.issues)
            next_due = _resolve_next_due(
                source_id=source_id,
                hint=adapter_result.next_due_at,
                evaluation_time=now,
                fallback_interval_seconds=policy.fallback_interval_seconds,
                issues=issues,
            )
            updated_state = _state_after_adapter_return(
                previous=previous,
                attempted_at=now,
                adapter_result=adapter_result,
                next_due_at=next_due,
            )
            updated_sources[source_id] = updated_state
            sources_run.append(source_id)
            outcomes.append(
                ContextRefreshSourceOutcome(
                    source_id=source_id,
                    status=adapter_result.status,
                    due_at_start=due_at_start,
                    attempted=True,
                    next_due_at=next_due,
                    usable_context=adapter_result.usable_context,
                    native_result=adapter_result.native_result,
                    error_type=updated_state.last_error_type,
                    error_message=updated_state.last_error_message,
                )
            )

        return ContextRefreshRunResult(
            evaluation_time=now,
            updated_runtime_state=ContextRefreshRuntimeState(sources=updated_sources),
            source_outcomes=tuple(outcomes),
            sources_run=tuple(sources_run),
            sources_skipped_not_due=tuple(sources_skipped_not_due),
            sources_disabled=tuple(sources_disabled),
            issues=tuple(issues),
        )

    def _complete_runtime_state(
        self,
        runtime_state: ContextRefreshRuntimeState | None,
    ) -> ContextRefreshRuntimeState:
        if runtime_state is None:
            runtime_state = ContextRefreshRuntimeState()
        unknown = sorted(set(runtime_state.sources).difference(self.policy.source_order))
        if unknown:
            raise ContextRefreshError(f"unknown context refresh source state: {unknown[0]}")
        sources = {
            source_id: runtime_state.sources.get(source_id, ContextRefreshSourceState())
            for source_id in self.policy.source_order
        }
        return ContextRefreshRuntimeState(sources=sources)


class EIAWPSRRefreshAdapter:
    """Thin coordinator adapter for the existing EIA WPSR collector."""

    source_id = "eia_wpsr"

    def __init__(self, collector: object) -> None:
        self.collector = collector

    def is_enabled(self) -> bool:
        config = self.collector.config
        return bool(config.event_windows_enabled or config.numeric_source_enabled)

    def run_once(
        self,
        evaluation_time: datetime,
        source_state: ContextRefreshSourceState,
        *,
        write_questdb: bool,
        questdb_required: bool,
        run_id: str | None,
        session_id: str | None,
    ) -> ContextRefreshAdapterResult:
        adapter_state = source_state.adapter_state
        last_numeric_attempt_at = _adapter_state_datetime(
            adapter_state,
            "last_numeric_attempt_at",
        )
        last_successful_report_period = _adapter_state_date(
            adapter_state,
            "last_successful_report_period",
        )
        result = self.collector.collect(
            evaluation_time=evaluation_time,
            last_numeric_attempt_at=last_numeric_attempt_at,
            last_successful_report_period=last_successful_report_period,
            write_questdb=write_questdb,
            questdb_required=questdb_required,
            run_id=run_id,
            session_id=session_id,
        )
        status, issues = _map_native_status(
            source_id=self.source_id,
            native_status=result.status,
            allowed={
                "DISABLED": ContextRefreshStatus.DISABLED,
                "SUCCESS": ContextRefreshStatus.SUCCESS,
                "PARTIAL": ContextRefreshStatus.PARTIAL,
                "NO_FRESH_DATA": ContextRefreshStatus.NO_FRESH_DATA,
                "DATA_DELAYED": ContextRefreshStatus.DATA_DELAYED,
                "SUPERSEDED": ContextRefreshStatus.SUPERSEDED,
                "FAILED": ContextRefreshStatus.FAILED,
            },
        )
        config = self.collector.config
        numeric_source_enabled = config.numeric_source_enabled is True
        if numeric_source_enabled:
            next_due = result.next_retry_at or result.action_plan.next_action_at
        else:
            next_due = _next_eia_release_window_start(
                getattr(config, "releases", ()),
                evaluation_time,
            )
        next_state: dict[str, object] = dict(adapter_state)
        action_kind = getattr(result.action_plan.action_kind, "value", result.action_plan.action_kind)
        numeric_attempt_was_permitted = numeric_source_enabled and action_kind in {
            "FETCH_NUMERIC_REPORT",
            "RETRY_NUMERIC_REPORT",
        }
        if numeric_attempt_was_permitted:
            next_state["last_numeric_attempt_at"] = to_utc_iso(evaluation_time)
        if (
            getattr(result.data_status, "value", result.data_status) == "CURRENT"
            and result.expected_report_period is not None
            and result.last_seen_report_period == result.expected_report_period
        ):
            next_state["last_successful_report_period"] = result.expected_report_period.isoformat()
        usable = bool(result.context_flags or result.indicator_snapshots)
        return ContextRefreshAdapterResult(
            status=status,
            usable_context=usable,
            next_due_at=next_due,
            adapter_state=next_state,
            native_result=result,
            issues=tuple(issues),
        )


class MacroCalendarRefreshAdapter:
    """Thin coordinator adapter for the existing local macro calendar collector."""

    source_id = "macro_calendar"

    def __init__(self, collector: MacroCalendarCollector, *, calendar: MacroCalendar | None = None) -> None:
        self.collector = collector
        self.calendar = calendar

    def is_enabled(self) -> bool:
        return bool(self.collector.config.enabled and self.collector.config.feeds_memory_cache)

    def run_once(
        self,
        evaluation_time: datetime,
        source_state: ContextRefreshSourceState,
        *,
        write_questdb: bool,
        questdb_required: bool,
        run_id: str | None,
        session_id: str | None,
    ) -> ContextRefreshAdapterResult:
        del source_state
        result = self.collector.collect_once(
            evaluation_time,
            write_questdb=write_questdb,
            questdb_required=questdb_required,
            run_id=run_id,
            session_id=session_id,
        )
        status, issues = _map_native_status(
            source_id=self.source_id,
            native_status=result.status,
            allowed={
                "DISABLED": ContextRefreshStatus.DISABLED,
                "SUCCESS": ContextRefreshStatus.SUCCESS,
                "NO_ACTIVE_EVENTS": ContextRefreshStatus.NO_ACTIVE_EVENTS,
                "PARTIAL": ContextRefreshStatus.PARTIAL,
            },
        )
        next_due = _next_macro_transition(self._calendar(), evaluation_time)
        return ContextRefreshAdapterResult(
            status=status,
            usable_context=bool(result.indicator_snapshots or result.cache_update_results),
            next_due_at=next_due,
            adapter_state={},
            native_result=result,
            issues=tuple(issues),
        )

    def _calendar(self) -> MacroCalendar:
        if self.calendar is not None:
            return self.calendar
        if self.collector.calendar is not None:
            return self.collector.calendar
        return load_macro_calendar(
            self.collector.config.artifact_path,
            base_dir=self.collector.base_dir,
        )


class FREDRefreshAdapter:
    """Thin coordinator adapter for the existing FRED collector."""

    source_id = "fred"

    def __init__(self, collector: object) -> None:
        self.collector = collector

    def is_enabled(self) -> bool:
        return bool(self.collector.config.enabled)

    def run_once(
        self,
        evaluation_time: datetime,
        source_state: ContextRefreshSourceState,
        *,
        write_questdb: bool,
        questdb_required: bool,
        run_id: str | None,
        session_id: str | None,
    ) -> ContextRefreshAdapterResult:
        del source_state
        result = self.collector.collect(
            evaluation_time=evaluation_time,
            write_questdb=write_questdb,
            questdb_required=questdb_required,
            run_id=run_id,
            session_id=session_id,
        )
        status, issues = _map_native_status(
            source_id=self.source_id,
            native_status=result.status,
            allowed={
                "DISABLED": ContextRefreshStatus.DISABLED,
                "FAILED": ContextRefreshStatus.FAILED,
                "STALE": ContextRefreshStatus.STALE,
                "PARTIAL": ContextRefreshStatus.PARTIAL,
                "SUCCESS": ContextRefreshStatus.SUCCESS,
            },
        )
        return ContextRefreshAdapterResult(
            status=status,
            usable_context=bool(result.indicator_snapshots),
            adapter_state={},
            native_result=result,
            issues=tuple(issues),
        )


class USAspendingRefreshAdapter:
    """Thin coordinator adapter for the existing USAspending collector."""

    source_id = "usaspending"

    def __init__(self, collector: object) -> None:
        self.collector = collector

    def is_enabled(self) -> bool:
        return bool(self.collector.config.enabled)

    def run_once(
        self,
        evaluation_time: datetime,
        source_state: ContextRefreshSourceState,
        *,
        write_questdb: bool,
        questdb_required: bool,
        run_id: str | None,
        session_id: str | None,
    ) -> ContextRefreshAdapterResult:
        del source_state
        result = self.collector.collect(
            evaluation_time=evaluation_time,
            write_questdb=write_questdb,
            questdb_required=questdb_required,
            run_id=run_id,
            session_id=session_id,
        )
        status, issues = _map_native_status(
            source_id=self.source_id,
            native_status=result.status,
            allowed={
                "DISABLED": ContextRefreshStatus.DISABLED,
                "FAILED": ContextRefreshStatus.FAILED,
                "STALE": ContextRefreshStatus.STALE,
                "PARTIAL": ContextRefreshStatus.PARTIAL,
                "SUCCESS": ContextRefreshStatus.SUCCESS,
            },
        )
        return ContextRefreshAdapterResult(
            status=status,
            usable_context=bool(result.indicator_snapshots),
            adapter_state={},
            native_result=result,
            issues=tuple(issues),
        )


class YFinanceRefreshAdapter:
    """Thin coordinator adapter for the existing yfinance development collector."""

    source_id = "yfinance_dev_only"

    def __init__(self, collector: YFinanceProxyCollector) -> None:
        self.collector = collector

    def is_enabled(self) -> bool:
        return bool(self.collector.config.enabled)

    def run_once(
        self,
        evaluation_time: datetime,
        source_state: ContextRefreshSourceState,
        *,
        write_questdb: bool,
        questdb_required: bool,
        run_id: str | None,
        session_id: str | None,
    ) -> ContextRefreshAdapterResult:
        del source_state
        result = self.collector.collect(
            evaluation_time=evaluation_time,
            write_questdb=write_questdb,
            questdb_required=questdb_required,
            run_id=run_id,
            session_id=session_id,
        )
        status, issues = _map_native_status(
            source_id=self.source_id,
            native_status=result.status,
            allowed={
                "DISABLED": ContextRefreshStatus.DISABLED,
                "SUCCESS": ContextRefreshStatus.SUCCESS,
                "PARTIAL": ContextRefreshStatus.PARTIAL,
                "NO_FRESH_DATA": ContextRefreshStatus.NO_FRESH_DATA,
                "FAILED": ContextRefreshStatus.FAILED,
            },
        )
        return ContextRefreshAdapterResult(
            status=status,
            usable_context=bool(result.indicator_snapshots),
            next_due_at=next_yfinance_bar_due_at(
                evaluation_time,
                bar_completion_grace_seconds=self.collector.config.bar_completion_grace_seconds,
            ),
            adapter_state={},
            native_result=result,
            issues=tuple(issues),
        )


def run_due_once(
    evaluation_time: datetime,
    runtime_state: ContextRefreshRuntimeState | None,
    *,
    coordinator: ContextRefreshCoordinator,
    write_questdb: bool = False,
    questdb_required: bool = False,
    run_id: str | None = None,
    session_id: str | None = None,
) -> ContextRefreshRunResult:
    """Thin convenience wrapper around an injected coordinator."""
    return coordinator.run_due_once(
        evaluation_time,
        runtime_state,
        write_questdb=write_questdb,
        questdb_required=questdb_required,
        run_id=run_id,
        session_id=session_id,
    )


def next_yfinance_bar_due_at(
    evaluation_time: datetime,
    *,
    bar_completion_grace_seconds: int,
) -> datetime:
    """Return the next five-minute completed-bar availability boundary."""
    now = ensure_timezone_aware_utc(evaluation_time)
    grace = timedelta(seconds=_non_negative_int(bar_completion_grace_seconds, "bar_completion_grace_seconds"))
    seconds_since_hour = now.minute * 60 + now.second
    boundary_seconds = (seconds_since_hour // INTERVAL_SECONDS) * INTERVAL_SECONDS
    boundary = now.replace(minute=0, second=0, microsecond=0) + timedelta(seconds=boundary_seconds)
    candidate = boundary + grace
    if candidate <= now:
        candidate = boundary + timedelta(seconds=INTERVAL_SECONDS) + grace
    return candidate


def _state_after_adapter_return(
    *,
    previous: ContextRefreshSourceState,
    attempted_at: datetime,
    adapter_result: ContextRefreshAdapterResult,
    next_due_at: datetime,
) -> ContextRefreshSourceState:
    if adapter_result.status is ContextRefreshStatus.FAILED:
        return _copy_source_state(
            previous,
            last_attempted_at=attempted_at,
            last_status=ContextRefreshStatus.FAILED,
            next_due_at=next_due_at,
            consecutive_failure_count=previous.consecutive_failure_count + 1,
            consecutive_non_usable_count=previous.consecutive_non_usable_count + 1,
            last_error_type="AdapterFailed",
            last_error_message="adapter returned FAILED",
            adapter_state=adapter_result.adapter_state,
        )

    last_usable_at = previous.last_usable_at
    non_usable_count = previous.consecutive_non_usable_count + 1
    if adapter_result.usable_context:
        last_usable_at = attempted_at
        non_usable_count = 0
    last_full_success_at = previous.last_full_success_at
    if adapter_result.status is ContextRefreshStatus.SUCCESS:
        last_full_success_at = attempted_at
    return _copy_source_state(
        previous,
        last_attempted_at=attempted_at,
        last_completed_at=attempted_at,
        last_usable_at=last_usable_at,
        last_full_success_at=last_full_success_at,
        last_status=adapter_result.status,
        next_due_at=next_due_at,
        consecutive_failure_count=0,
        consecutive_non_usable_count=non_usable_count,
        last_error_type=None,
        last_error_message=None,
        adapter_state=adapter_result.adapter_state,
    )


def _copy_source_state(
    state: ContextRefreshSourceState,
    **overrides: object,
) -> ContextRefreshSourceState:
    values = {
        "last_attempted_at": state.last_attempted_at,
        "last_completed_at": state.last_completed_at,
        "last_usable_at": state.last_usable_at,
        "last_full_success_at": state.last_full_success_at,
        "last_status": state.last_status,
        "next_due_at": state.next_due_at,
        "consecutive_failure_count": state.consecutive_failure_count,
        "consecutive_non_usable_count": state.consecutive_non_usable_count,
        "last_error_type": state.last_error_type,
        "last_error_message": state.last_error_message,
        "adapter_state": state.adapter_state,
    }
    values.update(overrides)
    return ContextRefreshSourceState(**values)


def _source_is_due(state: ContextRefreshSourceState, evaluation_time: datetime) -> bool:
    return (
        state.last_attempted_at is None
        or state.next_due_at is None
        or evaluation_time >= state.next_due_at
    )


def _resolve_next_due(
    *,
    source_id: str,
    hint: datetime | None,
    evaluation_time: datetime,
    fallback_interval_seconds: int,
    issues: list[ContextRefreshIssue],
) -> datetime:
    fallback = evaluation_time + timedelta(seconds=fallback_interval_seconds)
    if hint is None:
        return fallback
    try:
        normalized = ensure_timezone_aware_utc(hint)
    except (TypeError, ValueError):
        issues.append(
            ContextRefreshIssue(
                issue_type="INVALID_NEXT_DUE_HINT",
                source_id=source_id,
                message="adapter returned a non-timezone-aware next_due_at hint",
            )
        )
        return fallback
    if normalized <= evaluation_time:
        issues.append(
            ContextRefreshIssue(
                issue_type="INVALID_NEXT_DUE_HINT",
                source_id=source_id,
                message="adapter returned a next_due_at hint that was not in the future",
                details={"hint": to_utc_iso(normalized)},
            )
        )
        return fallback
    return normalized


def _next_macro_transition(calendar: MacroCalendar, evaluation_time: datetime) -> datetime | None:
    now = ensure_timezone_aware_utc(evaluation_time)
    transitions: list[datetime] = []
    for event in calendar.events:
        if not event.can_be_active:
            continue
        profile = calendar.profile_for(event)
        effective_from = event.effective_from(profile)
        valid_until = event.valid_until(profile)
        if effective_from > now:
            transitions.append(effective_from)
        if effective_from <= now <= valid_until:
            transitions.append(valid_until + timedelta(microseconds=1))
    if not transitions:
        return None
    return min(transitions)


def _next_eia_release_window_start(
    releases: Sequence[object],
    evaluation_time: datetime,
) -> datetime | None:
    now = ensure_timezone_aware_utc(evaluation_time)
    candidates: list[datetime] = []
    for release in releases:
        window_start = getattr(release, "window_start", None)
        if window_start is None:
            continue
        normalized = ensure_timezone_aware_utc(window_start)
        if normalized > now:
            candidates.append(normalized)
    if not candidates:
        return None
    return min(candidates)


def _map_native_status(
    *,
    source_id: str,
    native_status: object,
    allowed: Mapping[str, ContextRefreshStatus],
) -> tuple[ContextRefreshStatus, list[ContextRefreshIssue]]:
    value = getattr(native_status, "value", native_status)
    if isinstance(value, str) and value in allowed:
        return allowed[value], []
    message = f"unsupported native status {value!r}"
    return ContextRefreshStatus.FAILED, [
        ContextRefreshIssue(
            issue_type="UNSUPPORTED_NATIVE_STATUS",
            source_id=source_id,
            message=message,
        )
    ]


def _adapter_state_datetime(mapping: Mapping[str, object], key: str) -> datetime | None:
    value = mapping.get(key)
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_timezone_aware_utc(value)
    if isinstance(value, str):
        return parse_utc_iso(value)
    raise ContextRefreshError(f"adapter_state.{key} must be an ISO UTC string")


def _adapter_state_date(mapping: Mapping[str, object], key: str) -> date | None:
    value = mapping.get(key)
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ContextRefreshError(f"adapter_state.{key} must be an ISO date") from exc
    raise ContextRefreshError(f"adapter_state.{key} must be an ISO date string")


def _validate_exact_source_order(order: tuple[str, ...]) -> None:
    duplicates = sorted({item for item in order if order.count(item) > 1})
    if duplicates:
        raise ContextRefreshError(f"duplicate source_order entry: {duplicates[0]}")
    if set(order) != set(SUPPORTED_SOURCE_IDS) or len(order) != len(SUPPORTED_SOURCE_IDS):
        missing = sorted(set(SUPPORTED_SOURCE_IDS).difference(order))
        unexpected = sorted(set(order).difference(SUPPORTED_SOURCE_IDS))
        if missing:
            raise ContextRefreshError(f"missing source_order entry: {missing[0]}")
        raise ContextRefreshError(f"unsupported source_order entry: {unexpected[0]}")


def _json_safe_mapping(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ContextRefreshError(f"{field_name} must be a mapping")
    try:
        safe = _repo_to_json_dict(dict(value))
        text = json.dumps(safe, allow_nan=False, separators=(",", ":"), sort_keys=True)
        loaded = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ContextRefreshError(f"{field_name} must be JSON-safe") from exc
    if not isinstance(loaded, dict):
        raise ContextRefreshError(f"{field_name} must be a mapping")
    return loaded


def _optional_aware_datetime(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    try:
        return ensure_timezone_aware_utc(value)
    except (TypeError, ValueError) as exc:
        raise ContextRefreshError(f"{field_name} must be timezone-aware") from exc


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextRefreshError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_source_id(value: str | None) -> str | None:
    if value is None:
        return None
    source_id = _required_string(value, "source_id")
    if source_id not in SUPPORTED_SOURCE_IDS:
        raise ContextRefreshError(f"unsupported source id: {source_id}")
    return source_id


def _optional_error_string(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, field_name)


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContextRefreshError(f"{field_name} must be a positive integer")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContextRefreshError(f"{field_name} must be a non-negative integer")
    return value


def _bounded_text(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    if len(text) > MAX_ERROR_MESSAGE_LENGTH:
        return text[: MAX_ERROR_MESSAGE_LENGTH - 3] + "..."
    return text


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else to_utc_iso(value)


def _native_result_summary(value: object | None) -> dict[str, object] | None:
    if value is None:
        return None
    status = getattr(value, "status", None)
    status_value = getattr(status, "value", status)
    return {
        "type": type(value).__name__,
        "status": status_value if isinstance(status_value, str) else None,
    }


__all__ = [
    "ContextRefreshAdapter",
    "ContextRefreshAdapterResult",
    "ContextRefreshCoordinator",
    "ContextRefreshError",
    "ContextRefreshIssue",
    "ContextRefreshPolicy",
    "ContextRefreshRunResult",
    "ContextRefreshRuntimeState",
    "ContextRefreshSourceOutcome",
    "ContextRefreshSourcePolicy",
    "ContextRefreshSourceState",
    "ContextRefreshStatus",
    "DEFAULT_CONFIG_PATH",
    "EIAWPSRRefreshAdapter",
    "FREDRefreshAdapter",
    "MacroCalendarRefreshAdapter",
    "SUPPORTED_SOURCE_IDS",
    "USAspendingRefreshAdapter",
    "YFinanceRefreshAdapter",
    "next_yfinance_bar_due_at",
    "run_due_once",
]
