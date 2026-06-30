"""Validate the checked-in macro calendar artifact offline."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.common.time import to_utc_iso  # noqa: E402
from market_relay_engine.context.macro_calendar import (  # noqa: E402
    SUPPORTED_EVENT_TYPES,
    MacroCalendar,
    MacroCalendarError,
    active_events_at,
    cache_key_for_event,
    indicator_name_for_event,
    load_macro_calendar,
)


ARTIFACT_PATH = REPO_ROOT / "config" / "macro_calendar.yaml"
RUNTIME_MODULE_PATH = REPO_ROOT / "src" / "market_relay_engine" / "context" / "macro_calendar.py"
FORBIDDEN_IMPORT_PATTERNS = (
    re.compile(r"^\s*import\s+requests\b", re.MULTILINE),
    re.compile(r"^\s*from\s+requests\b", re.MULTILINE),
    re.compile(r"^\s*import\s+yfinance\b", re.MULTILINE),
    re.compile(r"^\s*from\s+yfinance\b", re.MULTILINE),
    re.compile(r"^\s*import\s+selenium\b", re.MULTILINE),
    re.compile(r"^\s*from\s+selenium\b", re.MULTILINE),
    re.compile(r"^\s*import\s+playwright\b", re.MULTILINE),
    re.compile(r"^\s*from\s+playwright\b", re.MULTILINE),
    re.compile(r"^\s*import\s+urllib\.request\b", re.MULTILINE),
    re.compile(r"^\s*from\s+urllib\.request\b", re.MULTILINE),
)


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    message: str


def _record(results: list[CheckResult], ok: bool, message: str) -> None:
    results.append(CheckResult(ok=ok, message=message))


def _check_loader() -> tuple[MacroCalendar | None, CheckResult]:
    try:
        calendar = load_macro_calendar(ARTIFACT_PATH)
    except Exception as exc:  # noqa: BLE001 - validation script reports clear failures.
        return None, CheckResult(False, f"Artifact failed runtime loader/validator: {exc}")
    return calendar, CheckResult(True, "Artifact loads and passes runtime validation")


def _check_no_forbidden_imports() -> CheckResult:
    text = RUNTIME_MODULE_PATH.read_text(encoding="utf-8")
    hits = [pattern.pattern for pattern in FORBIDDEN_IMPORT_PATTERNS if pattern.search(text)]
    return CheckResult(
        not hits,
        f"Runtime module has no source/network/browser client imports: {hits or 'ok'}",
    )


def _check_manifest_coverage(calendar: MacroCalendar) -> CheckResult:
    coverage = calendar.source_manifest.get("coverage")
    if not isinstance(coverage, dict):
        return CheckResult(False, "source_manifest.coverage must be a mapping")
    event_counts = Counter(event.event_type for event in calendar.events)
    issues: list[str] = []
    if set(coverage) != set(event_counts):
        issues.append(
            "coverage keys must match represented event families: "
            f"coverage={sorted(coverage)} events={sorted(event_counts)}"
        )
    for event_type, count in sorted(event_counts.items()):
        entry = coverage.get(event_type)
        if not isinstance(entry, dict):
            issues.append(f"{event_type}: missing coverage entry")
            continue
        scheduled_values = sorted(
            to_utc_iso(event.scheduled_at)
            for event in calendar.events
            if event.event_type == event_type
        )
        if entry.get("included_event_count") != count:
            issues.append(f"{event_type}: included_event_count mismatch")
        if entry.get("first_scheduled_at") != scheduled_values[0]:
            issues.append(f"{event_type}: first_scheduled_at mismatch")
        if entry.get("last_scheduled_at") != scheduled_values[-1]:
            issues.append(f"{event_type}: last_scheduled_at mismatch")
        providers = {
            event.source_provider
            for event in calendar.events
            if event.event_type == event_type
        }
        if entry.get("source_provider") not in providers:
            issues.append(f"{event_type}: source_provider mismatch")
    missing_supported = sorted(SUPPORTED_EVENT_TYPES.difference(event_counts))
    if missing_supported:
        issues.append(f"missing supported event families: {missing_supported}")
    return CheckResult(
        not issues,
        f"Source manifest coverage matches artifact records: {issues or 'ok'}",
    )


def _check_no_excluded_events(calendar: MacroCalendar) -> CheckResult:
    issues = [
        event.logical_occurrence_id
        for event in calendar.events
        if "EIA" in event.event_type.upper()
        or "PETROLEUM" in event.event_type.upper()
        or "EIA" in event.source_record_id.upper()
        or "PETROLEUM" in event.source_record_id.upper()
    ]
    return CheckResult(not issues, f"No EIA/petroleum macro calendar events: {issues or 'ok'}")


def _check_inactive_records_never_active(calendar: MacroCalendar) -> CheckResult:
    issues: list[str] = []
    for event in calendar.events:
        if event.schedule_status not in {"SUPERSEDED", "CANCELLED"}:
            continue
        active_ids = {
            active.logical_occurrence_id
            for active in active_events_at(calendar, event.scheduled_at)
        }
        if event.logical_occurrence_id in active_ids:
            issues.append(event.logical_occurrence_id)
    return CheckResult(
        not issues,
        f"Cancelled/superseded records cannot become active: {issues or 'ok'}",
    )


def _check_identity_conventions(calendar: MacroCalendar) -> CheckResult:
    issues: list[str] = []
    for event in calendar.events:
        expected_cache_key = f"macro_calendar:active:{event.logical_occurrence_id}"
        expected_indicator_name = (
            f"macro_calendar_active:{event.event_type}:{event.logical_occurrence_id}"
        )
        if cache_key_for_event(event) != expected_cache_key:
            issues.append(f"{event.logical_occurrence_id}: cache key mismatch")
        if indicator_name_for_event(event) != expected_indicator_name:
            issues.append(f"{event.logical_occurrence_id}: indicator name mismatch")
        if "event_window" in expected_cache_key.lower() or "event_window" in expected_indicator_name.lower():
            issues.append(f"{event.logical_occurrence_id}: forbidden event_window marker")
    return CheckResult(not issues, f"Cache key and indicator conventions hold: {issues or 'ok'}")


def run_checks() -> list[CheckResult]:
    results: list[CheckResult] = []
    calendar, loader_result = _check_loader()
    results.append(loader_result)
    _record(results, ARTIFACT_PATH.is_file(), "config/macro_calendar.yaml exists")
    results.append(_check_no_forbidden_imports())
    if calendar is None:
        return results
    results.append(_check_manifest_coverage(calendar))
    results.append(_check_no_excluded_events(calendar))
    results.append(_check_inactive_records_never_active(calendar))
    results.append(_check_identity_conventions(calendar))
    return results


def main() -> int:
    results = run_checks()
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"[{status}] {result.message}")
    failures = [result for result in results if not result.ok]
    print()
    if failures:
        print(f"Macro calendar validation FAILED with {len(failures)} failure(s).")
        return 1
    print("Macro calendar validation PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
