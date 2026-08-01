"""Builds a wired-up orchestrator from a configuration object."""

from __future__ import annotations

import logging

from orchestrator.config.app_config import AppConfig
from orchestrator.docker_cli.docker_command import DockerCommand
from orchestrator.docker_cli.docker_runner import DockerRunner
from orchestrator.runtime.orchestrator import Orchestrator
from orchestrator.state.daily_run_state import DailyRunState
from orchestrator.state.run_state_store import RunStateStore


class OrchestratorFactory:
    """Single place where the object graph is assembled.

    The daemon uses it to rebuild everything after a config reload, and tests
    use it to build the same graph around a fake runner.
    """

    def __init__(self, docker_executable: str = "docker") -> None:
        self.docker_executable = docker_executable
        self._log = logging.getLogger(self.__class__.__name__)

    def create_command(self, config: AppConfig) -> DockerCommand:
        runtime = config.runtime
        return DockerCommand(
            executable=self.docker_executable,
            timeout_seconds=runtime.docker_command_timeout_seconds,
            retries=runtime.docker_retries,
            retry_backoff_seconds=runtime.docker_retry_backoff_seconds,
        )

    def create_runner(self, config: AppConfig) -> DockerRunner:
        runtime = config.runtime
        return DockerRunner(
            command=self.create_command(config),
            create_missing_from_image=runtime.create_missing_from_image,
            failure_log_lines=runtime.failure_log_lines,
            default_timeout_seconds=runtime.container_timeout_seconds,
        )

    def create_state(self, config: AppConfig) -> DailyRunState:
        return DailyRunState(RunStateStore(config.state_file))

    def create(self, config: AppConfig) -> Orchestrator:
        self._log.debug("Building orchestrator for %s", config.source_path)
        return Orchestrator(
            config=config,
            runner=self.create_runner(config),
            daily_state=self.create_state(config),
        )
