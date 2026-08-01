"""End-to-end behaviour of one cycle, with Docker replaced by a fake."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from orchestrator.config.app_config import AppConfig
from orchestrator.runtime.orchestrator import Orchestrator
from orchestrator.state.daily_run_state import DailyRunState
from tests.fakes import FakeContainerRunner

BASE_CONFIG = {
    "schedule": {"timezone": "UTC", "hourly_minute": 0, "daily_hour": 2, "daily_minute": 0},
    "pipelines": {
        "alpha": {
            "sync": "alphaSync",
            "hourly_snapshot": "alphaHourly",
            "daily_snapshot": "alphaDaily",
        }
    },
    "scheduled_containers": [{"name": "maintenance", "period": "daily"}],
}


def moment(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 3, 15, hour, minute, tzinfo=timezone.utc)


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.state_path = Path(self._temp.name) / "state.json"

    def build(self, raw: dict | None = None, failing: tuple[str, ...] = ()) -> tuple:
        config = AppConfig.from_dict(raw or BASE_CONFIG)
        runner = FakeContainerRunner(failing=failing)
        orchestrator = Orchestrator(
            config=config,
            runner=runner,
            daily_state=DailyRunState(self.state_path),
        )
        return orchestrator, runner

    # ------------------------------------------------------------- scheduling

    def test_skips_when_the_minute_does_not_match(self) -> None:
        orchestrator, runner = self.build()

        report = orchestrator.run(now=moment(13, 17))

        self.assertTrue(report.was_skipped)
        self.assertEqual(runner.started, [])
        self.assertEqual(report.exit_code, 0)

    def test_hourly_cycle_runs_sync_then_hourly_snapshot(self) -> None:
        orchestrator, runner = self.build()
        DailyRunState(self.state_path).record_daily_date(date(2026, 3, 15))

        report = orchestrator.run(now=moment(13))

        self.assertEqual(runner.started, ["alphaSync", "alphaHourly"])
        self.assertFalse(report.daily_snapshots)
        self.assertTrue(report.ok)

    def test_daily_cycle_adds_the_daily_snapshot_and_claims_the_day(self) -> None:
        orchestrator, runner = self.build()

        report = orchestrator.run(now=moment(3))

        self.assertTrue(report.daily_snapshots)
        self.assertIn("alphaDaily", runner.started)
        self.assertIn("maintenance", runner.started)
        self.assertEqual(
            DailyRunState(self.state_path).last_daily_date(), date(2026, 3, 15)
        )

    def test_daily_runs_only_once_per_day(self) -> None:
        orchestrator, runner = self.build()
        orchestrator.run(now=moment(3))
        runner.started.clear()

        orchestrator.run(now=moment(4))

        self.assertNotIn("alphaDaily", runner.started)
        self.assertNotIn("maintenance", runner.started)

    def test_force_daily_overrides_the_state(self) -> None:
        orchestrator, runner = self.build()
        DailyRunState(self.state_path).record_daily_date(date(2026, 3, 15))

        orchestrator.run(now=moment(13), force_daily=True)

        self.assertIn("alphaDaily", runner.started)

    # --------------------------------------------------------------- failures

    def test_failed_sync_still_snapshots_and_is_reported(self) -> None:
        orchestrator, runner = self.build(failing=("alphaSync",))
        DailyRunState(self.state_path).record_daily_date(date(2026, 3, 15))

        report = orchestrator.run(now=moment(13))

        self.assertIn("alphaHourly", runner.started)
        self.assertFalse(report.ok)
        self.assertEqual([result.name for result in report.failed], ["alpha"])
        self.assertEqual(report.exit_code, 1)

    def test_allowed_sync_failure_is_not_a_failure(self) -> None:
        raw = {
            **BASE_CONFIG,
            "pipelines": {
                "alpha": {**BASE_CONFIG["pipelines"]["alpha"], "allow_sync_failure": True}
            },
        }
        orchestrator, runner = self.build(raw, failing=("alphaSync",))
        DailyRunState(self.state_path).record_daily_date(date(2026, 3, 15))

        report = orchestrator.run(now=moment(13))

        self.assertIn("alphaHourly", runner.started)
        self.assertTrue(report.ok)

    def test_one_broken_pipeline_does_not_stop_the_others(self) -> None:
        raw = {
            **BASE_CONFIG,
            "pipelines": {
                "alpha": BASE_CONFIG["pipelines"]["alpha"],
                "beta": {
                    "sync": "betaSync",
                    "hourly_snapshot": "betaHourly",
                    "daily_snapshot": "betaDaily",
                },
            },
        }
        orchestrator, runner = self.build(raw, failing=("alphaHourly",))
        DailyRunState(self.state_path).record_daily_date(date(2026, 3, 15))

        report = orchestrator.run(now=moment(13))

        self.assertIn("betaHourly", runner.started)
        self.assertEqual({result.name for result in report.failed}, {"alpha"})

    def test_unavailable_docker_aborts_the_cycle_cleanly(self) -> None:
        orchestrator, runner = self.build()
        runner.available = False

        report = orchestrator.run(now=moment(13))

        self.assertEqual(runner.started, [])
        self.assertIsNotNone(report.fatal_error)
        self.assertEqual(report.exit_code, 1)

    def test_state_is_not_claimed_when_docker_is_unavailable(self) -> None:
        orchestrator, runner = self.build()
        runner.available = False

        orchestrator.run(now=moment(3))

        self.assertIsNone(DailyRunState(self.state_path).last_daily_date())

    # --------------------------------------------------------------- dry run

    def test_dry_run_touches_nothing(self) -> None:
        orchestrator, runner = self.build()

        report = orchestrator.run(now=moment(3), dry_run=True)

        self.assertEqual(runner.started, [])
        self.assertEqual(runner.availability_checks, 0)
        self.assertTrue(report.dry_run)
        self.assertIsNone(DailyRunState(self.state_path).last_daily_date())

    def test_disabled_pipeline_is_skipped(self) -> None:
        raw = {
            **BASE_CONFIG,
            "pipelines": {
                "alpha": {**BASE_CONFIG["pipelines"]["alpha"], "enabled": False}
            },
            "scheduled_containers": [{"name": "maintenance", "period": "hourly"}],
        }
        orchestrator, runner = self.build(raw)

        orchestrator.run(now=moment(13))

        self.assertEqual(runner.started, ["maintenance"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
