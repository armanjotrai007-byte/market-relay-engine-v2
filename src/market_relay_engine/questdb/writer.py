'''Small QuestDB /exec ledger writer for V2 bot-ledger rows.'''

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import math
import os
from pathlib import Path
from time import perf_counter
from typing import Any

from dotenv import load_dotenv
import requests
import yaml

from market_relay_engine.common.ids import new_record_id
from market_relay_engine.common.serialization import to_json_string
from market_relay_engine.common.time import ensure_timezone_aware_utc, utc_now
from market_relay_engine.contracts.base import DEFAULT_SCHEMA_VERSION


DEFAULT_HTTP_SCHEME = 'http'
DEFAULT_HTTP_HOST = 'localhost'
DEFAULT_HTTP_PORT = 9000
DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_REQUIRED = False
DEFAULT_MAX_SQL_LENGTH_CHARS = 7000

ENV_HTTP_SCHEME = 'QUESTDB_HTTP_SCHEME'
ENV_HTTP_HOST = 'QUESTDB_HTTP_HOST'
ENV_HTTP_PORT = 'QUESTDB_HTTP_PORT'
ENV_TIMEOUT_SECONDS = 'QUESTDB_HEALTH_TIMEOUT_SECONDS'
ENV_REQUIRED = 'QUESTDB_WRITE_REQUIRED'
ENV_MAX_SQL_LENGTH_CHARS = 'QUESTDB_MAX_SQL_LENGTH_CHARS'

TABLE_COLUMN_TEXT = '''
bot_runs: started_at ended_at run_id session_id environment mode paper_trading git_commit config_hash status message schema_version trace_id
bot_sessions: session_start_time session_end_time session_id run_id machine_name service_name environment mode ntp_status clock_offset_ms status message schema_version trace_id
feature_snapshots: snapshot_time write_time feature_snapshot_id ticker feature_version source_record_count lookback_window_seconds features_json run_id session_id schema_version trace_id
model_signals: signal_time write_time signal_id ticker signal confidence raw_score model_version calibration_version feature_version feature_snapshot_id run_id session_id schema_version trace_id
cost_estimates: estimate_time write_time cost_estimate_id ticker signal_id feature_snapshot_id side horizon order_style quantity midprice spread_bps expected_gross_move_bps spread_cost_bps estimated_slippage_bps size_penalty_bps base_cost_bps missed_fill_probability pre_missed_fill_net_edge_bps missed_fill_penalty_bps total_cost_bps min_edge_bps net_expected_edge_bps exceeds_min_edge_threshold profitable_after_costs assumptions_version reason run_id session_id schema_version trace_id
context_state_snapshots: snapshot_time write_time context_snapshot_id ticker sector active_indicator_ids_json active_context_event_ids_json active_context_flag_ids_json context_summary_json highest_severity risk_level valid_until run_id session_id schema_version trace_id
risk_decisions: decision_time write_time risk_decision_id ticker model_signal_id cost_estimate_id context_snapshot_id decision approved risk_version reduce_size_factor reasons_json thresholds_used_json run_id session_id schema_version trace_id
context_indicator_snapshots: snapshot_time write_time context_indicator_id source ticker_or_sector indicator_name value_json window units freshness_seconds source_event_time run_id session_id schema_version trace_id
context_ai_events: event_time write_time context_event_id source source_id affected_tickers_json affected_sector event_type sentiment urgency risk_level confidence valid_from valid_until summary prompt_version model_version raw_input_hash run_id session_id schema_version trace_id
context_flags: event_time write_time context_flag_id source flag_type severity ticker sector confidence valid_until run_id session_id schema_version trace_id
order_events: order_time write_time order_id ticker side order_type quantity status expected_price submitted_price broker broker_order_id paper_trading model_signal_id risk_decision_id feature_snapshot_id run_id session_id schema_version trace_id
fill_events: fill_time write_time fill_id order_id ticker side quantity fill_price expected_price slippage slippage_bps broker_status broker_fill_id model_signal_id risk_decision_id run_id session_id schema_version trace_id
trade_outcomes: entry_time write_time outcome_id signal_id order_id fill_id ticker exit_time entry_price exit_price quantity realized_pnl return_1m return_5m return_15m max_favorable_excursion max_adverse_excursion result run_id session_id schema_version trace_id
latency_metrics: measured_time write_time latency_metric_id component source latency_ms ticker event_type run_id session_id schema_version trace_id
system_health_events: event_time write_time health_event_id component status message cpu_percent memory_percent clock_offset_ms feed_delay_ms reconnect_count queue_depth ledger_write_errors jsonl_fallback_count run_id session_id schema_version trace_id
ledger_write_errors: event_time write_time error_id target_table component severity record_type record_id error_message payload_json jsonl_fallback_path fallback_written run_id session_id schema_version trace_id
jsonl_fallback_events: event_time write_time fallback_event_id component target_table record_type record_id file_path bytes_written status message run_id session_id schema_version trace_id
'''

