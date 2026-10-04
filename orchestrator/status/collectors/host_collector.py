"""Composes the host report from the individual collectors."""

from __future__ import annotations

import socket
from datetime import datetime, timezone

from orchestrator.status.collectors.cpu_collector import CpuCollector
from orchestrator.status.collectors.disk_io_collector import DiskIoCollector
from orchestrator.status.collectors.docker_network_mapper import DockerNetworkMapper
from orchestrator.status.collectors.filesystem_collector import FilesystemCollector
from orchestrator.status.collectors.memory_collector import MemoryCollector
from orchestrator.status.collectors.network_collector import NetworkCollector
from orchestrator.status.collectors.thermal_collector import ThermalCollector
from orchestrator.status.collectors.throttle_collector import ThrottleCollector
from orchestrator.status.host_paths import HostPaths


class HostCollector:
    """One host report: CPU, memory, temperatures, throttling, filesystems,
    disk I/O and every interface with the Docker networks it carries."""

    def __init__(
        self,
        paths: HostPaths,
        cpu: CpuCollector,
        memory: MemoryCollector,
        thermal: ThermalCollector,
        throttle: ThrottleCollector,
        filesystems: FilesystemCollector,
        disk_io: DiskIoCollector,
        network: NetworkCollector,
        network_mapper: DockerNetworkMapper,
    ) -> None:
        self._paths = paths
        self._cpu = cpu
        self._memory = memory
        self._thermal = thermal
        self._throttle = throttle
        self._filesystems = filesystems
        self._disk_io = disk_io
        self._network = network
        self._network_mapper = network_mapper

    def collect(self) -> dict:
        collected_at = datetime.now(timezone.utc)
        return {
            "collectedAt": collected_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "hostname": socket.gethostname()[:253],
            "uptimeSeconds": self._uptime(),
            "cpu": self._cpu.collect(),
            "memory": self._memory.collect(),
            "temperatures": self._thermal.collect(),
            "throttledRaw": self._throttle.collect(),
            "filesystems": self._filesystems.collect(),
            "diskIo": self._disk_io.collect(),
            "interfaces": self._network.collect(self._network_mapper.interfaces()),
        }

    def _uptime(self) -> float:
        text = self._paths.read_text(self._paths.proc / "uptime") or "0"
        try:
            return float(text.split()[0])
        except (IndexError, ValueError):
            return 0.0
