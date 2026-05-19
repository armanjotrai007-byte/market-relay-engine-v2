"""Inspect a local historical Parquet file through the MarketRecord reader."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pyarrow.parquet as pq


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.common.serialization import to_json_dict  # noqa: E402
from market_relay_engine.market_data.historical_parquet import (  # noqa: E402
    HistoricalParquetError,
    read_market_records_from_parquet,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect a local historical market-data Parquet file."
    )
    parser.add_argument("--path", required=True, help="Path to a local Parquet file.")
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum number of normalized records to print.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    parquet_path = Path(args.path)

    try:
        parquet_file = pq.ParquetFile(parquet_path)
        records = read_market_records_from_parquet(parquet_path, limit=args.limit)
    except (HistoricalParquetError, OSError) as exc:
        print(f"Historical Parquet inspection FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"path: {parquet_path}")
    print(f"row_count: {parquet_file.metadata.num_rows}")
    print(f"columns: {parquet_file.schema_arrow.names}")
    print("schema:")
    print(parquet_file.schema_arrow)
    print(f"normalized_record_count: {len(records)}")
    print("records:")
    print(json.dumps([to_json_dict(record) for record in records], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
