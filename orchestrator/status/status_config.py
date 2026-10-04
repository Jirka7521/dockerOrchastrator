"""The optional ``status_reporter`` section of the config file."""

from __future__ import annotations

import base64
import binascii
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Tuple
from urllib.parse import urlsplit

from orchestrator.config.field_reader import FieldReader
from orchestrator.errors import ConfigError
from orchestrator.status.command_types import ALL as COMMAND_TYPES
from orchestrator.status.command_types import DEFAULT_ALLOWED

KNOWN_KEYS = (
    "enabled",
    "api_url",
    "shared_key_file",
    "host_interval_seconds",
    "containers_interval_seconds",
    "connectivity_interval_seconds",
    "disk_health_interval_seconds",
    "request_timeout_seconds",
    "command_poll_seconds",
    "ping_targets",
    "ping_count",
    "dns_probe_host",
    "smartctl_executable",
    "vcgencmd_executable",
    "max_log_lines",
    "max_log_bytes",
    "allowed_commands",
    "container_action_timeout_seconds",
    "systemctl_executable",
)

#: Host names and IP literals -- what may be handed to ping and getaddrinfo.
_HOST_PATTERN = re.compile(r"^[A-Za-z0-9.:-]{1,253}$")

#: HMAC-SHA256 gains nothing above its block size and loses everything below 32 bytes.
MINIMUM_KEY_BYTES = 32


