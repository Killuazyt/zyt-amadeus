from __future__ import annotations

import logging
from pathlib import Path

from amadeus_desktop.logging_config import close_logger, configure_logging, redact_log_text


def test_redaction_covers_headers_and_common_token_prefixes() -> None:
    source = (
        "Authorization: Bearer sk-example123456 api-key=tp-example654321 api_key: ordinary-value"
    )

    redacted = redact_log_text(source)

    assert "example123456" not in redacted
    assert "example654321" not in redacted
    assert "ordinary-value" not in redacted
    assert redacted.count("[REDACTED]") == 3


def test_logger_redacts_message_arguments(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "amadeus.log"
    logger = configure_logging(log_file, logger_name="amadeus.test.redaction")

    logger.info("request %s", "Authorization=Bearer sk-never-write-this")
    close_logger(logger)

    content = log_file.read_text(encoding="utf-8")
    assert "never-write-this" not in content
    assert "[REDACTED]" in content


def test_log_rotation_respects_backup_limit(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "amadeus.log"
    logger = configure_logging(
        log_file,
        logger_name="amadeus.test.rotation",
        max_bytes=128,
        backup_count=3,
    )

    for index in range(30):
        logger.info("rotation-entry-%02d %s", index, "x" * 40)
    close_logger(logger)

    backups = sorted(log_file.parent.glob("amadeus.log.*"))
    assert 1 <= len(backups) <= 3
    assert all(path.stat().st_size > 0 for path in backups)


def test_configured_logger_does_not_propagate(tmp_path: Path) -> None:
    logger = configure_logging(
        tmp_path / "amadeus.log",
        logger_name="amadeus.test.propagation",
    )
    try:
        assert logger.propagate is False
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0], logging.FileHandler)
    finally:
        close_logger(logger)
