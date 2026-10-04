"""StatusReporter threads end to end, against a fake API client and fake host."""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import threading
import time
import unittest
from pathlib import Path
from typing import List

from orchestrator.status.command_types import ALL
from orchestrator.status.status_config import StatusReporterConfig
from orchestrator.status.status_reporter import StatusReporter
from tests.fakes import FakeDockerCommand
from tests.status_support import FakeHost
from tests.test_status_commands import FakeSystemctl, fake_power

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
        self.accepts: List[tuple] = []
        self.commands = [{"id": "c" * 32, "type": "logs", "container": "temp-fe", "tail": 5}]
        #: What send_command_result answers: False is the API's 404, "nobody waits".
        self.taken = True
        self.lock = threading.Lock()

    def send_report(self, method: str, endpoint: str, payload: dict) -> None:
        with self.lock:
            self.reports.append((method, endpoint, payload))

    def poll_commands(self, wait_seconds: int, accept=("logs",)) -> list:
        time.sleep(0.05)
        with self.lock:
            self.accepts.append(tuple(accept))
            commands, self.commands = self.commands, []
        return commands

    def send_command_result(self, command_id: str, result: dict) -> bool:
        with self.lock:
            self.results.append((command_id, result))
        return self.taken

    def container_reports(self) -> int:
        with self.lock:
            return sum(1 for _, endpoint, _ in self.reports if endpoint == "/containers")

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

    def _reporter(self, config: StatusReporterConfig | None = None, systemctl: FakeSystemctl | None = None) -> StatusReporter:
        return StatusReporter(
            config or self.config,
            self.command,
            self.host.paths,
            client_factory=lambda cfg, key: self.client,
            power=fake_power(systemctl or FakeSystemctl()),
        )

    def _allowing_everything(self, **changes) -> StatusReporterConfig:
        return dataclasses.replace(self.config, allowed_commands=ALL, **changes)

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

    def test_every_poll_says_what_this_host_allows(self) -> None:
        reporter = self._reporter(dataclasses.replace(self.config, allowed_commands=("logs", "restart")))
        reporter.start()
        try:
            self._wait_for(lambda: len(self.client.accepts) >= 2)
        finally:
            reporter.stop()

        self.assertEqual({("logs", "restart")}, set(self.client.accepts))

    def test_a_container_action_runs_on_a_worker_and_the_list_follows_at_once(self) -> None:
        # A slow regular cadence: a container report within the first second
        # can only come from the action waking the channel.
        self.client.commands = [{"id": "d" * 32, "type": "restart", "container": "temp-fe"}]
        reporter = self._reporter(self._allowing_everything(containers_interval_seconds=60))
        reporter.start()
        try:
            self._wait_for(lambda: len(self.client.results) == 1)
            self._wait_for(lambda: self.client.container_reports() >= 1, timeout=0.8)
        finally:
            reporter.stop()

        command_id, result = self.client.results[0]
        self.assertEqual(("d" * 32, True), (command_id, result["ok"]))
        self.assertIn(["restart", "temp-fe"], self.command.calls)

    def test_a_reboot_happens_only_after_the_api_took_the_answer(self) -> None:
        systemctl = FakeSystemctl()
        self.client.commands = [{"id": "e" * 32, "type": "reboot"}]
        reporter = self._reporter(self._allowing_everything(), systemctl)
        reporter.start()
        try:
            self.assertTrue(systemctl.ran.wait(5))
        finally:
            reporter.stop()

        self.assertEqual(("e" * 32, True), (self.client.results[0][0], self.client.results[0][1]["ok"]))
        self.assertEqual("reboot", systemctl.calls[0][-1])

    def test_a_reboot_nobody_waits_for_any_more_never_happens(self) -> None:
        systemctl = FakeSystemctl()
        self.client.commands = [{"id": "f" * 32, "type": "poweroff"}]
        self.client.taken = False  # the dashboard request timed out: the API answers 404
        reporter = self._reporter(self._allowing_everything(), systemctl)
        reporter.start()
        try:
            self._wait_for(lambda: len(self.client.results) == 1)
            self.assertFalse(systemctl.ran.wait(0.5))
        finally:
            reporter.stop()

        self.assertEqual([], systemctl.calls)

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
