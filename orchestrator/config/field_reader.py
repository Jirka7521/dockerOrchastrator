"""Typed, path-aware reads out of raw JSON dictionaries."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Sequence

from orchestrator.errors import ConfigError

_UNSET = object()


class FieldReader:
    """Reads one JSON object and reports problems with a full key path.

    Every error message names the exact location in the config file
    (``pipelines.personal.sync``) instead of a bare Python type error, because
    a config mistake at 03:00 should be readable straight out of the log.
    """

    def __init__(self, data: Any, path: str = "") -> None:
        if not isinstance(data, Mapping):
            raise ConfigError(f"{self._describe(path)} must be a JSON object")
        self._data: Mapping[str, Any] = data
        self._path = path
        self._log = logging.getLogger(self.__class__.__name__)

    @staticmethod
    def _describe(path: str) -> str:
        return path or "config root"

    def _key_path(self, key: str) -> str:
        return f"{self._path}.{key}" if self._path else key

    def raw(self) -> Mapping[str, Any]:
        return self._data

    def has(self, key: str) -> bool:
        return key in self._data

    def _value(self, key: str, default: Any) -> Any:
        value = self._data.get(key, _UNSET)
        if value is _UNSET or value is None:
            if default is _UNSET:
                raise ConfigError(f"{self._key_path(key)} is required")
            return default
        return value

    def text(self, key: str, default: Any = _UNSET) -> str:
        value = self._value(key, default)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{self._key_path(key)} must be a non-empty string")
        return value.strip()

    def optional_text(self, key: str) -> str | None:
        """A string that may be absent or explicitly ``null``."""
        if key not in self._data or self._data[key] is None:
            return None
        return self.text(key)

    def integer(
        self,
        key: str,
        default: Any = _UNSET,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        value = self._value(key, default)
        # bool is an int subclass; a boolean here is virtually always a mistake.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{self._key_path(key)} must be an integer")
        if minimum is not None and value < minimum:
            raise ConfigError(f"{self._key_path(key)} must be >= {minimum}")
        if maximum is not None and value > maximum:
            raise ConfigError(f"{self._key_path(key)} must be <= {maximum}")
        return value

    def number(
        self,
        key: str,
        default: Any = _UNSET,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        value = self._value(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{self._key_path(key)} must be a number")
        value = float(value)
        if minimum is not None and value < minimum:
            raise ConfigError(f"{self._key_path(key)} must be >= {minimum}")
        if maximum is not None and value > maximum:
            raise ConfigError(f"{self._key_path(key)} must be <= {maximum}")
        return value

    def optional_number(
        self,
        key: str,
        minimum: float | None = None,
    ) -> float | None:
        """A number that may be explicitly ``null`` to mean "no limit"."""
        if key not in self._data or self._data[key] is None:
            return None
        return self.number(key, minimum=minimum)

    def boolean(self, key: str, default: Any = _UNSET) -> bool:
        value = self._value(key, default)
        if not isinstance(value, bool):
            raise ConfigError(f"{self._key_path(key)} must be true or false")
        return value

    def mapping(self, key: str, default: Any = _UNSET) -> Dict[str, Any]:
        value = self._value(key, default)
        if not isinstance(value, Mapping):
            raise ConfigError(f"{self._key_path(key)} must be a JSON object")
        return dict(value)

    def sequence(self, key: str, default: Any = _UNSET) -> List[Any]:
        value = self._value(key, default)
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise ConfigError(f"{self._key_path(key)} must be an array")
        return list(value)

    def child(self, key: str, default: Any = _UNSET) -> "FieldReader":
        return FieldReader(self.mapping(key, default), self._key_path(key))

    def warn_unknown_keys(self, known: Sequence[str]) -> None:
        """Log (never fail on) keys we do not understand.

        A typo like ``hourly_minutes`` is worth flagging, but refusing to start
        because of one unrecognised key would be a worse failure mode than
        ignoring it.
        """
        unknown = sorted(set(self._data) - set(known))
        if unknown:
            self._log.warning(
                "Ignoring unknown key(s) in %s: %s",
                self._describe(self._path),
                ", ".join(unknown),
            )
