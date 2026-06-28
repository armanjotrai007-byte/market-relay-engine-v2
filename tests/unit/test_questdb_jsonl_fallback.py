from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

import pytest

from market_relay_engine.contracts.context import ContextIndicatorSnapshot
from market_relay_engine.questdb.jsonl_fallback import (
    FALLBACK_SCHEMA_VERSION,
    EmergencyJSONLLedgerFallback,
    EmergencyLedgerFallbackConfig,
    EmergencyLedgerFallbackError,
    FsyncingJSONLFileWriter,
    load_emergency_ledger_fallback_config,
)


EVENT_TIME = datetime(2026, 6, 20, 16, 0, tzinfo=UTC)
WRITTEN_AT = datetime(2026, 6, 21, 1, 2, 3, tzinfo=UTC)


def _fallback(
    directory: Path,
    *,
    written_at: datetime = WRITTEN_AT,
    file_writer: Any | None = None,
) -> EmergencyJSONLLedgerFallback:
    return EmergencyJSONLLedgerFallback(
        EmergencyLedgerFallbackConfig(enabled=True, directory=directory),
        clock=lambda: written_at,
        file_writer=file_writer,
    )


def _append_sample(fallback: EmergencyJSONLLedgerFallback, *, record_id: str = "record_1") -> None:
    fallback.append_record(
        record_type="context_indicator_snapshot",
        target_table="context_indicator_snapshots",
        record_id=record_id,
        event_time=EVENT_TIME,
        source="unit_test",
        ticker_or_sector="TST",
        primary_write_failure={
            "failure_code": "QUESTDB_CONTEXT_INDICATOR_WRITE_FAILED",
            "failure_type": "RuntimeError",
        },
        payload={"value": {"nested": True}},
    )


def test_jsonl_fallback_writes_exactly_one_object_plus_newline(tmp_path: Path) -> None:
    fallback = _fallback(tmp_path / "emergency")

    _append_sample(fallback)

    path = tmp_path / "emergency" / "20260621.jsonl"
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1
    row = json.loads(raw.decode("utf-8"))
    assert row["fallback_schema_version"] == FALLBACK_SCHEMA_VERSION
    assert row["record_id"] == "record_1"
    assert row["payload"] == {"value": {"nested": True}}


def test_jsonl_fallback_creates_configured_directory_when_absent(tmp_path: Path) -> None:
    directory = tmp_path / "missing" / "emergency"
    assert not directory.exists()

    _append_sample(_fallback(directory))

    assert (directory / "20260621.jsonl").is_file()


def test_jsonl_fallback_preserves_envelope_and_complete_payload(tmp_path: Path) -> None:
    snapshot = ContextIndicatorSnapshot(
        snapshot_time=EVENT_TIME,
        source="usaspending_awards_v1",
        ticker_or_sector="TST",
        indicator_name="award",
        value="NEW_AWARD_DISCOVERED",
        context_indicator_id="context_indicator_1",
        source_event_time=EVENT_TIME - timedelta(days=1),
        details={"canonical_award_id": "CONT_AWD_1", "amount": 100.0},
    )
    fallback = _fallback(tmp_path / "emergency")

    result = fallback.append_record(
        record_type="context_indicator_snapshot",
        target_table="context_indicator_snapshots",
        record_id=snapshot.context_indicator_id,
        event_time=snapshot.snapshot_time,
        source=snapshot.source,
        ticker_or_sector=snapshot.ticker_or_sector,
        primary_write_failure={
            "failure_code": "QUESTDB_CONTEXT_INDICATOR_WRITE_FAILED",
            "failure_type": "RuntimeError",
        },
        payload={
            "context_indicator_snapshot": snapshot,
            "write_request": {"run_id": "run_1", "session_id": "session_1"},
        },
    )

    row = json.loads(result.path.read_text(encoding="utf-8"))
    assert row["fallback_written_at"] == "2026-06-21T01:02:03Z"
    assert row["record_type"] == "context_indicator_snapshot"
    assert row["target_table"] == "context_indicator_snapshots"
    assert row["event_time"] == "2026-06-20T16:00:00Z"
    assert row["source"] == "usaspending_awards_v1"
    assert row["ticker_or_sector"] == "TST"
    assert row["primary_write_failure"]["failure_code"] == (
        "QUESTDB_CONTEXT_INDICATOR_WRITE_FAILED"
    )
    payload = row["payload"]["context_indicator_snapshot"]
    assert payload["context_indicator_id"] == "context_indicator_1"
    assert payload["details"]["canonical_award_id"] == "CONT_AWD_1"
    assert row["payload"]["write_request"]["run_id"] == "run_1"


def test_jsonl_fallback_filename_uses_utc_fallback_write_date(tmp_path: Path) -> None:
    local_time = datetime(2026, 6, 21, 0, 30, tzinfo=timezone(timedelta(hours=1)))

    _append_sample(_fallback(tmp_path / "emergency", written_at=local_time))

    assert (tmp_path / "emergency" / "20260620.jsonl").is_file()


def test_jsonl_fallback_appends_without_overwriting_prior_rows(tmp_path: Path) -> None:
    fallback = _fallback(tmp_path / "emergency")

    _append_sample(fallback, record_id="record_1")
    _append_sample(fallback, record_id="record_2")

    path = tmp_path / "emergency" / "20260621.jsonl"
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["record_id"] for row in rows] == ["record_1", "record_2"]


def test_fsyncing_file_writer_flushes_and_fsyncs_before_success(tmp_path: Path) -> None:
    events: list[str] = []

    class RecordingHandle:
        def __enter__(self) -> "RecordingHandle":
            return self

        def __exit__(self, *args: object) -> None:
            events.append("close")

        def write(self, payload: bytes) -> int:
            events.append(f"write:{payload.decode('utf-8')}")
            return len(payload)

        def flush(self) -> None:
            events.append("flush")

        def fileno(self) -> int:
            events.append("fileno")
            return 7

    writer = FsyncingJSONLFileWriter(
        open_file=lambda path: RecordingHandle(),
        fsync=lambda fd: events.append(f"fsync:{fd}"),
    )

    assert writer.append(tmp_path / "emergency" / "20260621.jsonl", b"{}\n") == 3
    assert events == ["write:{}\n", "flush", "fileno", "fsync:7", "close"]


def test_jsonl_fallback_write_exception_is_surfaced_without_success(
    tmp_path: Path,
) -> None:
    class FailingWriter:
        def append(self, path: Path, payload: bytes) -> int:
            raise OSError("disk full")

    fallback = _fallback(tmp_path / "emergency", file_writer=FailingWriter())

    with pytest.raises(EmergencyLedgerFallbackError, match="disk full|OSError"):
        _append_sample(fallback)
    assert not (tmp_path / "emergency" / "20260621.jsonl").exists()


def test_load_jsonl_fallback_config_uses_questdb_section(tmp_path: Path) -> None:
    config_path = tmp_path / "questdb.yaml"
    config_path.write_text(
        """
jsonl_fallback:
  enabled: true
  directory: data/emergency_ledger
""",
        encoding="utf-8",
    )

    config = load_emergency_ledger_fallback_config(config_path, base_dir=tmp_path)

    assert config.enabled is True
    assert config.directory == tmp_path / "data" / "emergency_ledger"
