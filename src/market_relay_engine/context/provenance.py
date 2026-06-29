"""Shared provenance helpers for structured context details."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import json
from typing import Any

from market_relay_engine.common.serialization import to_json_dict
from market_relay_engine.common.time import (
    ensure_timezone_aware_utc,
    parse_utc_iso,
    to_utc_iso,
)


CONTEXT_PROVENANCE_VERSION = "context_provenance_v1"

_PROVENANCE_KEY = "provenance"
_TIMESTAMP_FIELDS = (
    "source_event_time",
    "source_observed_at",
    "available_at",
    "collected_at",
    "effective_from",
    "valid_until",
)
_PROVENANCE_FIELDS = (
    "provenance_version",
    *_TIMESTAMP_FIELDS,
    "availability_basis",
    "research_asof_eligible",
    "revision_id",
    "vintage_id",
    "source_record_id",
)
_OPTIONAL_STRING_FIELDS = ("revision_id", "vintage_id", "source_record_id")
_AUDIT_ONLY_TOP_LEVEL_FIELDS = ("freshness_seconds", "collector_observed_at")


class ContextProvenanceError(ValueError):
    """Raised when a provenance object is invalid."""


def normalize_provenance(
    provenance: Mapping[str, object] | None = None,
    **overrides: object,
) -> dict[str, object]:
    """Return a strict JSON-safe normalized context provenance object."""
    raw: dict[str, object] = {}
    if provenance is not None:
        if not isinstance(provenance, Mapping):
            raise ContextProvenanceError("provenance must be a mapping")
        raw.update(dict(provenance))
    raw.update(overrides)
    unexpected = sorted(set(raw).difference(_PROVENANCE_FIELDS))
    if unexpected:
        raise ContextProvenanceError(f"unexpected provenance field: {unexpected[0]}")

    if "collected_at" not in raw or raw["collected_at"] is None:
        raise ContextProvenanceError("provenance.collected_at is required")
    availability_basis = _required_string(
        raw.get("availability_basis"),
        "provenance.availability_basis",
    )
    research_asof_eligible = raw.get("research_asof_eligible")
    if not isinstance(research_asof_eligible, bool):
        raise ContextProvenanceError("provenance.research_asof_eligible must be bool")

    normalized: dict[str, object] = {
        "provenance_version": _version(raw.get("provenance_version")),
        "availability_basis": availability_basis,
        "research_asof_eligible": research_asof_eligible,
    }
    for field_name in _TIMESTAMP_FIELDS:
        normalized[field_name] = _normalize_optional_timestamp(
            raw.get(field_name),
            f"provenance.{field_name}",
        )
    for field_name in _OPTIONAL_STRING_FIELDS:
        normalized[field_name] = _optional_string(
            raw.get(field_name),
            f"provenance.{field_name}",
        )
    return _json_safe_object_copy(normalized)


def attach_provenance(
    details: Mapping[str, object] | None,
    provenance: Mapping[str, object],
) -> dict[str, object]:
    """Return details with normalized provenance stored under details['provenance']."""
    if details is None:
        copied: dict[str, object] = {}
    elif isinstance(details, Mapping):
        copied = _json_safe_object_copy(dict(details))
    else:
        raise ContextProvenanceError("details must be a mapping")
    copied[_PROVENANCE_KEY] = normalize_provenance(provenance)
    return _json_safe_object_copy(copied)


def extract_provenance(details: Mapping[str, object] | None) -> dict[str, object] | None:
    """Safely extract normalized persisted provenance, or None when absent/malformed."""
    if not isinstance(details, Mapping):
        return None
    raw = details.get(_PROVENANCE_KEY)
    if not isinstance(raw, Mapping):
        return None
    try:
        return _normalize_persisted_provenance(raw)
    except ContextProvenanceError:
        return None


def is_research_asof_eligible_at(
    details: Mapping[str, object] | None,
    decision_time: datetime,
) -> bool:
    """Return whether details are eligible for research at decision_time."""
    decision = _comparison_time(decision_time)
    provenance = extract_provenance(details)
    if provenance is None or provenance.get("research_asof_eligible") is not True:
        return False
    try:
        available_at = _parse_persisted_timestamp(
            provenance.get("available_at"),
            "provenance.available_at",
        )
    except ContextProvenanceError:
        return False
    if available_at is None:
        return False
    return available_at <= decision


def is_active_in_time_window(
    details: Mapping[str, object] | None,
    decision_time: datetime,
) -> bool:
    """Return whether details are active in their inclusive provenance window."""
    decision = _comparison_time(decision_time)
    provenance = extract_provenance(details)
    if provenance is None:
        return False
    try:
        effective_from = _parse_persisted_timestamp(
            provenance.get("effective_from"),
            "provenance.effective_from",
        )
        valid_until = _parse_persisted_timestamp(
            provenance.get("valid_until"),
            "provenance.valid_until",
        )
    except ContextProvenanceError:
        return False
    if effective_from is None or valid_until is None:
        return False
    return effective_from <= decision <= valid_until


def semantic_details_for_comparison(details: Mapping[str, object]) -> dict[str, object]:
    """Return a JSON-safe details projection that excludes audit-only fields."""
    if not isinstance(details, Mapping):
        raise ContextProvenanceError("details must be a mapping")
    projected = _json_safe_object_copy(dict(details))
    for field_name in _AUDIT_ONLY_TOP_LEVEL_FIELDS:
        projected.pop(field_name, None)
    provenance = projected.get(_PROVENANCE_KEY)
    if isinstance(provenance, dict):
        provenance = dict(provenance)
        provenance.pop("collected_at", None)
        projected[_PROVENANCE_KEY] = provenance
    return _json_safe_object_copy(projected)


def validate_provenance_entry_alignment(
    details: Mapping[str, object],
    source_event_time: datetime | None,
    valid_until: datetime | None,
) -> None:
    """Validate that provenance mirrors ContextStateEntry source and validity times."""
    provenance = _strict_persisted_provenance_from_details(details)
    if provenance is None:
        return
    _assert_timestamp_alignment(
        provenance.get("source_event_time"),
        source_event_time,
        "source_event_time",
    )
    _assert_timestamp_alignment(
        provenance.get("valid_until"),
        valid_until,
        "valid_until",
    )


def validate_snapshot_provenance_alignment(snapshot: object) -> None:
    """Validate that snapshot provenance mirrors snapshot.source_event_time."""
    details = getattr(snapshot, "details", None)
    provenance = _strict_persisted_provenance_from_details(details)
    if provenance is None:
        return
    _assert_timestamp_alignment(
        provenance.get("source_event_time"),
        getattr(snapshot, "source_event_time", None),
        "source_event_time",
    )


def _normalize_persisted_provenance(raw: Mapping[str, object]) -> dict[str, object]:
    if set(raw) != set(_PROVENANCE_FIELDS):
        missing = sorted(set(_PROVENANCE_FIELDS).difference(raw))
        if missing:
            raise ContextProvenanceError(f"missing provenance field: {missing[0]}")
        unexpected = sorted(set(raw).difference(_PROVENANCE_FIELDS))
        raise ContextProvenanceError(f"unexpected provenance field: {unexpected[0]}")
    if raw.get("provenance_version") != CONTEXT_PROVENANCE_VERSION:
        raise ContextProvenanceError("unsupported provenance version")
    if not isinstance(raw.get("availability_basis"), str) or not str(
        raw.get("availability_basis")
    ).strip():
        raise ContextProvenanceError("provenance.availability_basis must be a string")
    if not isinstance(raw.get("research_asof_eligible"), bool):
        raise ContextProvenanceError("provenance.research_asof_eligible must be bool")
    normalized: dict[str, object] = {
        "provenance_version": CONTEXT_PROVENANCE_VERSION,
        "availability_basis": str(raw["availability_basis"]).strip(),
        "research_asof_eligible": raw["research_asof_eligible"],
    }
    for field_name in _TIMESTAMP_FIELDS:
        parsed = _parse_persisted_timestamp(raw.get(field_name), f"provenance.{field_name}")
        normalized[field_name] = None if parsed is None else to_utc_iso(parsed)
    for field_name in _OPTIONAL_STRING_FIELDS:
        normalized[field_name] = _optional_string(
            raw.get(field_name),
            f"provenance.{field_name}",
        )
    return _json_safe_object_copy(normalized)


def _strict_persisted_provenance_from_details(
    details: object,
) -> dict[str, object] | None:
    if not isinstance(details, Mapping) or _PROVENANCE_KEY not in details:
        return None
    raw = details.get(_PROVENANCE_KEY)
    if not isinstance(raw, Mapping):
        raise ContextProvenanceError("details.provenance must be a mapping")
    return _normalize_persisted_provenance(raw)


def _assert_timestamp_alignment(
    persisted_value: object,
    expected: datetime | None,
    field_name: str,
) -> None:
    persisted = _parse_persisted_timestamp(persisted_value, f"provenance.{field_name}")
    expected_utc = None if expected is None else ensure_timezone_aware_utc(expected)
    if persisted != expected_utc:
        raise ContextProvenanceError(
            f"provenance.{field_name} must match record {field_name}"
        )


def _normalize_optional_timestamp(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        try:
            return to_utc_iso(value)
        except (TypeError, ValueError) as exc:
            raise ContextProvenanceError(f"{field_name} must be timezone-aware") from exc
    if isinstance(value, str):
        try:
            return to_utc_iso(parse_utc_iso(value))
        except (TypeError, ValueError) as exc:
            raise ContextProvenanceError(f"{field_name} must be offset-aware ISO") from exc
    raise ContextProvenanceError(f"{field_name} must be a datetime, ISO string, or None")


def _parse_persisted_timestamp(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContextProvenanceError(f"{field_name} must be a UTC ISO string or null")
    try:
        return parse_utc_iso(value)
    except (TypeError, ValueError) as exc:
        raise ContextProvenanceError(f"{field_name} must be offset-aware ISO") from exc


def _comparison_time(value: datetime) -> datetime:
    try:
        return ensure_timezone_aware_utc(value)
    except (TypeError, ValueError) as exc:
        raise ContextProvenanceError("decision_time must be timezone-aware") from exc


def _version(value: object) -> str:
    if value is None:
        return CONTEXT_PROVENANCE_VERSION
    if value != CONTEXT_PROVENANCE_VERSION:
        raise ContextProvenanceError("unsupported provenance version")
    return CONTEXT_PROVENANCE_VERSION


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextProvenanceError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, field_name)


def _json_safe_object_copy(value: Any) -> Any:
    try:
        safe_value = to_json_dict(value)
        encoded = json.dumps(
            safe_value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ContextProvenanceError("value must be JSON-safe") from exc


__all__ = [
    "CONTEXT_PROVENANCE_VERSION",
    "ContextProvenanceError",
    "attach_provenance",
    "extract_provenance",
    "is_active_in_time_window",
    "is_research_asof_eligible_at",
    "normalize_provenance",
    "semantic_details_for_comparison",
    "validate_provenance_entry_alignment",
    "validate_snapshot_provenance_alignment",
]
