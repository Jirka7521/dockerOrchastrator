"""Builders for fake /proc and /sys trees used by the status tests."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Mapping

from orchestrator.status.host_paths import HostPaths

# The same vectors the API's C# tests assert (SignatureVectors), so the two
# signature implementations cannot drift apart unnoticed.
KEY = bytes(range(32))
NONCE = "00112233445566778899aabbccddeeff"
TIMESTAMP = 1_790_000_000
REQUEST_BODY = b'{"collectedAt":"2026-10-03T12:00:00Z"}'
REQUEST_SIGNATURE = "v1=gdDi6aQps4y3guaqrzC3ZVypvkftG0p7rAlWvBl86nA="
RESPONSE_BODY = b'{"commands":[]}'
RESPONSE_SIGNATURE = "v1=nj8lFhNDweDCEnzDDMr4Q3dHOp0e+0UUW4Vbbs9M2sI="


class FakeHost:
    """A temporary directory laid out like / with proc/ and sys/ inside."""

    def __init__(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="status-host-")
        self.root = Path(self._dir.name)
        self.paths = HostPaths(proc=self.root / "proc", sys=self.root / "sys")

    def cleanup(self) -> None:
        self._dir.cleanup()

    def write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def write_many(self, files: Mapping[str, str]) -> None:
        for relative, content in files.items():
            self.write(relative, content)

    def symlink(self, relative: str, target: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, path)

    def mkdir(self, relative: str) -> Path:
        path = self.root / relative
        path.mkdir(parents=True, exist_ok=True)
        return path


class ManualClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
