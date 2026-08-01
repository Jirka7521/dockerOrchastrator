"""Logging configuration for the command line application."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-24s | %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class LoggingSetup:
    """Configures the root logger for console and (optionally) a rotating file."""

    MAX_BYTES = 5 * 1024 * 1024
    BACKUP_COUNT = 3

    @staticmethod
    def configure(
        level: str = "INFO",
        log_file: Path | str | None = None,
        max_bytes: int = MAX_BYTES,
        backup_count: int = BACKUP_COUNT,
    ) -> None:
        formatter = logging.Formatter(DEFAULT_FORMAT, DEFAULT_DATE_FORMAT)
        root = logging.getLogger()
        root.setLevel(getattr(logging, level.upper(), logging.INFO))

        for handler in list(root.handlers):
            root.removeHandler(handler)

        console = logging.StreamHandler(stream=sys.stderr)
        console.setFormatter(formatter)
        root.addHandler(console)

        if log_file is None:
            return

        path = Path(log_file).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
        except OSError as exc:
            # A log file we cannot open is not a reason to refuse to run.
            root.warning("Could not open log file %s: %s. Logging to console only.", path, exc)
            return

        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        root.info("Logging to %s", path)