@dataclass(frozen=True)
class StatusReporterConfig:
    """Where to report, how often, and what to probe.

    Disabled by default, so an existing config file keeps working unchanged:
    the reporter only runs once ``"enabled": true`` and an API URL and key
    file are configured.
    """

    enabled: bool = False
    api_url: str = ""
    shared_key_file: Path | None = None

    host_interval_seconds: float = 15.0
    containers_interval_seconds: float = 10.0
    connectivity_interval_seconds: float = 30.0
    disk_health_interval_seconds: float = 600.0

    request_timeout_seconds: float = 10.0
    command_poll_seconds: int = 25

    ping_targets: Tuple[str, ...] = ("1.1.1.1", "8.8.8.8")
    ping_count: int = 3
    dns_probe_host: str = "cloudflare.com"

    smartctl_executable: str = "smartctl"
    vcgencmd_executable: str = "vcgencmd"

    max_log_lines: int = 5000
    max_log_bytes: int = 2_000_000

    #: What the dashboard may ask this host to do. Reading logs only, unless
    #: the person who owns the host adds more: this file is the one place
    #: that decides, and the API cannot change it.
    allowed_commands: Tuple[str, ...] = DEFAULT_ALLOWED
    container_action_timeout_seconds: float = 120.0
    systemctl_executable: str = "systemctl"

    # ------------------------------------------------------------------ build

    @staticmethod
    def from_dict(
        data: Mapping[str, Any],
        base_dir: Path,
        path: str = "status_reporter",
    ) -> "StatusReporterConfig":
        reader = FieldReader(data, path)
        reader.warn_unknown_keys(KNOWN_KEYS)

        if not reader.boolean("enabled", False):
            return StatusReporterConfig()

        defaults = StatusReporterConfig()
        api_url = StatusReporterConfig._http_url(reader, "api_url", required=True)

        key_file = Path(reader.text("shared_key_file")).expanduser()
        if not key_file.is_absolute():
            key_file = base_dir / key_file

        targets = tuple(
            StatusReporterConfig._host(item, f"{path}.ping_targets[{index}]")
            for index, item in enumerate(reader.sequence("ping_targets", list(defaults.ping_targets)))
        )

        # Duplicates dropped, order kept: the list is shown back as written.
        allowed = tuple(
            dict.fromkeys(
                StatusReporterConfig._command_type(item, f"{path}.allowed_commands[{index}]")
                for index, item in enumerate(reader.sequence("allowed_commands", list(defaults.allowed_commands)))
            )
        )

        return StatusReporterConfig(
            enabled=True,
            api_url=api_url.rstrip("/"),
            shared_key_file=key_file.resolve(),
            host_interval_seconds=reader.number(
                "host_interval_seconds", defaults.host_interval_seconds, minimum=5, maximum=3600
            ),
            containers_interval_seconds=reader.number(
                "containers_interval_seconds", defaults.containers_interval_seconds, minimum=5, maximum=3600
            ),
            connectivity_interval_seconds=reader.number(
                "connectivity_interval_seconds", defaults.connectivity_interval_seconds, minimum=10, maximum=3600
            ),
            disk_health_interval_seconds=reader.number(
                "disk_health_interval_seconds", defaults.disk_health_interval_seconds, minimum=60, maximum=86400
            ),
            request_timeout_seconds=reader.number(
                "request_timeout_seconds", defaults.request_timeout_seconds, minimum=1, maximum=120
            ),
            command_poll_seconds=reader.integer(
                "command_poll_seconds", defaults.command_poll_seconds, minimum=1, maximum=55
            ),
            ping_targets=targets,
            ping_count=reader.integer("ping_count", defaults.ping_count, minimum=1, maximum=10),
            dns_probe_host=StatusReporterConfig._host(
                reader.text("dns_probe_host", defaults.dns_probe_host), f"{path}.dns_probe_host"
            ),
            smartctl_executable=reader.text("smartctl_executable", defaults.smartctl_executable),
            vcgencmd_executable=reader.text("vcgencmd_executable", defaults.vcgencmd_executable),
            max_log_lines=reader.integer("max_log_lines", defaults.max_log_lines, minimum=10, maximum=20000),
            max_log_bytes=reader.integer(
                "max_log_bytes", defaults.max_log_bytes, minimum=10_000, maximum=4_000_000
            ),
            allowed_commands=allowed,
            container_action_timeout_seconds=reader.number(
                "container_action_timeout_seconds", defaults.container_action_timeout_seconds, minimum=10, maximum=900
            ),
            systemctl_executable=reader.text("systemctl_executable", defaults.systemctl_executable),
        )

    # --------------------------------------------------------------- key file

    def load_key(self) -> bytes:
        """Read and decode the shared key, refusing a file others can read.

        The key authenticates this root process to the API. A copy readable by
        any other host user would let that user impersonate the orchestrator,
        so a group- or world-readable file is a configuration error, not a
        warning.
        """
        if self.shared_key_file is None:
            raise ConfigError("status_reporter.shared_key_file is not set")

        path = self.shared_key_file
        try:
            info = path.stat()
        except OSError as exc:
            raise ConfigError(f"Cannot read the status key file {path}: {exc}") from exc

        if os.name == "posix" and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ConfigError(
                f"The status key file {path} is accessible to other users "
                f"(mode {stat.S_IMODE(info.st_mode):o}). Run: chmod 600 {path}"
            )

        try:
            key = base64.b64decode(path.read_text(encoding="ascii").strip(), validate=True)
        except (OSError, UnicodeDecodeError, binascii.Error) as exc:
            raise ConfigError(f"The status key file {path} is not base64: {exc}") from exc

        if len(key) < MINIMUM_KEY_BYTES:
            raise ConfigError(
                f"The status key in {path} is {len(key)} bytes; at least {MINIMUM_KEY_BYTES} are required"
            )
        return key

    def describe(self) -> str:
        if not self.enabled:
            return "status reporter   : disabled"
        return (
            f"status reporter   : {self.api_url} (host {self.host_interval_seconds:g}s, "
            f"containers {self.containers_interval_seconds:g}s; "
            f"allowed commands: {', '.join(self.allowed_commands) or 'none'})"
        )

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _http_url(reader: FieldReader, key: str, required: bool) -> str:
        value = reader.text(key) if required else reader.optional_text(key)
        if value is None:
            return None  # type: ignore[return-value]
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ConfigError(f"status_reporter.{key} must be an http:// or https:// URL")
        return value

    @staticmethod
    def _command_type(value: Any, key_path: str) -> str:
        if value not in COMMAND_TYPES:
            raise ConfigError(f"{key_path} must be one of {', '.join(COMMAND_TYPES)}")
        return value

    @staticmethod
    def _host(value: Any, key_path: str) -> str:
        if not isinstance(value, str) or not _HOST_PATTERN.match(value.strip()):
            raise ConfigError(f"{key_path} must be a host name or an IP address")
        return value.strip()
