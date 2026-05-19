import importlib
from pathlib import Path

from market_relay_engine.common.serialization import from_json_string, to_json_string
from scripts.check_fixtures import (
    FIXTURE_MODULES,
    build_fixture_examples_by_category,
    find_banned_imports,
)
from tests.fixtures.ids import stable_record_id


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_all_fixture_modules_import() -> None:
    for module_name in FIXTURE_MODULES:
        assert importlib.import_module(module_name)


def test_stable_record_id_uses_exact_fixture_format() -> None:
    assert stable_record_id("signal", 1) == "FIXTURE-SIGNAL-0001"
    assert stable_record_id("order", 1) == "FIXTURE-ORDER-0001"
    assert stable_record_id("fill", 1) == "FIXTURE-FILL-0001"
    assert stable_record_id("context", 1) == "FIXTURE-CONTEXT-0001"
    assert stable_record_id("risk_decision", 23) == "FIXTURE-RISK-DECISION-0023"
    assert stable_record_id("feature snapshot", 7) == "FIXTURE-FEATURE-SNAPSHOT-0007"
    assert stable_record_id("signal", 1) == stable_record_id("signal", 1)


def test_market_fixture_warning_is_present() -> None:
    market_fixture_text = (REPO_ROOT / "tests" / "fixtures" / "market_records.py").read_text(
        encoding="utf-8"
    )
    docs_text = (REPO_ROOT / "docs" / "testing_fixtures.md").read_text(encoding="utf-8")

    warning_text = "do not represent exact Databento DBN schema field"
    assert warning_text in market_fixture_text
    assert warning_text in docs_text


def test_banned_import_scan_catches_obvious_external_import(tmp_path: Path) -> None:
    (tmp_path / "bad_fixture.py").write_text(
        "import requests\nfrom urllib import request\n",
        encoding="utf-8",
    )

    issues = find_banned_imports(tmp_path)

    assert any("requests" in issue for issue in issues)
    assert any("urllib" in issue for issue in issues)


def test_all_fixture_examples_serialize_with_pr3_helpers() -> None:
    examples_by_category = build_fixture_examples_by_category()

    assert examples_by_category
    for examples in examples_by_category.values():
        assert examples
        for example in examples:
            parsed = from_json_string(to_json_string(example))
            assert isinstance(parsed, dict)

