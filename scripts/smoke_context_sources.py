"""Manual server-only context source smoke validation."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import secrets
import sys
from tempfile import TemporaryDirectory
from typing import Any, Callable, Iterable, Protocol, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

SOURCE_IDS: tuple[str, ...] = (
    "macro_calendar",
    "eia_wpsr",
    "fred",
    "usaspending",
    "yfinance_dev_only",
)

PASS = "PASS"
EXPECTED_NO_DATA = "EXPECTED_NO_DATA"
SKIPPED_DISABLED = "SKIPPED_DISABLED"
FAILED = "FAILED"

LEDGER_NOT_REQUESTED = "NOT_REQUESTED"
LEDGER_NOT_CONFIGURED = "NOT_CONFIGURED"
LEDGER_WRITTEN_READBACK = "WRITTEN_READBACK"
LEDGER_NO_CONTEXT = "NO_CONTEXT"
LEDGER_FAILED = "FAILED"

QUESTDB_MARKER_SOURCE_ID = "questdb_marker"

_SENSITIVE_MARKERS = (
    "api_key",
    "authorization",
    "bearer",
    "credential",
    "password",
    "secret",
    "token",
)
_MAX_MESSAGE_LENGTH = 160


def _bootstrap_repository_src_path() -> None:
    src_dir = SRC_DIR.resolve()
    if not src_dir.is_dir():
        raise RuntimeError(f"repository src directory does not exist: {src_dir}")
    src_path = str(src_dir)
    if src_path not in sys.path:
        sys.path.insert(0, src_path)


_bootstrap_repository_src_path()


class SmokeRunner(Protocol):
    def run(self, *, sources: tuple[str, ...] | None = None) -> list["SmokeOutcome"]:
        ...


class QuestDBRuntime(Protocol):
    identity: "QuestDBValidationIdentity"

    @property
    def ledger_writer(self) -> object | None:
        ...

    def validate_marker(self) -> "SmokeOutcome":
        ...

    def verify_source_ledger_results(
        self,
        ledger_results: Sequence[object],
    ) -> tuple[str, str | None]:
        ...


@dataclass(frozen=True, kw_only=True)
class QuestDBValidationIdentity:
    run_id: str
    session_id: str
    trace_id: str


@dataclass(frozen=True, kw_only=True)
class ProbeResult:
    source_id: str
    enabled: bool
    attempted: bool
    status: str | None = None
    materialized_entry_count: int = 0
    valid_no_data: bool = False
    failed: bool = False
    error_type: str | None = None
    message: str = ""
    source_ledger: str = LEDGER_NOT_REQUESTED


@dataclass(frozen=True, kw_only=True)
class SmokeOutcome:
    source_id: str
    outcome: str
    status: str | None = None
    error_type: str | None = None
    message: str = ""
    attempted: bool = False
    source_ledger: str = LEDGER_NOT_REQUESTED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manual server-only context source smoke validation.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Required explicit confirmation for live source checks.",
    )
    parser.add_argument(
        "--env-file",
        help="Required absolute path to the server .env file.",
    )
    parser.add_argument(
        "--questdb",
        action="store_true",
        help="Explicitly require QuestDB marker and source-persistence validation.",
    )
    parser.add_argument(
        "--source",
        action="append",
        choices=SOURCE_IDS,
        help="Optional source to run; may be repeated.",
    )
    return parser


def validate_cli_confirmation(args: argparse.Namespace) -> tuple[bool, str]:
    if args.live is not True:
        return False, "--live is required before configuration or source setup"
    if not args.env_file:
        return False, "--env-file with an absolute existing path is required"
    env_path = Path(args.env_file)
    if not env_path.is_absolute():
        return False, "--env-file must be an absolute path"
    if not env_path.is_file():
        return False, "--env-file must point to an existing file"
    return True, ""


def classify_probe_result(result: ProbeResult) -> SmokeOutcome:
    if result.enabled is False:
        return SmokeOutcome(
            source_id=result.source_id,
            outcome=SKIPPED_DISABLED,
            status=result.status,
            message=_safe_message(result.message or "disabled by configuration"),
            attempted=False,
            source_ledger=result.source_ledger,
        )
    if result.attempted is False:
        return SmokeOutcome(
            source_id=result.source_id,
            outcome=FAILED,
            status=result.status,
            error_type=result.error_type or "NotAttempted",
            message=_safe_message(result.message or "enabled source was not attempted"),
            attempted=False,
            source_ledger=result.source_ledger,
        )
    if result.failed:
        return SmokeOutcome(
            source_id=result.source_id,
            outcome=FAILED,
            status=result.status,
            error_type=result.error_type or "SourceFailed",
            message=_safe_message(result.message or "source probe failed"),
            attempted=True,
            source_ledger=result.source_ledger,
        )
    if result.materialized_entry_count > 0:
        return SmokeOutcome(
            source_id=result.source_id,
            outcome=PASS,
            status=result.status,
            message=_safe_message(result.message or "materialized context selected by assembler"),
            attempted=True,
            source_ledger=result.source_ledger,
        )
    if result.valid_no_data:
        return SmokeOutcome(
            source_id=result.source_id,
            outcome=EXPECTED_NO_DATA,
            status=result.status,
            message=_safe_message(result.message or "source returned valid no-data result"),
            attempted=True,
            source_ledger=result.source_ledger,
        )
    return SmokeOutcome(
        source_id=result.source_id,
        outcome=FAILED,
        status=result.status,
        error_type=result.error_type or "NoMaterializedContext",
        message=_safe_message(result.message or "source produced no materialized context"),
        attempted=True,
        source_ledger=result.source_ledger,
    )


def aggregate_exit_code(
    outcomes: Sequence[SmokeOutcome],
    *,
    questdb_mode: bool = False,
) -> int:
    if any(
        outcome.outcome == FAILED or outcome.source_ledger == LEDGER_FAILED
        for outcome in outcomes
    ):
        return 1
    if questdb_mode:
        marker_ok = any(
            outcome.source_id == QUESTDB_MARKER_SOURCE_ID
            and outcome.outcome == PASS
            and outcome.source_ledger == LEDGER_WRITTEN_READBACK
            for outcome in outcomes
        )
        return 0 if marker_ok else 1
    tested = [
        outcome for outcome in outcomes if outcome.outcome in {PASS, EXPECTED_NO_DATA}
    ]
    if not tested:
        return 1
    return 0


def render_outcomes(outcomes: Sequence[SmokeOutcome]) -> str:
    lines = ["source_id outcome status source_ledger error_type message"]
    for outcome in outcomes:
        fields = (
            outcome.source_id,
            outcome.outcome,
            outcome.status or "-",
            outcome.source_ledger,
            outcome.error_type or "-",
            _safe_message(outcome.message) or "-",
        )
        lines.append(" | ".join(fields))
    return "\n".join(lines)


class QuestDBRuntimeValidation:
    def __init__(
        self,
        *,
        repo_root: Path,
        identity: QuestDBValidationIdentity | None = None,
        health_checker: Callable[[], bool] | None = None,
        writer: object | None = None,
        reader: object | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.identity = identity or generate_questdb_validation_identity()
        self._health_checker = health_checker
        self._writer = writer
        self._reader = reader

    @property
    def ledger_writer(self) -> object | None:
        return self._writer

    def validate_marker(self) -> SmokeOutcome:
        try:
            self._ensure_health_writer_reader()
            assert self._writer is not None
            assert self._reader is not None
            self._write_system_health_marker(self._writer)
            if not _marker_readback_succeeded(self._reader, self.identity):
                return self._failed_marker("MarkerReadbackMissing")
        except Exception as exc:  # noqa: BLE001 - script boundary sanitizes output.
            return self._failed_marker(type(exc).__name__)
        return SmokeOutcome(
            source_id=QUESTDB_MARKER_SOURCE_ID,
            outcome=PASS,
            status="VALIDATION",
            attempted=True,
            source_ledger=LEDGER_WRITTEN_READBACK,
            message="QuestDB marker write and exact read-back succeeded",
        )

    def verify_source_ledger_results(
        self,
        ledger_results: Sequence[object],
    ) -> tuple[str, str | None]:
        if not ledger_results:
            return LEDGER_FAILED, "NoLedgerResults"
        if self._reader is None:
            return LEDGER_FAILED, "ReaderUnavailable"
        expected_by_table: dict[str, int] = {}
        for result in ledger_results:
            if getattr(result, "success", False) is not True:
                return LEDGER_FAILED, "LedgerWriteFailed"
            table_name = getattr(result, "table_name", None)
            if not isinstance(table_name, str) or not _SAFE_SQL_IDENTIFIER.fullmatch(table_name):
                return LEDGER_FAILED, "UnsafeTableName"
            try:
                row_count = int(getattr(result, "row_count", 0))
            except (TypeError, ValueError):
                return LEDGER_FAILED, "InvalidRowCount"
            if row_count <= 0:
                return LEDGER_FAILED, "InvalidRowCount"
            expected_by_table[table_name] = expected_by_table.get(table_name, 0) + row_count

        for table_name, expected_count in expected_by_table.items():
            try:
                actual_count = _readback_count(
                    self._reader,
                    table_name,
                    self.identity.run_id,
                    self.identity.session_id,
                )
            except Exception:  # noqa: BLE001 - output exposes only safe status.
                return LEDGER_FAILED, "LedgerReadbackFailed"
            if actual_count < expected_count:
                return LEDGER_FAILED, "LedgerReadbackMismatch"
        return LEDGER_WRITTEN_READBACK, None

    def _ensure_health_writer_reader(self) -> None:
        if self._health_checker is not None:
            if self._health_checker() is not True:
                raise RuntimeError("QuestDB health validation failed")
        else:
            from market_relay_engine.questdb.health import (
                check_questdb_http,
                load_questdb_health_config,
            )

            config = load_questdb_health_config(
                self.repo_root / "config" / "questdb.yaml",
                required=True,
                load_dotenv_file=False,
            )
            result = check_questdb_http(config)
            if not result.reachable:
                raise RuntimeError("QuestDB health validation failed")

        if self._writer is None:
            from market_relay_engine.questdb.writer import (
                QuestDBLedgerWriter,
                load_questdb_write_config,
            )

            self._writer = QuestDBLedgerWriter(
                load_questdb_write_config(
                    self.repo_root / "config" / "questdb.yaml",
                    required=True,
                    load_dotenv_file=False,
                )
            )
        if self._reader is None:
            from market_relay_engine.questdb.analysis import (
                QuestDBLedgerReader,
                load_questdb_analysis_config,
            )

            self._reader = QuestDBLedgerReader(
                load_questdb_analysis_config(
                    self.repo_root / "config" / "questdb.yaml",
                    required=True,
                    load_dotenv_file=False,
                )
            )

    def _write_system_health_marker(self, writer: object) -> None:
        from market_relay_engine.contracts.system import SystemHealthEvent

        event = SystemHealthEvent(
            event_time=datetime.now(UTC),
            component="context_source_smoke",
            status="VALIDATION",
            message="explicit PR33 server QuestDB validation",
            trace_id=self.identity.trace_id,
        )
        result = writer.write_system_health_event(
            event,
            run_id=self.identity.run_id,
            session_id=self.identity.session_id,
        )
        if getattr(result, "success", False) is not True:
            raise RuntimeError("QuestDB marker write failed")

    @staticmethod
    def _failed_marker(error_type: str) -> SmokeOutcome:
        return SmokeOutcome(
            source_id=QUESTDB_MARKER_SOURCE_ID,
            outcome=FAILED,
            status="VALIDATION",
            error_type=error_type,
            message="QuestDB marker health, write, or read-back failed",
            attempted=True,
            source_ledger=LEDGER_FAILED,
        )


_SAFE_SQL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SAFE_VALIDATION_ID = re.compile(r"server_validation_pr33_[A-Za-z0-9_]+")


def generate_questdb_validation_identity() -> QuestDBValidationIdentity:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return QuestDBValidationIdentity(
        run_id=f"server_validation_pr33_{timestamp}_{secrets.token_hex(4)}",
        session_id=f"server_validation_pr33_{timestamp}_{secrets.token_hex(4)}",
        trace_id=f"server_validation_pr33_{timestamp}_{secrets.token_hex(4)}",
    )


def _marker_readback_succeeded(
    reader: object,
    identity: QuestDBValidationIdentity,
) -> bool:
    count = _readback_count(
        reader,
        "system_health_events",
        identity.run_id,
        identity.session_id,
        trace_id=identity.trace_id,
        component="context_source_smoke",
        status="VALIDATION",
    )
    return count >= 1


def _readback_count(
    reader: object,
    table_name: str,
    run_id: str,
    session_id: str,
    *,
    trace_id: str | None = None,
    component: str | None = None,
    status: str | None = None,
) -> int:
    if not _SAFE_SQL_IDENTIFIER.fullmatch(table_name):
        raise RuntimeError("unsafe table name")
    filters = [
        f"run_id = {_safe_validation_sql_literal(run_id)}",
        f"session_id = {_safe_validation_sql_literal(session_id)}",
    ]
    if trace_id is not None:
        filters.append(f"trace_id = {_safe_validation_sql_literal(trace_id)}")
    if component is not None:
        filters.append(f"component = {_safe_fixed_sql_literal(component)}")
    if status is not None:
        filters.append(f"status = {_safe_fixed_sql_literal(status)}")
    result = reader.execute_select(
        f"SELECT count() AS row_count FROM {table_name} WHERE {' AND '.join(filters)}"
    )
    rows = getattr(result, "rows", ())
    if not rows:
        return 0
    value = rows[0].get("row_count") if isinstance(rows[0], dict) else None
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_validation_sql_literal(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_VALIDATION_ID.fullmatch(value):
        raise RuntimeError("unsafe validation identity")
    return f"'{value}'"


def _safe_fixed_sql_literal(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_ ]{1,80}", value):
        raise RuntimeError("unsafe fixed marker literal")
    return f"'{value}'"


class ContextSourceSmokeRunner:
    def __init__(
        self,
        *,
        repo_root: Path,
        write_questdb: bool = False,
        questdb_required: bool = False,
        questdb_runtime: QuestDBRuntime | None = None,
    ) -> None:
        if questdb_required and not write_questdb:
            raise ValueError("QuestDB-required smoke runner must request QuestDB writes")
        self.repo_root = repo_root
        self.write_questdb = write_questdb
        self.questdb_required = questdb_required
        self.questdb_runtime = (
            questdb_runtime
            if questdb_runtime is not None
            else (
                QuestDBRuntimeValidation(repo_root=repo_root)
                if write_questdb
                else None
            )
        )

    def run(self, *, sources: tuple[str, ...] | None = None) -> list[SmokeOutcome]:
        requested = SOURCE_IDS if sources is None else sources
        evaluation_time = datetime.now(UTC)
        outcomes: list[SmokeOutcome] = []
        if self.write_questdb:
            assert self.questdb_runtime is not None
            marker = self.questdb_runtime.validate_marker()
            outcomes.append(marker)
            if marker.outcome == FAILED:
                return outcomes

        configs = self._load_configs()
        for source_id in requested:
            try:
                if source_id == "macro_calendar":
                    probe = self._probe_macro_calendar(configs, evaluation_time)
                elif source_id == "eia_wpsr":
                    probe = self._probe_eia_wpsr(configs, evaluation_time)
                elif source_id == "fred":
                    probe = self._probe_fred(configs, evaluation_time)
                elif source_id == "usaspending":
                    probe = self._probe_usaspending(configs, evaluation_time)
                elif source_id == "yfinance_dev_only":
                    probe = self._probe_yfinance(configs, evaluation_time)
                else:
                    probe = ProbeResult(
                        source_id=source_id,
                        enabled=True,
                        attempted=False,
                        failed=True,
                        error_type="UnsupportedSource",
                        message="unsupported source id",
                    )
            except Exception as exc:  # noqa: BLE001 - script boundary sanitizes output.
                probe = ProbeResult(
                    source_id=source_id,
                    enabled=True,
                    attempted=True,
                    failed=True,
                    error_type=type(exc).__name__,
                    message="source probe raised a safe boundary exception",
                    source_ledger=LEDGER_FAILED if self.write_questdb else LEDGER_NOT_REQUESTED,
                )
            outcomes.append(classify_probe_result(probe))
        return outcomes

    @property
    def _run_id(self) -> str | None:
        return None if self.questdb_runtime is None else self.questdb_runtime.identity.run_id

    @property
    def _session_id(self) -> str | None:
        return None if self.questdb_runtime is None else self.questdb_runtime.identity.session_id

    @property
    def _ledger_writer(self) -> object | None:
        return None if self.questdb_runtime is None else self.questdb_runtime.ledger_writer

    def _load_configs(self) -> dict[str, dict[str, object]]:
        from market_relay_engine.common.config import load_all_configs

        return load_all_configs(base_dir=self.repo_root)

    def _probe_macro_calendar(
        self,
        configs: dict[str, dict[str, object]],
        evaluation_time: datetime,
    ) -> ProbeResult:
        from market_relay_engine.context.macro_calendar import (
            MacroCalendarCollectionStatus,
            MacroCalendarCollector,
            MacroCalendarConfig,
        )
        from market_relay_engine.context.state_cache import ContextStateCache

        config = MacroCalendarConfig.from_repository_config(configs["context_sources"])
        if not config.enabled:
            return ProbeResult(
                source_id="macro_calendar",
                enabled=False,
                attempted=False,
                status="DISABLED",
            )
        cache = ContextStateCache()
        collector = MacroCalendarCollector(
            cache=cache,
            config=config,
            ledger_writer=self._ledger_writer,
            base_dir=self.repo_root,
        )
        result = collector.collect_once(
            evaluation_time,
            write_questdb=self.write_questdb,
            questdb_required=self.questdb_required,
            run_id=self._run_id,
            session_id=self._session_id,
        )
        return self._classify_materialized_result(
            source_id="macro_calendar",
            cache=cache,
            evaluation_time=evaluation_time,
            status=_status_value(result.status),
            valid_no_data_statuses={MacroCalendarCollectionStatus.NO_ACTIVE_EVENTS.value},
            failure_statuses=set(),
            failure_issue_types=set(),
            native_result=result,
            config_writes_questdb_ledger=config.writes_questdb_ledger,
        )

    def _probe_eia_wpsr(
        self,
        configs: dict[str, dict[str, object]],
        evaluation_time: datetime,
    ) -> ProbeResult:
        from market_relay_engine.context.eia_wpsr import (
            EIAWPSRCollectionStatus,
            EIAWPSRConfig,
            EIAWPSRCollector,
        )
        from market_relay_engine.context.state_cache import ContextStateCache

        config = EIAWPSRConfig.from_repository_configs(
            configs["calendar_events"],
            configs["context_sources"],
            configs["symbols"],
        )
        if not config.event_windows_enabled and not config.numeric_source_enabled:
            return ProbeResult(
                source_id="eia_wpsr",
                enabled=False,
                attempted=False,
                status="DISABLED",
            )
        cache = ContextStateCache()
        collector = EIAWPSRCollector(
            cache=cache,
            config=config,
            ledger_writer=self._ledger_writer,
        )
        if config.numeric_source_enabled:
            result = collector.probe_numeric_source(
                evaluation_time=evaluation_time,
                write_questdb=self.write_questdb,
                questdb_required=self.questdb_required,
                run_id=self._run_id,
                session_id=self._session_id,
            )
            message = "numeric EIA API probe"
        else:
            result = collector.collect(
                evaluation_time=evaluation_time,
                write_questdb=self.write_questdb,
                questdb_required=self.questdb_required,
                run_id=self._run_id,
                session_id=self._session_id,
            )
            message = "local EIA release-window validation only; numeric disabled"
        return self._classify_materialized_result(
            source_id="eia_wpsr",
            cache=cache,
            evaluation_time=evaluation_time,
            status=_status_value(result.status),
            valid_no_data_statuses={
                EIAWPSRCollectionStatus.NO_FRESH_DATA.value,
                EIAWPSRCollectionStatus.DATA_DELAYED.value,
            },
            failure_statuses={EIAWPSRCollectionStatus.FAILED.value},
            failure_issue_types={"SOURCE_REQUEST_FAILED"},
            native_result=result,
            message=message,
            config_writes_questdb_ledger=config.writes_questdb_ledger,
        )

    def _probe_fred(
        self,
        configs: dict[str, dict[str, object]],
        evaluation_time: datetime,
    ) -> ProbeResult:
        from market_relay_engine.context.fred_collector import (
            FREDCollectionStatus,
            FREDCollector,
            FREDConfig,
        )
        from market_relay_engine.context.state_cache import ContextStateCache

        config = FREDConfig.from_repository_config(configs["context_sources"])
        if not config.enabled:
            return ProbeResult(
                source_id="fred",
                enabled=False,
                attempted=False,
                status="DISABLED",
            )
        cache = ContextStateCache()
        result = FREDCollector(
            cache=cache,
            config=config,
            ledger_writer=self._ledger_writer,
        ).collect(
            evaluation_time=evaluation_time,
            write_questdb=self.write_questdb,
            questdb_required=self.questdb_required,
            run_id=self._run_id,
            session_id=self._session_id,
        )
        return self._classify_materialized_result(
            source_id="fred",
            cache=cache,
            evaluation_time=evaluation_time,
            status=_status_value(result.status),
            valid_no_data_statuses={FREDCollectionStatus.STALE.value},
            failure_statuses={FREDCollectionStatus.FAILED.value},
            failure_issue_types={"SOURCE_REQUEST_FAILED"},
            native_result=result,
            config_writes_questdb_ledger=config.writes_questdb_ledger,
        )

    def _probe_usaspending(
        self,
        configs: dict[str, dict[str, object]],
        evaluation_time: datetime,
    ) -> ProbeResult:
        from dataclasses import replace

        from market_relay_engine.context.state_cache import ContextStateCache
        from market_relay_engine.context.usaspending_collector import (
            USAspendingCollectionStatus,
            USAspendingCollector,
            USAspendingConfig,
        )

        base_config = USAspendingConfig.from_repository_config(configs["context_sources"])
        if not base_config.enabled:
            return ProbeResult(
                source_id="usaspending",
                enabled=False,
                attempted=False,
                status="DISABLED",
            )
        with TemporaryDirectory(
            prefix=".tmp-context-source-smoke-usaspending-",
            dir=self.repo_root,
        ) as temp_dir:
            checkpoint_path = Path(temp_dir) / "award_checkpoint.json"
            config = replace(
                base_config,
                checkpoint_path=_repo_relative(checkpoint_path, self.repo_root),
            )
            cache = ContextStateCache()
            result = USAspendingCollector(
                cache=cache,
                config=config,
                ledger_writer=self._ledger_writer,
            ).collect(
                evaluation_time=evaluation_time,
                write_questdb=self.write_questdb,
                questdb_required=self.questdb_required,
                run_id=self._run_id,
                session_id=self._session_id,
            )
            return self._classify_materialized_result(
                source_id="usaspending",
                cache=cache,
                evaluation_time=evaluation_time,
                status=_status_value(result.status),
                valid_no_data_statuses={USAspendingCollectionStatus.SUCCESS.value},
                failure_statuses={
                    USAspendingCollectionStatus.FAILED.value,
                    USAspendingCollectionStatus.STALE.value,
                },
                failure_issue_types={
                    "SOURCE_LAST_UPDATED_FAILED",
                    "SOURCE_LAST_UPDATED_EMPTY",
                    "SOURCE_LAST_UPDATED_INVALID",
                    "SOURCE_LAST_UPDATED_FUTURE",
                    "RECIPIENT_DISCOVERY_FAILED",
                    "AWARD_ENRICHMENT_FAILED",
                    "CHECKPOINT_PERSISTENCE_FAILED",
                },
                native_result=result,
                config_writes_questdb_ledger=config.writes_questdb_ledger,
            )

    def _probe_yfinance(
        self,
        configs: dict[str, dict[str, object]],
        evaluation_time: datetime,
    ) -> ProbeResult:
        from market_relay_engine.context.state_cache import ContextStateCache
        from market_relay_engine.context.yfinance_proxy import (
            YFinanceProxyCollectionStatus,
            YFinanceProxyCollector,
            YFinanceProxyConfig,
        )

        config = YFinanceProxyConfig.from_repository_configs(
            configs["context_sources"],
            configs["symbols"],
        )
        if not config.enabled:
            return ProbeResult(
                source_id="yfinance_dev_only",
                enabled=False,
                attempted=False,
                status="DISABLED",
            )
        cache = ContextStateCache()
        result = YFinanceProxyCollector(
            cache=cache,
            config=config,
            ledger_writer=self._ledger_writer,
        ).collect(
            evaluation_time=evaluation_time,
            write_questdb=self.write_questdb,
            questdb_required=self.questdb_required,
            run_id=self._run_id,
            session_id=self._session_id,
        )
        return self._classify_materialized_result(
            source_id="yfinance_dev_only",
            cache=cache,
            evaluation_time=evaluation_time,
            status=_status_value(result.status),
            valid_no_data_statuses={YFinanceProxyCollectionStatus.NO_FRESH_DATA.value},
            failure_statuses={YFinanceProxyCollectionStatus.FAILED.value},
            failure_issue_types={
                "AMBIGUOUS_ONE_LEVEL_COLUMNS",
                "DOWNLOAD_FAILED",
                "MISSING_CLOSE_COLUMN",
                "SYMBOL_MISSING",
                "SYMBOL_NORMALIZATION_FAILED",
                "UNSUPPORTED_COLUMN_SHAPE",
            },
            native_result=result,
            config_writes_questdb_ledger=config.writes_questdb_ledger,
        )

    def _classify_materialized_result(
        self,
        *,
        source_id: str,
        cache: object,
        evaluation_time: datetime,
        status: str,
        valid_no_data_statuses: set[str],
        failure_statuses: set[str],
        failure_issue_types: set[str],
        native_result: object,
        config_writes_questdb_ledger: bool,
        message: str = "",
    ) -> ProbeResult:
        issue_types = _issue_types(native_result)
        entries = _snapshot_entries(cache, evaluation_time)
        ledger_state, ledger_error = self._source_ledger_status(
            materialized_entry_count=len(entries),
            config_writes_questdb_ledger=config_writes_questdb_ledger,
            native_result=native_result,
        )
        if status in failure_statuses or issue_types.intersection(failure_issue_types):
            return ProbeResult(
                source_id=source_id,
                enabled=True,
                attempted=True,
                status=status,
                materialized_entry_count=len(entries),
                failed=True,
                error_type="SourceFailed",
                message=message or "source returned a failed operational status",
                source_ledger=ledger_state,
            )
        if entries:
            try:
                _verify_assembler_entries(cache, entries, evaluation_time)
            except Exception as exc:  # noqa: BLE001 - output only exposes type.
                return ProbeResult(
                    source_id=source_id,
                    enabled=True,
                    attempted=True,
                    status=status,
                    materialized_entry_count=len(entries),
                    failed=True,
                    error_type=type(exc).__name__,
                    message="assembler rejected materialized context",
                    source_ledger=ledger_state,
                )
            if ledger_state == LEDGER_FAILED:
                return ProbeResult(
                    source_id=source_id,
                    enabled=True,
                    attempted=True,
                    status=status,
                    materialized_entry_count=len(entries),
                    failed=True,
                    error_type=ledger_error or "LedgerValidationFailed",
                    message=message or "source-specific QuestDB read-back failed",
                    source_ledger=ledger_state,
                )
        return ProbeResult(
            source_id=source_id,
            enabled=True,
            attempted=True,
            status=status,
            materialized_entry_count=len(entries),
            valid_no_data=(len(entries) == 0 and status in valid_no_data_statuses),
            message=message,
            source_ledger=ledger_state,
        )

    def _source_ledger_status(
        self,
        *,
        materialized_entry_count: int,
        config_writes_questdb_ledger: bool,
        native_result: object,
    ) -> tuple[str, str | None]:
        if not self.write_questdb:
            return LEDGER_NOT_REQUESTED, None
        if materialized_entry_count <= 0:
            return LEDGER_NO_CONTEXT, None
        if not config_writes_questdb_ledger:
            return LEDGER_NOT_CONFIGURED, None
        if self.questdb_runtime is None:
            return LEDGER_FAILED, "QuestDBRuntimeUnavailable"
        return self.questdb_runtime.verify_source_ledger_results(
            tuple(getattr(native_result, "ledger_write_results", ()))
        )


def load_explicit_env_file(env_path: Path) -> None:
    from dotenv import load_dotenv

    load_dotenv(env_path, override=False)


def main(
    argv: Sequence[str] | None = None,
    *,
    env_loader: Callable[[Path], None] = load_explicit_env_file,
    runner_factory: Callable[..., SmokeRunner] = ContextSourceSmokeRunner,
    stdout: object = sys.stdout,
    stderr: object = sys.stderr,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ok, message = validate_cli_confirmation(args)
    if not ok:
        print(message, file=stderr)
        return 2

    env_path = Path(args.env_file)
    env_loader(env_path)
    runner = runner_factory(
        repo_root=REPO_ROOT,
        write_questdb=args.questdb,
        questdb_required=args.questdb,
    )
    requested = None if args.source is None else tuple(dict.fromkeys(args.source))
    outcomes = runner.run(sources=requested)
    print(render_outcomes(outcomes), file=stdout)
    return aggregate_exit_code(outcomes, questdb_mode=args.questdb)


def _snapshot_entries(cache: object, evaluation_time: datetime) -> list[dict[str, object]]:
    snapshot = cache.snapshot(now=evaluation_time)
    entries: list[dict[str, object]] = []
    entries.extend(dict(entry) for entry in snapshot["global"].values())
    for by_name in snapshot["tickers"].values():
        entries.extend(dict(entry) for entry in by_name.values())
    for by_name in snapshot["sectors"].values():
        entries.extend(dict(entry) for entry in by_name.values())
    return entries


def _verify_assembler_entries(
    cache: object,
    entries: Sequence[dict[str, object]],
    evaluation_time: datetime,
) -> None:
    from market_relay_engine.context.decision_context import DecisionContextAssembler

    for raw_entry in entries:
        scope = raw_entry["scope"]
        if scope == "GLOBAL":
            ticker = "XOM"
            sector = None
            scope_target = None
        elif scope == "TICKER":
            ticker = str(raw_entry["ticker"])
            sector = None
            scope_target = ticker
        else:
            ticker = "XOM"
            sector = str(raw_entry["sector"])
            scope_target = sector
        context = DecisionContextAssembler(cache=cache).build_for_decision(
            ticker,
            evaluation_time,
            f"trace_smoke_{_safe_identifier(str(raw_entry['source']))}_{_safe_identifier(str(raw_entry['name']))}",
            None,
            ticker_sector=sector,
        )
        selected = [
            entry
            for entry in context.all_structured_context
            if entry.cache_scope == raw_entry["scope"]
            and entry.cache_name == raw_entry["name"]
            and entry.scope_target == scope_target
            and entry.source == raw_entry["source"]
        ]
        if len(selected) != 1:
            raise RuntimeError("materialized entry was not selected by assembler")
        json.dumps(context.to_audit_payload().to_json_dict(), allow_nan=False, sort_keys=True)


def _issue_types(native_result: object) -> set[str]:
    issue_types: set[str] = set()
    for issue in getattr(native_result, "issues", ()):
        value = getattr(issue, "issue_type", None)
        if isinstance(value, str):
            issue_types.add(value)
    return issue_types


def _status_value(status: object) -> str:
    value = getattr(status, "value", status)
    return str(value)


def _repo_relative(path: Path, repo_root: Path) -> str:
    return path.resolve().relative_to(repo_root.resolve()).as_posix()


def _safe_identifier(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value)[:64] or "entry"


def _safe_message(message: str) -> str:
    text = re.sub(r"\s+", " ", str(message)).strip()
    lowered = text.lower()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        return "redacted"
    if len(text) > _MAX_MESSAGE_LENGTH:
        return text[: _MAX_MESSAGE_LENGTH - 3] + "..."
    return text


if __name__ == "__main__":
    raise SystemExit(main())
