"""Operational knobs: timeouts, retries, parallelism, daemon behaviour.

Every field is optional and the defaults reproduce the historical behaviour
closely enough that an existing config file keeps working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from orchestrator.config.field_reader import FieldReader

KNOWN_KEYS = (
    "max_parallel_tasks",
    "container_timeout_seconds",
    "docker_command_timeout_seconds",
    "docker_retries",
    "docker_retry_backoff_seconds",
    "failure_log_lines",
    "create_missing_from_image",
    "poll_interval_seconds",
    "startup_catch_up_minutes",
    "reload_config_on_change",
)


@dataclass(frozen=True)
class RuntimeConfig:
    """Tuning that affects how work is executed, not when."""

    #: 0 means "one worker per task"; a positive value caps concurrency so a
    #: config with 40 pipelines cannot flood the Docker daemon.
    max_parallel_tasks: int = 0

    #: Wall-clock budget for a single container. ``None`` waits indefinitely,
    #: which is the historical behaviour.
    container_timeout_seconds: float | None = None

    #: Budget for short bookkeeping commands (``inspect``, ``start``, ``logs``).
    docker_command_timeout_seconds: float = 60.0

    #: Extra attempts for commands that failed for a transient reason.
    docker_retries: int = 2
    docker_retry_backoff_seconds: float = 2.0

    #: How many log lines to attach to a failed container's error message.
    failure_log_lines: int = 20

    #: Create and run a container from an equally named image when no container
    #: with that name exists.
    create_missing_from_image: bool = True

    #: Daemon loop granularity; also the longest a stop signal can take effect.
    poll_interval_seconds: float = 5.0

    #: On startup, still run a tick that was missed this many minutes ago.
    startup_catch_up_minutes: int = 5

    #: Pick up config file edits without a restart.
    reload_config_on_change: bool = True

    @staticmethod
    def from_dict(data: Mapping[str, Any], path: str = "runtime") -> "RuntimeConfig":
        reader = FieldReader(data, path)
        reader.warn_unknown_keys(KNOWN_KEYS)
        defaults = RuntimeConfig()

        return RuntimeConfig(
            max_parallel_tasks=reader.integer(
                "max_parallel_tasks", defaults.max_parallel_tasks, minimum=0, maximum=1024
            ),
            container_timeout_seconds=reader.optional_number(
                "container_timeout_seconds", minimum=1
            ),
            docker_command_timeout_seconds=reader.number(
                "docker_command_timeout_seconds",
                defaults.docker_command_timeout_seconds,
                minimum=1,
            ),
            docker_retries=reader.integer(
                "docker_retries", defaults.docker_retries, minimum=0, maximum=10
            ),
            docker_retry_backoff_seconds=reader.number(
                "docker_retry_backoff_seconds",
                defaults.docker_retry_backoff_seconds,
                minimum=0,
                maximum=300,
            ),
            failure_log_lines=reader.integer(
                "failure_log_lines", defaults.failure_log_lines, minimum=0, maximum=1000
            ),
            create_missing_from_image=reader.boolean(
                "create_missing_from_image", defaults.create_missing_from_image
            ),
            poll_interval_seconds=reader.number(
                "poll_interval_seconds", defaults.poll_interval_seconds, minimum=0.1, maximum=60
            ),
            startup_catch_up_minutes=reader.integer(
                "startup_catch_up_minutes",
                defaults.startup_catch_up_minutes,
                minimum=0,
                maximum=59,
            ),
            reload_config_on_change=reader.boolean(
                "reload_config_on_change", defaults.reload_config_on_change
            ),
        )

    def worker_count(self, task_count: int) -> int:
        """Thread-pool size for ``task_count`` tasks, never zero."""
        if task_count <= 0:
            return 1
        if self.max_parallel_tasks <= 0:
            return task_count
        return min(task_count, self.max_parallel_tasks)
