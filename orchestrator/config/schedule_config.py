"""When the orchestrator is allowed to act."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from orchestrator.config.field_reader import FieldReader
from orchestrator.scheduling.clock import Clock

KNOWN_KEYS = ("timezone", "hourly_minute", "daily_hour", "daily_minute")


@dataclass(frozen=True)
class ScheduleConfig:
    """Runtime schedule settings loaded from JSON."""

    timezone: str = Clock.UTC
    hourly_minute: int = 0
    daily_hour: int = 0
    daily_minute: int = 0

    @staticmethod
    def from_dict(data: Mapping[str, Any], path: str = "schedule") -> "ScheduleConfig":
        reader = FieldReader(data, path)
        reader.warn_unknown_keys(KNOWN_KEYS)

        config = ScheduleConfig(
            timezone=reader.text("timezone", Clock.UTC),
            hourly_minute=reader.integer("hourly_minute", 0, minimum=0, maximum=59),
            daily_hour=reader.integer("daily_hour", 0, minimum=0, maximum=23),
            daily_minute=reader.integer("daily_minute", 0, minimum=0, maximum=59),
        )

        # Resolve the timezone now so a bad name fails at load time with a clear
        # message rather than on the first tick, hours later.
        Clock.resolve(config.timezone)
        return config

    def create_clock(self) -> Clock:
        return Clock(self.timezone)

    @property
    def daily_time_text(self) -> str:
        return f"{self.daily_hour:02d}:{self.daily_minute:02d}"
