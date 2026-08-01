"""Picks the standalone containers that are due on a given tick."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Iterable, List

from orchestrator.scheduling.periods import Period

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps config -> scheduling one-way
    from orchestrator.config.scheduled_container_config import ScheduledContainerConfig


class ScheduledContainerSelector:
    """Filters configured scheduled containers down to the ones due now."""

    def __init__(self, containers: Iterable["ScheduledContainerConfig"]) -> None:
        self.containers = list(containers)
        self._log = logging.getLogger(self.__class__.__name__)

    def due(self, now: datetime, daily_due: bool) -> List["ScheduledContainerConfig"]:
        return [
            container
            for container in self.containers
            if container.enabled and self._is_due(container, now, daily_due)
        ]

    def _is_due(
        self,
        container: "ScheduledContainerConfig",
        now: datetime,
        daily_due: bool,
    ) -> bool:
        if container.period == Period.HOURLY:
            return True
        if container.period == Period.DAILY:
            # Daily containers ride along with the daily snapshot decision so
            # that "once per day" means the same thing everywhere.
            return daily_due
        if container.interval_hours:
            return now.hour % container.interval_hours == 0

        self._log.warning(
            "Scheduled container '%s' has unrecognised period %r; skipping it.",
            container.name,
            container.period,
        )
        return False
