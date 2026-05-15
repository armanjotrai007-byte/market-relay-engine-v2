"""Small logging helper for local development."""

from __future__ import annotations

import logging
import os


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
