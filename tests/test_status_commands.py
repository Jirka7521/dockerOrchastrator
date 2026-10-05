"""Log fetching, container and power control, and the command executor -- the trust boundary to the API."""

from __future__ import annotations

import subprocess
import sys
import threading
import unittest
from typing import List, Sequence

from orchestrator.status.collectors.log_fetcher import LogFetcher
from orchestrator.status.command_executor import CommandExecutor
from orchestrator.status.command_types import ALL
from orchestrator.status.container_control import ContainerControl
from orchestrator.status.power_control import PowerControl
from tests.fakes import FakeDockerCommand


class FakeSystemctl:
    """Records what would have run; the host is never touched."""

    def __init__(self, returncode: int = 0) -> None:
        self.calls: List[List[str]] = []
        self.returncode = returncode
        self.ran = threading.Event()

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        self.ran.set()
        return subprocess.CompletedProcess(list(args), self.returncode, "", "boom" if self.returncode else "")


def fake_power(systemctl: FakeSystemctl, executable: str = sys.executable) -> PowerControl:
    """Power control whose "systemctl" is a fake, with no delay. ``executable``
    only has to exist for the pre-check; it is never run."""
    return PowerControl(executable=executable, runner=systemctl, sleeper=lambda _seconds: None)

STDOUT = (
    "2026-10-03T19:30:00.000000001Z first\n"
    "2026-10-03T19:30:02.5Z third\r\n"
)
STDERR = "2026-10-03T19:30:01.123456789Z second (an error)\n"


class LogFetcherTests(unittest.TestCase):
    def test_streams_are_merged_by_timestamp_and_labelled(self) -> None:
        command = FakeDockerCommand({"logs": (0, STDOUT, STDERR)})

        result = LogFetcher(command, max_lines=100, max_bytes=100_000).fetch("temp-fe", 500, None)

        self.assertTrue(result["ok"])
        self.assertFalse(result["truncated"])
        self.assertEqual(["first", "second (an error)", "third"], [line["text"] for line in result["lines"]])
        self.assertEqual(["stdout", "stderr", "stdout"], [line["stream"] for line in result["lines"]])
        self.assertEqual("2026-10-03T19:30:01.123456Z", result["lines"][1]["timestamp"])
        self.assertEqual(["logs", "--timestamps", "--tail", "500", "temp-fe"], command.calls[0])

    def test_the_newest_lines_are_kept_when_capping(self) -> None:
        lines = "".join(f"2026-10-03T19:30:{i:02d}Z line {i}\n" for i in range(10))
        command = FakeDockerCommand({"logs": (0, lines, "")})

        result = LogFetcher(command, max_lines=3, max_bytes=100_000).fetch("x", 10, None)

        self.assertTrue(result["truncated"])
        self.assertEqual(["line 7", "line 8", "line 9"], [line["text"] for line in result["lines"]])

    def test_since_is_passed_and_failures_are_reported(self) -> None:
        command = FakeDockerCommand({"logs": (1, "", "Error: configured logging driver does not support reading")})

        result = LogFetcher(command, 10, 10_000).fetch("x", 10, "2026-10-03T19:00:00Z")

        self.assertFalse(result["ok"])
        self.assertIn("logging driver", result["error"])
        self.assertEqual(["logs", "--timestamps", "--tail", "10", "--since", "2026-10-03T19:00:00Z", "x"], command.calls[0])


class CommandExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.command = FakeDockerCommand({"logs": (0, "2026-10-03T19:30:00Z hello\n", "")})
        fetcher = LogFetcher(self.command, max_lines=5000, max_bytes=100_000)
        self.executor = CommandExecutor(fetcher, known_containers=lambda: {"temp-fe", "status-api"}, max_lines=5000)

    def _refused(self, command: dict, executor: CommandExecutor | None = None) -> str:
        outcome = (executor or self.executor).execute(command)
        self.assertFalse(outcome.result["ok"])
        self.assertIsNone(outcome.after_delivery)
        self.assertEqual([], self.command.calls, "docker must not have been called")
        return outcome.result["error"]

    def _allowing_everything(self, systemctl: FakeSystemctl | None = None, systemctl_path: str = sys.executable) -> CommandExecutor:
        fetcher = LogFetcher(self.command, max_lines=5000, max_bytes=100_000)
        return CommandExecutor(
            fetcher,
            known_containers=lambda: {"temp-fe", "status-api"},
            max_lines=5000,
            allowed=ALL,
            containers=ContainerControl(self.command, timeout_seconds=120),
            power=fake_power(systemctl or FakeSystemctl(), systemctl_path),
        )

    def test_a_valid_log_command_runs(self) -> None:
        result = self.executor.execute({"type": "logs", "container": "temp-fe", "tail": 100}).result

        self.assertTrue(result["ok"])
        self.assertEqual("hello", result["lines"][0]["text"])

    def test_only_logs_run_unless_the_config_allows_more(self) -> None:
        for kind in ("start", "stop", "restart", "reboot", "poweroff"):
            with self.subTest(kind=kind):
                error = self._refused({"type": kind, "container": "temp-fe"})
                self.assertIn("not in status_reporter.allowed_commands", error)

    def test_a_container_action_runs_docker_with_the_name_and_nothing_else(self) -> None:
        executor = self._allowing_everything()

        outcome = executor.execute({"type": "restart", "container": "temp-fe", "tail": 5, "extra": "-t 0"})

        self.assertEqual({"ok": True, "error": None, "truncated": False, "lines": []}, outcome.result)
        self.assertIsNone(outcome.after_delivery)
        self.assertEqual([["restart", "temp-fe"]], self.command.calls)

    def test_a_failed_action_reports_dockers_message(self) -> None:
        self.command.responses["stop"] = (1, "", "Error response from daemon: cannot stop container: temp-fe: permission denied")

        result = self._allowing_everything().execute({"type": "stop", "container": "temp-fe"}).result

        self.assertFalse(result["ok"])
        self.assertIn("permission denied", result["error"])

    def test_container_actions_check_the_name_like_log_requests(self) -> None:
        executor = self._allowing_everything()

        self.assertIn("not in the current container list", self._refused({"type": "stop", "container": "other"}, executor))
        for name in ("--help", "-t", "a b", None):
            with self.subTest(name=name):
                self.assertIn("invalid container name", self._refused({"type": "start", "container": name}, executor))

    def test_power_is_accepted_first_and_carried_out_only_after_delivery(self) -> None:
        systemctl = FakeSystemctl()
        outcome = self._allowing_everything(systemctl).execute({"type": "reboot", "container": "ignored"})

        self.assertTrue(outcome.result["ok"])
        self.assertEqual([], systemctl.calls, "nothing may happen before the API has the answer")
        self.assertIsNotNone(outcome.after_delivery)

        outcome.after_delivery()
        self.assertTrue(systemctl.ran.wait(5))
        self.assertEqual([[sys.executable, "reboot"]], systemctl.calls)
        self.assertEqual([], self.command.calls)

    def test_power_is_refused_up_front_when_systemctl_is_missing(self) -> None:
        executor = self._allowing_everything(systemctl_path="definitely-not-installed-systemctl")

        self.assertIn("was not found", self._refused({"type": "poweroff"}, executor))

    def test_other_command_types_are_refused(self) -> None:
        self.assertIn("unsupported", self._refused({"type": "exec", "container": "temp-fe"}))
        self.assertIn("unsupported", self._refused({"container": "temp-fe"}))
        self.assertIn("unsupported", self._refused({"type": "exec", "container": "temp-fe"}, self._allowing_everything()))

    def test_containers_not_in_the_own_list_are_refused(self) -> None:
        self.assertIn("not in the current container list", self._refused({"type": "logs", "container": "other"}))

    def test_names_that_are_not_container_names_are_refused(self) -> None:
        for name in ("--help", "-f", "a b", "../etc", "", None, 42, "x;rm -rf /"):
            with self.subTest(name=name):
                self.assertIn("invalid container name", self._refused({"type": "logs", "container": name}))

    def test_malformed_since_is_refused(self) -> None:
        self.assertIn("invalid since", self._refused({"type": "logs", "container": "temp-fe", "since": "--until=now"}))

    def test_an_unknown_name_is_rechecked_once_against_docker(self) -> None:
        refreshed = []
        fetcher = LogFetcher(self.command, max_lines=5000, max_bytes=100_000)
        executor = CommandExecutor(
            fetcher, known_containers=set, max_lines=5000,
            refresh_containers=lambda: refreshed.append(1) or {"brand-new"},
        )

        self.assertTrue(executor.execute({"type": "logs", "container": "brand-new"}).result["ok"])
        self.assertFalse(executor.execute({"type": "logs", "container": "never-existed"}).result["ok"])
        self.assertEqual(2, len(refreshed))

    def test_the_tail_is_clamped(self) -> None:
        self.executor.execute({"type": "logs", "container": "temp-fe", "tail": 10**9})
        self.executor.execute({"type": "logs", "container": "temp-fe", "tail": -5})

        self.assertEqual("5000", self.command.calls[0][3])
        self.assertEqual("1", self.command.calls[1][3])


if __name__ == "__main__":
    unittest.main()
