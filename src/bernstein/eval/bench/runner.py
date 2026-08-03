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
# Stochastic mock adapter (hermetic, seed-parameterised — used in
# reliability tests to model model-sampling variance under fixed
# coordination)
# ---------------------------------------------------------------------------


class StochasticMockReplayAdapter:
    """
    Seed-parameterised mock whose *model-output* payload varies per call
    while every coordination field stays byte-identical across calls.

    This is the hermetic stand-in for the one genuinely stochastic element
    of a fixed-coordination run: model sampling.  Each ``run_task`` call
    for the same task emits the same event schedule (same seqs, same
    kinds, same coordination payloads) but a different ``model.output``
    sample, derived deterministically from ``(seed, task_hash,
    call_index)`` so tests are reproducible.

    The verdict is a pure function of the sample embedded in the receipt
    (:meth:`sample_passes`), so ``score_task`` re-derives it from the
    receipt alone — exactly what the offline verifier needs.
    """

    def __init__(self, seed: int = 0) -> None:
        self._seed = seed
        self._call_counts: dict[str, int] = {}

    @staticmethod
    def derive_sample(seed: int, task_hash: str, attempt_index: int) -> int:
        """Deterministic 64-bit sample for ``(seed, task, attempt)``."""
        digest = hashlib.sha256(f"sample:{seed}:{task_hash}:{attempt_index}".encode()).digest()
        return int.from_bytes(digest[:8], "big")

    @staticmethod
    def sample_passes(sample: int) -> bool:
        """Verdict rule: a pure function of the sampled value."""
        return sample % 2 == 0

    def run_task(self, task: BenchTask, scheduler_config: dict[str, Any]) -> dict[str, Any]:
        task_hash = task.content_hash()
        attempt_index = self._call_counts.get(task_hash, 0)
        self._call_counts[task_hash] = attempt_index + 1
        sample = self.derive_sample(self._seed, task_hash, attempt_index)
        # journal_head commits to the events including the model output, so
        # it legitimately varies per attempt; the spine head does not.
        journal_head = hashlib.sha256(f"journal:{task_hash}:{sample}".encode()).hexdigest()
        spine_head = hashlib.sha256(f"spine:{task_hash}".encode()).hexdigest()
        return {
            "journal_head": journal_head,
            "spine_head": spine_head,
            "run_id": f"mock-{task_hash[:12]}-a{attempt_index}",
            "events": [
                {"seq": 0, "kind": "task.started", "task_hash": task_hash},
                {"seq": 1, "kind": "model.output", "sample": sample},
                {"seq": 2, "kind": "task.completed", "task_hash": task_hash},
            ],
        }

    def score_task(self, task: BenchTask, receipt: dict[str, Any]) -> tuple[bool, float, dict[str, Any]]:
        sample = next(event["sample"] for event in receipt.get("events", []) if event.get("kind") == "model.output")
        passed = self.sample_passes(sample)
        return passed, 1.0 if passed else 0.0, {"sample": sample}


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
