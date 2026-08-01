"""A standalone container that runs on its own period."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from orchestrator.config.field_reader import FieldReader
from orchestrator.docker_cli.container_name import ContainerName
from orchestrator.errors import ConfigError
from orchestrator.scheduling.periods import Period

KNOWN_KEYS = ("name", "period", "enabled", "timeout_seconds")


@dataclass(frozen=True)
class ScheduledContainerConfig:
    """Container configured to run on a named schedule."""

    name: str
    period: str
    interval_hours: int | None = None
    enabled: bool = True
    timeout_seconds: float | None = None

    @staticmethod
    def from_dict(
        data: Mapping[str, Any],
        index: int = 0,
        path: str = "scheduled_containers",
    ) -> "ScheduledContainerConfig":
        item_path = f"{path}[{index}]"
        reader = FieldReader(data, item_path)
        reader.warn_unknown_keys(KNOWN_KEYS)

        period = Period.normalize(reader.text("period"))
        try:
            interval_hours = Period.parse_interval_hours(period)
        except ValueError as exc:
            raise ConfigError(f"{item_path}.period {exc}") from exc

        return ScheduledContainerConfig(
            name=ContainerName.validate(reader.text("name"), f"{item_path}.name"),
            period=period,
            interval_hours=interval_hours,
            enabled=reader.boolean("enabled", True),
            timeout_seconds=reader.optional_number("timeout_seconds", minimum=1),
        )

    @property
    def description(self) -> str:
        return f"{self.name} ({self.period})"
