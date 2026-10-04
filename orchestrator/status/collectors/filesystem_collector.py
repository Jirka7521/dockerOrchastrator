"""How full each real filesystem is, and its ext4 error counters."""

from __future__ import annotations

import os
import re
from typing import Callable, List

from orchestrator.status.collectors.block_device_resolver import BlockDeviceResolver
from orchestrator.status.host_paths import HostPaths

#: Filesystems that hold data. Everything else in /proc/mounts -- proc, sysfs,
#: cgroups, tmpfs, overlay, squashfs snaps -- is not a disk anyone fills.
REAL_FILESYSTEMS = frozenset(
    {"ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "exfat", "f2fs", "zfs", "ntfs", "ntfs3", "fuseblk"}
)

#: Mount points the API accepts as a label (no key-structuring characters).
_MOUNT_LABEL = re.compile(r"^/[^{}=,\x00-\x1f\x7f]{0,255}$")
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


class FilesystemCollector:
    """Size, use and free space as ``df`` reports them, per mount point.

    "Available" excludes the blocks reserved for root, and "used" is
    total minus free -- the same arithmetic as df, so the dashboard and a
    terminal never disagree.
    """

    def __init__(
        self,
        paths: HostPaths,
        resolver: BlockDeviceResolver,
        statvfs: Callable[[str], os.statvfs_result] = os.statvfs,
    ) -> None:
        self._paths = paths
        self._resolver = resolver
        self._statvfs = statvfs

    def collect(self) -> List[dict]:
        text = self._paths.read_text(self._paths.proc / "self/mounts") or ""
        result: List[dict] = []
        seen_devices: set = set()
        seen_mounts: set = set()

        for line in text.splitlines():
            fields = line.split()
            if len(fields) < 3:
                continue
            device, mountpoint, fstype = (self._unescape(fields[0]), self._unescape(fields[1]), fields[2])
            if fstype not in REAL_FILESYSTEMS or not device.startswith("/dev/"):
                continue
            # The same device bind-mounted twice is one filesystem.
            if device in seen_devices or mountpoint in seen_mounts or not _MOUNT_LABEL.match(mountpoint):
                continue

            try:
                usage = self._statvfs(mountpoint)
            except OSError:
                continue
            seen_devices.add(device)
            seen_mounts.add(mountpoint)

            total = usage.f_blocks * usage.f_frsize
            free = usage.f_bfree * usage.f_frsize
            entry = {
                "mountpoint": mountpoint,
                "device": device[:256],
                "fsType": fstype,
                "disk": self._resolver.physical_disk(device),
                "totalBytes": total,
                "usedBytes": max(0, total - free),
                "availableBytes": usage.f_bavail * usage.f_frsize,
                "inodesTotal": usage.f_files or None,
                "inodesUsed": (usage.f_files - usage.f_ffree) if usage.f_files else None,
                "errorCount": None,
                "lifetimeWriteKilobytes": None,
            }

            # ext4 keeps a count of the errors it has seen, and how much it has
            # ever written -- the closest thing an SD card has to wear data.
            kernel_name = self._resolver.kernel_name(device)
            if fstype.startswith("ext") and kernel_name:
                ext4 = self._paths.sys / "fs/ext4" / kernel_name
                entry["errorCount"] = self._paths.read_int(ext4 / "errors_count")
                entry["lifetimeWriteKilobytes"] = self._paths.read_int(ext4 / "lifetime_write_kbytes")

            result.append(entry)
        return result

    @staticmethod
    def _unescape(value: str) -> str:
        """/proc/mounts escapes spaces and friends as octal (``\\040``)."""
        return _OCTAL_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)
