"""Outcome of a single Docker CLI invocation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, Tuple

from orchestrator.errors import DockerCommandError


@dataclass(frozen=True)
class CommandResult:
    """Everything a caller may need about one finished command."""

    args: Tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    attempts: int = 1

    @staticmethod
    def create(
        args: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str,
        duration_seconds: float,
        attempts: int = 1,
    ) -> "CommandResult":
        return CommandResult(
            args=tuple(args),
            returncode=returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            duration_seconds=duration_seconds,
            attempts=attempts,
        )

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def out(self) -> str:
        return self.stdout.strip()

    @property
    def err(self) -> str:
        return self.stderr.strip()

    @property
    def command_line(self) -> str:
        return " ".join(self.args)

    def raise_for_status(self, message: str | None = None) -> "CommandResult":
        if not self.ok:
            raise DockerCommandError(
                command=self.args,
                returncode=self.returncode,
                stderr=self.stderr,
                message=message,
            )
        return self

    def stderr_contains(self, *needles: str) -> bool:
        haystack = self.stderr.lower()
        return any(needle.lower() in haystack for needle in needles)
