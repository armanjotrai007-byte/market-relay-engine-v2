"""Validate and optionally apply the QuestDB V2 ledger schema."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
import sys
from typing import Any

import requests
import yaml


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
from market_relay_engine.questdb.writer import (  # noqa: E402
    ALLOWED_LEDGER_TABLES,
    TABLE_COLUMNS,
)


SCHEMA_PATH = REPO_ROOT / "db" / "schema" / "questdb_ledger_v1.sql"
PR26_MIGRATION_PATH = (
    REPO_ROOT / "db" / "schema" / "questdb_pr26_add_context_indicator_details_json.sql"
)
PR34_MIGRATION_PATH = (
    REPO_ROOT / "db" / "schema" / "questdb_pr34_add_phase7_context_ledger.sql"
)
QUESTDB_CONFIG_PATH = REPO_ROOT / "config" / "questdb.yaml"

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
    "context_classification_attempts",
    "shadow_context_policy_evaluations",
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

PR34_EXISTING_TABLE_ADDITIONS = {
    "context_ai_events": (
        ("raw_input_id", "STRING"),
        ("source_document_id", "STRING"),
        ("classification_request_id", "STRING"),
        ("classification_attempt_id", "STRING"),
        ("validation_result_id", "STRING"),
        ("source_type", "SYMBOL"),
        ("source_platform", "SYMBOL"),
        ("source_uri", "STRING"),
        ("source_locator", "STRING"),
        ("document_hash", "STRING"),
        ("source_published_at", "TIMESTAMP"),
        ("source_updated_at", "TIMESTAMP"),
        ("collected_at", "TIMESTAMP"),
        ("normalized_at", "TIMESTAMP"),
        ("classified_at", "TIMESTAMP"),
        ("available_at", "TIMESTAMP"),
        ("validated_at", "TIMESTAMP"),
        ("provider", "SYMBOL"),
    ),
    "context_flags": (
        ("context_event_id", "STRING"),
        ("raw_input_id", "STRING"),
        ("source_document_id", "STRING"),
        ("classification_request_id", "STRING"),
        ("classification_attempt_id", "STRING"),
        ("validation_result_id", "STRING"),
        ("source_type", "SYMBOL"),
        ("source_id", "STRING"),
        ("source_platform", "SYMBOL"),
        ("source_uri", "STRING"),
        ("source_locator", "STRING"),
        ("document_hash", "STRING"),
        ("raw_input_hash", "STRING"),
        ("valid_from", "TIMESTAMP"),
        ("available_at", "TIMESTAMP"),
        ("validated_at", "TIMESTAMP"),
        ("reason_codes_json", "STRING"),
        ("summary", "STRING"),
    ),
}

PR34_NEW_TABLES = (
    "context_classification_attempts",
    "shadow_context_policy_evaluations",
)

PR34_NEW_TABLE_DEFINITIONS = {
    "context_classification_attempts": (
        ("requested_at", "TIMESTAMP"),
        ("write_time", "TIMESTAMP"),
        ("classification_attempt_id", "STRING"),
        ("classification_request_id", "STRING"),
        ("raw_input_id", "STRING"),
        ("source_document_id", "STRING"),
        ("source", "SYMBOL"),
        ("source_type", "SYMBOL"),
        ("source_platform", "SYMBOL"),
        ("source_uri", "STRING"),
        ("source_locator", "STRING"),
        ("affected_tickers_json", "STRING"),
        ("raw_input_hash", "STRING"),
        ("document_hash", "STRING"),
        ("source_published_at", "TIMESTAMP"),
        ("source_updated_at", "TIMESTAMP"),
        ("collected_at", "TIMESTAMP"),
        ("normalized_at", "TIMESTAMP"),
        ("classified_at", "TIMESTAMP"),
        ("provider", "SYMBOL"),
        ("model_version", "SYMBOL"),
        ("prompt_version", "SYMBOL"),
        ("status", "SYMBOL"),
        ("event_type", "SYMBOL"),
        ("risk_level", "SYMBOL"),
        ("urgency", "SYMBOL"),
        ("confidence", "DOUBLE"),
        ("summary", "STRING"),
        ("validation_result_id", "STRING"),
        ("validation_outcome", "BOOLEAN"),
        ("validation_reason_codes_json", "STRING"),
        ("validator_version", "SYMBOL"),
        ("validated_at", "TIMESTAMP"),
        ("provider_latency_ms", "DOUBLE"),
        ("safe_failure_category", "SYMBOL"),
        ("safe_failure_summary", "STRING"),
        ("run_id", "STRING"),
        ("session_id", "STRING"),
        ("schema_version", "SYMBOL"),
        ("trace_id", "STRING"),
    ),
    "shadow_context_policy_evaluations": (
        ("decision_evaluation_time", "TIMESTAMP"),
        ("write_time", "TIMESTAMP"),
        ("shadow_evaluation_id", "STRING"),
        ("model_signal_id", "STRING"),
        ("risk_decision_id", "STRING"),
        ("matched_context_event_ids_json", "STRING"),
        ("matched_context_flag_ids_json", "STRING"),
        ("shadow_context_fingerprint", "STRING"),
        ("policy_version", "SYMBOL"),
        ("policy_config_hash", "STRING"),
        ("hypothetical_action", "SYMBOL"),
        ("proposed_size_factor", "DOUBLE"),
        ("reason_codes_json", "STRING"),
        ("run_id", "STRING"),
        ("session_id", "STRING"),
        ("schema_version", "SYMBOL"),
        ("trace_id", "STRING"),
    ),
}

PR34_DESIGNATED_TIMESTAMPS = {
    "context_classification_attempts": "requested_at",
    "shadow_context_policy_evaluations": "decision_evaluation_time",
}

FORBIDDEN_CONTEXT_LEDGER_COLUMNS = {
    "input_text",
    "source_text",
    "source_body",
    "full_text",
    "raw_document",
    "raw_document_json",
    "raw_prompt",
    "raw_prompt_contents",
    "prompt",
    "prompt_json",
    "prompt_body",
    "prompt_text",
    "prompt_contents",
    "raw_text",
    "document_text",
    "normalized_document",
    "normalized_text",
    "normalized_body",
    "document_body",
    "filing_body",
    "filing_text",
    "article_body",
    "article_text",
    "social_post_body",
    "social_post_text",
    "provider_response",
    "raw_provider_response",
    "exception",
    "exception_text",
    "exception_message",
    "traceback",
    "stack_trace",
    "secret",
    "credential",
    "api_key",
    "access_token",
    "password",
}


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
    """Split the repository's simple schema or migration SQL into statements."""
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
    """Validate the current QuestDB reset schema without contacting QuestDB."""
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

    indicator_body = _table_body(cleaned, "context_indicator_snapshots")
    if indicator_body is None:
        failures.append("context_indicator_snapshots table body is missing")
    elif not re.search(r"\bdetails_json\s+STRING\b", indicator_body, flags=re.IGNORECASE):
        failures.append("context_indicator_snapshots.details_json STRING is missing")

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
    failures.extend(_schema_writer_column_failures(cleaned))
    failures.extend(_context_ledger_raw_text_failures())

    return failures


