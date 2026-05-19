"""Local historical Parquet reader for market records.

This module is a small boundary for local historical market-data Parquet files.
It does not call Databento APIs, decode DBN, write QuestDB records, or build
features. Midprice and spread are not computed by this reader. The canonical
feature builder is responsible for deriving these from bid_price and ask_price.

``iter_market_records_from_parquet`` is the intended production path for the
canonical feature builder and any code processing large files.
``read_market_records_from_parquet`` loads all records into a list and is
intended for small test files and inspection only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import math
from pathlib import Path
from typing import Any, Iterator

import pyarrow as pa
import pyarrow.parquet as pq

from market_relay_engine.common.time import ensure_timezone_aware_utc, parse_utc_iso
from market_relay_engine.contracts.market import MarketRecord


DEFAULT_BATCH_SIZE = 65_536
NANOSECONDS_PER_SECOND = 1_000_000_000


class HistoricalParquetError(ValueError):
    """Raised when local historical Parquet records cannot be read safely."""


@dataclass(frozen=True, kw_only=True)
class ParquetColumnMapping:
    """Map source Parquet columns into the generic ``MarketRecord`` fields."""

    event_time: str = "event_time"
    ticker: str = "ticker"
    record_type: str = "record_type"
    raw_symbol: str | None = "raw_symbol"
    price: str | None = "price"
    size: str | None = "size"
    bid_price: str | None = "bid_price"
    ask_price: str | None = "ask_price"
    bid_size: str | None = "bid_size"
    ask_size: str | None = "ask_size"
    spread: str | None = "spread"
    midprice: str | None = "midprice"
    source_event_time: str | None = "source_event_time"
    local_receive_time: str | None = None


def default_market_column_mapping() -> ParquetColumnMapping:
    """Return the generic fake/test mapping for local Parquet files.

    This mapping is not an exact Databento production schema mapping. Real
    Databento schema validation will happen later with ignored local samples.
    """
    return ParquetColumnMapping()


def _resolve_file_path(path: str | Path) -> Path:
    parquet_path = Path(path)
    if not parquet_path.exists():
        raise HistoricalParquetError(f"Parquet file does not exist: {parquet_path}")
    if not parquet_path.is_file():
        raise HistoricalParquetError(f"Parquet path is not a file: {parquet_path}")
    return parquet_path


def _arrow_to_python(value: Any) -> Any:
    if hasattr(value, "as_py") and callable(value.as_py):
        return value.as_py()
    return value


def normalize_parquet_timestamp(value: Any, field_name: str) -> datetime:
    """Normalize one Parquet timestamp-like value to timezone-aware UTC.

    Integer values are treated as nanoseconds since the Unix epoch. PR 5 does
    not infer seconds, milliseconds, or microseconds from numeric magnitude.
    """
    value = _arrow_to_python(value)
    if isinstance(value, datetime):
        try:
            return ensure_timezone_aware_utc(value)
        except (TypeError, ValueError) as exc:
            raise HistoricalParquetError(
                f"{field_name} must be timezone-aware UTC-compatible"
            ) from exc
    if isinstance(value, str):
        try:
            return parse_utc_iso(value)
        except ValueError as exc:
            raise HistoricalParquetError(f"{field_name} is not a valid UTC ISO timestamp") from exc
    if isinstance(value, int) and not isinstance(value, bool):
        seconds, nanoseconds = divmod(value, NANOSECONDS_PER_SECOND)
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
            seconds=seconds,
            microseconds=nanoseconds // 1_000,
        )
    raise HistoricalParquetError(
        f"{field_name} has unsupported timestamp type: {type(value).__name__}"
    )


def read_parquet_table(path: str | Path) -> pa.Table:
    """Read a full Parquet file into memory as a PyArrow table.

    This helper is for small files, tests, and inspection only. For large
    historical files, use ``iter_market_records_from_parquet()`` with batching
    and/or ``limit``.
    """
    parquet_path = _resolve_file_path(path)
    try:
        return pq.read_table(parquet_path)
    except Exception as exc:  # noqa: BLE001 - boundary should wrap PyArrow failures.
        raise HistoricalParquetError(f"Failed to read Parquet file: {parquet_path}") from exc


def validate_parquet_columns(
    schema: pa.Schema,
    mapping: ParquetColumnMapping,
) -> list[str]:
    """Return required source columns that are missing from ``schema``."""
    column_names = set(schema.names)
    required_columns = (mapping.event_time, mapping.ticker, mapping.record_type)
    return [column for column in required_columns if column not in column_names]


def _column_value(row: dict[str, Any], column_name: str | None) -> Any:
    if column_name is None:
        return None
    return _arrow_to_python(row.get(column_name))


def _required_string(row: dict[str, Any], column_name: str, field_name: str) -> str:
    value = _column_value(row, column_name)
    if value is None:
        raise HistoricalParquetError(f"{field_name} is missing")
    text = str(value).strip()
    if not text:
        raise HistoricalParquetError(f"{field_name} must be a non-empty string")
    return text


def _optional_string(row: dict[str, Any], column_name: str | None) -> str | None:
    value = _column_value(row, column_name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(
    row: dict[str, Any],
    column_name: str | None,
    field_name: str,
) -> float | None:
    value = _column_value(row, column_name)
    if value is None:
        return None
    if isinstance(value, bool):
        raise HistoricalParquetError(f"{field_name} must be numeric")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise HistoricalParquetError(f"{field_name} must be numeric") from exc
    if not math.isfinite(numeric):
        return None
    return numeric


def _optional_timestamp(
    row: dict[str, Any],
    column_name: str | None,
    field_name: str,
) -> datetime | None:
    value = _column_value(row, column_name)
    if value is None:
        return None
    return normalize_parquet_timestamp(value, field_name)


def _market_record_from_row(
    row: dict[str, Any],
    mapping: ParquetColumnMapping,
    source: str,
) -> MarketRecord:
    event_time_value = _column_value(row, mapping.event_time)
    if event_time_value is None:
        raise HistoricalParquetError("event_time is missing")

    return MarketRecord(
        event_time=normalize_parquet_timestamp(event_time_value, "event_time"),
        ticker=_required_string(row, mapping.ticker, "ticker"),
        source=source,
        record_type=_required_string(row, mapping.record_type, "record_type"),
        raw_symbol=_optional_string(row, mapping.raw_symbol),
        price=_optional_float(row, mapping.price, "price"),
        size=_optional_float(row, mapping.size, "size"),
        bid_price=_optional_float(row, mapping.bid_price, "bid_price"),
        ask_price=_optional_float(row, mapping.ask_price, "ask_price"),
        bid_size=_optional_float(row, mapping.bid_size, "bid_size"),
        ask_size=_optional_float(row, mapping.ask_size, "ask_size"),
        spread=_optional_float(row, mapping.spread, "spread"),
        midprice=_optional_float(row, mapping.midprice, "midprice"),
        source_event_time=_optional_timestamp(
            row,
            mapping.source_event_time,
            "source_event_time",
        ),
        local_receive_time=_optional_timestamp(
            row,
            mapping.local_receive_time,
            "local_receive_time",
        ),
    )


def iter_market_records_from_parquet(
    path: str | Path,
    mapping: ParquetColumnMapping | None = None,
    source: str = "historical_parquet",
    limit: int | None = None,
) -> Iterator[MarketRecord]:
    """Yield ``MarketRecord`` objects from a local Parquet file.

    This is the intended path for large historical files and future feature
    builder integration. The limit parameter controls the number of records
    yielded. It does not guarantee that only limit rows are read from disk, as
    PyArrow reads in batches.
    """
    if limit is not None and limit < 0:
        raise HistoricalParquetError("limit must be greater than or equal to 0")
    if limit == 0:
        return

    resolved_mapping = mapping or default_market_column_mapping()
    parquet_path = _resolve_file_path(path)

    try:
        parquet_file = pq.ParquetFile(parquet_path)
    except Exception as exc:  # noqa: BLE001 - boundary should wrap PyArrow failures.
        raise HistoricalParquetError(f"Failed to open Parquet file: {parquet_path}") from exc

    missing_columns = validate_parquet_columns(parquet_file.schema_arrow, resolved_mapping)
    if missing_columns:
        raise HistoricalParquetError(
            "Missing required Parquet column(s): " + ", ".join(missing_columns)
        )

    records_yielded = 0
    batch_size = limit if limit is not None else DEFAULT_BATCH_SIZE
    try:
        for batch in parquet_file.iter_batches(batch_size=batch_size):
            for row in batch.to_pylist():
                if limit is not None and records_yielded >= limit:
                    return
                try:
                    yield _market_record_from_row(row, resolved_mapping, source)
                except HistoricalParquetError as exc:
                    raise HistoricalParquetError(
                        f"Failed to normalize Parquet row {records_yielded + 1}: {exc}"
                    ) from exc
                records_yielded += 1
    except HistoricalParquetError:
        raise
    except Exception as exc:  # noqa: BLE001 - boundary should wrap PyArrow failures.
        raise HistoricalParquetError(f"Failed to iterate Parquet file: {parquet_path}") from exc


def read_market_records_from_parquet(
    path: str | Path,
    mapping: ParquetColumnMapping | None = None,
    source: str = "historical_parquet",
    limit: int | None = None,
) -> list[MarketRecord]:
    """Read local Parquet records into a list for small tests and inspection."""
    return list(
        iter_market_records_from_parquet(
            path=path,
            mapping=mapping,
            source=source,
            limit=limit,
        )
    )
