"""Reboots or powers off the host, as asked from the dashboard."""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from typing import Callable, Sequence

from orchestrator.status.command_types import POWER_ACTIONS

#: Between the API taking the answer and the act: time for the dashboard to
#: show what is about to happen before the network goes.
DELAY_SECONDS = 3.0


class PowerControl:
    """``systemctl reboot|poweroff``, run only after the API has the answer.

    The order is the point. The answer ("accepted, about to happen") goes out
    first, since nothing can be sent once the host is going down; the caller
    schedules the act only if the API took that answer, meaning a dashboard
    request was still waiting for it. An answer that arrives too late is
    refused by the API, and then nothing happens.

    Under systemd, ``systemctl reboot`` stops every service in order, this one
    included: a scheduled job that is running is finished first, within the
    unit's ``TimeoutStopSec``.
    """

    def __init__(
        self,
        executable: str = "systemctl",
        runner: Callable[[Sequence[str]], subprocess.CompletedProcess] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        delay_seconds: float = DELAY_SECONDS,
    ) -> None:
        self._executable = executable
        self._runner = runner or _run
        self._sleep = sleeper
        self._delay = delay_seconds
        self._log = logging.getLogger(self.__class__.__name__)

    def check(self) -> str | None:
        """Why the act could not happen, or None. Asked before saying "accepted"."""
        if shutil.which(self._executable) is None:
            return f"{self._executable!r} was not found"
        return None

    def schedule(self, action: str) -> threading.Thread:
        """Run the act after the delay, on a thread of its own."""
        if action not in POWER_ACTIONS:
            raise ValueError(f"not a power action: {action!r}")
        thread = threading.Thread(target=self._act, args=(action,), name=f"status-{action}", daemon=True)
        thread.start()
        return thread

    def _act(self, action: str) -> None:
        self._sleep(self._delay)
        self._log.warning("Dashboard request: running systemctl %s", action)
        try:
            completed = self._runner([self._executable, action])
        except (OSError, subprocess.SubprocessError) as exc:
            self._log.error("systemctl %s did not run: %s", action, exc)
            return
        if completed.returncode != 0:
            self._log.error(
                "systemctl %s failed (exit %d): %s",
                action,
                completed.returncode,
                (completed.stderr or "").strip()[:500],
            )


def _run(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed executable, list args
        list(args), capture_output=True, text=True, timeout=60, check=False
    )