def validate_schema_file(schema_path: Path = SCHEMA_PATH) -> list[str]:
    return validate_schema_text(load_schema_text(schema_path))


def validate_pr26_migration_file(
    migration_path: Path = PR26_MIGRATION_PATH,
) -> list[str]:
    if not migration_path.is_file():
        return [f"PR26 migration file not found: {migration_path}"]
    text = migration_path.read_text(encoding="utf-8")
    cleaned = strip_sql_line_comments(text).strip()
    expected = "ALTER TABLE context_indicator_snapshots ADD COLUMN details_json STRING;"
    failures: list[str] = []
    if cleaned != expected:
        failures.append("PR26 migration must contain only the details_json ALTER TABLE statement")
    if re.search(r"\b(DROP|CREATE|INSERT|SELECT)\b", cleaned, flags=re.IGNORECASE):
        failures.append("PR26 migration must be non-destructive")
    return failures


def validate_pr34_migration_file(
    migration_path: Path = PR34_MIGRATION_PATH,
) -> list[str]:
    if not migration_path.is_file():
        return [f"PR34 migration file not found: {migration_path}"]
    cleaned = strip_sql_line_comments(
        migration_path.read_text(encoding="utf-8")
    ).strip()
    statements = split_sql_statements(cleaned)
    failures: list[str] = []

    forbidden = re.findall(
        r"\b(DROP|RENAME|TRUNCATE|INSERT|UPDATE|DELETE|SELECT)\b",
        cleaned,
        flags=re.IGNORECASE,
    )
    if forbidden:
        failures.append(
            f"PR34 migration contains destructive or DML operations: {forbidden}"
        )

    expected_alters = [
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {column_type}"
        for table, additions in PR34_EXISTING_TABLE_ADDITIONS.items()
        for column, column_type in additions
    ]
    actual_alters = [
        statement
        for statement in statements
        if re.match(r"^ALTER\s+TABLE\b", statement, flags=re.IGNORECASE)
    ]
    if actual_alters != expected_alters:
        failures.append(
            "PR34 migration ALTER statements must exactly match the ordered, "
            "one-column IF NOT EXISTS additions"
        )

    create_statements = [
        statement
        for statement in statements
        if re.match(r"^CREATE\s+TABLE\b", statement, flags=re.IGNORECASE)
    ]
    if len(create_statements) != len(PR34_NEW_TABLES):
        failures.append("PR34 migration must create exactly the two new ledger tables")
    for table in PR34_NEW_TABLES:
        statement = next(
            (
                candidate
                for candidate in create_statements
                if re.match(
                    rf"^CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(table)}\b",
                    candidate,
                    flags=re.IGNORECASE,
                )
            ),
            None,
        )
        if statement is None:
            failures.append(
                f"PR34 migration missing CREATE TABLE IF NOT EXISTS for {table}"
            )
            continue
        columns = _column_definitions_from_create(statement, table)
        expected_columns = PR34_NEW_TABLE_DEFINITIONS[table]
        if columns != expected_columns:
            failures.append(
                f"PR34 migration columns/types differ from PR34 contract for {table}"
            )
        timestamp = PR34_DESIGNATED_TIMESTAMPS[table]
        if not re.search(
            rf"\)\s*TIMESTAMP\s*\(\s*{re.escape(timestamp)}\s*\)\s*PARTITION\s+BY\s+DAY$",
            statement,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            failures.append(
                f"PR34 migration {table} must designate {timestamp} and partition by DAY"
            )

    if len(statements) != len(expected_alters) + len(PR34_NEW_TABLES):
        failures.append("PR34 migration contains unexpected statements")
    return failures


def validate_questdb_config_table_order(
    config_path: Path = QUESTDB_CONFIG_PATH,
) -> list[str]:
    if not config_path.is_file():
        return [f"QuestDB config file not found: {config_path}"]
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"QuestDB config is invalid YAML: {exc}"]
    if not isinstance(loaded, dict):
        return ["QuestDB config must be a mapping"]
    tables = loaded.get("ledger_tables")
    if tables != list(ALLOWED_LEDGER_TABLES):
        return [
            "config/questdb.yaml ledger_tables must exactly match writer "
            "ALLOWED_LEDGER_TABLES order"
        ]
    return []


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
        failures.extend(validate_pr26_migration_file())
        failures.extend(validate_pr34_migration_file())
        failures.extend(validate_questdb_config_table_order())
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


