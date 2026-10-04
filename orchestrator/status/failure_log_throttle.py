"""Rate-limited logging for a channel that fails repeatedly."""

from __future__ import annotations

import logging
import time
from typing import Callable


class FailureLogThrottle:
    """Logs the first failure, then one line per interval, then a recovery.

    When the API is down for an hour, a reporter that sends every 15 seconds
    would otherwise write 240 identical warnings to the journal. This keeps the
    first one (with the reason), a reminder every ten minutes with a count, and
    one line when it works again.
    """

    def __init__(
        self,
        logger: logging.Logger,
        channel: str,
        interval_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._log = logger
        self._channel = channel
        self._interval = interval_seconds
        self._clock = clock
        self._failures = 0
        self._last_logged: float | None = None

    @property
    def failures(self) -> int:
        return self._failures

    def failure(self, reason: str) -> None:
        self._failures += 1
        now = self._clock()
        if self._last_logged is None:
            self._log.warning("%s failed: %s", self._channel, reason)
            self._last_logged = now
        elif now - self._last_logged >= self._interval:
            self._log.warning("%s still failing (%d attempts): %s", self._channel, self._failures, reason)
            self._last_logged = now

    def success(self) -> None:
        if self._failures:
            self._log.info("%s recovered after %d failed attempt(s)", self._channel, self._failures)
        self._failures = 0
        self._last_logged = None
