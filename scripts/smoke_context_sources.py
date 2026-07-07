"""Manual server-only context source smoke validation."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from typing import Callable, Iterable, Protocol, Sequence


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


class SmokeRunner(Protocol):
    def run(self, *, sources: tuple[str, ...] | None = None) -> list["SmokeOutcome"]:
        ...


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


@dataclass(frozen=True, kw_only=True)
class SmokeOutcome:
    source_id: str
    outcome: str
    status: str | None = None
    error_type: str | None = None
    message: str = ""
    attempted: bool = False


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
        )
    if result.attempted is False:
        return SmokeOutcome(
            source_id=result.source_id,
            outcome=FAILED,
            status=result.status,
            error_type=result.error_type or "NotAttempted",
            message=_safe_message(result.message or "enabled source was not attempted"),
            attempted=False,
        )
    if result.failed:
        return SmokeOutcome(
            source_id=result.source_id,
            outcome=FAILED,
            status=result.status,
            error_type=result.error_type or "SourceFailed",
            message=_safe_message(result.message or "source probe failed"),
            attempted=True,
        )
    if result.materialized_entry_count > 0:
        return SmokeOutcome(
            source_id=result.source_id,
            outcome=PASS,
            status=result.status,
            message=_safe_message(result.message or "materialized context selected by assembler"),
            attempted=True,
        )
    if result.valid_no_data:
        return SmokeOutcome(
            source_id=result.source_id,
            outcome=EXPECTED_NO_DATA,
            status=result.status,
            message=_safe_message(result.message or "source returned valid no-data result"),
            attempted=True,
        )
    return SmokeOutcome(
        source_id=result.source_id,
        outcome=FAILED,
        status=result.status,
        error_type=result.error_type or "NoMaterializedContext",
        message=_safe_message(result.message or "source produced no materialized context"),
        attempted=True,
    )


def aggregate_exit_code(outcomes: Sequence[SmokeOutcome]) -> int:
    if any(outcome.outcome == FAILED for outcome in outcomes):
        return 1
    tested = [
        outcome for outcome in outcomes if outcome.outcome in {PASS, EXPECTED_NO_DATA}
    ]
    if not tested:
        return 1
    return 0


def render_outcomes(outcomes: Sequence[SmokeOutcome]) -> str:
    lines = ["source_id outcome status error_type message"]
    for outcome in outcomes:
        fields = (
            outcome.source_id,
            outcome.outcome,
            outcome.status or "-",
            outcome.error_type or "-",
            _safe_message(outcome.message) or "-",
        )
        lines.append(" | ".join(fields))
    return "\n".join(lines)


class ContextSourceSmokeRunner:
    def __init__(
        self,
        *,
        repo_root: Path,
        write_questdb: bool = False,
        questdb_required: bool = False,
    ) -> None:
        if write_questdb is not False or questdb_required is not False:
            raise ValueError("smoke runner must disable QuestDB writes")
        self.repo_root = repo_root
        self.write_questdb = write_questdb
        self.questdb_required = questdb_required

    def run(self, *, sources: tuple[str, ...] | None = None) -> list[SmokeOutcome]:
        requested = SOURCE_IDS if sources is None else sources
        configs = self._load_configs()
        evaluation_time = datetime.now(UTC)
        outcomes: list[SmokeOutcome] = []
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
                )
            outcomes.append(classify_probe_result(probe))
        return outcomes

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
            base_dir=self.repo_root,
        )
        result = collector.collect_once(
            evaluation_time,
            write_questdb=self.write_questdb,
            questdb_required=self.questdb_required,
            run_id=None,
            session_id=None,
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
        collector = EIAWPSRCollector(cache=cache, config=config)
        if config.numeric_source_enabled:
            result = collector.probe_numeric_source(evaluation_time=evaluation_time)
            message = "numeric EIA API probe"
        else:
            result = collector.collect(
                evaluation_time=evaluation_time,
                write_questdb=self.write_questdb,
                questdb_required=self.questdb_required,
                run_id=None,
                session_id=None,
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
        result = FREDCollector(cache=cache, config=config).collect(
            evaluation_time=evaluation_time,
            write_questdb=self.write_questdb,
            questdb_required=self.questdb_required,
            run_id=None,
            session_id=None,
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
            result = USAspendingCollector(cache=cache, config=config).collect(
                evaluation_time=evaluation_time,
                write_questdb=self.write_questdb,
                questdb_required=self.questdb_required,
                run_id=None,
                session_id=None,
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
        result = YFinanceProxyCollector(cache=cache, config=config).collect(
            evaluation_time=evaluation_time,
            write_questdb=self.write_questdb,
            questdb_required=self.questdb_required,
            run_id=None,
            session_id=None,
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
        message: str = "",
    ) -> ProbeResult:
        issue_types = _issue_types(native_result)
        entries = _snapshot_entries(cache, evaluation_time)
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
                )
        return ProbeResult(
            source_id=source_id,
            enabled=True,
            attempted=True,
            status=status,
            materialized_entry_count=len(entries),
            valid_no_data=(len(entries) == 0 and status in valid_no_data_statuses),
            message=message,
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
    repo_root = Path(__file__).resolve().parents[1]
    runner = runner_factory(
        repo_root=repo_root,
        write_questdb=False,
        questdb_required=False,
    )
    requested = None if args.source is None else tuple(dict.fromkeys(args.source))
    outcomes = runner.run(sources=requested)
    print(render_outcomes(outcomes), file=stdout)
    return aggregate_exit_code(outcomes)


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
