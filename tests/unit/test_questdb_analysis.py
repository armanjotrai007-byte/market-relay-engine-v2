from __future__ import annotations

from pathlib import Path

import pytest
import requests

from market_relay_engine.questdb.analysis import (
    ANALYSIS_TABLES,
    ENV_HTTP_HOST,
    ENV_HTTP_PORT,
    ENV_HTTP_SCHEME,
    ENV_MAX_ENCODED_URL_LENGTH_CHARS,
    ENV_REQUIRED,
    ENV_TIMEOUT_SECONDS,
    QuestDBAnalysisConfig,
    QuestDBAnalysisError,
    QuestDBLedgerReader,
    QuestDBQueryResult,
    build_basic_ledger_summary,
    encoded_exec_url_length,
    get_cost_summary,
    get_execution_summary,
    get_outcome_summary,
    get_risk_decision_summary,
    get_signal_summary,
    get_system_health_summary,
    get_table_counts,
    load_questdb_analysis_config,
    parse_exec_response,
    validate_read_only_sql,
)


ALLOWED_SQL = (
    "SELECT * FROM model_signals",
    "   select * from model_signals",
    "\n\tSELECT * FROM system_health_events",
    "with x as (select * from model_signals) select * from x",
)

REJECTED_SQL = (
    "",
    "   ",
    "model_signals",
    "DELETE FROM model_signals",
    "delete from model_signals",
    "Drop table risk_decisions",
    "SELECT * FROM model_signals;",
    "SELECT * FROM model_signals; DROP TABLE risk_decisions",
    "WITH x AS (SELECT * FROM model_signals) DELETE FROM risk_decisions",
    "CREATE TABLE bad_table (x INT)",
    "ALTER TABLE model_signals ADD COLUMN bad INT",
)

FORBIDDEN_RAW_TABLES = {
    "raw_trades",
    "raw_bbo",
    "raw_tbbo",
    "raw_ohlcv",
    "raw_mbp10",
    "databento_definitions",
}


class FakeResponse:
    def __init__(self, status_code: int, payload: object | Exception) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_analysis_config_defaults_when_no_env_yaml_or_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)

    config = load_questdb_analysis_config(
        tmp_path / "missing.yaml",
        load_dotenv_file=False,
    )

    assert config == QuestDBAnalysisConfig()
    assert config.http_host == "localhost"
    assert config.http_port == 9000
    assert config.max_encoded_url_length_chars == 7000
    assert config.required is False


def test_yaml_env_and_explicit_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    config_path = tmp_path / "questdb.yaml"
    config_path.write_text(
        """
connection:
  default_http_scheme: http
  default_http_host: connection-host
  default_http_port: 9000
  default_health_timeout_seconds: 3.0
analysis:
  http_scheme: https
  http_host: analysis-host
  http_port: 9100
  timeout_seconds: 4.5
  required_by_default: true
  max_encoded_url_length_chars: 6000
""",
        encoding="utf-8",
    )

    yaml_config = load_questdb_analysis_config(config_path, load_dotenv_file=False)
    assert yaml_config.http_scheme == "https"
    assert yaml_config.http_host == "analysis-host"
    assert yaml_config.http_port == 9100
    assert yaml_config.timeout_seconds == 4.5
    assert yaml_config.required is True
    assert yaml_config.max_encoded_url_length_chars == 6000

    monkeypatch.setenv(ENV_HTTP_HOST, "env-host")
    monkeypatch.setenv(ENV_MAX_ENCODED_URL_LENGTH_CHARS, "5000")
    env_config = load_questdb_analysis_config(config_path, load_dotenv_file=False)
    assert env_config.http_host == "env-host"
    assert env_config.max_encoded_url_length_chars == 5000

    explicit_config = load_questdb_analysis_config(
        config_path,
        http_host="explicit-host",
        max_encoded_url_length_chars=4000,
        load_dotenv_file=False,
    )
    assert explicit_config.http_host == "explicit-host"
    assert explicit_config.max_encoded_url_length_chars == 4000


