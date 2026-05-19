"""Validate the local DBN inspector with generated dummy files only."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.market_data.dbn_inspector import (  # noqa: E402
    DBNInspectionError,
    DatabentoDependencyError,
    databento_available,
    discover_dbn_files,
    inspect_dbn_file,
    inspect_dbn_file_info,
    inspect_dbn_folder,
)


def _record(results: list[tuple[bool, str]], ok: bool, message: str) -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {message}")
    results.append((ok, message))


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _build_dummy_folder(root: Path) -> Path:
    job_folder = root / "LTN 2025-05-14 DBN" / "XNAS-20260515-5GXPNM33CY"
    job_folder.mkdir(parents=True)
    (job_folder / "xnas-itch-20260513.trades.dbn").write_bytes(b"dummy dbn bytes")
    _write_json(job_folder / "condition.json", [{"date": "2026-05-13", "condition": "ok"}])
    _write_json(
        job_folder / "manifest.json",
        {"job_id": "XNAS-20260515-5GXPNM33CY", "files": ["xnas-itch-20260513.trades.dbn"]},
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
    return job_folder


def main() -> int:
    results: list[tuple[bool, str]] = []

    try:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_folder = _build_dummy_folder(root)
            dbn_path = job_folder / "xnas-itch-20260513.trades.dbn"

            file_info = inspect_dbn_file_info(dbn_path)
            _record(results, file_info.file_name == dbn_path.name, "File-info inspection works")
            _record(results, file_info.schema_hint == "trades", "Sidecar schema hint is read")
            _record(results, file_info.schema_hint_source == "sidecar", "Schema hint source is sidecar")
            _record(results, len(file_info.sidecar_paths) == 3, "Sidecar files are detected")

            folder_info = inspect_dbn_folder(root / "LTN 2025-05-14 DBN")
            _record(results, folder_info.dbn_file_count == 1, "Folder inspection counts DBN files")
            _record(results, folder_info.job_folder_count == 1, "Folder inspection counts job folders")
            _record(results, folder_info.sidecar_file_count == 3, "Folder inspection counts sidecars")
            _record(results, folder_info.total_dbn_bytes == dbn_path.stat().st_size, "Folder bytes sum")

            discovered = discover_dbn_files(root)
            _record(results, discovered == [dbn_path], "Recursive discovery finds dummy DBN")

            try:
                inspect_dbn_file_info(root / "missing.dbn")
                _record(results, False, "Missing path fails")
            except DBNInspectionError:
                _record(results, True, "Missing path fails")

            invalid_extension = job_folder / "not-dbn.txt"
            invalid_extension.write_text("no", encoding="utf-8")
            try:
                inspect_dbn_file_info(invalid_extension)
                _record(results, False, "Invalid extension fails")
            except DBNInspectionError:
                _record(results, True, "Invalid extension fails")

            invalid_job = root / "invalid-json"
            invalid_job.mkdir()
            invalid_dbn = invalid_job / "xnas-itch-20260513.trades.dbn"
            invalid_dbn.write_bytes(b"dummy")
            (invalid_job / "metadata.json").write_text("{invalid", encoding="utf-8")
            try:
                inspect_dbn_file_info(invalid_dbn)
                _record(results, False, "Invalid JSON fails clearly")
            except DBNInspectionError:
                _record(results, True, "Invalid JSON fails clearly")

            try:
                inspect_dbn_file(dbn_path, file_info_only=False)
                if databento_available():
                    _record(results, False, "Dummy DBN preview fails cleanly when Databento is installed")
                else:
                    _record(results, False, "Missing Databento preview dependency fails clearly")
            except DatabentoDependencyError as exc:
                _record(
                    results,
                    "Databento package is required" in str(exc),
                    "Missing Databento preview dependency fails clearly",
                )
            except DBNInspectionError as exc:
                _record(
                    results,
                    databento_available() and "Failed to parse DBN file" in str(exc),
                    "Dummy DBN preview fails cleanly when Databento is installed",
                )
    except OSError as exc:
        _record(results, False, f"DBN inspector validation setup failed: {exc}")

    failures = [message for ok, message in results if not ok]
    print()
    if failures:
        print(f"DBN inspector validation FAILED with {len(failures)} failure(s).")
        return 1

    print("DBN inspector validation PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