TABLE_COLUMNS = {
    table: tuple(columns.strip().split())
    for table, columns in (
        line.strip().split(':', maxsplit=1)
        for line in TABLE_COLUMN_TEXT.strip().splitlines()
    )
}
ALLOWED_LEDGER_TABLES = tuple(TABLE_COLUMNS)
_FIELD_NOT_PROVIDED = object()


class QuestDBWriteError(RuntimeError):
    def __init__(self, message: str, *, table_name: str | None = None) -> None:
        if table_name:
            message = f'{table_name}: {message}'
        super().__init__(message)
        self.table_name = table_name


@dataclass(frozen=True, kw_only=True)
class QuestDBWriteConfig:
    http_scheme: str = DEFAULT_HTTP_SCHEME
    http_host: str = DEFAULT_HTTP_HOST
    http_port: int = DEFAULT_HTTP_PORT
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    required: bool = DEFAULT_REQUIRED
    max_sql_length_chars: int = DEFAULT_MAX_SQL_LENGTH_CHARS

    def __post_init__(self) -> None:
        scheme = _non_empty_string(self.http_scheme, 'http_scheme').lower()
        if scheme not in {'http', 'https'}:
            raise QuestDBWriteError('http_scheme must be http or https')
        object.__setattr__(self, 'http_scheme', scheme)
        object.__setattr__(self, 'http_host', _non_empty_string(self.http_host, 'http_host'))
        object.__setattr__(self, 'http_port', _port(self.http_port, 'http_port'))
        object.__setattr__(self, 'timeout_seconds', _positive_finite_float(self.timeout_seconds, 'timeout_seconds'))
        if not isinstance(self.required, bool):
            raise QuestDBWriteError('required must be bool')
        object.__setattr__(self, 'max_sql_length_chars', _positive_int(self.max_sql_length_chars, 'max_sql_length_chars'))


@dataclass(frozen=True, kw_only=True)
class QuestDBWriteResult:
    success: bool
    table_name: str
    row_count: int
    status_code: int | None
    message: str
    latency_ms: float | None


def load_questdb_write_config(
    config_path: str | Path | None = None,
    *,
    http_scheme: str | None = None,
    http_host: str | None = None,
    http_port: int | str | None = None,
    timeout_seconds: float | str | None = None,
    required: bool | None = None,
    max_sql_length_chars: int | str | None = None,
    load_dotenv_file: bool = True,
) -> QuestDBWriteConfig:
    if load_dotenv_file:
        load_dotenv(_repo_root() / '.env', override=False)
    values: dict[str, Any] = {
        'http_scheme': DEFAULT_HTTP_SCHEME,
        'http_host': DEFAULT_HTTP_HOST,
        'http_port': DEFAULT_HTTP_PORT,
        'timeout_seconds': DEFAULT_TIMEOUT_SECONDS,
        'required': DEFAULT_REQUIRED,
        'max_sql_length_chars': DEFAULT_MAX_SQL_LENGTH_CHARS,
    }
    values.update({key: value for key, value in _load_yaml_write_values(_resolve_config_path(config_path)).items() if value is not None})
    values.update({
        key: value
        for key, value in {
            'http_scheme': os.getenv(ENV_HTTP_SCHEME),
            'http_host': os.getenv(ENV_HTTP_HOST),
            'http_port': os.getenv(ENV_HTTP_PORT),
            'timeout_seconds': os.getenv(ENV_TIMEOUT_SECONDS),
            'required': _optional_bool_from_env(os.getenv(ENV_REQUIRED), ENV_REQUIRED),
            'max_sql_length_chars': os.getenv(ENV_MAX_SQL_LENGTH_CHARS),
        }.items()
        if value is not None
    })
    values.update({
        key: value
        for key, value in {
            'http_scheme': http_scheme,
            'http_host': http_host,
            'http_port': http_port,
            'timeout_seconds': timeout_seconds,
            'required': required,
            'max_sql_length_chars': max_sql_length_chars,
        }.items()
        if value is not None
    })
    return QuestDBWriteConfig(**values)


