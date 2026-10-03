"""Log fetching and the command executor -- the trust boundary to the API."""

from __future__ import annotations

import unittest

from orchestrator.status.collectors.log_fetcher import LogFetcher
from orchestrator.status.command_executor import CommandExecutor
from tests.fakes import FakeDockerCommand

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

    def _refused(self, command: dict) -> str:
        result = self.executor.execute(command)
        self.assertFalse(result["ok"])
        self.assertEqual([], self.command.calls, "docker must not have been called")
        return result["error"]

    def test_a_valid_log_command_runs(self) -> None:
        result = self.executor.execute({"type": "logs", "container": "temp-fe", "tail": 100})

        self.assertTrue(result["ok"])
        self.assertEqual("hello", result["lines"][0]["text"])

    def test_other_command_types_are_refused(self) -> None:
        self.assertIn("unsupported", self._refused({"type": "exec", "container": "temp-fe"}))
        self.assertIn("unsupported", self._refused({"container": "temp-fe"}))

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

        self.assertTrue(executor.execute({"type": "logs", "container": "brand-new"})["ok"])
        self.assertFalse(executor.execute({"type": "logs", "container": "never-existed"})["ok"])
        self.assertEqual(2, len(refreshed))

    def test_the_tail_is_clamped(self) -> None:
        self.executor.execute({"type": "logs", "container": "temp-fe", "tail": 10**9})
        self.executor.execute({"type": "logs", "container": "temp-fe", "tail": -5})

        self.assertEqual("5000", self.command.calls[0][3])
        self.assertEqual("1", self.command.calls[1][3])


if __name__ == "__main__":
    unittest.main()
