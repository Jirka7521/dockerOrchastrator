"""The unit of work the orchestrator schedules."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from orchestrator.runtime.task_result import TaskResult


class Task(ABC):
    """One independent piece of work in a cycle.

    Subclasses implement :meth:`execute` and may raise freely; :meth:`run`
    turns any outcome into a :class:`TaskResult`, so a single broken container
    can never abort the whole cycle.
    """

    #: Short category used in logs and reports, e.g. ``pipeline``.
    kind: str = "task"

    def __init__(self, name: str) -> None:
        self.name = name
        self.log = logging.getLogger(self.__class__.__name__)

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.name}"

    @abstractmethod
    def execute(self) -> None:
        """Do the work. Raise to signal failure."""

    def run(self) -> TaskResult:
        started_at = datetime.now(timezone.utc)
        started = time.monotonic()
        try:
            self.execute()
        except Exception as exc:  # noqa: BLE001 - converted into a result
            duration = time.monotonic() - started
            self.log.error("[%s] failed after %.1fs: %s", self.label, duration, exc)
            self.log.debug("[%s] failure detail", self.label, exc_info=True)
            return TaskResult.failed(self.kind, self.name, started_at, duration, exc)

        duration = time.monotonic() - started
        self.log.info("[%s] completed in %.1fs", self.label, duration)
        return TaskResult.succeeded(self.kind, self.name, started_at, duration)
