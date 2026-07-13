from __future__ import annotations

from pathlib import Path
import re

import pytest
import requests

from market_relay_engine.questdb.health import QuestDBHealthConfig
from scripts import check_questdb_schema


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "db" / "schema" / "questdb_ledger_v1.sql"
MIGRATION_PATH = REPO_ROOT / "db" / "schema" / "questdb_pr26_add_context_indicator_details_json.sql"
PR34_MIGRATION_PATH = (
    REPO_ROOT / "db" / "schema" / "questdb_pr34_add_phase7_context_ledger.sql"
)
PR35_MIGRATION_PATH = (
    REPO_ROOT
    / "db"
    / "schema"
    / "questdb_pr35_add_context_classification_accounting.sql"
)


class FakeResponse:
    def __init__(self, status_code: int, payload: object | Exception) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_schema_file_exists() -> None:
    assert SCHEMA_PATH.is_file()
    assert MIGRATION_PATH.is_file()
    assert PR34_MIGRATION_PATH.is_file()
    assert PR35_MIGRATION_PATH.is_file()


def test_offline_schema_validation_passes() -> None:
    failures = check_questdb_schema.validate_schema_file(SCHEMA_PATH)

    assert failures == []


def test_required_tables_are_present() -> None:
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    cleaned = check_questdb_schema.strip_sql_line_comments(text)
    created_tables = set(check_questdb_schema._created_table_names(cleaned))

    assert set(check_questdb_schema.REQUIRED_TABLES).issubset(created_tables)


def test_forbidden_raw_market_table_create_statements_are_absent() -> None:
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    cleaned = check_questdb_schema.strip_sql_line_comments(text)

    for table in check_questdb_schema.FORBIDDEN_RAW_TABLES:
        assert not check_questdb_schema._contains_create_table(cleaned, table)


def test_raw_market_table_names_only_appear_in_drop_statements() -> None:
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    cleaned = check_questdb_schema.strip_sql_line_comments(text)
    statements = check_questdb_schema.split_sql_statements(cleaned)

    for table in check_questdb_schema.FORBIDDEN_RAW_TABLES:
        matching = [statement for statement in statements if table in statement]
        assert matching == [f"DROP TABLE IF EXISTS {table}"]


def test_context_state_snapshots_and_risk_context_snapshot_link_exist() -> None:
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    cleaned = check_questdb_schema.strip_sql_line_comments(text)

    assert check_questdb_schema._contains_create_table(cleaned, "context_state_snapshots")
    risk_body = check_questdb_schema._table_body(cleaned, "risk_decisions")
    assert risk_body is not None
    assert "context_snapshot_id STRING" in risk_body


def test_context_indicator_details_schema_and_migration_are_valid() -> None:
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    body = check_questdb_schema._table_body(
        check_questdb_schema.strip_sql_line_comments(text),
        "context_indicator_snapshots",
    )
    assert body is not None
    assert "details_json STRING" in body
    assert check_questdb_schema.validate_pr26_migration_file(MIGRATION_PATH) == []
    migration = MIGRATION_PATH.read_text(encoding="utf-8").upper()
    assert "ALTER TABLE CONTEXT_INDICATOR_SNAPSHOTS ADD COLUMN DETAILS_JSON STRING" in migration
    assert "DROP TABLE" not in migration


def test_pr34_additive_migration_is_exact_idempotent_and_non_destructive() -> None:
    assert check_questdb_schema.validate_pr34_migration_file(PR34_MIGRATION_PATH) == []

    cleaned = check_questdb_schema.strip_sql_line_comments(
        PR34_MIGRATION_PATH.read_text(encoding="utf-8")
    )
    statements = check_questdb_schema.split_sql_statements(cleaned)
    alters = [statement for statement in statements if statement.startswith("ALTER TABLE")]
    creates = [statement for statement in statements if statement.startswith("CREATE TABLE")]

    assert len(alters) == 36
    assert len(creates) == 2
    assert all(" ADD COLUMN IF NOT EXISTS " in statement for statement in alters)
    assert all(
        statement.startswith("CREATE TABLE IF NOT EXISTS ") for statement in creates
    )
    for forbidden in ("DROP", "RENAME", "TRUNCATE", "INSERT", "UPDATE", "DELETE", "SELECT"):
        assert re.search(rf"\b{forbidden}\b", cleaned, flags=re.IGNORECASE) is None


def test_pr34_migration_rejects_missing_idempotency_and_destructive_sql(tmp_path: Path) -> None:
    migration = tmp_path / "bad.sql"
    migration.write_text(
        "ALTER TABLE context_ai_events ADD COLUMN raw_input_id STRING;\n"
        "DROP TABLE context_flags;\n",
        encoding="utf-8",
    )

    failures = check_questdb_schema.validate_pr34_migration_file(migration)

    assert any("destructive or DML" in failure for failure in failures)
    assert any("ALTER statements" in failure for failure in failures)


