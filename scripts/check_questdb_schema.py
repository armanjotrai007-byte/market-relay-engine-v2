"""Validate and optionally apply the QuestDB V2 ledger schema."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
import sys
from typing import Any

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.questdb.health import (  # noqa: E402
    QuestDBHealthConfig,
    QuestDBHealthError,
    build_questdb_exec_url,
    check_questdb_http,
    format_questdb_health_result,
    load_questdb_health_config,
)


SCHEMA_PATH = REPO_ROOT / "db" / "schema" / "questdb_ledger_v1.sql"

OLD_TABLES = (
    "raw_trades",
    "raw_mbp10",
    "raw_ohlcv",
    "raw_bbo",
    "raw_tbbo",
    "databento_definitions",
    "eia_events",
    "sec_events",
    "usaspending_events",
    "macro_timeseries",
    "calendar_events",
    "system_health",
    "ingestion_events",
    "archive_manifest",
    "paper_orders",
    "paper_fills",
    "test_questdb_setup",
)

REQUIRED_TABLES = (
    "bot_runs",
    "bot_sessions",
    "feature_snapshots",
    "model_signals",
    "cost_estimates",
    "context_state_snapshots",
    "risk_decisions",
    "context_indicator_snapshots",
    "context_ai_events",
    "context_flags",
    "order_events",
    "fill_events",
    "trade_outcomes",
    "latency_metrics",
    "system_health_events",
    "ledger_write_errors",
    "jsonl_fallback_events",
)

FORBIDDEN_RAW_TABLES = (
    "raw_trades",
    "raw_bbo",
    "raw_tbbo",
    "raw_ohlcv",
    "raw_mbp10",
    "databento_definitions",
)


class QuestDBSchemaError(RuntimeError):
    """Raised when QuestDB schema validation or apply fails."""


def strip_sql_line_comments(sql_text: str) -> str:
    """Remove full-line SQL comments before simple statement splitting."""
    cleaned_lines: list[str] = []
    for line in sql_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def split_sql_statements(sql_text: str) -> list[str]:
    """Split simple PR12 schema SQL into statements."""
    no_comments = strip_sql_line_comments(sql_text)
    return [
        statement.strip()
        for statement in no_comments.split(";")
        if statement.strip()
    ]


def load_schema_text(schema_path: Path = SCHEMA_PATH) -> str:
    if not schema_path.is_file():
        raise QuestDBSchemaError(f"Schema file not found: {schema_path}")
    return schema_path.read_text(encoding="utf-8")


def validate_schema_text(sql_text: str) -> list[str]:
    """Validate the PR12 QuestDB schema without contacting QuestDB."""
    failures: list[str] = []
    cleaned = strip_sql_line_comments(sql_text)

    if "This file is destructive for development/local setup" not in sql_text:
        failures.append("schema warning is missing destructive local-dev reset text")
    if "Do not run against data you need to preserve" not in sql_text:
        failures.append("schema warning is missing preservation warning")

    created_tables = set(_created_table_names(cleaned))
    missing_tables = [table for table in REQUIRED_TABLES if table not in created_tables]
    if missing_tables:
        failures.append(f"required table CREATE statements missing: {missing_tables}")

    forbidden_creates = [
        table
        for table in FORBIDDEN_RAW_TABLES
        if _contains_create_table(cleaned, table)
    ]
    if forbidden_creates:
        failures.append(f"forbidden raw market-data CREATE statements found: {forbidden_creates}")

    if "context_state_snapshots" not in created_tables:
        failures.append("context_state_snapshots table is missing")

    risk_body = _table_body(cleaned, "risk_decisions")
    if risk_body is None:
        failures.append("risk_decisions table body is missing")
    elif not re.search(r"\bcontext_snapshot_id\s+STRING\b", risk_body, flags=re.IGNORECASE):
        failures.append("risk_decisions.context_snapshot_id STRING is missing")

    if re.search(r"\bTTL\b", cleaned, flags=re.IGNORECASE):
        failures.append("schema must not include TTL clauses")
    if re.search(r"\bINSERT\b", cleaned, flags=re.IGNORECASE):
        failures.append("schema must not include test INSERT statements")
    if re.search(r"\bSELECT\b", cleaned, flags=re.IGNORECASE):
        failures.append("schema must not include SELECT verification queries")

    ordering_failures = _drop_create_order_failures(cleaned)
    failures.extend(ordering_failures)

    return failures


def validate_schema_file(schema_path: Path = SCHEMA_PATH) -> list[str]:
    return validate_schema_text(load_schema_text(schema_path))


def apply_schema_statements(
    config: QuestDBHealthConfig,
    statements: list[str],
    *,
    verify_tables: bool = True,
) -> None:
    """Apply schema statements to QuestDB with one documented GET per statement."""
    if not statements:
        raise QuestDBSchemaError("No SQL statements found to apply")

    for index, statement in enumerate(statements, start=1):
        _exec_questdb_statement(config, statement, statement_index=index)

    if verify_tables:
        verify_expected_tables_exist(config)


def verify_expected_tables_exist(config: QuestDBHealthConfig) -> None:
    payload = _exec_questdb_statement(
        config,
        "SELECT table_name FROM tables()",
        statement_index="verification",
    )
    table_names = _table_names_from_tables_payload(payload)
    missing = [table for table in REQUIRED_TABLES if table not in table_names]
    if missing:
        raise QuestDBSchemaError(f"Schema apply verification missing tables: {missing}")


def run_required_health_check(config: QuestDBHealthConfig) -> None:
    try:
        result = check_questdb_http(config)
    except QuestDBHealthError as exc:
        if exc.result is not None:
            print(format_questdb_health_result(exc.result))
        raise QuestDBSchemaError(f"QuestDB required health check failed: {exc}") from exc
    print(format_questdb_health_result(result))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and optionally apply the QuestDB V2 ledger schema."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the schema to QuestDB after offline validation.",
    )
    parser.add_argument(
        "--required",
        action="store_true",
        help="Require QuestDB to be reachable and healthy for apply mode.",
    )
    parser.add_argument("--host", help="QuestDB HTTP host override.")
    parser.add_argument("--port", help="QuestDB HTTP port override.")
    parser.add_argument("--scheme", help="QuestDB HTTP scheme override.")
    parser.add_argument("--timeout", help="QuestDB health timeout seconds override.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        sql_text = load_schema_text()
        failures = validate_schema_text(sql_text)
        if failures:
            for failure in failures:
                print(f"[FAIL] {failure}")
            print(f"QuestDB schema validation FAILED with {len(failures)} failure(s).")
            return 1

        statements = split_sql_statements(sql_text)
        print(
            "[PASS] QuestDB schema offline validation passed "
            f"({len(REQUIRED_TABLES)} tables, {len(statements)} statements)."
        )

        if not args.apply:
            return 0

        if not args.required:
            print("[FAIL] --apply requires --required for destructive schema reset.")
            return 1

        config = load_questdb_health_config(
            http_scheme=args.scheme,
            http_host=args.host,
            http_port=args.port,
            timeout_seconds=args.timeout,
            required=True,
        )
        run_required_health_check(config)
        apply_schema_statements(config, statements)
        print("[PASS] QuestDB schema apply validation passed.")
        return 0
    except QuestDBSchemaError as exc:
        print(f"[FAIL] QuestDB schema check failed: {exc}")
        return 1


def _exec_questdb_statement(
    config: QuestDBHealthConfig,
    statement: str,
    *,
    statement_index: int | str,
) -> dict[str, Any]:
    if not statement.strip():
        raise QuestDBSchemaError(f"Statement {statement_index} is empty")

    try:
        response = requests.get(
            build_questdb_exec_url(config),
            params={"query": statement, "fmt": "json"},
            timeout=config.timeout_seconds,
        )
    except requests.RequestException as exc:
        raise QuestDBSchemaError(
            f"Statement {statement_index} QuestDB /exec request failed: {exc}"
        ) from exc

    if response.status_code != 200:
        raise QuestDBSchemaError(
            f"Statement {statement_index} returned HTTP {response.status_code}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise QuestDBSchemaError(
            f"Statement {statement_index} returned invalid JSON: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise QuestDBSchemaError(
            f"Statement {statement_index} returned non-object JSON"
        )
    if "error" in payload:
        raise QuestDBSchemaError(
            f"Statement {statement_index} returned QuestDB error: {payload.get('error')}"
        )
    return payload


def _created_table_names(sql_text: str) -> list[str]:
    return [
        match.group(1).lower()
        for match in re.finditer(
            r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Z_][A-Z0-9_]*)\b",
            sql_text,
            flags=re.IGNORECASE,
        )
    ]


def _contains_create_table(sql_text: str, table_name: str) -> bool:
    return bool(
        re.search(
            rf"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{re.escape(table_name)}\b",
            sql_text,
            flags=re.IGNORECASE,
        )
    )


def _table_body(sql_text: str, table_name: str) -> str | None:
    match = re.search(
        rf"\bCREATE\s+TABLE\s+{re.escape(table_name)}\s*\((.*?)\)\s*TIMESTAMP\s*\(",
        sql_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return None
    return match.group(1)


def _drop_create_order_failures(sql_text: str) -> list[str]:
    failures: list[str] = []
    first_create = _first_create_index(sql_text)
    if first_create < 0:
        failures.append("schema has no CREATE TABLE statements")
        return failures

    for table in OLD_TABLES:
        drop_index = _drop_index(sql_text, table)
        if drop_index < 0:
            failures.append(f"old table DROP statement missing: {table}")
        elif drop_index > first_create:
            failures.append(f"old table DROP appears after CREATE statements: {table}")

    for table in REQUIRED_TABLES:
        drop_index = _drop_index(sql_text, table)
        create_index = _create_index(sql_text, table)
        if drop_index < 0:
            failures.append(f"required table DROP statement missing: {table}")
        if create_index < 0:
            failures.append(f"required table CREATE statement missing: {table}")
        if drop_index >= 0 and create_index >= 0 and drop_index > create_index:
            failures.append(f"DROP appears after CREATE for table: {table}")

    return failures


def _first_create_index(sql_text: str) -> int:
    match = re.search(r"\bCREATE\s+TABLE\b", sql_text, flags=re.IGNORECASE)
    return -1 if match is None else match.start()


def _drop_index(sql_text: str, table_name: str) -> int:
    match = re.search(
        rf"\bDROP\s+TABLE\s+IF\s+EXISTS\s+{re.escape(table_name)}\b",
        sql_text,
        flags=re.IGNORECASE,
    )
    return -1 if match is None else match.start()


def _create_index(sql_text: str, table_name: str) -> int:
    match = re.search(
        rf"\bCREATE\s+TABLE\s+{re.escape(table_name)}\b",
        sql_text,
        flags=re.IGNORECASE,
    )
    return -1 if match is None else match.start()


def _table_names_from_tables_payload(payload: dict[str, Any]) -> set[str]:
    columns = payload.get("columns")
    dataset = payload.get("dataset")
    if not isinstance(columns, list) or not isinstance(dataset, list):
        raise QuestDBSchemaError("QuestDB tables() response missing columns or dataset")

    names = [
        str(column.get("name", "")).lower()
        for column in columns
        if isinstance(column, dict)
    ]
    try:
        table_index = names.index("table_name")
    except ValueError:
        try:
            table_index = names.index("name")
        except ValueError as exc:
            raise QuestDBSchemaError(
                "QuestDB tables() response missing table_name column"
            ) from exc

    table_names: set[str] = set()
    for row in dataset:
        if isinstance(row, (list, tuple)) and len(row) > table_index:
            table_names.add(str(row[table_index]))
    return table_names


if __name__ == "__main__":
    raise SystemExit(main())
