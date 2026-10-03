"""Read/write throughput and utilisation per physical disk."""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Tuple

from orchestrator.status.collectors.block_device_resolver import WHOLE_DISK
from orchestrator.status.host_paths import HostPaths

#: /proc/diskstats counts sectors of 512 bytes, whatever the hardware uses.
SECTOR_BYTES = 512


class DiskIoCollector:
    """Rates from /proc/diskstats deltas between two calls.

    Utilisation is the share of wall time the device had I/O in flight
    (iostat's %util). The first call has nothing to compare against and
    returns an empty list rather than misleading zeros.
    """

    def __init__(self, paths: HostPaths, clock: Callable[[], float] = time.monotonic) -> None:
        self._paths = paths
        self._clock = clock
        self._previous: Tuple[float, Dict[str, Tuple[int, int, int]]] | None = None

    def collect(self) -> List[dict]:
        now = self._clock()
        current = self._read()
        previous, self._previous = self._previous, (now, current)
        if previous is None:
            return []

        elapsed = now - previous[0]
        if elapsed <= 0:
            return []

        result = []
        for name, (read_sectors, written_sectors, io_ms) in sorted(current.items()):
            before = previous[1].get(name)
            if before is None:
                continue
            read_delta = read_sectors - before[0]
            write_delta = written_sectors - before[1]
            busy_delta = io_ms - before[2]
            if read_delta < 0 or write_delta < 0 or busy_delta < 0:
                continue  # counters reset (device re-attached)
            result.append(
                {
                    "device": name,
                    "readBytesPerSecond": round(read_delta * SECTOR_BYTES / elapsed, 1),
                    "writeBytesPerSecond": round(write_delta * SECTOR_BYTES / elapsed, 1),
                    "utilizationPercent": round(min(100.0, busy_delta / (elapsed * 1000.0) * 100.0), 2),
                    "readBytesTotal": read_sectors * SECTOR_BYTES,
                    "writeBytesTotal": written_sectors * SECTOR_BYTES,
                }
            )
        return result

    def _read(self) -> Dict[str, Tuple[int, int, int]]:
        text = self._paths.read_text(self._paths.proc / "diskstats") or ""
        stats: Dict[str, Tuple[int, int, int]] = {}
        for line in text.splitlines():
            fields = line.split()
            if len(fields) < 14 or not WHOLE_DISK.match(fields[2]):
                continue
            try:
                # sectors read (field 6), sectors written (10), ms doing I/O (13).
                stats[fields[2]] = (int(fields[5]), int(fields[9]), int(fields[12]))
            except ValueError:
                continue
        return stats
