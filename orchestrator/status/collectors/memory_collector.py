"""Memory and swap from /proc/meminfo."""

from __future__ import annotations

from typing import Dict

from orchestrator.status.host_paths import HostPaths


class MemoryCollector:
    """Used memory is total minus *available*, the kernel's own estimate of
    what can be handed out without swapping -- the same figure `free` shows.
    Subtracting only "free" would count the page cache as used and make a
    healthy server look full."""

    def __init__(self, paths: HostPaths) -> None:
        self._paths = paths

    def collect(self) -> dict:
        info = self._read()
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", info.get("MemFree", 0))
        swap_total = info.get("SwapTotal", 0)
        return {
            "totalBytes": total,
            "availableBytes": available,
            "usedBytes": max(0, total - available),
            "buffersBytes": info.get("Buffers", 0),
            # Reclaimable slab is cache in all but name; `free` counts it too.
            "cachedBytes": info.get("Cached", 0) + info.get("SReclaimable", 0),
            "swapTotalBytes": swap_total,
            "swapUsedBytes": max(0, swap_total - info.get("SwapFree", 0)),
        }

    def _read(self) -> Dict[str, int]:
        text = self._paths.read_text(self._paths.proc / "meminfo") or ""
        values: Dict[str, int] = {}
        for line in text.splitlines():
            name, _, rest = line.partition(":")
            parts = rest.split()
            if not parts:
                continue
            try:
                value = int(parts[0])
            except ValueError:
                continue
            # Every figure is in kB except the page counters (no unit).
            values[name.strip()] = value * 1024 if len(parts) > 1 and parts[1] == "kB" else value
        return values
