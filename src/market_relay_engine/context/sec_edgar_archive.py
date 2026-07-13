"""Source-specific immutable archive and checkpoint for SEC EDGAR collection."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Mapping


class SECArchiveError(RuntimeError):
    """Raised when the SEC archive cannot preserve its durable state."""


class SECEDGARArchive:
    """Keep source documents immutable while atomically updating collector state."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.filings = self.root / "filings"
        self.form4 = self.root / "form4"
        self.manifest_path = self.root / "manifests" / "sec_filings.json"

    def archive_document(self, content: bytes, *, extension: str) -> str:
        digest = sha256(content).hexdigest()
        safe_extension = _safe_extension(extension)
        target = self.objects / digest / f"original.{safe_extension}"
        if target.exists():
            if target.read_bytes() != content:
                raise SECArchiveError("SEC archive hash path contains different content")
            return digest
        self._atomic_bytes(target, content)
        return digest

    def archive_normalized_text(self, document_hash: str, text: str) -> None:
        target = self.objects / document_hash / "normalized.txt"
        payload = text.encode("utf-8")
        if target.exists():
            if target.read_bytes() != payload:
                raise SECArchiveError("SEC normalized archive would overwrite different content")
            return
        self._atomic_bytes(target, payload)

    def archive_normalized_section(
        self,
        document_hash: str,
        *,
        item_number: str,
        section_hash: str,
        text: str,
    ) -> Path:
        """Archive one complete normalized 8-K item without truncation."""
        safe_item = item_number.replace(".", "_")
        payload = text.encode("utf-8")
        if sha256(payload).hexdigest() != section_hash:
            raise SECArchiveError("SEC normalized section hash does not match content")
        target = (
            self.objects
            / document_hash
            / "sections"
            / f"item_{safe_item}_{section_hash}.txt"
        )
        if target.exists():
            if target.read_bytes() != payload:
                raise SECArchiveError(
                    "SEC normalized section would overwrite different content"
                )
            return target
        self._atomic_bytes(target, payload)
        return target

    def read_document(self, document_hash: str, *, extension: str) -> bytes:
        if len(document_hash) != 64 or any(
            value not in "0123456789abcdef" for value in document_hash
        ):
            raise SECArchiveError("SEC archive document hash is invalid")
        path = self.objects / document_hash / f"original.{_safe_extension(extension)}"
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise SECArchiveError("SEC archive document is missing") from exc
        if sha256(content).hexdigest() != document_hash:
            raise SECArchiveError("SEC archive document hash does not match content")
        return content

    def read_filing_metadata(self, accession_number: str) -> dict[str, Any] | None:
        path = self.filings / f"{accession_number}.json"
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SECArchiveError("SEC immutable filing metadata could not be read") from exc
        if not isinstance(value, dict):
            raise SECArchiveError("SEC immutable filing metadata has invalid shape")
        return value

    def write_filing_once(self, accession_number: str, payload: Mapping[str, Any]) -> None:
        self._write_json_once(self.filings / f"{accession_number}.json", payload)

    def write_form4_once(self, accession_number: str, payload: Mapping[str, Any]) -> None:
        self._write_json_once(self.form4 / f"{accession_number}.json", payload)

    def load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {"schema_version": 2, "filings": {}}
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SECArchiveError("SEC checkpoint could not be read") from exc
        if not isinstance(value, dict) or not isinstance(value.get("filings"), dict):
            raise SECArchiveError("SEC checkpoint has invalid shape")
        return value

    def save_manifest(self, manifest: Mapping[str, Any]) -> None:
        self._atomic_replace(
            self.manifest_path,
            json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=True).encode("utf-8") + b"\n",
        )

    def _write_json_once(self, path: Path, payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True).encode("utf-8") + b"\n"
        if path.exists():
            if path.read_bytes() != encoded:
                raise SECArchiveError("SEC immutable metadata would overwrite different content")
            return
        self._atomic_bytes(path, encoded)

    @staticmethod
    def _atomic_bytes(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # Hard-link publication does not replace an existing target, unlike
                # os.replace on Windows.  That preserves immutable object identity.
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != payload:
                    raise SECArchiveError("SEC archive target contains different content")
        except SECArchiveError:
            raise
        except OSError as exc:
            raise SECArchiveError("SEC archive atomic write failed") from exc
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _atomic_replace(path: Path, payload: bytes) -> None:
        """Atomically publish a new mutable checkpoint generation."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise SECArchiveError("SEC checkpoint atomic update failed") from exc
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)


def _safe_extension(extension: str) -> str:
    value = extension.lower().lstrip(".")
    if value not in {"htm", "html", "txt", "xml"}:
        return "bin"
    return value


__all__ = ["SECArchiveError", "SECEDGARArchive"]
