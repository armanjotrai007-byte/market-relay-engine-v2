"""Inspect a local Databento DBN file or batch folder safely."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.market_data.dbn_inspector import (  # noqa: E402
    DBNInspectionError,
    DatabentoDependencyError,
    check_dbn_path,
    format_dbn_inspection_result,
    inspect_dbn_file,
    inspect_dbn_file_info,
    inspect_dbn_folder,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect local Databento DBN files or batch folders."
    )
    parser.add_argument("--path", required=True, help="Path to a .dbn/.dbn.zst file or folder.")
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum number of DBN records to preview when not in file-info-only mode.",
    )
    parser.add_argument(
        "--file-info-only",
        action="store_true",
        help="Inspect file/folder metadata without importing Databento or reading DBN records.",
    )
    parser.add_argument(
        "--recursive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Recursively inspect directories. Use --no-recursive for top-level only.",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=20,
        help="Maximum number of DBN files to display for folder summaries.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        dbn_path = check_dbn_path(args.path)
        if dbn_path.is_dir():
            folder_info = inspect_dbn_folder(dbn_path, recursive=args.recursive)
            print(format_dbn_inspection_result(folder_info, max_files=args.max_files))
            if not args.file_info_only and folder_info.files:
                preview_result = inspect_dbn_file(
                    folder_info.files[0].path,
                    limit=args.limit,
                    file_info_only=False,
                )
                print()
                print("Previewing first DBN file only.")
                print(format_dbn_inspection_result(preview_result, max_files=args.max_files))
            return 0

        result = (
            inspect_dbn_file_info(dbn_path)
            if args.file_info_only
            else inspect_dbn_file(dbn_path, limit=args.limit, file_info_only=False)
        )
        print(format_dbn_inspection_result(result, max_files=args.max_files))
        return 0
    except (DBNInspectionError, DatabentoDependencyError) as exc:
        print(f"DBN inspection FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
