"""Small logging helper for local development."""

from __future__ import annotations

import logging
import os
from typing import Any


def get_logger(name: str, level: str | int | None = None) -> logging.Logger:
    """Return a standard console logger without creating log files."""
    logger = logging.getLogger(name)
    resolved_level = level or os.getenv("LOG_LEVEL", "INFO")
    logger.setLevel(resolved_level)

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
        )
        logger.addHandler(handler)

    logger.propagate = False
    return logger


def build_log_context(
    run_id: str | None = None,
    session_id: str | None = None,
    trace_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Return a plain structured log context dictionary."""
    context: dict[str, Any] = {}
    if run_id is not None:
        context["run_id"] = run_id
    if session_id is not None:
        context["session_id"] = session_id
    if trace_id is not None:
        context["trace_id"] = trace_id
    context.update(extra)
    return context
