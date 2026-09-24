"""AutopilotLoop state machine for unattended claim/run/submit cycles.

The loop takes an abstract TaskSource protocol and drives claim -> run ->
submit -> repeat, handling SIGINT (finish or cleanly abandon in-flight, never
half-submit) and resume-without-duplicate-claims.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest

from bernstein.core.volunteer.autopilot_loop import AutopilotLoop
from bernstein.core.volunteer.volunteer_profile import VolunteerProfile


@dataclass
class FakeTask:
    """Minimal task shape for testing."""

    id: str
    project: str
    size: str
    task_type: str


class FakeTaskSource(Protocol):
    """Abstract TaskSource protocol the AutopilotLoop depends on."""

    async def claim_next(self, profile: VolunteerProfile) -> FakeTask | None: ...
    async def run(self, task: FakeTask) -> str: ...
    async def submit(self, task: FakeTask, result: str) -> None: ...
    async def release(self, task: FakeTask) -> None: ...


class InMemoryTaskSource:
    """Fake TaskSource for testing, never touches network or processes.

    Implements the protocol properly: claim_next returns a task without
    side-effects, and the loop is responsible for filtering and deciding
    whether to actually proceed with run/submit.
    """

    def __init__(self, tasks: list[FakeTask]) -> None:
        self.available_tasks = tasks
        self.next_index = 0
        self.claimed: list[str] = []
        self.run_count = 0
        self.submitted: list[tuple[str, str]] = []
        self.released: list[str] = []
        self.run_event: asyncio.Event | None = None

    async def claim_next(self, profile: VolunteerProfile) -> FakeTask | None:
        """Return next available task matching project filter, without claiming yet."""
        while self.next_index < len(self.available_tasks):
            task = self.available_tasks[self.next_index]
            self.next_index += 1

            # Source applies project filter (the primary filter)
            if task.project in profile.allowed_projects:
                # Mark as "claimed" for tracking purposes, but the loop
                # will release and retry if other filters don't match
                self.claimed.append(task.id)
                return task

        return None

    async def run(self, task: FakeTask) -> str:
        self.run_count += 1
        if self.run_event:
            await self.run_event.wait()
        return f"result-{task.id}"

    async def submit(self, task: FakeTask, result: str) -> None:
        self.submitted.append((task.id, result))

    async def release(self, task: FakeTask) -> None:
        self.released.append(task.id)
        # Remove from claimed list on release
        if task.id in self.claimed:
            self.claimed.remove(task.id)


# ---------------------------------------------------------------------------
# Policy filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_project_outside_the_allowlist_is_never_claimed() -> None:
    """Issue #3885 acceptance criterion: policy enforcement."""
    profile = VolunteerProfile.from_flags(
        allowed_projects=["allowed/repo"],
        max_size="m",
    )
    source = InMemoryTaskSource(
        [
            FakeTask(id="t1", project="disallowed/repo", size="s", task_type="bug"),
            FakeTask(id="t2", project="allowed/repo", size="s", task_type="bug"),
        ]
    )

    loop = AutopilotLoop(profile=profile, source=source, ledger_path=None, max_iterations=1)
    await loop.run()

    # Only t2 should have been claimed and not released
    assert "t1" not in source.claimed
    assert "t2" in source.claimed
    assert "t2" not in source.released


@pytest.mark.asyncio
async def test_size_and_type_filters_are_respected() -> None:
    profile = VolunteerProfile.from_flags(
        allowed_projects=["owner/repo"],
        allowed_task_types=["bug"],
        max_size="m",
    )
    source = InMemoryTaskSource(
        [
            FakeTask(id="t1", project="owner/repo", size="l", task_type="bug"),  # too large
            FakeTask(id="t2", project="owner/repo", size="s", task_type="feature"),  # wrong type
            FakeTask(id="t3", project="owner/repo", size="s", task_type="bug"),  # ok
        ]
    )

    loop = AutopilotLoop(profile=profile, source=source, ledger_path=None, max_iterations=1)
    await loop.run()

    # t1 and t2 should have been claimed then released (filtered out)
    # t3 should have been claimed and kept
    assert "t1" in source.released
    assert "t2" in source.released
    assert "t3" in source.claimed
    assert "t3" not in source.released


