"""Which Docker network each host interface carries."""

from __future__ import annotations

import logging
import time
from typing import Callable, Dict, List

from orchestrator.docker_cli.docker_command import DockerCommand
from orchestrator.errors import DockerError


class DockerNetworkMapper:
    """Interface name -> Docker network names, cached for a few minutes.

    A macvlan network names its interface in the ``parent`` option
    (``eth0.130``). A bridge network uses ``com.docker.network.bridge.name``
    when set (``docker0`` for the default bridge) and ``br-<12 hex of id>``
    otherwise. Networks change rarely, so this is refreshed every five minutes
    rather than on every report.
    """

    CACHE_SECONDS = 300.0

    #: One line per network: id, name, driver, macvlan parent, bridge name.
    _FORMAT = (
        '{{.Id}}\t{{.Name}}\t{{.Driver}}\t{{index .Options "parent"}}'
        '\t{{index .Options "com.docker.network.bridge.name"}}'
    )

    def __init__(self, command: DockerCommand, clock: Callable[[], float] = time.monotonic) -> None:
        self._command = command
        self._clock = clock
        self._cache: Dict[str, List[str]] = {}
        self._fetched_at: float | None = None
        self._log = logging.getLogger(self.__class__.__name__)

    def interfaces(self) -> Dict[str, List[str]]:
        now = self._clock()
        if self._fetched_at is not None and now - self._fetched_at < self.CACHE_SECONDS:
            return self._cache

        try:
            names = self._command.run(["network", "ls", "-q"])
            ids = names.out.split()
            if not names.ok or not ids:
                return self._cache
            result = self._command.run(["network", "inspect", "--format", self._FORMAT, *ids])
        except DockerError as exc:
            self._log.debug("Could not list Docker networks: %s", exc)
            return self._cache
        if not result.ok:
            return self._cache

        mapping: Dict[str, List[str]] = {}
        for line in result.out.splitlines():
            fields = (line.split("\t") + [""] * 5)[:5]
            network_id, name, driver, parent, bridge = (field.strip() for field in fields)
            if driver in ("macvlan", "ipvlan") and parent and parent != "<no value>":
                interface = parent
            elif driver == "bridge":
                interface = bridge if bridge and bridge != "<no value>" else f"br-{network_id[:12]}"
            else:
                continue
            mapping.setdefault(interface, []).append(name)

        self._cache = mapping
        self._fetched_at = now
        return mapping
