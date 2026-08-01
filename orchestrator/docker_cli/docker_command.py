"""Executes Docker CLI commands with timeouts, retries and logging."""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from typing import Callable, Sequence

from orchestrator.docker_cli.command_result import CommandResult
from orchestrator.errors import DockerTimeoutError, DockerUnavailableError

_UNSET = object()


class DockerCommand:
    """The single place where this project spawns a process.

    Responsibilities kept here on purpose:

    * resolve the ``docker`` executable once, with a clear error if it is absent
    * never let a command hang forever (except where a caller explicitly asks)
    * retry commands that failed for a transient daemon-side reason
    * decode output defensively — Docker output is UTF-8, but a container's
      log line may not be, and a decode error must not crash a run
    """

    #: Substrings that mean "the daemon was momentarily unreachable". Matching
    #: on text is unpleasant, but the Docker CLI does not offer typed exit codes
    #: for these cases.
    TRANSIENT_MARKERS: tuple[str, ...] = (
        "cannot connect to the docker daemon",
        "error during connect",
        "connection refused",
        "connection reset",
        "i/o timeout",
        "context deadline exceeded",
        "temporarily unavailable",
        "the system cannot find the file specified",
        "docker daemon is not running",
        "unexpected eof",
    )

    def __init__(
        self,
        executable: str = "docker",
        timeout_seconds: float | None = 60.0,
        retries: int = 2,
        retry_backoff_seconds: float = 2.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.retries = max(0, retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._sleep = sleeper
        self._resolved_executable: str | None = None
        self._log = logging.getLogger(self.__class__.__name__)

    # ------------------------------------------------------------- executable

    def resolve_executable(self) -> str:
        if self._resolved_executable is None:
            found = shutil.which(self.executable)
            if found is None:
                raise DockerUnavailableError(
                    f"The '{self.executable}' executable was not found on PATH. "
                    "Install Docker (or Docker Desktop) and make sure the CLI is "
                    "on the PATH of the user running the orchestrator."
                )
            self._resolved_executable = found
            self._log.debug("Using Docker executable: %s", found)
        return self._resolved_executable

    # -------------------------------------------------------------------- run

    def run(
        self,
        args: Sequence[str],
        timeout_seconds: float | None | object = _UNSET,
        retries: int | None = None,
    ) -> CommandResult:
        """Run ``docker <args>`` and return the result (non-zero exits included).

        ``timeout_seconds`` defaults to the instance value; pass ``None``
        explicitly for commands that are allowed to block indefinitely, such as
        ``docker wait`` on a long job.
        """
        timeout = self.timeout_seconds if timeout_seconds is _UNSET else timeout_seconds
        attempts_allowed = (self.retries if retries is None else max(0, retries)) + 1
        command = [self.resolve_executable(), *args]

        last_result: CommandResult | None = None
        for attempt in range(1, attempts_allowed + 1):
            result = self._run_once(command, timeout, attempt)
            if result.ok or not self._is_transient(result) or attempt == attempts_allowed:
                return result

            last_result = result
            delay = self.retry_backoff_seconds * attempt
            self._log.warning(
                "Transient Docker failure (attempt %d/%d) for `%s`: %s. Retrying in %.1fs.",
                attempt,
                attempts_allowed,
                result.command_line,
                result.err or f"exit code {result.returncode}",
                delay,
            )
            if delay:
                self._sleep(delay)

        # Unreachable in practice; kept so the signature is honest.
        assert last_result is not None
        return last_result

    def run_checked(
        self,
        args: Sequence[str],
        timeout_seconds: float | None | object = _UNSET,
        retries: int | None = None,
        message: str | None = None,
    ) -> CommandResult:
        """Like :meth:`run`, but raises :class:`DockerCommandError` on failure."""
        return self.run(args, timeout_seconds=timeout_seconds, retries=retries).raise_for_status(
            message
        )

    # -------------------------------------------------------------- internals

    def _run_once(
        self,
        command: Sequence[str],
        timeout: float | None,
        attempt: int,
    ) -> CommandResult:
        self._log.debug("Running: %s (timeout=%s)", " ".join(command), timeout)
        started = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603 - fixed executable, list args
                list(command),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DockerTimeoutError(command, float(timeout or 0)) from exc
        except FileNotFoundError as exc:
            self._resolved_executable = None
            raise DockerUnavailableError(
                f"Could not execute '{command[0]}': {exc}"
            ) from exc
        except OSError as exc:
            raise DockerUnavailableError(
                f"Could not execute '{' '.join(command)}': {exc}"
            ) from exc

        return CommandResult.create(
            args=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - started,
            attempts=attempt,
        )

    def _is_transient(self, result: CommandResult) -> bool:
        return result.stderr_contains(*self.TRANSIENT_MARKERS)
