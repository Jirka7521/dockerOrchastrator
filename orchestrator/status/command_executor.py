"""Validates and runs the commands the API hands out -- of which there is one."""

from __future__ import annotations

import logging
import re
from typing import Callable, Mapping, Set

from orchestrator.status.collectors.log_fetcher import LogFetcher

CONTAINER_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
COMMAND_ID = re.compile(r"^[0-9a-f]{32}$")
_SINCE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$")


class CommandExecutor:
    """The trust boundary between the API and this root process.

    Everything in a command is treated as hostile input, signature or not:
    the type must be ``logs``; the container must be one this orchestrator
    itself listed in its last snapshot *and* look like a container name; the
    tail is clamped; ``since`` must be an RFC 3339 timestamp. The values then
    reach `docker logs` as separate arguments, never through a shell. There is
    no other command, and no way to add one from the API side.
    """

    def __init__(
        self,
        fetcher: LogFetcher,
        known_containers: Callable[[], Set[str]],
        max_lines: int,
        refresh_containers: Callable[[], Set[str]] | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._known = known_containers
        self._max_lines = max_lines
        self._refresh = refresh_containers
        self._log = logging.getLogger(self.__class__.__name__)

    def execute(self, command: Mapping[str, object]) -> dict:
        if command.get("type") != "logs":
            return self._refused(f"unsupported command type {str(command.get('type'))[:32]!r}")

        container = command.get("container")
        if not isinstance(container, str) or not CONTAINER_NAME.match(container):
            return self._refused("invalid container name")
        if container not in self._known():
            # Not in the last snapshot: it may be newer than the snapshot, or
            # this process may have just started. Ask Docker once before refusing.
            if self._refresh is None or container not in self._refresh():
                return self._refused(f"container {container!r} is not in the current container list")

        try:
            tail = int(command.get("tail") or 500)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return self._refused("invalid tail")
        tail = max(1, min(tail, self._max_lines))

        since = command.get("since")
        if since is not None and (not isinstance(since, str) or not _SINCE.match(since)):
            return self._refused("invalid since")

        self._log.debug("Reading %d log lines of %s", tail, container)
        return self._fetcher.fetch(container, tail, since)  # type: ignore[arg-type]

    @staticmethod
    def _refused(reason: str) -> dict:
        return {"ok": False, "error": f"refused: {reason}", "truncated": False, "lines": []}