def test_yaml_analysis_falls_back_to_connection_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    config_path = tmp_path / "questdb.yaml"
    config_path.write_text(
        """
connection:
  default_http_host: connection-host
  default_http_port: 9000
  default_health_timeout_seconds: 2.5
analysis:
  max_encoded_url_length_chars: 5000
""",
        encoding="utf-8",
    )

    config = load_questdb_analysis_config(config_path, load_dotenv_file=False)

    assert config.http_host == "connection-host"
    assert config.http_port == 9000
    assert config.timeout_seconds == 2.5
    assert config.max_encoded_url_length_chars == 5000


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"http_host": ""}, "http_host"),
        ({"http_port": 0}, "http_port"),
        ({"http_port": 70000}, "http_port"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"timeout_seconds": "nan"}, "timeout_seconds"),
        ({"max_encoded_url_length_chars": 0}, "max_encoded_url_length_chars"),
    ],
)
def test_analysis_config_rejects_invalid_values(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(QuestDBAnalysisError, match=match):
        QuestDBAnalysisConfig(**kwargs)


@pytest.mark.parametrize("sql", ALLOWED_SQL)
def test_validate_read_only_sql_allows_select_and_with(sql: str) -> None:
    assert validate_read_only_sql(sql) == sql


@pytest.mark.parametrize("sql", REJECTED_SQL)
def test_validate_read_only_sql_rejects_unsafe_sql(sql: str) -> None:
    with pytest.raises(QuestDBAnalysisError):
        validate_read_only_sql(sql)


def test_validate_read_only_sql_rejects_non_string() -> None:
    with pytest.raises(QuestDBAnalysisError, match="string"):
        validate_read_only_sql(123)  # type: ignore[arg-type]


def test_encoded_url_length_exceeds_raw_sql_for_encoded_values() -> None:
    sql = "SELECT * FROM model_signals WHERE ticker = 'XOM US'"

    assert encoded_exec_url_length("http://localhost:9000/exec", sql) > len(sql)


def test_execute_select_sends_normal_get_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse(
            200,
            {
                "columns": [{"name": "total_model_signals"}],
                "dataset": [[2]],
                "count": 1,
            },
        )

    monkeypatch.setattr(requests, "get", fake_get)

    result = QuestDBLedgerReader(QuestDBAnalysisConfig()).execute_select(
        "SELECT count() AS total_model_signals FROM model_signals"
    )

    assert result.rows == [{"total_model_signals": 2}]
    assert calls[0]["url"] == "http://localhost:9000/exec"
    assert calls[0]["params"]["fmt"] == "json"  # type: ignore[index]
    assert "SELECT count()" in calls[0]["params"]["query"]  # type: ignore[index]
    assert calls[0]["timeout"] == 3.0


def test_execute_select_rejects_oversized_encoded_url_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        calls.append("called")
        return FakeResponse(200, {"columns": [{"name": "x"}], "dataset": [[1]]})

    monkeypatch.setattr(requests, "get", fake_get)
    reader = QuestDBLedgerReader(
        QuestDBAnalysisConfig(max_encoded_url_length_chars=40)
    )

    with pytest.raises(QuestDBAnalysisError, match="too long"):
        reader.execute_select("SELECT * FROM model_signals WHERE ticker = 'XOM US Equity'")

    assert calls == []


def test_execute_select_rejects_mutating_sql_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: calls.append("called"),
    )

    with pytest.raises(QuestDBAnalysisError, match="Semicolons"):
        QuestDBLedgerReader(QuestDBAnalysisConfig()).execute_select(
            "SELECT * FROM model_signals; DROP TABLE risk_decisions"
        )

    assert calls == []