def test_pr35_additive_migration_is_exact_idempotent_and_non_destructive() -> None:
    assert check_questdb_schema.validate_pr35_migration_file(PR35_MIGRATION_PATH) == []

    cleaned = check_questdb_schema.strip_sql_line_comments(
        PR35_MIGRATION_PATH.read_text(encoding="utf-8")
    )
    statements = check_questdb_schema.split_sql_statements(cleaned)

    assert len(statements) == 4
    assert all(
        statement.startswith("ALTER TABLE context_classification_attempts")
        for statement in statements
    )
    assert all(" ADD COLUMN IF NOT EXISTS " in statement for statement in statements)
    for forbidden in (
        "DROP",
        "RENAME",
        "TRUNCATE",
        "CREATE",
        "INSERT",
        "UPDATE",
        "DELETE",
        "SELECT",
    ):
        assert re.search(rf"\b{forbidden}\b", cleaned, flags=re.IGNORECASE) is None


def test_pr35_migration_rejects_missing_idempotency_and_destructive_sql(
    tmp_path: Path,
) -> None:
    migration = tmp_path / "bad-pr35.sql"
    migration.write_text(
        "ALTER TABLE context_classification_attempts ADD COLUMN retry_count LONG;\n"
        "DROP TABLE context_classification_attempts;\n",
        encoding="utf-8",
    )

    failures = check_questdb_schema.validate_pr35_migration_file(migration)

    assert any("destructive" in failure for failure in failures)
    assert any("statements must exactly match" in failure for failure in failures)


def test_reset_schema_columns_exactly_match_writer_tables() -> None:
    cleaned = check_questdb_schema.strip_sql_line_comments(
        SCHEMA_PATH.read_text(encoding="utf-8")
    )

    assert check_questdb_schema._schema_writer_column_failures(cleaned) == []
    assert check_questdb_schema.validate_questdb_config_table_order() == []


def test_phase7_context_ledger_has_no_raw_text_or_exception_columns() -> None:
    assert check_questdb_schema._context_ledger_raw_text_failures() == []

    columns = {
        column
        for table in (
            "context_ai_events",
            "context_flags",
            "context_classification_attempts",
            "shadow_context_policy_evaluations",
        )
        for column in check_questdb_schema.TABLE_COLUMNS[table]
    }
    assert "safe_failure_category" in columns
    assert "safe_failure_summary" in columns
    assert not columns.intersection(check_questdb_schema.FORBIDDEN_CONTEXT_LEDGER_COLUMNS)


@pytest.mark.parametrize("forbidden", ["TTL", "INSERT", "SELECT"])
def test_schema_file_contains_no_forbidden_schema_operations(forbidden: str) -> None:
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    cleaned = check_questdb_schema.strip_sql_line_comments(text)

    assert forbidden not in cleaned.upper()


def test_drop_statements_are_before_create_statements() -> None:
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    cleaned = check_questdb_schema.strip_sql_line_comments(text)

    assert check_questdb_schema._drop_create_order_failures(cleaned) == []


def test_check_script_exits_zero_on_valid_schema(capsys: pytest.CaptureFixture[str]) -> None:
    assert check_questdb_schema.main([]) == 0

    output = capsys.readouterr().out
    assert "[PASS] QuestDB schema offline validation passed" in output


def test_line_comments_are_removed_before_splitting() -> None:
    sql = "-- comment\nDROP TABLE IF EXISTS bot_runs;\n-- another comment\nCREATE TABLE bot_runs (started_at TIMESTAMP) TIMESTAMP(started_at) PARTITION BY DAY;"

    assert check_questdb_schema.strip_sql_line_comments(sql) == (
        "DROP TABLE IF EXISTS bot_runs;\n"
        "CREATE TABLE bot_runs (started_at TIMESTAMP) TIMESTAMP(started_at) PARTITION BY DAY;"
    )


def test_comment_semicolons_do_not_create_fake_statements() -> None:
    sql = "-- comment with ; semicolon\nDROP TABLE IF EXISTS bot_runs;\n-- another ; comment\n"

    assert check_questdb_schema.split_sql_statements(sql) == [
        "DROP TABLE IF EXISTS bot_runs"
    ]


def test_empty_statements_are_ignored() -> None:
    sql = ";\nDROP TABLE IF EXISTS bot_runs;;\n\n;"

    assert check_questdb_schema.split_sql_statements(sql) == [
        "DROP TABLE IF EXISTS bot_runs"
    ]