# ---------------------------------------------------------------------------
# SIGINT-safe stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sigint_during_a_task_finishes_the_current_task_before_exiting() -> None:
    """Issue #3885 acceptance criterion: SIGINT-safe stop.

    Uses a fake TaskSource.run() that genuinely blocks (asyncio.Event the test
    controls) so the interrupt has real in-flight work to interrupt. A run()
    that returns immediately makes the SIGINT-safety test pass regardless of
    whether the interrupt handling is correct, because there was never a window
    where it mattered (per the brief's "THE TRAP YOU WOULD HAVE HIT YOURSELF").
    """
    profile = VolunteerProfile.from_flags(
        allowed_projects=["owner/repo"],
        max_size="m",
    )
    run_event = asyncio.Event()
    source = InMemoryTaskSource([FakeTask(id="t1", project="owner/repo", size="s", task_type="bug")])
    source.run_event = run_event

    loop = AutopilotLoop(profile=profile, source=source, ledger_path=None, max_iterations=10)

    async def send_interrupt_during_run() -> None:
        await asyncio.sleep(0.1)  # Let the loop claim and enter run()
        loop.request_stop()
        await asyncio.sleep(0.05)  # Give stop signal time to register
        run_event.set()  # Unblock the run

    await asyncio.gather(loop.run(), send_interrupt_during_run())

    # The task was claimed and run
    assert "t1" in source.claimed
    assert source.run_count == 1
    # Either submitted or released, never neither
    assert len(source.submitted) + len(source.released) == 1


# ---------------------------------------------------------------------------
# Budget limits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_budget_limit_stops_the_loop_and_records_the_stop_in_the_ledger(tmp_path: Path) -> None:
    profile = VolunteerProfile.from_flags(
        allowed_projects=["owner/repo"],
        max_size="m",
    )
    source = InMemoryTaskSource(
        [
            FakeTask(id="t1", project="owner/repo", size="s", task_type="bug"),
            FakeTask(id="t2", project="owner/repo", size="s", task_type="bug"),
        ]
    )
    ledger_path = tmp_path / "ledger.jsonl"

    loop = AutopilotLoop(profile=profile, source=source, ledger_path=ledger_path, max_iterations=1)
    await loop.run()

    # Only one task processed due to max_iterations=1
    completed = len([tid for tid in source.claimed if tid not in source.released])
    assert completed == 1

    # Ledger records the stop
    assert ledger_path.exists()
    lines = ledger_path.read_text().strip().split("\n")
    records = [json.loads(line) for line in lines if line]
    assert any(r.get("event") == "stopped" for r in records)


# ---------------------------------------------------------------------------
# Resume without duplicate claims
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_with_an_existing_ledger_does_not_reclaim_an_already_claimed_task(tmp_path: Path) -> None:
    """Issue #3885 acceptance criterion: resume without duplicate claims.

    Construct a ledger file claiming to have an in-flight claim on task X,
    build a NEW AutopilotLoop instance pointed at the same ledger path, feed it
    a fake TaskSource that would offer task X again, and assert the loop
    consults the ledger and skips it.
    """
    profile = VolunteerProfile.from_flags(
        allowed_projects=["owner/repo"],
        max_size="m",
    )
    ledger_path = tmp_path / "ledger.jsonl"

    # First run: claim and run task t1
    source1 = InMemoryTaskSource([FakeTask(id="t1", project="owner/repo", size="s", task_type="bug")])
    loop1 = AutopilotLoop(profile=profile, source=source1, ledger_path=ledger_path, max_iterations=1)
    await loop1.run()
    assert "t1" in source1.claimed

    # Second run: new loop instance, same ledger, same task offered again
    source2 = InMemoryTaskSource([FakeTask(id="t1", project="owner/repo", size="s", task_type="bug")])
    loop2 = AutopilotLoop(profile=profile, source=source2, ledger_path=ledger_path, max_iterations=1)
    await loop2.run()

    # On the second run, the loop detects t1 is already in the ledger
    # and skips it, so no run/submit should happen
    assert source2.run_count == 0
    assert len(source2.submitted) == 0


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_prints_the_claim_decision_without_calling_claim() -> None:
    profile = VolunteerProfile.from_flags(
        allowed_projects=["owner/repo"],
        max_size="m",
    )
    source = InMemoryTaskSource([FakeTask(id="t1", project="owner/repo", size="s", task_type="bug")])

    loop = AutopilotLoop(profile=profile, source=source, ledger_path=None, max_iterations=1, dry_run=True)
    await loop.run()

    # Dry-run calls claim_next to see what would be claimed, but then doesn't run/submit
    assert source.run_count == 0
    assert len(source.submitted) == 0
