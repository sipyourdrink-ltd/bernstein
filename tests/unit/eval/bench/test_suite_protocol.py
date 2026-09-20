"""
Tests for BenchmarkSuite Protocol and Score type.

Issue #5444, slice m35-5444-suite-protocol-retry.

Hermetic tests — no network, MockReplayAdapter only.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from bernstein.eval.bench.suite import BenchmarkSuite, BenchTask, Score

if TYPE_CHECKING:
    from collections.abc import Generator


# ---------------------------------------------------------------------------
# Test implementation of the protocol
# ---------------------------------------------------------------------------


class MinimalTestSuite:
    """A minimal implementation of BenchmarkSuite for testing."""

    def load(self, sample: int, seed: int) -> list[BenchTask]:
        return [
            BenchTask(
                id=f"task_{seed}_{i}",
                description=f"Test task {i}",
                steps=(f"step {i}",),
                assertions=({"kind": "exists"},),
                category="test",
            )
            for i in range(sample)
        ]

    @contextmanager
    def sandbox(self, task: BenchTask) -> Generator[None]:
        """Minimal sandbox context manager."""
        yield

    def score(self, task: BenchTask, result: dict[str, Any]) -> Score:
        """Minimal scoring."""
        passed = result.get("verdict") == "pass"
        return Score(
            value=1.0 if passed else -0.5,
            passed=passed,
            details={"task_id": task.id},
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "suite_name": "minimal-test",
            "version": "1.0",
            "dataset_hash": "abc123",
        }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSuiteProtocolShape:
    """Verify BenchmarkSuite Protocol shape and Score dataclass."""

    def test_suite_protocol_shape(self) -> None:
        """Protocol has load, sandbox, score, metadata methods."""
        suite = MinimalTestSuite()

        # load returns list of BenchTask
        tasks = suite.load(sample=2, seed=42)
        assert len(tasks) == 2
        assert all(isinstance(t, BenchTask) for t in tasks)
        assert tasks[0].id == "task_42_0"

        # sandbox is a context manager
        task = tasks[0]
        with suite.sandbox(task):
            pass  # Context manager works

        # score returns Score
        result: dict[str, Any] = {"verdict": "pass"}
        score = suite.score(task, result)
        assert isinstance(score, Score)
        assert score.value == 1.0
        assert score.passed is True

        # metadata returns dict
        meta = suite.metadata()
        assert isinstance(meta, dict)  # type: ignore[arg-type]
        assert "suite_name" in meta
        assert "version" in meta
        assert "dataset_hash" in meta

    def test_score_dataclass_fields(self) -> None:
        """Score has value, passed, details fields."""
        score = Score(value=1.0, passed=True, details={"key": "val"})
        assert score.value == 1.0
        assert score.passed is True
        assert score.details == {"key": "val"}

    def test_score_negative_value_for_failure(self) -> None:
        """Score can have negative value for wrong answer."""
        score = Score(value=-0.5, passed=False, details={})
        assert score.value == -0.5
        assert score.passed is False

    def test_protocol_runtime_checkable(self) -> None:
        """BenchmarkSuite is a runtime_checkable Protocol."""
        suite = MinimalTestSuite()
        assert isinstance(suite, BenchmarkSuite)
