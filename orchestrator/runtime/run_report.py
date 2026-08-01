"""What one orchestration cycle did."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from orchestrator.runtime.task_result import TaskResult

EXIT_OK = 0
EXIT_FAILED = 1


@dataclass
class RunReport:
    """Collected results of one cycle, plus the exit code they imply."""

    results: List[TaskResult] = field(default_factory=list)
    planned: List[str] = field(default_factory=list)
    skipped_reason: str | None = None
    daily_snapshots: bool = False
    dry_run: bool = False
    fatal_error: BaseException | None = None

    @staticmethod
    def skipped(reason: str) -> "RunReport":
        return RunReport(skipped_reason=reason)

    def add(self, result: TaskResult) -> None:
        self.results.append(result)

    def add_fatal(self, error: BaseException) -> None:
        """Record a failure that prevented the cycle from running at all."""
        self.fatal_error = error

    # ------------------------------------------------------------ accessors

    @property
    def was_skipped(self) -> bool:
        return self.skipped_reason is not None

    @property
    def succeeded(self) -> List[TaskResult]:
        return [result for result in self.results if result.success]

    @property
    def failed(self) -> List[TaskResult]:
        return [result for result in self.results if not result.success]

    @property
    def ok(self) -> bool:
        return self.fatal_error is None and not self.failed

    @property
    def exit_code(self) -> int:
        return EXIT_OK if self.ok else EXIT_FAILED

    def summary(self) -> str:
        if self.fatal_error is not None:
            return f"Cycle aborted: {self.fatal_error}"
        if self.was_skipped:
            return f"Cycle skipped: {self.skipped_reason}"
        if self.dry_run:
            planned = ", ".join(self.planned) or "nothing"
            return f"Dry run: {len(self.planned)} task(s) would have run ({planned})"
        if not self.results:
            return "Nothing was due in this cycle"

        parts = [
            f"{len(self.succeeded)}/{len(self.results)} task(s) succeeded",
            f"daily snapshots {'on' if self.daily_snapshots else 'off'}",
        ]
        if self.failed:
            parts.append("failed: " + ", ".join(result.label for result in self.failed))
        return "; ".join(parts)

    def detailed_lines(self) -> List[str]:
        return [result.summary() for result in self.results]
