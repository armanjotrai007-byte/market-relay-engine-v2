"""Local Databento DBN file and folder inspection helpers.

This module only inspects local files. File-info inspection does not import
Databento, read DBN records, call external APIs, write QuestDB records, convert
DBN to Parquet, or build production ``MarketRecord`` objects.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass
import importlib
import importlib.util
from itertools import islice
import json
from json import JSONDecodeError
from pathlib import Path
from typing import Any


DBN_SUFFIXES = (".dbn", ".dbn.zst")
SIDECAR_JSON_NAMES = ("condition.json", "manifest.json", "metadata.json")
DEPENDENCY_ERROR_MESSAGE = (
    "Databento package is required for record-level DBN preview. "
    "File/folder inspection still works without it."
)
MAX_PREVIEW_TEXT_LENGTH = 500
MAX_SUMMARY_TEXT_LENGTH = 120


class DBNInspectionError(ValueError):
    """Raised when a local DBN path or sidecar cannot be inspected safely."""


class DatabentoDependencyError(DBNInspectionError):
    """Raised when record-level DBN preview is requested without Databento."""


@dataclass(frozen=True, kw_only=True)
class DBNFileInfo:
    """Small file-info summary for one local DBN file."""

    path: Path
    file_name: str
    suffix: str
    size_bytes: int
    parent_folder: Path
    schema_hint: str | None
    schema_hint_source: str | None
    sidecar_paths: dict[str, Path]
    sidecar_summaries: dict[str, dict[str, Any]]


@dataclass(frozen=True, kw_only=True)
class DBNBatchFileInfo:
    """Small folder-level summary for a local Databento batch/root folder."""

    root_path: Path
    dbn_file_count: int
    job_folder_count: int
    sidecar_file_count: int
    schema_hints: list[str]
    total_dbn_bytes: int
    files: list[DBNFileInfo]


def check_dbn_path(path: str | Path) -> Path:
    """Validate that ``path`` exists and is a DBN file or directory."""
    dbn_path = Path(path).expanduser()
    if not dbn_path.exists():
        raise DBNInspectionError(f"DBN path does not exist: {dbn_path}")
    if dbn_path.is_dir():
        return dbn_path
    if not dbn_path.is_file():
        raise DBNInspectionError(f"DBN path is not a file or directory: {dbn_path}")
    if _dbn_suffix(dbn_path) is None:
        raise DBNInspectionError(f"DBN file must end with .dbn or .dbn.zst: {dbn_path}")
    return dbn_path


def databento_available() -> bool:
    """Return whether the optional Databento Python package can be imported."""
    try:
        return importlib.util.find_spec("databento") is not None
    except (ImportError, ValueError):
        return False


def discover_dbn_files(path: str | Path, recursive: bool = True) -> list[Path]:
    """Return DBN files under ``path`` without reading record contents."""
    dbn_path = check_dbn_path(path)
    if dbn_path.is_file():
        return [dbn_path]

    iterator = dbn_path.rglob("*") if recursive else dbn_path.glob("*")
    return sorted(
        candidate
        for candidate in iterator
        if candidate.is_file() and _dbn_suffix(candidate) is not None
    )


def read_sidecar_json(path: str | Path) -> Any:
    """Read one sidecar JSON file with a clear error for invalid JSON."""
    sidecar_path = Path(path)
    if not sidecar_path.exists():
        raise DBNInspectionError(f"Sidecar JSON does not exist: {sidecar_path}")
    if not sidecar_path.is_file():
        raise DBNInspectionError(f"Sidecar JSON path is not a file: {sidecar_path}")
    try:
        return json.loads(sidecar_path.read_text(encoding="utf-8"))
    except JSONDecodeError as exc:
        raise DBNInspectionError(f"Invalid sidecar JSON: {sidecar_path}: {exc}") from exc
    except OSError as exc:
        raise DBNInspectionError(f"Failed to read sidecar JSON: {sidecar_path}: {exc}") from exc


def inspect_dbn_file_info(path: str | Path) -> DBNFileInfo:
    """Inspect DBN file metadata and nearby Databento sidecars only."""
    dbn_path = check_dbn_path(path)
    if not dbn_path.is_file():
        raise DBNInspectionError(f"DBN file inspection requires a file path: {dbn_path}")

    sidecar_paths: dict[str, Path] = {}
    sidecar_summaries: dict[str, dict[str, Any]] = {}
    sidecar_schema_hint: str | None = None

    for sidecar_name in SIDECAR_JSON_NAMES:
        sidecar_path = dbn_path.parent / sidecar_name
        if not sidecar_path.exists():
            continue
        payload = read_sidecar_json(sidecar_path)
        sidecar_paths[sidecar_name] = sidecar_path
        sidecar_summaries[sidecar_name] = _summarize_sidecar_json(payload)
        if sidecar_schema_hint is None:
            sidecar_schema_hint = _find_schema_hint(payload)

    schema_hint_source = None
    schema_hint = sidecar_schema_hint
    if schema_hint:
        schema_hint_source = "sidecar"
    else:
        schema_hint = _schema_hint_from_filename(dbn_path)
        if schema_hint:
            schema_hint_source = "filename"

    return DBNFileInfo(
        path=dbn_path,
        file_name=dbn_path.name,
        suffix=_dbn_suffix(dbn_path) or "",
        size_bytes=dbn_path.stat().st_size,
        parent_folder=dbn_path.parent,
        schema_hint=schema_hint,
        schema_hint_source=schema_hint_source,
        sidecar_paths=sidecar_paths,
        sidecar_summaries=sidecar_summaries,
    )


def inspect_dbn_folder(path: str | Path, recursive: bool = True) -> DBNBatchFileInfo:
    """Inspect DBN files recursively under a local Databento folder."""
    root_path = check_dbn_path(path)
    if not root_path.is_dir():
        raise DBNInspectionError(f"DBN folder inspection requires a directory path: {root_path}")

    files = [inspect_dbn_file_info(dbn_path) for dbn_path in discover_dbn_files(root_path, recursive)]
    job_folders = {file_info.parent_folder for file_info in files}
    sidecar_files = {
        sidecar_path
        for file_info in files
        for sidecar_path in file_info.sidecar_paths.values()
    }
    schema_hints = sorted(
        {file_info.schema_hint for file_info in files if file_info.schema_hint is not None}
    )

    return DBNBatchFileInfo(
        root_path=root_path,
        dbn_file_count=len(files),
        job_folder_count=len(job_folders),
        sidecar_file_count=len(sidecar_files),
        schema_hints=schema_hints,
        total_dbn_bytes=sum(file_info.size_bytes for file_info in files),
        files=files,
    )


def inspect_dbn_file(
    path: str | Path,
    limit: int = 5,
    file_info_only: bool = False,
) -> dict[str, Any]:
    """Inspect one DBN file or folder, optionally previewing records."""
    if limit < 0:
        raise DBNInspectionError("limit must be greater than or equal to 0")

    dbn_path = check_dbn_path(path)
    if dbn_path.is_dir():
        folder_info = inspect_dbn_folder(dbn_path)
        preview = None
        preview_file = None
        if not file_info_only and folder_info.files:
            preview_file = folder_info.files[0].path
            preview = _preview_dbn_records(preview_file, limit)
        return {
            "kind": "folder",
            "folder": folder_info,
            "preview_file": preview_file,
            "preview": preview,
        }

    file_info = inspect_dbn_file_info(dbn_path)
    preview = None if file_info_only else _preview_dbn_records(dbn_path, limit)
    return {"kind": "file", "file": file_info, "preview": preview}


def format_dbn_inspection_result(result: Any, max_files: int = 20) -> str:
    """Format a file, folder, or preview result for CLI output."""
    if isinstance(result, DBNFileInfo):
        return _format_file_info(result)
    if isinstance(result, DBNBatchFileInfo):
        return _format_folder_info(result, max_files=max_files)
    if isinstance(result, dict):
        kind = result.get("kind")
        if kind == "file":
            lines = [_format_file_info(result["file"])]
            if result.get("preview") is not None:
                lines.extend(["", _format_preview(result["preview"])])
            return "\n".join(lines)
        if kind == "folder":
            lines = [_format_folder_info(result["folder"], max_files=max_files)]
            if result.get("preview") is not None:
                lines.extend(
                    [
                        "",
                        f"preview_file: {result.get('preview_file')}",
                        _format_preview(result["preview"]),
                    ]
                )
            return "\n".join(lines)
    return str(result)


def _load_databento() -> Any:
    try:
        return importlib.import_module("databento")
    except ImportError as exc:
        raise DatabentoDependencyError(DEPENDENCY_ERROR_MESSAGE) from exc


def _preview_dbn_records(path: Path, limit: int) -> dict[str, Any]:
    if limit == 0:
        return {
            "records_previewed": 0,
            "limit": limit,
            "record_type_names": [],
            "field_names": [],
            "records": [],
        }

    databento = _load_databento()
    try:
        store = databento.DBNStore.from_file(path)
        records: list[dict[str, Any]] = []
        field_names: set[str] = set()
        record_type_names: set[str] = set()

        for record in islice(store, limit):
            preview = _safe_preview_record(record)
            records.append(preview)
            record_type_names.add(preview["type"])
            field_names.update(preview["fields"])

        return {
            "records_previewed": len(records),
            "limit": limit,
            "record_type_names": sorted(record_type_names),
            "field_names": sorted(field_names),
            "records": records,
        }
    except Exception as exc:  # noqa: BLE001 - wrap optional parser failures clearly.
        raise DBNInspectionError(f"Failed to parse DBN file: {exc}") from exc


def _safe_preview_record(record: Any) -> dict[str, Any]:
    values = _record_to_preview_dict(record)
    field_names = sorted(values)
    if values:
        preview: Any = {key: _short_summary_value(value) for key, value in values.items()}
    else:
        preview = _truncate_text(repr(record), MAX_PREVIEW_TEXT_LENGTH)
    return {
        "type": type(record).__name__,
        "fields": field_names,
        "preview": preview,
    }


def _record_to_preview_dict(record: Any) -> dict[str, Any]:
    if is_dataclass(record) and not isinstance(record, type):
        try:
            return asdict(record)
        except Exception:
            return {}
    if hasattr(record, "_asdict") and callable(record._asdict):
        try:
            return dict(record._asdict())
        except Exception:
            return {}
    if is_dataclass(record):
        try:
            return {field.name: getattr(record, field.name) for field in fields(record)}
        except Exception:
            return {}
    if hasattr(record, "__dict__"):
        return {
            key: value
            for key, value in vars(record).items()
            if not key.startswith("_") and not callable(value)
        }
    return {}


def _dbn_suffix(path: Path) -> str | None:
    name = path.name.lower()
    for suffix in DBN_SUFFIXES:
        if name.endswith(suffix):
            return suffix
    return None


def _schema_hint_from_filename(path: Path) -> str | None:
    name = path.name
    suffix = _dbn_suffix(path)
    if suffix is None:
        return None
    stem = name[: -len(suffix)]
    if "." not in stem:
        return None
    schema_hint = stem.rsplit(".", maxsplit=1)[-1].strip().lower()
    return schema_hint or None


def _find_schema_hint(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    direct_schema = payload.get("schema")
    if isinstance(direct_schema, str) and direct_schema.strip():
        return direct_schema.strip()
    query = payload.get("query")
    if isinstance(query, dict):
        query_schema = query.get("schema")
        if isinstance(query_schema, str) and query_schema.strip():
            return query_schema.strip()
    return None


def _summarize_sidecar_json(payload: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "top_level_type": _top_level_type(payload),
    }
    if isinstance(payload, dict):
        summary["top_level_keys"] = sorted(str(key) for key in payload)
        selected = _selected_summary_values(payload)
        if selected:
            summary["selected_values"] = selected
    elif isinstance(payload, list):
        summary["list_length"] = len(payload)
        if payload and isinstance(payload[0], dict):
            summary["first_item_keys"] = sorted(str(key) for key in payload[0])
            selected = _selected_summary_values(payload[0])
            if selected:
                summary["first_item_selected_values"] = selected
    return summary


def _top_level_type(payload: Any) -> str:
    if isinstance(payload, dict):
        return "object"
    if isinstance(payload, list):
        return "array"
    if payload is None:
        return "null"
    return type(payload).__name__


def _selected_summary_values(payload: dict[str, Any]) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    for key in (
        "dataset",
        "schema",
        "symbols",
        "start",
        "end",
        "start_date",
        "end_date",
        "date_range",
        "job_id",
        "stype_in",
        "stype_out",
    ):
        if key in payload:
            selected[key] = _short_summary_value(payload[key])

    query = payload.get("query")
    if isinstance(query, dict):
        for key in ("dataset", "schema", "symbols", "start", "end", "start_date", "end_date"):
            if key in query:
                selected[f"query.{key}"] = _short_summary_value(query[key])
    return selected


def _short_summary_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _truncate_text(str(value), MAX_SUMMARY_TEXT_LENGTH) if isinstance(value, str) else value
    if isinstance(value, list):
        if len(value) <= 5 and all(isinstance(item, (str, int, float, bool)) or item is None for item in value):
            return value
        return f"{len(value)} item(s)"
    if isinstance(value, dict):
        keys = sorted(str(key) for key in value)
        return f"object keys: {', '.join(keys[:8])}"
    return _truncate_text(repr(value), MAX_SUMMARY_TEXT_LENGTH)


def _truncate_text(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


def _format_file_info(file_info: DBNFileInfo) -> str:
    lines = [
        "DBN file:",
        f"  path: {file_info.path}",
        f"  file_name: {file_info.file_name}",
        f"  suffix: {file_info.suffix}",
        f"  size_bytes: {file_info.size_bytes}",
        f"  parent_folder: {file_info.parent_folder}",
        f"  schema_hint: {file_info.schema_hint or 'none'}",
        f"  schema_hint_source: {file_info.schema_hint_source or 'none'}",
        f"  sidecars: {', '.join(file_info.sidecar_paths) if file_info.sidecar_paths else 'none'}",
    ]
    for sidecar_name, summary in file_info.sidecar_summaries.items():
        lines.append(f"  {sidecar_name}:")
        lines.append(f"    top_level_type: {summary.get('top_level_type')}")
        if "top_level_keys" in summary:
            lines.append(f"    top_level_keys: {', '.join(summary['top_level_keys'])}")
        if "list_length" in summary:
            lines.append(f"    list_length: {summary['list_length']}")
        if "first_item_keys" in summary:
            lines.append(f"    first_item_keys: {', '.join(summary['first_item_keys'])}")
        selected = summary.get("selected_values") or summary.get("first_item_selected_values")
        if selected:
            lines.append(f"    selected_values: {json.dumps(selected, sort_keys=True)}")
    return "\n".join(lines)


def _format_folder_info(folder_info: DBNBatchFileInfo, max_files: int) -> str:
    lines = [
        "DBN folder:",
        f"  root_path: {folder_info.root_path}",
        f"  dbn_file_count: {folder_info.dbn_file_count}",
        f"  job_folder_count: {folder_info.job_folder_count}",
        f"  sidecar_file_count: {folder_info.sidecar_file_count}",
        f"  schema_hints: {', '.join(folder_info.schema_hints) if folder_info.schema_hints else 'none'}",
        f"  total_dbn_bytes: {folder_info.total_dbn_bytes}",
        f"  displayed_files: {min(max_files, folder_info.dbn_file_count)}",
    ]
    for index, file_info in enumerate(folder_info.files[:max_files], start=1):
        lines.extend(
            [
                f"  file_{index}:",
                f"    path: {file_info.path}",
                f"    size_bytes: {file_info.size_bytes}",
                f"    schema_hint: {file_info.schema_hint or 'none'}",
                f"    schema_hint_source: {file_info.schema_hint_source or 'none'}",
                f"    sidecars: {', '.join(file_info.sidecar_paths) if file_info.sidecar_paths else 'none'}",
            ]
        )
    return "\n".join(lines)


def _format_preview(preview: dict[str, Any]) -> str:
    return "\n".join(
        [
            "DBN record preview:",
            f"  records_previewed: {preview['records_previewed']}",
            f"  limit: {preview['limit']}",
            f"  record_type_names: {', '.join(preview['record_type_names']) if preview['record_type_names'] else 'none'}",
            f"  field_names: {', '.join(preview['field_names']) if preview['field_names'] else 'none'}",
            "  records:",
            json.dumps(preview["records"], indent=2, sort_keys=True),
        ]
    )
