"""Runs one pipeline: sync first, then its snapshots."""

from __future__ import annotations

from typing import Dict

from orchestrator.config.pipeline_config import PipelineConfig
from orchestrator.docker_cli.container_runner import ContainerRunner
from orchestrator.errors import TaskGroupError
from orchestrator.runtime.task import Task


class PipelineTask(Task):
    """Sync -> snapshot chain for a single pipeline.

    Two behaviours here are deliberate and easy to get wrong on a rewrite:

    * A failed sync does **not** cancel the snapshots. Whatever is on disk is
      still worth capturing, so the snapshot runs and the sync failure is
      re-raised afterwards (unless the pipeline allows sync failures).
    * When the daily snapshot is due, hourly and daily run in parallel.
    """

    kind = "pipeline"

    def __init__(
        self,
        pipeline: PipelineConfig,
        runner: ContainerRunner,
        run_daily_snapshot: bool,
        max_workers: int | None = None,
    ) -> None:
        super().__init__(pipeline.name)
        self.pipeline = pipeline
        self.runner = runner
        self.run_daily_snapshot = run_daily_snapshot
        self.max_workers = max_workers

    def execute(self) -> None:
        failures: Dict[str, BaseException] = {}

        sync_error = self._run_sync()
        if sync_error is not None:
            failures[self.pipeline.sync] = sync_error

        snapshot_error = self._run_snapshots()
        if snapshot_error is not None:
            failures["snapshots"] = snapshot_error

        if len(failures) == 1:
            raise next(iter(failures.values()))
        if failures:
            raise TaskGroupError(failures)

    # ------------------------------------------------------------------ steps

    def _run_sync(self) -> BaseException | None:
        """Run the sync container; return the error to report later, if any."""
        self.log.info("[%s] Starting sync container: %s", self.name, self.pipeline.sync)
        try:
            self.runner.run_and_wait(self.pipeline.sync, self.pipeline.timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - reported after snapshots run
            if self.pipeline.allow_sync_failure:
                self.log.warning(
                    "[%s] Sync '%s' failed (allowed by config), continuing to snapshot: %s",
                    self.name,
                    self.pipeline.sync,
                    exc,
                )
                return None
            self.log.error(
                "[%s] Sync '%s' failed; running snapshots anyway: %s",
                self.name,
                self.pipeline.sync,
                exc,
            )
            return exc
        return None

    def _run_snapshots(self) -> BaseException | None:
        snapshots = self.pipeline.snapshots(include_daily=self.run_daily_snapshot)
        self.log.info(
            "[%s] Starting %s snapshot(s): %s",
            self.name,
            "hourly + daily" if self.run_daily_snapshot else "hourly",
            ", ".join(snapshots),
        )
        try:
            self.runner.run_parallel_and_wait(
                snapshots, self.pipeline.timeout_seconds, self.max_workers
            )
        except Exception as exc:  # noqa: BLE001 - reported by execute()
            return exc
        return None
