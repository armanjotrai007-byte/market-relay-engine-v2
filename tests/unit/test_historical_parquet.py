from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from market_relay_engine.common.serialization import from_json_string, to_json_string
from market_relay_engine.market_data.historical_parquet import (
    HistoricalParquetError,
    NANOSECONDS_PER_SECOND,
    ParquetColumnMapping,
    default_market_column_mapping,
    iter_market_records_from_parquet,
    normalize_parquet_timestamp,
    read_market_records_from_parquet,
    read_parquet_table,
    validate_parquet_columns,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
EXAMPLE_TIME = datetime(2026, 5, 18, 13, 30, 0, tzinfo=UTC)
SECOND_EXAMPLE_TIME = datetime(2026, 5, 18, 13, 30, 1, tzinfo=UTC)


def _timestamp_ns(value: datetime) -> int:
    delta = value - EPOCH
    return (
        delta.days * 86_400 * NANOSECONDS_PER_SECOND
        + delta.seconds * NANOSECONDS_PER_SECOND
        + delta.microseconds * 1_000
    )


def _write_table(path: Path, table: pa.Table) -> Path:
    pq.write_table(table, path)
    return path


def test_historical_parquet_module_imports_cleanly() -> None:
    assert importlib.import_module("market_relay_engine.market_data.historical_parquet")


def test_missing_file_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(HistoricalParquetError, match="does not exist"):
        read_market_records_from_parquet(tmp_path / "missing.parquet")


def test_directory_path_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(HistoricalParquetError, match="not a file"):
        read_market_records_from_parquet(tmp_path)


def test_generic_pyarrow_timestamp_column_loads(tmp_path: Path) -> None:
    path = _write_table(
        tmp_path / "generic.parquet",
        pa.table(
            {
                "event_time": pa.array(
                    [EXAMPLE_TIME, SECOND_EXAMPLE_TIME],
                    type=pa.timestamp("ns", tz="UTC"),
                ),
                "ticker": pa.array(["XOM", "LMT"], type=pa.string()),
                "record_type": pa.array(["trade", "quote"], type=pa.string()),
                "price": pa.array([118.42, None], type=pa.float64()),
                "size": pa.array([100.0, None], type=pa.float64()),
                "bid_price": pa.array([None, 472.15], type=pa.float64()),
                "ask_price": pa.array([None, 472.55], type=pa.float64()),
            }
        ),
    )

    records = read_market_records_from_parquet(path)

    assert [record.ticker for record in records] == ["XOM", "LMT"]
    assert records[0].event_time == EXAMPLE_TIME
    assert records[0].price == 118.42
    assert records[0].size == 100.0
    assert records[1].bid_price == 472.15
    assert records[1].ask_price == 472.55
    assert records[1].midprice is None
    assert records[1].spread is None
    assert records[0].local_receive_time is None


def test_databento_like_fake_mapping_uses_integer_nanoseconds(tmp_path: Path) -> None:
    # These fake Parquet files test reader mechanics and common timestamp shapes
    # only. They are not official Databento schema fixtures. Real Databento
    # schema mapping will be validated later using ignored local samples.
    path = _write_table(
        tmp_path / "databento_like.parquet",
        pa.table(
            {
                "ts_event": pa.array([_timestamp_ns(EXAMPLE_TIME)], type=pa.int64()),
                "symbol": pa.array(["LMT"], type=pa.string()),
                "record_type": pa.array(["quote"], type=pa.string()),
                "bid_price": pa.array([472.15], type=pa.float64()),
                "ask_price": pa.array([472.55], type=pa.float64()),
            }
        ),
    )
    mapping = ParquetColumnMapping(
        event_time="ts_event",
        ticker="symbol",
        record_type="record_type",
        raw_symbol="symbol",
        bid_price="bid_price",
        ask_price="ask_price",
        source_event_time="ts_event",
    )

    records = read_market_records_from_parquet(
        path,
        mapping=mapping,
        source="fake_databento_like_parquet",
    )

    assert len(records) == 1
    assert records[0].event_time == EXAMPLE_TIME
    assert records[0].source_event_time == EXAMPLE_TIME
    assert records[0].source == "fake_databento_like_parquet"
    assert records[0].ticker == "LMT"
    assert records[0].raw_symbol == "LMT"
    assert records[0].local_receive_time is None
    assert records[0].midprice is None
    assert records[0].spread is None


def test_normalize_parquet_timestamp_accepts_supported_shapes() -> None:
    assert normalize_parquet_timestamp(EXAMPLE_TIME, "event_time") == EXAMPLE_TIME
    assert (
        normalize_parquet_timestamp("2026-05-18T13:30:00Z", "event_time")
        == EXAMPLE_TIME
    )
    assert normalize_parquet_timestamp(_timestamp_ns(EXAMPLE_TIME), "event_time") == EXAMPLE_TIME


def test_normalize_parquet_timestamp_rejects_naive_datetime() -> None:
    with pytest.raises(HistoricalParquetError, match="timezone-aware"):
        normalize_parquet_timestamp(datetime(2026, 5, 18, 13, 30), "event_time")


def test_normalize_parquet_timestamp_rejects_unsupported_type() -> None:
    with pytest.raises(HistoricalParquetError, match="unsupported timestamp type"):
        normalize_parquet_timestamp(1.25, "event_time")


def test_utc_iso_string_timestamp_column_loads(tmp_path: Path) -> None:
    path = _write_table(
        tmp_path / "iso.parquet",
        pa.table(
            {
                "event_time": pa.array(["2026-05-18T13:30:00Z"], type=pa.string()),
                "ticker": pa.array(["XOM"], type=pa.string()),
                "record_type": pa.array(["trade"], type=pa.string()),
            }
        ),
    )

    records = read_market_records_from_parquet(path)

    assert records[0].event_time == EXAMPLE_TIME


def test_naive_timestamp_column_rejected(tmp_path: Path) -> None:
    path = _write_table(
        tmp_path / "naive.parquet",
        pa.table(
            {
                "event_time": pa.array(
                    [datetime(2026, 5, 18, 13, 30, 0)],
                    type=pa.timestamp("us"),
                ),
                "ticker": pa.array(["XOM"], type=pa.string()),
                "record_type": pa.array(["trade"], type=pa.string()),
            }
        ),
    )

    with pytest.raises(HistoricalParquetError, match="timezone-aware"):
        read_market_records_from_parquet(path)


def test_missing_required_columns_return_and_raise_clearly(tmp_path: Path) -> None:
    path = _write_table(
        tmp_path / "missing_required.parquet",
        pa.table(
            {
                "event_time": pa.array([EXAMPLE_TIME], type=pa.timestamp("ns", tz="UTC")),
                "ticker": pa.array(["XOM"], type=pa.string()),
            }
        ),
    )
    table = read_parquet_table(path)

    assert validate_parquet_columns(table.schema, default_market_column_mapping()) == [
        "record_type"
    ]
    with pytest.raises(HistoricalParquetError, match="record_type"):
        read_market_records_from_parquet(path)


def test_optional_columns_can_be_absent(tmp_path: Path) -> None:
    path = _write_table(
        tmp_path / "required_only.parquet",
        pa.table(
            {
                "event_time": pa.array([EXAMPLE_TIME], type=pa.timestamp("ns", tz="UTC")),
                "ticker": pa.array(["XOM"], type=pa.string()),
                "record_type": pa.array(["trade"], type=pa.string()),
            }
        ),
    )

    record = read_market_records_from_parquet(path)[0]

    assert record.raw_symbol is None
    assert record.price is None
    assert record.bid_price is None
    assert record.ask_price is None
    assert record.midprice is None
    assert record.spread is None
    assert record.source_event_time is None
    assert record.local_receive_time is None


def test_non_finite_optional_numeric_values_normalize_to_none(tmp_path: Path) -> None:
    path = _write_table(
        tmp_path / "non_finite.parquet",
        pa.table(
            {
                "event_time": pa.array([EXAMPLE_TIME], type=pa.timestamp("ns", tz="UTC")),
                "ticker": pa.array(["XOM"], type=pa.string()),
                "record_type": pa.array(["quote"], type=pa.string()),
                "price": pa.array([float("nan")], type=pa.float64()),
                "bid_price": pa.array([float("inf")], type=pa.float64()),
                "ask_price": pa.array([float("-inf")], type=pa.float64()),
            }
        ),
    )

    record = read_market_records_from_parquet(path)[0]
    parsed = from_json_string(to_json_string(record))

    assert record.price is None
    assert record.bid_price is None
    assert record.ask_price is None
    assert parsed["price"] is None
    assert parsed["bid_price"] is None
    assert parsed["ask_price"] is None


def test_bool_optional_numeric_value_rejected(tmp_path: Path) -> None:
    path = _write_table(
        tmp_path / "bool_numeric.parquet",
        pa.table(
            {
                "event_time": pa.array([EXAMPLE_TIME], type=pa.timestamp("ns", tz="UTC")),
                "ticker": pa.array(["XOM"], type=pa.string()),
                "record_type": pa.array(["trade"], type=pa.string()),
                "price": pa.array([True], type=pa.bool_()),
            }
        ),
    )

    with pytest.raises(HistoricalParquetError, match="price must be numeric"):
        read_market_records_from_parquet(path)


def test_limit_zero_one_partial_and_full_reads(tmp_path: Path) -> None:
    path = _write_table(
        tmp_path / "limits.parquet",
        pa.table(
            {
                "event_time": pa.array(
                    [
                        "2026-05-18T13:30:00Z",
                        "not-a-timestamp",
                        "2026-05-18T13:30:02Z",
                    ],
                    type=pa.string(),
                ),
                "ticker": pa.array(["XOM", "LMT", "CVX"], type=pa.string()),
                "record_type": pa.array(["trade", "quote", "trade"], type=pa.string()),
            }
        ),
    )

    assert read_market_records_from_parquet(path, limit=0) == []
    assert [record.ticker for record in read_market_records_from_parquet(path, limit=1)] == [
        "XOM"
    ]
    assert [record.ticker for record in iter_market_records_from_parquet(path, limit=1)] == [
        "XOM"
    ]
    with pytest.raises(HistoricalParquetError, match="UTC ISO"):
        read_market_records_from_parquet(path)


def test_full_read_loads_all_valid_records(tmp_path: Path) -> None:
    path = _write_table(
        tmp_path / "full.parquet",
        pa.table(
            {
                "event_time": pa.array(
                    [EXAMPLE_TIME, SECOND_EXAMPLE_TIME],
                    type=pa.timestamp("ns", tz="UTC"),
                ),
                "ticker": pa.array(["XOM", "CVX"], type=pa.string()),
                "record_type": pa.array(["trade", "trade"], type=pa.string()),
            }
        ),
    )

    assert [record.ticker for record in read_market_records_from_parquet(path)] == [
        "XOM",
        "CVX",
    ]


def test_records_serialize_through_pr3_helpers(tmp_path: Path) -> None:
    path = _write_table(
        tmp_path / "serialize.parquet",
        pa.table(
            {
                "event_time": pa.array([_timestamp_ns(EXAMPLE_TIME)], type=pa.int64()),
                "ticker": pa.array(["XOM"], type=pa.string()),
                "record_type": pa.array(["trade"], type=pa.string()),
            }
        ),
    )

    parsed = from_json_string(to_json_string(read_market_records_from_parquet(path)[0]))

    assert parsed["event_time"] == "2026-05-18T13:30:00Z"
    assert parsed["ticker"] == "XOM"
    assert parsed["local_receive_time"] is None


def test_reader_docstrings_warn_about_memory_and_limit_behavior() -> None:
    assert "small files, tests, and inspection only" in (read_parquet_table.__doc__ or "")
    assert "does not guarantee that only limit rows are read" in (
        iter_market_records_from_parquet.__doc__ or ""
    )


def test_check_historical_parquet_uses_temporary_directory() -> None:
    script_text = (REPO_ROOT / "scripts" / "check_historical_parquet.py").read_text(
        encoding="utf-8"
    )

    assert "TemporaryDirectory" in script_text


def test_no_real_parquet_or_dbn_files_are_committed() -> None:
    forbidden_suffixes = {".parquet", ".dbn"}
    ignored_dirs = {".venv", ".pytest_cache", "__pycache__"}
    forbidden_files = []

    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in ignored_dirs for part in path.parts):
            continue
        if path.suffix.lower() in forbidden_suffixes or path.name.endswith(".dbn.zst"):
            forbidden_files.append(path.relative_to(REPO_ROOT).as_posix())

    assert forbidden_files == []
