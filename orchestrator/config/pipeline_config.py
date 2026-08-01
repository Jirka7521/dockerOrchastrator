"""One sync -> snapshot processing chain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping

from orchestrator.config.field_reader import FieldReader
from orchestrator.docker_cli.container_name import ContainerName
from orchestrator.errors import ConfigError

KNOWN_KEYS = (
    "sync",
    "hourly_snapshot",
    "daily_snapshot",
    "allow_sync_failure",
    "enabled",
    "timeout_seconds",
)


@dataclass(frozen=True)
class PipelineConfig:
    """Container names that define one processing chain."""

    name: str
    sync: str
    hourly_snapshot: str
    daily_snapshot: str
    allow_sync_failure: bool = False
    enabled: bool = True
    timeout_seconds: float | None = None

    @staticmethod
    def from_dict(
        name: str,
        data: Mapping[str, Any],
        path: str = "pipelines",
    ) -> "PipelineConfig":
        if not name.strip():
            raise ConfigError(f"{path} contains an empty pipeline name")

        pipeline_path = f"{path}.{name}"
        reader = FieldReader(data, pipeline_path)
        reader.warn_unknown_keys(KNOWN_KEYS)

        def container(key: str) -> str:
            return ContainerName.validate(reader.text(key), f"{pipeline_path}.{key}")

        return PipelineConfig(
            name=name.strip(),
            sync=container("sync"),
            hourly_snapshot=container("hourly_snapshot"),
            daily_snapshot=container("daily_snapshot"),
            allow_sync_failure=reader.boolean("allow_sync_failure", False),
            enabled=reader.boolean("enabled", True),
            timeout_seconds=reader.optional_number("timeout_seconds", minimum=1),
        )

    def container_names(self, include_daily: bool = True) -> List[str]:
        names = [self.sync, self.hourly_snapshot]
        if include_daily:
            names.append(self.daily_snapshot)
        return names

    def snapshots(self, include_daily: bool) -> List[str]:
        names = [self.hourly_snapshot]
        if include_daily:
            names.append(self.daily_snapshot)
        return names
