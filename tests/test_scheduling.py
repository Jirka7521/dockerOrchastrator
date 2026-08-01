"""Tick arithmetic and scheduled-container selection."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from orchestrator.config.schedule_config import ScheduleConfig
from orchestrator.config.scheduled_container_config import ScheduledContainerConfig
from orchestrator.scheduling.clock import Clock
from orchestrator.scheduling.schedule_evaluator import ScheduleEvaluator
from orchestrator.scheduling.scheduled_container_selector import (
    ScheduledContainerSelector,
)
from orchestrator.errors import TimezoneError


def moment(hour: int, minute: int, day: int = 15) -> datetime:
    return datetime(2026, 3, day, hour, minute, tzinfo=timezone.utc)


class ScheduleEvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evaluator = ScheduleEvaluator(
            ScheduleConfig(timezone="UTC", hourly_minute=0, daily_hour=2, daily_minute=30)
        )

    def test_hourly_tick_matches_configured_minute(self) -> None:
        self.assertTrue(self.evaluator.is_hourly_tick(moment(13, 0)))
        self.assertFalse(self.evaluator.is_hourly_tick(moment(13, 1)))

    def test_current_slot_is_the_most_recent_tick(self) -> None:
        self.assertEqual(self.evaluator.current_slot(moment(13, 42)), moment(13, 0))
        self.assertEqual(self.evaluator.current_slot(moment(13, 0)), moment(13, 0))

    def test_current_slot_falls_back_to_the_previous_hour(self) -> None:
        evaluator = ScheduleEvaluator(ScheduleConfig(hourly_minute=30))

        self.assertEqual(evaluator.current_slot(moment(13, 10)), moment(12, 30))

    def test_next_slot_and_countdown(self) -> None:
        self.assertEqual(self.evaluator.next_slot(moment(13, 42)), moment(14, 0))
        self.assertAlmostEqual(
            self.evaluator.seconds_until_next_slot(moment(13, 42)), 18 * 60, places=3
        )

    def test_minutes_since_slot(self) -> None:
        self.assertAlmostEqual(self.evaluator.minutes_since_slot(moment(13, 7)), 7.0)

    def test_daily_is_due_once_per_day_after_the_daily_time(self) -> None:
        self.assertFalse(self.evaluator.is_daily_due(moment(1, 0), None))
        self.assertTrue(self.evaluator.is_daily_due(moment(3, 0), None))
        self.assertTrue(self.evaluator.is_daily_due(moment(3, 0), date(2026, 3, 14)))
        self.assertFalse(self.evaluator.is_daily_due(moment(3, 0), date(2026, 3, 15)))

    def test_daily_catches_up_later_in_the_day(self) -> None:
        # A cycle that overran the 02:30 window must not cost the day's snapshot.
        self.assertTrue(self.evaluator.is_daily_due(moment(23, 0), date(2026, 3, 14)))


class ScheduledContainerSelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hourly = ScheduledContainerConfig("h", "hourly")
        self.daily = ScheduledContainerConfig("d", "daily")
        self.every_six = ScheduledContainerConfig("s", "every_6_hours", interval_hours=6)
        self.disabled = ScheduledContainerConfig("off", "hourly", enabled=False)
        self.selector = ScheduledContainerSelector(
            [self.hourly, self.daily, self.every_six, self.disabled]
        )

    def test_hourly_always_runs_and_disabled_never_does(self) -> None:
        due = self.selector.due(moment(13, 0), daily_due=False)

        self.assertIn(self.hourly, due)
        self.assertNotIn(self.disabled, due)

    def test_daily_follows_the_daily_decision(self) -> None:
        self.assertNotIn(self.daily, self.selector.due(moment(13, 0), daily_due=False))
        self.assertIn(self.daily, self.selector.due(moment(13, 0), daily_due=True))

    def test_every_n_hours_runs_on_matching_hours(self) -> None:
        self.assertIn(self.every_six, self.selector.due(moment(12, 0), daily_due=False))
        self.assertNotIn(self.every_six, self.selector.due(moment(13, 0), daily_due=False))


class ClockTests(unittest.TestCase):
    def test_utc_always_resolves(self) -> None:
        self.assertIsNotNone(Clock("UTC").now().tzinfo)

    def test_unknown_zone_raises_timezone_error(self) -> None:
        with self.assertRaises(TimezoneError):
            Clock("Nowhere/Special")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
