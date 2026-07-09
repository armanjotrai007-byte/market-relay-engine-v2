from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from io import StringIO
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from tempfile import TemporaryDirectory

import pytest

import scripts.smoke_context_sources as smoke
from market_relay_engine.common.config import load_all_configs

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXED_EVALUATION_TIME = datetime(2026, 7, 8, 16, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _FakeWriteResult:
    success: bool
    table_name: str
    row_count: int


class _FakeReader:
    def __init__(self, row_count: int) -> None:
        self.row_count = row_count
        self.sql: list[str] = []

    def execute_select(self, sql: str) -> object:
        self.sql.append(sql)

        @dataclass(frozen=True)
        class Result:
            rows: list[dict[str, int]]

        return Result(rows=[{"row_count": self.row_count}])


class _SourceScopedFakeReader:
    def __init__(self, counts_by_source: dict[str, int]) -> None:
        self.counts_by_source = dict(counts_by_source)
        self.sql: list[str] = []

    def execute_select(self, sql: str) -> object:
        self.sql.append(sql)
        row_count = 0
        for source, count in self.counts_by_source.items():
            if f"source = '{source}'" in sql:
                row_count = count
                break

        @dataclass(frozen=True)
        class Result:
            rows: list[dict[str, int]]

        return Result(rows=[{"row_count": row_count}])


class _FakeWriter:
    def __init__(self, success: bool = True) -> None:
        self.success = success
        self.events: list[object] = []
        self.kwargs: list[dict[str, object]] = []

    def write_system_health_event(self, event: object, **kwargs: object) -> _FakeWriteResult:
        self.events.append(event)
        self.kwargs.append(dict(kwargs))
        return _FakeWriteResult(
            success=self.success,
            table_name="system_health_events",
            row_count=1 if self.success else 0,
        )


class _FakeQuestDBRuntime:
    def __init__(
        self,
        *,
        marker: smoke.SmokeOutcome | None = None,
        ledger_status: str = smoke.LEDGER_WRITTEN_READBACK,
        ledger_error: str | None = None,
    ) -> None:
        self.identity = smoke.QuestDBValidationIdentity(
            run_id="server_validation_pr33_20260101T000000000000Z_abcd1234",
            session_id="server_validation_pr33_20260101T000000000000Z_efab5678",
            trace_id="server_validation_pr33_20260101T000000000000Z_0123abcd",
        )
        self._marker = marker or smoke.SmokeOutcome(
            source_id=smoke.QUESTDB_MARKER_SOURCE_ID,
            outcome=smoke.PASS,
            status="VALIDATION",
            attempted=True,
            source_ledger=smoke.LEDGER_WRITTEN_READBACK,
        )
        self.ledger_status = ledger_status
        self.ledger_error = ledger_error

    @property
    def ledger_writer(self) -> object:
        return object()

    def validate_marker(self) -> smoke.SmokeOutcome:
        return self._marker

    def verify_source_ledger_results(
        self,
        ledger_results: tuple[object, ...],
        canonical_source: str,
    ) -> tuple[str, str | None]:
        return self.ledger_status, self.ledger_error


def _repo_configs() -> dict[str, dict[str, object]]:
    return deepcopy(load_all_configs(base_dir=REPO_ROOT))


def _enable_structured_source(configs: dict[str, dict[str, object]], source_id: str) -> None:
    structured = configs["context_sources"]["structured_sources"]  # type: ignore[index]
    structured[source_id]["enabled"] = True  # type: ignore[index]


def _disable_structured_source(configs: dict[str, dict[str, object]], source_id: str) -> None:
    structured = configs["context_sources"]["structured_sources"]  # type: ignore[index]
    structured[source_id]["enabled"] = False  # type: ignore[index]


def _add_reviewed_eia_release(configs: dict[str, dict[str, object]]) -> None:
    eia_window = configs["calendar_events"]["event_windows"]["eia"]  # type: ignore[index]
    eia_window["enabled"] = True
    eia_window["releases"] = [
        {
            "release_id": "eia_wpsr_2026_07_08",
            "release_at": "2026-07-08T10:30:00-04:00",
            "report_period": "2026-07-03",
        }
    ]


def _set_usaspending_map_path(
    configs: dict[str, dict[str, object]],
    map_path: Path,
) -> None:
    source = configs["context_sources"]["structured_sources"]["usaspending"]  # type: ignore[index]
    source["recipient_map_path"] = map_path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()


def test_script_bootstraps_src_path_from_absolute_import_without_site_packages() -> None:
    with TemporaryDirectory(prefix=".tmp-smoke-bootstrap-") as temp_dir:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        script_path = REPO_ROOT / "scripts" / "smoke_context_sources.py"
        src_dir = REPO_ROOT / "src"
        code = f"""
import importlib.util
import json
from pathlib import Path
import sys

script_path = Path({json.dumps(str(script_path))})
src_dir = Path({json.dumps(str(src_dir))}).resolve()
spec = importlib.util.spec_from_file_location("smoke_bootstrap_regression", script_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)
package_spec = importlib.util.find_spec("market_relay_engine")
locations = []
if package_spec is not None:
    if package_spec.origin is not None:
        locations.append(package_spec.origin)
    if package_spec.submodule_search_locations is not None:
        locations.extend(str(path) for path in package_spec.submodule_search_locations)
print(json.dumps({{
    "src_in_path": str(src_dir) in sys.path,
    "package_spec_found": package_spec is not None,
    "locations": locations,
}}))
"""

        completed = subprocess.run(
            [sys.executable, "-S", "-c", code],
            cwd=temp_dir,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["src_in_path"] is True
    assert payload["package_spec_found"] is True
    assert any(
        Path(location).resolve().is_relative_to(src_dir.resolve())
        for location in payload["locations"]
    )


def test_help_exits_before_environment_loading_or_runner_construction() -> None:
    calls: list[str] = []

    with pytest.raises(SystemExit) as exc_info:
        smoke.main(
            ["--help"],
            env_loader=lambda path: calls.append(f"env:{path}"),
            runner_factory=lambda **kwargs: calls.append(f"runner:{kwargs}"),  # type: ignore[arg-type]
            stdout=StringIO(),
            stderr=StringIO(),
        )

    assert exc_info.value.code == 0
    assert calls == []


def test_no_live_refuses_before_environment_loading_or_runner_construction() -> None:
    calls: list[str] = []

    def env_loader(path: Path) -> None:
        calls.append(f"env:{path}")

    def runner_factory(**kwargs: object) -> object:
        calls.append(f"runner:{kwargs}")
        raise AssertionError("runner must not be constructed")

    stderr = StringIO()
    code = smoke.main(
        [],
        env_loader=env_loader,
        runner_factory=runner_factory,
        stdout=StringIO(),
        stderr=stderr,
    )

    assert code == 2
    assert calls == []
    assert "--live is required" in stderr.getvalue()


def test_questdb_without_live_refuses_before_environment_loading_or_runner_construction() -> None:
    calls: list[str] = []

    with TemporaryDirectory(prefix=".tmp-smoke-test-", dir=REPO_ROOT) as temp_dir:
        env_file = Path(temp_dir) / ".env"
        env_file.write_text("FRED_API_KEY=fake\n", encoding="utf-8")
        stderr = StringIO()
        code = smoke.main(
            ["--questdb", "--env-file", str(env_file)],
            env_loader=lambda path: calls.append(f"env:{path}"),
            runner_factory=lambda **kwargs: calls.append(f"runner:{kwargs}"),  # type: ignore[arg-type]
            stdout=StringIO(),
            stderr=stderr,
        )

    assert code == 2
    assert calls == []
    assert "--live is required" in stderr.getvalue()


@pytest.mark.parametrize(
    "argv",
    (
        ["--live"],
        ["--live", "--env-file", "relative.env"],
    ),
)
def test_missing_or_relative_env_file_is_rejected(argv: list[str]) -> None:
    calls: list[str] = []

    code = smoke.main(
        argv,
        env_loader=lambda path: calls.append(f"env:{path}"),
        runner_factory=lambda **kwargs: calls.append(f"runner:{kwargs}"),  # type: ignore[arg-type]
        stdout=StringIO(),
        stderr=StringIO(),
    )

    assert code == 2
    assert calls == []


def test_explicit_env_file_overrides_ambient_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUESTDB_HTTP_HOST", "ambient-host")
    monkeypatch.setenv("FRED_API_KEY", "ambient-fred-key")

    with TemporaryDirectory(prefix=".tmp-smoke-env-") as temp_dir:
        env_file = Path(temp_dir) / "server.env"
        env_file.write_text(
            "QUESTDB_HTTP_HOST=explicit-host\n"
            "FRED_API_KEY=explicit-fred-key\n",
            encoding="utf-8",
        )

        smoke.load_explicit_env_file(env_file)

    assert os.environ["QUESTDB_HTTP_HOST"] == "explicit-host"
    assert os.environ["FRED_API_KEY"] == "explicit-fred-key"


def test_disabled_sources_produce_skipped_disabled() -> None:
    outcome = smoke.classify_probe_result(
        smoke.ProbeResult(
            source_id="fred",
            enabled=False,
            attempted=False,
            status="DISABLED",
        )
    )

    assert outcome.outcome == smoke.SKIPPED_DISABLED
    assert outcome.attempted is False


def test_macro_disabled_message_reports_parsed_config_path() -> None:
    configs = _repo_configs()
    _disable_structured_source(configs, "macro_calendar")
    runner = smoke.ContextSourceSmokeRunner(repo_root=REPO_ROOT)
    result = runner._probe_macro_calendar(configs, FIXED_EVALUATION_TIME)
    outcome = smoke.classify_probe_result(result)

    assert outcome.outcome == smoke.SKIPPED_DISABLED
    assert "structured_sources.macro_calendar.enabled is false" in outcome.message


def test_macro_enabled_config_is_not_classified_disabled() -> None:
    configs = _repo_configs()
    _enable_structured_source(configs, "macro_calendar")
    runner = smoke.ContextSourceSmokeRunner(repo_root=REPO_ROOT)

    result = runner._probe_macro_calendar(configs, FIXED_EVALUATION_TIME)
    outcome = smoke.classify_probe_result(result)

    assert result.enabled is True
    assert outcome.outcome != smoke.SKIPPED_DISABLED


def test_eia_enabled_source_requires_enabled_release_windows() -> None:
    configs = _repo_configs()
    _enable_structured_source(configs, "eia")
    eia_window = configs["calendar_events"]["event_windows"]["eia"]  # type: ignore[index]
    eia_window["enabled"] = False
    runner = smoke.ContextSourceSmokeRunner(repo_root=REPO_ROOT)

    result = runner._probe_eia_wpsr(configs, FIXED_EVALUATION_TIME)
    outcome = smoke.classify_probe_result(result)

    assert outcome.outcome == smoke.FAILED
    assert outcome.error_type == "EiaReleaseWindowsDisabled"
    assert "calendar_events.event_windows.eia.enabled=true" in outcome.message


def test_eia_enabled_windows_require_reviewed_releases() -> None:
    configs = _repo_configs()
    _enable_structured_source(configs, "eia")
    eia_window = configs["calendar_events"]["event_windows"]["eia"]  # type: ignore[index]
    eia_window["enabled"] = True
    eia_window["releases"] = []
    runner = smoke.ContextSourceSmokeRunner(repo_root=REPO_ROOT)

    result = runner._probe_eia_wpsr(configs, FIXED_EVALUATION_TIME)
    outcome = smoke.classify_probe_result(result)

    assert outcome.outcome == smoke.FAILED
    assert outcome.error_type == "EiaReleasesMissing"
    assert "reviewed release entries" in outcome.message


def test_eia_enabled_numeric_source_requires_key_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    configs = _repo_configs()
    _enable_structured_source(configs, "eia")
    _add_reviewed_eia_release(configs)
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    runner = smoke.ContextSourceSmokeRunner(repo_root=REPO_ROOT)

    result = runner._probe_eia_wpsr(configs, FIXED_EVALUATION_TIME)
    outcome = smoke.classify_probe_result(result)

    assert outcome.outcome == smoke.FAILED
    assert outcome.error_type == "EiaApiKeyMissing"
    assert "configured source key environment variable" in outcome.message
    assert "redacted" not in outcome.message


def test_eia_enabled_config_path_reaches_numeric_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    import market_relay_engine.context.eia_wpsr as eia_module

    configs = _repo_configs()
    _enable_structured_source(configs, "eia")
    _add_reviewed_eia_release(configs)
    monkeypatch.setenv("EIA_API_KEY", "fake-test-key")

    def fake_probe(self: object, **kwargs: object) -> object:
        return SimpleNamespace(status="NO_FRESH_DATA", issues=(), ledger_write_results=())

    monkeypatch.setattr(eia_module.EIAWPSRCollector, "probe_numeric_source", fake_probe)
    runner = smoke.ContextSourceSmokeRunner(repo_root=REPO_ROOT)

    result = runner._probe_eia_wpsr(configs, FIXED_EVALUATION_TIME)
    outcome = smoke.classify_probe_result(result)

    assert outcome.outcome == smoke.EXPECTED_NO_DATA
    assert outcome.error_type is None


def test_usaspending_enabled_empty_mapping_is_actionable_failure() -> None:
    with TemporaryDirectory(prefix=".tmp-smoke-usaspending-empty-", dir=REPO_ROOT) as temp_dir:
        map_path = Path(temp_dir) / "recipient_map.yaml"
        map_path.write_text(
            "mapping_version: usaspending_recipient_map_v1\nrecipients: []\n",
            encoding="utf-8",
        )
        configs = _repo_configs()
        _enable_structured_source(configs, "usaspending")
        _set_usaspending_map_path(configs, map_path)
        validation_mode = configs["context_sources"]["validation_modes"]["usaspending"]  # type: ignore[index]
        validation_mode["allow_health_only_without_recipient_mapping"] = False
        runner = smoke.ContextSourceSmokeRunner(repo_root=REPO_ROOT)

        result = runner._probe_usaspending(configs, FIXED_EVALUATION_TIME)
    outcome = smoke.classify_probe_result(result)

    assert outcome.outcome == smoke.FAILED
    assert outcome.error_type == "USAspendingRecipientMapEmpty"
    assert "active confirmed recipient mapping" in outcome.message
    assert "source probe raised a safe boundary exception" not in outcome.message


def test_usaspending_health_only_mode_allows_empty_mapping_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import market_relay_engine.context.usaspending_collector as usaspending_module

    configs = _repo_configs()
    _enable_structured_source(configs, "usaspending")

    class FakeUSAspendingHTTPClient:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = dict(kwargs)

        def fetch_last_updated(self) -> dict[str, object]:
            return {"last_updated": "2026-07-08"}

    monkeypatch.setattr(
        usaspending_module,
        "USAspendingHTTPClient",
        FakeUSAspendingHTTPClient,
    )
    with TemporaryDirectory(prefix=".tmp-smoke-usaspending-empty-", dir=REPO_ROOT) as temp_dir:
        map_path = Path(temp_dir) / "recipient_map.yaml"
        map_path.write_text(
            "mapping_version: usaspending_recipient_map_v1\nrecipients: []\n",
            encoding="utf-8",
        )
        _set_usaspending_map_path(configs, map_path)
        runner = smoke.ContextSourceSmokeRunner(repo_root=REPO_ROOT)

        result = runner._probe_usaspending(configs, FIXED_EVALUATION_TIME)
    outcome = smoke.classify_probe_result(result)

    assert outcome.outcome == smoke.EXPECTED_NO_DATA
    assert outcome.status == "HEALTH_ONLY_NO_MAPPING"
    assert outcome.error_type is None
    assert "source-health request and parser succeeded" in outcome.message


def test_usaspending_mapped_no_awards_is_expected_no_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import market_relay_engine.context.usaspending_collector as usaspending_module

    configs = _repo_configs()
    _enable_structured_source(configs, "usaspending")
    search_calls: list[dict[str, object]] = []

    class FakeUSAspendingHTTPClient:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = dict(kwargs)

        def fetch_last_updated(self) -> dict[str, object]:
            return {"last_updated": "2026-07-08"}

        def search_spending_by_award(self, **kwargs: object) -> dict[str, object]:
            search_calls.append(dict(kwargs))
            return {"results": [], "page_metadata": {"hasNext": False}}

        def fetch_award_detail(self, award_id: str) -> dict[str, object]:
            raise AssertionError("no awards should not request detail")

        def fetch_award_funding(self, award_id: str, *, limit: int) -> dict[str, object]:
            raise AssertionError("no awards should not request funding")

    monkeypatch.setattr(
        usaspending_module,
        "USAspendingHTTPClient",
        FakeUSAspendingHTTPClient,
    )
    with TemporaryDirectory(prefix=".tmp-smoke-usaspending-map-", dir=REPO_ROOT) as temp_dir:
        map_path = Path(temp_dir) / "recipient_map.yaml"
        map_path.write_text(
            "mapping_version: usaspending_recipient_map_v1\n"
            "recipients:\n"
            "  - recipient_uei: ABCDEF123456\n"
            "    recipient_name: EXACT LEGAL NAME\n"
            "    ticker: LMT\n"
            "    issuer_name: Lockheed Martin Corporation\n"
            "    mapping_confidence: confirmed\n"
            "    economic_beneficiary: prime_recipient\n"
            "    active: true\n"
            "    mapping_version: usaspending_recipient_map_v1\n",
            encoding="utf-8",
        )
        _set_usaspending_map_path(configs, map_path)
        runner = smoke.ContextSourceSmokeRunner(repo_root=REPO_ROOT)

        result = runner._probe_usaspending(configs, FIXED_EVALUATION_TIME)
    outcome = smoke.classify_probe_result(result)

    assert search_calls
    assert search_calls[0]["recipient_uei"] == "ABCDEF123456"
    assert outcome.outcome == smoke.EXPECTED_NO_DATA
    assert outcome.status == "SUCCESS"
    assert outcome.error_type is None


def test_source_issue_messages_are_actionable_and_redacted() -> None:
    eia_message = smoke._failure_message_from_issues(
        "eia_wpsr",
        {"SOURCE_REQUEST_FAILED"},
        "",
    )
    usaspending_message = smoke._failure_message_from_issues(
        "usaspending",
        {"SOURCE_LAST_UPDATED_FAILED"},
        "",
    )

    assert "official EIA source request failed" in eia_message
    assert "official USAspending source-health HTTP request failed" in usaspending_message
    rendered = smoke.render_outcomes(
        [
            smoke.SmokeOutcome(
                source_id="eia_wpsr",
                outcome=smoke.FAILED,
                status="FAILED",
                error_type="EiaApiKeyMissing",
                message=eia_message,
            )
        ]
    )
    assert "redacted" not in rendered


def test_attempted_valid_no_data_becomes_expected_no_data() -> None:
    outcome = smoke.classify_probe_result(
        smoke.ProbeResult(
            source_id="macro_calendar",
            enabled=True,
            attempted=True,
            status="NO_ACTIVE_EVENTS",
            materialized_entry_count=0,
            valid_no_data=True,
        )
    )

    assert outcome.outcome == smoke.EXPECTED_NO_DATA


def test_enabled_not_attempted_result_is_not_success() -> None:
    outcome = smoke.classify_probe_result(
        smoke.ProbeResult(
            source_id="eia_wpsr",
            enabled=True,
            attempted=False,
            status="SKIPPED_NOT_DUE",
        )
    )

    assert outcome.outcome == smoke.FAILED
    assert outcome.error_type == "NotAttempted"


def test_any_failure_makes_exit_nonzero_while_remaining_outcomes_render() -> None:
    outcomes = [
        smoke.SmokeOutcome(source_id="fred", outcome=smoke.FAILED, status="FAILED"),
        smoke.SmokeOutcome(source_id="macro_calendar", outcome=smoke.PASS, status="SUCCESS"),
    ]

    rendered = smoke.render_outcomes(outcomes)

    assert smoke.aggregate_exit_code(outcomes) == 1
    assert "fred" in rendered
    assert "macro_calendar" in rendered


def test_no_tested_sources_exits_nonzero() -> None:
    assert smoke.aggregate_exit_code([]) == 1
    assert smoke.aggregate_exit_code(
        [
            smoke.SmokeOutcome(
                source_id="fred",
                outcome=smoke.SKIPPED_DISABLED,
                status="DISABLED",
            )
        ]
    ) == 1


def test_normal_live_runner_is_invoked_with_questdb_writes_disabled() -> None:
    with TemporaryDirectory(prefix=".tmp-smoke-test-", dir=REPO_ROOT) as temp_dir:
        env_file = Path(temp_dir) / ".env"
        env_file.write_text("FRED_API_KEY=fake\n", encoding="utf-8")
        calls: list[object] = []

        class FakeRunner:
            def run(self, *, sources: tuple[str, ...] | None = None) -> list[smoke.SmokeOutcome]:
                calls.append(("run", sources))
                return [
                    smoke.SmokeOutcome(
                        source_id="fred",
                        outcome=smoke.PASS,
                        status="SUCCESS",
                        attempted=True,
                    )
                ]

        def runner_factory(**kwargs: object) -> FakeRunner:
            calls.append(("factory", kwargs))
            assert kwargs["write_questdb"] is False
            assert kwargs["questdb_required"] is False
            return FakeRunner()

        code = smoke.main(
            ["--live", "--env-file", str(env_file), "--source", "fred"],
            env_loader=lambda path: calls.append(("env", path)),
            runner_factory=runner_factory,
            stdout=StringIO(),
            stderr=StringIO(),
        )

        assert code == 0
        assert calls[0] == ("env", env_file)
        assert calls[1][0] == "factory"
        assert calls[2] == ("run", ("fred",))


def test_live_questdb_runner_is_invoked_with_questdb_required() -> None:
    with TemporaryDirectory(prefix=".tmp-smoke-test-", dir=REPO_ROOT) as temp_dir:
        env_file = Path(temp_dir) / ".env"
        env_file.write_text("FRED_API_KEY=fake\n", encoding="utf-8")
        calls: list[object] = []

        class FakeRunner:
            def run(self, *, sources: tuple[str, ...] | None = None) -> list[smoke.SmokeOutcome]:
                calls.append(("run", sources))
                return [
                    smoke.SmokeOutcome(
                        source_id=smoke.QUESTDB_MARKER_SOURCE_ID,
                        outcome=smoke.PASS,
                        status="VALIDATION",
                        attempted=True,
                        source_ledger=smoke.LEDGER_WRITTEN_READBACK,
                    ),
                    smoke.SmokeOutcome(
                        source_id="fred",
                        outcome=smoke.PASS,
                        status="SUCCESS",
                        attempted=True,
                        source_ledger=smoke.LEDGER_WRITTEN_READBACK,
                    )
                ]

        def runner_factory(**kwargs: object) -> FakeRunner:
            calls.append(("factory", kwargs))
            assert kwargs["write_questdb"] is True
            assert kwargs["questdb_required"] is True
            return FakeRunner()

        code = smoke.main(
            ["--live", "--questdb", "--env-file", str(env_file), "--source", "fred"],
            env_loader=lambda path: calls.append(("env", path)),
            runner_factory=runner_factory,
            stdout=StringIO(),
            stderr=StringIO(),
        )

    assert code == 0
    assert calls[0] == ("env", env_file)
    assert calls[1][0] == "factory"
    assert calls[2] == ("run", ("fred",))


def test_questdb_health_failure_fails_without_source_collection() -> None:
    marker = smoke.SmokeOutcome(
        source_id=smoke.QUESTDB_MARKER_SOURCE_ID,
        outcome=smoke.FAILED,
        status="VALIDATION",
        error_type="QuestDBHealthError",
        attempted=True,
        source_ledger=smoke.LEDGER_FAILED,
    )
    runner = smoke.ContextSourceSmokeRunner(
        repo_root=REPO_ROOT,
        write_questdb=True,
        questdb_required=True,
        questdb_runtime=_FakeQuestDBRuntime(marker=marker),
    )

    def fail_load_configs() -> dict[str, dict[str, object]]:
        raise AssertionError("sources must not load after QuestDB marker failure")

    runner._load_configs = fail_load_configs  # type: ignore[method-assign]

    outcomes = runner.run(sources=("fred",))

    assert outcomes == [marker]
    assert smoke.aggregate_exit_code(outcomes, questdb_mode=True) == 1


def test_successful_marker_write_plus_exact_reader_readback_is_accepted() -> None:
    writer = _FakeWriter()
    reader = _FakeReader(row_count=1)
    runtime = smoke.QuestDBRuntimeValidation(
        repo_root=REPO_ROOT,
        identity=_FakeQuestDBRuntime().identity,
        health_checker=lambda: True,
        writer=writer,
        reader=reader,
    )

    outcome = runtime.validate_marker()

    assert outcome.outcome == smoke.PASS
    assert outcome.source_ledger == smoke.LEDGER_WRITTEN_READBACK
    assert getattr(writer.events[0], "component") == "context_source_smoke"
    assert getattr(writer.events[0], "status") == "VALIDATION"
    assert writer.kwargs[0]["run_id"] == runtime.identity.run_id
    assert writer.kwargs[0]["session_id"] == runtime.identity.session_id
    marker_sql = reader.sql[0]
    assert f"run_id = '{runtime.identity.run_id}'" in marker_sql
    assert f"session_id = '{runtime.identity.session_id}'" in marker_sql
    assert f"trace_id = '{runtime.identity.trace_id}'" in marker_sql
    assert "component = 'context_source_smoke'" in marker_sql
    assert "status = 'VALIDATION'" in marker_sql
    assert "source =" not in marker_sql


def test_missing_marker_readback_fails() -> None:
    runtime = smoke.QuestDBRuntimeValidation(
        repo_root=REPO_ROOT,
        identity=_FakeQuestDBRuntime().identity,
        health_checker=lambda: True,
        writer=_FakeWriter(),
        reader=_FakeReader(row_count=0),
    )

    outcome = runtime.validate_marker()

    assert outcome.outcome == smoke.FAILED
    assert outcome.source_ledger == smoke.LEDGER_FAILED
    assert outcome.error_type == "MarkerReadbackMissing"


def test_source_readback_rejects_cross_source_shared_table_false_positive() -> None:
    runtime = smoke.QuestDBRuntimeValidation(
        repo_root=REPO_ROOT,
        identity=_FakeQuestDBRuntime().identity,
        reader=_SourceScopedFakeReader({"macro_calendar": 2, "fred": 0}),
    )

    status, error = runtime.verify_source_ledger_results(
        (_FakeWriteResult(True, "context_indicator_snapshots", 1),),
        "fred",
    )

    assert status == smoke.LEDGER_FAILED
    assert error == "LedgerReadbackMismatch"
    assert status != smoke.LEDGER_WRITTEN_READBACK


def test_source_readback_accepts_current_source_rows() -> None:
    runtime = smoke.QuestDBRuntimeValidation(
        repo_root=REPO_ROOT,
        identity=_FakeQuestDBRuntime().identity,
        reader=_SourceScopedFakeReader({"macro_calendar": 2, "fred": 1}),
    )

    status, error = runtime.verify_source_ledger_results(
        (_FakeWriteResult(True, "context_indicator_snapshots", 1),),
        "fred",
    )

    assert status == smoke.LEDGER_WRITTEN_READBACK
    assert error is None


def test_source_readback_query_is_source_scoped() -> None:
    identity = _FakeQuestDBRuntime().identity
    reader = _SourceScopedFakeReader({"fred": 1})
    runtime = smoke.QuestDBRuntimeValidation(
        repo_root=REPO_ROOT,
        identity=identity,
        reader=reader,
    )

    status, _ = runtime.verify_source_ledger_results(
        (_FakeWriteResult(True, "context_indicator_snapshots", 1),),
        "fred",
    )

    assert status == smoke.LEDGER_WRITTEN_READBACK
    assert len(reader.sql) == 1
    sql = reader.sql[0]
    assert f"run_id = '{identity.run_id}'" in sql
    assert f"session_id = '{identity.session_id}'" in sql
    assert "source = 'fred'" in sql
    assert "WHERE run_id" in sql


def test_unsupported_unscoped_source_table_fails_closed_without_query() -> None:
    reader = _SourceScopedFakeReader({"fred": 1})
    runtime = smoke.QuestDBRuntimeValidation(
        repo_root=REPO_ROOT,
        identity=_FakeQuestDBRuntime().identity,
        reader=reader,
    )

    status, error = runtime.verify_source_ledger_results(
        (_FakeWriteResult(True, "system_health_events", 1),),
        "fred",
    )

    assert status == smoke.LEDGER_FAILED
    assert error == "SourceReadbackUnscoped"
    assert reader.sql == []


def test_questdb_marker_only_is_not_successful_context_source_validation() -> None:
    outcomes = [
        smoke.SmokeOutcome(
            source_id=smoke.QUESTDB_MARKER_SOURCE_ID,
            outcome=smoke.PASS,
            status="VALIDATION",
            attempted=True,
            source_ledger=smoke.LEDGER_WRITTEN_READBACK,
        )
    ]

    assert smoke.aggregate_exit_code(outcomes, questdb_mode=True) == 1


def test_questdb_marker_plus_disabled_source_is_not_successful_context_source_validation() -> None:
    outcomes = [
        smoke.SmokeOutcome(
            source_id=smoke.QUESTDB_MARKER_SOURCE_ID,
            outcome=smoke.PASS,
            status="VALIDATION",
            attempted=True,
            source_ledger=smoke.LEDGER_WRITTEN_READBACK,
        ),
        smoke.SmokeOutcome(
            source_id="fred",
            outcome=smoke.SKIPPED_DISABLED,
            status="DISABLED",
            attempted=False,
            source_ledger=smoke.LEDGER_NOT_REQUESTED,
        ),
    ]

    assert smoke.aggregate_exit_code(outcomes, questdb_mode=True) == 1


def test_questdb_marker_plus_materialized_persisted_source_succeeds() -> None:
    outcomes = [
        smoke.SmokeOutcome(
            source_id=smoke.QUESTDB_MARKER_SOURCE_ID,
            outcome=smoke.PASS,
            status="VALIDATION",
            attempted=True,
            source_ledger=smoke.LEDGER_WRITTEN_READBACK,
        ),
        smoke.SmokeOutcome(
            source_id="fred",
            outcome=smoke.PASS,
            status="SUCCESS",
            attempted=True,
            source_ledger=smoke.LEDGER_WRITTEN_READBACK,
        ),
    ]

    assert smoke.aggregate_exit_code(outcomes, questdb_mode=True) == 0


def test_materialized_context_with_failed_ledger_readback_fails_source_validation() -> None:
    runner = smoke.ContextSourceSmokeRunner(
        repo_root=REPO_ROOT,
        write_questdb=True,
        questdb_required=True,
        questdb_runtime=_FakeQuestDBRuntime(
            ledger_status=smoke.LEDGER_FAILED,
            ledger_error="LedgerReadbackMismatch",
        ),
    )

    status, error = runner._source_ledger_status(
        materialized_entry_count=1,
        config_writes_questdb_ledger=True,
        native_result=type(
            "NativeResult",
            (),
            {"ledger_write_results": (_FakeWriteResult(True, "context_indicator_snapshots", 1),)},
        )(),
        canonical_ledger_source="fred",
    )

    assert status == smoke.LEDGER_FAILED
    assert error == "LedgerReadbackMismatch"


def test_materialized_context_with_questdb_not_configured_fails_source_validation() -> None:
    runner = smoke.ContextSourceSmokeRunner(
        repo_root=REPO_ROOT,
        write_questdb=True,
        questdb_required=True,
        questdb_runtime=_FakeQuestDBRuntime(),
    )

    status, error = runner._source_ledger_status(
        materialized_entry_count=1,
        config_writes_questdb_ledger=False,
        native_result=type("NativeResult", (), {"ledger_write_results": ()})(),
        canonical_ledger_source="fred",
    )

    assert status == smoke.LEDGER_FAILED
    assert error == "LedgerNotConfigured"


def test_valid_no_data_still_succeeds_when_mandatory_questdb_marker_succeeds() -> None:
    outcomes = [
        smoke.SmokeOutcome(
            source_id=smoke.QUESTDB_MARKER_SOURCE_ID,
            outcome=smoke.PASS,
            status="VALIDATION",
            attempted=True,
            source_ledger=smoke.LEDGER_WRITTEN_READBACK,
        ),
        smoke.SmokeOutcome(
            source_id="fred",
            outcome=smoke.EXPECTED_NO_DATA,
            status="STALE",
            attempted=True,
            source_ledger=smoke.LEDGER_NO_CONTEXT,
        ),
    ]

    assert smoke.aggregate_exit_code(outcomes, questdb_mode=True) == 0


def test_questdb_marker_with_pass_not_configured_source_is_not_successful() -> None:
    outcomes = [
        smoke.SmokeOutcome(
            source_id=smoke.QUESTDB_MARKER_SOURCE_ID,
            outcome=smoke.PASS,
            status="VALIDATION",
            attempted=True,
            source_ledger=smoke.LEDGER_WRITTEN_READBACK,
        ),
        smoke.SmokeOutcome(
            source_id="fred",
            outcome=smoke.PASS,
            status="SUCCESS",
            attempted=True,
            source_ledger=smoke.LEDGER_NOT_CONFIGURED,
        ),
    ]

    assert smoke.aggregate_exit_code(outcomes, questdb_mode=True) == 1


def test_ordinary_non_questdb_aggregation_is_unchanged() -> None:
    assert smoke.aggregate_exit_code(
        [
            smoke.SmokeOutcome(
                source_id="fred",
                outcome=smoke.PASS,
                status="SUCCESS",
                attempted=True,
                source_ledger=smoke.LEDGER_NOT_REQUESTED,
            )
        ]
    ) == 0
    assert smoke.aggregate_exit_code(
        [
            smoke.SmokeOutcome(
                source_id="fred",
                outcome=smoke.EXPECTED_NO_DATA,
                status="STALE",
                attempted=True,
                source_ledger=smoke.LEDGER_NOT_REQUESTED,
            )
        ]
    ) == 0
    assert smoke.aggregate_exit_code([]) == 1


def test_not_configured_is_rendered_distinctly_without_claiming_persistence_success() -> None:
    rendered = smoke.render_outcomes(
        [
            smoke.SmokeOutcome(
                source_id="fred",
                outcome=smoke.PASS,
                status="SUCCESS",
                attempted=True,
                source_ledger=smoke.LEDGER_NOT_CONFIGURED,
            )
        ]
    )

    assert smoke.LEDGER_NOT_CONFIGURED in rendered
    assert smoke.LEDGER_WRITTEN_READBACK not in rendered


def test_console_rendering_redacts_secret_bearing_values() -> None:
    rendered = smoke.render_outcomes(
        [
            smoke.SmokeOutcome(
                source_id="fred",
                outcome=smoke.FAILED,
                status="FAILED",
                error_type="RuntimeError",
                message='config dump {"api_key": "hidden-secret-value"}',
            )
        ]
    )

    assert "hidden-secret-value" not in rendered
    assert "api_key" not in rendered
    assert "secret" not in rendered.lower()
    assert "redacted" in rendered
