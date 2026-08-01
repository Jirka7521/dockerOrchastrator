"""DockerRunner behaviour against a scripted Docker CLI."""

from __future__ import annotations

import unittest

from orchestrator.docker_cli.docker_runner import DockerRunner
from orchestrator.errors import (
    ContainerFailedError,
    ContainerNotFoundError,
    ContainerStartError,
    DockerUnavailableError,
)
from tests.fakes import FakeDockerCommand

NO_SUCH_OBJECT = (1, "", "Error: No such object: missing")


def build(responses: dict, **kwargs) -> tuple[DockerRunner, FakeDockerCommand]:
    command = FakeDockerCommand(responses)
    return DockerRunner(command=command, failure_log_lines=0, **kwargs), command


class DockerRunnerTests(unittest.TestCase):
    def test_available_docker_passes(self) -> None:
        runner, _ = build({"version": (0, "27.0.3", "")})

        runner.assert_available()

    def test_unavailable_docker_raises(self) -> None:
        runner, _ = build({"version": (1, "", "Cannot connect to the Docker daemon")})

        with self.assertRaises(DockerUnavailableError):
            runner.assert_available()

    def test_missing_container_reports_no_status(self) -> None:
        runner, _ = build({"inspect": NO_SUCH_OBJECT})

        self.assertIsNone(runner.status("missing"))
        self.assertFalse(runner.exists("missing"))

    def test_running_container_is_not_started_again(self) -> None:
        runner, command = build({"inspect": (0, "running", ""), "wait": (0, "0", "")})

        runner.run_and_wait("job")

        self.assertNotIn(["start", "job"], command.calls)
        self.assertIn(["wait", "job"], command.calls)

    def test_stopped_container_is_started_then_awaited(self) -> None:
        runner, command = build(
            {"inspect": (0, "exited", ""), "start": (0, "", ""), "wait": (0, "0", "")}
        )

        runner.run_and_wait("job")

        self.assertIn(["start", "job"], command.calls)

    def test_non_zero_exit_becomes_a_typed_error(self) -> None:
        runner, _ = build(
            {"inspect": (0, "exited", ""), "start": (0, "", ""), "wait": (0, "3", "")}
        )

        with self.assertRaises(ContainerFailedError) as ctx:
            runner.run_and_wait("job")
        self.assertEqual(ctx.exception.exit_code, 3)

    def test_paused_container_is_refused_instead_of_hanging(self) -> None:
        runner, _ = build({"inspect": (0, "paused", "")})

        with self.assertRaises(ContainerStartError):
            runner.run_and_wait("job")

    def test_missing_container_is_created_from_a_matching_image(self) -> None:
        runner, command = build(
            {
                "inspect": NO_SUCH_OBJECT,
                "image inspect": (0, "sha256:abc", ""),
                "run": (0, "container-id", ""),
                "wait": (0, "0", ""),
            }
        )

        runner.run_and_wait("job")

        self.assertIn(["run", "-d", "--name", "job", "job"], command.calls)

    def test_missing_container_without_image_is_an_error(self) -> None:
        runner, _ = build({"inspect": NO_SUCH_OBJECT, "image inspect": NO_SUCH_OBJECT})

        with self.assertRaises(ContainerNotFoundError):
            runner.run_and_wait("job")

    def test_create_from_image_can_be_disabled(self) -> None:
        runner, _ = build({"inspect": NO_SUCH_OBJECT}, create_missing_from_image=False)

        with self.assertRaises(ContainerNotFoundError):
            runner.run_and_wait("job")

    def test_container_removed_while_waiting_is_explained(self) -> None:
        runner, _ = build(
            {
                "inspect": (0, "running", ""),
                "wait": (1, "", "Error: No such container: job"),
            }
        )

        with self.assertRaises(ContainerNotFoundError) as ctx:
            runner.run_and_wait("job")
        self.assertIn("--rm", str(ctx.exception))

    def test_parallel_run_reports_every_failure(self) -> None:
        runner, _ = build(
            {
                "inspect": (0, "exited", ""),
                "start": (0, "", ""),
                "wait": (0, "1", ""),
            }
        )

        with self.assertRaises(Exception) as ctx:
            runner.run_parallel_and_wait(["a", "b"])
        self.assertIn("2 task(s) failed", str(ctx.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
