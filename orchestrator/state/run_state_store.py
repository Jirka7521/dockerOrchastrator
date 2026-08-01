"""A tiny, crash-safe JSON key/value file."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict


class RunStateStore:
    """Reads and writes a small JSON document, atomically and forgivingly.

    Design rules, in order of importance:

    1. A state problem must never stop orchestration. The worst consequence of
       losing this file is one redundant daily snapshot, which is far cheaper
       than a run that refuses to start.
    2. Writes are atomic (temp file + ``fsync`` + ``os.replace``), so a power
       cut cannot leave a half-written file behind.
    3. Unknown keys are preserved on update, so older and newer versions of the
       orchestrator can share a state file.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._log = logging.getLogger(self.__class__.__name__)

    # ------------------------------------------------------------------ read

    def read(self) -> Dict[str, Any]:
        try:
            text = self.path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            self._log.warning("Could not read state file %s: %s", self.path, exc)
            return {}

        try:
            data = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as exc:
            self._log.warning(
                "State file %s is corrupt (%s); starting from an empty state.",
                self.path,
                exc,
            )
            self._quarantine()
            return {}

        if not isinstance(data, dict):
            self._log.warning(
                "State file %s does not contain a JSON object; ignoring it.", self.path
            )
            self._quarantine()
            return {}
        return data

    def get(self, key: str, default: Any = None) -> Any:
        return self.read().get(key, default)

    # ----------------------------------------------------------------- write

    def update(self, values: Dict[str, Any]) -> bool:
        """Merge ``values`` into the file. Returns ``False`` if it could not be saved."""
        with self._lock:
            data = self.read()
            data.update(values)
            return self._write(data)

    def set(self, key: str, value: Any) -> bool:
        return self.update({key: value})

    def clear(self) -> bool:
        with self._lock:
            return self._write({})

    # ------------------------------------------------------------- internals

    def _write(self, data: Dict[str, Any]) -> bool:
        temp_path = self.path.with_name(f"{self.path.name}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            return True
        except OSError as exc:
            self._log.warning("Could not persist state file %s: %s", self.path, exc)
            self._remove_quietly(temp_path)
            return False

    def _quarantine(self) -> None:
        """Move an unreadable state file aside so it is not re-read every tick."""
        broken_path = self.path.with_name(f"{self.path.name}.corrupt")
        try:
            os.replace(self.path, broken_path)
            self._log.warning("Moved corrupt state file to %s", broken_path)
        except OSError as exc:  # pragma: no cover - best effort only
            self._log.debug("Could not move corrupt state file aside: %s", exc)

    def _remove_quietly(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort only
            pass
