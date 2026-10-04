"""SmartCollector against scripted smartctl output."""

from __future__ import annotations

import json
import subprocess
import unittest
from typing import List, Sequence
from unittest import mock

from orchestrator.status.collectors.smart_collector import SmartCollector
from tests.status_support import FakeHost

IRONWOLF = {
    "smartctl": {"exit_status": 0, "messages": []},
    "model_name": "ST4000VN006-3CW104",
    "serial_number": "WW61ZY5B",
    "user_capacity": {"bytes": 4000787030016},
    "rotation_rate": 5400,
    "smart_support": {"available": True, "enabled": True},
    "smart_status": {"passed": True},
    "temperature": {"current": 34},
    "power_on_time": {"hours": 12345},
    "power_cycle_count": 87,
    "ata_smart_attributes": {
        "table": [
            {"id": 5, "raw": {"value": 0}},
            {"id": 197, "raw": {"value": 0}},
            {"id": 198, "raw": {"value": 0}},
            {"id": 199, "raw": {"value": 4}},
            {"id": 9, "raw": {"value": 12345}},
        ]
    },
}

UNKNOWN_BRIDGE = {
    "smartctl": {"exit_status": 1, "messages": [{"string": "/dev/sda: Unknown USB bridge [0x152d:0x0578]", "severity": "error"}]}
}

STANDBY = {"smartctl": {"exit_status": 2, "messages": [{"string": "Device is in STANDBY mode, exit(2)", "severity": "information"}]}}


class ScriptedSmartctl:
    def __init__(self, responses: List[tuple]) -> None:
        self.responses = list(responses)
        self.calls: List[List[str]] = []

    def __call__(self, args: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        status, payload = self.responses.pop(0)
        return subprocess.CompletedProcess(args, status, json.dumps(payload), "")


class SmartCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeHost()
        self.addCleanup(self.host.cleanup)

    def _disk(self, name: str, model: str = "", rotational: int = 1, usb: bool = True) -> None:
        self.host.write_many(
            {
                f"sys/block/{name}/size": "7814037168\n",
                f"sys/block/{name}/queue/rotational": f"{rotational}\n",
                f"sys/block/{name}/device/model": model + "\n",
            }
        )

    def _collect(self, runner: ScriptedSmartctl) -> dict:
        with mock.patch("orchestrator.status.collectors.smart_collector.shutil.which", return_value="/usr/sbin/smartctl"):
            return SmartCollector(self.host.paths, runner=runner).collect()

    def test_a_healthy_disk_is_parsed(self) -> None:
        self._disk("sdb", "006-3CW104")
        runner = ScriptedSmartctl([(0, IRONWOLF)])

        report = self._collect(runner)

        disk = report["disks"][0]
        self.assertTrue(report["smartctlAvailable"])
        self.assertEqual("ST4000VN006-3CW104", disk["model"])
        self.assertTrue(disk["smartPassed"])
        self.assertEqual(34, disk["temperatureCelsius"])
        self.assertEqual(12345, disk["powerOnHours"])
        self.assertEqual(0, disk["reallocatedSectors"])
        self.assertEqual(4, disk["udmaCrcErrors"])
        self.assertTrue(disk["rotational"])
        self.assertIsNone(disk["error"])
        self.assertEqual(["-n", "standby"], runner.calls[0][3:5])  # never wakes a sleeping disk

    def test_an_unknown_usb_bridge_falls_back_to_sat_and_remembers_it(self) -> None:
        self._disk("sda")
        runner = ScriptedSmartctl([(1, UNKNOWN_BRIDGE), (0, IRONWOLF), (0, IRONWOLF)])
        with mock.patch("orchestrator.status.collectors.smart_collector.shutil.which", return_value="/usr/sbin/smartctl"):
            collector = SmartCollector(self.host.paths, runner=runner)
            first = collector.collect()
            collector.collect()

        self.assertTrue(first["disks"][0]["smartPassed"])
        self.assertNotIn("-d", runner.calls[0])
        self.assertEqual(["-d", "sat"], runner.calls[1][-2:])
        self.assertEqual(["-d", "sat"], runner.calls[2][-2:])  # straight to sat next time

    def test_a_disk_in_standby_is_not_woken(self) -> None:
        self._disk("sdb")
        report = self._collect(ScriptedSmartctl([(2, STANDBY)]))

        self.assertTrue(report["disks"][0]["inStandby"])

    def test_a_failing_disk_says_so(self) -> None:
        self._disk("sdb")
        failing = {**IRONWOLF, "smart_status": {"passed": False}}
        report = self._collect(ScriptedSmartctl([(8, failing)]))

        disk = report["disks"][0]
        self.assertFalse(disk["smartPassed"])
        self.assertIn("FAILED", disk["error"])

    def test_sd_cards_have_no_smart_and_absurd_values_are_dropped(self) -> None:
        self.host.write("sys/block/mmcblk0/size", "244277248\n")
        self.host.write("sys/block/mmcblk0/device/name", "SD128\n")
        self._disk("sdb")
        absurd = {**IRONWOLF, "temperature": {"current": 9999}}

        report = self._collect(ScriptedSmartctl([(0, absurd)]))

        sd, hdd = report["disks"]
        self.assertEqual(("mmcblk0", False, "mmc"), (sd["device"], sd["smartSupported"], sd["transport"]))
        self.assertIsNone(hdd["temperatureCelsius"])

    def test_without_smartctl_every_disk_says_why(self) -> None:
        self._disk("sdb")
        with mock.patch("orchestrator.status.collectors.smart_collector.shutil.which", return_value=None):
            report = SmartCollector(self.host.paths, runner=ScriptedSmartctl([])).collect()

        self.assertFalse(report["smartctlAvailable"])
        self.assertIn("smartmontools", report["disks"][0]["error"])


if __name__ == "__main__":
    unittest.main()
