from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from market_relay_engine.context.macro_calendar import (
    SUPPORTED_EVENT_TYPES,
    MacroCalendarCollectionStatus,
    MacroCalendarCollector,
    MacroCalendarConfig,
    MacroCalendarError,
    active_events_at,
    cache_key_for_event,
    deterministic_calendar_event_id,
    deterministic_context_indicator_id,
    events_between,
    indicator_name_for_event,
    load_macro_calendar,
    validate_macro_calendar,
)
from market_relay_engine.context.provenance import extract_provenance
from market_relay_engine.context.state_cache import (
    ContextStateCache,
    ContextStateUpdateStatus,
    make_global_context_entry,
)
from scripts.check_macro_calendar import run_checks


REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_PATH = REPO_ROOT / "config" / "macro_calendar.yaml"


class FakeWriter:
    def __init__(self) -> None:
        self.snapshots: list[object] = []

    def write_context_indicator_snapshot(self, snapshot: object, **kwargs: object) -> str:
        self.snapshots.append(snapshot)
        return "written"


def _raw_artifact() -> dict[str, Any]:
    loaded = yaml.safe_load(ARTIFACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _event(raw: dict[str, Any], event_type: str) -> dict[str, Any]:
    return next(item for item in raw["events"] if item["event_type"] == event_type)


def _recompute_event_id(raw: dict[str, Any], event: dict[str, Any]) -> None:
    event["calendar_event_id"] = deterministic_calendar_event_id(
        calendar_version=raw["calendar_version"],
        logical_occurrence_id=event["logical_occurrence_id"],
        schedule_revision_id=event["schedule_revision_id"],
    )


def _event_revision(
    raw: dict[str, Any],
    source_event: dict[str, Any],
    *,
    schedule_status: str,
    schedule_revision_id: str,
    scheduled_at: str | None = None,
) -> dict[str, Any]:
    revision = deepcopy(source_event)
    revision["schedule_status"] = schedule_status
    revision["schedule_revision_id"] = schedule_revision_id
    if scheduled_at is not None:
        revision["scheduled_at"] = scheduled_at
    _recompute_event_id(raw, revision)
    return revision


def _single_event_calendar(event_type: str = "CPI") -> dict[str, Any]:
    raw = _raw_artifact()
    selected = deepcopy(_event(raw, event_type))
    raw["events"] = [selected]
    raw["source_manifest"]["coverage"] = {
        event_type: {
            "included_event_count": 1,
            "first_scheduled_at": selected["scheduled_at"],
            "last_scheduled_at": selected["scheduled_at"],
            "source_provider": selected["source_provider"],
            "coverage_note": "test fixture",
        }
    }
    return raw


def _collector(
    raw: dict[str, Any],
    *,
    cache: ContextStateCache | None = None,
    writer: FakeWriter | None = None,
) -> MacroCalendarCollector:
    return MacroCalendarCollector(
        cache=cache or ContextStateCache(),
        config=MacroCalendarConfig(enabled=True),
        calendar=validate_macro_calendar(raw),
        ledger_writer=writer,
        base_dir=REPO_ROOT,
    )


def test_valid_artifact_loads() -> None:
    calendar = load_macro_calendar(ARTIFACT_PATH)

    assert calendar.schema_version == 1
    assert {event.event_type for event in calendar.events} == SUPPORTED_EVENT_TYPES


def test_malformed_schema_is_rejected() -> None:
    raw = _raw_artifact()
    raw.pop("events")

    with pytest.raises(MacroCalendarError, match="missing events"):
        validate_macro_calendar(raw)


def test_naive_timestamps_are_rejected() -> None:
    raw = _single_event_calendar()
    raw["events"][0]["scheduled_at"] = "2026-07-14T12:30:00"

    with pytest.raises(MacroCalendarError, match="Z or \\+00:00"):
        validate_macro_calendar(raw)


def test_non_utc_offset_timestamps_are_rejected_by_schema() -> None:
    raw = _single_event_calendar()
    raw["events"][0]["scheduled_at"] = "2026-07-14T08:30:00-04:00"

    with pytest.raises(MacroCalendarError, match="Z or \\+00:00"):
        validate_macro_calendar(raw)


def test_dst_sensitive_source_text_with_utc_scheduled_at_does_not_drift() -> None:
    calendar = load_macro_calendar(ARTIFACT_PATH)
    employment = next(event for event in calendar.events if event.event_type == "EMPLOYMENT_SITUATION")

    assert employment.source_time_text == "08:30 AM Eastern Time"
    assert employment.scheduled_at == datetime(2026, 7, 2, 12, 30, tzinfo=UTC)


def test_every_supported_event_type_maps_to_valid_tier_and_window_profile() -> None:
    calendar = load_macro_calendar(ARTIFACT_PATH)

    assert set(calendar.event_type_policies) == SUPPORTED_EVENT_TYPES
    for event_type, policy in calendar.event_type_policies.items():
        assert policy.research_tier in {"TIER_1", "TIER_2", "TIER_3"}
        assert policy.window_profile_id in calendar.window_profiles
        assert event_type in SUPPORTED_EVENT_TYPES


def test_eia_petroleum_event_types_are_rejected_and_checker_has_no_excluded_events() -> None:
    raw = _single_event_calendar()
    raw["events"][0]["event_type"] = "EIA_PETROLEUM_RELEASE"

    with pytest.raises(MacroCalendarError):
        validate_macro_calendar(raw)

    assert all(result.ok for result in run_checks())


def test_events_between_uses_start_inclusive_end_exclusive() -> None:
    calendar = load_macro_calendar(ARTIFACT_PATH)
    cpi = next(event for event in calendar.events if event.event_type == "CPI")

    assert cpi in events_between(
        calendar,
        cpi.scheduled_at,
        cpi.scheduled_at + timedelta(seconds=1),
    )
    assert cpi not in events_between(
        calendar,
        cpi.scheduled_at - timedelta(minutes=1),
        cpi.scheduled_at,
    )


def test_active_events_are_inclusive_at_effective_from_and_valid_until() -> None:
    calendar = load_macro_calendar(ARTIFACT_PATH)
    cpi = next(event for event in calendar.events if event.event_type == "CPI")
    profile = calendar.profile_for(cpi)

    assert cpi in active_events_at(calendar, cpi.effective_from(profile))
    assert cpi in active_events_at(calendar, cpi.valid_until(profile))


def test_active_events_are_false_immediately_before_and_after_window() -> None:
    calendar = load_macro_calendar(ARTIFACT_PATH)
    cpi = next(event for event in calendar.events if event.event_type == "CPI")
    profile = calendar.profile_for(cpi)

    assert cpi not in active_events_at(calendar, cpi.effective_from(profile) - timedelta(microseconds=1))
    assert cpi not in active_events_at(calendar, cpi.valid_until(profile) + timedelta(microseconds=1))


def test_overlapping_active_events_coexist_under_different_cache_keys() -> None:
    raw = _raw_artifact()
    cache = ContextStateCache()
    writer = FakeWriter()
    result = _collector(raw, cache=cache, writer=writer).collect_once(
        datetime(2026, 7, 30, 12, 30, tzinfo=UTC),
        write_questdb=True,
    )

    active_types = {event.event_type for event in result.active_events}
    assert {"GDP", "PERSONAL_INCOME_AND_OUTLAYS"}.issubset(active_types)
    keys = [update.key.name for update in result.cache_update_results]
    assert len(keys) == len(set(keys))
    assert len(writer.snapshots) == 2


def test_recurring_occurrences_have_distinct_identities() -> None:
    calendar = load_macro_calendar(ARTIFACT_PATH)
    recurring = [
        event
        for event in calendar.events
        if event.event_type in {"CPI", "FOMC_DECISION"}
    ]

    assert len(recurring) >= 4
    assert len({event.logical_occurrence_id for event in recurring}) == len(recurring)
    assert len({cache_key_for_event(event) for event in recurring}) == len(recurring)
    assert len({indicator_name_for_event(event) for event in recurring}) == len(recurring)
    assert len(
        {
            deterministic_context_indicator_id(event, calendar.profile_for(event))
            for event in recurring
        }
    ) == len(recurring)


def test_repeated_collect_once_ignores_duplicate_and_writes_no_second_ledger_row() -> None:
    cache = ContextStateCache()
    writer = FakeWriter()
    collector = _collector(_single_event_calendar(), cache=cache, writer=writer)
    first = collector.collect_once(
        datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
        write_questdb=True,
    )
    second = collector.collect_once(
        datetime(2026, 7, 14, 12, 31, tzinfo=UTC),
        write_questdb=True,
    )

    assert first.cache_update_results[0].status is ContextStateUpdateStatus.WRITTEN
    assert second.cache_update_results[0].status is ContextStateUpdateStatus.IGNORED_DUPLICATE
    assert len(writer.snapshots) == 1


def test_future_events_do_not_create_cache_or_ledger_writes() -> None:
    cache = ContextStateCache()
    writer = FakeWriter()
    result = _collector(_single_event_calendar(), cache=cache, writer=writer).collect_once(
        datetime(2026, 7, 14, 12, 19, 59, tzinfo=UTC),
        write_questdb=True,
    )

    assert result.status is MacroCalendarCollectionStatus.NO_ACTIVE_EVENTS
    assert result.upcoming
    assert result.cache_update_results == ()
    assert writer.snapshots == []


def test_expired_events_are_hidden_by_normal_cache_expiry() -> None:
    raw = _single_event_calendar()
    cache = ContextStateCache()
    collector = _collector(raw, cache=cache)
    result = collector.collect_once(datetime(2026, 7, 14, 12, 30, tzinfo=UTC))
    event = result.active_events[0]
    key = cache_key_for_event(event)

    assert cache.get_global(key, now=datetime(2026, 7, 14, 12, 45, tzinfo=UTC)) is not None
    assert cache.get_global(key, now=datetime(2026, 7, 14, 12, 45, 0, 1, tzinfo=UTC)) is None


def test_cancelled_events_never_become_active() -> None:
    raw = _single_event_calendar()
    raw["events"][0]["schedule_status"] = "CANCELLED"

    calendar = validate_macro_calendar(raw)
    assert active_events_at(calendar, datetime(2026, 7, 14, 12, 30, tzinfo=UTC)) == ()


def test_superseded_events_revoke_existing_active_cache_entry() -> None:
    raw = _single_event_calendar()
    event = raw["events"][0]
    event["schedule_status"] = "SUPERSEDED"
    event["schedule_revision_id"] = "schedule_rev_2026_06_29_superseded"
    _recompute_event_id(raw, event)
    calendar = validate_macro_calendar(raw)
    superseded = calendar.events[0]
    cache = ContextStateCache()
    cache.update(
        make_global_context_entry(
            name=cache_key_for_event(superseded),
            value=True,
            updated_at=datetime(2026, 7, 14, 12, 20, tzinfo=UTC),
            severity="INFO",
            source="macro_calendar_v1",
            source_event_time=superseded.scheduled_at,
            valid_until=datetime(2026, 7, 14, 12, 45, tzinfo=UTC),
            details={"logical_occurrence_id": superseded.logical_occurrence_id},
        )
    )

    result = MacroCalendarCollector(
        cache=cache,
        config=MacroCalendarConfig(enabled=True),
        calendar=calendar,
    ).collect_once(datetime(2026, 7, 14, 12, 30, tzinfo=UTC))
    stored = cache.get_global(
        cache_key_for_event(superseded),
        now=superseded.schedule_captured_at,
        include_expired=True,
    )

    assert result.cache_update_results[-1].status is ContextStateUpdateStatus.REPLACED
    assert stored is not None
    assert stored.value is False


def test_current_revision_survives_old_inactive_revision_after_it() -> None:
    raw = _single_event_calendar()
    base = raw["events"][0]
    current = _event_revision(
        raw,
        base,
        schedule_status="CONFIRMED",
        schedule_revision_id="schedule_rev_current",
    )
    prior = _event_revision(
        raw,
        base,
        schedule_status="SUPERSEDED",
        schedule_revision_id="schedule_rev_prior",
    )
    raw["events"] = [current, prior]
    cache = ContextStateCache()
    writer = FakeWriter()

    result = _collector(raw, cache=cache, writer=writer).collect_once(
        datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
        write_questdb=True,
    )
    entry = cache.get_global(
        f"macro_calendar:active:{current['logical_occurrence_id']}",
        now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
    )

    assert entry is not None
    assert entry.value is True
    assert entry.details["schedule_revision_id"] == "schedule_rev_current"
    assert entry.details["calendar_event_id"] == current["calendar_event_id"]
    assert [update.status for update in result.cache_update_results] == [
        ContextStateUpdateStatus.WRITTEN
    ]
    assert len(writer.snapshots) == 1


def test_current_revision_collection_is_order_independent() -> None:
    raw = _single_event_calendar()
    base = raw["events"][0]
    current = _event_revision(
        raw,
        base,
        schedule_status="CONFIRMED",
        schedule_revision_id="schedule_rev_current",
    )
    prior = _event_revision(
        raw,
        base,
        schedule_status="SUPERSEDED",
        schedule_revision_id="schedule_rev_prior",
    )
    states: list[tuple[bool, str, int]] = []
    for ordered_events in ([current, prior], [prior, current]):
        candidate = deepcopy(raw)
        candidate["events"] = deepcopy(ordered_events)
        cache = ContextStateCache()
        writer = FakeWriter()
        _collector(candidate, cache=cache, writer=writer).collect_once(
            datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
            write_questdb=True,
        )
        entry = cache.get_global(
            f"macro_calendar:active:{current['logical_occurrence_id']}",
            now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
        )
        assert entry is not None
        states.append(
            (
                entry.value,
                str(entry.details["schedule_revision_id"]),
                len(writer.snapshots),
            )
        )

    assert states == [(True, "schedule_rev_current", 1), (True, "schedule_rev_current", 1)]


def test_future_replacement_revokes_stale_old_active_state_without_ledger_write() -> None:
    cache = ContextStateCache()
    old_calendar = _single_event_calendar()
    old_result = _collector(old_calendar, cache=cache).collect_once(
        datetime(2026, 7, 14, 12, 30, tzinfo=UTC)
    )
    old_event = old_result.active_events[0]
    assert cache.get_global(cache_key_for_event(old_event), now=old_event.scheduled_at) is not None

    updated = _single_event_calendar()
    base = updated["events"][0]
    old_superseded = _event_revision(
        updated,
        base,
        schedule_status="SUPERSEDED",
        schedule_revision_id="schedule_rev_old_superseded",
    )
    future_confirmed = _event_revision(
        updated,
        base,
        schedule_status="CONFIRMED",
        schedule_revision_id="schedule_rev_future_confirmed",
        scheduled_at="2026-07-14T13:30:00Z",
    )
    updated["events"] = [old_superseded, future_confirmed]
    writer = FakeWriter()

    result = _collector(updated, cache=cache, writer=writer).collect_once(
        datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
        write_questdb=True,
    )

    assert result.active_events == ()
    assert cache.get_global(cache_key_for_event(old_event), now=old_event.scheduled_at) is None
    revoked = cache.get_global(
        cache_key_for_event(old_event),
        now=old_event.scheduled_at,
        include_expired=True,
    )
    assert revoked is not None
    assert revoked.value is False
    assert result.indicator_snapshots == ()
    assert writer.snapshots == []


def test_ambiguous_active_revisions_for_same_logical_occurrence_are_invalid() -> None:
    raw = _single_event_calendar()
    base = raw["events"][0]
    confirmed = _event_revision(
        raw,
        base,
        schedule_status="CONFIRMED",
        schedule_revision_id="schedule_rev_confirmed",
    )
    tentative = _event_revision(
        raw,
        base,
        schedule_status="TENTATIVE",
        schedule_revision_id="schedule_rev_tentative",
    )
    raw["events"] = [confirmed, tentative]

    with pytest.raises(MacroCalendarError, match="duplicate active logical_occurrence_id"):
        validate_macro_calendar(raw)


def test_provenance_exists_and_aligns_with_cache_entry_and_snapshot() -> None:
    cache = ContextStateCache()
    result = _collector(_single_event_calendar(), cache=cache).collect_once(
        datetime(2026, 7, 14, 12, 30, tzinfo=UTC)
    )
    event = result.active_events[0]
    entry = cache.get_global(cache_key_for_event(event), now=event.scheduled_at)
    snapshot = result.indicator_snapshots[0]

    assert entry is not None
    provenance = extract_provenance(entry.details)
    assert provenance is not None
    assert provenance["source_event_time"] == "2026-07-14T12:30:00Z"
    assert provenance["valid_until"] == "2026-07-14T12:45:00Z"
    assert entry.source_event_time == snapshot.source_event_time == event.scheduled_at
    assert entry.valid_until == datetime(2026, 7, 14, 12, 45, tzinfo=UTC)


def test_research_asof_eligible_only_when_confirmed_with_verified_publication_time() -> None:
    raw = _single_event_calendar()
    raw["events"][0]["official_schedule_published_at"] = "2026-06-01T00:00:00Z"
    confirmed = _collector(raw).collect_once(datetime(2026, 7, 14, 12, 30, tzinfo=UTC))

    assert confirmed.indicator_snapshots[0].details["provenance"]["research_asof_eligible"] is True

    tentative = _single_event_calendar()
    tentative["events"][0]["schedule_status"] = "TENTATIVE"
    tentative["events"][0]["official_schedule_published_at"] = "2026-06-01T00:00:00Z"
    tentative_result = _collector(tentative).collect_once(datetime(2026, 7, 14, 12, 30, tzinfo=UTC))
    assert tentative_result.indicator_snapshots[0].details["provenance"]["research_asof_eligible"] is False


def test_collected_at_uses_schedule_capture_not_runtime_evaluation_time() -> None:
    result = _collector(_single_event_calendar()).collect_once(
        datetime(2026, 7, 14, 12, 31, tzinfo=UTC)
    )

    assert result.indicator_snapshots[0].details["provenance"]["collected_at"] == "2026-06-29T20:00:00Z"


def test_no_output_details_contain_forbidden_event_window_marker() -> None:
    result = _collector(_single_event_calendar()).collect_once(
        datetime(2026, 7, 14, 12, 30, tzinfo=UTC)
    )

    text = yaml.safe_dump(result.indicator_snapshots[0].details).lower()
    assert "event_window" not in text


def test_cache_severity_is_info() -> None:
    cache = ContextStateCache()
    result = _collector(_single_event_calendar(), cache=cache).collect_once(
        datetime(2026, 7, 14, 12, 30, tzinfo=UTC)
    )
    event = result.active_events[0]
    entry = cache.get_global(cache_key_for_event(event), now=event.scheduled_at)

    assert entry is not None
    assert entry.severity == "INFO"


def test_disabled_config_performs_no_writes_and_no_artifact_load() -> None:
    cache = ContextStateCache()
    result = MacroCalendarCollector(
        cache=cache,
        config=MacroCalendarConfig(enabled=False, artifact_path="missing.yaml"),
    ).collect_once(datetime(2026, 7, 14, 12, 30, tzinfo=UTC))

    assert result.status is MacroCalendarCollectionStatus.DISABLED
    assert result.cache_update_results == ()
    assert cache.latest_global(now=datetime(2026, 7, 14, 12, 30, tzinfo=UTC)) == []


def test_checker_succeeds_against_checked_in_artifact() -> None:
    failures = [result.message for result in run_checks() if not result.ok]

    assert failures == []
