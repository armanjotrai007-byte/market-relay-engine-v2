"""Validate QuestDB ledger readback analysis and optional real summaries."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.questdb.analysis import (  # noqa: E402
    ANALYSIS_TABLES,
    QuestDBAnalysisConfig,
    QuestDBAnalysisError,
    QuestDBLedgerReader,
    QuestDBQueryResult,
    build_basic_ledger_summary,
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
from market_relay_engine.questdb.health import (  # noqa: E402
    QuestDBHealthError,
    check_questdb_http,
    format_questdb_health_result,
    load_questdb_health_config,
)


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
        description="Check QuestDB V2 ledger readback analysis."
    )
    parser.add_argument(
        "--required",
        action="store_true",
        help="Require QuestDB and run real read-only summary queries.",
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
        print("[PASS] QuestDB analysis offline validation passed.")
        if not args.required:
            return 0
        run_required_checks(args)
        print("[PASS] QuestDB analysis required validation passed.")
        return 0
    except (QuestDBAnalysisError, QuestDBHealthError, AssertionError) as exc:
        print(f"[FAIL] QuestDB analysis check failed: {exc}")
        return 1


def run_offline_checks() -> None:
    _assert_sql_safety()
    _assert_parser()
    _assert_summary_queries_are_safe()


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

    analysis_config = load_questdb_analysis_config(
        http_scheme=args.scheme,
        http_host=args.host,
        http_port=args.port,
        timeout_seconds=args.timeout,
        required=True,
    )
    summary = build_basic_ledger_summary(QuestDBLedgerReader(analysis_config))
    print(_format_required_summary(summary))


def _assert_sql_safety() -> None:
    for sql in (
        "SELECT * FROM model_signals",
        "   select * from model_signals",
        "\n\tSELECT * FROM system_health_events",
        "with x as (select * from model_signals) select * from x",
    ):
        assert validate_read_only_sql(sql) == sql

    for sql in (
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
    ):
        try:
            validate_read_only_sql(sql)
        except QuestDBAnalysisError:
            continue
        raise AssertionError(f"unsafe SQL was accepted: {sql!r}")


def _assert_parser() -> None:
    parsed = parse_exec_response(
        {
            "columns": [{"name": "ticker"}, {"name": "signal_count"}],
            "dataset": [["XOM", 2]],
            "count": 1,
        }
    )
    assert parsed.rows == [{"ticker": "XOM", "signal_count": 2}]
    assert parsed.row_count == 1

    empty = parse_exec_response(
        {
            "columns": [{"name": "ticker"}, {"name": "signal_count"}],
            "dataset": [],
        }
    )
    assert empty.rows == []

    for payload in (
        {"dataset": []},
        {"columns": [{"name": "x"}]},
        {"columns": [{"name": "x"}], "dataset": [["a", "b"]]},
        {"error": "bad query"},
    ):
        try:
            parse_exec_response(payload)
        except QuestDBAnalysisError:
            continue
        raise AssertionError(f"malformed payload was accepted: {payload!r}")


def _assert_summary_queries_are_safe() -> None:
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
        raw_tables = [table for table in FORBIDDEN_RAW_TABLES if table in lowered]
        assert not raw_tables, f"raw market-data tables referenced: {raw_tables}"
    assert not FORBIDDEN_RAW_TABLES.intersection(ANALYSIS_TABLES)


def _format_required_summary(summary: dict[str, Any]) -> str:
    counts = summary.get("table_counts", {})
    signals = summary.get("signals", {})
    risk = summary.get("risk_decisions", {})
    execution = summary.get("execution", {})
    outcomes = summary.get("outcomes", {})
    health = summary.get("system_health", {})
    return (
        "[INFO] QuestDB ledger summary: "
        f"model_signals={counts.get('model_signals', 0)}, "
        f"risk_decisions={counts.get('risk_decisions', 0)}, "
        f"orders={execution.get('order_count', 0)}, "
        f"fills={execution.get('fill_count', 0)}, "
        f"outcomes={outcomes.get('outcome_count', 0)}, "
        f"approved={risk.get('approved_count', 0)}, "
        f"blocked={risk.get('blocked_count', 0)}, "
        f"avg_confidence={signals.get('average_confidence')}, "
        f"health_warning_errors={health.get('warning_error_count', 0)}"
    )


class _RecordingReader:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.config = QuestDBAnalysisConfig()

    def execute_select(self, sql: str) -> QuestDBQueryResult:
        validate_read_only_sql(sql)
        self.queries.append(sql)
        aliases = _aliases_from_sql(sql)
        row = {alias: 0 for alias in aliases}
        if "average" in " ".join(aliases):
            for alias in aliases:
                if alias.startswith("average_"):
                    row[alias] = None
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


if __name__ == "__main__":
    raise SystemExit(main())
