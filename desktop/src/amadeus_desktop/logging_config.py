"""Redacted rotating application logging."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 3

_HEADER_PATTERN = re.compile(
    r"\b(authorization|api[-_]?key)\b(\s*[:=]\s*)(?:bearer\s+)?([^\s,;]+)",
    flags=re.IGNORECASE,
)
_TOKEN_PATTERN = re.compile(r"\b(?:sk|tp)-[A-Za-z0-9][A-Za-z0-9._-]{5,}\b", re.IGNORECASE)


def redact_log_text(text: str) -> str:
    """Remove supported credential forms from already-rendered log text."""

    redacted = _HEADER_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text
    )
    return _TOKEN_PATTERN.sub("[REDACTED]", redacted)


class RedactingFormatter(logging.Formatter):
    """Format first, then redact so both messages and arguments are covered."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(super().format(record))


def configure_logging(
    log_file: Path,
    *,
    logger_name: str = "amadeus",
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> logging.Logger:
    """Create one non-propagating rotating file logger."""

    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(
        RedactingFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    logger.addHandler(handler)
    return logger


def close_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.flush()
        handler.close()
        logger.removeHandler(handler)
