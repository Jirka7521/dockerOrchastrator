"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import List, Sequence

from orchestrator import __version__
from orchestrator.config.app_config import AppConfig
from orchestrator.config.config_loader import ConfigLoader
from orchestrator.errors import ConfigError, OrchestratorError
from orchestrator.logging_setup import LEVELS, LoggingSetup
from orchestrator.runtime.orchestrator_daemon import OrchestratorDaemon
from orchestrator.runtime.orchestrator_factory import OrchestratorFactory
from orchestrator.runtime.run_report import EXIT_FAILED, EXIT_OK

DEFAULT_CONFIG = "docker_schedule_config.json"

EXIT_CONFIG_ERROR = 2
EXIT_INTERRUPTED = 130


class Cli:
    """Parses arguments and wires the application together."""

    def __init__(self, argv: Sequence[str] | None = None) -> None:
        self.argv: List[str] | None = list(argv) if argv is not None else None
        self.log = logging.getLogger("orchestrator.cli")

    # ---------------------------------------------------------------- parsing

    @staticmethod
    def build_parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog="docker-orchestrator",
            description=(
                "Run Docker containers in a configured order on an hourly and "
                "daily schedule."
            ),
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        )
        parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
        parser.add_argument(
            "--config",
            default=DEFAULT_CONFIG,
            help="Path to the JSON config file.",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run a single cycle and exit instead of running as a daemon.",
        )
        parser.add_argument(
            "--force-daily",
            action="store_true",
            help="Run daily snapshots even if today already had them.",
        )
        parser.add_argument(
            "--ignore-hourly-tick",
            action="store_true",
            help="Run even when the current minute is not the configured hourly minute.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would run without touching Docker. Implies --once.",
        )
        parser.add_argument(
            "--validate-config",
            action="store_true",
            help="Load and check the config file, print a summary, and exit.",
        )
        parser.add_argument(
            "--exit-zero-on-failure",
            action="store_true",
            help="Always exit with 0, even when containers failed.",
        )
        parser.add_argument(
            "--docker-executable",
            default="docker",
            help="Name or path of the Docker CLI binary.",
        )
        parser.add_argument(
            "--log-level",
            default="INFO",
            choices=list(LEVELS),
            help="Console and file logging level.",
        )
        parser.add_argument(
            "--log-file",
            default=None,
            help="Also write logs to this file (rotating, 5 MB x 3).",
        )
        return parser

    # -------------------------------------------------------------------- run

    def run(self) -> int:
        args = self.build_parser().parse_args(self.argv)
        LoggingSetup.configure(level=args.log_level, log_file=args.log_file)

        try:
            loader = ConfigLoader(Path(args.config))
            config = loader.load()
        except ConfigError as exc:
            self.log.error("Configuration problem: %s", exc)
            return EXIT_CONFIG_ERROR
        except Exception:  # noqa: BLE001 - last line of defence
            self.log.exception("Unexpected error while loading the configuration.")
            return EXIT_CONFIG_ERROR

        if args.validate_config:
            self.log.info("Configuration is valid:\n%s", config.describe())
            return EXIT_OK

        factory = OrchestratorFactory(docker_executable=args.docker_executable)

        try:
            if args.once or args.dry_run:
                exit_code = self._run_once(config, factory, args)
            else:
                exit_code = self._run_daemon(config, loader, factory)
        except KeyboardInterrupt:
            self.log.info("Interrupted by user.")
            return EXIT_INTERRUPTED
        except OrchestratorError as exc:
            self.log.error("Orchestration failed: %s", exc)
            return EXIT_FAILED
        except Exception:  # noqa: BLE001 - last line of defence
            self.log.exception("Unexpected error; exiting.")
            return EXIT_FAILED

        if exit_code != EXIT_OK and args.exit_zero_on_failure:
            self.log.info("Exiting with 0 because --exit-zero-on-failure was requested.")
            return EXIT_OK
        return exit_code

    def _run_once(
        self,
        config: AppConfig,
        factory: OrchestratorFactory,
        args: argparse.Namespace,
    ) -> int:
        orchestrator = factory.create(config)
        report = orchestrator.run(
            now=config.schedule.create_clock().now(),
            force_daily=args.force_daily,
            ignore_hourly_tick=args.ignore_hourly_tick or args.dry_run,
            dry_run=args.dry_run,
        )
        self.log.info("Result: %s", report.summary())
        return report.exit_code

    def _run_daemon(
        self,
        config: AppConfig,
        loader: ConfigLoader,
        factory: OrchestratorFactory,
    ) -> int:
        self.log.info("Configuration loaded:\n%s", config.describe())
        daemon = OrchestratorDaemon(
            orchestrator=factory.create(config),
            config_loader=loader,
            factory=factory,
        )
        daemon.install_signal_handlers()
        try:
            return daemon.run_forever()
        except KeyboardInterrupt:
            daemon.request_stop()
            self.log.info("Interrupted; daemon stopped.")
            return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point used by ``docker_orchestrator.py`` and ``python -m orchestrator``."""
    return Cli(argv).run()
