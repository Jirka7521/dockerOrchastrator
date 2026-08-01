"""Exception hierarchy for the orchestrator.

Every failure this package raises on purpose derives from
:class:`OrchestratorError`. That lets callers separate "something we
anticipated and can describe" from genuine bugs, and it removes the need to
match on error message text to decide what went wrong.

This is the one module that intentionally holds more than one class: the
exceptions are declarations rather than behaviour, and splitting a hierarchy
across a dozen three-line files makes it harder to read, not easier.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence


class OrchestratorError(Exception):
    """Base class for all errors raised deliberately by this package."""


class ConfigError(OrchestratorError):
    """The configuration file is missing, malformed or semantically invalid."""


class StateError(OrchestratorError):
    """The persistent run state could not be read or written."""


class TimezoneError(ConfigError):
    """The configured timezone cannot be resolved on this machine."""


class DockerError(OrchestratorError):
    """Base class for failures that originate from the Docker CLI."""


class DockerUnavailableError(DockerError):
    """The Docker CLI is missing or the daemon is not reachable."""


class DockerCommandError(DockerError):
    """A ``docker`` invocation returned a non-zero exit code."""

    def __init__(
        self,
        command: Sequence[str],
        returncode: int,
        stderr: str = "",
        message: str | None = None,
    ) -> None:
        self.command = tuple(command)
        self.returncode = returncode
        self.stderr = (stderr or "").strip()

        text = message or f"`{' '.join(self.command)}` failed with exit code {returncode}"
        if self.stderr:
            text = f"{text}: {self.stderr}"
        super().__init__(text)


class DockerTimeoutError(DockerError):
    """A ``docker`` invocation did not finish within its timeout."""

    def __init__(self, command: Sequence[str], timeout_seconds: float) -> None:
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"`{' '.join(self.command)}` timed out after {timeout_seconds:g}s"
        )


class ContainerError(DockerError):
    """Base class for failures tied to one specific container."""

    def __init__(self, container: str, message: str) -> None:
        self.container = container
        super().__init__(message)


class ContainerNotFoundError(ContainerError):
    """No container (and no fallback image) exists with the configured name."""

    def __init__(self, container: str, detail: str = "") -> None:
        message = f"Container '{container}' does not exist"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(container, message)


class ContainerStartError(ContainerError):
    """The container exists but could not be started."""


class ContainerTimeoutError(ContainerError):
    """The container was still running when its timeout elapsed."""

    def __init__(self, container: str, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(
            container,
            f"Container '{container}' did not finish within {timeout_seconds:g}s "
            "(it was left running)",
        )


class ContainerFailedError(ContainerError):
    """The container ran to completion but exited with a non-zero status."""

    def __init__(self, container: str, exit_code: int, logs: str = "") -> None:
        self.exit_code = exit_code
        self.logs = logs.strip()

        message = f"Container '{container}' exited with code {exit_code}"
        if self.logs:
            message = f"{message}. Last log lines:\n{self.logs}"
        super().__init__(container, message)


class TaskGroupError(OrchestratorError):
    """One or more tasks in a parallel group failed.

    Carries every individual failure instead of only the first one, so a run
    that loses two containers reports two containers.
    """

    def __init__(self, failures: Mapping[str, BaseException]) -> None:
        self.failures = dict(failures)
        summary = "; ".join(f"{name}: {exc}" for name, exc in self.failures.items())
        super().__init__(f"{len(self.failures)} task(s) failed: {summary}")

    @property
    def names(self) -> Iterable[str]:
        return self.failures.keys()
