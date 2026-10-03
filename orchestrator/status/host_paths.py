"""Where the host's kernel interfaces live."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HostPaths:
    """Roots of /proc and /sys, injectable so collectors can be tested
    against fixture directories instead of the machine running the tests."""

    proc: Path = Path("/proc")
    sys: Path = Path("/sys")

    def read_text(self, path: Path) -> str | None:
        """Contents of a kernel file, or ``None`` when it is absent or unreadable.

        Many of these files exist only on some hardware or for some devices
        (a speed for a down link raises EINVAL), so absence is normal, not an
        error.
        """
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def read_int(self, path: Path) -> int | None:
        text = self.read_text(path)
        if text is None:
            return None
        try:
            return int(text.strip(), 0)
        except ValueError:
            return None
