"""Decides whether a given moment is a tick, and when the next one is."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps config -> scheduling one-way
    from orchestrator.config.schedule_config import ScheduleConfig

SLOT_FORMAT = "%Y-%m-%dT%H:%M"


class ScheduleEvaluator:
    """Pure schedule arithmetic for one :class:`ScheduleConfig`.

    Everything here is a function of the time it is handed, so the daemon's
    behaviour around midnight, DST and long-running cycles is testable without
    waiting for the wall clock.
    """

    def __init__(self, schedule: "ScheduleConfig") -> None:
        self.schedule = schedule

    # ------------------------------------------------------------- hourly

    def is_hourly_tick(self, now: datetime) -> bool:
        return now.minute == self.schedule.hourly_minute

    def current_slot(self, now: datetime) -> datetime:
        """The most recent tick moment at or before ``now``."""
        slot = now.replace(
            minute=self.schedule.hourly_minute, second=0, microsecond=0
        )
        if slot > now:
            slot -= timedelta(hours=1)
        return slot

    def next_slot(self, now: datetime) -> datetime:
        """The first tick moment strictly after ``now``."""
        return self.current_slot(now) + timedelta(hours=1)

    def seconds_until_next_slot(self, now: datetime) -> float:
        """Seconds to the next tick, clamped so a clock jump cannot stall us."""
        delta = self.next_slot(now).timestamp() - now.timestamp()
        # A backwards DST shift can make the next wall-clock tick up to two
        # hours away; a forwards shift can make it negative.
        return max(0.0, min(delta, 2 * 3600.0))

    @staticmethod
    def slot_key(moment: datetime) -> str:
        """Stable identifier for one hourly execution window."""
        return moment.strftime(SLOT_FORMAT)

    def minutes_since_slot(self, now: datetime) -> float:
        return (now.timestamp() - self.current_slot(now).timestamp()) / 60.0

    # -------------------------------------------------------------- daily

    def is_past_daily_time(self, now: datetime) -> bool:
        return (now.hour, now.minute) >= (
            self.schedule.daily_hour,
            self.schedule.daily_minute,
        )

    def is_daily_due(self, now: datetime, last_daily_date: date | None) -> bool:
        """Whether this tick owns today's daily snapshot.

        Daily snapshots run on the first tick at or after the configured daily
        time, once per calendar day. An exact hour/minute match is deliberately
        not required: a cycle that overruns its slot would otherwise swallow the
        daily window and skip that day entirely.
        """
        if not self.is_past_daily_time(now):
            return False
        return last_daily_date != now.date()
