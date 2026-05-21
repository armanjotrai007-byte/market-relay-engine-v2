from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import requests

from market_relay_engine.questdb.health import (
    ENV_HTTP_HOST,
    ENV_HTTP_PORT,
    ENV_HTTP_SCHEME,
    ENV_REQUIRED,
    ENV_TIMEOUT_SECONDS,
    FAILURE_UNHEALTHY,
    FAILURE_UNREACHABLE,
    QuestDBHealthConfig,
    QuestDBHealthError,
    QuestDBHealthResult,
    build_questdb_exec_url,
    check_questdb_http,
    format_questdb_health_result,
    load_questdb_health_config,
    _validate_exec_payload,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_KEYS = (
    ENV_HTTP_SCHEME,
    ENV_HTTP_HOST,
    ENV_HTTP_PORT,
    ENV_TIMEOUT_SECONDS,
    ENV_REQUIRED,
)


class FakeResponse:
    def __init__(self, status_code: int, payload: object | Exception) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _write_yaml(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_questdb_health_module_imports_cleanly() -> None:
    assert importlib.import_module("market_relay_engine.questdb.health")


def test_default_config_values_when_no_env_yaml_or_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)

    config = load_questdb_health_config(
        tmp_path / "missing.yaml",
        load_dotenv_file=False,
    )

    assert config == QuestDBHealthConfig()
    assert config.http_host == "localhost"
    assert config.http_port == 9000
    assert config.required is False


def test_yaml_values_override_hardcoded_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    config_path = _write_yaml(
        tmp_path / "questdb.yaml",
        """
connection:
  default_http_scheme: https
  default_http_host: questdb.local
  default_http_port: 19000
  default_health_timeout_seconds: 1.5
health_check:
  required_by_default: true
""",
    )

    config = load_questdb_health_config(config_path, load_dotenv_file=False)

    assert config.http_scheme == "https"
    assert config.http_host == "questdb.local"
    assert config.http_port == 19000
    assert config.timeout_seconds == 1.5
    assert config.required is True


def test_environment_values_override_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    config_path = _write_yaml(
        tmp_path / "questdb.yaml",
        """
connection:
  default_http_scheme: https
  default_http_host: yaml-host
  default_http_port: 19000
  default_health_timeout_seconds: 1.5
health_check:
  required_by_default: true
""",
    )
    monkeypatch.setenv(ENV_HTTP_SCHEME, "http")
    monkeypatch.setenv(ENV_HTTP_HOST, "env-host")
    monkeypatch.setenv(ENV_HTTP_PORT, "9001")
    monkeypatch.setenv(ENV_TIMEOUT_SECONDS, "2.5")
    monkeypatch.setenv(ENV_REQUIRED, "false")

    config = load_questdb_health_config(config_path, load_dotenv_file=False)

    assert config.http_scheme == "http"
    assert config.http_host == "env-host"
    assert config.http_port == 9001
    assert config.timeout_seconds == 2.5
    assert config.required is False


def test_explicit_values_override_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    config_path = _write_yaml(
        tmp_path / "questdb.yaml",
        """
connection:
  default_http_host: yaml-host
  default_http_port: 19000
health_check:
  required_by_default: false
""",
    )
    monkeypatch.setenv(ENV_HTTP_SCHEME, "http")
    monkeypatch.setenv(ENV_HTTP_HOST, "env-host")
    monkeypatch.setenv(ENV_HTTP_PORT, "9001")
    monkeypatch.setenv(ENV_TIMEOUT_SECONDS, "2.5")
    monkeypatch.setenv(ENV_REQUIRED, "true")

    config = load_questdb_health_config(
        config_path,
        http_scheme="https",
        http_host="explicit-host",
        http_port="9443",
        timeout_seconds="0.75",
        required=False,
        load_dotenv_file=False,
    )

    assert config.http_scheme == "https"
    assert config.http_host == "explicit-host"
    assert config.http_port == 9443
    assert config.timeout_seconds == 0.75
    assert config.required is False


@pytest.mark.parametrize(
    ("yaml_text", "match"),
    [
        ("connection:\n  default_http_scheme: ftp\n", "http_scheme"),
        ("connection:\n  default_http_host: ''\n", "http_host"),
        ("connection:\n  default_http_port: 70000\n", "http_port"),
        ("connection:\n  default_health_timeout_seconds: -1\n", "timeout"),
        ("health_check:\n  required_by_default: yes please\n", "required"),
        ("connection: bad\n", "connection section"),
    ],
)
def test_invalid_yaml_values_fail_clearly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    yaml_text: str,
    match: str,
) -> None:
    _clear_env(monkeypatch)
    config_path = _write_yaml(tmp_path / "questdb.yaml", yaml_text)

    with pytest.raises(QuestDBHealthError, match=match):
        load_questdb_health_config(config_path, load_dotenv_file=False)


