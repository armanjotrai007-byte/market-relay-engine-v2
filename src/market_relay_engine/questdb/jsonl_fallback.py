"""Append-only emergency JSONL fallback for failed QuestDB ledger writes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, BinaryIO, Protocol

import yaml

from market_relay_engine.common.config import repo_root
from market_relay_engine.common.serialization import to_json_dict
from market_relay_engine.common.time import ensure_timezone_aware_utc, to_utc_iso, utc_now


FALLBACK_SCHEMA_VERSION = "questdb_emergency_jsonl_fallback_v1"


class EmergencyLedgerFallbackError(RuntimeError):
    """Raised when the emergency JSONL fallback cannot safely preserve a row."""


@dataclass(frozen=True, kw_only=True)
class EmergencyLedgerFallbackConfig:
    enabled: bool
    directory: Path

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise EmergencyLedgerFallbackError("enabled must be bool")
        if not isinstance(self.directory, Path):
            raise EmergencyLedgerFallbackError("directory must be a Path")


@dataclass(frozen=True, kw_only=True)
class EmergencyLedgerFallbackResult:
    path: Path
    record_id: str
    bytes_written: int
    written_at: datetime


class JSONLFileWriter(Protocol):
    def append(self, path: Path, payload: bytes) -> int:
        ...


@dataclass(frozen=True, kw_only=True)
class FsyncingJSONLFileWriter:
    open_file: Callable[[Path], Any] | None = None
    fsync: Callable[[int], None] = os.fsync

    def append(self, path: Path, payload: bytes) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        opener = self.open_file or _open_append_binary
        try:
            with opener(path) as handle:
                _write_all(handle, payload)
                handle.flush()
                self.fsync(handle.fileno())
        except EmergencyLedgerFallbackError:
            raise
        except Exception as exc:  # noqa: BLE001 - filesystem boundary.
            raise EmergencyLedgerFallbackError(
                f"emergency JSONL fallback write failed: {type(exc).__name__}"
            ) from exc
        return len(payload)


class EmergencyJSONLLedgerFallback:
    def __init__(
        self,
        config: EmergencyLedgerFallbackConfig | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
        file_writer: JSONLFileWriter | None = None,
    ) -> None:
        self.config = config or load_emergency_ledger_fallback_config()
        self.clock = clock
        self.file_writer = file_writer or FsyncingJSONLFileWriter()

    def append_record(
        self,
        *,
        record_type: str,
        target_table: str,
        record_id: str,
        event_time: datetime,
        source: str,
        ticker_or_sector: str,
        primary_write_failure: Mapping[str, object],
        payload: Mapping[str, object],
    ) -> EmergencyLedgerFallbackResult:
        if not self.config.enabled:
            raise EmergencyLedgerFallbackError("emergency JSONL fallback is disabled")

        written_at = ensure_timezone_aware_utc(self.clock())
        envelope = {
            "fallback_schema_version": FALLBACK_SCHEMA_VERSION,
            "fallback_written_at": to_utc_iso(written_at),
            "record_type": _required_string(record_type, "record_type"),
            "target_table": _required_string(target_table, "target_table"),
            "record_id": _required_string(record_id, "record_id"),
            "event_time": to_utc_iso(event_time),
            "source": _required_string(source, "source"),
            "ticker_or_sector": _required_string(ticker_or_sector, "ticker_or_sector"),
            "primary_write_failure": dict(primary_write_failure),
            "payload": dict(payload),
        }
        line = (
            json.dumps(
                to_json_dict(envelope),
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        path = self.config.directory / f"{written_at:%Y%m%d}.jsonl"
        try:
            bytes_written = self.file_writer.append(path, line)
        except EmergencyLedgerFallbackError:
            raise
        except Exception as exc:  # noqa: BLE001 - injected filesystem boundary.
            raise EmergencyLedgerFallbackError(
                f"emergency JSONL fallback write failed: {type(exc).__name__}"
            ) from exc
        if bytes_written != len(line):
            raise EmergencyLedgerFallbackError("emergency JSONL fallback write was incomplete")
        return EmergencyLedgerFallbackResult(
            path=path,
            record_id=str(envelope["record_id"]),
            bytes_written=bytes_written,
            written_at=written_at,
        )


def load_emergency_ledger_fallback_config(
    config_path: str | Path | None = None,
    *,
    enabled: bool | None = None,
    directory: str | Path | None = None,
    base_dir: str | Path | None = None,
) -> EmergencyLedgerFallbackConfig:
    root = Path(base_dir) if base_dir is not None else repo_root()
    values: dict[str, object] = {"enabled": False}
    values.update(_load_yaml_fallback_values(_resolve_config_path(config_path, root)))
    if enabled is not None:
        values["enabled"] = enabled
    if directory is not None:
        values["directory"] = directory
    if "directory" not in values:
        raise EmergencyLedgerFallbackError("jsonl_fallback.directory must be configured")
    return EmergencyLedgerFallbackConfig(
        enabled=_bool_value(values["enabled"], "enabled"),
        directory=_resolve_directory(values["directory"], root),
    )


def _open_append_binary(path: Path) -> BinaryIO:
    return path.open("ab")


def _write_all(handle: Any, payload: bytes) -> None:
    written = handle.write(payload)
    if written is not None and written != len(payload):
        raise EmergencyLedgerFallbackError("emergency JSONL fallback write was incomplete")


def _resolve_config_path(config_path: str | Path | None, root: Path) -> Path:
    return root / "config" / "questdb.yaml" if config_path is None else Path(config_path)


def _load_yaml_fallback_values(config_path: Path) -> dict[str, object]:
    if not config_path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise EmergencyLedgerFallbackError(f"Invalid QuestDB YAML config: {config_path}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise EmergencyLedgerFallbackError("QuestDB YAML config must be a mapping")
    section = loaded.get("jsonl_fallback", {})
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        raise EmergencyLedgerFallbackError("questdb.yaml jsonl_fallback section must be a mapping")
    return {
        key: section[key]
        for key in ("enabled", "directory")
        if key in section and section[key] is not None
    }


def _resolve_directory(value: object, root: Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise EmergencyLedgerFallbackError("directory must be a path")
    text = str(value).strip()
    if not text:
        raise EmergencyLedgerFallbackError("directory must be non-empty")
    path = Path(text)
    if path.is_absolute():
        return path
    if ".." in path.parts:
        raise EmergencyLedgerFallbackError("directory must not contain parent traversal")
    return root / path


def _bool_value(value: object, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise EmergencyLedgerFallbackError(f"{field_name} must be bool")


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EmergencyLedgerFallbackError(f"{field_name} must be a non-empty string")
    return value.strip()


__all__ = [
    "EmergencyJSONLLedgerFallback",
    "EmergencyLedgerFallbackConfig",
    "EmergencyLedgerFallbackError",
    "EmergencyLedgerFallbackResult",
    "FALLBACK_SCHEMA_VERSION",
    "FsyncingJSONLFileWriter",
    "load_emergency_ledger_fallback_config",
]
