"""Raspberry Pi under-voltage and throttling flags."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess

from orchestrator.status.host_paths import HostPaths

_THROTTLED = re.compile(r"throttled=(0x[0-9a-fA-F]+)")


class ThrottleCollector:
    """The firmware's throttle bitmask, or ``None`` on hardware without one.

    ``vcgencmd get_throttled`` is preferred: besides the current state it
    reports the sticky "has occurred since boot" bits, which catch an
    under-voltage dip that happened between two samples. The sysfs node is the
    fallback for systems without the userland tool.
    """

    def __init__(self, paths: HostPaths, vcgencmd: str = "vcgencmd") -> None:
        self._paths = paths
        self._vcgencmd = vcgencmd
        self._log = logging.getLogger(self.__class__.__name__)

    def collect(self) -> int | None:
        executable = shutil.which(self._vcgencmd)
        if executable is not None:
            try:
                completed = subprocess.run(  # noqa: S603 - fixed executable, list args
                    [executable, "get_throttled"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                match = _THROTTLED.search(completed.stdout)
                if completed.returncode == 0 and match:
                    return int(match.group(1), 16)
            except (OSError, subprocess.TimeoutExpired) as exc:
                self._log.debug("vcgencmd get_throttled failed: %s", exc)

        value = self._paths.read_text(self._paths.sys / "devices/platform/soc/soc:firmware/get_throttled")
        if value is None:
            return None
        try:
            return int(value.strip(), 16)
        except ValueError:
            return None
