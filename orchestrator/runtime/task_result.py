"""The outcome of one task in one cycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TaskResult:
    """What happened to a single pipeline or scheduled container."""

    kind: str
    name: str
    success: bool
    duration_seconds: float
    started_at: datetime
    error: BaseException | None = None

    @staticmethod
    def succeeded(
        kind: str, name: str, started_at: datetime, duration_seconds: float
    ) -> "TaskResult":
        return TaskResult(
            kind=kind,
            name=name,
            success=True,
            duration_seconds=duration_seconds,
            started_at=started_at,
        )

    @staticmethod
    def failed(
        kind: str,
        name: str,
        started_at: datetime,
        duration_seconds: float,
        error: BaseException,
    ) -> "TaskResult":
        return TaskResult(
            kind=kind,
            name=name,
            success=False,
            duration_seconds=duration_seconds,
            started_at=started_at,
            error=error,
        )

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.name}"

    def summary(self) -> str:
        state = "ok" if self.success else "FAILED"
        line = f"{self.label} {state} in {self.duration_seconds:.1f}s"
        if self.error is not None:
            line = f"{line} — {self.error}"
        return line
