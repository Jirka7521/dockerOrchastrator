"""Container operations expressed in terms of the Docker CLI."""

from __future__ import annotations

import logging

from orchestrator.docker_cli.command_result import CommandResult
from orchestrator.docker_cli.container_runner import ContainerRunner
from orchestrator.docker_cli.docker_command import DockerCommand
from orchestrator.errors import (
    ContainerFailedError,
    ContainerNotFoundError,
    ContainerStartError,
    ContainerTimeoutError,
    DockerCommandError,
    DockerTimeoutError,
    DockerUnavailableError,
)

#: Statuses in which the container is doing work; starting it again is wrong.
ACTIVE_STATUSES = frozenset({"running", "restarting"})

#: Statuses from which ``docker start`` is the right move.
STARTABLE_STATUSES = frozenset({"created", "exited", "dead"})

_MISSING_MARKERS = ("no such object", "no such container", "no such image")


class DockerRunner(ContainerRunner):
    """Starts containers, waits for them, and explains what went wrong."""

    def __init__(
        self,
        command: DockerCommand | None = None,
        create_missing_from_image: bool = True,
        failure_log_lines: int = 20,
        default_timeout_seconds: float | None = None,
    ) -> None:
        self.command = command or DockerCommand()
        self.create_missing_from_image = create_missing_from_image
        self.failure_log_lines = max(0, failure_log_lines)
        self.default_timeout_seconds = default_timeout_seconds
        self.log = logging.getLogger(self.__class__.__name__)

    # ------------------------------------------------------------ engine

    def assert_available(self) -> None:
        result = self.command.run(["version", "--format", "{{.Server.Version}}"])
        if not result.ok:
            raise DockerUnavailableError(
                "Docker is not available. Check that the daemon is running and "
                "that this user may talk to it. "
                f"docker said: {result.err or f'exit code {result.returncode}'}"
            )
        self.log.debug("Docker server version: %s", result.out or "unknown")

    # --------------------------------------------------------- inspection

    def status(self, container: str) -> str | None:
        """Container status (``running``, ``exited``, …) or ``None`` if absent."""
        result = self.command.run(
            ["inspect", "--type", "container", "-f", "{{.State.Status}}", container]
        )
        if result.ok:
            return result.out.lower() or None
        if result.stderr_contains(*_MISSING_MARKERS):
            return None
        raise DockerCommandError(
            command=result.args,
            returncode=result.returncode,
            stderr=result.stderr,
            message=f"Could not inspect container '{container}'",
        )

    def exists(self, container: str) -> bool:
        return self.status(container) is not None

    def is_running(self, container: str) -> bool:
        return (self.status(container) or "") in ACTIVE_STATUSES

    def image_exists(self, image: str) -> bool:
        return self.command.run(["image", "inspect", "-f", "{{.Id}}", image]).ok

    # ------------------------------------------------------------ actions

    def start(self, container: str) -> None:
        result = self.command.run(["start", container])
        if not result.ok:
            raise ContainerStartError(
                container,
                f"Failed to start container '{container}': "
                f"{result.err or f'exit code {result.returncode}'}",
            )
        self.log.info("Started container: %s", container)

    def wait(self, container: str, timeout_seconds: float | None = None) -> int:
        """Block until the container stops and return its exit code."""
        try:
            result = self.command.run(
                ["wait", container], timeout_seconds=timeout_seconds, retries=0
            )
        except DockerTimeoutError as exc:
            raise ContainerTimeoutError(container, exc.timeout_seconds) from exc

        if not result.ok:
            if result.stderr_contains(*_MISSING_MARKERS):
                raise ContainerNotFoundError(
                    container,
                    "it disappeared while we were waiting for it — containers run by "
                    "the orchestrator must not use --rm",
                )
            raise DockerCommandError(
                command=result.args,
                returncode=result.returncode,
                stderr=result.stderr,
                message=f"Failed while waiting for container '{container}'",
            )

        return self._parse_exit_code(container, result)

    def logs(self, container: str, lines: int) -> str:
        """Best-effort tail of a container's output; never raises."""
        if lines <= 0:
            return ""
        try:
            result = self.command.run(
                ["logs", "--tail", str(lines), container], retries=0
            )
        except (DockerTimeoutError, DockerUnavailableError) as exc:
            self.log.debug("Could not read logs of '%s': %s", container, exc)
            return ""
        if not result.ok:
            return ""
        return "\n".join(
            line for line in (result.stdout + result.stderr).splitlines() if line.strip()
        )

    def create_from_image(self, name: str) -> None:
        """Create and start a container from an image with the same name."""
        if not self.create_missing_from_image:
            raise ContainerNotFoundError(
                name, "creating containers from images is disabled in the config"
            )
        if not self.image_exists(name):
            raise ContainerNotFoundError(
                name, "there is no container and no local image with this name"
            )

        self.log.info("Container '%s' does not exist; creating it from image '%s'.", name, name)
        result = self.command.run(["run", "-d", "--name", name, name])
        if result.ok:
            return

        if result.stderr_contains("already in use", "is already in progress"):
            # Another process won the race and created the container first.
            self.log.info("Container '%s' was created concurrently; starting it.", name)
            self.start(name)
            return

        raise ContainerStartError(
            name,
            f"Failed to run image '{name}' as container '{name}': "
            f"{result.err or f'exit code {result.returncode}'}",
        )

    # ----------------------------------------------------------- high level

    def run_and_wait(self, container: str, timeout_seconds: float | None = None) -> None:
        """Ensure the container is running, then wait for a successful exit.

        A container that is already running is never started a second time, so
        a tick that arrives while the previous cycle is still busy attaches to
        the running job instead of duplicating it.
        """
        timeout = timeout_seconds if timeout_seconds is not None else self.default_timeout_seconds
        self._ensure_started(container)

        exit_code = self.wait(container, timeout)
        if exit_code != 0:
            raise ContainerFailedError(
                container, exit_code, self.logs(container, self.failure_log_lines)
            )
        self.log.info("Container finished successfully: %s", container)

    def _ensure_started(self, container: str) -> None:
        status = self.status(container)

        if status is None:
            self.create_from_image(container)
            return
        if status in ACTIVE_STATUSES:
            self.log.info("Container is already active (%s), waiting: %s", status, container)
            return
        if status == "paused":
            raise ContainerStartError(
                container,
                f"Container '{container}' is paused; waiting for it would block "
                "forever. Unpause it manually (docker unpause) and re-run.",
            )
        if status not in STARTABLE_STATUSES:
            self.log.warning(
                "Container '%s' is in unexpected state '%s'; trying to start it anyway.",
                container,
                status,
            )
        self.start(container)

    @staticmethod
    def _parse_exit_code(container: str, result: CommandResult) -> int:
        # `docker wait` prints one exit code per line; a single container yields
        # exactly one, but be lenient about trailing newlines and warnings.
        for line in reversed(result.out.splitlines()):
            candidate = line.strip()
            if candidate.lstrip("-").isdigit():
                return int(candidate)
        raise DockerCommandError(
            command=result.args,
            returncode=result.returncode,
            stderr=result.stderr,
            message=(
                f"`docker wait {container}` produced unexpected output: {result.out!r}"
            ),
        )