def test_execute_select_non_200_invalid_json_non_object_error_and_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = QuestDBLedgerReader(QuestDBAnalysisConfig())

    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(500, {"error": "server error"}),
    )
    with pytest.raises(QuestDBAnalysisError, match="HTTP 500"):
        reader.execute_select("SELECT * FROM model_signals")

    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(200, ValueError("bad json")),
    )
    with pytest.raises(QuestDBAnalysisError, match="invalid JSON"):
        reader.execute_select("SELECT * FROM model_signals")

    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(200, ["not", "object"]),
    )
    with pytest.raises(QuestDBAnalysisError, match="non-object"):
        reader.execute_select("SELECT * FROM model_signals")

    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(200, {"error": "syntax error"}),
    )
    with pytest.raises(QuestDBAnalysisError, match="syntax error"):
        reader.execute_select("SELECT * FROM model_signals")

    def raise_timeout(*args: object, **kwargs: object) -> FakeResponse:
        raise requests.Timeout("timed out")

    monkeypatch.setattr(requests, "get", raise_timeout)
    with pytest.raises(QuestDBAnalysisError, match="timed out"):
        reader.execute_select("SELECT * FROM model_signals")


def test_parse_exec_response_normal_and_empty_dataset() -> None:
    result = parse_exec_response(
        {
            "columns": [{"name": "ticker"}, {"name": "signal_count"}],
            "dataset": [["XOM", 2], ["CVX", 1]],
            "count": 2,
        }
    )

    assert result.columns == ["ticker", "signal_count"]
    assert result.rows == [
        {"ticker": "XOM", "signal_count": 2},
        {"ticker": "CVX", "signal_count": 1},
    ]
    assert result.row_count == 2

    empty = parse_exec_response(
        {
            "columns": [{"name": "ticker"}, {"name": "signal_count"}],
            "dataset": [],
        }
    )
    assert empty.rows == []
    assert empty.row_count == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"dataset": []},
        {"columns": [{"name": "x"}]},
        {"columns": [], "dataset": []},
        {"columns": [{"name": "x"}], "dataset": [["a", "b"]]},
        {"columns": [{"name": ""}], "dataset": []},
        {"error": "bad query"},
    ],
)
def test_parse_exec_response_rejects_malformed_payloads(payload: object) -> None:
    with pytest.raises(QuestDBAnalysisError):
        parse_exec_response(payload)


def test_summary_functions_emit_read_only_allowed_table_queries() -> None:
    reader = _RecordingReader()

    get_table_counts(reader)
    get_signal_summary(reader)
    get_risk_decision_summary(reader)
    get_cost_summary(reader)
    get_execution_summary(reader)
    get_outcome_summary(reader)
    get_system_health_summary(reader)
    build_basic_ledger_summary(reader)

    assert reader.queries
    for query in reader.queries:
        validate_read_only_sql(query)
        lowered = query.lower()
        assert not any(table in lowered for table in FORBIDDEN_RAW_TABLES)
    assert not FORBIDDEN_RAW_TABLES.intersection(ANALYSIS_TABLES)


class _RecordingReader:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.config = QuestDBAnalysisConfig()

    def execute_select(self, sql: str) -> QuestDBQueryResult:
        validate_read_only_sql(sql)
        self.queries.append(sql)
        aliases = _aliases_from_sql(sql)
        row = {alias: 0 for alias in aliases}
        return QuestDBQueryResult(
            columns=aliases or ["value"],
            rows=[row] if aliases else [],
            row_count=1 if aliases else 0,
        )


def _aliases_from_sql(sql: str) -> list[str]:
    tokens = sql.replace(",", " ").split()
    aliases: list[str] = []
    for index, token in enumerate(tokens[:-1]):
        if token.upper() == "AS":
            aliases.append(tokens[index + 1])
    return aliases


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        ENV_HTTP_SCHEME,
        ENV_HTTP_HOST,
        ENV_HTTP_PORT,
        ENV_TIMEOUT_SECONDS,
        ENV_REQUIRED,
        ENV_MAX_ENCODED_URL_LENGTH_CHARS,
    ):
        monkeypatch.delenv(name, raising=False)
