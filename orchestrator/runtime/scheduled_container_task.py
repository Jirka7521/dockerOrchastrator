"""Runs one standalone scheduled container."""

from __future__ import annotations

from orchestrator.config.scheduled_container_config import ScheduledContainerConfig
from orchestrator.docker_cli.container_runner import ContainerRunner
from orchestrator.runtime.task import Task


class ScheduledContainerTask(Task):
    """A container that runs on its own period, independent of any pipeline."""

    kind = "scheduled"

    def __init__(
        self,
        container: ScheduledContainerConfig,
        runner: ContainerRunner,
    ) -> None:
        super().__init__(container.name)
        self.container = container
        self.runner = runner

    def execute(self) -> None:
        self.log.info(
            "Starting scheduled container: %s (%s)",
            self.container.name,
            self.container.period,
        )
        self.runner.run_and_wait(self.container.name, self.container.timeout_seconds)