def test_build_exec_url_returns_expected_endpoint() -> None:
    config = QuestDBHealthConfig(http_scheme="http", http_host="localhost", http_port=9000)

    assert build_questdb_exec_url(config) == "http://localhost:9000/exec"


def test_validate_exec_payload_accepts_full_select_one_result() -> None:
    assert (
        _validate_exec_payload(
            {
                "columns": [{"name": "1", "type": "INT"}],
                "dataset": [[1]],
                "count": 1,
            }
        )
        is None
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "SELECT 1"},
        {"columns": [{"name": "1", "type": "INT"}]},
        {"dataset": [[1]]},
        {"error": "bad query"},
        {"columns": [{"name": "1", "type": "INT"}], "dataset": []},
        {"columns": [{"name": "1", "type": "INT"}], "dataset": [[]]},
        {"columns": [{"name": "1", "type": "INT"}], "dataset": [[2]]},
        {"columns": [{"name": "1", "type": "INT"}], "dataset": [[1]], "count": 0},
        {"columns": [{"name": "1", "type": "INT"}], "dataset": [[1]], "count": "bad"},
    ],
)
def test_validate_exec_payload_rejects_malformed_select_one_results(payload: object) -> None:
    assert _validate_exec_payload(payload) is not None


def test_check_http_success_parses_select_one_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse(
            200,
            {
                "query": "SELECT 1",
                "columns": [{"name": "1", "type": "INT"}],
                "dataset": [[1]],
                "count": 1,
            },
        )

    monkeypatch.setattr(requests, "get", fake_get)

    result = check_questdb_http(QuestDBHealthConfig())

    assert result.reachable is True
    assert result.failure_kind is None
    assert result.status_code == 200
    assert result.latency_ms is not None and result.latency_ms >= 0
    assert calls[0]["url"] == "http://localhost:9000/exec"
    assert calls[0]["params"] == {"query": "SELECT 1"}
    assert calls[0]["timeout"] == 3.0


def test_check_http_json_error_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(200, {"error": "syntax error"}),
    )

    result = check_questdb_http(QuestDBHealthConfig())

    assert result.reachable is False
    assert result.failure_kind == FAILURE_UNHEALTHY
    assert "syntax error" in result.message


def test_check_http_non_200_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(500, {"error": "server error"}),
    )

    result = check_questdb_http(QuestDBHealthConfig())

    assert result.reachable is False
    assert result.failure_kind == FAILURE_UNHEALTHY
    assert result.status_code == 500
    assert "HTTP 500" in result.message


def test_check_http_invalid_json_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(200, ValueError("bad json")),
    )

    result = check_questdb_http(QuestDBHealthConfig())

    assert result.reachable is False
    assert result.failure_kind == FAILURE_UNHEALTHY
    assert "invalid JSON" in result.message


def test_check_http_unrecognized_success_json_is_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(200, {"ok": True}),
    )

    result = check_questdb_http(QuestDBHealthConfig())

    assert result.reachable is False
    assert result.failure_kind == FAILURE_UNHEALTHY
    assert "columns" in result.message


def test_check_http_query_only_payload_is_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(200, {"query": "SELECT 1"}),
    )

    result = check_questdb_http(QuestDBHealthConfig())

    assert result.reachable is False
    assert result.failure_kind == FAILURE_UNHEALTHY
    assert "columns" in result.message


def test_connection_exception_is_not_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(requests, "get", fake_get)

    result = check_questdb_http(QuestDBHealthConfig())

    assert result.reachable is False
    assert result.failure_kind == FAILURE_UNREACHABLE
    assert result.status_code is None
    assert "connection refused" in result.message


def test_required_connection_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        raise requests.Timeout("timed out")

    monkeypatch.setattr(requests, "get", fake_get)

    with pytest.raises(QuestDBHealthError) as exc_info:
        check_questdb_http(QuestDBHealthConfig(required=True))

    assert exc_info.value.result is not None
    assert exc_info.value.result.failure_kind == FAILURE_UNREACHABLE


