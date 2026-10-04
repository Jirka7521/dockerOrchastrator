"""CPU usage, load and frequency."""

from __future__ import annotations

from typing import Dict, List, Tuple

from orchestrator.status.host_paths import HostPaths


class CpuCollector:
    """Usage from /proc/stat deltas, load from /proc/loadavg, clock from cpufreq.

    Usage is (busy time) / (total time) between two readings. iowait counts as
    idle -- a CPU waiting for a disk is not doing work -- which matches what
    top and htop show.
    """

    def __init__(self, paths: HostPaths) -> None:
        self._paths = paths
        self._previous: Dict[str, Tuple[int, int]] | None = None

    def collect(self) -> dict:
        current = self._read_stat()
        usage: float | None = None
        per_core: List[float] = []

        if self._previous is not None:
            usage = self._usage(self._previous.get("cpu"), current.get("cpu"))
            cores = sorted((name for name in current if name != "cpu"), key=lambda n: int(n[3:]))
            values = [self._usage(self._previous.get(name), current[name]) for name in cores]
            if values and all(value is not None for value in values):
                per_core = [round(value, 2) for value in values if value is not None]
        self._previous = current

        load1, load5, load15 = self._read_loadavg()
        cpufreq = self._paths.sys / "devices/system/cpu/cpu0/cpufreq"
        frequency = self._paths.read_int(cpufreq / "scaling_cur_freq")
        maximum = self._paths.read_int(cpufreq / "cpuinfo_max_freq")

        return {
            "usagePercent": None if usage is None else round(usage, 2),
            "perCorePercent": per_core,
            "load1": load1,
            "load5": load5,
            "load15": load15,
            # cpufreq reports kHz.
            "frequencyMhz": None if frequency is None else round(frequency / 1000, 1),
            "maxFrequencyMhz": None if maximum is None else round(maximum / 1000, 1),
            "coreCount": max(1, sum(1 for name in current if name != "cpu")),
        }

    def _read_stat(self) -> Dict[str, Tuple[int, int]]:
        """(busy, total) jiffies per cpu line of /proc/stat."""
        text = self._paths.read_text(self._paths.proc / "stat") or ""
        result: Dict[str, Tuple[int, int]] = {}
        for line in text.splitlines():
            fields = line.split()
            if not fields or not fields[0].startswith("cpu"):
                continue
            name = fields[0]
            if name != "cpu" and not name[3:].isdigit():
                continue
            # user nice system idle iowait irq softirq steal -- guest time is
            # already included in user and must not be counted twice.
            numbers = [int(value) for value in fields[1:9]]
            numbers += [0] * (8 - len(numbers))
            idle = numbers[3] + numbers[4]
            total = sum(numbers)
            result[name] = (total - idle, total)
        return result

    @staticmethod
    def _usage(before: Tuple[int, int] | None, after: Tuple[int, int] | None) -> float | None:
        if before is None or after is None:
            return None
        busy = after[0] - before[0]
        total = after[1] - before[1]
        if total <= 0 or busy < 0:
            return None
        return max(0.0, min(100.0, 100.0 * busy / total))

    def _read_loadavg(self) -> Tuple[float, float, float]:
        text = self._paths.read_text(self._paths.proc / "loadavg") or "0 0 0"
        parts = text.split()
        try:
            return float(parts[0]), float(parts[1]), float(parts[2])
        except (IndexError, ValueError):
            return 0.0, 0.0, 0.0
