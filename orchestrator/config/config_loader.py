"""Reads config files from disk and detects edits while running."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Tuple

from orchestrator.config.app_config import AppConfig
from orchestrator.errors import ConfigError


class ConfigLoader:
    """Loads :class:`AppConfig` from a JSON file and tracks its fingerprint.

    The daemon uses :meth:`reload_if_changed` so a config edit takes effect on
    the next tick. A broken edit is reported and then ignored: the daemon keeps
    running with the last configuration that parsed.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self._fingerprint: Tuple[int, int] | None = None
        self._log = logging.getLogger(self.__class__.__name__)

    # ------------------------------------------------------------------ load

    def load(self) -> AppConfig:
        raw = self._read_json()
        config = AppConfig.from_dict(raw, source_path=self._resolved_path())
        self._fingerprint = self._current_fingerprint()
        return config

    def reload_if_changed(self) -> AppConfig | None:
        """Return a fresh config if the file changed, otherwise ``None``.

        Never raises: a config that no longer parses must not take down a
        daemon that is otherwise healthy.
        """
        fingerprint = self._current_fingerprint()
        if fingerprint is None or fingerprint == self._fingerprint:
            return None

        self._log.info("Config file changed, reloading: %s", self.path)
        try:
            return self.load()
        except ConfigError as exc:
            # Remember the broken fingerprint so we complain once, not every tick.
            self._fingerprint = fingerprint
            self._log.error(
                "Keeping previous configuration; the new one is invalid: %s", exc
            )
            return None

    # -------------------------------------------------------------- internals

    def _resolved_path(self) -> Path:
        try:
            return self.path.resolve()
        except OSError:  # pragma: no cover - exotic filesystem states
            return self.path

    def _read_json(self) -> Any:
        path = self.path
        if not path.exists():
            raise ConfigError(
                f"Config file not found: {path}. Copy "
                "docker_schedule_config.example.json to that location and edit it."
            )
        if path.is_dir():
            raise ConfigError(f"Config path is a directory, not a file: {path}")

        try:
            # utf-8-sig transparently strips the BOM that Windows editors add.
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ConfigError(f"Could not read config file {path}: {exc}") from exc

        if not text.strip():
            raise ConfigError(f"Config file is empty: {path}")

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"Config file {path} is not valid JSON: {exc.msg} "
                f"(line {exc.lineno}, column {exc.colno})"
            ) from exc

    def _current_fingerprint(self) -> Tuple[int, int] | None:
        try:
            stat = os.stat(self.path)
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)
