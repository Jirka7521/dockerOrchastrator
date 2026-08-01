"""The container-execution contract the orchestrator depends on."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Iterable

from orchestrator.errors import TaskGroupError


class ContainerRunner(ABC):
    """Abstract "start this container and wait for it" capability.

    The orchestrator talks to this interface rather than to Docker directly,
    which keeps the scheduling logic testable and leaves room for a different
    backend later.
    """

    @abstractmethod
    def assert_available(self) -> None:
        """Raise if the container engine cannot be used right now."""

    @abstractmethod
    def run_and_wait(self, container: str, timeout_seconds: float | None = None) -> None:
        """Start ``container`` if needed and block until it exits successfully."""

    def run_parallel_and_wait(
        self,
        containers: Iterable[str],
        timeout_seconds: float | None = None,
        max_workers: int | None = None,
    ) -> None:
        """Run several containers at once, waiting for all of them.

        Unlike a fail-fast implementation, every container is awaited even when
        one has already failed: a snapshot that succeeded should be reported as
        such, and a run that lost two containers should say so.
        """
        names = list(containers)
        if not names:
            return
        if len(names) == 1:
            self.run_and_wait(names[0], timeout_seconds)
            return

        workers = max(1, min(len(names), max_workers or len(names)))
        failures: Dict[str, BaseException] = {}

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="container") as pool:
            futures = {
                pool.submit(self.run_and_wait, name, timeout_seconds): name
                for name in names
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    future.result()
                except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                    logging.getLogger(self.__class__.__name__).error(
                        "Container '%s' failed: %s", name, exc
                    )
                    failures[name] = exc

        if failures:
            raise TaskGroupError(failures)
