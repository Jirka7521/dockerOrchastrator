"""Long-running loop that triggers a cycle on every configured hourly tick."""

from __future__ import annotations

import logging
import signal
import threading
from datetime import datetime

from orchestrator.config.config_loader import ConfigLoader
from orchestrator.runtime.orchestrator import Orchestrator
from orchestrator.runtime.orchestrator_factory import OrchestratorFactory
from orchestrator.scheduling.clock import Clock
from orchestrator.scheduling.schedule_evaluator import ScheduleEvaluator


class OrchestratorDaemon:
    """Runs cycles forever, and keeps running when they fail.

    Two properties matter more than anything else here:

    * The loop is driven by "which tick am I in", not by "did I see minute N
      go by". A cycle that overruns its hour therefore still triggers the next
      tick as soon as it finishes instead of silently losing it.
    * Nothing that happens inside a cycle can end the loop. Only a stop request
      (signal, :meth:`request_stop`, or ``KeyboardInterrupt``) does.
    """

    def __init__(
        self,
        orchestrator: Orchestrator,
        config_loader: ConfigLoader | None = None,
        factory: OrchestratorFactory | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.config_loader = config_loader
        self.factory = factory
        self.stop_event = threading.Event()
        self.last_slot_key: str | None = None
        self.consecutive_failures = 0
        self.log = logging.getLogger(self.__class__.__name__)

        self._clock = orchestrator.config.schedule.create_clock()
        self._evaluator = orchestrator.evaluator

    # -------------------------------------------------------------- lifecycle

    def request_stop(self) -> None:
        """Signal-safe stop request; the loop exits after the current cycle."""
        self.stop_event.set()

    def install_signal_handlers(self) -> None:
        """Ask the OS to route termination signals into :meth:`request_stop`."""

        def handle(signum: int, _frame: object) -> None:
            self.log.info("Received signal %s; shutting down after this cycle.", signum)
            self.request_stop()

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            handler_signal = getattr(signal, name, None)
            if handler_signal is None:
                continue
            try:
                signal.signal(handler_signal, handle)
            except (ValueError, OSError) as exc:
                # Not the main thread, or the signal is unavailable here.
                self.log.debug("Could not install handler for %s: %s", name, exc)

    # ------------------------------------------------------------------- loop

    def run_forever(self) -> int:
        schedule = self.orchestrator.config.schedule
        self.log.info(
            "Daemon started: minute %d of every hour, timezone %s. Next tick at %s.",
            schedule.hourly_minute,
            self._clock.timezone_name,
            self._evaluator.next_slot(self._clock.now()).isoformat(timespec="minutes"),
        )
        self._prime_startup_slot()

        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - the loop outlives every failure
                self.log.exception("Unexpected daemon error; continuing.")
            self._sleep_until_next_check()

        self.log.info("Daemon stopped gracefully.")
        return 0

    def _tick(self) -> None:
        self._reload_config_if_changed()

        now = self._clock.now()
        slot = self._evaluator.current_slot(now)
        slot_key = ScheduleEvaluator.slot_key(slot)
        if slot_key == self.last_slot_key:
            return

        self.log.info("Tick %s is due (now %s).", slot_key, now.isoformat(timespec="seconds"))
        # Claim the slot before running: if the cycle raises something truly
        # unexpected we must not spin on it for the rest of the hour.
        self.last_slot_key = slot_key
        self._run_cycle(now)

    def _run_cycle(self, now: datetime) -> None:
        try:
            report = self.orchestrator.run(now=now, ignore_hourly_tick=True)
        except Exception:  # noqa: BLE001 - Orchestrator.run should not raise
            self.consecutive_failures += 1
            self.log.exception("Cycle crashed (%d in a row).", self.consecutive_failures)
            return

        if report.ok:
            if self.consecutive_failures:
                self.log.info("Cycle recovered after %d failure(s).", self.consecutive_failures)
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
            self.log.warning(
                "Cycle reported problems (%d in a row): %s",
                self.consecutive_failures,
                report.summary(),
            )

    # --------------------------------------------------------------- helpers

    def _prime_startup_slot(self) -> None:
        """Decide whether the tick that already passed should still run.

        Starting the service a couple of minutes after the hour usually means a
        restart or a deployment, and the operator expects the missed tick to be
        picked up. Starting it half an hour later does not.
        """
        now = self._clock.now()
        grace = self.orchestrator.config.runtime.startup_catch_up_minutes
        minutes_late = self._evaluator.minutes_since_slot(now)

        if minutes_late <= grace:
            self.log.info(
                "Startup is %.1f min after tick %s (grace %d min); it will run now.",
                minutes_late,
                ScheduleEvaluator.slot_key(self._evaluator.current_slot(now)),
                grace,
            )
            return

        self.last_slot_key = ScheduleEvaluator.slot_key(self._evaluator.current_slot(now))
        self.log.info(
            "Tick %s is %.1f min old; waiting for the next one at %s.",
            self.last_slot_key,
            minutes_late,
            self._evaluator.next_slot(now).isoformat(timespec="minutes"),
        )

    def _reload_config_if_changed(self) -> None:
        if self.config_loader is None or self.factory is None:
            return
        if not self.orchestrator.config.runtime.reload_config_on_change:
            return

        config = self.config_loader.reload_if_changed()
        if config is None:
            return

        self.orchestrator = self.factory.create(config)
        self._clock = config.schedule.create_clock()
        self._evaluator = self.orchestrator.evaluator
        self.log.info("Configuration reloaded:\n%s", config.describe())

    def _sleep_until_next_check(self) -> None:
        """Wait a bounded amount of time, waking early on a stop request."""
        poll = self.orchestrator.config.runtime.poll_interval_seconds
        remaining = self._evaluator.seconds_until_next_slot(self._clock.now())
        # Polling (rather than sleeping straight to the next tick) keeps the
        # daemon correct across suspend/resume and clock adjustments.
        self.stop_event.wait(max(0.1, min(poll, remaining or poll)))

    @property
    def clock(self) -> Clock:
        return self._clock