def test_required_unhealthy_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(200, {"error": "not ready"}),
    )

    with pytest.raises(QuestDBHealthError) as exc_info:
        check_questdb_http(QuestDBHealthConfig(required=True))

    assert exc_info.value.result is not None
    assert exc_info.value.result.failure_kind == FAILURE_UNHEALTHY


def test_format_optional_connection_failure_uses_skip() -> None:
    result = QuestDBHealthResult(
        reachable=False,
        required=False,
        url="http://localhost:9000/exec",
        status_code=None,
        message="connection refused",
        query="SELECT 1",
        latency_ms=1.0,
        failure_kind=FAILURE_UNREACHABLE,
    )

    assert format_questdb_health_result(result).startswith("[SKIP]")


def test_format_optional_unhealthy_uses_warn() -> None:
    result = QuestDBHealthResult(
        reachable=False,
        required=False,
        url="http://localhost:9000/exec",
        status_code=500,
        message="HTTP 500",
        query="SELECT 1",
        latency_ms=1.0,
        failure_kind=FAILURE_UNHEALTHY,
    )

    assert format_questdb_health_result(result).startswith("[WARN]")


def test_script_optional_connection_failure_exits_zero_with_skip(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import check_questdb

    result = QuestDBHealthResult(
        reachable=False,
        required=False,
        url="http://localhost:9000/exec",
        status_code=None,
        message="connection refused",
        query="SELECT 1",
        latency_ms=1.0,
        failure_kind=FAILURE_UNREACHABLE,
    )
    monkeypatch.setattr(check_questdb, "load_questdb_health_config", lambda **kwargs: QuestDBHealthConfig())
    monkeypatch.setattr(check_questdb, "check_questdb_http", lambda config: result)

    assert check_questdb.main([]) == 0
    assert "[SKIP]" in capsys.readouterr().out


def test_script_optional_unhealthy_exits_zero_with_warn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import check_questdb

    result = QuestDBHealthResult(
        reachable=False,
        required=False,
        url="http://localhost:9000/exec",
        status_code=500,
        message="HTTP 500",
        query="SELECT 1",
        latency_ms=1.0,
        failure_kind=FAILURE_UNHEALTHY,
    )
    monkeypatch.setattr(check_questdb, "load_questdb_health_config", lambda **kwargs: QuestDBHealthConfig())
    monkeypatch.setattr(check_questdb, "check_questdb_http", lambda config: result)

    assert check_questdb.main([]) == 0
    assert "[WARN]" in capsys.readouterr().out


def test_script_required_connection_failure_exits_nonzero_with_fail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import check_questdb

    result = QuestDBHealthResult(
        reachable=False,
        required=True,
        url="http://localhost:9000/exec",
        status_code=None,
        message="connection refused",
        query="SELECT 1",
        latency_ms=1.0,
        failure_kind=FAILURE_UNREACHABLE,
    )

    def raise_failure(config: QuestDBHealthConfig) -> QuestDBHealthResult:
        raise QuestDBHealthError(result.message, result=result)

    monkeypatch.setattr(check_questdb, "load_questdb_health_config", lambda **kwargs: QuestDBHealthConfig(required=True))
    monkeypatch.setattr(check_questdb, "check_questdb_http", raise_failure)

    assert check_questdb.main(["--required"]) == 1
    assert "[FAIL]" in capsys.readouterr().out


def test_script_required_unhealthy_exits_nonzero_with_fail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import check_questdb

    result = QuestDBHealthResult(
        reachable=False,
        required=True,
        url="http://localhost:9000/exec",
        status_code=500,
        message="HTTP 500",
        query="SELECT 1",
        latency_ms=1.0,
        failure_kind=FAILURE_UNHEALTHY,
    )

    def raise_failure(config: QuestDBHealthConfig) -> QuestDBHealthResult:
        raise QuestDBHealthError(result.message, result=result)

    monkeypatch.setattr(check_questdb, "load_questdb_health_config", lambda **kwargs: QuestDBHealthConfig(required=True))
    monkeypatch.setattr(check_questdb, "check_questdb_http", raise_failure)

    assert check_questdb.main(["--required"]) == 1
    assert "[FAIL]" in capsys.readouterr().out


def test_docs_explain_exec_select_one_and_pr12_context_snapshot_warning() -> None:
    text = (REPO_ROOT / "docs" / "questdb_health.md").read_text(encoding="utf-8")

    assert "/exec" in text
    assert "SELECT 1" in text
    assert "SQL query engine" in text
    assert "context_snapshot_id" in text
    assert "context_state_snapshots" in text
