"""Runtime and contract ID helpers."""

from __future__ import annotations

from uuid import uuid4


def new_prefixed_uuid(prefix: str) -> str:
    """Return a log-safe UUID string with a short semantic prefix."""
    if not isinstance(prefix, str) or not prefix.strip():
        raise ValueError("ID prefix must be a non-empty string")
    safe_prefix = prefix.strip().lower().replace("-", "_")
    return f"{safe_prefix}_{uuid4().hex}"


def new_run_id() -> str:
    """Return a new process/run identifier."""
    return new_prefixed_uuid("run")


def new_session_id() -> str:
    """Return a new session identifier."""
    return new_prefixed_uuid("session")


def new_trace_id() -> str:
    """Return a new trace identifier."""
    return new_prefixed_uuid("trace")


def new_record_id(prefix: str) -> str:
    """Return a new record identifier for a contract instance."""
    return new_prefixed_uuid(prefix)
