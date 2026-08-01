"""Timezone-aware time source."""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from orchestrator.errors import TimezoneError


class Clock:
    """Produces timezone-aware "now" values for one configured timezone.

    Injecting a clock (instead of calling :func:`datetime.now` inline) keeps
    every scheduling decision testable with a fixed time.
    """

    UTC = "UTC"

    def __init__(self, timezone_name: str = UTC) -> None:
        self.timezone_name = timezone_name
        self._tzinfo = self.resolve(timezone_name)

    @staticmethod
    def resolve(timezone_name: str) -> tzinfo:
        """Resolve an IANA timezone name, with an actionable error message.

        Windows ships no IANA database, so ``ZoneInfo("Europe/Prague")`` fails
        there unless the ``tzdata`` package is installed. Without this branch
        that surfaces as an opaque ``ZoneInfoNotFoundError`` at startup.
        """
        if not isinstance(timezone_name, str) or not timezone_name.strip():
            raise TimezoneError("schedule.timezone must be a non-empty string")

        name = timezone_name.strip()
        if name.upper() == Clock.UTC:
            return dt_timezone.utc

        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError as exc:
            raise TimezoneError(
                f"Timezone '{name}' was not found on this machine. "
                "Windows has no system IANA timezone database: install it with "
                "`pip install tzdata`, or set schedule.timezone to 'UTC'."
            ) from exc
        except (ValueError, OSError) as exc:
            raise TimezoneError(f"Timezone '{name}' is not a valid IANA name: {exc}") from exc

    @property
    def tzinfo(self) -> tzinfo:
        return self._tzinfo

    def now(self) -> datetime:
        return datetime.now(self._tzinfo)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Clock(timezone_name={self.timezone_name!r})"
