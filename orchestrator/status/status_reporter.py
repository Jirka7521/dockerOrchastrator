"""Runs the status reporting channels alongside the scheduling daemon."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Set, Tuple

from orchestrator.docker_cli.docker_command import DockerCommand
from orchestrator.errors import ConfigError, OrchestratorError
from orchestrator.status.collectors.block_device_resolver import BlockDeviceResolver
from orchestrator.status.collectors.connectivity_collector import ConnectivityCollector
from orchestrator.status.collectors.container_collector import ContainerCollector
from orchestrator.status.collectors.cpu_collector import CpuCollector
from orchestrator.status.collectors.disk_io_collector import DiskIoCollector
from orchestrator.status.collectors.docker_network_mapper import DockerNetworkMapper
from orchestrator.status.collectors.filesystem_collector import FilesystemCollector
from orchestrator.status.collectors.host_collector import HostCollector
from orchestrator.status.collectors.log_fetcher import LogFetcher
from orchestrator.status.collectors.memory_collector import MemoryCollector
from orchestrator.status.collectors.network_collector import NetworkCollector
from orchestrator.status.collectors.smart_collector import SmartCollector
from orchestrator.status.collectors.thermal_collector import ThermalCollector
from orchestrator.status.collectors.throttle_collector import ThrottleCollector
from orchestrator.status.command_executor import COMMAND_ID, CommandExecutor
from orchestrator.status.command_types import CONTAINER_ACTIONS
from orchestrator.status.container_control import ContainerControl
from orchestrator.status.failure_log_throttle import FailureLogThrottle
from orchestrator.status.host_paths import HostPaths
from orchestrator.status.power_control import PowerControl
from orchestrator.status.request_signer import RequestSigner
from orchestrator.status.status_api_client import StatusApiClient, StatusApiRejected, StatusApiUnavailable
from orchestrator.status.status_config import StatusReporterConfig


#: Container actions run beside the command poll, so a slow ``docker stop``
#: holds up neither log requests nor other actions. A small, fixed number:
#: the API allows one action per container, and rate-limits the rest.
ACTION_WORKERS = 2


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class _Collectors:
    host: HostCollector
    containers: ContainerCollector
    connectivity: ConnectivityCollector
    smart: SmartCollector
    logs: LogFetcher


class StatusReporter:
    """One thread per report channel, plus the command long-poll.

    The channels are independent on purpose: a smartctl call that takes a
    minute on a sleepy USB bridge must not delay the container list, and an
    API outage on one endpoint must not stop the others. Every thread is a
    daemon thread that checks one shared stop event, so the scheduling daemon
    stays in charge of the process's lifetime: when it exits, reporting ends.

    Container actions run on worker threads of their own and wake the
    container channel when done, so the dashboard sees the new state at once.
    A reboot or power-off runs only after the API took its answer.
    """

    def __init__(
        self,
        config: StatusReporterConfig,
        command: DockerCommand,
        paths: HostPaths | None = None,
        client_factory: Callable[[StatusReporterConfig, bytes], StatusApiClient] | None = None,
        clock: Callable[[], float] = time.monotonic,
        power: PowerControl | None = None,
    ) -> None:
        self._config = config
        self._command = command
        self._power = power
        self._paths = paths or HostPaths()
        self._client_factory = client_factory or (
            lambda cfg, key: StatusApiClient(cfg.api_url, RequestSigner(key), cfg.request_timeout_seconds)
        )
        self._clock = clock
        self._stop = threading.Event()
        self._wake_containers = threading.Event()
        self._threads: List[threading.Thread] = []
        self._known_containers: Set[str] = set()
        self._lock = threading.Lock()
        self._log = logging.getLogger(self.__class__.__name__)

    # -------------------------------------------------------------- lifecycle

    @property
    def running(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)

    def start(self) -> bool:
        """Start every channel. False (and nothing started) when disabled.

        Raises :class:`ConfigError` for an unusable key file, so a broken
        setup is reported loudly instead of silently reporting nothing.
        """
        if not self._config.enabled:
            self._log.info("Status reporter is disabled.")
            return False

        key = self._config.load_key()
        client = self._client_factory(self._config, key)
        collectors = self.build_collectors()
        config = self._config
        executor = CommandExecutor(
            collectors.logs,
            self.known_containers,
            config.max_log_lines,
            self.refresh_containers,
            allowed=config.allowed_commands,
            containers=ContainerControl(self._command, config.container_action_timeout_seconds),
            power=self._power or PowerControl(config.systemctl_executable),
        )

        self._stop = threading.Event()
        self._wake_containers = threading.Event()
        channels: List[tuple] = [
            ("host", config.host_interval_seconds, lambda: client.send_report("POST", "/host", collectors.host.collect()), collectors.host.collect, None),
            ("containers", config.containers_interval_seconds, lambda: self._report_containers(client, collectors), None, self._wake_containers),
            ("connectivity", config.connectivity_interval_seconds, lambda: client.send_report("POST", "/connectivity", self._stamped(collectors.connectivity.collect())), None, None),
            ("disks", config.disk_health_interval_seconds, lambda: client.send_report("POST", "/disks", self._stamped(collectors.smart.collect())), None, None),
        ]

        self._threads = [
            self._thread(f"status-{name}", self._periodic, name, interval, work, prime, wake)
            for name, interval, work, prime, wake in channels
        ]
        actions: "queue.Queue[Tuple[str, dict]]" = queue.Queue()
        self._threads.append(self._thread("status-commands", self._command_loop, client, executor, actions))
        self._threads.extend(
            self._thread(f"status-action-{index}", self._action_worker, client, executor, actions)
            for index in range(ACTION_WORKERS)
        )

        self._log.info(
            "Status reporter started: %s (%s; allowed commands: %s)",
            config.api_url,
            ", ".join(f"{name} every {interval:g}s" for name, interval, _, _, _ in channels),
            ", ".join(config.allowed_commands) or "none",
        )
        return True

    def stop(self, timeout_seconds: float = 3.0) -> None:
        self._stop.set()
        self._wake_containers.set()
        deadline = self._clock() + timeout_seconds
        for thread in self._threads:
            # The command thread may sit in a long-poll; it is a daemon thread
            # and ends with the process, so it is not waited for indefinitely.
            thread.join(max(0.0, deadline - self._clock()))
        self._threads = []

    def reconfigure(self, config: StatusReporterConfig) -> None:
        """Apply a reloaded config: restart only if the section changed."""
        if config == self._config:
            return
        self._log.info("Status reporter configuration changed; restarting it.")
        self.stop()
        self._config = config
        try:
            self.start()
        except ConfigError as exc:
            self._log.error("Status reporter not restarted: %s", exc)

    def known_containers(self) -> Set[str]:
        with self._lock:
            return set(self._known_containers)

    def refresh_containers(self) -> Set[str]:
        """Re-list container names straight from Docker (cheap: names only)."""
        try:
            result = self._command.run(["ps", "-a", "--format", "{{.Names}}"])
        except OrchestratorError as exc:
            self._log.debug("Could not refresh the container list: %s", exc)
            return self.known_containers()
        if result.ok:
            names = {name.strip() for name in result.out.splitlines() if name.strip()}
            with self._lock:
                self._known_containers = names
            return set(names)
        return self.known_containers()

    # ------------------------------------------------------------- collection

    def build_collectors(self) -> _Collectors:
        config = self._config
        paths = self._paths
        resolver = BlockDeviceResolver(paths)
        host = HostCollector(
            paths=paths,
            cpu=CpuCollector(paths),
            memory=MemoryCollector(paths),
            thermal=ThermalCollector(paths),
            throttle=ThrottleCollector(paths, config.vcgencmd_executable),
            filesystems=FilesystemCollector(paths, resolver),
            disk_io=DiskIoCollector(paths),
            network=NetworkCollector(paths),
            network_mapper=DockerNetworkMapper(self._command),
        )
        return _Collectors(
            host=host,
            containers=ContainerCollector(self._command),
            connectivity=ConnectivityCollector(config.ping_targets, config.ping_count, config.dns_probe_host),
            smart=SmartCollector(paths, config.smartctl_executable),
            logs=LogFetcher(self._command, config.max_log_lines, config.max_log_bytes),
        )

    def snapshot(self) -> Dict[str, object]:
        """Every report, collected once and returned instead of sent.

        Used by ``--status-snapshot`` to check what the host looks like to the
        collectors -- no key, no network to the API needed.
        """
        collectors = self.build_collectors()
        collectors.host.collect()  # prime the rate counters
        time.sleep(1.0)
        result: Dict[str, object] = {
            "host": collectors.host.collect(),
            "containers": self._stamped(collectors.containers.collect()),
            "connectivity": self._stamped(collectors.connectivity.collect()),
            "disks": self._stamped(collectors.smart.collect()),
        }
        return result

    def test_round(self) -> Dict[str, str]:
        """Send every report once and poll for commands once; outcome per channel."""
        key = self._config.load_key()
        client = self._client_factory(self._config, key)
        collectors = self.build_collectors()
        collectors.host.collect()
        time.sleep(1.0)

        steps: List[tuple] = [
            ("host", lambda: client.send_report("POST", "/host", collectors.host.collect())),
            ("containers", lambda: self._report_containers(client, collectors)),
            ("connectivity", lambda: client.send_report("POST", "/connectivity", self._stamped(collectors.connectivity.collect()))),
            ("disks", lambda: client.send_report("POST", "/disks", self._stamped(collectors.smart.collect()))),
            ("commands", lambda: client.poll_commands(0, self._config.allowed_commands)),
        ]

        outcome: Dict[str, str] = {}
        for name, step in steps:
            try:
                step()
                outcome[name] = "ok"
            except OrchestratorError as exc:
                outcome[name] = f"FAILED: {exc}"
        return outcome

    # ---------------------------------------------------------------- threads

    def _thread(self, name: str, target: Callable, *args: object) -> threading.Thread:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        thread.start()
        return thread

    def _periodic(
        self,
        name: str,
        interval: float,
        work: Callable[[], object],
        prime: Callable[[], object] | None,
        wake: threading.Event | None = None,
    ) -> None:
        """Run ``work`` every ``interval`` -- or at once when ``wake`` is set."""
        throttle = FailureLogThrottle(self._log, f"Status {name} report")
        stop = self._stop

        if prime is not None:
            # Rate collectors need a first reading to compute the first rate.
            try:
                prime()
            except Exception as exc:  # noqa: BLE001 - priming is best effort
                self._log.debug("Priming the %s collector failed: %s", name, exc)
            next_run = self._clock() + min(interval, 5.0)
        else:
            next_run = self._clock() + 1.0

        while True:
            timeout = max(0.0, next_run - self._clock())
            woken = False
            if wake is None:
                if stop.wait(timeout):
                    break
            else:
                # stop() sets the wake event too, so this wait ends either way.
                woken = wake.wait(timeout)
                if stop.is_set():
                    break
                wake.clear()

            try:
                work()
                throttle.success()
            except (StatusApiUnavailable, StatusApiRejected, OrchestratorError) as exc:
                throttle.failure(str(exc))
            except Exception as exc:  # noqa: BLE001 - a reporting thread must survive anything
                throttle.failure(f"unexpected {type(exc).__name__}: {exc}")
                self._log.debug("Unexpected error in the %s channel", name, exc_info=True)

            now = self._clock()
            if woken:
                # An extra run: the regular cadence starts again from here.
                next_run = now + interval
                continue
            next_run += interval
            if next_run < now:
                # The work overran its interval: skip the missed runs instead
                # of firing them back to back.
                next_run = now + interval

    def _command_loop(
        self, client: StatusApiClient, executor: CommandExecutor, actions: "queue.Queue[Tuple[str, dict]]"
    ) -> None:
        throttle = FailureLogThrottle(self._log, "Status command poll")
        backoff = 5.0
        stop = self._stop

        while not stop.is_set():
            try:
                commands = client.poll_commands(self._config.command_poll_seconds, self._config.allowed_commands)
                throttle.success()
                backoff = 5.0
            except (StatusApiUnavailable, StatusApiRejected) as exc:
                throttle.failure(str(exc))
                stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            except Exception as exc:  # noqa: BLE001 - the poll loop must survive anything
                throttle.failure(f"unexpected {type(exc).__name__}: {exc}")
                stop.wait(backoff)
                continue

            for command in commands:
                command_id = command.get("id")
                if not isinstance(command_id, str) or not COMMAND_ID.match(command_id):
                    self._log.warning("Ignoring a command with an invalid id.")
                    continue
                if command.get("type") in CONTAINER_ACTIONS:
                    # Checked and run by a worker; a slow stop must not hold the poll.
                    actions.put((command_id, command))
                    continue

                outcome = executor.execute(command)
                delivered = self._deliver(client, command_id, outcome.result)
                if outcome.after_delivery is not None:
                    if delivered:
                        outcome.after_delivery()
                    else:
                        self._log.warning(
                            "The API did not take the answer to command %s, so it is not carried out.", command_id
                        )

    def _action_worker(
        self, client: StatusApiClient, executor: CommandExecutor, actions: "queue.Queue[Tuple[str, dict]]"
    ) -> None:
        stop = self._stop
        while not stop.is_set():
            try:
                command_id, command = actions.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                outcome = executor.execute(command)
            except Exception as exc:  # noqa: BLE001 - a worker must survive anything
                self._log.warning("Command %s failed unexpectedly: %s", command_id, exc, exc_info=True)
                outcome_result = {"ok": False, "error": f"unexpected {type(exc).__name__}", "truncated": False, "lines": []}
            else:
                outcome_result = outcome.result
            # A fresh container list now, not at the next tick: the dashboard
            # refreshes right after the answer and should see the new state.
            self._wake_containers.set()
            self._deliver(client, command_id, outcome_result)

    def _deliver(self, client: StatusApiClient, command_id: str, result: dict) -> bool:
        """Post a result. False when the API did not take it -- nobody waits, or it is away."""
        try:
            return client.send_command_result(command_id, result)
        except (StatusApiUnavailable, StatusApiRejected) as exc:
            self._log.warning("Could not deliver the result of command %s: %s", command_id, exc)
            return False

    def _report_containers(self, client: StatusApiClient, collectors: _Collectors) -> None:
        snapshot = self._stamped(collectors.containers.collect())
        with self._lock:
            # Updated before sending: the command executor must know the
            # current containers even while the API is unreachable.
            self._known_containers = {c["name"] for c in snapshot["containers"] if c.get("name")}
        client.send_report("PUT", "/containers", snapshot)

    @staticmethod
    def _stamped(report: dict) -> dict:
        return {"collectedAt": utc_now_iso(), **report}
