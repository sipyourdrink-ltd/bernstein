"""AutopilotLoop state machine for unattended claim/run/submit cycles.

The loop takes an abstract TaskSource protocol and drives claim -> run ->
submit -> repeat, handling SIGINT (finish or cleanly abandon in-flight, never
half-submit) and resume-without-duplicate-claims.

Once #3869 (runner), #3871 (verification), and #3872 (submission) exist, a real
TaskSource implementation plugs in with zero changes to the loop itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.volunteer.volunteer_profile import VolunteerProfile

logger = logging.getLogger(__name__)


class TaskSource(Protocol):
    """Abstract interface for task claim/run/submit operations.

    This protocol defines the shape AutopilotLoop depends on. Any real
    implementation MUST call ``build_volunteer_profile`` from
    ``bernstein.core.volunteer.sandbox_profile`` with no caller-supplied
    override to skip it, enforcing the "hardened sandbox profile is mandatory
    in autopilot mode, no flag to disable it" constraint from issue #3885's
    security section.
    """

    async def claim_next(self, profile: VolunteerProfile) -> Any | None:
        """Claim the next task matching the donor's policy.

        Args:
            profile: Donor policy to filter by.

        Returns:
            A task object, or None if nothing matches.
        """
        ...

    async def run(self, task: Any) -> str:
        """Execute the task in the hardened sandbox.

        Must call ``build_volunteer_profile`` internally with no override to
        disable the hardened sandbox.

        Args:
            task: The task to run.

        Returns:
            Result summary.
        """
        ...

    async def submit(self, task: Any, result: str) -> None:
        """Submit the completed task result.

        Args:
            task: The completed task.
            result: The result summary from run().
        """
        ...

    async def release(self, task: Any) -> None:
        """Release a claimed task without submitting.

        Args:
            task: The task to release.
        """
        ...


class AutopilotLoop:
    """Unattended claim/run/submit loop with SIGINT-safe stop and resume.

    The loop claims tasks matching the donor's profile, runs them in the
    hardened sandbox, submits results, and repeats until stopped by budget,
    signal, or iteration limit.

    Attributes:
        profile: Donor policy.
        source: TaskSource implementation providing claim/run/submit operations.
        ledger_path: Path to append ledger records to, or None for no ledger.
        max_iterations: Stop after this many tasks (for testing/budget limits).
        dry_run: Print what would be claimed without actually claiming.
    """

    def __init__(
        self,
        *,
        profile: VolunteerProfile,
        source: TaskSource,
        ledger_path: Path | None = None,
        max_iterations: int | None = None,
        dry_run: bool = False,
    ) -> None:
        self.profile = profile
        self.source = source
        self.ledger_path = ledger_path
        self.max_iterations = max_iterations
        self.dry_run = dry_run
        self._stop_requested = asyncio.Event()
        self._claimed_task_ids: set[str] = set()
        self._iterations = 0

        if ledger_path:
            self._load_ledger()

    def _load_ledger(self) -> None:
        """Load already-claimed task IDs from the ledger to avoid duplicates."""
        if not self.ledger_path or not self.ledger_path.exists():
            return

        try:
            for line in self.ledger_path.read_text().strip().split("\n"):
                if not line:
                    continue
                record = json.loads(line)
                if record.get("event") == "claimed":
                    self._claimed_task_ids.add(record["task_id"])
        except Exception as exc:
            logger.warning("Failed to load ledger from %s: %s", self.ledger_path, exc)

    def _append_ledger(self, record: dict[str, Any]) -> None:
        """Append a record to the ledger."""
        if not self.ledger_path:
            return

        try:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as exc:
            logger.warning("Failed to append to ledger %s: %s", self.ledger_path, exc)

    def request_stop(self) -> None:
        """Request the loop to stop after the current task completes.

        This is the signal handler's entry point. The loop finishes or cleanly
        abandons the in-flight task before actually exiting, never
        half-submitting.
        """
        self._stop_requested.set()

    async def run(self) -> None:
        """Run the autopilot loop until stopped.

        The loop:
        1. Checks for stop signal or budget exhaustion
        2. Claims next task matching profile (skipping already-claimed)
        3. Runs the task
        4. Submits or releases based on result
        5. Records to ledger
        6. Repeats

        On SIGINT, the current task finishes its submit-or-release before the
        loop exits.
        """
        self._append_ledger({"event": "started", "profile_version": self.profile.version})

        while not self._stop_requested.is_set():
            if self.max_iterations is not None and self._iterations >= self.max_iterations:
                logger.info("Budget limit reached: %d iterations", self.max_iterations)
                self._append_ledger({"event": "stopped", "reason": "budget_limit"})
                break

            # Claim next task
            task = await self._claim_next_matching()
            if task is None:
                logger.info("No more tasks matching profile")
                self._append_ledger({"event": "stopped", "reason": "no_tasks"})
                break

            task_id = self._get_task_id(task)

            if self.dry_run:
                logger.info("DRY-RUN: would claim task %s", task_id)
                self._append_ledger({"event": "dry_run_claim", "task_id": task_id})
                break

            self._claimed_task_ids.add(task_id)
            self._append_ledger({"event": "claimed", "task_id": task_id})
            self._iterations += 1

            # Run and submit/release
            try:
                result = await self.source.run(task)
                await self.source.submit(task, result)
                self._append_ledger({"event": "submitted", "task_id": task_id})
            except Exception as exc:
                logger.exception("Task %s failed: %s", task_id, exc)
                await self.source.release(task)
                self._append_ledger({"event": "released", "task_id": task_id, "reason": str(exc)})

            # Check for stop request after completing the task
            if self._stop_requested.is_set():
                logger.info("Stop requested, exiting after completing task %s", task_id)
                self._append_ledger({"event": "stopped", "reason": "signal"})
                break

        self._append_ledger({"event": "finished"})

    async def _claim_next_matching(self) -> Any | None:
        """Claim next task matching profile, filtering by policy and already-claimed.

        Returns:
            Task object or None.
        """
        # Keep trying until we find a matching task or run out
        while True:
            # Call source to get a candidate (source applies project filter)
            task = await self.source.claim_next(self.profile)

            if task is None:
                return None

            task_id = self._get_task_id(task)

            # Resume safety: skip already-claimed tasks
            if task_id in self._claimed_task_ids:
                logger.info("Task %s already claimed in this session, skipping", task_id)
                await self.source.release(task)
                continue

            # Additional policy filters beyond what source applied
            if not self._matches_profile(task):
                logger.info("Task %s does not match profile, skipping", task_id)
                await self.source.release(task)
                continue

            return task

    def _matches_profile(self, task: Any) -> bool:
        """Check if task matches the donor's profile.

        Args:
            task: Task to check.

        Returns:
            True if task matches profile policy.
        """
        # Task type filter
        if (
            self.profile.allowed_task_types
            and hasattr(task, "task_type")
            and task.task_type not in self.profile.allowed_task_types
        ):
            return False

        # Size filter
        if hasattr(task, "size"):
            size_order = {"xs": 0, "s": 1, "m": 2, "l": 3, "xl": 4}
            max_size = size_order.get(self.profile.max_size, 2)
            task_size = size_order.get(task.size, 2)
            if task_size > max_size:
                return False

        return True

    def _get_task_id(self, task: Any) -> str:
        """Extract task ID from task object.

        Args:
            task: Task object.

        Returns:
            Task ID string.
        """
        if hasattr(task, "id"):
            return task.id
        if hasattr(task, "task_id"):
            return task.task_id
        return str(task)
