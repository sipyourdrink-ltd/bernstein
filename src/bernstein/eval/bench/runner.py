"""
bernstein-bench: ``bernstein bench run <suite>``

Executes every task in a :class:`BenchSuite`, collects per-task run receipts
from the replay journal + lineage spine, scores them via ``harness.py``
multiplicative scoring, and emits a signed :class:`SubmissionBundle`.

The real adapter / journal path is injected via the ``ReplayAdapter``
protocol so tests can pass a hermetic mock without network or real adapters.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult

if TYPE_CHECKING:
    from bernstein.eval.bench.suite import BenchSuite, BenchTask

# ---------------------------------------------------------------------------
# Protocol: replay adapter
# ---------------------------------------------------------------------------


class ReplayAdapter(Protocol):
    """
    Thin boundary the runner calls to execute one task and fetch its receipt.

    The production implementation wraps ``scenario_runner.py`` +
    ``journal.py``; tests inject a ``MockReplayAdapter``.
    """

    def run_task(self, task: BenchTask, scheduler_config: dict[str, Any]) -> dict[str, Any]:
        """
        Execute *task* and return the raw run receipt::

            {
                "journal_head": "<sha256>",
                "spine_head":   "<sha256>",
                "run_id":       "<uuid>",
                "events":       [...],   # replay-sufficient event log
            }
        """
        ...

    def score_task(self, task: BenchTask, receipt: dict[str, Any]) -> tuple[bool, float, dict[str, Any]]:
        """
        Re-run harness.py multiplicative scoring against *receipt*.

        Returns ``(passed, score, harness_output)``.
        """
        ...


# ---------------------------------------------------------------------------
# Mock adapter (hermetic, no network, no real adapters — used in tests)
# ---------------------------------------------------------------------------


class MockReplayAdapter:
    """
    Deterministic stub: every task passes with score 1.0.

    The receipt is derived from the task content hash so two calls with the
    same task produce byte-identical receipts (empirical determinism property
    the acceptance test verifies).
    """

    def run_task(self, task: BenchTask, scheduler_config: dict[str, Any]) -> dict[str, Any]:
        task_hash = task.content_hash()
        # Deterministic synthetic journal / spine heads.
        journal_head = hashlib.sha256(f"journal:{task_hash}".encode()).hexdigest()
        spine_head = hashlib.sha256(f"spine:{task_hash}".encode()).hexdigest()
        return {
            "journal_head": journal_head,
            "spine_head": spine_head,
            "run_id": f"mock-{task_hash[:12]}",
            "events": [
                {"seq": 0, "kind": "task.started", "task_hash": task_hash},
                {"seq": 1, "kind": "task.completed", "task_hash": task_hash},
            ],
        }

    def score_task(self, task: BenchTask, receipt: dict[str, Any]) -> tuple[bool, float, dict[str, Any]]:
        return True, 1.0, {"note": "mock: all assertions satisfied"}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class BenchRunner:
    """
    Runs a :class:`BenchSuite` end-to-end and produces a
    :class:`SubmissionBundle`.
    """

    suite: BenchSuite
    adapter: ReplayAdapter
    scheduler_config: dict[str, Any]

    def run(self) -> SubmissionBundle:
        """Execute every task; return the unsigned bundle."""
        task_results: list[TaskResult] = []

        for task in self.suite.tasks:
            receipt = self.adapter.run_task(task, self.scheduler_config)
            passed, score, harness_output = self.adapter.score_task(task, receipt)

            task_results.append(
                TaskResult(
                    task_id=task.id,
                    task_hash=task.content_hash(),
                    receipt=receipt,
                    passed=passed,
                    score=score,
                    harness_output=harness_output,
                )
            )

        return SubmissionBundle(
            suite_hash=self.suite.suite_hash,
            suite_version=self.suite.version,
            task_results=task_results,
            scheduler_config=self.scheduler_config,
            submitted_at=time.time(),
        )
