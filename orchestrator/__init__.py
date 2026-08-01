"""Docker job orchestrator.

The package is split into focused sub-packages:

- :mod:`orchestrator.config` — configuration objects and loading/validation
- :mod:`orchestrator.docker_cli` — everything that talks to the Docker CLI
- :mod:`orchestrator.state` — persistent run state
- :mod:`orchestrator.scheduling` — pure time/schedule logic (no I/O)
- :mod:`orchestrator.runtime` — task execution, orchestration and the daemon

Every class lives in its own module; this file only re-exports the names that
form the public API so callers can write ``from orchestrator import Orchestrator``.
"""

from __future__ import annotations

from orchestrator.config.app_config import AppConfig
from orchestrator.config.config_loader import ConfigLoader
from orchestrator.config.pipeline_config import PipelineConfig
from orchestrator.config.runtime_config import RuntimeConfig
from orchestrator.config.schedule_config import ScheduleConfig
from orchestrator.config.scheduled_container_config import ScheduledContainerConfig
from orchestrator.docker_cli.docker_runner import DockerRunner
from orchestrator.errors import ConfigError, OrchestratorError
from orchestrator.runtime.orchestrator import Orchestrator
from orchestrator.runtime.orchestrator_daemon import OrchestratorDaemon
from orchestrator.runtime.orchestrator_factory import OrchestratorFactory
from orchestrator.runtime.run_report import RunReport

__version__ = "2.0.0"

__all__ = [
    "AppConfig",
    "ConfigError",
    "ConfigLoader",
    "DockerRunner",
    "Orchestrator",
    "OrchestratorDaemon",
    "OrchestratorError",
    "OrchestratorFactory",
    "PipelineConfig",
    "RunReport",
    "RuntimeConfig",
    "ScheduleConfig",
    "ScheduledContainerConfig",
    "__version__",
]
