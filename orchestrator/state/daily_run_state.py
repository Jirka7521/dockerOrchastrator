"""Remembers which calendar day already had its daily snapshot."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from orchestrator.state.run_state_store import RunStateStore

KEY_LAST_DAILY_DATE = "last_daily_date"
KEY_LAST_RUN_AT = "last_run_at"


class DailyRunState:
    """Daily bookkeeping on top of a :class:`RunStateStore`.

    Persisted to disk so a service restart (or a manual ``--once`` run) does
    not trigger a second daily snapshot for a day that already had one.
    """

    def __init__(self, store: RunStateStore | Path | str) -> None:
        self.store = store if isinstance(store, RunStateStore) else RunStateStore(store)
        self._log = logging.getLogger(self.__class__.__name__)

    @property
    def path(self) -> Path:
        return self.store.path

    def last_daily_date(self) -> date | None:
        raw = self.store.get(KEY_LAST_DAILY_DATE)
        if not isinstance(raw, str):
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            self._log.warning(
                "State file %s holds an unreadable date (%r); treating today's daily "
                "snapshot as not yet run.",
                self.path,
                raw,
            )
            return None

    def has_run_on(self, day: date) -> bool:
        return self.last_daily_date() == day

    def record_daily_date(self, day: date) -> bool:
        saved = self.store.update({KEY_LAST_DAILY_DATE: day.isoformat()})
        if saved:
            self._log.debug("Recorded daily snapshot for %s", day.isoformat())
        else:
            self._log.warning(
                "Daily snapshot for %s could not be recorded; it may run again "
                "on the next tick.",
                day.isoformat(),
            )
        return saved

    def record_run(self, moment_iso: str) -> bool:
        """Store the last orchestration timestamp (diagnostics only)."""
        return self.store.update({KEY_LAST_RUN_AT: moment_iso})
