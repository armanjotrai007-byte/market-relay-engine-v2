"""Read-only QuestDB ledger readback and basic summaries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
from time import perf_counter
from typing import Any

from dotenv import load_dotenv
import requests
import yaml


DEFAULT_HTTP_SCHEME = "http"
DEFAULT_HTTP_HOST = "localhost"
DEFAULT_HTTP_PORT = 9000
DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_REQUIRED = False
DEFAULT_MAX_ENCODED_URL_LENGTH_CHARS = 7000

ENV_HTTP_SCHEME = "QUESTDB_HTTP_SCHEME"
ENV_HTTP_HOST = "QUESTDB_HTTP_HOST"
ENV_HTTP_PORT = "QUESTDB_HTTP_PORT"
ENV_TIMEOUT_SECONDS = "QUESTDB_HEALTH_TIMEOUT_SECONDS"
ENV_REQUIRED = "QUESTDB_ANALYSIS_REQUIRED"
ENV_MAX_ENCODED_URL_LENGTH_CHARS = "QUESTDB_ANALYSIS_MAX_ENCODED_URL_LENGTH_CHARS"

FORBIDDEN_SQL_TOKENS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "CREATE",
    "ALTER",
    "TRUNCATE",
    "COPY",
    "ATTACH",
    "DETACH",
)

ANALYSIS_TABLES = (
    "model_signals",
    "risk_decisions",
    "cost_estimates",
    "order_events",
    "fill_events",
    "trade_outcomes",
    "system_health_events",
)


class QuestDBAnalysisError(RuntimeError):
    """Raised when a QuestDB analysis query is unsafe or fails."""


@dataclass(frozen=True, kw_only=True)
class QuestDBAnalysisConfig:
    """QuestDB HTTP readback configuration."""

    http_scheme: str = DEFAULT_HTTP_SCHEME
    http_host: str = DEFAULT_HTTP_HOST
    http_port: int = DEFAULT_HTTP_PORT
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_encoded_url_length_chars: int = DEFAULT_MAX_ENCODED_URL_LENGTH_CHARS
    required: bool = DEFAULT_REQUIRED

    def __post_init__(self) -> None:
        scheme = _non_empty_string(self.http_scheme, "http_scheme").lower()
        if scheme not in {"http", "https"}:
            raise QuestDBAnalysisError("http_scheme must be http or https")
        object.__setattr__(self, "http_scheme", scheme)
        object.__setattr__(self, "http_host", _non_empty_string(self.http_host, "http_host"))
        object.__setattr__(self, "http_port", _port(self.http_port, "http_port"))
        object.__setattr__(
            self,
            "timeout_seconds",
            _positive_finite_float(self.timeout_seconds, "timeout_seconds"),
        )
        object.__setattr__(
            self,
            "max_encoded_url_length_chars",
            _positive_int(
                self.max_encoded_url_length_chars,
                "max_encoded_url_length_chars",
            ),
        )
        if not isinstance(self.required, bool):
            raise QuestDBAnalysisError("required must be bool")


@dataclass(frozen=True, kw_only=True)
class QuestDBQueryResult:
    """Rows parsed from a QuestDB /exec SELECT response."""

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int


def load_questdb_analysis_config(
    config_path: str | Path | None = None,
    *,
    http_scheme: str | None = None,
    http_host: str | None = None,
    http_port: int | str | None = None,
    timeout_seconds: float | str | None = None,
    max_encoded_url_length_chars: int | str | None = None,
    required: bool | None = None,
    load_dotenv_file: bool = True,
) -> QuestDBAnalysisConfig:
    """Load QuestDB analysis settings from args, env, YAML, then defaults."""
    if load_dotenv_file:
        load_dotenv(_repo_root() / ".env", override=False)

    values: dict[str, Any] = {
        "http_scheme": DEFAULT_HTTP_SCHEME,
        "http_host": DEFAULT_HTTP_HOST,
        "http_port": DEFAULT_HTTP_PORT,
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "max_encoded_url_length_chars": DEFAULT_MAX_ENCODED_URL_LENGTH_CHARS,
        "required": DEFAULT_REQUIRED,
    }
    values.update(
        {
            key: value
            for key, value in _load_yaml_analysis_values(_resolve_config_path(config_path)).items()
            if value is not None
        }
    )
    values.update(
        {
            key: value
            for key, value in {
                "http_scheme": os.getenv(ENV_HTTP_SCHEME),
                "http_host": os.getenv(ENV_HTTP_HOST),
                "http_port": os.getenv(ENV_HTTP_PORT),
                "timeout_seconds": os.getenv(ENV_TIMEOUT_SECONDS),
                "required": _optional_bool_from_env(os.getenv(ENV_REQUIRED), ENV_REQUIRED),
                "max_encoded_url_length_chars": os.getenv(ENV_MAX_ENCODED_URL_LENGTH_CHARS),
            }.items()
            if value is not None
        }
    )
    values.update(
        {
            key: value
            for key, value in {
                "http_scheme": http_scheme,
                "http_host": http_host,
                "http_port": http_port,
                "timeout_seconds": timeout_seconds,
                "required": required,
                "max_encoded_url_length_chars": max_encoded_url_length_chars,
            }.items()
            if value is not None
        }
    )
    return QuestDBAnalysisConfig(**values)


def build_questdb_analysis_exec_url(config: QuestDBAnalysisConfig) -> str:
    """Return the QuestDB HTTP /exec endpoint URL."""
    return f"{config.http_scheme}://{config.http_host}:{config.http_port}/exec"


def encoded_exec_url_length(exec_url: str, sql: str) -> int:
    """Return the encoded GET URL length for a QuestDB /exec query."""
    request = requests.Request(
        "GET",
        exec_url,
        params={"query": sql, "fmt": "json"},
    )
    prepared = request.prepare()
    return len(prepared.url or "")


def validate_read_only_sql(sql: str) -> str:
    """Validate a small read-only SELECT/WITH query and return the original SQL."""
    if not isinstance(sql, str):
        raise QuestDBAnalysisError("SQL must be a string")

    normalized_sql = sql.strip().upper()
    if not normalized_sql:
        raise QuestDBAnalysisError("SQL must not be blank")
    if ";" in normalized_sql:
        raise QuestDBAnalysisError("Semicolons are not allowed in read-only analysis SQL")
    if not (normalized_sql.startswith("SELECT") or normalized_sql.startswith("WITH")):
        raise QuestDBAnalysisError("Only SELECT or WITH queries are allowed")

    for token in FORBIDDEN_SQL_TOKENS:
        if re.search(rf"\b{token}\b", normalized_sql):
            raise QuestDBAnalysisError(f"Forbidden SQL token: {token}")
    return sql


def parse_exec_response(payload: Any) -> QuestDBQueryResult:
    """Parse QuestDB /exec JSON into column names and row dictionaries."""
    if not isinstance(payload, dict):
        raise QuestDBAnalysisError("QuestDB /exec returned non-object JSON")
    if "error" in payload:
        raise QuestDBAnalysisError(f"QuestDB /exec returned error: {payload.get('error')}")

    raw_columns = payload.get("columns")
    dataset = payload.get("dataset")
    if not isinstance(raw_columns, list) or not raw_columns:
        raise QuestDBAnalysisError("QuestDB /exec response missing columns")
    if not isinstance(dataset, list):
        raise QuestDBAnalysisError("QuestDB /exec response missing dataset")

    columns = [_column_name(column) for column in raw_columns]
    rows: list[dict[str, Any]] = []
    for index, raw_row in enumerate(dataset):
        if not isinstance(raw_row, (list, tuple)):
            raise QuestDBAnalysisError(f"QuestDB /exec dataset row {index} is not a list")
        if len(raw_row) != len(columns):
            raise QuestDBAnalysisError(
                f"QuestDB /exec dataset row {index} has {len(raw_row)} values for {len(columns)} columns"
            )
        rows.append(dict(zip(columns, raw_row, strict=True)))

    return QuestDBQueryResult(columns=columns, rows=rows, row_count=len(rows))


class QuestDBLedgerReader:
    """Small synchronous read-only reader for QuestDB V2 ledger tables."""

    def __init__(self, config: QuestDBAnalysisConfig | None = None) -> None:
        self.config = config or load_questdb_analysis_config()
        self.exec_url = build_questdb_analysis_exec_url(self.config)

    def execute_select(self, sql: str) -> QuestDBQueryResult:
        safe_sql = validate_read_only_sql(sql)
        encoded_length = encoded_exec_url_length(self.exec_url, safe_sql)
        if encoded_length > self.config.max_encoded_url_length_chars:
            raise QuestDBAnalysisError(
                "encoded /exec GET request is too long for read-only analysis; "
                "a future bulk/read path is required"
            )

        started = perf_counter()
        try:
            response = requests.get(
                self.exec_url,
                params={"query": safe_sql, "fmt": "json"},
                timeout=self.config.timeout_seconds,
            )
            _ = max((perf_counter() - started) * 1000.0, 0.0)
        except requests.RequestException as exc:
            raise QuestDBAnalysisError(f"QuestDB /exec request failed: {exc}") from exc

        if response.status_code != 200:
            raise QuestDBAnalysisError(f"QuestDB /exec returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise QuestDBAnalysisError(f"QuestDB /exec returned invalid JSON: {exc}") from exc
        return parse_exec_response(payload)


def get_table_counts(reader: QuestDBLedgerReader) -> dict[str, int]:
    """Return row counts for important ledger tables."""
    counts: dict[str, int] = {}
    for table_name in ANALYSIS_TABLES:
        result = reader.execute_select(f"SELECT count() AS row_count FROM {table_name}")
        counts[table_name] = _int_value(_first_value(result, "row_count"), default=0)
    return counts


def get_signal_summary(reader: QuestDBLedgerReader) -> dict[str, Any]:
    result = reader.execute_select(
        "SELECT count() AS total_model_signals, avg(confidence) AS average_confidence FROM model_signals"
    )
    by_ticker = reader.execute_select(
        "SELECT ticker, count() AS signal_count FROM model_signals GROUP BY ticker"
    )
    row = _first_row(result)
    return {
        "total_model_signals": _int_value(row.get("total_model_signals"), default=0),
        "average_confidence": row.get("average_confidence"),
        "by_ticker": by_ticker.rows,
    }


def get_risk_decision_summary(reader: QuestDBLedgerReader) -> dict[str, Any]:
    total = reader.execute_select("SELECT count() AS total_risk_decisions FROM risk_decisions")
    approved = reader.execute_select(
        "SELECT count() AS approved_count FROM risk_decisions WHERE approved = true"
    )
    blocked = reader.execute_select(
        "SELECT count() AS blocked_count FROM risk_decisions WHERE approved = false"
    )
    by_ticker = reader.execute_select(
        "SELECT ticker, count() AS decision_count FROM risk_decisions GROUP BY ticker"
    )
    return {
        "total_risk_decisions": _int_value(_first_value(total, "total_risk_decisions"), default=0),
        "approved_count": _int_value(_first_value(approved, "approved_count"), default=0),
        "blocked_count": _int_value(_first_value(blocked, "blocked_count"), default=0),
        "by_ticker": by_ticker.rows,
    }


def get_cost_summary(reader: QuestDBLedgerReader) -> dict[str, Any]:
    total = reader.execute_select(
        "SELECT count() AS total_cost_estimates, avg(net_expected_edge_bps) AS average_net_expected_edge_bps FROM cost_estimates"
    )
    profitable = reader.execute_select(
        "SELECT count() AS profitable_after_costs_count FROM cost_estimates WHERE profitable_after_costs = true"
    )
    row = _first_row(total)
    return {
        "total_cost_estimates": _int_value(row.get("total_cost_estimates"), default=0),
        "profitable_after_costs_count": _int_value(
            _first_value(profitable, "profitable_after_costs_count"),
            default=0,
        ),
        "average_net_expected_edge_bps": row.get("average_net_expected_edge_bps"),
    }


def get_execution_summary(reader: QuestDBLedgerReader) -> dict[str, Any]:
    orders = reader.execute_select("SELECT count() AS order_count FROM order_events")
    fills = reader.execute_select(
        "SELECT count() AS fill_count, avg(slippage_bps) AS average_slippage_bps FROM fill_events"
    )
    fill_row = _first_row(fills)
    return {
        "order_count": _int_value(_first_value(orders, "order_count"), default=0),
        "fill_count": _int_value(fill_row.get("fill_count"), default=0),
        "average_slippage_bps": fill_row.get("average_slippage_bps"),
    }


def get_outcome_summary(reader: QuestDBLedgerReader) -> dict[str, Any]:
    result = reader.execute_select(
        "SELECT count() AS outcome_count, avg(realized_pnl) AS average_realized_pnl, "
        "avg(return_1m) AS average_return_1m, avg(return_5m) AS average_return_5m, "
        "avg(return_15m) AS average_return_15m FROM trade_outcomes"
    )
    row = _first_row(result)
    return {
        "outcome_count": _int_value(row.get("outcome_count"), default=0),
        "average_realized_pnl": row.get("average_realized_pnl"),
        "average_return_1m": row.get("average_return_1m"),
        "average_return_5m": row.get("average_return_5m"),
        "average_return_15m": row.get("average_return_15m"),
    }


def get_system_health_summary(reader: QuestDBLedgerReader) -> dict[str, Any]:
    by_status = reader.execute_select(
        "SELECT component, status, count() AS event_count FROM system_health_events GROUP BY component, status"
    )
    warnings = reader.execute_select(
        "SELECT count() AS warning_error_count FROM system_health_events "
        "WHERE status IN ('warn', 'warning', 'error', 'WARN', 'WARNING', 'ERROR')"
    )
    return {
        "by_component_status": by_status.rows,
        "warning_error_count": _int_value(_first_value(warnings, "warning_error_count"), default=0),
    }


def build_basic_ledger_summary(reader: QuestDBLedgerReader) -> dict[str, Any]:
    """Return a compact read-only ledger summary."""
    return {
        "table_counts": get_table_counts(reader),
        "signals": get_signal_summary(reader),
        "risk_decisions": get_risk_decision_summary(reader),
        "costs": get_cost_summary(reader),
        "execution": get_execution_summary(reader),
        "outcomes": get_outcome_summary(reader),
        "system_health": get_system_health_summary(reader),
    }


def _column_name(column: Any) -> str:
    if isinstance(column, Mapping):
        name = column.get("name")
    else:
        name = column
    if not isinstance(name, str) or not name.strip():
        raise QuestDBAnalysisError("QuestDB /exec response column is missing a name")
    return name.strip()


def _first_row(result: QuestDBQueryResult) -> dict[str, Any]:
    return result.rows[0] if result.rows else {}


def _first_value(result: QuestDBQueryResult, key: str) -> Any:
    return _first_row(result).get(key)


def _int_value(value: Any, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve_config_path(config_path: str | Path | None) -> Path:
    return _repo_root() / "config" / "questdb.yaml" if config_path is None else Path(config_path)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_yaml_analysis_values(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise QuestDBAnalysisError(f"Invalid QuestDB YAML config: {config_path}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise QuestDBAnalysisError("QuestDB YAML config must be a mapping")

    connection = _yaml_section_mapping(loaded, "connection")
    analysis = _yaml_section_mapping(loaded, "analysis")
    return {
        "http_scheme": _yaml_value(analysis, "http_scheme", connection, "default_http_scheme"),
        "http_host": _yaml_value(analysis, "http_host", connection, "default_http_host"),
        "http_port": _yaml_value(analysis, "http_port", connection, "default_http_port"),
        "timeout_seconds": _yaml_value(
            analysis,
            "timeout_seconds",
            connection,
            "default_health_timeout_seconds",
        ),
        "required": analysis.get("required_by_default"),
        "max_encoded_url_length_chars": analysis.get("max_encoded_url_length_chars"),
    }


def _yaml_section_mapping(loaded: Mapping[str, Any], name: str) -> dict[str, Any]:
    section = loaded.get(name, {})
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise QuestDBAnalysisError(f"questdb.yaml {name} section must be a mapping")
    return section


def _yaml_value(
    primary: Mapping[str, Any],
    primary_key: str,
    fallback: Mapping[str, Any],
    fallback_key: str,
) -> Any:
    if primary_key in primary and primary[primary_key] is not None:
        return primary[primary_key]
    if fallback_key in fallback and fallback[fallback_key] is not None:
        return fallback[fallback_key]
    return None


def _optional_bool_from_env(value: str | None, env_name: str) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise QuestDBAnalysisError(f"{env_name} must be a boolean value")


def _non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuestDBAnalysisError(f"{field_name} must be a non-empty string")
    return value.strip()


def _port(value: Any, field_name: str) -> int:
    port = _positive_int(value, field_name)
    if port > 65535:
        raise QuestDBAnalysisError(f"{field_name} must be between 1 and 65535")
    return port


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise QuestDBAnalysisError(f"{field_name} must be an integer, not bool")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise QuestDBAnalysisError(f"{field_name} must be an integer") from exc
    if str(number) != str(value).strip() and not isinstance(value, int):
        raise QuestDBAnalysisError(f"{field_name} must be an integer")
    if number <= 0:
        raise QuestDBAnalysisError(f"{field_name} must be positive")
    return number


def _positive_finite_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise QuestDBAnalysisError(f"{field_name} must be numeric, not bool")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise QuestDBAnalysisError(f"{field_name} must be numeric") from exc
    if not math.isfinite(number):
        raise QuestDBAnalysisError(f"{field_name} must be finite")
    if number <= 0:
        raise QuestDBAnalysisError(f"{field_name} must be positive")
    return number
