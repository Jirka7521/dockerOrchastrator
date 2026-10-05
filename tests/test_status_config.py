"""The optional status_reporter config section."""

from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path

from orchestrator.config.app_config import AppConfig
from orchestrator.errors import ConfigError
from orchestrator.status.status_config import StatusReporterConfig


class StatusConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.base = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _key_file(self, key: bytes = bytes(range(32)), mode: int = 0o600) -> Path:
        path = self.base / "agent.key"
        path.write_text(base64.b64encode(key).decode("ascii") + "\n", encoding="ascii")
        os.chmod(path, mode)
        return path

    def test_it_is_disabled_unless_configured(self) -> None:
        config = StatusReporterConfig.from_dict({}, self.base)

        self.assertFalse(config.enabled)
        self.assertEqual(StatusReporterConfig(), config)

    def test_an_existing_config_file_keeps_working(self) -> None:
        config = AppConfig.from_dict({"scheduled_containers": [{"name": "job", "period": "daily"}]})

        self.assertFalse(config.status_reporter.enabled)

    def test_a_status_only_config_is_valid(self) -> None:
        self._key_file()
        config = AppConfig.from_dict(
            {"status_reporter": {"enabled": True, "api_url": "http://192.168.130.2:8080", "shared_key_file": "agent.key"}},
            source_path=self.base / "config.json",
        )

        self.assertTrue(config.status_reporter.enabled)
        self.assertEqual(self.base.resolve() / "agent.key", config.status_reporter.shared_key_file)

    def test_enabled_requires_a_url_and_a_key_file(self) -> None:
        with self.assertRaises(ConfigError):
            StatusReporterConfig.from_dict({"enabled": True, "shared_key_file": "k"}, self.base)
        with self.assertRaises(ConfigError):
            StatusReporterConfig.from_dict({"enabled": True, "api_url": "http://api:8080"}, self.base)

    def _enabled(self, **extra) -> StatusReporterConfig:
        self._key_file()
        section = {"enabled": True, "api_url": "http://192.168.130.2:8080", "shared_key_file": "agent.key", **extra}
        return StatusReporterConfig.from_dict(section, self.base)

    def test_only_logs_are_allowed_unless_listed(self) -> None:
        self.assertEqual(("logs",), self._enabled().allowed_commands)
        self.assertIn("allowed commands: logs", self._enabled().describe())

    def test_allowed_commands_are_checked_and_kept_in_order(self) -> None:
        config = self._enabled(allowed_commands=["restart", "logs", "restart", "reboot"])

        self.assertEqual(("restart", "logs", "reboot"), config.allowed_commands)
        self.assertEqual((), self._enabled(allowed_commands=[]).allowed_commands)
        for bad in (["exec"], ["logs", "rm"], [None], "logs"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                self._enabled(allowed_commands=bad)

    def test_container_actions_get_a_bounded_timeout(self) -> None:
        self.assertEqual(120, self._enabled().container_action_timeout_seconds)
        with self.assertRaises(ConfigError):
            self._enabled(container_action_timeout_seconds=5)

    def test_urls_must_be_http(self) -> None:
        for url in ("ftp://api", "api:8080", "file:///etc/passwd", "http://"):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                StatusReporterConfig.from_dict({"enabled": True, "api_url": url, "shared_key_file": "k"}, self.base)

    def test_ping_targets_are_validated(self) -> None:
        with self.assertRaises(ConfigError):
            StatusReporterConfig.from_dict(
                {"enabled": True, "api_url": "http://api", "shared_key_file": "k", "ping_targets": ["1.1.1.1; rm -rf /"]},
                self.base,
            )

    def test_the_key_is_loaded_from_a_private_file(self) -> None:
        self._key_file()
        config = StatusReporterConfig.from_dict(
            {"enabled": True, "api_url": "http://api", "shared_key_file": "agent.key"}, self.base
        )

        self.assertEqual(bytes(range(32)), config.load_key())

    @unittest.skipUnless(os.name == "posix", "file modes are POSIX")
    def test_a_key_file_other_users_can_read_is_refused(self) -> None:
        self._key_file(mode=0o644)
        config = StatusReporterConfig.from_dict(
            {"enabled": True, "api_url": "http://api", "shared_key_file": "agent.key"}, self.base
        )

        with self.assertRaisesRegex(ConfigError, "chmod 600"):
            config.load_key()

    def test_a_short_key_is_refused(self) -> None:
        self._key_file(key=bytes(16))
        config = StatusReporterConfig.from_dict(
            {"enabled": True, "api_url": "http://api", "shared_key_file": "agent.key"}, self.base
        )

        with self.assertRaisesRegex(ConfigError, "at least 32"):
            config.load_key()


if __name__ == "__main__":
    unittest.main()
