from __future__ import annotations

from datetime import UTC, datetime, timedelta
import math
from pathlib import Path

import pytest
import requests

from market_relay_engine.contracts.context import (
    ContextAIEvent,
    ContextFlag,
    ContextIndicatorSnapshot,
    ContextStateSnapshot,
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
    QuestDBLedgerWriter,
    QuestDBWriteConfig,
    QuestDBWriteError,
    build_insert_sql,
    context_ai_event_to_row,
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
        event_type="headline",
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
    assert context_ai_event_to_row(ai_event)["context_event_id"] == ai_event.context_event_id
    assert context_flag_to_row(flag)["context_flag_id"] == flag.context_flag_id
    assert context_state_snapshot_to_row(state)["context_snapshot_id"] == state.context_snapshot_id


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
