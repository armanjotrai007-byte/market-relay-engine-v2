"""Deterministic fake IDs and hashes for reusable fixture records."""

import hashlib

RUN_ID = "FIXTURE-RUN-0001"
SESSION_ID = "FIXTURE-SESSION-0001"
TRACE_ID_APPROVED_OIL = "FIXTURE-TRACE-0001"
TRACE_ID_BLOCKED_DEFENSE = "FIXTURE-TRACE-0002"
TRACE_ID_REDUCED_SIZE = "FIXTURE-TRACE-0003"
TRACE_ID_LATENCY_WARNING = "FIXTURE-TRACE-0004"
TRACE_ID_STALE_CONTEXT = "FIXTURE-TRACE-0005"


def stable_record_id(prefix: str, index: int) -> str:
    """Return ``FIXTURE-{PREFIX}-{INDEX:04d}`` with a normalized prefix.

    The output is deterministic across test runs and visually distinct from
    production UUID-based IDs. Prefix text is uppercased, and spaces or
    underscores are normalized to hyphens.
    """
    if not isinstance(prefix, str) or not prefix.strip():
        raise ValueError("Fixture ID prefix must be a non-empty string")
    if not isinstance(index, int) or index < 0:
        raise ValueError("Fixture ID index must be a non-negative integer")

    normalized = prefix.strip().replace("_", "-").replace(" ", "-").upper()
    normalized = "-".join(part for part in normalized.split("-") if part)
    if not normalized:
        raise ValueError("Fixture ID prefix must contain at least one valid character")
    return f"FIXTURE-{normalized}-{index:04d}"


def stable_sha256(label: str, index: int) -> str:
    """Return a deterministic lowercase SHA-256 value for fixture metadata."""
    if not isinstance(label, str) or not label.strip():
        raise ValueError("Fixture hash label must be a non-empty string")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("Fixture hash index must be a non-negative integer")
    payload = f"market-relay-engine-fixture:{label.strip()}:{index}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

