from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import math
from pathlib import Path

import pytest
import requests

from market_relay_engine.contracts.context import (
    ContextAIEvent,
    ContextClassificationEventType,
    ContextClassificationRequest,
    ContextClassificationResponse,
    ContextClassificationStatus,
    ContextFlag,
    ContextIndicatorSnapshot,
    ContextRiskLevel,
    ContextStateSnapshot,
    ContextUrgency,
    ContextValidationResult,
    ShadowContextAction,
    ShadowContextPolicyEvaluation,
)
from market_relay_engine.contracts.execution import (
    FillEvent,
    OrderEvent,
    OrderSide,
    OrderStatus,
    OrderType,
)
from market_relay_engine.contracts.features import FeatureSnapshot
from market_relay_engine.contracts.ledger import LatencyMetric, TradeOutcome
from market_relay_engine.contracts.model import ModelSignal, SignalSide
from market_relay_engine.contracts.risk import RiskDecision, RiskDecisionType
from market_relay_engine.contracts.system import SystemHealthEvent
from market_relay_engine.market_data.cost_model import estimate_cost_from_expected_move
from market_relay_engine.questdb.writer import (
    ALLOWED_LEDGER_TABLES,
    TABLE_COLUMNS,
    QuestDBLedgerWriter,
    QuestDBWriteConfig,
    QuestDBWriteError,
    build_insert_sql,
    context_ai_event_to_row,
    context_classification_attempt_to_row,
    context_flag_to_row,
    context_indicator_snapshot_to_row,
    context_state_snapshot_to_row,
    cost_estimate_to_row,
    encoded_exec_url_length,
    feature_snapshot_to_row,
    fill_event_to_row,
    latency_metric_to_row,
    load_questdb_write_config,
    model_signal_to_row,
    order_event_to_row,
    risk_decision_to_row,
    sanitize_sql_string,
    shadow_context_policy_evaluation_to_row,
    sql_literal,
    system_health_event_to_row,
    timestamp_sql_literal,
    trade_outcome_to_row,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_TIME = datetime(2026, 5, 22, 15, 30, 0, tzinfo=UTC)


class FakeResponse:
    def __init__(self, status_code: int, payload: object | Exception) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_sql_literal_handles_supported_values() -> None:
    assert sql_literal(None) == "null"
    assert sql_literal(True) == "true"
    assert sql_literal(False) == "false"
    assert sql_literal(7) == "7"
    assert sql_literal(1.25) == "1.25"
    assert sql_literal("Fed's rate hike") == "'Fed''s rate hike'"
    assert sql_literal(SignalSide.BUY) == "'BUY'"
    assert timestamp_sql_literal(EXAMPLE_TIME) == "'2026-05-22T15:30:00.000000Z'"


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_sql_literal_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises(QuestDBWriteError, match="finite"):
        sql_literal(value)


def test_sql_literal_rejects_naive_datetimes() -> None:
    with pytest.raises(QuestDBWriteError, match="timezone-aware"):
        sql_literal(datetime(2026, 5, 22, 15, 30, 0))


def test_sanitize_sql_string_removes_control_chars_and_escapes_apostrophe() -> None:
    assert sanitize_sql_string("a\x00b\nc\td's") == "ab c d''s"


def test_json_apostrophes_are_escaped_in_insert_sql() -> None:
    sql = build_insert_sql(
        "feature_snapshots",
        {
            "snapshot_time": EXAMPLE_TIME,
            "features_json": {"summary": "Fed's rate hike"},
        },
    )

    assert "Fed''s rate hike" in sql


def test_nested_json_apostrophes_are_escaped_in_insert_sql() -> None:
    sql = build_insert_sql(
        "context_state_snapshots",
        {
            "snapshot_time": EXAMPLE_TIME,
            "context_summary_json": {"nested": {"summary": "Fed's"}},
        },
    )

    assert "Fed''s" in sql


def test_insert_builder_preserves_column_order_and_escapes_values() -> None:
    row = {
        "event_time": EXAMPLE_TIME,
        "health_event_id": "health_1",
        "message": "can't break SQL",
    }

    sql = build_insert_sql("system_health_events", row)

    assert sql.startswith(
        "INSERT INTO system_health_events (event_time, health_event_id, message)"
    )
    assert "can''t break SQL" in sql


def test_insert_builder_rejects_unknown_table_unknown_column_and_empty_row() -> None:
    with pytest.raises(QuestDBWriteError, match="unknown QuestDB ledger table"):
        build_insert_sql("raw_trades", {"event_time": EXAMPLE_TIME})
    with pytest.raises(QuestDBWriteError, match="unknown columns"):
        build_insert_sql("system_health_events", {"bad_column": "x"})
    with pytest.raises(QuestDBWriteError, match="non-empty"):
        build_insert_sql("system_health_events", {})


def test_config_validates_positive_max_sql_length() -> None:
    assert QuestDBWriteConfig(max_sql_length_chars=1).max_sql_length_chars == 1
    with pytest.raises(QuestDBWriteError, match="max_sql_length_chars"):
        QuestDBWriteConfig(max_sql_length_chars=0)


def test_load_write_config_uses_env_max_sql_length(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_write_env(monkeypatch)
    monkeypatch.setenv("QUESTDB_MAX_SQL_LENGTH_CHARS", "1234")

    config = load_questdb_write_config(
        tmp_path / "missing.yaml",
        load_dotenv_file=False,
    )

    assert config.max_sql_length_chars == 1234


def test_load_write_config_writer_yaml_overrides_connection_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_write_env(monkeypatch)
    config_path = tmp_path / "questdb.yaml"
    config_path.write_text(
        """
connection:
  default_http_scheme: http
  default_http_host: connection-host
  default_http_port: 9000
  default_health_timeout_seconds: 3.0
writer:
  http_scheme: https
  http_host: writer-host
  http_port: 9100
  timeout_seconds: 4.5
""",
        encoding="utf-8",
    )

    config = load_questdb_write_config(config_path, load_dotenv_file=False)

    assert config.http_scheme == "https"
    assert config.http_host == "writer-host"
    assert config.http_port == 9100
    assert config.timeout_seconds == 4.5


def test_load_write_config_yaml_falls_back_to_connection_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_write_env(monkeypatch)
    config_path = tmp_path / "questdb.yaml"
    config_path.write_text(
        """
connection:
  default_http_host: connection-host
  default_http_port: 9000
  default_health_timeout_seconds: 2.5
writer:
  max_sql_length_chars: 5000
""",
        encoding="utf-8",
    )

    config = load_questdb_write_config(config_path, load_dotenv_file=False)

    assert config.http_host == "connection-host"
    assert config.http_port == 9000
    assert config.timeout_seconds == 2.5
    assert config.max_sql_length_chars == 5000


def test_load_write_config_empty_writer_value_does_not_fall_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_write_env(monkeypatch)
    config_path = tmp_path / "questdb.yaml"
    config_path.write_text(
        """
connection:
  default_http_host: connection-host
writer:
  http_host: ""
""",
        encoding="utf-8",
    )

    with pytest.raises(QuestDBWriteError, match="http_host"):
        load_questdb_write_config(config_path, load_dotenv_file=False)


def test_load_write_config_env_and_explicit_override_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_write_env(monkeypatch)
    config_path = tmp_path / "questdb.yaml"
    config_path.write_text(
        """
connection:
  default_http_host: connection-host
writer:
  http_host: writer-host
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("QUESTDB_HTTP_HOST", "env-host")

    env_config = load_questdb_write_config(config_path, load_dotenv_file=False)
    explicit_config = load_questdb_write_config(
        config_path,
        http_host="explicit-host",
        load_dotenv_file=False,
    )

    assert env_config.http_host == "env-host"
    assert explicit_config.http_host == "explicit-host"


def test_encoded_exec_url_length_exceeds_raw_sql_for_encoded_values() -> None:
    sql = build_insert_sql(
        "feature_snapshots",
        {
            "snapshot_time": EXAMPLE_TIME,
            "features_json": {"summary": "Fed's rate hike with spaces"},
        },
    )

    assert encoded_exec_url_length("http://localhost:9000/exec", sql) > len(sql)


def test_write_row_success_uses_get_exec_with_fmt_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse(200, {"ddl": "OK"})

    monkeypatch.setattr(requests, "get", fake_get)

    result = QuestDBLedgerWriter(QuestDBWriteConfig()).write_raw_row(
        "system_health_events",
        {
            "event_time": EXAMPLE_TIME,
            "write_time": EXAMPLE_TIME,
            "health_event_id": "health_1",
            "component": "unit_test",
            "status": "ok",
        },
    )

    assert result.success is True
    assert result.table_name == "system_health_events"
    assert calls[0]["url"] == "http://localhost:9000/exec"
    assert calls[0]["params"]["fmt"] == "json"  # type: ignore[index]
    assert "INSERT INTO system_health_events" in calls[0]["params"]["query"]  # type: ignore[index]
    assert calls[0]["timeout"] == 3.0


def test_write_row_rejects_long_sql_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        calls.append("called")
        return FakeResponse(200, {"ddl": "OK"})

    monkeypatch.setattr(requests, "get", fake_get)
    writer = QuestDBLedgerWriter(QuestDBWriteConfig(max_sql_length_chars=80))

    with pytest.raises(QuestDBWriteError, match="too long for safe /exec GET"):
        writer.write_raw_row(
            "system_health_events",
            {
                "event_time": EXAMPLE_TIME,
                "write_time": EXAMPLE_TIME,
                "health_event_id": "health_1",
                "message": "x" * 200,
            },
        )

    assert calls == []


def test_write_row_rejects_encoded_url_over_limit_when_raw_sql_is_under_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    row = {
        "snapshot_time": EXAMPLE_TIME,
        "features_json": {"summary": "Fed's rate hike with spaces"},
    }
    raw_sql = build_insert_sql("feature_snapshots", row)

    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        calls.append("called")
        return FakeResponse(200, {"ddl": "OK"})

    monkeypatch.setattr(requests, "get", fake_get)
    writer = QuestDBLedgerWriter(
        QuestDBWriteConfig(max_sql_length_chars=len(raw_sql) + 1)
    )

    with pytest.raises(QuestDBWriteError, match="encoded /exec GET request"):
        writer.write_raw_row("feature_snapshots", row)

    assert calls == []


def test_write_row_allows_normal_insert_under_length_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(200, {"ddl": "OK"}),
    )

    result = QuestDBLedgerWriter(
        QuestDBWriteConfig(max_sql_length_chars=1000)
    ).write_raw_row("system_health_events", {"event_time": EXAMPLE_TIME})

    assert result.success is True


def test_write_row_non_200_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(500, {"error": "server error"}),
    )

    with pytest.raises(QuestDBWriteError, match="HTTP 500"):
        QuestDBLedgerWriter(QuestDBWriteConfig()).write_raw_row(
            "system_health_events",
            {"event_time": EXAMPLE_TIME},
        )


def test_write_row_json_error_payload_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(200, {"error": "syntax error"}),
    )

    with pytest.raises(QuestDBWriteError, match="syntax error"):
        QuestDBLedgerWriter(QuestDBWriteConfig()).write_raw_row(
            "system_health_events",
            {"event_time": EXAMPLE_TIME},
        )


def test_write_row_invalid_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(200, ValueError("bad json")),
    )

    with pytest.raises(QuestDBWriteError, match="invalid JSON"):
        QuestDBLedgerWriter(QuestDBWriteConfig()).write_raw_row(
            "system_health_events",
            {"event_time": EXAMPLE_TIME},
        )


def test_write_row_non_object_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: FakeResponse(200, ["not", "object"]),
    )

    with pytest.raises(QuestDBWriteError, match="non-object"):
        QuestDBLedgerWriter(QuestDBWriteConfig()).write_raw_row(
            "system_health_events",
            {"event_time": EXAMPLE_TIME},
        )


def test_write_row_request_exception_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        raise requests.Timeout("timed out")

    monkeypatch.setattr(requests, "get", fake_get)

    with pytest.raises(QuestDBWriteError, match="timed out"):
        QuestDBLedgerWriter(QuestDBWriteConfig()).write_raw_row(
            "system_health_events",
            {"event_time": EXAMPLE_TIME},
        )


def test_feature_snapshot_maps_to_feature_snapshots_row() -> None:
    feature = _feature_snapshot()
    row = feature_snapshot_to_row(feature, run_id="run_1", session_id="session_1")

    assert row["feature_snapshot_id"] == feature.feature_snapshot_id
    assert row["features_json"] == '{"midprice":100.25,"summary":"Fed\'s"}'
    assert row["run_id"] == "run_1"
    assert row["session_id"] == "session_1"


def test_model_signal_maps_to_model_signals_row() -> None:
    signal = _model_signal()
    row = model_signal_to_row(signal)

    assert row["signal_id"] == signal.signal_id
    assert row["signal"] == SignalSide.BUY
    assert row["feature_snapshot_id"] == signal.feature_snapshot_id


def test_cost_estimate_maps_to_cost_estimates_row() -> None:
    estimate = estimate_cost_from_expected_move(
        ticker="XOM",
        side=SignalSide.BUY,
        expected_gross_move_bps=5.0,
        horizon="1m",
        midprice=100.25,
        spread_bps=1.2,
    )
    row = cost_estimate_to_row(
        estimate,
        signal_id="signal_1",
        feature_snapshot_id="feature_1",
        write_time=EXAMPLE_TIME,
    )

    assert row["cost_estimate_id"].startswith("cost_estimate_")
    assert row["signal_id"] == "signal_1"
    assert row["feature_snapshot_id"] == "feature_1"
    assert row["write_time"] == EXAMPLE_TIME


def test_risk_decision_maps_to_risk_decisions_row() -> None:
    decision = RiskDecision(
        decision_time=EXAMPLE_TIME,
        ticker="XOM",
        model_signal_id="signal_1",
        decision=RiskDecisionType.BLOCK,
        approved=False,
        risk_version="risk_v1",
        reasons=["Fed's headline"],
        thresholds_used={"max_spread_bps": 10},
        context_snapshot_id="context_snapshot_1",
    )
    row = risk_decision_to_row(decision, cost_estimate_id="cost_1")

    assert row["risk_decision_id"] == decision.risk_decision_id
    assert row["cost_estimate_id"] == "cost_1"
    assert row["reasons_json"] == '["Fed\'s headline"]'


def test_context_records_map_to_context_rows() -> None:
    indicator = ContextIndicatorSnapshot(
        snapshot_time=EXAMPLE_TIME,
        source="calendar",
        ticker_or_sector="oil",
        indicator_name="eia_window",
        value={"summary": "Fed's"},
        details={"release_id": "release_1", "nested": {"verified": True}},
    )
    ai_event = ContextAIEvent(
        event_time=EXAMPLE_TIME,
        source="ai",
        source_id="source_1",
        affected_tickers=["XOM"],
        event_type=ContextClassificationEventType.OTHER,
        summary="Fed's headline",
    )
    flag = ContextFlag(
        event_time=EXAMPLE_TIME,
        source="ai",
        flag_type="context_risk",
        severity="normal",
        ticker="XOM",
    )
    state = _context_state()

    indicator_row = context_indicator_snapshot_to_row(indicator)
    assert indicator_row["value_json"] == '{"summary":"Fed\'s"}'
    assert indicator_row["details_json"] == '{"nested":{"verified":true},"release_id":"release_1"}'
    ai_event_row = context_ai_event_to_row(ai_event)
    flag_row = context_flag_to_row(flag)
    assert tuple(ai_event_row) == TABLE_COLUMNS["context_ai_events"]
    assert ai_event_row["context_event_id"] == ai_event.context_event_id
    assert ai_event_row["event_type"] == "OTHER"
    assert tuple(flag_row) == TABLE_COLUMNS["context_flags"]
    assert flag_row["context_flag_id"] == flag.context_flag_id
    assert flag_row["reason_codes_json"] == "[]"
    assert context_state_snapshot_to_row(state)["context_snapshot_id"] == state.context_snapshot_id


def test_phase7_ledger_table_columns_are_exact_and_metadata_only() -> None:
    assert TABLE_COLUMNS["context_classification_attempts"] == tuple(
        "requested_at write_time classification_attempt_id classification_request_id "
        "raw_input_id source_document_id source source_type source_platform source_uri "
        "source_locator affected_tickers_json raw_input_hash document_hash "
        "source_published_at source_updated_at collected_at normalized_at classified_at provider "
        "model_version prompt_version status event_type risk_level urgency confidence "
        "summary validation_result_id validation_outcome validation_reason_codes_json "
        "validator_version validated_at provider_latency_ms safe_failure_category "
        "safe_failure_summary run_id session_id schema_version trace_id "
        "provider_request_count retry_count deduplicated "
        "reused_classification_attempt_id".split()
    )
    assert TABLE_COLUMNS["shadow_context_policy_evaluations"] == tuple(
        "decision_evaluation_time write_time shadow_evaluation_id model_signal_id risk_decision_id "
        "matched_context_event_ids_json matched_context_flag_ids_json "
        "shadow_context_fingerprint policy_version policy_config_hash hypothetical_action "
        "proposed_size_factor reason_codes_json run_id session_id schema_version trace_id".split()
    )
    assert ALLOWED_LEDGER_TABLES[-9:-7] == (
        "context_classification_attempts",
        "shadow_context_policy_evaluations",
    )
    forbidden = {
        "input_text",
        "prompt_text",
        "raw_text",
        "document_body",
        "exception",
        "traceback",
        "secret",
        "credential",
    }
    for table_name in (
        "context_ai_events",
        "context_flags",
        "context_classification_attempts",
        "shadow_context_policy_evaluations",
    ):
        assert not forbidden.intersection(TABLE_COLUMNS[table_name])


def test_context_classification_attempt_maps_trusted_metadata_and_enum_values() -> None:
    request, response, validation = _classification_records()

    row = context_classification_attempt_to_row(
        request,
        response,
        validation_result=validation,
        run_id="run_1",
        session_id="session_1",
        write_time=EXAMPLE_TIME,
    )

    assert tuple(row) == TABLE_COLUMNS["context_classification_attempts"]
    assert row["classification_attempt_id"] == response.classification_attempt_id
    assert row["write_time"] == EXAMPLE_TIME
    assert row["classification_request_id"] == request.classification_request_id
    assert row["affected_tickers_json"] == '["XOM","CVX"]'
    assert row["status"] == "VALID"
    assert row["event_type"] == "SEC_8K_RESULTS"
    assert row["risk_level"] == "MEDIUM"
    assert row["urgency"] == "HIGH"
    assert row["validation_outcome"] is True
    assert row["validation_reason_codes_json"] == "[]"
    assert row["provider_latency_ms"] == 125.5
    assert row["safe_failure_category"] is None
    assert row["safe_failure_summary"] is None
    assert row["provider_request_count"] == 1
    assert row["retry_count"] == 0
    assert row["deduplicated"] is False
    assert row["reused_classification_attempt_id"] is None
    assert row["trace_id"] == "trace_phase7"
    assert "input_text" not in row
    assert "safe_detail" not in row
    assert "exception" not in row
    assert "traceback" not in row


def test_context_classification_attempt_without_validation_uses_null_metadata() -> None:
    request, response, _ = _classification_records()

    row = context_classification_attempt_to_row(request, response)

    assert row["validation_result_id"] is None
    assert row["validation_outcome"] is None
    assert row["validation_reason_codes_json"] is None
    assert row["validator_version"] is None
    assert row["validated_at"] is None


def test_context_classification_attempt_maps_retry_and_deduplication_accounting() -> None:
    request, response, _ = _classification_records()
    retried = replace(response, provider_request_count=3, retry_count=2)
    deduplicated = replace(
        response,
        provider_request_count=0,
        retry_count=0,
        deduplicated=True,
        reused_classification_attempt_id="classification_attempt_original",
    )

    retried_row = context_classification_attempt_to_row(request, retried)
    deduplicated_row = context_classification_attempt_to_row(request, deduplicated)

    assert retried_row["provider_request_count"] == 3
    assert retried_row["retry_count"] == 2
    assert retried_row["deduplicated"] is False
    assert deduplicated_row["provider_request_count"] == 0
    assert deduplicated_row["retry_count"] == 0
    assert deduplicated_row["deduplicated"] is True
    assert (
        deduplicated_row["reused_classification_attempt_id"]
        == "classification_attempt_original"
    )


def test_provider_failure_row_keeps_only_safe_failure_fields() -> None:
    request, _, _ = _classification_records()
    response = ContextClassificationResponse(
        classification_request_id=request.classification_request_id,
        classified_at=EXAMPLE_TIME,
        provider="gemini",
        model_version="gemini-model-v1",
        prompt_version=request.prompt_version,
        status=ContextClassificationStatus.PROVIDER_FAILED,
        provider_latency_ms=200.0,
        safe_failure_category="timeout",
        safe_failure_summary="Provider timed out before a response was available.",
        trace_id=request.trace_id,
    )

    row = context_classification_attempt_to_row(request, response)

    assert row["safe_failure_category"] == "timeout"
    assert row["safe_failure_summary"] == "Provider timed out before a response was available."
    assert row["status"] == "PROVIDER_FAILED"
    assert row["event_type"] == "UNKNOWN"
    assert row["confidence"] is None
    assert not {"exception", "traceback", "raw_provider_response"}.intersection(row)


@pytest.mark.parametrize(
    ("response_change", "validation_change", "match"),
    [
        ({"classification_request_id": "classification_request_other"}, {}, "response.classification_request_id"),
        ({"prompt_version": "prompt_other"}, {}, "response.prompt_version"),
        ({}, {"classification_request_id": "classification_request_other"}, "validation_result.classification_request_id"),
        ({}, {"classification_attempt_id": "classification_attempt_other"}, "validation_result.classification_attempt_id"),
        ({"trace_id": "trace_other"}, {}, "trace_id values must match"),
    ],
)
def test_context_classification_attempt_rejects_cross_record_mismatches(
    response_change: dict[str, object],
    validation_change: dict[str, object],
    match: str,
) -> None:
    request, response, validation = _classification_records()

    with pytest.raises(QuestDBWriteError, match=match):
        context_classification_attempt_to_row(
            request,
            replace(response, **response_change),
            validation_result=replace(validation, **validation_change),
        )


def test_shadow_context_policy_evaluation_maps_json_and_enum_values() -> None:
    evaluation = ShadowContextPolicyEvaluation(
        model_signal_id="signal_1",
        risk_decision_id="risk_1",
        decision_evaluation_time=EXAMPLE_TIME,
        matched_context_event_ids=["event_1", "event_2"],
        matched_context_flag_ids=["flag_1"],
        shadow_context_fingerprint="c" * 64,
        policy_version="shadow_policy_v1",
        policy_config_hash="d" * 64,
        hypothetical_action=ShadowContextAction.REDUCE_SIZE,
        proposed_size_factor=0.5,
        reason_codes=["high_context_risk"],
        trace_id="trace_phase7",
    )

    row = shadow_context_policy_evaluation_to_row(
        evaluation,
        run_id="run_1",
        session_id="session_1",
        write_time=EXAMPLE_TIME,
    )

    assert tuple(row) == TABLE_COLUMNS["shadow_context_policy_evaluations"]
    assert row["matched_context_event_ids_json"] == '["event_1","event_2"]'
    assert row["write_time"] == EXAMPLE_TIME
    assert row["matched_context_flag_ids_json"] == '["flag_1"]'
    assert row["hypothetical_action"] == "REDUCE_SIZE"
    assert row["proposed_size_factor"] == 0.5
    assert row["reason_codes_json"] == '["high_context_risk"]'


def test_phase7_writer_convenience_methods_use_canonical_mappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = QuestDBLedgerWriter(QuestDBWriteConfig())
    captured: list[tuple[str, dict[str, object]]] = []

    def fake_write_row(table_name: str, row: dict[str, object]) -> str:
        captured.append((table_name, row))
        return "written"

    monkeypatch.setattr(writer, "write_row", fake_write_row)
    request, response, validation = _classification_records()
    ai_event = ContextAIEvent(
        event_time=EXAMPLE_TIME,
        source="sec_edgar",
        source_id="accession:writer-test",
        affected_tickers=["XOM"],
        event_type=ContextClassificationEventType.OTHER,
    )
    evaluation = ShadowContextPolicyEvaluation(
        model_signal_id="signal_1",
        decision_evaluation_time=EXAMPLE_TIME,
        shadow_context_fingerprint="c" * 64,
        policy_version="shadow_policy_v1",
        policy_config_hash="d" * 64,
        hypothetical_action=ShadowContextAction.NO_CHANGE,
    )

    assert writer.write_context_ai_event(ai_event, write_time=EXAMPLE_TIME) == "written"
    assert writer.write_context_classification_attempt(
        request,
        response,
        validation,
        write_time=EXAMPLE_TIME,
    ) == "written"
    assert writer.write_shadow_context_policy_evaluation(
        evaluation,
        write_time=EXAMPLE_TIME,
    ) == "written"
    assert captured[0] == (
        "context_ai_events",
        context_ai_event_to_row(ai_event, write_time=EXAMPLE_TIME),
    )
    assert captured[1] == (
        "context_classification_attempts",
        context_classification_attempt_to_row(
            request,
            response,
            validation_result=validation,
            write_time=EXAMPLE_TIME,
        ),
    )
    assert captured[2] == (
        "shadow_context_policy_evaluations",
        shadow_context_policy_evaluation_to_row(
            evaluation,
            write_time=EXAMPLE_TIME,
        ),
    )


def test_phase7_ledger_rejects_unknown_raw_text_columns() -> None:
    for table_name, timestamp_column in (
        ("context_classification_attempts", "requested_at"),
        ("shadow_context_policy_evaluations", "decision_evaluation_time"),
    ):
        with pytest.raises(QuestDBWriteError, match="unknown columns"):
            build_insert_sql(
                table_name,
                {timestamp_column: EXAMPLE_TIME, "input_text": "must remain in memory"},
            )


def test_context_indicator_details_column_is_allowed_and_unknown_columns_rejected() -> None:
    sql = build_insert_sql(
        "context_indicator_snapshots",
        {"snapshot_time": EXAMPLE_TIME, "details_json": "{}"},
    )
    assert "details_json" in sql
    with pytest.raises(QuestDBWriteError, match="unknown columns"):
        build_insert_sql(
            "context_indicator_snapshots",
            {"snapshot_time": EXAMPLE_TIME, "not_migrated_column": "x"},
        )


def test_writer_context_flag_convenience_uses_existing_converter(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = QuestDBLedgerWriter(QuestDBWriteConfig())
    captured: dict[str, object] = {}

    def fake_write_row(table_name: str, row: dict[str, object]) -> str:
        captured["table"] = table_name
        captured["row"] = row
        return "written"

    monkeypatch.setattr(writer, "write_row", fake_write_row)
    flag = ContextFlag(
        event_time=EXAMPLE_TIME,
        source="eia_wpsr_v1",
        flag_type="eia_wpsr_event_window",
        severity="NORMAL",
        ticker="XOM",
    )
    assert writer.write_context_flag(flag) == "written"
    assert captured["table"] == "context_flags"
    assert captured["row"] == context_flag_to_row(flag)


def test_context_state_snapshot_mapper_preserves_explicit_write_time() -> None:
    row = context_state_snapshot_to_row(_context_state(), write_time=EXAMPLE_TIME)

    assert row["write_time"] == EXAMPLE_TIME
    assert row["context_summary_json"] == '{"summary":"Fed\'s rate hike"}'


def test_mapper_without_write_time_generates_utc_aware_value() -> None:
    row = model_signal_to_row(_model_signal())

    assert row["write_time"].tzinfo is not None
    assert row["write_time"].utcoffset() == timedelta(0)


def test_execution_outcome_latency_and_health_records_map_to_rows() -> None:
    order = OrderEvent(
        order_time=EXAMPLE_TIME,
        ticker="XOM",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=1,
        status=OrderStatus.SUBMITTED,
        expected_price=100.25,
        submitted_price=100.24,
    )
    fill = FillEvent(
        fill_time=EXAMPLE_TIME,
        order_id=order.order_id,
        ticker="XOM",
        side=OrderSide.BUY,
        quantity=1,
        fill_price=100.26,
        expected_price=100.25,
        slippage=0.01,
    )
    outcome = TradeOutcome(
        signal_id="signal_1",
        order_id=order.order_id,
        ticker="XOM",
        entry_time=EXAMPLE_TIME,
        exit_time=EXAMPLE_TIME + timedelta(minutes=5),
        realized_pnl=1.0,
    )
    latency = LatencyMetric(
        measured_time=EXAMPLE_TIME,
        component="feature_builder",
        latency_ms=12.5,
        source="local_timer",
    )
    health = SystemHealthEvent(
        event_time=EXAMPLE_TIME,
        component="writer",
        status="ok",
    )

    assert order_event_to_row(order, broker_order_id="broker_order_1")["order_id"] == order.order_id
    assert fill_event_to_row(fill, broker_fill_id="broker_fill_1")["fill_id"] == fill.fill_id
    assert trade_outcome_to_row(outcome, fill_id=fill.fill_id)["fill_id"] == fill.fill_id
    assert latency_metric_to_row(latency, ticker="XOM")["ticker"] == "XOM"
    assert system_health_event_to_row(health, queue_depth=1)["queue_depth"] == 1


def test_write_row_does_not_replace_existing_write_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse(200, {"ddl": "OK"})

    monkeypatch.setattr(requests, "get", fake_get)
    row = {
        "event_time": EXAMPLE_TIME,
        "write_time": EXAMPLE_TIME,
        "health_event_id": "health_1",
    }

    QuestDBLedgerWriter(QuestDBWriteConfig()).write_raw_row("system_health_events", row)

    assert row["write_time"] == EXAMPLE_TIME
    assert "2026-05-22T15:30:00.000000Z" in calls[0]["params"]["query"]  # type: ignore[index]


def test_context_state_snapshot_contract_rejects_bad_values() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ContextStateSnapshot(
            snapshot_time=datetime(2026, 5, 22, 15, 30, 0),
            ticker="XOM",
        )
    with pytest.raises(ValueError, match="ticker"):
        ContextStateSnapshot(snapshot_time=EXAMPLE_TIME, ticker="")


def _classification_records() -> tuple[
    ContextClassificationRequest,
    ContextClassificationResponse,
    ContextValidationResult,
]:
    request = ContextClassificationRequest(
        requested_at=EXAMPLE_TIME,
        source="sec_edgar",
        source_type="sec_filing",
        source_platform="sec_edgar",
        source_uri="https://www.sec.gov/Archives/example",
        source_locator="accession:0000000000-26-000001",
        raw_input_id="raw_input_1",
        source_document_id="source_document_1",
        raw_input_hash="a" * 64,
        document_hash="b" * 64,
        affected_tickers=["XOM", "CVX"],
        input_text="Bounded filing excerpt for in-memory classification only.",
        prompt_version="context_prompt_v1",
        collected_at=EXAMPLE_TIME,
        normalized_at=EXAMPLE_TIME,
        source_published_at=EXAMPLE_TIME,
        trace_id="trace_phase7",
    )
    response = ContextClassificationResponse(
        classification_request_id=request.classification_request_id,
        classified_at=EXAMPLE_TIME,
        provider="gemini",
        model_version="gemini-model-v1",
        prompt_version=request.prompt_version,
        status=ContextClassificationStatus.VALID,
        provider_latency_ms=125.5,
        provider_request_count=1,
        event_type=ContextClassificationEventType.SEC_8K_RESULTS,
        risk_level=ContextRiskLevel.MEDIUM,
        urgency=ContextUrgency.HIGH,
        confidence=0.75,
        summary="Issuer reported material financial results.",
        trace_id=request.trace_id,
    )
    validation = ContextValidationResult(
        classification_request_id=request.classification_request_id,
        classification_attempt_id=response.classification_attempt_id,
        validation_outcome=True,
        reason_codes=[],
        validator_version="context_validator_v1",
        validated_at=EXAMPLE_TIME,
        trace_id=request.trace_id,
    )
    return request, response, validation


def _feature_snapshot() -> FeatureSnapshot:
    return FeatureSnapshot(
        snapshot_time=EXAMPLE_TIME,
        ticker="XOM",
        feature_version="feature_v1",
        features={"summary": "Fed's", "midprice": 100.25},
        source_record_count=3,
        lookback_window_seconds=60,
    )


def _model_signal() -> ModelSignal:
    feature = _feature_snapshot()
    return ModelSignal(
        signal_time=EXAMPLE_TIME,
        ticker="XOM",
        signal=SignalSide.BUY,
        confidence=0.62,
        raw_score=0.24,
        model_version="model_v1",
        calibration_version="calibration_v1",
        feature_version=feature.feature_version,
        feature_snapshot_id=feature.feature_snapshot_id,
    )


def _context_state() -> ContextStateSnapshot:
    return ContextStateSnapshot(
        snapshot_time=EXAMPLE_TIME,
        ticker="XOM",
        sector="oil",
        active_indicator_ids=["indicator_1"],
        active_context_event_ids=["event_1"],
        active_context_flag_ids=["flag_1"],
        context_summary={"summary": "Fed's rate hike"},
        highest_severity="normal",
        risk_level="normal",
        valid_until=EXAMPLE_TIME + timedelta(minutes=30),
    )


def _clear_write_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "QUESTDB_HTTP_SCHEME",
        "QUESTDB_HTTP_HOST",
        "QUESTDB_HTTP_PORT",
        "QUESTDB_HEALTH_TIMEOUT_SECONDS",
        "QUESTDB_WRITE_REQUIRED",
        "QUESTDB_MAX_SQL_LENGTH_CHARS",
    ):
        monkeypatch.delenv(name, raising=False)
