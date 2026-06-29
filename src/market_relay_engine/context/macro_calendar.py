"""Versioned local U.S. macro calendar context collection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Protocol

import yaml

from market_relay_engine.common.config import repo_root
from market_relay_engine.common.time import (
    ensure_timezone_aware_utc,
    parse_utc_iso,
    to_utc_iso,
)
from market_relay_engine.context.provenance import attach_provenance
from market_relay_engine.context.state_cache import (
    ContextStateCache,
    ContextStateEntry,
    ContextStateUpdateResult,
    ContextStateUpdateStatus,
    make_global_context_entry,
)
from market_relay_engine.contracts.context import ContextIndicatorSnapshot


SOURCE_NAME = "macro_calendar_v1"
RESEARCH_WINDOW_KIND = "MACRO_CALENDAR_RESEARCH_WINDOW"
DEFAULT_ARTIFACT_PATH = "config/macro_calendar.yaml"
CALENDAR_EVENT_ID_PREFIX = "macro_calendar_event"
CONTEXT_INDICATOR_ID_PREFIX = "context_indicator"

SUPPORTED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "FOMC_DECISION",
        "CPI",
        "PPI",
        "EMPLOYMENT_SITUATION",
        "JOLTS",
        "PERSONAL_INCOME_AND_OUTLAYS",
        "GDP",
        "RETAIL_SALES",
        "ISM_MANUFACTURING_PMI",
        "ISM_SERVICES_PMI",
        "INITIAL_JOBLESS_CLAIMS",
    }
)
ACTIVE_STATUSES: frozenset[str] = frozenset({"CONFIRMED", "TENTATIVE"})
INACTIVE_STATUSES: frozenset[str] = frozenset({"SUPERSEDED", "CANCELLED"})
SCHEDULE_STATUSES: frozenset[str] = ACTIVE_STATUSES | INACTIVE_STATUSES
RESEARCH_TIERS: frozenset[str] = frozenset({"TIER_1", "TIER_2", "TIER_3"})
FORBIDDEN_EVENT_MARKERS: tuple[str, ...] = ("EIA", "PETROLEUM")
FORBIDDEN_OUTPUT_MARKER = "event_window"
LOGICAL_OCCURRENCE_TIMESTAMP_PATTERN = re.compile(r"\d{8}t\d{6}z")
REQUIRED_TOP_LEVEL_FIELDS: tuple[str, ...] = (
    "schema_version",
    "calendar_version",
    "calendar_captured_at",
    "source_manifest",
    "window_profiles",
    "event_type_policies",
    "events",
)
REQUIRED_EVENT_FIELDS: tuple[str, ...] = (
    "calendar_event_id",
    "logical_occurrence_id",
    "event_type",
    "scheduled_at",
    "source_time_text",
    "schedule_status",
    "source_provider",
    "source_record_id",
    "source_reference",
    "schedule_revision_id",
    "schedule_captured_at",
    "official_schedule_published_at",
    "research_tier",
    "window_profile_id",
)


class MacroCalendarError(ValueError):
    """Raised when the local macro calendar is invalid."""


class MacroCalendarCollectionStatus(str, Enum):
    """Outcome from one explicit macro calendar collection pass."""

    DISABLED = "DISABLED"
    SUCCESS = "SUCCESS"
    NO_ACTIVE_EVENTS = "NO_ACTIVE_EVENTS"
    PARTIAL = "PARTIAL"


@dataclass(frozen=True)
class MacroCalendarIssue:
    """Non-fatal macro calendar collection issue."""

    issue_type: str
    message: str
    logical_occurrence_id: str | None = None
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class MacroWindowProfile:
    """Research-only pre/post window profile."""

    profile_id: str
    pre_event_minutes: int
    post_event_minutes: int


@dataclass(frozen=True)
class MacroEventTypePolicy:
    """Research tier and window mapping for one supported event type."""

    event_type: str
    research_tier: str
    window_profile_id: str


@dataclass(frozen=True)
class MacroCalendarEvent:
    """One reviewed macro calendar occurrence."""

    calendar_event_id: str
    logical_occurrence_id: str
    event_type: str
    scheduled_at: datetime
    source_time_text: str
    schedule_status: str
    source_provider: str
    source_record_id: str
    source_reference: str
    schedule_revision_id: str
    schedule_captured_at: datetime
    official_schedule_published_at: datetime | None
    research_tier: str
    window_profile_id: str

    def effective_from(self, profile: MacroWindowProfile) -> datetime:
        return self.scheduled_at - timedelta(minutes=profile.pre_event_minutes)

    def valid_until(self, profile: MacroWindowProfile) -> datetime:
        return self.scheduled_at + timedelta(minutes=profile.post_event_minutes)

    @property
    def can_be_active(self) -> bool:
        return self.schedule_status in ACTIVE_STATUSES

    @property
    def is_inactive_revision(self) -> bool:
        return self.schedule_status in INACTIVE_STATUSES


@dataclass(frozen=True)
class MacroCalendar:
    """Validated local macro calendar artifact."""

    schema_version: int
    calendar_version: str
    calendar_captured_at: datetime
    source_manifest: dict[str, object]
    window_profiles: dict[str, MacroWindowProfile]
    event_type_policies: dict[str, MacroEventTypePolicy]
    events: tuple[MacroCalendarEvent, ...]

    def profile_for(self, event: MacroCalendarEvent) -> MacroWindowProfile:
        try:
            return self.window_profiles[event.window_profile_id]
        except KeyError as exc:
            raise MacroCalendarError(
                f"unknown window_profile_id for {event.logical_occurrence_id}"
            ) from exc


@dataclass(frozen=True)
class MacroCalendarConfig:
    """Runtime configuration for the local macro calendar collector."""

    enabled: bool = False
    artifact_path: str = DEFAULT_ARTIFACT_PATH
    feeds_memory_cache: bool = True
    writes_questdb_ledger: bool = True
    used_in_per_tick_loop: bool = False
    offline_local_artifact: bool = True
    upcoming_lookahead_days: int = 14

    @classmethod
    def from_repository_config(
        cls,
        context_sources: Mapping[str, Any],
    ) -> "MacroCalendarConfig":
        structured = _required_mapping(context_sources, "structured_sources")
        source = _required_mapping(structured, "macro_calendar")
        return cls(
            enabled=_bool(source.get("enabled", False), "macro_calendar.enabled"),
            artifact_path=_required_string(
                source.get("artifact_path", DEFAULT_ARTIFACT_PATH),
                "macro_calendar.artifact_path",
            ),
            feeds_memory_cache=_bool(
                source.get("feeds_memory_cache", True),
                "macro_calendar.feeds_memory_cache",
            ),
            writes_questdb_ledger=_bool(
                source.get("writes_questdb_ledger", True),
                "macro_calendar.writes_questdb_ledger",
            ),
            used_in_per_tick_loop=_bool(
                source.get("used_in_per_tick_loop", False),
                "macro_calendar.used_in_per_tick_loop",
            ),
            offline_local_artifact=_bool(
                source.get("offline_local_artifact", True),
                "macro_calendar.offline_local_artifact",
            ),
            upcoming_lookahead_days=_non_negative_int(
                source.get("upcoming_lookahead_days", 14),
                "macro_calendar.upcoming_lookahead_days",
            ),
        )


class MacroCalendarLedgerWriter(Protocol):
    """Existing context-indicator writer surface used by the collector."""

    def write_context_indicator_snapshot(
        self,
        snapshot: ContextIndicatorSnapshot,
        **kwargs: Any,
    ) -> object | None:
        """Persist a context indicator snapshot."""


@dataclass(frozen=True)
class MacroCalendarCollectionResult:
    """Result from one explicit macro calendar collection pass."""

    status: MacroCalendarCollectionStatus
    evaluation_time: datetime
    active_events: tuple[MacroCalendarEvent, ...] = ()
    upcoming: tuple[MacroCalendarEvent, ...] = ()
    indicator_snapshots: tuple[ContextIndicatorSnapshot, ...] = ()
    cache_update_results: tuple[ContextStateUpdateResult, ...] = ()
    ledger_write_results: tuple[object, ...] = ()
    issues: tuple[MacroCalendarIssue, ...] = ()


def load_macro_calendar(
    path: str | Path = DEFAULT_ARTIFACT_PATH,
    *,
    base_dir: str | Path | None = None,
) -> MacroCalendar:
    """Load and validate a checked-in local macro calendar artifact."""
    resolved = _resolve_path(path, base_dir=base_dir)
    try:
        loaded = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MacroCalendarError(f"invalid YAML macro calendar: {resolved}") from exc
    except OSError as exc:
        raise MacroCalendarError(f"macro calendar could not be read: {resolved}") from exc
    if not isinstance(loaded, Mapping):
        raise MacroCalendarError("macro calendar must be a top-level mapping")
    return validate_macro_calendar(loaded)


def validate_macro_calendar(calendar: MacroCalendar | Mapping[str, Any]) -> MacroCalendar:
    """Return a normalized calendar or raise on any schema/semantic failure."""
    if isinstance(calendar, MacroCalendar):
        _validate_normalized_calendar(calendar)
        return calendar
    if not isinstance(calendar, Mapping):
        raise MacroCalendarError("macro calendar must be a mapping")

    _validate_top_level_fields(calendar)
    schema_version = _positive_int(calendar["schema_version"], "schema_version")
    calendar_version = _required_string(calendar["calendar_version"], "calendar_version")
    calendar_captured_at = _parse_required_utc(
        calendar["calendar_captured_at"],
        "calendar_captured_at",
    )
    source_manifest = _json_mapping(calendar["source_manifest"], "source_manifest")
    window_profiles = _parse_window_profiles(calendar["window_profiles"])
    policies = _parse_event_type_policies(calendar["event_type_policies"], window_profiles)
    events = tuple(
        _parse_event(
            raw_event,
            calendar_version=calendar_version,
            policies=policies,
            window_profiles=window_profiles,
        )
        for raw_event in _required_sequence(calendar["events"], "events")
    )
    normalized = MacroCalendar(
        schema_version=schema_version,
        calendar_version=calendar_version,
        calendar_captured_at=calendar_captured_at,
        source_manifest=source_manifest,
        window_profiles=window_profiles,
        event_type_policies=policies,
        events=events,
    )
    _validate_normalized_calendar(normalized)
    return normalized


def events_between(
    calendar: MacroCalendar | Mapping[str, Any],
    start: datetime,
    end: datetime,
) -> tuple[MacroCalendarEvent, ...]:
    """Return events whose scheduled_at is in [start, end)."""
    normalized = validate_macro_calendar(calendar)
    start_utc = ensure_timezone_aware_utc(start)
    end_utc = ensure_timezone_aware_utc(end)
    if end_utc < start_utc:
        raise MacroCalendarError("end must be greater than or equal to start")
    return tuple(
        event
        for event in sorted(normalized.events, key=_event_sort_key)
        if start_utc <= event.scheduled_at < end_utc
    )


def upcoming_events(
    calendar: MacroCalendar | Mapping[str, Any],
    evaluation_time: datetime,
    *,
    limit: int | None = None,
    within: timedelta | None = None,
) -> tuple[MacroCalendarEvent, ...]:
    """Return future active-status events without writing cache or ledger state."""
    normalized = validate_macro_calendar(calendar)
    now = ensure_timezone_aware_utc(evaluation_time)
    if limit is not None and limit < 0:
        raise MacroCalendarError("limit must be non-negative")
    end = None if within is None else now + within
    events = [
        event
        for event in sorted(normalized.events, key=_event_sort_key)
        if event.can_be_active
        and event.scheduled_at > now
        and (end is None or event.scheduled_at <= end)
    ]
    if limit is not None:
        events = events[:limit]
    return tuple(events)


def active_events_at(
    calendar: MacroCalendar | Mapping[str, Any],
    evaluation_time: datetime,
) -> tuple[MacroCalendarEvent, ...]:
    """Return active macro events using inclusive window semantics."""
    normalized = validate_macro_calendar(calendar)
    now = ensure_timezone_aware_utc(evaluation_time)
    active: list[MacroCalendarEvent] = []
    for event in normalized.events:
        if not event.can_be_active:
            continue
        profile = normalized.profile_for(event)
        if event.effective_from(profile) <= now <= event.valid_until(profile):
            active.append(event)
    return tuple(sorted(active, key=_event_sort_key))


class MacroCalendarCollector:
    """One-shot collector for reviewed local macro calendar records."""

    def __init__(
        self,
        *,
        cache: ContextStateCache,
        config: MacroCalendarConfig | None = None,
        calendar: MacroCalendar | None = None,
        ledger_writer: MacroCalendarLedgerWriter | None = None,
        base_dir: str | Path | None = None,
    ) -> None:
        self.cache = cache
        self.config = config or MacroCalendarConfig()
        self.calendar = calendar
        self.ledger_writer = ledger_writer
        self.base_dir = base_dir

    def collect_once(
        self,
        evaluation_time: datetime,
        write_questdb: bool = False,
        questdb_required: bool = False,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> MacroCalendarCollectionResult:
        """Collect currently active events once without runtime source I/O."""
        if evaluation_time is None:
            raise MacroCalendarError("evaluation_time is required")
        now = ensure_timezone_aware_utc(evaluation_time)
        if write_questdb and questdb_required and self.ledger_writer is None:
            raise MacroCalendarError("QuestDB writes are required but no writer was provided")
        if not self.config.enabled:
            return MacroCalendarCollectionResult(
                status=MacroCalendarCollectionStatus.DISABLED,
                evaluation_time=now,
            )
        if not self.config.feeds_memory_cache:
            return MacroCalendarCollectionResult(
                status=MacroCalendarCollectionStatus.DISABLED,
                evaluation_time=now,
            )

        calendar = self.calendar or load_macro_calendar(
            self.config.artifact_path,
            base_dir=self.base_dir,
        )
        active = active_events_at(calendar, now)
        currently_active_ids = {event.logical_occurrence_id for event in active}
        inactive_revisions = _inactive_revision_by_logical_id(calendar.events)
        upcoming = upcoming_events(
            calendar,
            now,
            within=timedelta(days=self.config.upcoming_lookahead_days),
        )
        snapshots: list[ContextIndicatorSnapshot] = []
        cache_results: list[ContextStateUpdateResult] = []
        ledger_results: list[object] = []
        issues: list[MacroCalendarIssue] = []

        for event in active:
            snapshot, update = self._publish_active_event(event, calendar)
            cache_results.append(update)
            if update.status is ContextStateUpdateStatus.IGNORED_DUPLICATE:
                existing = self.cache.get_global(
                    cache_key_for_event(event),
                    now=now,
                    include_expired=True,
                )
                if existing is not None:
                    snapshot = replace(snapshot, details=existing.details)
            snapshots.append(snapshot)
            self._write_snapshot_if_changed(
                snapshot,
                update,
                ledger_results,
                issues,
                write_questdb=write_questdb,
                required=questdb_required,
                run_id=run_id,
                session_id=session_id,
            )

        for logical_occurrence_id, event in sorted(inactive_revisions.items()):
            if logical_occurrence_id in currently_active_ids:
                continue
            update = self._revoke_inactive_revision(event, now)
            if update is not None:
                cache_results.append(update)

        status = (
            MacroCalendarCollectionStatus.SUCCESS
            if active or cache_results
            else MacroCalendarCollectionStatus.NO_ACTIVE_EVENTS
        )
        if issues and (active or snapshots):
            status = MacroCalendarCollectionStatus.PARTIAL
        return MacroCalendarCollectionResult(
            status=status,
            evaluation_time=now,
            active_events=active,
            upcoming=upcoming,
            indicator_snapshots=tuple(snapshots),
            cache_update_results=tuple(cache_results),
            ledger_write_results=tuple(ledger_results),
            issues=tuple(issues),
        )

    def _publish_active_event(
        self,
        event: MacroCalendarEvent,
        calendar: MacroCalendar,
    ) -> tuple[ContextIndicatorSnapshot, ContextStateUpdateResult]:
        profile = calendar.profile_for(event)
        effective_from = event.effective_from(profile)
        valid_until = event.valid_until(profile)
        indicator_id = deterministic_context_indicator_id(event, profile)
        details = active_event_details(
            event,
            calendar_version=calendar.calendar_version,
            context_indicator_id=indicator_id,
            effective_from=effective_from,
            valid_until=valid_until,
        )
        indicator_name = indicator_name_for_event(event)
        snapshot = ContextIndicatorSnapshot(
            snapshot_time=effective_from,
            source=SOURCE_NAME,
            ticker_or_sector="GLOBAL",
            indicator_name=indicator_name,
            value=True,
            context_indicator_id=indicator_id,
            window=event.window_profile_id,
            units="boolean",
            freshness_seconds=None,
            source_event_time=event.scheduled_at,
            details=details,
        )
        update = self.cache.update(
            make_global_context_entry(
                name=cache_key_for_event(event),
                value=True,
                updated_at=effective_from,
                severity="INFO",
                source=SOURCE_NAME,
                source_event_time=event.scheduled_at,
                valid_until=valid_until,
                details=details,
            )
        )
        return snapshot, update

    def _revoke_inactive_revision(
        self,
        event: MacroCalendarEvent,
        now: datetime,
    ) -> ContextStateUpdateResult | None:
        existing = self.cache.get_global(
            cache_key_for_event(event),
            now=now,
            include_expired=True,
        )
        if existing is None or existing.value is not True:
            return None
        details = inactive_event_details(event)
        return self.cache.update(
            make_global_context_entry(
                name=cache_key_for_event(event),
                value=False,
                updated_at=existing.updated_at,
                severity="INFO",
                source=SOURCE_NAME,
                source_event_time=event.scheduled_at,
                valid_until=event.schedule_captured_at,
                details=details,
            )
        )

    def _write_snapshot_if_changed(
        self,
        snapshot: ContextIndicatorSnapshot,
        update: ContextStateUpdateResult,
        ledger_results: list[object],
        issues: list[MacroCalendarIssue],
        *,
        write_questdb: bool,
        required: bool,
        run_id: str | None,
        session_id: str | None,
    ) -> None:
        if (
            not write_questdb
            or not self.config.writes_questdb_ledger
            or self.ledger_writer is None
            or update.status
            not in {ContextStateUpdateStatus.WRITTEN, ContextStateUpdateStatus.REPLACED}
        ):
            return
        try:
            result = self.ledger_writer.write_context_indicator_snapshot(
                snapshot,
                run_id=run_id,
                session_id=session_id,
            )
            if result is not None:
                ledger_results.append(result)
        except Exception as exc:  # noqa: BLE001 - writer protocol boundary.
            issues.append(
                MacroCalendarIssue(
                    issue_type="LEDGER_WRITE_FAILED",
                    message="QuestDB context indicator write failed",
                    details={"error_type": type(exc).__name__},
                )
            )
            if required:
                raise MacroCalendarError("QuestDB context indicator write failed") from exc


def cache_key_for_event(event: MacroCalendarEvent) -> str:
    """Return the documented global cache key for one event."""
    return f"macro_calendar:active:{event.logical_occurrence_id}"


def indicator_name_for_event(event: MacroCalendarEvent) -> str:
    """Return the documented context indicator name for one event."""
    return f"macro_calendar_active:{event.event_type}:{event.logical_occurrence_id}"


def deterministic_calendar_event_id(
    *,
    calendar_version: str,
    logical_occurrence_id: str,
    schedule_revision_id: str,
) -> str:
    """Return the stable checked-in calendar event identity."""
    return _deterministic_id(
        CALENDAR_EVENT_ID_PREFIX,
        {
            "calendar_version": calendar_version,
            "logical_occurrence_id": logical_occurrence_id,
            "schedule_revision_id": schedule_revision_id,
        },
    )


def deterministic_context_indicator_id(
    event: MacroCalendarEvent,
    profile: MacroWindowProfile,
) -> str:
    """Return a stable context indicator id independent of polling time."""
    return _deterministic_id(
        CONTEXT_INDICATOR_ID_PREFIX,
        {
            "logical_occurrence_id": event.logical_occurrence_id,
            "event_type": event.event_type,
            "schedule_revision_id": event.schedule_revision_id,
            "research_tier": event.research_tier,
            "window_profile_id": event.window_profile_id,
            "pre_event_minutes": profile.pre_event_minutes,
            "post_event_minutes": profile.post_event_minutes,
        },
    )


def active_event_details(
    event: MacroCalendarEvent,
    *,
    calendar_version: str,
    context_indicator_id: str,
    effective_from: datetime,
    valid_until: datetime,
) -> dict[str, object]:
    """Return stable research metadata plus PR29 provenance for one active event."""
    details: dict[str, object] = {
        "calendar_event_id": event.calendar_event_id,
        "logical_occurrence_id": event.logical_occurrence_id,
        "event_type": event.event_type,
        "research_tier": event.research_tier,
        "window_profile_id": event.window_profile_id,
        "schedule_status": event.schedule_status,
        "source_provider": event.source_provider,
        "source_record_id": event.source_record_id,
        "source_reference": event.source_reference,
        "schedule_revision_id": event.schedule_revision_id,
        "calendar_version": calendar_version,
        "scheduled_at": to_utc_iso(event.scheduled_at),
        "source_time_text": event.source_time_text,
        "official_schedule_published_at": None
        if event.official_schedule_published_at is None
        else to_utc_iso(event.official_schedule_published_at),
        "effective_from": to_utc_iso(effective_from),
        "valid_until": to_utc_iso(valid_until),
        "context_indicator_id": context_indicator_id,
        "research_window_kind": RESEARCH_WINDOW_KIND,
    }
    _reject_forbidden_output(details)
    return attach_provenance(
        details,
        {
            "source_event_time": event.scheduled_at,
            "source_observed_at": None,
            "available_at": event.official_schedule_published_at,
            "collected_at": event.schedule_captured_at,
            "effective_from": effective_from,
            "valid_until": valid_until,
            "availability_basis": (
                "official_schedule_publication"
                if event.official_schedule_published_at is not None
                else "versioned_local_schedule_unverified"
            ),
            "research_asof_eligible": (
                event.schedule_status == "CONFIRMED"
                and event.official_schedule_published_at is not None
            ),
            "revision_id": event.schedule_revision_id,
            "vintage_id": calendar_version,
            "source_record_id": event.source_record_id,
        },
    )


def inactive_event_details(event: MacroCalendarEvent) -> dict[str, object]:
    """Return metadata for a cancellation/supersession cache revocation."""
    details: dict[str, object] = {
        "calendar_event_id": event.calendar_event_id,
        "logical_occurrence_id": event.logical_occurrence_id,
        "event_type": event.event_type,
        "schedule_status": event.schedule_status,
        "source_provider": event.source_provider,
        "source_record_id": event.source_record_id,
        "source_reference": event.source_reference,
        "schedule_revision_id": event.schedule_revision_id,
        "scheduled_at": to_utc_iso(event.scheduled_at),
        "source_time_text": event.source_time_text,
        "revoked_at": to_utc_iso(event.schedule_captured_at),
        "research_window_kind": RESEARCH_WINDOW_KIND,
    }
    _reject_forbidden_output(details)
    return details


def _validate_top_level_fields(calendar: Mapping[str, Any]) -> None:
    missing = [field_name for field_name in REQUIRED_TOP_LEVEL_FIELDS if field_name not in calendar]
    if missing:
        raise MacroCalendarError(f"macro calendar missing {missing[0]}")
    unexpected = sorted(set(calendar).difference(REQUIRED_TOP_LEVEL_FIELDS))
    if unexpected:
        raise MacroCalendarError(f"unexpected macro calendar field {unexpected[0]}")


def _parse_window_profiles(raw: object) -> dict[str, MacroWindowProfile]:
    mapping = _required_mapping(raw, "window_profiles")
    if set(mapping) != RESEARCH_TIERS:
        raise MacroCalendarError("window_profiles must define exactly TIER_1, TIER_2, TIER_3")
    profiles: dict[str, MacroWindowProfile] = {}
    for profile_id, values in mapping.items():
        profile_values = _required_mapping(values, f"window_profiles.{profile_id}")
        pre = _non_negative_int(
            profile_values.get("pre_event_minutes"),
            f"window_profiles.{profile_id}.pre_event_minutes",
        )
        post = _non_negative_int(
            profile_values.get("post_event_minutes"),
            f"window_profiles.{profile_id}.post_event_minutes",
        )
        profiles[str(profile_id)] = MacroWindowProfile(
            profile_id=str(profile_id),
            pre_event_minutes=pre,
            post_event_minutes=post,
        )
    return profiles


def _parse_event_type_policies(
    raw: object,
    window_profiles: Mapping[str, MacroWindowProfile],
) -> dict[str, MacroEventTypePolicy]:
    mapping = _required_mapping(raw, "event_type_policies")
    if set(mapping) != SUPPORTED_EVENT_TYPES:
        missing = sorted(SUPPORTED_EVENT_TYPES.difference(mapping))
        extra = sorted(set(mapping).difference(SUPPORTED_EVENT_TYPES))
        if missing:
            raise MacroCalendarError(f"missing event_type policy {missing[0]}")
        raise MacroCalendarError(f"unknown event_type policy {extra[0]}")
    policies: dict[str, MacroEventTypePolicy] = {}
    for event_type, values in mapping.items():
        policy_values = _required_mapping(values, f"event_type_policies.{event_type}")
        tier = _required_string(
            policy_values.get("research_tier"),
            f"event_type_policies.{event_type}.research_tier",
        )
        profile_id = _required_string(
            policy_values.get("window_profile_id"),
            f"event_type_policies.{event_type}.window_profile_id",
        )
        if tier not in RESEARCH_TIERS:
            raise MacroCalendarError(f"unknown research tier for {event_type}")
        if profile_id not in window_profiles:
            raise MacroCalendarError(f"unknown window profile for {event_type}")
        policies[str(event_type)] = MacroEventTypePolicy(
            event_type=str(event_type),
            research_tier=tier,
            window_profile_id=profile_id,
        )
    return policies


def _parse_event(
    raw: object,
    *,
    calendar_version: str,
    policies: Mapping[str, MacroEventTypePolicy],
    window_profiles: Mapping[str, MacroWindowProfile],
) -> MacroCalendarEvent:
    event_mapping = _required_mapping(raw, "events[]")
    missing = [field_name for field_name in REQUIRED_EVENT_FIELDS if field_name not in event_mapping]
    if missing:
        raise MacroCalendarError(f"macro calendar event missing {missing[0]}")
    unexpected = sorted(set(event_mapping).difference(REQUIRED_EVENT_FIELDS))
    if unexpected:
        raise MacroCalendarError(f"unexpected macro calendar event field {unexpected[0]}")

    event_type = _required_string(event_mapping["event_type"], "event_type")
    if event_type not in SUPPORTED_EVENT_TYPES:
        raise MacroCalendarError(f"unsupported event_type {event_type}")
    _reject_forbidden_event_text(event_type, "event_type")

    scheduled_at = _parse_required_utc(event_mapping["scheduled_at"], "scheduled_at")
    schedule_captured_at = _parse_required_utc(
        event_mapping["schedule_captured_at"],
        "schedule_captured_at",
    )
    official_schedule_published_at = _parse_optional_utc(
        event_mapping["official_schedule_published_at"],
        "official_schedule_published_at",
    )
    if (
        official_schedule_published_at is not None
        and official_schedule_published_at > schedule_captured_at
    ):
        raise MacroCalendarError(
            "official_schedule_published_at cannot be after schedule_captured_at"
        )

    status = _required_string(event_mapping["schedule_status"], "schedule_status")
    if status not in SCHEDULE_STATUSES:
        raise MacroCalendarError(f"unsupported schedule_status {status}")
    if status == "CONFIRMED" and scheduled_at is None:
        raise MacroCalendarError("CONFIRMED events require scheduled_at")

    source_provider = _required_string(event_mapping["source_provider"], "source_provider")
    source_record_id = _required_string(event_mapping["source_record_id"], "source_record_id")
    source_reference = _required_string(event_mapping["source_reference"], "source_reference")
    _reject_forbidden_event_text(source_provider, "source_provider")
    _reject_forbidden_event_text(source_record_id, "source_record_id")
    _reject_forbidden_event_text(source_reference, "source_reference")

    logical_occurrence_id = _required_string(
        event_mapping["logical_occurrence_id"],
        "logical_occurrence_id",
    )
    schedule_revision_id = _required_string(
        event_mapping["schedule_revision_id"],
        "schedule_revision_id",
    )
    _validate_logical_occurrence_id(
        logical_occurrence_id,
        event_type=event_type,
        scheduled_at=scheduled_at,
        source_record_id=source_record_id,
    )
    expected_event_id = deterministic_calendar_event_id(
        calendar_version=calendar_version,
        logical_occurrence_id=logical_occurrence_id,
        schedule_revision_id=schedule_revision_id,
    )
    calendar_event_id = _required_string(
        event_mapping["calendar_event_id"],
        "calendar_event_id",
    )
    if calendar_event_id != expected_event_id:
        raise MacroCalendarError(
            f"calendar_event_id is not deterministic for {logical_occurrence_id}"
        )

    policy = policies[event_type]
    research_tier = _required_string(event_mapping["research_tier"], "research_tier")
    window_profile_id = _required_string(
        event_mapping["window_profile_id"],
        "window_profile_id",
    )
    if research_tier != policy.research_tier:
        raise MacroCalendarError(f"research_tier mismatch for {event_type}")
    if window_profile_id != policy.window_profile_id:
        raise MacroCalendarError(f"window_profile_id mismatch for {event_type}")
    if window_profile_id not in window_profiles:
        raise MacroCalendarError(f"unknown window_profile_id {window_profile_id}")

    return MacroCalendarEvent(
        calendar_event_id=calendar_event_id,
        logical_occurrence_id=logical_occurrence_id,
        event_type=event_type,
        scheduled_at=scheduled_at,
        source_time_text=_required_string(
            event_mapping["source_time_text"],
            "source_time_text",
        ),
        schedule_status=status,
        source_provider=source_provider,
        source_record_id=source_record_id,
        source_reference=source_reference,
        schedule_revision_id=schedule_revision_id,
        schedule_captured_at=schedule_captured_at,
        official_schedule_published_at=official_schedule_published_at,
        research_tier=research_tier,
        window_profile_id=window_profile_id,
    )


def _validate_normalized_calendar(calendar: MacroCalendar) -> None:
    event_ids: set[str] = set()
    active_logical_ids: set[str] = set()
    if not calendar.events:
        raise MacroCalendarError("events must not be empty")
    for event in calendar.events:
        if event.calendar_event_id in event_ids:
            raise MacroCalendarError(f"duplicate calendar_event_id {event.calendar_event_id}")
        event_ids.add(event.calendar_event_id)
        if event.can_be_active:
            if event.logical_occurrence_id in active_logical_ids:
                raise MacroCalendarError(
                    f"duplicate active logical_occurrence_id {event.logical_occurrence_id}"
                )
            active_logical_ids.add(event.logical_occurrence_id)
        profile = calendar.profile_for(event)
        if event.effective_from(profile) > event.valid_until(profile):
            raise MacroCalendarError(f"invalid window for {event.logical_occurrence_id}")
        details = {
            "cache_key": cache_key_for_event(event),
            "indicator_name": indicator_name_for_event(event),
            "research_window_kind": RESEARCH_WINDOW_KIND,
        }
        _reject_forbidden_output(details)


def _validate_logical_occurrence_id(
    value: str,
    *,
    event_type: str,
    scheduled_at: datetime,
    source_record_id: str,
) -> None:
    text = value.lower()
    provider, _, remainder = value.partition(":")
    if not provider or not remainder:
        raise MacroCalendarError("logical_occurrence_id must include provider and event identity")
    if event_type.lower() not in text:
        raise MacroCalendarError("logical_occurrence_id must include event type")
    if LOGICAL_OCCURRENCE_TIMESTAMP_PATTERN.search(text) is None:
        raise MacroCalendarError("logical_occurrence_id must include a scheduled UTC time")
    canonical_record = _slug(source_record_id)
    if canonical_record and canonical_record not in text:
        raise MacroCalendarError("logical_occurrence_id must include source record identity")


def _parse_required_utc(value: object, field_name: str) -> datetime:
    if value is None:
        raise MacroCalendarError(f"{field_name} is required")
    if not isinstance(value, str) or "T" not in value:
        raise MacroCalendarError(f"{field_name} must be a canonical UTC ISO timestamp")
    if not (value.endswith("Z") or value.endswith("+00:00")):
        raise MacroCalendarError(f"{field_name} must use Z or +00:00 UTC")
    try:
        return parse_utc_iso(value)
    except (TypeError, ValueError) as exc:
        raise MacroCalendarError(f"{field_name} must be an offset-aware UTC ISO timestamp") from exc


def _parse_optional_utc(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _parse_required_utc(value, field_name)


def _resolve_path(path: str | Path, *, base_dir: str | Path | None = None) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    root = Path(base_dir) if base_dir is not None else repo_root()
    return root / candidate


def _required_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MacroCalendarError(f"{field_name} must be a mapping")
    return value


def _json_mapping(value: object, field_name: str) -> dict[str, object]:
    mapping = _required_mapping(value, field_name)
    try:
        encoded = json.dumps(mapping, allow_nan=False, sort_keys=True)
        loaded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise MacroCalendarError(f"{field_name} must be JSON-safe") from exc
    if not isinstance(loaded, dict):
        raise MacroCalendarError(f"{field_name} must be a mapping")
    return loaded


def _required_sequence(value: object, field_name: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise MacroCalendarError(f"{field_name} must be a sequence")
    return value


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MacroCalendarError(f"{field_name} must be a non-empty string")
    return value.strip()


def _bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise MacroCalendarError(f"{field_name} must be bool")
    return value


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MacroCalendarError(f"{field_name} must be a positive integer")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MacroCalendarError(f"{field_name} must be a non-negative integer")
    return value


def _deterministic_id(prefix: str, payload: Mapping[str, object]) -> str:
    text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _compact_utc(value: datetime) -> str:
    return to_utc_iso(value).replace("-", "").replace(":", "").replace(".000000", "")


def _slug(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


def _reject_forbidden_event_text(value: str, field_name: str) -> None:
    normalized = value.upper()
    for marker in FORBIDDEN_EVENT_MARKERS:
        if marker in normalized:
            raise MacroCalendarError(f"{field_name} contains excluded event family {marker}")


def _reject_forbidden_output(details: Mapping[str, object]) -> None:
    encoded = json.dumps(details, ensure_ascii=True, sort_keys=True).lower()
    if FORBIDDEN_OUTPUT_MARKER in encoded:
        raise MacroCalendarError("macro calendar output must not contain event_window")


def _event_sort_key(event: MacroCalendarEvent) -> tuple[datetime, str]:
    return (event.scheduled_at, event.logical_occurrence_id)


def _inactive_revision_by_logical_id(
    events: Sequence[MacroCalendarEvent],
) -> dict[str, MacroCalendarEvent]:
    revisions: dict[str, MacroCalendarEvent] = {}
    for event in events:
        if not event.is_inactive_revision:
            continue
        existing = revisions.get(event.logical_occurrence_id)
        if existing is None or (
            event.schedule_captured_at,
            event.schedule_revision_id,
            event.calendar_event_id,
        ) > (
            existing.schedule_captured_at,
            existing.schedule_revision_id,
            existing.calendar_event_id,
        ):
            revisions[event.logical_occurrence_id] = event
    return revisions


__all__ = [
    "ACTIVE_STATUSES",
    "DEFAULT_ARTIFACT_PATH",
    "INACTIVE_STATUSES",
    "MacroCalendar",
    "MacroCalendarCollectionResult",
    "MacroCalendarCollectionStatus",
    "MacroCalendarCollector",
    "MacroCalendarConfig",
    "MacroCalendarError",
    "MacroCalendarEvent",
    "MacroCalendarIssue",
    "MacroCalendarLedgerWriter",
    "MacroEventTypePolicy",
    "MacroWindowProfile",
    "RESEARCH_WINDOW_KIND",
    "SCHEDULE_STATUSES",
    "SOURCE_NAME",
    "SUPPORTED_EVENT_TYPES",
    "active_event_details",
    "active_events_at",
    "cache_key_for_event",
    "deterministic_calendar_event_id",
    "deterministic_context_indicator_id",
    "events_between",
    "inactive_event_details",
    "indicator_name_for_event",
    "load_macro_calendar",
    "upcoming_events",
    "validate_macro_calendar",
]