def build_questdb_write_exec_url(config: QuestDBWriteConfig) -> str:
    return f'{config.http_scheme}://{config.http_host}:{config.http_port}/exec'


def encoded_exec_url_length(exec_url: str, sql: str) -> int:
    request = requests.Request('GET', exec_url, params={'query': sql, 'fmt': 'json'})
    prepared = request.prepare()
    return len(prepared.url or '')


def sanitize_sql_string(value: str) -> str:
    if not isinstance(value, str):
        raise QuestDBWriteError('SQL string value must be str')
    sanitized = []
    for character in value:
        if character == '\x00':
            continue
        sanitized.append(' ' if ord(character) < 32 else character)
    return ''.join(sanitized).replace("'", "''")


def timestamp_sql_literal(value: datetime) -> str:
    try:
        timestamp = ensure_timezone_aware_utc(value)
    except (TypeError, ValueError) as exc:
        raise QuestDBWriteError('timestamp values must be timezone-aware datetimes') from exc
    text = timestamp.isoformat(timespec='microseconds').replace('+00:00', 'Z')
    return f"'{text}'"


def sql_literal(value: Any) -> str:
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, Enum):
        return sql_literal(value.value)
    if isinstance(value, datetime):
        return timestamp_sql_literal(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise QuestDBWriteError('numeric SQL values must be finite')
        return str(value)
    if isinstance(value, str):
        return f"'{sanitize_sql_string(value)}'"
    if isinstance(value, (dict, list, tuple)):
        return sql_literal(to_json_string(value))
    raise QuestDBWriteError(f'unsupported SQL literal type: {type(value).__name__}')


def build_insert_sql(table_name: str, row: Mapping[str, Any]) -> str:
    if table_name not in TABLE_COLUMNS:
        raise QuestDBWriteError('unknown QuestDB ledger table', table_name=table_name)
    if not isinstance(row, Mapping) or not row:
        raise QuestDBWriteError('row must be a non-empty mapping', table_name=table_name)
    allowed_columns = set(TABLE_COLUMNS[table_name])
    columns = list(row.keys())
    unknown_columns = [column for column in columns if column not in allowed_columns]
    if unknown_columns:
        raise QuestDBWriteError(f'unknown columns for table: {unknown_columns}', table_name=table_name)
    return f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({', '.join(sql_literal(row[column]) for column in columns)})"


class QuestDBLedgerWriter:
    def __init__(self, config: QuestDBWriteConfig | None = None) -> None:
        self.config = config or load_questdb_write_config()
        self.exec_url = build_questdb_write_exec_url(self.config)

    def write_row(self, table_name: str, row: Mapping[str, Any]) -> QuestDBWriteResult:
        sql = build_insert_sql(table_name, row)
        encoded_length = encoded_exec_url_length(self.exec_url, sql)
        if encoded_length > self.config.max_sql_length_chars:
            raise QuestDBWriteError(
                'encoded /exec GET request is too long for safe /exec GET; PR14 fallback or a future bulk ingestion path is required',
                table_name=table_name,
            )
        started = perf_counter()
        try:
            response = requests.get(self.exec_url, params={'query': sql, 'fmt': 'json'}, timeout=self.config.timeout_seconds)
            latency_ms = max((perf_counter() - started) * 1000.0, 0.0)
        except requests.RequestException as exc:
            raise QuestDBWriteError(f'QuestDB /exec request failed: {exc}', table_name=table_name) from exc
        if response.status_code != 200:
            raise QuestDBWriteError(f'QuestDB /exec returned HTTP {response.status_code}', table_name=table_name)
        try:
            payload = response.json()
        except ValueError as exc:
            raise QuestDBWriteError(f'QuestDB /exec returned invalid JSON: {exc}', table_name=table_name) from exc
        if not isinstance(payload, dict):
            raise QuestDBWriteError('QuestDB /exec returned non-object JSON', table_name=table_name)
        if 'error' in payload:
            raise QuestDBWriteError(f"QuestDB /exec returned error: {payload.get('error')}", table_name=table_name)
        return QuestDBWriteResult(success=True, table_name=table_name, row_count=1, status_code=response.status_code, message='QuestDB /exec INSERT accepted', latency_ms=latency_ms)

    def write_raw_row(self, table_name: str, row: Mapping[str, Any]) -> QuestDBWriteResult:
        return self.write_row(table_name, row)

    def write_feature_snapshot(self, snapshot: Any, **kwargs: Any) -> QuestDBWriteResult:
        return self.write_row('feature_snapshots', feature_snapshot_to_row(snapshot, **kwargs))

    def write_model_signal(self, signal: Any, **kwargs: Any) -> QuestDBWriteResult:
        return self.write_row('model_signals', model_signal_to_row(signal, **kwargs))

    def write_cost_estimate(self, estimate: Any, **kwargs: Any) -> QuestDBWriteResult:
        return self.write_row('cost_estimates', cost_estimate_to_row(estimate, **kwargs))

    def write_context_indicator_snapshot(self, snapshot: Any, **kwargs: Any) -> QuestDBWriteResult:
        return self.write_row('context_indicator_snapshots', context_indicator_snapshot_to_row(snapshot, **kwargs))

    def write_risk_decision(self, decision: Any, **kwargs: Any) -> QuestDBWriteResult:
        return self.write_row('risk_decisions', risk_decision_to_row(decision, **kwargs))

    def write_order_event(self, order: Any, **kwargs: Any) -> QuestDBWriteResult:
        return self.write_row('order_events', order_event_to_row(order, **kwargs))

    def write_fill_event(self, fill: Any, **kwargs: Any) -> QuestDBWriteResult:
        return self.write_row('fill_events', fill_event_to_row(fill, **kwargs))

    def write_latency_metric(self, metric: Any, **kwargs: Any) -> QuestDBWriteResult:
        return self.write_row('latency_metrics', latency_metric_to_row(metric, **kwargs))

    def write_system_health_event(self, event: Any, **kwargs: Any) -> QuestDBWriteResult:
        return self.write_row('system_health_events', system_health_event_to_row(event, **kwargs))


def feature_snapshot_to_row(snapshot: Any, *, run_id: str | None = None, session_id: str | None = None, write_time: datetime | None = None) -> dict[str, Any]:
    return {
        'snapshot_time': snapshot.snapshot_time,
        'write_time': _resolve_write_time(write_time),
        'feature_snapshot_id': snapshot.feature_snapshot_id,
        'ticker': snapshot.ticker,
        'feature_version': snapshot.feature_version,
        'source_record_count': snapshot.source_record_count,
        'lookback_window_seconds': snapshot.lookback_window_seconds,
        'features_json': to_json_string(snapshot.features),
        'run_id': run_id,
        'session_id': session_id,
        'schema_version': snapshot.schema_version,
        'trace_id': snapshot.trace_id,
    }


def model_signal_to_row(signal: Any, *, run_id: str | None = None, session_id: str | None = None, write_time: datetime | None = None) -> dict[str, Any]:
    return {
        'signal_time': signal.signal_time,
        'write_time': _resolve_write_time(write_time),
        'signal_id': signal.signal_id,
        'ticker': signal.ticker,
        'signal': signal.signal,
        'confidence': signal.confidence,
        'raw_score': signal.raw_score,
        'model_version': signal.model_version,
        'calibration_version': signal.calibration_version,
        'feature_version': signal.feature_version,
        'feature_snapshot_id': signal.feature_snapshot_id,
        'run_id': run_id,
        'session_id': session_id,
        'schema_version': signal.schema_version,
        'trace_id': signal.trace_id,
    }


def cost_estimate_to_row(estimate: Any, *, run_id: str | None = None, session_id: str | None = None, signal_id: str | None = None, feature_snapshot_id: str | None = None, estimate_time: datetime | None = None, cost_estimate_id: str | None = None, write_time: datetime | None = None) -> dict[str, Any]:
    resolved_write_time = _resolve_write_time(write_time)
    return {
        'estimate_time': estimate_time or getattr(estimate, 'estimate_time', None) or resolved_write_time,
        'write_time': resolved_write_time,
        'cost_estimate_id': cost_estimate_id or getattr(estimate, 'cost_estimate_id', None) or new_record_id('cost_estimate'),
        'ticker': estimate.ticker,
        'signal_id': signal_id,
        'feature_snapshot_id': feature_snapshot_id,
        'side': estimate.side,
        'horizon': estimate.horizon,
        'order_style': estimate.order_style,
        'quantity': estimate.quantity,
        'midprice': estimate.midprice,
        'spread_bps': estimate.spread_bps,
        'expected_gross_move_bps': estimate.expected_gross_move_bps,
        'spread_cost_bps': estimate.spread_cost_bps,
        'estimated_slippage_bps': estimate.estimated_slippage_bps,
        'size_penalty_bps': estimate.size_penalty_bps,
        'base_cost_bps': estimate.base_cost_bps,
        'missed_fill_probability': estimate.missed_fill_probability,
        'pre_missed_fill_net_edge_bps': estimate.pre_missed_fill_net_edge_bps,
        'missed_fill_penalty_bps': estimate.missed_fill_penalty_bps,
        'total_cost_bps': estimate.total_cost_bps,
        'min_edge_bps': estimate.min_edge_bps,
        'net_expected_edge_bps': estimate.net_expected_edge_bps,
        'exceeds_min_edge_threshold': estimate.exceeds_min_edge_threshold,
        'profitable_after_costs': estimate.profitable_after_costs,
        'assumptions_version': estimate.assumptions_version,
        'reason': estimate.reason,
        'run_id': run_id,
        'session_id': session_id,
        'schema_version': getattr(estimate, 'schema_version', DEFAULT_SCHEMA_VERSION),
        'trace_id': estimate.trace_id,
    }


def risk_decision_to_row(decision: Any, *, run_id: str | None = None, session_id: str | None = None, cost_estimate_id: str | None = None, write_time: datetime | None = None) -> dict[str, Any]:
    return {
        'decision_time': decision.decision_time,
        'write_time': _resolve_write_time(write_time),
        'risk_decision_id': decision.risk_decision_id,
        'ticker': decision.ticker,
        'model_signal_id': decision.model_signal_id,
        'cost_estimate_id': cost_estimate_id if cost_estimate_id is not None else getattr(decision, 'cost_estimate_id', None),
        'context_snapshot_id': decision.context_snapshot_id,
        'decision': decision.decision,
        'approved': decision.approved,
        'risk_version': decision.risk_version,
        'reduce_size_factor': decision.reduce_size_factor,
        'reasons_json': to_json_string(decision.reasons),
        'thresholds_used_json': to_json_string(decision.thresholds_used),
        'run_id': run_id,
        'session_id': session_id,
        'schema_version': decision.schema_version,
        'trace_id': decision.trace_id,
    }


def context_indicator_snapshot_to_row(record: Any, *, run_id: str | None = None, session_id: str | None = None, write_time: datetime | None = None) -> dict[str, Any]:
    return {
        'snapshot_time': record.snapshot_time,
        'write_time': _resolve_write_time(write_time),
        'context_indicator_id': record.context_indicator_id,
        'source': record.source,
        'ticker_or_sector': record.ticker_or_sector,
        'indicator_name': record.indicator_name,
        'value_json': to_json_string(record.value),
        'window': record.window,
        'units': record.units,
        'freshness_seconds': record.freshness_seconds,
        'source_event_time': record.source_event_time,
        'run_id': run_id,
        'session_id': session_id,
        'schema_version': record.schema_version,
        'trace_id': record.trace_id,
    }


def context_ai_event_to_row(record: Any, *, run_id: str | None = None, session_id: str | None = None, write_time: datetime | None = None) -> dict[str, Any]:
    return {
        'event_time': record.event_time,
        'write_time': _resolve_write_time(write_time),
        'context_event_id': record.context_event_id,
        'source': record.source,
        'source_id': record.source_id,
        'affected_tickers_json': to_json_string(record.affected_tickers),
        'affected_sector': record.affected_sector,
        'event_type': record.event_type,
        'sentiment': record.sentiment,
        'urgency': record.urgency,
        'risk_level': record.risk_level,
        'confidence': record.confidence,
        'valid_from': record.valid_from,
        'valid_until': record.valid_until,
        'summary': record.summary,
        'prompt_version': record.prompt_version,
        'model_version': record.model_version,
        'raw_input_hash': record.raw_input_hash,
        'run_id': run_id,
        'session_id': session_id,
        'schema_version': record.schema_version,
        'trace_id': record.trace_id,
    }


def context_flag_to_row(record: Any, *, run_id: str | None = None, session_id: str | None = None, write_time: datetime | None = None) -> dict[str, Any]:
    return {
        'event_time': record.event_time,
        'write_time': _resolve_write_time(write_time),
        'context_flag_id': record.context_flag_id,
        'source': record.source,
        'flag_type': record.flag_type,
        'severity': record.severity,
        'ticker': record.ticker,
        'sector': record.sector,
        'confidence': record.confidence,
        'valid_until': record.valid_until,
        'run_id': run_id,
        'session_id': session_id,
        'schema_version': record.schema_version,
        'trace_id': record.trace_id,
    }


def context_state_snapshot_to_row(record: Any, *, run_id: str | None = None, session_id: str | None = None, write_time: datetime | None = None) -> dict[str, Any]:
    return {
        'snapshot_time': record.snapshot_time,
        'write_time': _resolve_write_time(write_time),
        'context_snapshot_id': record.context_snapshot_id,
        'ticker': record.ticker,
        'sector': record.sector,
        'active_indicator_ids_json': to_json_string(record.active_indicator_ids),
        'active_context_event_ids_json': to_json_string(record.active_context_event_ids),
        'active_context_flag_ids_json': to_json_string(record.active_context_flag_ids),
        'context_summary_json': to_json_string(record.context_summary),
        'highest_severity': record.highest_severity,
        'risk_level': record.risk_level,
        'valid_until': record.valid_until,
        'run_id': run_id,
        'session_id': session_id,
        'schema_version': record.schema_version,
        'trace_id': record.trace_id,
    }


def order_event_to_row(record: Any, *, run_id: str | None = None, session_id: str | None = None, broker_order_id: str | None = None, model_signal_id: str | None = None, risk_decision_id: str | None = None, feature_snapshot_id: str | None = None, write_time: datetime | None = None) -> dict[str, Any]:
    return {
        'order_time': record.order_time,
        'write_time': _resolve_write_time(write_time),
        'order_id': record.order_id,
        'ticker': record.ticker,
        'side': record.side,
        'order_type': record.order_type,
        'quantity': record.quantity,
        'status': record.status,
        'expected_price': record.expected_price,
        'submitted_price': record.submitted_price,
        'broker': record.broker,
        'broker_order_id': broker_order_id,
        'paper_trading': record.paper_trading,
        'model_signal_id': model_signal_id,
        'risk_decision_id': risk_decision_id,
        'feature_snapshot_id': feature_snapshot_id,
        'run_id': run_id,
        'session_id': session_id,
        'schema_version': record.schema_version,
        'trace_id': record.trace_id,
    }


def fill_event_to_row(record: Any, *, run_id: str | None = None, session_id: str | None = None, broker_fill_id: Any = _FIELD_NOT_PROVIDED, model_signal_id: Any = _FIELD_NOT_PROVIDED, risk_decision_id: Any = _FIELD_NOT_PROVIDED, slippage_bps: Any = _FIELD_NOT_PROVIDED, write_time: datetime | None = None) -> dict[str, Any]:
    return {
        'fill_time': record.fill_time,
        'write_time': _resolve_write_time(write_time),
        'fill_id': record.fill_id,
        'order_id': record.order_id,
        'ticker': record.ticker,
        'side': record.side,
        'quantity': record.quantity,
        'fill_price': record.fill_price,
        'expected_price': record.expected_price,
        'slippage': record.slippage,
        'slippage_bps': getattr(record, 'slippage_bps', None) if slippage_bps is _FIELD_NOT_PROVIDED else slippage_bps,
        'broker_status': record.broker_status,
        'broker_fill_id': getattr(record, 'broker_fill_id', None) if broker_fill_id is _FIELD_NOT_PROVIDED else broker_fill_id,
        'model_signal_id': getattr(record, 'model_signal_id', None) if model_signal_id is _FIELD_NOT_PROVIDED else model_signal_id,
        'risk_decision_id': getattr(record, 'risk_decision_id', None) if risk_decision_id is _FIELD_NOT_PROVIDED else risk_decision_id,
        'run_id': run_id,
        'session_id': session_id,
        'schema_version': record.schema_version,
        'trace_id': record.trace_id,
    }


def trade_outcome_to_row(record: Any, *, run_id: str | None = None, session_id: str | None = None, fill_id: str | None = None, entry_price: float | None = None, exit_price: float | None = None, quantity: float | None = None, write_time: datetime | None = None) -> dict[str, Any]:
    return {
        'entry_time': record.entry_time,
        'write_time': _resolve_write_time(write_time),
        'outcome_id': record.outcome_id,
        'signal_id': record.signal_id,
        'order_id': record.order_id,
        'fill_id': fill_id,
        'ticker': record.ticker,
        'exit_time': record.exit_time,
        'entry_price': entry_price,
        'exit_price': exit_price,
        'quantity': quantity,
        'realized_pnl': record.realized_pnl,
        'return_1m': record.return_1m,
        'return_5m': record.return_5m,
        'return_15m': record.return_15m,
        'max_favorable_excursion': record.max_favorable_excursion,
        'max_adverse_excursion': record.max_adverse_excursion,
        'result': record.result,
        'run_id': run_id,
        'session_id': session_id,
        'schema_version': record.schema_version,
        'trace_id': record.trace_id,
    }


def latency_metric_to_row(record: Any, *, run_id: str | None = None, session_id: str | None = None, ticker: str | None = None, event_type: str | None = None, write_time: datetime | None = None) -> dict[str, Any]:
    return {
        'measured_time': record.measured_time,
        'write_time': _resolve_write_time(write_time),
        'latency_metric_id': record.latency_metric_id,
        'component': record.component,
        'source': record.source,
        'latency_ms': record.latency_ms,
        'ticker': ticker,
        'event_type': event_type,
        'run_id': run_id,
        'session_id': session_id,
        'schema_version': record.schema_version,
        'trace_id': record.trace_id,
    }


def system_health_event_to_row(record: Any, *, run_id: str | None = None, session_id: str | None = None, queue_depth: int | None = None, ledger_write_errors: int | None = None, jsonl_fallback_count: int | None = None, write_time: datetime | None = None) -> dict[str, Any]:
    return {
        'event_time': record.event_time,
        'write_time': _resolve_write_time(write_time),
        'health_event_id': record.health_event_id,
        'component': record.component,
        'status': record.status,
        'message': record.message,
        'cpu_percent': record.cpu_percent,
        'memory_percent': record.memory_percent,
        'clock_offset_ms': record.clock_offset_ms,
        'feed_delay_ms': record.feed_delay_ms,
        'reconnect_count': record.reconnect_count,
        'queue_depth': queue_depth,
        'ledger_write_errors': ledger_write_errors,
        'jsonl_fallback_count': jsonl_fallback_count,
        'run_id': run_id,
        'session_id': session_id,
        'schema_version': record.schema_version,
        'trace_id': record.trace_id,
    }


def _resolve_write_time(value: datetime | None) -> datetime:
    if value is None:
        return utc_now()
    try:
        return ensure_timezone_aware_utc(value)
    except (TypeError, ValueError) as exc:
        raise QuestDBWriteError('write_time must be timezone-aware UTC') from exc


def _resolve_config_path(config_path: str | Path | None) -> Path:
    return _repo_root() / 'config' / 'questdb.yaml' if config_path is None else Path(config_path)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_yaml_write_values(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    except yaml.YAMLError as exc:
        raise QuestDBWriteError(f'Invalid QuestDB YAML config: {config_path}') from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise QuestDBWriteError('QuestDB YAML config must be a mapping')
    connection = _yaml_section_mapping(loaded, 'connection')
    health_check = _yaml_section_mapping(loaded, 'health_check')
    writer = _yaml_section_mapping(loaded, 'writer')
    return {
        'http_scheme': _yaml_value(writer, 'http_scheme', connection, 'default_http_scheme'),
        'http_host': _yaml_value(writer, 'http_host', connection, 'default_http_host'),
        'http_port': _yaml_value(writer, 'http_port', connection, 'default_http_port'),
        'timeout_seconds': _yaml_value(writer, 'timeout_seconds', connection, 'default_health_timeout_seconds'),
        'required': writer.get('required_by_default', health_check.get('required_by_default')),
        'max_sql_length_chars': writer.get('max_sql_length_chars'),
    }


def _yaml_section_mapping(loaded: Mapping[str, Any], name: str) -> dict[str, Any]:
    section = loaded.get(name, {})
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise QuestDBWriteError(f'questdb.yaml {name} section must be a mapping')
    return section


def _yaml_value(primary: Mapping[str, Any], primary_key: str, fallback: Mapping[str, Any], fallback_key: str) -> Any:
    if primary_key in primary and primary[primary_key] is not None:
        return primary[primary_key]
    if fallback_key in fallback and fallback[fallback_key] is not None:
        return fallback[fallback_key]
    return None


def _optional_bool_from_env(value: str | None, env_name: str) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {'1', 'true', 'yes', 'y', 'on'}:
        return True
    if normalized in {'0', 'false', 'no', 'n', 'off'}:
        return False
    raise QuestDBWriteError(f'{env_name} must be a boolean value')


def _non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuestDBWriteError(f'{field_name} must be a non-empty string')
    return value.strip()


def _port(value: Any, field_name: str) -> int:
    port = _positive_int(value, field_name)
    if port > 65535:
        raise QuestDBWriteError(f'{field_name} must be between 1 and 65535')
    return port


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise QuestDBWriteError(f'{field_name} must be an integer, not bool')
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise QuestDBWriteError(f'{field_name} must be an integer') from exc
    if str(number) != str(value).strip() and not isinstance(value, int):
        raise QuestDBWriteError(f'{field_name} must be an integer')
    if number <= 0:
        raise QuestDBWriteError(f'{field_name} must be positive')
    return number


def _positive_finite_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise QuestDBWriteError(f'{field_name} must be numeric, not bool')
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise QuestDBWriteError(f'{field_name} must be numeric') from exc
    if not math.isfinite(number):
        raise QuestDBWriteError(f'{field_name} must be finite')
    if number <= 0:
        raise QuestDBWriteError(f'{field_name} must be positive')
    return number
