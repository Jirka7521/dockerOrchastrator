"""Coordinates one orchestration cycle."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import List

from orchestrator.config.app_config import AppConfig
from orchestrator.docker_cli.container_runner import ContainerRunner
from orchestrator.errors import OrchestratorError
from orchestrator.runtime.pipeline_task import PipelineTask
from orchestrator.runtime.run_report import RunReport
from orchestrator.runtime.scheduled_container_task import ScheduledContainerTask
from orchestrator.runtime.task import Task
from orchestrator.runtime.task_result import TaskResult
from orchestrator.scheduling.schedule_evaluator import ScheduleEvaluator
from orchestrator.scheduling.scheduled_container_selector import (
    ScheduledContainerSelector,
)
from orchestrator.state.daily_run_state import DailyRunState


class Orchestrator:
    """Turns "what time is it" into "which containers run now".

    One cycle never raises: every failure is captured in the returned
    :class:`RunReport`, because a daemon that dies on a bad container is worse
    than one that reports the bad container and keeps its schedule.
    """

    def __init__(
        self,
        config: AppConfig,
        runner: ContainerRunner,
        daily_state: DailyRunState | None = None,
        evaluator: ScheduleEvaluator | None = None,
        selector: ScheduledContainerSelector | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.daily_state = daily_state or DailyRunState(config.state_file)
        self.evaluator = evaluator or ScheduleEvaluator(config.schedule)
        self.selector = selector or ScheduledContainerSelector(config.scheduled_containers)
        self.log = logging.getLogger(self.__class__.__name__)
        self._cycle_lock = threading.Lock()

    # ------------------------------------------------------------------- run

    def run(
        self,
        now: datetime,
        force_daily: bool = False,
        ignore_hourly_tick: bool = False,
        dry_run: bool = False,
    ) -> RunReport:
        """Execute (or deliberately skip) one cycle for the moment ``now``."""
        if not self._cycle_lock.acquire(blocking=False):
            self.log.warning("A cycle is already running; skipping this trigger.")
            return RunReport.skipped("a previous cycle is still running")
        try:
            return self._run_locked(now, force_daily, ignore_hourly_tick, dry_run)
        finally:
            self._cycle_lock.release()

    def _run_locked(
        self,
        now: datetime,
        force_daily: bool,
        ignore_hourly_tick: bool,
        dry_run: bool,
    ) -> RunReport:
        if not ignore_hourly_tick and not self.evaluator.is_hourly_tick(now):
            reason = (
                f"minute {now.minute} does not match the configured hourly minute "
                f"{self.config.schedule.hourly_minute}"
            )
            self.log.info("Skipping run: %s.", reason)
            return RunReport.skipped(reason)

        daily_due = force_daily or self.evaluator.is_daily_due(
            now, self.daily_state.last_daily_date()
        )
        report = RunReport(daily_snapshots=daily_due, dry_run=dry_run)

        tasks = self._build_tasks(now, daily_due)
        report.planned = [task.label for task in tasks]
        self.log.info(
            "Starting cycle at %s (daily snapshots: %s, tasks: %d)",
            now.isoformat(timespec="seconds"),
            "enabled" if daily_due else "disabled",
            len(tasks),
        )
        for task in tasks:
            self.log.info("  planned: %s", task.label)

        if dry_run:
            self.log.info("Dry run: no containers were touched.")
            return report

        if not tasks:
            self.log.info("Nothing is due in this cycle.")
            return report

        try:
            self.runner.assert_available()
        except OrchestratorError as exc:
            self.log.error("Cannot run this cycle: %s", exc)
            report.add_fatal(exc)
            return report

        if daily_due:
            # Claim the day before running anything: individual tasks are
            # allowed to fail without aborting the cycle, so waiting until the
            # end would leave the day unclaimed and re-trigger daily snapshots
            # on the next tick.
            self.daily_state.record_daily_date(now.date())
        self.daily_state.record_run(now.isoformat(timespec="seconds"))

        self._execute(tasks, report)
        self.log.info("Cycle finished: %s", report.summary())
        return report

    # ----------------------------------------------------------------- tasks

    def _build_tasks(self, now: datetime, daily_due: bool) -> List[Task]:
        max_workers = self.config.runtime.max_parallel_tasks or None
        tasks: List[Task] = [
            PipelineTask(pipeline, self.runner, daily_due, max_workers)
            for pipeline in self.config.enabled_pipelines
        ]
        tasks.extend(
            ScheduledContainerTask(container, self.runner)
            for container in self.selector.due(now, daily_due)
        )
        return tasks

    def _execute(self, tasks: List[Task], report: RunReport) -> None:
        workers = self.config.runtime.worker_count(len(tasks))
        self.log.debug("Running %d task(s) with %d worker(s).", len(tasks), workers)

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="task") as pool:
            futures = {pool.submit(task.run): task for task in tasks}
            for future in as_completed(futures):
                task = futures[future]
                try:
                    report.add(future.result())
                except BaseException as exc:  # noqa: BLE001 - Task.run should not raise
                    self.log.exception("Task %s crashed unexpectedly.", task.label)
                    report.add(
                        TaskResult.failed(
                            task.kind,
                            task.name,
                            datetime.now(timezone.utc),
                            0.0,
                            exc,
                        )
                    )

        for line in report.detailed_lines():
            self.log.info("  %s", line)
