"""Offline checks for the context refresh coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import json
import re
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.context.refresh_coordinator import (  # noqa: E402
    ContextRefreshAdapterResult,
    ContextRefreshCoordinator,
    ContextRefreshPolicy,
    ContextRefreshRuntimeState,
    ContextRefreshSourceState,
    ContextRefreshStatus,
    SUPPORTED_SOURCE_IDS,
)


CONFIG_PATH = REPO_ROOT / "config" / "context_refresh.yaml"
COORDINATOR_PATH = REPO_ROOT / "src" / "market_relay_engine" / "context" / "refresh_coordinator.py"
FORBIDDEN_IMPORT_PATTERNS = (
    re.compile(r"^\s*import\s+asyncio\b", re.MULTILINE),
    re.compile(r"^\s*from\s+asyncio\b", re.MULTILINE),
    re.compile(r"^\s*import\s+threading\b", re.MULTILINE),
    re.compile(r"^\s*from\s+threading\b", re.MULTILINE),
    re.compile(r"^\s*import\s+requests\b", re.MULTILINE),
    re.compile(r"^\s*from\s+requests\b", re.MULTILINE),
    re.compile(r"^\s*import\s+urllib\.request\b", re.MULTILINE),
    re.compile(r"^\s*from\s+urllib\.request\b", re.MULTILINE),
    re.compile(r"^\s*import\s+yfinance\b", re.MULTILINE),
    re.compile(r"^\s*from\s+yfinance\b", re.MULTILINE),
    re.compile(r"^\s*from\s+market_relay_engine\.(risk|execution|ai_context|model)\b", re.MULTILINE),
    re.compile(r"^\s*import\s+market_relay_engine\.(risk|execution|ai_context|model)\b", re.MULTILINE),
)
FORBIDDEN_TEXT_MARKERS = (
    "QuestDBLedgerReader",
    "check_questdb_http",
    "Alpaca",
    "create_task",
    "sleep(",
)


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    message: str


class _DisabledAdapter:
    source_id = "macro_calendar"

    def __init__(self) -> None:
        self.called = False

    def is_enabled(self) -> bool:
        return False

    def run_once(self, *args: Any, **kwargs: Any) -> ContextRefreshAdapterResult:
        self.called = True
        raise AssertionError("disabled adapter must not be invoked")


class _EnabledAdapter:
    def __init__(self, source_id: str) -> None:
        self.source_id = source_id

    def is_enabled(self) -> bool:
        return True

    def run_once(self, *args: Any, **kwargs: Any) -> ContextRefreshAdapterResult:
        return ContextRefreshAdapterResult(
            status=ContextRefreshStatus.NO_FRESH_DATA,
            usable_context=False,
            native_result={"not": "serialized"},
        )


def _record(results: list[CheckResult], ok: bool, message: str) -> None:
    results.append(CheckResult(ok=ok, message=message))


def _check_config() -> CheckResult:
    try:
        policy = ContextRefreshPolicy.from_yaml(CONFIG_PATH)
    except Exception as exc:  # noqa: BLE001 - checker reports clear failures.
        return CheckResult(False, f"context_refresh config failed validation: {exc}")
    ok = policy.source_order == SUPPORTED_SOURCE_IDS and set(policy.sources) == set(SUPPORTED_SOURCE_IDS)
    return CheckResult(ok, f"context_refresh config source IDs/order valid: {policy.source_order}")


def _check_forbidden_imports() -> CheckResult:
    text = COORDINATOR_PATH.read_text(encoding="utf-8")
    hits = [pattern.pattern for pattern in FORBIDDEN_IMPORT_PATTERNS if pattern.search(text)]
    hits.extend(marker for marker in FORBIDDEN_TEXT_MARKERS if marker in text)
    return CheckResult(not hits, f"coordinator has no forbidden runtime imports/markers: {hits or 'ok'}")


def _check_disabled_adapter() -> CheckResult:
    policy = ContextRefreshPolicy.from_yaml(CONFIG_PATH)
    disabled = _DisabledAdapter()
    adapters = [
        disabled if source_id == disabled.source_id else _EnabledAdapter(source_id)
        for source_id in policy.source_order
    ]
    coordinator = ContextRefreshCoordinator(adapters=adapters, policy=policy)
    result = coordinator.run_due_once(
        datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
        None,
    )
    ok = not disabled.called and disabled.source_id in result.sources_disabled
    return CheckResult(ok, "disabled fake adapter is not invoked")


def _check_statuses() -> CheckResult:
    expected = {
        "DISABLED",
        "SKIPPED_NOT_DUE",
        "SUCCESS",
        "PARTIAL",
        "STALE",
        "NO_FRESH_DATA",
        "DATA_DELAYED",
        "NO_ACTIVE_EVENTS",
        "SUPERSEDED",
        "FAILED",
    }
    actual = {status.value for status in ContextRefreshStatus}
    return CheckResult(actual == expected, f"supported coordinator statuses valid: {sorted(actual)}")


def _check_state_serialization() -> CheckResult:
    state = ContextRefreshRuntimeState(
        sources={
            "macro_calendar": ContextRefreshSourceState(
                last_status=ContextRefreshStatus.SUCCESS,
                last_status_observed_at=datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
                adapter_state={"last_numeric_attempt_at": "2026-01-02T12:00:00Z"}
            )
        }
    )
    try:
        encoded = json.dumps(state.to_json_dict(), allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        return CheckResult(False, f"runtime state JSON projection failed: {exc}")
    ok = "native_result" not in encoded and "last_status_observed_at" in encoded
    return CheckResult(ok, "runtime state projection is JSON-safe and includes status anchor")


def _check_package_imports() -> CheckResult:
    try:
        __import__("market_relay_engine.context.refresh_coordinator")
        __import__("market_relay_engine.context")
    except Exception as exc:  # noqa: BLE001 - checker reports clear failures.
        return CheckResult(False, f"package import failed: {exc}")
    return CheckResult(True, "package imports/load paths succeed")


def run_checks() -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(_check_config())
    _record(results, CONFIG_PATH.is_file(), "config/context_refresh.yaml exists")
    results.append(_check_forbidden_imports())
    results.append(_check_package_imports())
    results.append(_check_disabled_adapter())
    results.append(_check_statuses())
    results.append(_check_state_serialization())
    return results


def main() -> int:
    results = run_checks()
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"[{status}] {result.message}")
    failures = [result for result in results if not result.ok]
    print()
    if failures:
        print(f"Context refresh coordinator validation FAILED with {len(failures)} failure(s).")
        return 1
    print("Context refresh coordinator validation PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