def test_drop_and_create_statements_remain_in_order() -> None:
    sql = """
DROP TABLE IF EXISTS bot_runs;
CREATE TABLE bot_runs (started_at TIMESTAMP) TIMESTAMP(started_at) PARTITION BY DAY;
DROP TABLE IF EXISTS bot_sessions;
CREATE TABLE bot_sessions (session_start_time TIMESTAMP) TIMESTAMP(session_start_time) PARTITION BY DAY;
"""

    assert check_questdb_schema.split_sql_statements(sql) == [
        "DROP TABLE IF EXISTS bot_runs",
        "CREATE TABLE bot_runs (started_at TIMESTAMP) TIMESTAMP(started_at) PARTITION BY DAY",
        "DROP TABLE IF EXISTS bot_sessions",
        "CREATE TABLE bot_sessions (session_start_time TIMESTAMP) TIMESTAMP(session_start_time) PARTITION BY DAY",
    ]


def test_schema_with_no_comments_splits_into_expected_statements() -> None:
    sql = "DROP TABLE IF EXISTS bot_runs;CREATE TABLE bot_runs (started_at TIMESTAMP) TIMESTAMP(started_at) PARTITION BY DAY;"

    assert check_questdb_schema.split_sql_statements(sql) == [
        "DROP TABLE IF EXISTS bot_runs",
        "CREATE TABLE bot_runs (started_at TIMESTAMP) TIMESTAMP(started_at) PARTITION BY DAY",
    ]


def test_apply_sends_each_statement_individually_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse(200, {"ddl": "OK"})

    monkeypatch.setattr(requests, "get", fake_get)
    statements = ["DROP TABLE IF EXISTS bot_runs", "CREATE TABLE bot_runs (started_at TIMESTAMP)"]

    check_questdb_schema.apply_schema_statements(
        QuestDBHealthConfig(),
        statements,
        verify_tables=False,
    )

    assert [call["params"] for call in calls] == [
        {"query": statements[0], "fmt": "json"},
        {"query": statements[1], "fmt": "json"},
    ]
    assert all(call["url"] == "http://localhost:9000/exec" for call in calls)
    assert all(call["timeout"] == 3.0 for call in calls)


def test_apply_successful_responses_continue(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        calls.append(str(kwargs["params"]["query"]))  # type: ignore[index]
        return FakeResponse(200, {"ddl": "OK"})

    monkeypatch.setattr(requests, "get", fake_get)

    check_questdb_schema.apply_schema_statements(
        QuestDBHealthConfig(),
        ["DROP TABLE IF EXISTS bot_runs", "DROP TABLE IF EXISTS bot_sessions"],
        verify_tables=False,
    )

    assert calls == ["DROP TABLE IF EXISTS bot_runs", "DROP TABLE IF EXISTS bot_sessions"]


def test_apply_non_200_response_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(500, {"error": "server error"}),
    )

    with pytest.raises(check_questdb_schema.QuestDBSchemaError, match="Statement 1.*HTTP 500"):
        check_questdb_schema.apply_schema_statements(
            QuestDBHealthConfig(),
            ["DROP TABLE IF EXISTS bot_runs"],
            verify_tables=False,
        )


def test_apply_json_error_body_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(200, {"error": "syntax error"}),
    )

    with pytest.raises(check_questdb_schema.QuestDBSchemaError, match="Statement 1.*syntax error"):
        check_questdb_schema.apply_schema_statements(
            QuestDBHealthConfig(),
            ["DROP TABLE IF EXISTS bot_runs"],
            verify_tables=False,
        )


def test_apply_invalid_json_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(200, ValueError("bad json")),
    )

    with pytest.raises(check_questdb_schema.QuestDBSchemaError, match="Statement 1.*invalid JSON"):
        check_questdb_schema.apply_schema_statements(
            QuestDBHealthConfig(),
            ["DROP TABLE IF EXISTS bot_runs"],
            verify_tables=False,
        )


def test_apply_mode_runs_required_health_before_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        check_questdb_schema,
        "run_required_health_check",
        lambda config: calls.append("health"),
    )
    monkeypatch.setattr(
        check_questdb_schema,
        "apply_schema_statements",
        lambda config, statements: calls.append("apply"),
    )

    assert check_questdb_schema.main(["--apply", "--required"]) == 0
    assert calls == ["health", "apply"]


def test_apply_mode_requires_required_flag() -> None:
    assert check_questdb_schema.main(["--apply"]) == 1


def test_verify_expected_tables_parses_tables_response(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [[table] for table in check_questdb_schema.REQUIRED_TABLES]

    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            200,
            {
                "columns": [{"name": "table_name", "type": "STRING"}],
                "dataset": rows,
            },
        ),
    )

    check_questdb_schema.verify_expected_tables_exist(QuestDBHealthConfig())
