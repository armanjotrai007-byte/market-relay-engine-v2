"""Offline validation for decision-time context assembly."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import builtins
import json
import re
import socket
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.context import DecisionContextAssembler  # noqa: E402
from market_relay_engine.context.decision_context import (  # noqa: E402
    KNOWN_SOURCE_CLASSIFICATION,
    SUPPORTED_REFRESH_SOURCE_IDS,
    DecisionContextPolicy,
)
from market_relay_engine.context.refresh_coordinator import (  # noqa: E402
    ContextRefreshRuntimeState,
    ContextRefreshSourceState,
    ContextRefreshStatus,
)
from market_relay_engine.context.state_cache import (  # noqa: E402
    ContextStateCache,
    make_global_context_entry,
)


DECISION_CONTEXT_PATH = REPO_ROOT / "src" / "market_relay_engine" / "context" / "decision_context.py"
EXPECTED_SOURCE_NAMES = {
    "macro_calendar_v1",
    "eia_wpsr_v1",
    "fred_rates_v1",
    "usaspending_awards_v1",
    "yfinance_dev_raw_v1",
}
FORBIDDEN_IMPORT_PATTERNS = (
    re.compile(r"^\s*import\s+requests\b", re.MULTILINE),
    re.compile(r"^\s*from\s+requests\b", re.MULTILINE),
    re.compile(r"^\s*import\s+urllib\b", re.MULTILINE),
    re.compile(r"^\s*from\s+urllib\b", re.MULTILINE),
    re.compile(r"^\s*import\s+socket\b", re.MULTILINE),
    re.compile(r"^\s*from\s+socket\b", re.MULTILINE),
    re.compile(r"^\s*import\s+threading\b", re.MULTILINE),
    re.compile(r"^\s*from\s+threading\b", re.MULTILINE),
    re.compile(r"^\s*import\s+asyncio\b", re.MULTILINE),
    re.compile(r"^\s*from\s+asyncio\b", re.MULTILINE),
    re.compile(r"^\s*from\s+market_relay_engine\.context\.(eia_wpsr|fred_collector|macro_calendar|usaspending_collector|yfinance_proxy)\b", re.MULTILINE),
    re.compile(r"^\s*from\s+market_relay_engine\.(questdb|risk|execution|ai_context|model)\b", re.MULTILINE),
    re.compile(r"^\s*import\s+market_relay_engine\.(questdb|risk|execution|ai_context|model)\b", re.MULTILINE),
)
FORBIDDEN_TEXT_MARKERS = (
    "QuestDB",
    "ContextRefreshCoordinator(",
    "run_due_once(",
    "ContextFlag",
    "ContextAIEvent",
)


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    message: str


def _fixture_context(
    *,
    trace_id: str = "trace_check",
    state: ContextRefreshRuntimeState | None = None,
) -> object:
    cache = ContextStateCache()
    cache.update(
        make_global_context_entry(
            name="fixture_global",
            value="ok",
            source="macro_calendar_v1",
            updated_at=datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
            details={"nested": {"value": "original"}},
        )
    )
    return DecisionContextAssembler(cache=cache).build_for_decision(
        "XOM",
        datetime(2026, 1, 2, 12, 5, tzinfo=UTC),
        trace_id,
        state,
        ticker_sector=None,
    )


def _check_package_imports() -> CheckResult:
    try:
        from market_relay_engine.context import (  # noqa: F401
            DecisionContext,
            DecisionContextAuditPayload,
            DecisionContextEntry,
            DecisionContextPolicy,
            SourceReadiness,
        )
    except Exception as exc:  # noqa: BLE001 - checker reports all failures.
        return CheckResult(False, f"decision context package exports failed: {exc}")
    return CheckResult(True, "decision context package exports import")


def _check_forbidden_imports() -> CheckResult:
    text = DECISION_CONTEXT_PATH.read_text(encoding="utf-8")
    hits = [pattern.pattern for pattern in FORBIDDEN_IMPORT_PATTERNS if pattern.search(text)]
    hits.extend(marker for marker in FORBIDDEN_TEXT_MARKERS if marker in text)
    return CheckResult(not hits, f"decision_context has no forbidden imports/markers: {hits or 'ok'}")


def _check_source_mapping() -> CheckResult:
    mapped_names = set(KNOWN_SOURCE_CLASSIFICATION)
    refresh_ids = {
        classification["refresh_source_id"]
        for classification in KNOWN_SOURCE_CLASSIFICATION.values()
    }
    known_flags = {
        classification["known_source"]
        for classification in KNOWN_SOURCE_CLASSIFICATION.values()
    }
    ok = (
        mapped_names == EXPECTED_SOURCE_NAMES
        and refresh_ids == set(SUPPORTED_REFRESH_SOURCE_IDS)
        and known_flags == {True}
    )
    return CheckResult(ok, f"known source mapping covers current sources: {sorted(mapped_names)}")


def _check_default_policy() -> CheckResult:
    policy = DecisionContextPolicy()
    context = _fixture_context()
    ok = policy.approved_entry_rules == () and context.approved_risk_context == ()
    return CheckResult(ok, "default policy is deny-all")


def _check_development_only_is_not_approvable() -> CheckResult:
    try:
        DecisionContextPolicy(
            policy_version="unsafe_policy",
            approved_entry_rules=(
                {
                    "source": "yfinance_dev_raw_v1",
                    "cache_scope": "GLOBAL",
                    "cache_name": "yfinance_dev",
                },
            ),
        )
    except Exception as exc:  # noqa: BLE001 - checker reports policy behavior directly.
        rejected = "development-only source cannot be approved" in str(exc)
    else:
        rejected = False

    cache = ContextStateCache()
    cache.update(
        make_global_context_entry(
            name="yfinance_dev",
            value="visible",
            source="yfinance_dev_raw_v1",
            updated_at=datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
        )
    )
    context = DecisionContextAssembler(cache=cache).build_for_decision(
        "XOM",
        datetime(2026, 1, 2, 12, 5, tzinfo=UTC),
        "trace_check",
        None,
        ticker_sector=None,
    )
    entry = context.all_structured_context[0]
    ok = rejected and entry.authority_class == "DEVELOPMENT_ONLY" and context.approved_risk_context == ()
    return CheckResult(ok, "development-only context remains visible but unapproved")


def _check_unknown_source_is_not_approvable() -> CheckResult:
    try:
        DecisionContextPolicy(
            policy_version="unsafe_policy",
            approved_entry_rules=(
                {
                    "source": "fred_rate_v1",
                    "cache_scope": "GLOBAL",
                    "cache_name": "typo",
                },
            ),
        )
    except Exception as exc:  # noqa: BLE001 - checker reports policy behavior directly.
        rejected = "unknown source cannot be approved" in str(exc)
    else:
        rejected = False

    cache = ContextStateCache()
    cache.update(
        make_global_context_entry(
            name="manual",
            value="visible",
            source="manual",
            updated_at=datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
        )
    )
    context = DecisionContextAssembler(cache=cache).build_for_decision(
        "XOM",
        datetime(2026, 1, 2, 12, 5, tzinfo=UTC),
        "trace_check",
        None,
        ticker_sector=None,
    )
    entry = context.all_structured_context[0]
    ok = rejected and entry.source_mode == "UNKNOWN" and entry.authority_class == "RESEARCH_ONLY"
    return CheckResult(ok and context.approved_risk_context == (), "unknown context remains research-only")


def _check_deterministic_fingerprint() -> CheckResult:
    first = _fixture_context()
    second = _fixture_context()
    changed_trace = _fixture_context(trace_id="trace_check_2")
    ok = (
        first.context_fingerprint == second.context_fingerprint
        and first.context_snapshot_id == second.context_snapshot_id
        and changed_trace.context_fingerprint == first.context_fingerprint
        and changed_trace.context_snapshot_id != first.context_snapshot_id
    )
    return CheckResult(ok, "fingerprint and context_snapshot_id are deterministic")


def _check_json_audit_payload() -> CheckResult:
    try:
        context = _fixture_context()
        payload = context.to_audit_payload().to_json_dict()
        encoded = json.dumps(payload, allow_nan=False, sort_keys=True)
        payload["all_structured_context"][0]["details"]["nested"]["value"] = "changed"  # type: ignore[index]
        unchanged = (
            context.to_audit_payload()
            .to_json_dict()["all_structured_context"][0]["details"]["nested"]["value"]  # type: ignore[index]
            == "original"
        )
    except (TypeError, ValueError) as exc:
        return CheckResult(False, f"audit payload JSON serialization failed: {exc}")
    ok = "native_result" not in encoded and "context_snapshot_id" in encoded and unchanged
    return CheckResult(ok, "audit payload is JSON-safe and omits native collector internals")


def _check_no_fixture_external_io() -> CheckResult:
    original_open = builtins.open
    original_socket = socket.socket

    def blocked_open(*args: object, **kwargs: object) -> object:
        raise AssertionError("fixture assembly attempted file I/O")

    def blocked_socket(*args: object, **kwargs: object) -> object:
        raise AssertionError("fixture assembly attempted socket I/O")

    try:
        builtins.open = blocked_open  # type: ignore[assignment]
        socket.socket = blocked_socket  # type: ignore[assignment]
        _fixture_context()
    except AssertionError as exc:
        return CheckResult(False, str(exc))
    finally:
        builtins.open = original_open  # type: ignore[assignment]
        socket.socket = original_socket  # type: ignore[assignment]
    return CheckResult(True, "fixture assembly performs no file or socket I/O")


def _check_readiness_safety() -> CheckResult:
    absent = _fixture_context()
    future = _fixture_context(
        state=ContextRefreshRuntimeState(
            sources={
                "macro_calendar": ContextRefreshSourceState(
                    last_status=ContextRefreshStatus.SUCCESS,
                    last_status_observed_at=datetime(2026, 1, 2, 12, 6, tzinfo=UTC),
                    last_completed_at=datetime(2026, 1, 2, 12, 6, tzinfo=UTC),
                    next_due_at=datetime(2026, 1, 2, 13, 0, tzinfo=UTC),
                )
            }
        )
    )
    absent_statuses = {item.refresh_status for item in absent.source_readiness}
    future_macro = future.source_readiness[0]
    ok = (
        absent_statuses == {"UNKNOWN_NOT_REFRESHED"}
        and future_macro.source_id == "macro_calendar"
        and future_macro.refresh_status == "UNKNOWN_NOT_REFRESHED"
        and future_macro.last_completed_at is None
    )
    return CheckResult(ok, "source readiness handles absent and future runtime state")


def run_checks() -> list[CheckResult]:
    return [
        _check_package_imports(),
        _check_forbidden_imports(),
        _check_source_mapping(),
        _check_default_policy(),
        _check_development_only_is_not_approvable(),
        _check_unknown_source_is_not_approvable(),
        _check_deterministic_fingerprint(),
        _check_json_audit_payload(),
        _check_no_fixture_external_io(),
        _check_readiness_safety(),
    ]


def main() -> int:
    results = run_checks()
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"[{status}] {result.message}")
    failures = [result for result in results if not result.ok]
    print()
    if failures:
        print(f"Decision context validation FAILED with {len(failures)} failure(s).")
        return 1
    print("Decision context validation PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
