"""Every container on the host: state, health, exit code, times."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Dict, List

from orchestrator.docker_cli.docker_command import DockerCommand
from orchestrator.errors import DockerCommandError

_FRACTION = re.compile(r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(?P<fraction>\d+))?(?P<zone>Z|[+-]\d{2}:\d{2})$")


class ContainerCollector:
    """``docker ps`` for the list and Docker's own status line, then one
    batched ``docker inspect`` for the details.

    Two things are deliberately NOT sent:

    - the command line and environment: argument lists carry secrets in
      practice (cloudflared's ``--token``, for one), and the dashboard has no
      use for them;
    - anything beyond the last healthcheck's output, truncated.
    """

    BATCH = 50
    VERSION_REFRESH_SECONDS = 3600.0

    def __init__(self, command: DockerCommand, clock: Callable[[], float] = time.monotonic) -> None:
        self._command = command
        self._clock = clock
        self._version: str | None = None
        self._version_at: float | None = None
        self._log = logging.getLogger(self.__class__.__name__)

    def collect(self) -> dict:
        listing = self._command.run(["ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Status}}"])
        listing.raise_for_status("Could not list containers")

        statuses: Dict[str, str] = {}
        for line in listing.out.splitlines():
            container_id, _, status = line.partition("\t")
            if container_id.strip():
                statuses[container_id.strip()] = status.strip()

        ids = list(statuses)
        inspected: List[dict] = []
        for start in range(0, len(ids), self.BATCH):
            inspected.extend(self._inspect(ids[start : start + self.BATCH]))

        containers = [self.normalise(item, statuses) for item in inspected]
        containers.sort(key=lambda c: c["name"])
        return {"dockerVersion": self._docker_version(), "containers": containers}

    def _inspect(self, ids: List[str]) -> List[dict]:
        result = self._command.run(["inspect", "--type", "container", *ids])
        # A container removed between `ps` and `inspect` makes the command exit
        # non-zero while still printing the others; use what was printed.
        try:
            parsed = json.loads(result.stdout) if result.stdout.strip() else []
        except ValueError as exc:
            raise DockerCommandError(
                command=result.args, returncode=result.returncode, stderr=result.stderr,
                message=f"docker inspect printed invalid JSON: {exc}",
            ) from exc
        return [item for item in parsed if isinstance(item, dict)]

    def _docker_version(self) -> str | None:
        now = self._clock()
        if self._version_at is None or now - self._version_at > self.VERSION_REFRESH_SECONDS:
            result = self._command.run(["version", "--format", "{{.Server.Version}}"])
            self._version = result.out[:32] if result.ok and result.out else self._version
            self._version_at = now
        return self._version

    # ------------------------------------------------------------- normalise

    @staticmethod
    def normalise(item: Dict[str, Any], statuses: Dict[str, str]) -> dict:
        state = item.get("State") or {}
        health = state.get("Health") or None
        config = item.get("Config") or {}
        labels = config.get("Labels") or {}
        host_config = item.get("HostConfig") or {}
        networks = (item.get("NetworkSettings") or {}).get("Networks") or {}

        last_output = None
        if health and health.get("Log"):
            last = health["Log"][-1] or {}
            last_output = (str(last.get("Output") or "").strip())[:1000] or None

        return {
            "id": item.get("Id"),
            "name": str(item.get("Name") or "").lstrip("/"),
            "image": str(config.get("Image") or "")[:256] or None,
            "command": None,
            "createdAt": ContainerCollector.timestamp(item.get("Created")),
            "state": state.get("Status"),
            "status": statuses.get(item.get("Id") or "", "")[:128] or None,
            "health": (health or {}).get("Status") or "none",
            "healthFailingStreak": (health or {}).get("FailingStreak") if health else None,
            "healthLastOutput": last_output,
            "exitCode": state.get("ExitCode"),
            "error": str(state.get("Error") or "")[:500] or None,
            "startedAt": ContainerCollector.timestamp(state.get("StartedAt")),
            "finishedAt": ContainerCollector.timestamp(state.get("FinishedAt")),
            "restartCount": int(item.get("RestartCount") or 0),
            "oomKilled": bool(state.get("OOMKilled")),
            "composeProject": (labels.get("com.docker.compose.project") or None),
            "composeService": (labels.get("com.docker.compose.service") or None),
            # The folder `docker compose` ran in: the dashboard groups
            # containers by the disk and folders it sits in.
            "composeWorkingDir": ContainerCollector.working_dir(labels.get("com.docker.compose.project.working_dir")),
            "restartPolicy": ((host_config.get("RestartPolicy") or {}).get("Name") or None),
            "networks": [
                {"name": name[:128], "ipAddress": (settings or {}).get("IPAddress") or None}
                for name, settings in list(networks.items())[:16]
            ],
        }

    @staticmethod
    def working_dir(value: Any) -> str | None:
        """An absolute path of at most 512 characters, or None.

        A label is whatever the compose file's author wrote; a relative path,
        an over-long one or one with control characters is dropped rather
        than cut, since a shortened path would name a different folder.
        """
        if not isinstance(value, str) or not value.startswith("/") or len(value) > 512:
            return None
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            return None
        return value.rstrip("/") or "/"

    @staticmethod
    def timestamp(value: Any) -> str | None:
        """Docker's RFC 3339 with nanoseconds -> microseconds; year 1 -> None.

        Docker writes ``0001-01-01T00:00:00Z`` for "never" (a container that
        has not finished yet), which must not show up as a date.
        """
        if not isinstance(value, str) or value.startswith("0001-01-01"):
            return None
        match = _FRACTION.match(value.strip())
        if not match:
            return None
        fraction = (match.group("fraction") or "")[:6]
        return f"{match.group('base')}{'.' + fraction if fraction else ''}{match.group('zone')}"
