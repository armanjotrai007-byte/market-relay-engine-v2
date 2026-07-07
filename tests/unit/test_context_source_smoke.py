from __future__ import annotations

import json
import os
from io import StringIO
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

import pytest

import scripts.smoke_context_sources as smoke

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def test_runner_is_invoked_with_questdb_writes_disabled() -> None:
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
