"""The vocabulary of scheduling periods.

Lives in :mod:`orchestrator.scheduling` rather than next to the config classes
so that dependencies point one way only: ``config`` may use ``scheduling``,
never the other way round.
"""

from __future__ import annotations

import re
from typing import List

_EVERY_N_HOURS = re.compile(r"every_(\d+)_hours?")


class Period:
    """Parses and describes the ``period`` field of a scheduled container."""

    HOURLY = "hourly"
    DAILY = "daily"

    #: Intervals that tile a day evenly. Anything else would make the last
    #: window of the day short and fire twice around midnight.
    VALID_INTERVALS: List[int] = [1, 2, 3, 4, 6, 8, 12, 24]

    @staticmethod
    def normalize(period: str) -> str:
        return period.strip().lower()

    @staticmethod
    def is_named(period: str) -> bool:
        return period in {Period.HOURLY, Period.DAILY}

    @staticmethod
    def parse_interval_hours(period: str) -> int | None:
        """Hours between runs, or ``None`` for ``hourly``/``daily``.

        Raises :class:`ValueError` with a human-readable reason for anything
        this vocabulary does not contain.
        """
        if Period.is_named(period):
            return None

        match = _EVERY_N_HOURS.fullmatch(period)
        if not match:
            raise ValueError(
                f"must be 'hourly', 'daily' or 'every_N_hours' (got {period!r})"
            )

        interval_hours = int(match.group(1))
        if interval_hours not in Period.VALID_INTERVALS:
            raise ValueError(
                f"every_N_hours must divide 24 exactly (got {interval_hours}; use "
                + ", ".join(str(value) for value in Period.VALID_INTERVALS)
                + ")"
            )
        return interval_hours
