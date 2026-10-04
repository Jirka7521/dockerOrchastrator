"""Collectors against fake /proc and /sys trees."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

from orchestrator.status.collectors.block_device_resolver import BlockDeviceResolver
from orchestrator.status.collectors.connectivity_collector import ConnectivityCollector
from orchestrator.status.collectors.container_collector import ContainerCollector
from orchestrator.status.collectors.cpu_collector import CpuCollector
from orchestrator.status.collectors.disk_io_collector import DiskIoCollector
from orchestrator.status.collectors.docker_network_mapper import DockerNetworkMapper
from orchestrator.status.collectors.filesystem_collector import FilesystemCollector
from orchestrator.status.collectors.memory_collector import MemoryCollector
from orchestrator.status.collectors.network_collector import NetworkCollector
from orchestrator.status.collectors.thermal_collector import ThermalCollector
from tests.fakes import FakeDockerCommand
from tests.status_support import FakeHost, ManualClock

NET_DEV_HEADER = (
    "Inter-|   Receive                                                |  Transmit\n"
    " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
)


def net_dev(rows: dict) -> str:
    lines = [
        f"{name}: {rx} 10 1 2 0 0 0 0 {tx} 20 3 4 0 0 0 0"
        for name, (rx, tx) in rows.items()
    ]
    return NET_DEV_HEADER + "\n".join(lines) + "\n"


class CollectorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeHost()
        self.addCleanup(self.host.cleanup)


class CpuAndMemoryTests(CollectorTestCase):
    def test_cpu_usage_comes_from_the_delta_between_two_readings(self) -> None:
        cpu = CpuCollector(self.host.paths)
        self.host.write_many(
            {
                "proc/stat": "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 50 0 50 400 0 0 0 0\ncpu1 50 0 50 400 0 0 0 0\n",
                "proc/loadavg": "0.70 1.00 1.11 2/700 12345\n",
                "sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq": "1500000\n",
                "sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq": "1800000\n",
            }
        )
        first = cpu.collect()
        # 300 more busy jiffies of 1000 in total: 30 %. iowait counts as idle.
        self.host.write("proc/stat", "cpu  250 0 250 1400 100 0 0 0 0 0\ncpu0 100 0 100 700 100 0 0 0\ncpu1 150 0 150 700 0 0 0 0\n")
        second = cpu.collect()

        self.assertIsNone(first["usagePercent"])
        self.assertEqual([], first["perCorePercent"])
        self.assertAlmostEqual(30.0, second["usagePercent"])
        # cpu0: (200 - 100) / (1000 - 500); cpu1: (300 - 100) / (1000 - 500).
        self.assertEqual([20.0, 40.0], second["perCorePercent"])
        self.assertEqual((0.7, 1.0, 1.11), (second["load1"], second["load5"], second["load15"]))
        self.assertEqual(1500.0, second["frequencyMhz"])
        self.assertEqual(2, second["coreCount"])

    def test_memory_used_is_total_minus_available(self) -> None:
        self.host.write(
            "proc/meminfo",
            "MemTotal: 8000 kB\nMemFree: 500 kB\nMemAvailable: 6000 kB\nBuffers: 100 kB\nCached: 3000 kB\n"
            "SReclaimable: 200 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n",
        )

        memory = MemoryCollector(self.host.paths).collect()

        self.assertEqual(8000 * 1024, memory["totalBytes"])
        self.assertEqual(2000 * 1024, memory["usedBytes"])
        self.assertEqual(3200 * 1024, memory["cachedBytes"])
        self.assertEqual(0, memory["swapUsedBytes"])


class ThermalTests(CollectorTestCase):
    def test_the_pi_soc_sensor_is_reported_once(self) -> None:
        self.host.write_many(
            {
                "sys/class/thermal/thermal_zone0/type": "cpu-thermal\n",
                "sys/class/thermal/thermal_zone0/temp": "76445\n",
                "sys/class/hwmon/hwmon0/name": "cpu_thermal\n",
                "sys/class/hwmon/hwmon0/temp1_input": "74984\n",
                "sys/class/hwmon/hwmon1/name": "nvme\n",
                "sys/class/hwmon/hwmon1/temp1_input": "41800\n",
                "sys/class/hwmon/hwmon1/temp1_label": "Composite\n",
            }
        )

        readings = ThermalCollector(self.host.paths).collect()

        self.assertEqual(
            [
                {"source": "cpu-thermal", "label": "SoC", "celsius": 76.4},
                {"source": "nvme-temp1", "label": "Composite", "celsius": 41.8},
            ],
            readings,
        )


class StorageTests(CollectorTestCase):
    def _block(self, name: str, parent: str | None = None, slaves: tuple = ()) -> None:
        """/sys/class/block/<name> -> ../../devices/.../<parent>/<name>, like the kernel."""
        device_dir = f"sys/devices/platform/{parent}/{name}" if parent else f"sys/devices/platform/{name}"
        self.host.mkdir(device_dir)
        if parent:
            self.host.write(f"{device_dir}/partition", "1\n")
        for slave in slaves:
            self.host.mkdir(f"{device_dir}/slaves/{slave}")
        self.host.symlink(f"sys/class/block/{name}", str(self.host.root / device_dir))

    def _dev(self, name: str, target: str | None = None) -> None:
        """/dev/<name>, optionally a symlink (as /dev/mapper/* are)."""
        if target is None:
            self.host.write(f"dev/{name}", "")
        else:
            self.host.symlink(f"dev/{name}", str(self.host.root / "dev" / target))

    def test_mounts_are_resolved_through_partitions_and_luks_to_disks(self) -> None:
        self._block("sda")
        self._block("dm-0", slaves=("sda",))
        self._block("mmcblk0")
        self._block("mmcblk0p2", parent="mmcblk0")
        self._dev("dm-0")
        self._dev("mapper/encrypted_SSD", target="dm-0")
        self._dev("mmcblk0p2")

        resolver = BlockDeviceResolver(self.host.paths, dev_root=self.host.root)

        self.assertEqual("sda", resolver.physical_disk("/dev/mapper/encrypted_SSD"))
        self.assertEqual("mmcblk0", resolver.physical_disk("/dev/mmcblk0p2"))
        self.assertEqual("dm-0", resolver.kernel_name("/dev/mapper/encrypted_SSD"))
        self.assertIsNone(resolver.physical_disk("tmpfs"))

    def test_only_real_filesystems_are_reported_with_df_arithmetic(self) -> None:
        self._block("mmcblk0")
        self._block("mmcblk0p2", parent="mmcblk0")
        self._dev("mmcblk0p2")
        self.host.write(
            "proc/self/mounts",
            "/dev/mmcblk0p2 / ext4 rw,relatime 0 0\n"
            "tmpfs /run tmpfs rw 0 0\n"
            "/dev/loop0 /snap/core squashfs ro 0 0\n"
            "/dev/mmcblk0p2 /var/lib/bind ext4 rw 0 0\n"
            "/dev/sdz1 /mnt/with\\040space ext4 rw 0 0\n",
        )
        self.host.write("sys/fs/ext4/mmcblk0p2/errors_count", "0\n")
        self.host.write("sys/fs/ext4/mmcblk0p2/lifetime_write_kbytes", "447256769\n")

        def statvfs(path: str) -> SimpleNamespace:
            return SimpleNamespace(f_blocks=1000, f_frsize=4096, f_bfree=700, f_bavail=600, f_files=100, f_ffree=40)

        resolver = BlockDeviceResolver(self.host.paths, dev_root=self.host.root)
        filesystems = FilesystemCollector(self.host.paths, resolver, statvfs=statvfs).collect()

        mounts = [fs["mountpoint"] for fs in filesystems]
        self.assertEqual(["/", "/mnt/with space"], mounts)  # one entry per device; no tmpfs, no snaps
        root = filesystems[0]
        self.assertEqual("mmcblk0", root["disk"])
        self.assertEqual(4096 * 1000, root["totalBytes"])
        self.assertEqual(4096 * 300, root["usedBytes"])
        self.assertEqual(4096 * 600, root["availableBytes"])
        self.assertEqual(0, root["errorCount"])
        self.assertEqual(447256769, root["lifetimeWriteKilobytes"])

    def test_disk_io_rates_and_utilisation_come_from_deltas(self) -> None:
        clock = ManualClock()
        collector = DiskIoCollector(self.host.paths, clock=clock)
        line = "   8       0 {name} 100 0 {read} 0 200 0 {written} 0 0 {busy} 0 0 0 0 0\n"
        self.host.write("proc/diskstats", line.format(name="sda", read=1000, written=2000, busy=100)
                        + line.format(name="sda1", read=1, written=1, busy=1)
                        + line.format(name="loop0", read=1, written=1, busy=1))
        self.assertEqual([], collector.collect())

        clock.advance(2.0)
        self.host.write("proc/diskstats", line.format(name="sda", read=3000, written=2000, busy=600))
        io = collector.collect()

        self.assertEqual(1, len(io))  # partitions and loop devices are not disks
        self.assertEqual("sda", io[0]["device"])
        self.assertEqual(2000 * 512 / 2, io[0]["readBytesPerSecond"])
        self.assertEqual(0.0, io[0]["writeBytesPerSecond"])
        self.assertEqual(25.0, io[0]["utilizationPercent"])  # 500 ms busy in 2 s


class NetworkTests(CollectorTestCase):
    def _interfaces(self) -> None:
        self.host.write_many(
            {
                "sys/class/net/eth0/uevent": "INTERFACE=eth0\n",
                "sys/class/net/eth0/speed": "1000\n",
                "sys/class/net/eth0/operstate": "up\n",
                "sys/class/net/eth0.130/uevent": "DEVTYPE=vlan\nINTERFACE=eth0.130\n",
                "sys/class/net/eth0.130/operstate": "up\n",
                "sys/class/net/docker0/uevent": "DEVTYPE=bridge\n",
                "sys/class/net/lo/uevent": "INTERFACE=lo\n",
            }
        )
        self.host.mkdir("sys/class/net/eth0/device")

    def test_interfaces_are_classified_and_rated(self) -> None:
        self._interfaces()
        clock = ManualClock()
        collector = NetworkCollector(self.host.paths, clock=clock)
        self.host.write("proc/net/dev", net_dev({"lo": (0, 0), "eth0": (1000, 500), "eth0.130": (100, 50), "docker0": (0, 0)}))
        first = {nic["name"]: nic for nic in collector.collect({"eth0.130": ["privateStatus"]})}

        clock.advance(10)
        self.host.write("proc/net/dev", net_dev({"lo": (0, 0), "eth0": (11000, 1500), "eth0.130": (600, 50), "docker0": (0, 0)}))
        second = {nic["name"]: nic for nic in collector.collect({"eth0.130": ["privateStatus"]})}

        self.assertIsNone(first["eth0"]["rxBytesPerSecond"])
        self.assertEqual(("physical", None, None), (second["eth0"]["kind"], second["eth0"]["parent"], second["eth0"]["vlanId"]))
        self.assertEqual(("vlan", "eth0", 130), (second["eth0.130"]["kind"], second["eth0.130"]["parent"], second["eth0.130"]["vlanId"]))
        self.assertEqual("bridge", second["docker0"]["kind"])
        self.assertEqual("loopback", second["lo"]["kind"])
        self.assertEqual(["privateStatus"], second["eth0.130"]["dockerNetworks"])
        self.assertEqual(1000.0, second["eth0"]["rxBytesPerSecond"])
        self.assertEqual(100.0, second["eth0"]["txBytesPerSecond"])
        self.assertEqual(50.0, second["eth0.130"]["rxBytesPerSecond"])
        self.assertEqual(1000, second["eth0"]["speedMbps"])

    def test_a_counter_reset_yields_no_rate(self) -> None:
        self._interfaces()
        clock = ManualClock()
        collector = NetworkCollector(self.host.paths, clock=clock)
        self.host.write("proc/net/dev", net_dev({"eth0": (5000, 5000)}))
        collector.collect()
        clock.advance(10)
        self.host.write("proc/net/dev", net_dev({"eth0": (10, 10)}))

        (eth0,) = collector.collect()

        self.assertIsNone(eth0["rxBytesPerSecond"])

    def test_docker_networks_are_mapped_to_their_interfaces(self) -> None:
        inspect_output = "\n".join(
            [
                "aaaaaaaaaaaaaaaa\tprivateStatus\tmacvlan\teth0.130\t<no value>",
                "bbbbbbbbbbbbbbbb\tbridge\tbridge\t<no value>\tdocker0",
                "01561e72f6c0ffff\ttempEdge\tbridge\t<no value>\t<no value>",
                "cccccccccccccccc\thost\thost\t<no value>\t<no value>",
            ]
        )
        command = FakeDockerCommand({"network ls": (0, "a b c d", ""), "network inspect": (0, inspect_output, "")})

        mapping = DockerNetworkMapper(command).interfaces()

        self.assertEqual(
            {"eth0.130": ["privateStatus"], "docker0": ["bridge"], "br-01561e72f6c0": ["tempEdge"]},
            mapping,
        )


class ConnectivityTests(unittest.TestCase):
    def test_a_ping_summary_is_parsed(self) -> None:
        output = (
            "3 packets transmitted, 2 received, 33.3333% packet loss, time 402ms\n"
            "rtt min/avg/max/mdev = 40.037/41.248/42.460/1.211 ms\n"
        )

        parsed = ConnectivityCollector.parse(output)

        self.assertEqual(
            {"ok": True, "sent": 3, "received": 2, "lossPercent": 33.3, "rttMinMs": 40.037, "rttAvgMs": 41.248, "rttMaxMs": 42.46},
            parsed,
        )

    def test_total_loss_is_not_ok_and_has_no_round_trip(self) -> None:
        parsed = ConnectivityCollector.parse("3 packets transmitted, 0 received, 100% packet loss, time 2042ms\n")

        self.assertEqual({"ok": False, "sent": 3, "received": 0, "lossPercent": 100.0}, parsed)

    def test_dns_and_ping_failures_are_reported_not_raised(self) -> None:
        def failing_runner(args, timeout):
            raise OSError("no such binary")

        def failing_resolver(*args):
            raise OSError("Name or service not known")

        report = ConnectivityCollector(["1.1.1.1"], 3, "example.invalid", runner=failing_runner, resolver=failing_resolver).collect()

        target = report["targets"][0]
        self.assertFalse(target["ok"])
        self.assertIn("error", target)
        self.assertFalse(report["dns"]["ok"])
        self.assertIn("Name or service not known", report["dns"]["error"])


class ContainerTests(unittest.TestCase):
    def test_inspect_output_is_normalised_without_command_lines(self) -> None:
        item = {
            "Id": "f" * 64,
            "Name": "/cloudflared",
            "Created": "2026-07-28T15:07:14.368199123Z",
            "Path": "cloudflared",
            "Args": ["tunnel", "run", "--token", "SECRET-TOKEN"],
            "State": {
                "Status": "exited",
                "ExitCode": 137,
                "Error": "",
                "OOMKilled": False,
                "StartedAt": "2026-09-28T09:18:25.276673891Z",
                "FinishedAt": "2026-09-29T10:00:00Z",
                "Health": {"Status": "unhealthy", "FailingStreak": 3, "Log": [{"Output": "  wget: connection refused  "}]},
            },
            "RestartCount": 2,
            "Config": {"Image": "cloudflare/cloudflared:latest", "Labels": {"com.docker.compose.project": "cloudflare"}},
            "HostConfig": {"RestartPolicy": {"Name": "always"}},
            "NetworkSettings": {"Networks": {"claudflareTunnel": {"IPAddress": "192.168.122.50"}}},
        }

        container = ContainerCollector.normalise(item, {"f" * 64: "Exited (137) 4 days ago"})

        self.assertNotIn("SECRET-TOKEN", repr(container))
        self.assertIsNone(container["command"])
        self.assertEqual("cloudflared", container["name"])
        self.assertEqual(("exited", 137, "unhealthy"), (container["state"], container["exitCode"], container["health"]))
        self.assertEqual("wget: connection refused", container["healthLastOutput"])
        self.assertEqual("2026-09-28T09:18:25.276673Z", container["startedAt"])
        self.assertEqual("cloudflare", container["composeProject"])
        self.assertEqual([{"name": "claudflareTunnel", "ipAddress": "192.168.122.50"}], container["networks"])
        self.assertEqual("Exited (137) 4 days ago", container["status"])

    def test_dockers_zero_time_means_never(self) -> None:
        self.assertIsNone(ContainerCollector.timestamp("0001-01-01T00:00:00Z"))
        self.assertIsNone(ContainerCollector.timestamp(None))
        self.assertEqual("2026-01-02T03:04:05+02:00", ContainerCollector.timestamp("2026-01-02T03:04:05+02:00"))


if __name__ == "__main__":
    unittest.main()
