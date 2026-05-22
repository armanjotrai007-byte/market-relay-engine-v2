"""Validate QuestDB ledger writer SQL generation and optional real writes."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.contracts.context import ContextStateSnapshot  # noqa: E402
from market_relay_engine.contracts.features import FeatureSnapshot  # noqa: E402
from market_relay_engine.contracts.model import ModelSignal, SignalSide  # noqa: E402
from market_relay_engine.contracts.system import SystemHealthEvent  # noqa: E402
from market_relay_engine.questdb import writer as writer_module  # noqa: E402
from market_relay_engine.questdb.health import (  # noqa: E402
    QuestDBHealthError,
    check_questdb_http,
    format_questdb_health_result,
    load_questdb_health_config,
)
from market_relay_engine.questdb.writer import (  # noqa: E402
    ALLOWED_LEDGER_TABLES,
    QuestDBLedgerWriter,
    QuestDBWriteConfig,
    QuestDBWriteError,
    build_insert_sql,
    context_state_snapshot_to_row,
    feature_snapshot_to_row,
    load_questdb_write_config,
    model_signal_to_row,
    sanitize_sql_string,
    system_health_event_to_row,
)


EXAMPLE_TIME = datetime(2026, 5, 22, 15, 30, 0, tzinfo=UTC)
FORBIDDEN_RAW_TABLES = {
    "raw_trades",
    "raw_bbo",
    "raw_tbbo",
    "raw_ohlcv",
    "raw_mbp10",
    "databento_definitions",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check QuestDB V2 ledger writer SQL generation."
    )
    parser.add_argument(
        "--required",
        action="store_true",
        help="Require QuestDB and write tiny validation rows.",
    )
    parser.add_argument("--host", help="QuestDB HTTP host override.")
    parser.add_argument("--port", help="QuestDB HTTP port override.")
    parser.add_argument("--scheme", help="QuestDB HTTP scheme override.")
    parser.add_argument("--timeout", help="QuestDB timeout seconds override.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        run_offline_checks()
        print("[PASS] QuestDB writer offline validation passed.")
        if not args.required:
            return 0
        run_required_checks(args)
        print("[PASS] QuestDB writer required validation passed.")
        return 0
    except (QuestDBHealthError, QuestDBWriteError, AssertionError) as exc:
        print(f"[FAIL] QuestDB writer check failed: {exc}")
        return 1


def run_offline_checks() -> None:
    rows = _build_example_rows()
    for table_name, row in rows.items():
        sql = build_insert_sql(table_name, row)
        assert sql.startswith(f"INSERT INTO {table_name} ")
        for column in row:
            assert column in sql

    assert "Fed''s rate hike" in build_insert_sql(
        "context_state_snapshots",
        rows["context_state_snapshots"],
    )
    assert sanitize_sql_string("bad\x00\n\ttext's") == "bad  text''s"
    _assert_no_raw_market_tables()
    _assert_long_sql_is_rejected_before_request()


def run_required_checks(args: argparse.Namespace) -> None:
    health_config = load_questdb_health_config(
        http_scheme=args.scheme,
        http_host=args.host,
        http_port=args.port,
        timeout_seconds=args.timeout,
        required=True,
    )
    health_result = check_questdb_http(health_config)
    print(format_questdb_health_result(health_result))

    write_config = load_questdb_write_config(
        http_scheme=args.scheme,
        http_host=args.host,
        http_port=args.port,
        timeout_seconds=args.timeout,
        required=True,
    )
    questdb_writer = QuestDBLedgerWriter(write_config)
    health_event, model_signal = _build_required_records()
    questdb_writer.write_row("system_health_events", system_health_event_to_row(health_event))
    questdb_writer.write_row("model_signals", model_signal_to_row(model_signal))


def _build_example_rows() -> dict[str, dict[str, Any]]:
    feature_snapshot = FeatureSnapshot(
        snapshot_time=EXAMPLE_TIME,
        ticker="XOM",
        feature_version="feature_v1",
        features={"summary": "Fed's rate hike", "midprice": 100.25},
        source_record_count=3,
        lookback_window_seconds=60,
        trace_id="trace_writer_check",
    )
    model_signal = ModelSignal(
        signal_time=EXAMPLE_TIME,
        ticker="XOM",
        signal=SignalSide.BUY,
        confidence=0.61,
        raw_score=0.22,
        model_version="model_writer_check",
        calibration_version="calibration_writer_check",
        feature_version=feature_snapshot.feature_version,
        feature_snapshot_id=feature_snapshot.feature_snapshot_id,
        trace_id=feature_snapshot.trace_id,
    )
    context_state = ContextStateSnapshot(
        snapshot_time=EXAMPLE_TIME,
        ticker="XOM",
        sector="oil",
        active_indicator_ids=["indicator_1"],
        active_context_event_ids=["event_1"],
        active_context_flag_ids=["flag_1"],
        context_summary={"summary": "Fed's rate hike"},
        highest_severity="normal",
        risk_level="normal",
        trace_id=feature_snapshot.trace_id,
    )
    health_event = SystemHealthEvent(
        event_time=EXAMPLE_TIME,
        component="questdb_writer_check",
        status="ok",
        message="Offline writer check.",
        trace_id=feature_snapshot.trace_id,
    )
    return {
        "feature_snapshots": feature_snapshot_to_row(feature_snapshot),
        "model_signals": model_signal_to_row(model_signal),
        "context_state_snapshots": context_state_snapshot_to_row(context_state),
        "system_health_events": system_health_event_to_row(health_event),
    }


def _build_required_records() -> tuple[SystemHealthEvent, ModelSignal]:
    feature_snapshot = FeatureSnapshot(
        snapshot_time=EXAMPLE_TIME,
        ticker="XOM",
        feature_version="feature_writer_required",
        features={"midprice": 100.25},
        source_record_count=1,
        lookback_window_seconds=60,
        trace_id="trace_writer_required",
    )
    return (
        SystemHealthEvent(
            event_time=EXAMPLE_TIME,
            component="questdb_writer_check",
            status="ok",
            message="Required QuestDB writer check.",
            trace_id=feature_snapshot.trace_id,
        ),
        ModelSignal(
            signal_time=EXAMPLE_TIME,
            ticker="XOM",
            signal=SignalSide.BUY,
            confidence=0.51,
            raw_score=0.12,
            model_version="model_writer_required",
            calibration_version="calibration_writer_required",
            feature_version=feature_snapshot.feature_version,
            feature_snapshot_id=feature_snapshot.feature_snapshot_id,
            trace_id=feature_snapshot.trace_id,
        ),
    )


def _assert_no_raw_market_tables() -> None:
    raw_tables = FORBIDDEN_RAW_TABLES.intersection(ALLOWED_LEDGER_TABLES)
    assert not raw_tables, f"raw market-data tables are not allowed: {sorted(raw_tables)}"


def _assert_long_sql_is_rejected_before_request() -> None:
    calls: list[str] = []

    def fake_get(*args: object, **kwargs: object) -> object:
        calls.append("called")
        raise AssertionError("requests.get must not be called for oversized SQL")

    original_get = writer_module.requests.get
    writer_module.requests.get = fake_get  # type: ignore[assignment]
    try:
        questdb_writer = QuestDBLedgerWriter(
            QuestDBWriteConfig(max_sql_length_chars=50)
        )
        try:
            questdb_writer.write_raw_row(
                "system_health_events",
                {
                    "event_time": EXAMPLE_TIME,
                    "write_time": EXAMPLE_TIME,
                    "health_event_id": "health_long",
                    "component": "writer_check",
                    "status": "ok",
                    "message": "x" * 200,
                },
            )
        except QuestDBWriteError as exc:
            assert "too long for safe /exec GET" in str(exc)
        else:
            raise AssertionError("oversized SQL did not raise QuestDBWriteError")
    finally:
        writer_module.requests.get = original_get  # type: ignore[assignment]
    assert calls == []


if __name__ == "__main__":
    raise SystemExit(main())
