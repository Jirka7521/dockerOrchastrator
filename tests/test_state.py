"""Persistent state behaviour, including the ugly cases."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from orchestrator.state.daily_run_state import DailyRunState
from orchestrator.state.run_state_store import RunStateStore


class RunStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.path = Path(self._temp.name) / "state.json"
        self.addCleanup(self._temp.cleanup)

    def test_missing_file_reads_as_empty(self) -> None:
        self.assertEqual(RunStateStore(self.path).read(), {})

    def test_round_trip_and_merge(self) -> None:
        store = RunStateStore(self.path)

        self.assertTrue(store.set("a", 1))
        self.assertTrue(store.set("b", 2))

        self.assertEqual(store.read(), {"a": 1, "b": 2})

    def test_creates_missing_directories(self) -> None:
        nested = Path(self._temp.name) / "deep" / "deeper" / "state.json"

        self.assertTrue(RunStateStore(nested).set("a", 1))
        self.assertTrue(nested.exists())

    def test_corrupt_file_is_quarantined_not_fatal(self) -> None:
        self.path.write_text("{{{ not json", encoding="utf-8")
        store = RunStateStore(self.path)

        self.assertEqual(store.read(), {})
        self.assertTrue(self.path.with_name("state.json.corrupt").exists())

    def test_no_temp_file_is_left_behind(self) -> None:
        store = RunStateStore(self.path)
        store.set("a", 1)

        self.assertFalse(self.path.with_name("state.json.tmp").exists())


class DailyRunStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.path = Path(self._temp.name) / "state.json"
        self.addCleanup(self._temp.cleanup)
        self.state = DailyRunState(self.path)

    def test_no_state_means_never_run(self) -> None:
        self.assertIsNone(self.state.last_daily_date())

    def test_records_and_reads_back_a_day(self) -> None:
        self.state.record_daily_date(date(2026, 3, 15))

        self.assertEqual(self.state.last_daily_date(), date(2026, 3, 15))
        self.assertTrue(self.state.has_run_on(date(2026, 3, 15)))

    def test_unreadable_date_is_treated_as_never_run(self) -> None:
        RunStateStore(self.path).set("last_daily_date", "not-a-date")

        self.assertIsNone(self.state.last_daily_date())

    def test_other_keys_survive_an_update(self) -> None:
        store = RunStateStore(self.path)
        store.set("written_by_a_future_version", True)

        self.state.record_daily_date(date(2026, 3, 15))

        self.assertTrue(store.read()["written_by_a_future_version"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
