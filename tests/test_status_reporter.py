"""StatusReporter threads end to end, against a fake API client and fake host."""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import unittest
from pathlib import Path
from typing import List

from orchestrator.status.status_config import StatusReporterConfig
from orchestrator.status.status_reporter import StatusReporter
from tests.fakes import FakeDockerCommand
from tests.status_support import FakeHost

CONTAINER = {
    "Id": "a" * 64,
    "Name": "/temp-fe",
    "Created": "2026-07-28T15:07:14Z",
    "State": {"Status": "running", "ExitCode": 0, "StartedAt": "2026-07-28T15:07:14Z", "FinishedAt": "0001-01-01T00:00:00Z"},
    "Config": {"Image": "temp-fe:latest", "Labels": {}},
    "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
    "NetworkSettings": {"Networks": {}},
}


class FakeClient:
    def __init__(self) -> None:
        self.reports: List[tuple] = []
        self.results: List[tuple] = []
        self.commands = [{"id": "c" * 32, "type": "logs", "container": "temp-fe", "tail": 5}]
        self.lock = threading.Lock()

    def send_report(self, method: str, endpoint: str, payload: dict) -> None:
        with self.lock:
            self.reports.append((method, endpoint, payload))

    def poll_commands(self, wait_seconds: int) -> list:
        time.sleep(0.05)
        with self.lock:
            commands, self.commands = self.commands, []
        return commands

    def send_command_result(self, command_id: str, result: dict) -> bool:
        with self.lock:
            self.results.append((command_id, result))
        return True

    def endpoints(self) -> set:
        with self.lock:
            return {endpoint for _, endpoint, _ in self.reports}


class StatusReporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeHost()
        self.addCleanup(self.host.cleanup)
        key_file = self.host.write("agent.key", base64.b64encode(bytes(range(32))).decode())
        os.chmod(key_file, 0o600)
        self.config = StatusReporterConfig(
            enabled=True,
            api_url="http://api.invalid",
            shared_key_file=Path(key_file),
            host_interval_seconds=0.05,
            containers_interval_seconds=0.05,
            connectivity_interval_seconds=0.05,
            disk_health_interval_seconds=0.05,
            command_poll_seconds=1,
            ping_targets=(),
            dns_probe_host="localhost",
            smartctl_executable="definitely-not-installed-smartctl",
            vcgencmd_executable="definitely-not-installed-vcgencmd",
        )
        self.command = FakeDockerCommand(
            {
                "ps": lambda args: (0, "temp-fe", "") if "{{.Names}}" in args else (0, f"{'a' * 64}\tUp 2 months (healthy)", ""),
                "inspect": (0, json.dumps([CONTAINER]), ""),
                "version": (0, "25.0.2", ""),
                "network ls": (0, "", ""),
                "logs": (0, "2026-10-03T19:30:00Z hello\n", ""),
            }
        )
        self.client = FakeClient()

    def _reporter(self) -> StatusReporter:
        return StatusReporter(self.config, self.command, self.host.paths, client_factory=lambda cfg, key: self.client)

    def _wait_for(self, condition, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.05)
        self.fail("condition not met in time")

    def test_every_channel_reports_and_log_commands_are_answered(self) -> None:
        reporter = self._reporter()
        self.assertTrue(reporter.start())
        try:
            self._wait_for(lambda: {"/host", "/containers", "/connectivity", "/disks"} <= self.client.endpoints())
            self._wait_for(lambda: len(self.client.results) == 1)
        finally:
            reporter.stop()

        self.assertFalse(reporter.running)
        self.assertEqual({"temp-fe"}, reporter.known_containers())

        command_id, result = self.client.results[0]
        self.assertEqual("c" * 32, command_id)
        self.assertTrue(result["ok"], result)
        self.assertEqual("hello", result["lines"][0]["text"])

        containers = [payload for _, endpoint, payload in self.client.reports if endpoint == "/containers"][0]
        self.assertIn("collectedAt", containers)
        self.assertEqual("Up 2 months (healthy)", containers["containers"][0]["status"])

    def test_a_disabled_reporter_starts_nothing(self) -> None:
        reporter = StatusReporter(StatusReporterConfig(), self.command, self.host.paths)

        self.assertFalse(reporter.start())
        self.assertFalse(reporter.running)

    def test_reconfiguring_to_disabled_stops_the_threads(self) -> None:
        reporter = self._reporter()
        reporter.start()
        self._wait_for(lambda: "/host" in self.client.endpoints())

        reporter.reconfigure(self.config)  # unchanged: keeps running
        self.assertTrue(reporter.running)

        reporter.reconfigure(StatusReporterConfig())
        self.assertFalse(reporter.running)


if __name__ == "__main__":
    unittest.main()
