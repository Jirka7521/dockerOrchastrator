"""SMART health of every physical disk, through smartctl."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence

from orchestrator.status.collectors.block_device_resolver import WHOLE_DISK
from orchestrator.status.host_paths import HostPaths

Runner = Callable[[Sequence[str], float], subprocess.CompletedProcess]

# smartctl's exit status is a bit mask (smartctl(8), "RETURN VALUES").
_EXIT_CANNOT_PARSE = 1
_EXIT_OPEN_FAILED = 2  # also: skipped because the disk is in standby (-n standby)

#: ATA attributes reported individually (id -> contract field).
_ATTRIBUTES = {
    5: "reallocatedSectors",
    197: "pendingSectors",
    198: "offlineUncorrectable",
    199: "udmaCrcErrors",
}


def _run(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed executable, list args
        list(args), capture_output=True, text=True, timeout=timeout, check=False
    )


class SmartCollector:
    """Read-only SMART queries: ``smartctl -a``, never a self-test, never a setting.

    - ``-n standby``: a spun-down disk is reported as such instead of being
      woken up every ten minutes just to be asked whether it is healthy.
    - USB enclosures often hide SMART behind a SCSI-to-ATA bridge that
      smartctl does not detect; when plain detection cannot open the disk,
      ``-d sat`` is tried, and whatever worked is remembered per disk.
    - SD cards and eMMC have no SMART; they are listed with
      ``smartSupported: false`` so the dashboard says so instead of "unknown".
    """

    def __init__(self, paths: HostPaths, smartctl: str = "smartctl", runner: Runner = _run) -> None:
        self._paths = paths
        self._smartctl = smartctl
        self._runner = runner
        self._device_types: Dict[str, str | None] = {}
        self._log = logging.getLogger(self.__class__.__name__)

    def collect(self) -> dict:
        executable = shutil.which(self._smartctl)
        disks: List[dict] = []
        for name in self._physical_disks():
            info = self._sysfs_info(name)
            if name.startswith("mmcblk"):
                disks.append({**info, "smartSupported": False})
            elif executable is None:
                disks.append({**info, "smartSupported": False, "error": "smartctl is not installed (apt install smartmontools)"})
            else:
                disks.append(self._query(executable, name, info))
        return {"smartctlAvailable": executable is not None, "disks": disks}

    # ------------------------------------------------------------------ smartctl

    def _query(self, executable: str, name: str, info: dict) -> dict:
        candidates = [self._device_types[name]] if name in self._device_types else [None, "sat"]
        error = "smartctl could not open the device"

        for device_type in candidates:
            args = [executable, "--json=c", "-a", "-n", "standby", f"/dev/{name}"]
            if device_type:
                args += ["-d", device_type]
            try:
                completed = self._runner(args, 60.0)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {**info, "smartSupported": False, "error": f"smartctl failed: {exc}"[:500]}

            data = self._json(completed.stdout)
            messages = self._messages(data)
            # Any severity: smartctl reports the skip as "information".
            if any("standby" in m.lower() or "sleep" in m.lower() for m in self._messages(data, every=True)):
                self._device_types[name] = device_type
                return {**info, "smartSupported": True, "inStandby": True}

            status = completed.returncode
            if status & (_EXIT_CANNOT_PARSE | _EXIT_OPEN_FAILED) and not data.get("smart_support"):
                error = (messages[0] if messages else completed.stderr.strip() or error)[:500]
                continue

            self._device_types[name] = device_type
            return self._parse(data, info, messages)

        return {**info, "smartSupported": False, "error": error}

    def _parse(self, data: Dict[str, Any], info: dict, messages: List[str]) -> dict:
        support = data.get("smart_support") or {}
        status = data.get("smart_status") or {}
        result = dict(info)
        result.update(
            {
                "model": str(data.get("model_name") or info.get("model") or "")[:128] or None,
                "serial": str(data.get("serial_number") or "")[:128] or None,
                "smartSupported": bool(support.get("available", "passed" in status)),
                "smartEnabled": support.get("enabled"),
                "smartPassed": status.get("passed"),
                "temperatureCelsius": self._bounded((data.get("temperature") or {}).get("current"), -50, 150),
                "powerOnHours": self._bounded((data.get("power_on_time") or {}).get("hours"), 0, 1e9),
                "powerCycleCount": self._bounded(data.get("power_cycle_count"), 0, 1e9),
            }
        )

        capacity = (data.get("user_capacity") or {}).get("bytes")
        if isinstance(capacity, int) and capacity > 0:
            result["capacityBytes"] = capacity
        rotation = data.get("rotation_rate")
        if isinstance(rotation, int):
            result["rotational"] = rotation > 0

        table = (data.get("ata_smart_attributes") or {}).get("table") or []
        for row in table:
            field = _ATTRIBUTES.get(row.get("id")) if isinstance(row, dict) else None
            if field:
                result[field] = self._bounded((row.get("raw") or {}).get("value"), 0, 1e12)

        nvme = data.get("nvme_smart_health_information_log") or {}
        if nvme:
            result["percentageUsed"] = self._bounded(nvme.get("percentage_used"), 0, 1000)
            result["temperatureCelsius"] = result["temperatureCelsius"] or self._bounded(nvme.get("temperature"), -50, 150)

        errors = [m for m in messages if m]
        if status.get("passed") is False:
            errors.insert(0, "SMART overall-health self-assessment: FAILED")
        result["error"] = "; ".join(errors)[:500] or None
        return result

    # ---------------------------------------------------------------- sysfs

    def _physical_disks(self) -> List[str]:
        block = self._paths.sys / "block"
        if not block.is_dir():
            return []
        return sorted(entry.name for entry in block.iterdir() if WHOLE_DISK.match(entry.name))

    def _sysfs_info(self, name: str) -> dict:
        node = self._paths.sys / "block" / name
        sectors = self._paths.read_int(node / "size")
        rotational = self._paths.read_int(node / "queue/rotational")
        model = (self._paths.read_text(node / "device/model") or self._paths.read_text(node / "device/name") or "").strip()
        try:
            real = os.path.realpath(node)
        except OSError:
            real = str(node)
        if name.startswith("mmcblk"):
            transport = "mmc"
        elif name.startswith("nvme"):
            transport = "nvme"
        elif "/usb" in real:
            transport = "usb"
        else:
            transport = "sata"
        return {
            "device": name,
            "model": model[:128] or None,
            "transport": transport,
            "capacityBytes": sectors * 512 if sectors else None,
            "rotational": None if rotational is None else rotational == 1,
            "inStandby": False,
        }

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _json(text: str) -> Dict[str, Any]:
        try:
            parsed = json.loads(text) if text.strip() else {}
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _messages(data: Dict[str, Any], every: bool = False) -> List[str]:
        """smartctl's messages: errors and warnings, or all of them."""
        messages = (data.get("smartctl") or {}).get("messages") or []
        return [
            str(m.get("string", "")).strip()
            for m in messages
            if isinstance(m, dict) and (every or m.get("severity") in ("error", "warning", None))
        ]

    @staticmethod
    def _bounded(value: Any, low: float, high: float) -> Any:
        """Vendor-specific raw values can be absurd; one must not void the report."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return value if low <= value <= high else None
