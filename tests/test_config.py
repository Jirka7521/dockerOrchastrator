"""Configuration parsing and validation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.config.app_config import AppConfig
from orchestrator.config.config_loader import ConfigLoader
from orchestrator.config.scheduled_container_config import ScheduledContainerConfig
from orchestrator.errors import ConfigError

MINIMAL = {
    "schedule": {"timezone": "UTC", "hourly_minute": 5, "daily_hour": 2, "daily_minute": 30},
    "pipelines": {
        "example": {
            "sync": "exampleSync",
            "hourly_snapshot": "exampleHourly",
            "daily_snapshot": "exampleDaily",
        }
    },
}


class AppConfigTests(unittest.TestCase):
    def test_minimal_config_is_accepted(self) -> None:
        config = AppConfig.from_dict(MINIMAL)

        self.assertEqual(config.schedule.hourly_minute, 5)
        self.assertEqual(len(config.pipelines), 1)
        self.assertFalse(config.pipelines[0].allow_sync_failure)
        self.assertTrue(config.pipelines[0].enabled)
        self.assertEqual(config.runtime.docker_retries, 2)

    def test_defaults_fill_in_missing_schedule(self) -> None:
        config = AppConfig.from_dict({"pipelines": MINIMAL["pipelines"]})

        self.assertEqual(config.schedule.timezone, "UTC")
        self.assertEqual(config.schedule.hourly_minute, 0)

    def test_rejects_out_of_range_minute(self) -> None:
        raw = json.loads(json.dumps(MINIMAL))
        raw["schedule"]["hourly_minute"] = 60

        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_dict(raw)
        self.assertIn("hourly_minute", str(ctx.exception))

    def test_rejects_missing_container_name(self) -> None:
        raw = json.loads(json.dumps(MINIMAL))
        del raw["pipelines"]["example"]["sync"]

        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_dict(raw)
        self.assertIn("pipelines.example.sync", str(ctx.exception))

    def test_rejects_duplicate_container_names(self) -> None:
        raw = json.loads(json.dumps(MINIMAL))
        raw["pipelines"]["example"]["daily_snapshot"] = "exampleHourly"

        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_dict(raw)
        self.assertIn("more than once", str(ctx.exception))

    def test_rejects_container_name_that_looks_like_a_flag(self) -> None:
        raw = json.loads(json.dumps(MINIMAL))
        raw["pipelines"]["example"]["sync"] = "--rm"

        with self.assertRaises(ConfigError):
            AppConfig.from_dict(raw)

    def test_rejects_empty_config(self) -> None:
        with self.assertRaises(ConfigError):
            AppConfig.from_dict({"schedule": {}})

    def test_unknown_timezone_is_reported_clearly(self) -> None:
        raw = json.loads(json.dumps(MINIMAL))
        raw["schedule"]["timezone"] = "Mars/Olympus_Mons"

        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_dict(raw)
        self.assertIn("Mars/Olympus_Mons", str(ctx.exception))


class ScheduledContainerConfigTests(unittest.TestCase):
    def test_every_n_hours_is_parsed(self) -> None:
        container = ScheduledContainerConfig.from_dict(
            {"name": "maintenance", "period": "every_6_hours"}
        )
        self.assertEqual(container.interval_hours, 6)

    def test_period_must_divide_the_day(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            ScheduledContainerConfig.from_dict({"name": "x", "period": "every_5_hours"})
        self.assertIn("divide 24", str(ctx.exception))

    def test_unknown_period_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            ScheduledContainerConfig.from_dict({"name": "x", "period": "fortnightly"})


class ConfigLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.directory = Path(self._temp.name)
        self.path = self.directory / "config.json"
        self.addCleanup(self._temp.cleanup)

    def write(self, data: dict, encoding: str = "utf-8") -> None:
        self.path.write_text(json.dumps(data, indent=2), encoding=encoding)

    def test_loads_and_resolves_state_file_next_to_config(self) -> None:
        self.write(MINIMAL)

        config = ConfigLoader(self.path).load()

        self.assertEqual(config.state_file, (self.directory / "config_state.json").resolve())

    def test_missing_file_gives_actionable_error(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            ConfigLoader(self.directory / "nope.json").load()
        self.assertIn("not found", str(ctx.exception))

    def test_invalid_json_reports_position(self) -> None:
        self.path.write_text("{ not json", encoding="utf-8")

        with self.assertRaises(ConfigError) as ctx:
            ConfigLoader(self.path).load()
        self.assertIn("line 1", str(ctx.exception))

    def test_byte_order_mark_is_tolerated(self) -> None:
        self.write(MINIMAL, encoding="utf-8-sig")

        config = ConfigLoader(self.path).load()

        self.assertEqual(len(config.pipelines), 1)

    def test_reload_returns_none_until_the_file_changes(self) -> None:
        self.write(MINIMAL)
        loader = ConfigLoader(self.path)
        loader.load()

        self.assertIsNone(loader.reload_if_changed())

        changed = json.loads(json.dumps(MINIMAL))
        changed["schedule"]["hourly_minute"] = 17
        self.write(changed)

        reloaded = loader.reload_if_changed()
        self.assertIsNotNone(reloaded)
        assert reloaded is not None
        self.assertEqual(reloaded.schedule.hourly_minute, 17)

    def test_broken_reload_keeps_the_previous_config(self) -> None:
        self.write(MINIMAL)
        loader = ConfigLoader(self.path)
        loader.load()

        self.path.write_text("{ broken", encoding="utf-8")

        self.assertIsNone(loader.reload_if_changed())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
