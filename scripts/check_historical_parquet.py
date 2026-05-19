"""Validate the local historical Parquet reader with generated fake data."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow as pa
import pyarrow.parquet as pq


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.common.serialization import (  # noqa: E402
    from_json_string,
    to_json_string,
)
from market_relay_engine.market_data.historical_parquet import (  # noqa: E402
    HistoricalParquetError,
    read_market_records_from_parquet,
)


def _record(results: list[tuple[bool, str]], ok: bool, message: str) -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {message}")
    results.append((ok, message))


def _write_fake_parquet(path: Path) -> None:
    table = pa.table(
        {
            "event_time": pa.array(
                [1_779_116_400_000_000_000, 1_779_116_401_000_000_000],
                type=pa.int64(),
            ),
            "ticker": pa.array(["XOM", "LMT"], type=pa.string()),
            "record_type": pa.array(["trade", "quote"], type=pa.string()),
            "price": pa.array([118.42, None], type=pa.float64()),
            "size": pa.array([100.0, None], type=pa.float64()),
            "bid_price": pa.array([None, 472.15], type=pa.float64()),
            "ask_price": pa.array([None, 472.55], type=pa.float64()),
        }
    )
    pq.write_table(table, path)


def main() -> int:
    results: list[tuple[bool, str]] = []

    try:
        with TemporaryDirectory() as temp_dir:
            parquet_path = Path(temp_dir) / "fake_market_records.parquet"
            _write_fake_parquet(parquet_path)

            records = read_market_records_from_parquet(parquet_path)
            serialized = [from_json_string(to_json_string(record)) for record in records]

            _record(results, len(records) == 2, "Generated Parquet records read")
            _record(
                results,
                all(record.local_receive_time is None for record in records),
                "Historical records do not fake local_receive_time",
            )
            _record(
                results,
                all(item["event_time"].endswith("Z") for item in serialized),
                "Records serialize with UTC timestamp strings",
            )
            _record(
                results,
                records[1].midprice is None and records[1].spread is None,
                "Reader does not compute midprice or spread",
            )
    except (HistoricalParquetError, OSError) as exc:
        _record(results, False, f"Historical Parquet validation failed: {exc}")

    failures = [message for ok, message in results if not ok]
    print()
    if failures:
        print(f"Historical Parquet validation FAILED with {len(failures)} failure(s).")
        return 1

    print("Historical Parquet validation PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
