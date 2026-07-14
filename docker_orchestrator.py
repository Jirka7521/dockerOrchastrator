#!/usr/bin/env python3
"""Docker job orchestrator for hourly and daily container chains.

This script is intended to be triggered once per hour (for example by cron or
systemd timer). On each valid hourly tick it:

1. Starts pipeline sync containers in parallel.
2. After each sync finishes, starts snapshot container(s) for that pipeline:
   - Always run hourly snapshot.
   - Run daily snapshot too when the current local time matches the daily time.
3. Runs optionally configured scheduled containers in the requested period.

All names, times and scheduled container definitions are stored in JSON config.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ScheduleConfig:
    """Runtime schedule settings loaded from JSON."""

    timezone: str
    hourly_minute: int
    daily_hour: int
    daily_minute: int

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "ScheduleConfig":
        timezone = str(data.get("timezone", "UTC"))
        hourly_minute = int(data["hourly_minute"])
        daily_hour = int(data["daily_hour"])
        daily_minute = int(data["daily_minute"])

        if not 0 <= hourly_minute <= 59:
            raise ValueError("schedule.hourly_minute must be in range 0-59")
        if not 0 <= daily_hour <= 23:
            raise ValueError("schedule.daily_hour must be in range 0-23")
        if not 0 <= daily_minute <= 59:
            raise ValueError("schedule.daily_minute must be in range 0-59")

        # Validate timezone early so startup errors are clear.
        ZoneInfo(timezone)

        return ScheduleConfig(
            timezone=timezone,
            hourly_minute=hourly_minute,
            daily_hour=daily_hour,
            daily_minute=daily_minute,
        )


@dataclass(frozen=True)
class PipelineConfig:
    """Container names that define one processing chain."""

    name: str
    sync: str
    hourly_snapshot: str
    daily_snapshot: str

    @staticmethod
    def from_dict(name: str, data: Dict[str, Any]) -> "PipelineConfig":
        def required_text(key: str) -> str:
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"pipelines.{name}.{key} must be a non-empty string")
            return value

        return PipelineConfig(
            name=name,
            sync=required_text("sync"),
            hourly_snapshot=required_text("hourly_snapshot"),
            daily_snapshot=required_text("daily_snapshot"),
        )


@dataclass(frozen=True)
class ScheduledContainerConfig:
    """Container configured to run on a named schedule."""

    name: str
    period: str
    interval_hours: int | None = None

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "ScheduledContainerConfig":
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("scheduled_containers[].name must be a non-empty string")

        period = data.get("period")
        if not isinstance(period, str):
            raise ValueError("scheduled_containers[].period must be a string")

        normalized_period = period.strip().lower()
        interval_hours: int | None = None

        if normalized_period in {"hourly", "daily"}:
            pass
        else:
            match = re.fullmatch(r"every_(\d+)_hours", normalized_period)
            if not match:
                raise ValueError(
                    "scheduled_containers[].period must be one of: 'hourly', 'daily', or 'every_N_hours'"
                )
            interval_hours = int(match.group(1))
            if interval_hours <= 0 or interval_hours > 24:
                raise ValueError(
                    "scheduled_containers[].period hours must be between 1 and 24"
                )
            if 24 % interval_hours != 0:
                raise ValueError(
                    "scheduled_containers[].period every_N_hours must divide 24 exactly"
                )

        return ScheduledContainerConfig(
            name=name.strip(),
            period=normalized_period,
            interval_hours=interval_hours,
        )


@dataclass(frozen=True)
class AppConfig:
    """Top-level application config."""

    schedule: ScheduleConfig
    pipelines: List[PipelineConfig]
    scheduled_containers: List[ScheduledContainerConfig]

    @staticmethod
    def load(path: Path) -> "AppConfig":
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)

        schedule = ScheduleConfig.from_dict(raw["schedule"])

        pipelines_raw = raw.get("pipelines")
        if not isinstance(pipelines_raw, dict) or not pipelines_raw:
            raise ValueError("pipelines must be a non-empty object")

        pipelines = [
            PipelineConfig.from_dict(name, payload)
            for name, payload in pipelines_raw.items()
        ]

        scheduled_containers_raw = raw.get("scheduled_containers", [])
        if scheduled_containers_raw is None:
            scheduled_containers_raw = []
        if not isinstance(scheduled_containers_raw, list):
            raise ValueError("scheduled_containers must be an array")

        scheduled_containers = [
            ScheduledContainerConfig.from_dict(item)
            for item in scheduled_containers_raw
        ]

        return AppConfig(
            schedule=schedule,
            pipelines=pipelines,
            scheduled_containers=scheduled_containers,
        )


class DockerRunner:
    """Small wrapper around Docker CLI commands with logging and checks."""

    def __init__(self) -> None:
        self.log = logging.getLogger(self.__class__.__name__)

    def _run(self, args: List[str]) -> subprocess.CompletedProcess[str]:
        self.log.debug("Running command: %s", " ".join(args))
        return subprocess.run(args, capture_output=True, text=True, check=False)

    def assert_docker_available(self) -> None:
        result = self._run(["docker", "version", "--format", "{{.Server.Version}}"])
        if result.returncode != 0:
            raise RuntimeError(
                "Docker is not available. Check Docker daemon and user permissions. "
                f"stderr: {result.stderr.strip()}"
            )

    def is_running(self, container: str) -> bool:
        result = self._run(["docker", "inspect", "-f", "{{.State.Running}}", container])
        if result.returncode != 0:
            raise RuntimeError(
                f"Container '{container}' cannot be inspected. stderr: {result.stderr.strip()}"
            )
        state = result.stdout.strip().lower()
        if state not in {"true", "false"}:
            raise RuntimeError(f"Unexpected running state for '{container}': {state!r}")
        return state == "true"

    def start_container(self, container: str) -> None:
        result = self._run(["docker", "start", container])
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to start '{container}'. stderr: {result.stderr.strip()}"
            )
        self.log.info("Started container: %s", container)

    def wait_container(self, container: str) -> int:
        result = self._run(["docker", "wait", container])
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed while waiting for '{container}'. stderr: {result.stderr.strip()}"
            )

        # docker wait prints exit code to stdout
        code_text = result.stdout.strip()
        try:
            exit_code = int(code_text)
        except ValueError as exc:
            raise RuntimeError(
                f"docker wait produced unexpected output for '{container}': {code_text!r}"
            ) from exc

        self.log.info("Container finished: %s (exit code %d)", container, exit_code)
        return exit_code

    def run_and_wait(self, container: str) -> None:
        """Start a container unless it is already running, then wait for completion.

        If the container is already running, we do not issue `docker start` again.
        This prevents duplicate starts when an hourly tick arrives while a job is
        still active from a previous run.
        """
        try:
            if self.is_running(container):
                self.log.info("Container is already running, waiting: %s", container)
            else:
                self.start_container(container)
        except RuntimeError as exc:
            # If the container cannot be inspected (commonly because it does
            # not exist), try to create and run it from an image with the
            # same name. This allows scheduled containers that are defined as
            # images (rather than pre-created containers) to run the same way
            # other containers do.
            msg = str(exc)
            if "No such object" in msg or "cannot be inspected" in msg:
                self.log.info(
                    "Container '%s' not found; attempting to create and run from image '%s'",
                    container,
                    container,
                )
                run_result = self._run(["docker", "run", "-d", "--name", container, container])
                if run_result.returncode != 0:
                    raise RuntimeError(
                        f"Failed to run image '{container}' as container '{container}': {run_result.stderr.strip()}"
                    )
            else:
                raise

        exit_code = self.wait_container(container)
        if exit_code != 0:
            raise RuntimeError(f"Container '{container}' exited with code {exit_code}")

    def run_parallel_and_wait(self, containers: Iterable[str]) -> None:
        """Run several containers in parallel and fail if any chain fails."""
        items = list(containers)
        if not items:
            return

        with ThreadPoolExecutor(max_workers=len(items)) as executor:
            futures = {executor.submit(self.run_and_wait, name): name for name in items}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 - we re-raise with context
                    raise RuntimeError(f"Parallel run failed for '{name}': {exc}") from exc


class Orchestrator:
    """Coordinates sync and snapshot chains according to schedule."""

    def __init__(self, config: AppConfig, runner: DockerRunner) -> None:
        self.config = config
        self.runner = runner
        self.log = logging.getLogger(self.__class__.__name__)

    def _is_hourly_tick(self, now: datetime) -> bool:
        return now.minute == self.config.schedule.hourly_minute

    def _is_daily_tick(self, now: datetime) -> bool:
        return (
            now.hour == self.config.schedule.daily_hour
            and now.minute == self.config.schedule.daily_minute
        )

    def _run_pipeline_chain(self, pipeline: PipelineConfig, run_daily_snapshot: bool) -> None:
        """Run sync first, then hourly snapshot, and daily snapshot on daily tick.

        On daily tick, hourly + daily snapshots are started in parallel.
        """
        self.log.info("[%s] Starting sync container: %s", pipeline.name, pipeline.sync)
        self.runner.run_and_wait(pipeline.sync)

        snapshots = [pipeline.hourly_snapshot]
        if run_daily_snapshot:
            snapshots.append(pipeline.daily_snapshot)

        if len(snapshots) == 1:
            self.log.info(
                "[%s] Starting hourly snapshot: %s",
                pipeline.name,
                pipeline.hourly_snapshot,
            )
            self.runner.run_and_wait(pipeline.hourly_snapshot)
        else:
            self.log.info(
                "[%s] Daily tick detected. Starting snapshots in parallel: %s",
                pipeline.name,
                ", ".join(snapshots),
            )
            self.runner.run_parallel_and_wait(snapshots)

    def _scheduled_containers_to_run(
        self, now: datetime, run_daily_snapshot: bool
    ) -> List[ScheduledContainerConfig]:
        if not self.config.scheduled_containers:
            return []

        scheduled = []
        for sc in self.config.scheduled_containers:
            if sc.period == "hourly":
                scheduled.append(sc)
                continue

            if sc.period == "daily" and run_daily_snapshot:
                scheduled.append(sc)
                continue

            if sc.interval_hours is not None:
                if now.hour % sc.interval_hours == 0:
                    scheduled.append(sc)
                continue

        return scheduled

    def _run_scheduled_container(self, container: ScheduledContainerConfig) -> None:
        self.log.info("Starting scheduled container: %s (%s)", container.name, container.period)
        self.runner.run_and_wait(container.name)

    def run(self, now: datetime, force_daily: bool = False, ignore_hourly_tick: bool = False) -> int:
        """Execute orchestration once for the current time window.

        Returns process-style exit code:
        - 0: success or intentionally skipped because current minute is outside hourly tick.
        - non-zero: orchestration failure.
        """
        if not ignore_hourly_tick and not self._is_hourly_tick(now):
            self.log.info(
                "Current minute (%d) does not match configured hourly minute (%d). "
                "Skipping this run.",
                now.minute,
                self.config.schedule.hourly_minute,
            )
            return 0

        run_daily_snapshot = force_daily or self._is_daily_tick(now)
        self.log.info(
            "Starting orchestration at %s (daily snapshots: %s)",
            now.isoformat(timespec="seconds"),
            "enabled" if run_daily_snapshot else "disabled",
        )

        self.runner.assert_docker_available()

        scheduled_containers = self._scheduled_containers_to_run(now, run_daily_snapshot)
        if scheduled_containers:
            self.log.info(
                "Scheduled containers to run: %s",
                ", ".join(sc.name for sc in scheduled_containers),
            )

        all_workers = len(self.config.pipelines) + len(scheduled_containers)
        if all_workers == 0:
            self.log.info("No pipelines or scheduled containers configured. Nothing to run.")
            return 0

        with ThreadPoolExecutor(max_workers=all_workers) as executor:
            futures = {
                executor.submit(self._run_pipeline_chain, pipeline, run_daily_snapshot): pipeline.name
                for pipeline in self.config.pipelines
            }
            futures.update({
                executor.submit(self._run_scheduled_container, sc): f"scheduled:{sc.name}"
                for sc in scheduled_containers
            })

            for future in as_completed(futures):
                task_name = futures[future]
                try:
                    future.result()
                    self.log.info("Task completed successfully: %s", task_name)
                except Exception as exc:  # noqa: BLE001 - we re-raise with context
                    # Keep the orchestrator moving even if one pipeline stops.
                    # Scheduled containers already behave this way, so we apply
                    # the same policy to pipeline chains as well.
                    self.log.exception("Task failed (continuing): %s", task_name)
                    continue

        self.log.info("All configured tasks completed (failures were logged and skipped).")
        return 0


class OrchestratorDaemon:
    """Long-running scheduler that triggers orchestration on configured hourly ticks."""

    def __init__(self, orchestrator: Orchestrator, timezone: str) -> None:
        self.orchestrator = orchestrator
        self.timezone = timezone
        self.log = logging.getLogger(self.__class__.__name__)
        self.stop_event = threading.Event()
        self.last_hourly_slot: str | None = None

    def request_stop(self) -> None:
        """Signal-safe stop request used by signal handlers and KeyboardInterrupt."""
        self.stop_event.set()

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(self.timezone))

    @staticmethod
    def _slot_key(now: datetime) -> str:
        """Unique key for one hourly execution window."""
        return now.strftime("%Y-%m-%dT%H:%M")

    def run_forever(self) -> int:
        """Run forever and execute orchestration once per matching hourly slot."""
        hourly_minute = self.orchestrator.config.schedule.hourly_minute
        self.log.info(
            "Daemon started. Waiting for minute=%d every hour in timezone=%s",
            hourly_minute,
            self.timezone,
        )

        while not self.stop_event.is_set():
            now = self._now()
            slot = self._slot_key(now)

            if now.minute == hourly_minute and slot != self.last_hourly_slot:
                self.log.info("Tick matched (%s). Running orchestration.", slot)
                try:
                    self.orchestrator.run(now=now)
                    self.last_hourly_slot = slot
                except Exception as exc:  # noqa: BLE001 - daemon should continue after failures
                    self.log.exception("Orchestration run failed for slot %s: %s", slot, exc)

            # Sleep in short intervals so signals/stop requests are handled quickly.
            for _ in range(30):
                if self.stop_event.is_set():
                    break
                time.sleep(1)

        self.log.info("Daemon stopping gracefully.")
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Docker containers in configured order on hourly/daily schedule."
    )
    parser.add_argument(
        "--config",
        default="docker_schedule_config.json",
        help="Path to JSON config file (default: docker_schedule_config.json)",
    )
    parser.add_argument(
        "--force-daily",
        action="store_true",
        help="Force daily snapshots even when current time is not the daily tick.",
    )
    parser.add_argument(
        "--ignore-hourly-tick",
        action="store_true",
        help="Run even when current minute does not match configured hourly minute.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run only one orchestration cycle and exit.",
    )
    return parser.parse_args()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )


def main() -> int:
    args = parse_args()
    setup_logging(args.log_level)

    try:
        config = AppConfig.load(Path(args.config))
        now = datetime.now(ZoneInfo(config.schedule.timezone))

        orchestrator = Orchestrator(config=config, runner=DockerRunner())

        if args.once:
            return orchestrator.run(
                now=now,
                force_daily=args.force_daily,
                ignore_hourly_tick=args.ignore_hourly_tick,
            )

        daemon = OrchestratorDaemon(orchestrator=orchestrator, timezone=config.schedule.timezone)

        # Register interrupt handlers for clean shutdown in long-running mode.
        def handle_signal(signum: int, _frame: object) -> None:
            logging.getLogger("main").info("Received signal %s, shutting down.", signum)
            daemon.request_stop()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        return daemon.run_forever()
    except Exception as exc:  # noqa: BLE001 - top-level guard with clear error output
        logging.getLogger("main").error("Orchestration failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())