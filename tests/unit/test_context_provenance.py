from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
import json

import pytest

from market_relay_engine.context.provenance import (
    CONTEXT_PROVENANCE_VERSION,
    ContextProvenanceError,
    attach_provenance,
    extract_provenance,
    is_active_in_time_window,
    is_research_asof_eligible_at,
    normalize_provenance,
    semantic_details_for_comparison,
)
from market_relay_engine.context.state_cache import make_global_context_entry
from market_relay_engine.contracts.context import ContextIndicatorSnapshot


BASE_TIME = datetime(2026, 6, 20, 16, 0, tzinfo=UTC)
SOURCE_TIME = datetime(2026, 6, 20, 10, 30, tzinfo=timezone(timedelta(hours=-4)))


def _provenance(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "source_event_time": SOURCE_TIME,
        "source_observed_at": None,
        "available_at": datetime(2026, 6, 20, 14, 30, tzinfo=UTC),
        "collected_at": BASE_TIME,
        "effective_from": datetime(2026, 6, 20, 14, 25, tzinfo=UTC),
        "valid_until": datetime(2026, 6, 20, 14, 45, tzinfo=UTC),
        "availability_basis": "official_release_timestamp",
        "research_asof_eligible": True,
        "revision_id": None,
        "vintage_id": None,
        "source_record_id": "source-record-1",
    }
    values.update(overrides)
    return normalize_provenance(values)


def test_normalizes_aware_datetimes_and_offset_aware_strings_to_utc() -> None:
    provenance = _provenance(
        source_event_time="2026-06-20T10:30:00-04:00",
        available_at=datetime(2026, 6, 20, 9, 30, tzinfo=timezone(timedelta(hours=-5))),
    )

    assert provenance["provenance_version"] == CONTEXT_PROVENANCE_VERSION
    assert provenance["source_event_time"] == "2026-06-20T14:30:00Z"
    assert provenance["available_at"] == "2026-06-20T14:30:00Z"
    assert provenance["collected_at"] == "2026-06-20T16:00:00Z"


@pytest.mark.parametrize(
    "field_name",
    ["source_event_time", "available_at", "collected_at", "effective_from", "valid_until"],
)
def test_rejects_naive_timestamps(field_name: str) -> None:
    with pytest.raises(ContextProvenanceError):
        _provenance(**{field_name: datetime(2026, 6, 20, 12, 0)})
    with pytest.raises(ContextProvenanceError):
        _provenance(**{field_name: "2026-06-20T12:00:00"})


def test_json_safe_round_trip_extracts_normalized_provenance() -> None:
    details = attach_provenance({"source": "test"}, _provenance())
    loaded = json.loads(json.dumps(details, sort_keys=True))

    assert extract_provenance(loaded) == details["provenance"]


def test_missing_and_malformed_legacy_provenance_are_safe_and_ineligible() -> None:
    assert extract_provenance({"source": "legacy"}) is None
    assert is_research_asof_eligible_at({"source": "legacy"}, BASE_TIME) is False
    assert is_active_in_time_window({"source": "legacy"}, BASE_TIME) is False

    malformed = {"provenance": {"available_at": "not-a-time"}}
    assert extract_provenance(malformed) is None
    assert is_research_asof_eligible_at(malformed, BASE_TIME) is False
    assert is_active_in_time_window(malformed, BASE_TIME) is False


def test_research_asof_eligibility_requires_all_conditions() -> None:
    eligible = attach_provenance({}, _provenance())

    assert is_research_asof_eligible_at(eligible, datetime(2026, 6, 20, 14, 30, tzinfo=UTC))
    assert is_research_asof_eligible_at(eligible, datetime(2026, 6, 20, 14, 29, 59, tzinfo=UTC)) is False
    assert is_research_asof_eligible_at(
        attach_provenance({}, _provenance(available_at=None)),
        BASE_TIME,
    ) is False
    assert is_research_asof_eligible_at(
        attach_provenance({}, _provenance(research_asof_eligible=False)),
        BASE_TIME,
    ) is False


def test_active_window_inclusive_boundaries() -> None:
    details = attach_provenance({}, _provenance())
    effective = datetime(2026, 6, 20, 14, 25, tzinfo=UTC)
    valid = datetime(2026, 6, 20, 14, 45, tzinfo=UTC)

    assert is_active_in_time_window(details, effective)
    assert is_active_in_time_window(details, valid)
    assert is_active_in_time_window(details, effective - timedelta(microseconds=1)) is False
    assert is_active_in_time_window(details, valid + timedelta(microseconds=1)) is False
    assert is_active_in_time_window(
        attach_provenance({}, _provenance(effective_from=None)),
        effective,
    ) is False
    assert is_active_in_time_window(
        attach_provenance({}, _provenance(valid_until=None)),
        effective,
    ) is False


def test_entry_provenance_alignment_mismatch_is_rejected() -> None:
    details = attach_provenance({}, _provenance())
    with pytest.raises(ContextProvenanceError, match="source_event_time"):
        make_global_context_entry(
            name="bad",
            value=1.0,
            updated_at=BASE_TIME,
            source_event_time=BASE_TIME,
            valid_until=datetime(2026, 6, 20, 14, 45, tzinfo=UTC),
            details=details,
        )


def test_snapshot_provenance_source_time_mismatch_is_rejected() -> None:
    details = attach_provenance({}, _provenance())
    with pytest.raises(ContextProvenanceError, match="source_event_time"):
        ContextIndicatorSnapshot(
            snapshot_time=BASE_TIME,
            source="test",
            ticker_or_sector="GLOBAL",
            indicator_name="metric",
            value=1.0,
            source_event_time=BASE_TIME,
            details=details,
        )


def test_semantic_projection_excludes_only_documented_audit_fields() -> None:
    left = attach_provenance(
        {
            "freshness_seconds": 1.0,
            "collector_observed_at": "2026-06-20T16:00:00Z",
            "forward_outcome_anchor_time": "2026-06-20T15:00:00Z",
        },
        _provenance(collected_at=datetime(2026, 6, 20, 16, 0, tzinfo=UTC)),
    )
    right = attach_provenance(
        {
            "freshness_seconds": 99.0,
            "collector_observed_at": "2026-06-20T17:00:00Z",
            "forward_outcome_anchor_time": "2026-06-20T15:00:00Z",
        },
        _provenance(collected_at=datetime(2026, 6, 20, 17, 0, tzinfo=UTC)),
    )

    assert semantic_details_for_comparison(left) == semantic_details_for_comparison(right)

    changed_available = attach_provenance(
        dict(left),
        _provenance(available_at=datetime(2026, 6, 20, 14, 31, tzinfo=UTC)),
    )
    changed_record = attach_provenance(dict(left), _provenance(source_record_id="other"))

    assert semantic_details_for_comparison(changed_available) != semantic_details_for_comparison(left)
    assert semantic_details_for_comparison(changed_record) != semantic_details_for_comparison(left)
