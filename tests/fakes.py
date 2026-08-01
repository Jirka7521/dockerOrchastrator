"""Test doubles shared by the test modules."""

from __future__ import annotations

import threading
from typing import Any, Dict, Iterable, List, Sequence

from orchestrator.docker_cli.command_result import CommandResult
from orchestrator.docker_cli.container_runner import ContainerRunner
from orchestrator.docker_cli.docker_command import DockerCommand
from orchestrator.errors import ContainerFailedError, DockerUnavailableError


class FakeContainerRunner(ContainerRunner):
    """Records which containers were run, and fails the ones you ask it to."""

    def __init__(
        self,
        failing: Iterable[str] = (),
        available: bool = True,
    ) -> None:
        self.failing = set(failing)
        self.available = available
        self.started: List[str] = []
        self.availability_checks = 0
        self._lock = threading.Lock()

    def assert_available(self) -> None:
        self.availability_checks += 1
        if not self.available:
            raise DockerUnavailableError("docker is unavailable in this test")

    def run_and_wait(self, container: str, timeout_seconds: float | None = None) -> None:
        with self._lock:
            self.started.append(container)
        if container in self.failing:
            raise ContainerFailedError(container, 1, "boom")


class FakeDockerCommand(DockerCommand):
    """A :class:`DockerCommand` that replays scripted results instead of running docker."""

    def __init__(self, responses: Dict[str, Any] | None = None) -> None:
        super().__init__(sleeper=lambda _seconds: None)
        self.responses = responses or {}
        self.calls: List[Sequence[str]] = []

    def resolve_executable(self) -> str:  # pragma: no cover - trivial
        return "docker"

    def run(self, args, timeout_seconds=None, retries=None) -> CommandResult:  # type: ignore[override]
        args = list(args)
        self.calls.append(args)
        key = " ".join(args[:2])
        response = self.responses.get(key, self.responses.get(args[0]))
        if callable(response):
            response = response(args)
        if response is None:
            response = (0, "", "")
        returncode, stdout, stderr = response
        return CommandResult.create(["docker", *args], returncode, stdout, stderr, 0.0)