def _column_definitions_from_create(
    create_statement: str,
    table_name: str,
) -> tuple[tuple[str, str], ...]:
    match = re.search(
        rf"^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{re.escape(table_name)}\s*\((.*?)\)\s*TIMESTAMP\s*\(",
        create_statement,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return ()
    return _parse_column_definitions(match.group(1))


def _table_column_definitions(
    sql_text: str,
    table_name: str,
) -> tuple[tuple[str, str], ...]:
    body = _table_body(sql_text, table_name)
    if body is None:
        return ()
    return _parse_column_definitions(body)


def _parse_column_definitions(body: str) -> tuple[tuple[str, str], ...]:
    definitions: list[tuple[str, str]] = []
    for raw_definition in body.split(","):
        definition = raw_definition.strip()
        match = re.fullmatch(
            r"([A-Z_][A-Z0-9_]*)\s+([A-Z][A-Z0-9_]*)",
            definition,
            flags=re.IGNORECASE,
        )
        if match is None:
            return ()
        definitions.append((match.group(1).lower(), match.group(2).upper()))
    return tuple(definitions)


def _schema_writer_column_failures(sql_text: str) -> list[str]:
    failures: list[str] = []
    for table, expected_columns in TABLE_COLUMNS.items():
        definitions = _table_column_definitions(sql_text, table)
        actual_columns = tuple(column for column, _ in definitions)
        if actual_columns != expected_columns:
            failures.append(
                f"reset schema columns must exactly match writer TABLE_COLUMNS for {table}"
            )

    for table, additions in PR34_EXISTING_TABLE_ADDITIONS.items():
        definitions = _table_column_definitions(sql_text, table)
        if definitions[-len(additions) :] != additions:
            failures.append(
                f"reset schema must append PR34 columns after trace_id for {table}"
            )

    for table, expected_definitions in PR34_NEW_TABLE_DEFINITIONS.items():
        if _table_column_definitions(sql_text, table) != expected_definitions:
            failures.append(
                f"reset schema columns/types differ from PR34 contract for {table}"
            )

    for table, timestamp in PR34_DESIGNATED_TIMESTAMPS.items():
        if not re.search(
            rf"CREATE\s+TABLE\s+{re.escape(table)}\s*\(.*?\)\s*TIMESTAMP\s*\(\s*{re.escape(timestamp)}\s*\)\s*PARTITION\s+BY\s+DAY\s*;",
            sql_text,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            failures.append(
                f"reset schema {table} must designate {timestamp} and partition by DAY"
            )
    return failures


def _context_ledger_raw_text_failures() -> list[str]:
    failures: list[str] = []
    for table in (
        "context_ai_events",
        "context_flags",
        "context_classification_attempts",
        "shadow_context_policy_evaluations",
    ):
        forbidden = FORBIDDEN_CONTEXT_LEDGER_COLUMNS.intersection(
            TABLE_COLUMNS.get(table, ())
        )
        if forbidden:
            failures.append(
                f"{table} contains forbidden raw-text/secret columns: {sorted(forbidden)}"
            )
    return failures


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
