"""Validates and runs the commands the API hands out."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable, Collection, Mapping, Set

from orchestrator.status.collectors.log_fetcher import LogFetcher
from orchestrator.status.command_types import ALL, CONTAINER_ACTIONS, DEFAULT_ALLOWED, LOGS, POWER_ACTIONS
from orchestrator.status.container_control import ContainerControl
from orchestrator.status.power_control import PowerControl

CONTAINER_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
COMMAND_ID = re.compile(r"^[0-9a-f]{32}$")
_SINCE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$")


@dataclass(frozen=True)
class Outcome:
    """A command's result, and what may only happen once the API has taken it."""

    result: dict
    #: A reboot or power-off: run by the caller only after the API accepted
    #: the result, so it never happens for an answer nobody received.
    after_delivery: Callable[[], object] | None = None


class CommandExecutor:
    """The trust boundary between the API and this root process.

    Everything in a command is treated as hostile input, signature or not:
    the type must be one this host's config allows (``logs`` alone by
    default); a container must be one this orchestrator itself listed in its
    last snapshot *and* look like a container name; the tail is clamped;
    ``since`` must be an RFC 3339 timestamp. The values then reach Docker as
    separate arguments, never through a shell. Nothing on the API side can
    widen what is allowed: that is the config file's decision, on this host.
    """

    def __init__(
        self,
        fetcher: LogFetcher,
        known_containers: Callable[[], Set[str]],
        max_lines: int,
        refresh_containers: Callable[[], Set[str]] | None = None,
        allowed: Collection[str] = DEFAULT_ALLOWED,
        containers: ContainerControl | None = None,
        power: PowerControl | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._known = known_containers
        self._max_lines = max_lines
        self._refresh = refresh_containers
        self._allowed = frozenset(allowed)
        self._containers = containers
        self._power = power
        self._log = logging.getLogger(self.__class__.__name__)

    def execute(self, command: Mapping[str, object]) -> Outcome:
        kind = command.get("type")
        if not isinstance(kind, str) or kind not in ALL:
            return self._refused(f"unsupported command type {str(kind)[:32]!r}")
        if kind not in self._allowed:
            return self._refused(f"{kind!r} is not in status_reporter.allowed_commands")

        if kind in POWER_ACTIONS:
            return self._power_action(kind)

        container = command.get("container")
        if not isinstance(container, str) or not CONTAINER_NAME.match(container):
            return self._refused("invalid container name")
        if container not in self._known():
            # Not in the last snapshot: it may be newer than the snapshot, or
            # this process may have just started. Ask Docker once before refusing.
            if self._refresh is None or container not in self._refresh():
                return self._refused(f"container {container!r} is not in the current container list")

        if kind in CONTAINER_ACTIONS:
            if self._containers is None:
                return self._refused("container actions are not set up")
            return Outcome(self._containers.run(kind, container))

        assert kind == LOGS
        return self._logs(container, command)

    def _logs(self, container: str, command: Mapping[str, object]) -> Outcome:
        try:
            tail = int(command.get("tail") or 500)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return self._refused("invalid tail")
        tail = max(1, min(tail, self._max_lines))

        since = command.get("since")
        if since is not None and (not isinstance(since, str) or not _SINCE.match(since)):
            return self._refused("invalid since")

        self._log.debug("Reading %d log lines of %s", tail, container)
        return Outcome(self._fetcher.fetch(container, tail, since))  # type: ignore[arg-type]

    def _power_action(self, kind: str) -> Outcome:
        if self._power is None:
            return self._refused("power control is not set up")
        problem = self._power.check()
        if problem is not None:
            return self._refused(problem)

        power = self._power
        self._log.warning("Dashboard request: %s the host, once the API has the answer", kind)
        return Outcome(
            {"ok": True, "error": None, "truncated": False, "lines": []},
            after_delivery=lambda: power.schedule(kind),
        )

    def _refused(self, reason: str) -> Outcome:
        self._log.warning("Refused a command from the API: %s", reason)
        return Outcome({"ok": False, "error": f"refused: {reason}", "truncated": False, "lines": []})
