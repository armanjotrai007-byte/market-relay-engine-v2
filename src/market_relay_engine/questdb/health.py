"""QuestDB HTTP health checks for the V2 bot ledger foundation."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
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
QUESTDB_HEALTH_QUERY = "SELECT 1"

ENV_HTTP_SCHEME = "QUESTDB_HTTP_SCHEME"
ENV_HTTP_HOST = "QUESTDB_HTTP_HOST"
ENV_HTTP_PORT = "QUESTDB_HTTP_PORT"
ENV_TIMEOUT_SECONDS = "QUESTDB_HEALTH_TIMEOUT_SECONDS"
ENV_REQUIRED = "QUESTDB_HEALTH_REQUIRED"

FAILURE_UNREACHABLE = "unreachable"
FAILURE_UNHEALTHY = "unhealthy"


class QuestDBHealthError(RuntimeError):
    """Raised when required QuestDB health validation fails."""

    def __init__(
        self,
        message: str,
        result: "QuestDBHealthResult | None" = None,
    ) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True, kw_only=True)
class QuestDBHealthConfig:
    """QuestDB HTTP health check configuration."""

    http_scheme: str = DEFAULT_HTTP_SCHEME
    http_host: str = DEFAULT_HTTP_HOST
    http_port: int = DEFAULT_HTTP_PORT
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    required: bool = DEFAULT_REQUIRED

    def __post_init__(self) -> None:
        scheme = _non_empty_string(self.http_scheme, "http_scheme").lower()
        if scheme not in {"http", "https"}:
            raise QuestDBHealthError("http_scheme must be http or https")
        object.__setattr__(self, "http_scheme", scheme)

        object.__setattr__(
            self,
            "http_host",
            _non_empty_string(self.http_host, "http_host"),
        )
        object.__setattr__(self, "http_port", _port(self.http_port, "http_port"))
        object.__setattr__(
            self,
            "timeout_seconds",
            _positive_finite_float(self.timeout_seconds, "timeout_seconds"),
        )
        if not isinstance(self.required, bool):
            raise QuestDBHealthError("required must be bool")


@dataclass(frozen=True, kw_only=True)
class QuestDBHealthResult:
    """JSON-safe result from a QuestDB HTTP health check."""

    reachable: bool
    required: bool
    url: str
    status_code: int | None
    message: str
    query: str
    latency_ms: float | None
    failure_kind: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reachable, bool):
            raise QuestDBHealthError("reachable must be bool")
        if not isinstance(self.required, bool):
            raise QuestDBHealthError("required must be bool")
        for field_name in ("url", "message", "query"):
            _non_empty_string(getattr(self, field_name), field_name)
        if self.status_code is not None:
            object.__setattr__(self, "status_code", _int_value(self.status_code, "status_code"))
        if self.latency_ms is not None:
            object.__setattr__(
                self,
                "latency_ms",
                _finite_non_negative_float(self.latency_ms, "latency_ms"),
            )
        if self.failure_kind is not None and self.failure_kind not in {
            FAILURE_UNREACHABLE,
            FAILURE_UNHEALTHY,
        }:
            raise QuestDBHealthError("failure_kind must be unreachable or unhealthy")


def load_questdb_health_config(
    config_path: str | Path | None = None,
    *,
    http_scheme: str | None = None,
    http_host: str | None = None,
    http_port: int | str | None = None,
    timeout_seconds: float | str | None = None,
    required: bool | None = None,
    load_dotenv_file: bool = True,
) -> QuestDBHealthConfig:
    """Load QuestDB health settings from args, env, YAML, then defaults."""
    if load_dotenv_file:
        load_dotenv(_repo_root() / ".env", override=False)

    yaml_values = _load_yaml_health_values(_resolve_config_path(config_path))
    values: dict[str, Any] = {
        "http_scheme": DEFAULT_HTTP_SCHEME,
        "http_host": DEFAULT_HTTP_HOST,
        "http_port": DEFAULT_HTTP_PORT,
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "required": DEFAULT_REQUIRED,
    }
    values.update({key: value for key, value in yaml_values.items() if value is not None})

    env_values = {
        "http_scheme": os.getenv(ENV_HTTP_SCHEME),
        "http_host": os.getenv(ENV_HTTP_HOST),
        "http_port": os.getenv(ENV_HTTP_PORT),
        "timeout_seconds": os.getenv(ENV_TIMEOUT_SECONDS),
        "required": _optional_bool_from_env(os.getenv(ENV_REQUIRED)),
    }
    values.update({key: value for key, value in env_values.items() if value is not None})

    explicit_values = {
        "http_scheme": http_scheme,
        "http_host": http_host,
        "http_port": http_port,
        "timeout_seconds": timeout_seconds,
        "required": required,
    }
    values.update({key: value for key, value in explicit_values.items() if value is not None})

    return QuestDBHealthConfig(**values)


def build_questdb_exec_url(config: QuestDBHealthConfig) -> str:
    """Return the QuestDB HTTP /exec endpoint URL."""
    return f"{config.http_scheme}://{config.http_host}:{config.http_port}/exec"


def check_questdb_http(
    config: QuestDBHealthConfig,
    *,
    query: str = QUESTDB_HEALTH_QUERY,
) -> QuestDBHealthResult:
    """Run a small QuestDB /exec health query."""
    if not isinstance(config, QuestDBHealthConfig):
        raise QuestDBHealthError("config must be a QuestDBHealthConfig")
    if not isinstance(query, str) or not query.strip():
        raise QuestDBHealthError("query must be a non-empty string")

    url = build_questdb_exec_url(config)
    started = perf_counter()
    try:
        response = requests.get(
            url,
            params={"query": query.strip()},
            timeout=config.timeout_seconds,
        )
        latency_ms = max((perf_counter() - started) * 1000.0, 0.0)
    except requests.RequestException as exc:
        latency_ms = max((perf_counter() - started) * 1000.0, 0.0)
        result = QuestDBHealthResult(
            reachable=False,
            required=config.required,
            url=url,
            status_code=None,
            message=f"QuestDB HTTP connection failed: {exc}",
            query=query.strip(),
            latency_ms=latency_ms,
            failure_kind=FAILURE_UNREACHABLE,
        )
        _raise_if_required(config, result)
        return result

    if response.status_code != 200:
        result = QuestDBHealthResult(
            reachable=False,
            required=config.required,
            url=url,
            status_code=response.status_code,
            message=f"QuestDB /exec returned HTTP {response.status_code}",
            query=query.strip(),
            latency_ms=latency_ms,
            failure_kind=FAILURE_UNHEALTHY,
        )
        _raise_if_required(config, result)
        return result

    try:
        payload = response.json()
    except ValueError as exc:
        result = QuestDBHealthResult(
            reachable=False,
            required=config.required,
            url=url,
            status_code=response.status_code,
            message=f"QuestDB /exec returned invalid JSON: {exc}",
            query=query.strip(),
            latency_ms=latency_ms,
            failure_kind=FAILURE_UNHEALTHY,
        )
        _raise_if_required(config, result)
        return result

    health_message = _validate_exec_payload(payload)
    if health_message is not None:
        result = QuestDBHealthResult(
            reachable=False,
            required=config.required,
            url=url,
            status_code=response.status_code,
            message=health_message,
            query=query.strip(),
            latency_ms=latency_ms,
            failure_kind=FAILURE_UNHEALTHY,
        )
        _raise_if_required(config, result)
        return result

    return QuestDBHealthResult(
        reachable=True,
        required=config.required,
        url=url,
        status_code=response.status_code,
        message="QuestDB /exec SELECT 1 returned a healthy response",
        query=query.strip(),
        latency_ms=latency_ms,
        failure_kind=None,
    )


def format_questdb_health_result(result: QuestDBHealthResult) -> str:
    """Format a health result for validation script output."""
    if result.reachable:
        prefix = "[PASS] QuestDB HTTP health check passed"
    elif result.required:
        prefix = "[FAIL] QuestDB required but not healthy"
    elif result.failure_kind == FAILURE_UNREACHABLE:
        prefix = "[SKIP] QuestDB not reachable in optional mode"
    else:
        prefix = "[WARN] QuestDB reachable but unhealthy in optional mode"

    status = "none" if result.status_code is None else str(result.status_code)
    latency = "none" if result.latency_ms is None else f"{result.latency_ms:.2f}ms"
    return (
        f"{prefix}: {result.message} "
        f"(url={result.url}, status={status}, latency={latency})"
    )


def _raise_if_required(
    config: QuestDBHealthConfig,
    result: QuestDBHealthResult,
) -> None:
    if config.required and not result.reachable:
        raise QuestDBHealthError(result.message, result=result)


def _validate_exec_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return "QuestDB /exec response was not a JSON object"

    if "error" in payload:
        return f"QuestDB /exec returned error: {payload.get('error')}"

    columns = payload.get("columns")
    if not isinstance(columns, list) or not columns:
        return "QuestDB /exec response did not include result columns"

    dataset = payload.get("dataset")
    if not isinstance(dataset, list) or not dataset:
        return "QuestDB /exec response did not include a result dataset"

    first_row = dataset[0]
    if not isinstance(first_row, (list, tuple)) or not first_row:
        return "QuestDB /exec response dataset did not include a SELECT 1 row"

    first_value = first_row[0]
    if isinstance(first_value, bool) or first_value not in (1, "1"):
        return "QuestDB /exec SELECT 1 result was not 1"

    count = payload.get("count")
    if count is not None:
        if isinstance(count, bool):
            return "QuestDB /exec response count was not numeric"
        try:
            if int(count) < 1:
                return "QuestDB /exec response count was less than 1"
        except (TypeError, ValueError):
            return "QuestDB /exec response count was not numeric"

    return None


def _resolve_config_path(config_path: str | Path | None) -> Path:
    if config_path is None:
        return _repo_root() / "config" / "questdb.yaml"
    return Path(config_path)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_yaml_health_values(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise QuestDBHealthError(f"Invalid QuestDB YAML config: {config_path}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise QuestDBHealthError("QuestDB YAML config must be a mapping")

    connection = loaded.get("connection", {})
    health_check = loaded.get("health_check", {})
    if connection is None:
        connection = {}
    if health_check is None:
        health_check = {}
    if not isinstance(connection, dict):
        raise QuestDBHealthError("questdb.yaml connection section must be a mapping")
    if not isinstance(health_check, dict):
        raise QuestDBHealthError("questdb.yaml health_check section must be a mapping")

    return {
        "http_scheme": _first_present(
            connection,
            health_check,
            "default_http_scheme",
            "http_scheme",
        ),
        "http_host": _first_present(
            connection,
            health_check,
            "default_http_host",
            "http_host",
        ),
        "http_port": _first_present(
            connection,
            health_check,
            "default_http_port",
            "http_port",
        ),
        "timeout_seconds": _first_present(
            connection,
            health_check,
            "default_health_timeout_seconds",
            "timeout_seconds",
        ),
        "required": _first_present(
            health_check,
            connection,
            "required_by_default",
            "required",
        ),
    }


def _first_present(
    primary: dict[str, Any],
    secondary: dict[str, Any],
    primary_key: str,
    secondary_key: str,
) -> Any:
    if primary_key in primary:
        return primary[primary_key]
    if secondary_key in secondary:
        return secondary[secondary_key]
    return None


def _optional_bool_from_env(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise QuestDBHealthError(f"{ENV_REQUIRED} must be a boolean value")


def _non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuestDBHealthError(f"{field_name} must be a non-empty string")
    return value.strip()


def _port(value: Any, field_name: str) -> int:
    port = _int_value(value, field_name)
    if port < 1 or port > 65535:
        raise QuestDBHealthError(f"{field_name} must be between 1 and 65535")
    return port


def _int_value(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise QuestDBHealthError(f"{field_name} must be an integer, not bool")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise QuestDBHealthError(f"{field_name} must be an integer") from exc
    if str(number) != str(value).strip() and not isinstance(value, int):
        raise QuestDBHealthError(f"{field_name} must be an integer")
    return number


def _positive_finite_float(value: Any, field_name: str) -> float:
    number = _finite_float(value, field_name)
    if number <= 0:
        raise QuestDBHealthError(f"{field_name} must be positive")
    return number


def _finite_non_negative_float(value: Any, field_name: str) -> float:
    number = _finite_float(value, field_name)
    if number < 0:
        raise QuestDBHealthError(f"{field_name} must be non-negative")
    return number


def _finite_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise QuestDBHealthError(f"{field_name} must be numeric, not bool")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise QuestDBHealthError(f"{field_name} must be numeric") from exc
    if not math.isfinite(number):
        raise QuestDBHealthError(f"{field_name} must be finite")
    return number
