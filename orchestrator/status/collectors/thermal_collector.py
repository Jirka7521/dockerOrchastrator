"""Temperatures from the kernel's thermal zones and hwmon sensors."""

from __future__ import annotations

import re
from typing import Dict, List, Set

from orchestrator.status.host_paths import HostPaths

_SOURCE_INVALID = re.compile(r"[^a-z0-9_.:-]+")

#: Friendlier labels for well-known zone types.
_LABELS = {
    "cpu-thermal": "SoC",
    "cpu_thermal": "SoC",
    "x86_pkg_temp": "CPU package",
    "acpitz": "Mainboard (ACPI)",
}


class ThermalCollector:
    """Every temperature the kernel exposes, deduplicated.

    On a Raspberry Pi the SoC sensor appears twice -- as thermal_zone0
    (``cpu-thermal``) and as hwmon ``cpu_thermal`` -- so hwmon devices whose
    name matches a thermal zone are skipped. Disk temperatures come from SMART
    (see SmartCollector), not from here.
    """

    def __init__(self, paths: HostPaths) -> None:
        self._paths = paths

    def collect(self) -> List[dict]:
        readings: List[dict] = []
        seen: Set[str] = set()

        thermal = self._paths.sys / "class/thermal"
        for zone in sorted(thermal.glob("thermal_zone*")) if thermal.exists() else []:
            zone_type = (self._paths.read_text(zone / "type") or zone.name).strip()
            millidegrees = self._paths.read_int(zone / "temp")
            if millidegrees is None:
                continue
            source = self._source(zone_type)
            seen.add(self._normalised(zone_type))
            readings.append(self._reading(source, _LABELS.get(zone_type, zone_type), millidegrees))

        hwmon = self._paths.sys / "class/hwmon"
        for device in sorted(hwmon.glob("hwmon*")) if hwmon.exists() else []:
            name = (self._paths.read_text(device / "name") or device.name).strip()
            if self._normalised(name) in seen:
                continue
            for sensor in sorted(device.glob("temp*_input")):
                millidegrees = self._paths.read_int(sensor)
                if millidegrees is None:
                    continue
                index = sensor.name[: -len("_input")]
                label = (self._paths.read_text(device / f"{index}_label") or "").strip()
                source = self._source(f"{name}-{index}")
                readings.append(self._reading(source, label or f"{name} {index}", millidegrees))

        return self._unique(readings)

    @staticmethod
    def _reading(source: str, label: str, millidegrees: int) -> dict:
        # One decimal: sensors resolve no better, and it avoids binary
        # rounding surprises (76.445 is stored as 76.44499...).
        return {"source": source, "label": label[:64], "celsius": round(millidegrees / 1000.0, 1)}

    @staticmethod
    def _normalised(name: str) -> str:
        return name.strip().lower().replace("_", "-")

    @staticmethod
    def _source(name: str) -> str:
        cleaned = _SOURCE_INVALID.sub("-", name.strip().lower()).strip("-.:_")
        return (cleaned or "sensor")[:64]

    @staticmethod
    def _unique(readings: List[dict]) -> List[dict]:
        result: List[dict] = []
        counts: Dict[str, int] = {}
        for reading in readings:
            source = reading["source"]
            counts[source] = counts.get(source, 0) + 1
            if counts[source] > 1:
                reading = {**reading, "source": f"{source}-{counts[source]}"[:64]}
            result.append(reading)
        return result
