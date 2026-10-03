"""Top-level configuration object."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping

from orchestrator.config.field_reader import FieldReader
from orchestrator.config.pipeline_config import PipelineConfig
from orchestrator.config.runtime_config import RuntimeConfig
from orchestrator.config.schedule_config import ScheduleConfig
from orchestrator.config.scheduled_container_config import ScheduledContainerConfig
from orchestrator.errors import ConfigError
from orchestrator.status.status_config import StatusReporterConfig

KNOWN_KEYS = (
    "schedule",
    "runtime",
    "pipelines",
    "scheduled_containers",
    "state_file",
    "status_reporter",
)

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppConfig:
    """Validated contents of one config file."""

    schedule: ScheduleConfig
    runtime: RuntimeConfig
    pipelines: List[PipelineConfig]
    scheduled_containers: List[ScheduledContainerConfig]
    state_file: Path
    source_path: Path | None = field(default=None, compare=False)
    #: Optional reporting to the serverStatusPage dashboard; disabled unless configured.
    status_reporter: StatusReporterConfig = field(default_factory=StatusReporterConfig)

    # ------------------------------------------------------------------ build

    @staticmethod
    def from_dict(
        raw: Mapping[str, Any],
        source_path: Path | None = None,
    ) -> "AppConfig":
        reader = FieldReader(raw)
        reader.warn_unknown_keys(KNOWN_KEYS)

        schedule = ScheduleConfig.from_dict(reader.mapping("schedule", {}))
        runtime = RuntimeConfig.from_dict(reader.mapping("runtime", {}))

        pipelines = AppConfig._build_pipelines(reader)
        scheduled_containers = AppConfig._build_scheduled_containers(reader)
        status_reporter = StatusReporterConfig.from_dict(
            reader.mapping("status_reporter", {}),
            base_dir=source_path.parent if source_path else Path.cwd(),
        )

        if not pipelines and not scheduled_containers and not status_reporter.enabled:
            raise ConfigError(
                "Nothing to run: define at least one entry under 'pipelines' or "
                "'scheduled_containers', or enable 'status_reporter'"
            )

        config = AppConfig(
            schedule=schedule,
            runtime=runtime,
            pipelines=pipelines,
            scheduled_containers=scheduled_containers,
            state_file=AppConfig._resolve_state_file(reader, source_path),
            source_path=source_path,
            status_reporter=status_reporter,
        )
        config.validate()
        return config

    @staticmethod
    def _build_pipelines(reader: FieldReader) -> List[PipelineConfig]:
        raw_pipelines: Dict[str, Any] = reader.mapping("pipelines", {})
        return [
            PipelineConfig.from_dict(name, payload)
            for name, payload in raw_pipelines.items()
        ]

    @staticmethod
    def _build_scheduled_containers(
        reader: FieldReader,
    ) -> List[ScheduledContainerConfig]:
        raw_items = reader.sequence("scheduled_containers", [])
        return [
            ScheduledContainerConfig.from_dict(item, index)
            for index, item in enumerate(raw_items)
        ]

    @staticmethod
    def _resolve_state_file(reader: FieldReader, source_path: Path | None) -> Path:
        raw_value = reader.optional_text("state_file")
        base_dir = source_path.parent if source_path else Path.cwd()

        if raw_value is None:
            stem = source_path.stem if source_path else "docker_schedule_config"
            return (base_dir / f"{stem}_state.json").resolve()

        state_file = Path(raw_value).expanduser()
        if not state_file.is_absolute():
            state_file = base_dir / state_file
        return state_file.resolve()

    # -------------------------------------------------------------- validate

    def validate(self) -> None:
        """Reject configurations that cannot work at runtime."""
        duplicates = [
            f"{name} (used {count} times)"
            for name, count in Counter(self.all_container_names()).items()
            if count > 1
        ]
        if duplicates:
            raise ConfigError(
                "The same container is referenced more than once, which would make "
                "the orchestrator start it twice in the same cycle: "
                + ", ".join(sorted(duplicates))
            )

        if (
            not self.enabled_pipelines
            and not self.enabled_scheduled_containers
            and not self.status_reporter.enabled
        ):
            _LOG.warning(
                "Every pipeline and scheduled container is disabled; runs will do nothing."
            )

    # -------------------------------------------------------------- accessors

    @property
    def enabled_pipelines(self) -> List[PipelineConfig]:
        return [pipeline for pipeline in self.pipelines if pipeline.enabled]

    @property
    def enabled_scheduled_containers(self) -> List[ScheduledContainerConfig]:
        return [item for item in self.scheduled_containers if item.enabled]

    def all_container_names(self) -> List[str]:
        names: List[str] = []
        for pipeline in self.pipelines:
            names.extend(pipeline.container_names())
        names.extend(item.name for item in self.scheduled_containers)
        return names

    def describe(self) -> str:
        """Human-readable one-paragraph summary used by ``--validate-config``."""
        lines = [
            f"config file        : {self.source_path or '<in memory>'}",
            f"state file         : {self.state_file}",
            f"timezone           : {self.schedule.timezone}",
            f"hourly tick        : minute {self.schedule.hourly_minute}",
            f"daily snapshots    : at or after {self.schedule.daily_time_text}",
            f"pipelines          : {len(self.enabled_pipelines)} enabled "
            f"of {len(self.pipelines)}",
            f"scheduled containers: {len(self.enabled_scheduled_containers)} enabled "
            f"of {len(self.scheduled_containers)}",
            self.status_reporter.describe(),
        ]
        for pipeline in self.pipelines:
            state = "" if pipeline.enabled else " [disabled]"
            lines.append(
                f"  - pipeline {pipeline.name}{state}: {pipeline.sync} -> "
                f"{pipeline.hourly_snapshot} / {pipeline.daily_snapshot}"
                + (" (sync failures allowed)" if pipeline.allow_sync_failure else "")
            )
        for item in self.scheduled_containers:
            state = "" if item.enabled else " [disabled]"
            lines.append(f"  - scheduled {item.description}{state}")
        return "\n".join(lines)
