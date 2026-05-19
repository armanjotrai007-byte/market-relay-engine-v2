"""Validate reusable PR 4 fake fixtures without external services."""

from __future__ import annotations

import ast
import importlib
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.common.serialization import (  # noqa: E402
    from_json_string,
    to_json_dict,
    to_json_string,
)
from tests.fixtures.context import build_context_examples  # noqa: E402
from tests.fixtures.execution import build_execution_examples  # noqa: E402
from tests.fixtures.feature_snapshots import build_feature_snapshot_examples  # noqa: E402
from tests.fixtures.ids import RUN_ID, SESSION_ID, TRACE_ID_APPROVED_OIL  # noqa: E402
from tests.fixtures.ledger import build_ledger_examples  # noqa: E402
from tests.fixtures.market_records import build_market_record_examples  # noqa: E402
from tests.fixtures.model_signals import build_model_signal_examples  # noqa: E402
from tests.fixtures.risk_decisions import build_risk_decision_examples  # noqa: E402
from tests.fixtures.scenarios import build_scenario_examples  # noqa: E402
from tests.fixtures.system import build_system_examples  # noqa: E402


FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
FIXTURE_MODULES = (
    "tests.fixtures",
    "tests.fixtures.ids",
    "tests.fixtures.times",
    "tests.fixtures.market_records",
    "tests.fixtures.feature_snapshots",
    "tests.fixtures.model_signals",
    "tests.fixtures.risk_decisions",
    "tests.fixtures.context",
    "tests.fixtures.execution",
    "tests.fixtures.ledger",
    "tests.fixtures.system",
    "tests.fixtures.scenarios",
)
BANNED_IMPORTS = (
    "databento",
    "alpaca",
    "alpaca_trade_api",
    "questdb",
    "yfinance",
    "requests",
    "urllib",
    "httpx",
    "aiohttp",
    "socket",
    "fredapi",
    "sec_edgar_downloader",
)
DATETIME_FIELD_NAMES = {
    "event_time",
    "source_event_time",
    "local_receive_time",
    "snapshot_time",
    "signal_time",
    "decision_time",
    "order_time",
    "fill_time",
    "entry_time",
    "exit_time",
    "measured_time",
    "write_time",
    "valid_from",
    "valid_until",
}


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def find_banned_imports_in_file(
    path: Path,
    banned_imports: tuple[str, ...] = BANNED_IMPORTS,
) -> list[str]:
    """Return direct banned imports found in one Python file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    banned_roots = set(banned_imports)
    issues: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", maxsplit=1)[0]
                if root in banned_roots:
                    issues.append(f"{_display_path(path)} imports {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".", maxsplit=1)[0]
            if root in banned_roots:
                issues.append(f"{_display_path(path)} imports {module}")

    return issues


def find_banned_imports(
    fixture_dir: Path = FIXTURE_DIR,
    banned_imports: tuple[str, ...] = BANNED_IMPORTS,
) -> list[str]:
    """Return direct banned external-service imports in fixture modules."""
    issues: list[str] = []
    for path in sorted(fixture_dir.glob("*.py")):
        try:
            issues.extend(find_banned_imports_in_file(path, banned_imports))
        except SyntaxError as exc:
            issues.append(f"{_display_path(path)} could not be parsed: {exc}")
    return issues


def build_fixture_examples_by_category() -> dict[str, list[Any]]:
    """Return representative examples from every fixture category."""
    return {
        "ids": [
            {
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "trace_id": TRACE_ID_APPROVED_OIL,
            }
        ],
        "market_records": build_market_record_examples(),
        "feature_snapshots": build_feature_snapshot_examples(),
        "model_signals": build_model_signal_examples(),
        "risk_decisions": build_risk_decision_examples(),
        "context": build_context_examples(),
        "execution": build_execution_examples(),
        "ledger": build_ledger_examples(),
        "system": build_system_examples(),
        "scenarios": build_scenario_examples(),
    }


def _walk_json(value: Any, path: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        entries: list[tuple[str, Any]] = []
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            entries.extend(_walk_json(child, child_path))
        return entries
    if isinstance(value, list):
        entries = []
        for index, child in enumerate(value):
            entries.extend(_walk_json(child, f"{path}[{index}]"))
        return entries
    return [(path, value)]


def _find_datetime_suffix_issues(value: Any) -> list[str]:
    issues: list[str] = []
    for path, child in _walk_json(value):
        field_name = path.rsplit(".", maxsplit=1)[-1]
        if "[" in field_name:
            field_name = field_name.split("[", maxsplit=1)[0]
        if field_name in DATETIME_FIELD_NAMES and child is not None:
            if not isinstance(child, str) or not child.endswith("Z"):
                issues.append(f"{path} is not a UTC Z-suffixed string")
    return issues


def _find_id_issues(value: Any) -> list[str]:
    issues: list[str] = []
    for path, child in _walk_json(value):
        field_name = path.rsplit(".", maxsplit=1)[-1]
        if "[" in field_name:
            field_name = field_name.split("[", maxsplit=1)[0]
        if field_name.endswith("_id") or field_name in {"run_id", "session_id"}:
            if child is not None and (not isinstance(child, str) or not child.strip()):
                issues.append(f"{path} is not a non-empty string")
    return issues


def _validate_serialization(value: Any) -> list[str]:
    issues: list[str] = []
    try:
        json_dict = to_json_dict(value)
        json_string = to_json_string(value)
        json.dumps(json_dict, allow_nan=False)
        parsed = from_json_string(json_string)
    except Exception as exc:  # noqa: BLE001 - check script reports fixture failures.
        return [f"serialization failed: {exc}"]

    if not isinstance(parsed, dict):
        issues.append("serialized value did not parse to a JSON object")
    issues.extend(_find_datetime_suffix_issues(json_dict))
    issues.extend(_find_id_issues(json_dict))
    return issues


def _record(results: list[tuple[bool, str]], ok: bool, message: str) -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {message}")
    results.append((ok, message))


def main() -> int:
    results: list[tuple[bool, str]] = []

    import_errors: list[str] = []
    for module_name in FIXTURE_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - check script reports all imports.
            import_errors.append(f"{module_name}: {exc}")
    _record(results, not import_errors, f"Fixture modules import cleanly: {import_errors or 'ok'}")

    banned_import_issues = find_banned_imports()
    _record(
        results,
        not banned_import_issues,
        f"Fixture modules avoid external-service imports: {banned_import_issues or 'ok'}",
    )

    try:
        examples_by_category = build_fixture_examples_by_category()
        empty_categories = [
            category for category, examples in examples_by_category.items() if not examples
        ]
        _record(
            results,
            not empty_categories,
            f"Fixture examples exist for every category: {empty_categories or 'ok'}",
        )
    except Exception as exc:  # noqa: BLE001 - check script reports fixture construction.
        _record(results, False, f"Fixture construction failed: {exc}")
        examples_by_category = {}

    for category, examples in examples_by_category.items():
        category_issues: list[str] = []
        for index, example in enumerate(examples, start=1):
            for issue in _validate_serialization(example):
                category_issues.append(f"{category}[{index}]: {issue}")
        _record(
            results,
            not category_issues,
            f"{category} fixtures serialize with UTC timestamps and non-empty IDs: "
            f"{category_issues or 'ok'}",
        )

    failures = [message for ok, message in results if not ok]
    print()
    if failures:
        print(f"Fixture validation FAILED with {len(failures)} failure(s).")
        return 1

    print("Fixture validation PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
