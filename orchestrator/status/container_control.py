"""Starts, stops and restarts one container, as asked from the dashboard."""

from __future__ import annotations

import logging

from orchestrator.docker_cli.docker_command import DockerCommand
from orchestrator.errors import DockerError
from orchestrator.status.command_types import CONTAINER_ACTIONS


class ContainerControl:
    """``docker start|stop|restart <name>`` -- the name already checked by the caller.

    No options are passed, so each container's own stop timeout applies, and
    nothing is retried: a second ``stop`` after a failed one is a decision for
    the person who asked. Every request and its outcome is logged, as the
    trail of what the dashboard did on this host.
    """

    def __init__(self, command: DockerCommand, timeout_seconds: float) -> None:
        self._command = command
        self._timeout = timeout_seconds
        self._log = logging.getLogger(self.__class__.__name__)

    def run(self, action: str, container: str) -> dict:
        if action not in CONTAINER_ACTIONS:
            raise ValueError(f"not a container action: {action!r}")

        self._log.info("Dashboard request: %s container %s", action, container)
        try:
            result = self._command.run([action, container], timeout_seconds=self._timeout, retries=0)
        except DockerError as exc:
            self._log.warning("Could not %s container %s: %s", action, container, exc)
            return _failed(str(exc))
        if not result.ok:
            error = result.err or f"exit code {result.returncode}"
            self._log.warning("Could not %s container %s: %s", action, container, error)
            return _failed(error)

        self._log.info("Done: %s container %s (%.1fs)", action, container, result.duration_seconds)
        return {"ok": True, "error": None, "truncated": False, "lines": []}


def _failed(error: str) -> dict:
    return {"ok": False, "error": error[:500], "truncated": False, "lines": []}
