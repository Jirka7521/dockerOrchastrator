"""Maps a mounted device to the physical disk underneath it."""

from __future__ import annotations

import os
import re
from pathlib import Path

from orchestrator.status.host_paths import HostPaths

#: Whole disks worth reporting: SATA/USB, SD/eMMC, NVMe, virtio.
WHOLE_DISK = re.compile(r"^(sd[a-z]+|hd[a-z]+|vd[a-z]+|xvd[a-z]+|mmcblk\d+|nvme\d+n\d+)$")


class BlockDeviceResolver:
    """Follows partitions and device-mapper layers down to a physical disk.

    ``/dev/mapper/encrypted_SSD`` is ``dm-0``, whose single slave is ``sda``;
    ``/dev/mmcblk0p2`` is a partition of ``mmcblk0``. Following those links in
    /sys is what lets the dashboard put "/mnt/externalSSD0 is 1 % full" and
    "sda's SMART health" on the same card.
    """

    def __init__(self, paths: HostPaths, dev_root: Path = Path("/")) -> None:
        self._paths = paths
        self._dev_root = dev_root

    def kernel_name(self, device: str) -> str | None:
        """``/dev/mapper/x`` -> ``dm-0``; ``/dev/sda1`` -> ``sda1``."""
        if not device.startswith("/dev/"):
            return None
        local = self._dev_root / device.lstrip("/")
        try:
            return os.path.basename(os.path.realpath(local))
        except OSError:
            return None

    def physical_disk(self, device: str) -> str | None:
        name = self.kernel_name(device)
        return self._disk_of(name, depth=0) if name else None

    def _disk_of(self, name: str, depth: int) -> str | None:
        if depth > 8:  # a loop in /sys would be a kernel bug; do not follow it forever
            return None

        node = self._paths.sys / "class/block" / name
        if not node.exists():
            return None

        # A partition: its parent directory in /sys/devices is the disk.
        if (node / "partition").exists():
            parent = Path(os.path.realpath(node)).parent.name
            return self._disk_of(parent, depth + 1)

        # device-mapper (LUKS, LVM) or md RAID: follow the first slave.
        slaves = node / "slaves"
        if slaves.is_dir():
            members = sorted(entry.name for entry in slaves.iterdir())
            if members:
                return self._disk_of(members[0], depth + 1)

        return name if WHOLE_DISK.match(name) else None
