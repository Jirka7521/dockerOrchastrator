"""Reads one container's log through `docker logs`."""

from __future__ import annotations

import logging
import re
from typing import List, Tuple

from orchestrator.docker_cli.docker_command import DockerCommand
from orchestrator.errors import DockerError

_TIMESTAMPED = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(?P<frac>\d+))?(?P<zone>Z|[+-]\d{2}:\d{2}) (?P<text>.*)$")

#: The API's limit for one line; longer lines are cut and flagged.
MAX_LINE_CHARS = 16000


class LogFetcher:
    """``docker logs --timestamps`` with both streams kept apart, then merged.

    Docker writes a container's stdout and stderr to separate pipes; reading
    them separately and merging by timestamp keeps the order *and* tells the
    dashboard which lines are errors. The result is capped by line count and
    by size, from the end, so the newest lines are always the ones kept.
    """

    def __init__(self, command: DockerCommand, max_lines: int, max_bytes: int, timeout_seconds: float = 20.0) -> None:
        self._command = command
        self._max_lines = max_lines
        self._max_bytes = max_bytes
        self._timeout = timeout_seconds
        self._log = logging.getLogger(self.__class__.__name__)

    def fetch(self, container: str, tail: int, since: str | None) -> dict:
        args = ["logs", "--timestamps", "--tail", str(tail)]
        if since:
            args += ["--since", since]
        args.append(container)

        try:
            result = self._command.run(args, timeout_seconds=self._timeout, retries=0)
        except DockerError as exc:
            return {"ok": False, "error": str(exc)[:500], "truncated": False, "lines": []}
        if not result.ok:
            return {"ok": False, "error": (result.err or f"exit code {result.returncode}")[:500], "truncated": False, "lines": []}

        entries = self.parse(result.stdout, "stdout") + self.parse(result.stderr, "stderr")
        # Stable sort: lines with equal timestamps keep their stream order.
        entries.sort(key=lambda entry: entry[0])
        lines, truncated = self._cap([line for _, line in entries])
        return {"ok": True, "error": None, "truncated": truncated, "lines": lines}

    @staticmethod
    def parse(text: str, stream: str) -> List[Tuple[str, dict]]:
        """(sort key, line) pairs. The sort key is the timestamp padded to
        nanoseconds, so string order is time order."""
        entries: List[Tuple[str, dict]] = []
        for raw in text.splitlines():
            match = _TIMESTAMPED.match(raw)
            if match:
                fraction = match.group("frac") or ""
                key = f"{match.group('ts')}.{fraction.ljust(9, '0')}"
                timestamp = f"{match.group('ts')}{'.' + fraction[:6] if fraction else ''}{match.group('zone')}"
                body = match.group("text")
            else:
                # Without a timestamp (should not happen with --timestamps),
                # keep the line next to its predecessor.
                key = entries[-1][0] if entries else ""
                timestamp = None
                body = raw
            body = body.rstrip("\r")
            line = {"timestamp": timestamp, "stream": stream, "text": body[:MAX_LINE_CHARS]}
            if len(body) > MAX_LINE_CHARS:
                line["text"] += " [...]"
            entries.append((key, line))
        return entries

    def _cap(self, lines: List[dict]) -> Tuple[List[dict], bool]:
        truncated = False
        if len(lines) > self._max_lines:
            lines = lines[-self._max_lines :]
            truncated = True

        total = 0
        kept: List[dict] = []
        for line in reversed(lines):
            total += len(line["text"]) + 64  # the JSON around each line
            if total > self._max_bytes:
                truncated = True
                break
            kept.append(line)
        kept.reverse()
        return kept, truncated
