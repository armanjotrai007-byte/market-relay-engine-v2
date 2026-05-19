from __future__ import annotations

from dataclasses import dataclass
import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

from market_relay_engine.market_data import dbn_inspector
from market_relay_engine.market_data.dbn_inspector import (
    DBNInspectionError,
    DatabentoDependencyError,
    databento_available,
    discover_dbn_files,
    format_dbn_inspection_result,
    inspect_dbn_file,
    inspect_dbn_file_info,
    inspect_dbn_folder,
    read_sidecar_json,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _build_databento_job(tmp_path: Path, file_name: str = "xnas-itch-20260513.trades.dbn") -> Path:
    job_folder = tmp_path / "LTN 2025-05-14 DBN" / "XNAS-20260515-5GXPNM33CY"
    job_folder.mkdir(parents=True)
    (job_folder / file_name).write_bytes(b"dummy dbn bytes")
    _write_json(job_folder / "condition.json", [{"date": "2026-05-13", "condition": "ok"}])
    _write_json(
        job_folder / "manifest.json",
        {"job_id": "XNAS-20260515-5GXPNM33CY", "files": [file_name]},
    )
    _write_json(
        job_folder / "metadata.json",
        {
            "job_id": "XNAS-20260515-5GXPNM33CY",
            "query": {
                "dataset": "XNAS.ITCH",
                "schema": "trades",
                "symbols": ["LMT"],
                "start": "2026-05-13",
                "end": "2026-05-14",
            },
        },
    )
    return job_folder / file_name


def test_dbn_inspector_module_imports_without_databento() -> None:
    assert importlib.import_module("market_relay_engine.market_data.dbn_inspector")


def test_valid_dbn_file_info_inspection_with_sidecars(tmp_path: Path) -> None:
    dbn_path = _build_databento_job(tmp_path)

    file_info = inspect_dbn_file_info(dbn_path)

    assert file_info.path == dbn_path
    assert file_info.file_name == "xnas-itch-20260513.trades.dbn"
    assert file_info.suffix == ".dbn"
    assert file_info.size_bytes == len(b"dummy dbn bytes")
    assert file_info.parent_folder == dbn_path.parent
    assert file_info.schema_hint == "trades"
    assert file_info.schema_hint_source == "sidecar"
    assert set(file_info.sidecar_paths) == {"condition.json", "manifest.json", "metadata.json"}
    assert file_info.sidecar_summaries["metadata.json"]["top_level_keys"] == [
        "job_id",
        "query",
    ]


def test_valid_dbn_zst_file_info_inspection(tmp_path: Path) -> None:
    dbn_path = tmp_path / "xnas-itch-20260513.mbp-1.dbn.zst"
    dbn_path.write_bytes(b"compressed-looking dummy bytes")

    file_info = inspect_dbn_file_info(dbn_path)

    assert file_info.suffix == ".dbn.zst"
    assert file_info.schema_hint == "mbp-1"
    assert file_info.schema_hint_source == "filename"


def test_recursive_folder_discovery_and_summary(tmp_path: Path) -> None:
    first = _build_databento_job(tmp_path)
    second_folder = tmp_path / "LTN 2025-05-14 DBN" / "XNAS-20260515-66AXE5GSP9"
    second_folder.mkdir()
    second = second_folder / "xnas-itch-20260513.bbo-1s.dbn"
    second.write_bytes(b"dummy")

    discovered = discover_dbn_files(tmp_path / "LTN 2025-05-14 DBN")
    folder_info = inspect_dbn_folder(tmp_path / "LTN 2025-05-14 DBN")

    assert discovered == sorted([first, second])
    assert folder_info.dbn_file_count == 2
    assert folder_info.job_folder_count == 2
    assert folder_info.sidecar_file_count == 3
    assert folder_info.schema_hints == ["bbo-1s", "trades"]
    assert folder_info.total_dbn_bytes == first.stat().st_size + second.stat().st_size


def test_non_recursive_discovery_only_checks_top_level(tmp_path: Path) -> None:
    _build_databento_job(tmp_path)
    root_file = tmp_path / "top.trades.dbn"
    root_file.write_bytes(b"root")

    assert discover_dbn_files(tmp_path, recursive=False) == [root_file]


def test_filename_schema_hint_and_unknown_pattern(tmp_path: Path) -> None:
    known = tmp_path / "xnas-itch-20260513.trades.dbn"
    known.write_bytes(b"dummy")
    unknown = tmp_path / "unknown.dbn"
    unknown.write_bytes(b"dummy")

    known_info = inspect_dbn_file_info(known)
    unknown_info = inspect_dbn_file_info(unknown)

    assert known_info.schema_hint == "trades"
    assert known_info.schema_hint_source == "filename"
    assert unknown_info.schema_hint is None
    assert unknown_info.schema_hint_source is None


def test_sidecar_json_top_level_keys_are_read(tmp_path: Path) -> None:
    sidecar_path = tmp_path / "metadata.json"
    _write_json(sidecar_path, {"schema": "trades", "dataset": "XNAS.ITCH"})

    payload = read_sidecar_json(sidecar_path)

    assert payload == {"schema": "trades", "dataset": "XNAS.ITCH"}


def test_missing_sidecars_are_allowed(tmp_path: Path) -> None:
    dbn_path = tmp_path / "xnas-itch-20260513.trades.dbn"
    dbn_path.write_bytes(b"dummy")

    file_info = inspect_dbn_file_info(dbn_path)

    assert file_info.sidecar_paths == {}
    assert file_info.sidecar_summaries == {}
    assert file_info.schema_hint == "trades"
    assert file_info.schema_hint_source == "filename"


def test_invalid_sidecar_json_fails_clearly(tmp_path: Path) -> None:
    dbn_path = tmp_path / "xnas-itch-20260513.trades.dbn"
    dbn_path.write_bytes(b"dummy")
    (tmp_path / "metadata.json").write_text("{invalid", encoding="utf-8")

    with pytest.raises(DBNInspectionError, match="Invalid sidecar JSON"):
        inspect_dbn_file_info(dbn_path)


def test_missing_path_raises_dbn_inspection_error(tmp_path: Path) -> None:
    with pytest.raises(DBNInspectionError, match="does not exist"):
        inspect_dbn_file_info(tmp_path / "missing.dbn")


def test_directory_path_works_for_folder_mode(tmp_path: Path) -> None:
    _build_databento_job(tmp_path)

    folder_info = inspect_dbn_folder(tmp_path / "LTN 2025-05-14 DBN")

    assert folder_info.dbn_file_count == 1


def test_invalid_extension_raises_dbn_inspection_error(tmp_path: Path) -> None:
    invalid_path = tmp_path / "not-dbn.txt"
    invalid_path.write_text("not dbn", encoding="utf-8")

    with pytest.raises(DBNInspectionError, match=".dbn or .dbn.zst"):
        inspect_dbn_file_info(invalid_path)


def test_databento_available_returns_bool() -> None:
    assert isinstance(databento_available(), bool)


def test_record_preview_without_databento_raises_dependency_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dbn_path = tmp_path / "xnas-itch-20260513.trades.dbn"
    dbn_path.write_bytes(b"dummy")

    def _raise_import_error(name: str) -> object:
        if name == "databento":
            raise ImportError("missing")
        return importlib.import_module(name)

    monkeypatch.setattr(dbn_inspector.importlib, "import_module", _raise_import_error)

    with pytest.raises(DatabentoDependencyError, match="Databento package is required"):
        inspect_dbn_file(dbn_path, file_info_only=False)


@dataclass(frozen=True)
class _FakeRecord:
    price: int
    symbol: str


def test_record_preview_uses_direct_bounded_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dbn_path = tmp_path / "xnas-itch-20260513.trades.dbn"
    dbn_path.write_bytes(b"dummy")
    yielded: list[int] = []

    class _FakeStore:
        def __iter__(self) -> Iterator[_FakeRecord]:
            for index in range(3):
                yielded.append(index)
                yield _FakeRecord(price=index, symbol="LMT")
            raise AssertionError("preview consumed beyond the requested limit")

    class _FakeDBNStore:
        replay = None
        to_df = None
        to_ndarray = None
        to_json = None
        to_csv = None

        @staticmethod
        def from_file(path: Path) -> _FakeStore:
            assert path == dbn_path
            return _FakeStore()

    monkeypatch.setattr(
        dbn_inspector.importlib,
        "import_module",
        lambda name: SimpleNamespace(DBNStore=_FakeDBNStore),
    )

    result = inspect_dbn_file(dbn_path, limit=3, file_info_only=False)

    assert yielded == [0, 1, 2]
    assert result["preview"]["records_previewed"] == 3
    assert result["preview"]["record_type_names"] == ["_FakeRecord"]
    assert result["preview"]["field_names"] == ["price", "symbol"]


def test_file_info_only_does_not_import_databento(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dbn_path = tmp_path / "xnas-itch-20260513.trades.dbn"
    dbn_path.write_bytes(b"dummy")

    def _fail_import(name: str) -> object:
        if name == "databento":
            raise AssertionError("file-info-only should not import Databento")
        return importlib.import_module(name)

    monkeypatch.setattr(dbn_inspector.importlib, "import_module", _fail_import)

    result = inspect_dbn_file(dbn_path, file_info_only=True)

    assert result["preview"] is None
    assert result["file"].schema_hint == "trades"


def test_record_preview_wraps_parse_and_iteration_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dbn_path = tmp_path / "xnas-itch-20260513.trades.dbn"
    dbn_path.write_bytes(b"dummy")

    class _FakeDBNStore:
        @staticmethod
        def from_file(path: Path) -> object:
            raise RuntimeError("low-level parse failure")

    monkeypatch.setattr(
        dbn_inspector.importlib,
        "import_module",
        lambda name: SimpleNamespace(DBNStore=_FakeDBNStore),
    )

    with pytest.raises(DBNInspectionError, match="Failed to parse DBN file: low-level parse failure"):
        inspect_dbn_file(dbn_path, file_info_only=False)


def test_cli_file_info_only_works_with_dummy_folder(tmp_path: Path) -> None:
    _build_databento_job(tmp_path)

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/inspect_dbn_file.py",
            "--path",
            str(tmp_path / "LTN 2025-05-14 DBN"),
            "--file-info-only",
            "--max-files",
            "10",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "DBN folder:" in completed.stdout
    assert "dbn_file_count: 1" in completed.stdout


def test_cli_invalid_path_fails_clearly(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/inspect_dbn_file.py",
            "--path",
            str(tmp_path / "missing.dbn"),
            "--file-info-only",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "DBN inspection FAILED" in completed.stderr
    assert "does not exist" in completed.stderr


def test_check_dbn_inspector_script_passes() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/check_dbn_inspector.py"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "DBN inspector validation PASSED" in completed.stdout


def test_format_dbn_inspection_result_summarizes_without_huge_json(tmp_path: Path) -> None:
    dbn_path = _build_databento_job(tmp_path)
    output = format_dbn_inspection_result(inspect_dbn_file_info(dbn_path))

    assert "metadata.json" in output
    assert "top_level_keys" in output
    assert "DBN file:" in output
