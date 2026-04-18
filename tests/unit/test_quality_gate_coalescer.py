"""Tests for serialized gate execution in :mod:`quality_gate_coalescer`.

Regression coverage for **audit-037**: prior to the fix, a second caller
arriving during an active gate run received ``passed=True`` with an empty
``gate_results`` list — a silent bypass.  The coalescer now serializes all
callers through a FIFO queue and returns each caller its own real result.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from bernstein.core.models import Complexity, Scope, Task
from bernstein.core.quality_gate_coalescer import QualityGateCoalescer, _PendingRun, _QueuedRun
from bernstein.core.quality_gates import QualityGatesConfig, QualityGatesResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(id: str = "T-001") -> Task:
    return Task(
        id=id,
        title=f"Task {id}",
        description="Do something.",
        role="backend",
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
    )


def _pass_result(task_id: str = "T-001") -> QualityGatesResult:
    return QualityGatesResult(task_id=task_id, passed=True)


def _fail_result(task_id: str = "T-001") -> QualityGatesResult:
    return QualityGatesResult(task_id=task_id, passed=False)


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


class TestInitialState:
    def test_not_in_progress_at_start(self) -> None:
        coalescer = QualityGateCoalescer()
        assert not coalescer.in_progress

    def test_no_pending_at_start(self) -> None:
        coalescer = QualityGateCoalescer()
        assert coalescer.pending_count == 0


# ---------------------------------------------------------------------------
# Single run — no contention
# ---------------------------------------------------------------------------


class TestSingleRun:
    def test_run_executes_quality_gates(self, tmp_path: Path) -> None:
        coalescer = QualityGateCoalescer()
        task = _make_task()
        config = QualityGatesConfig(enabled=False)

        result = coalescer.run(task, tmp_path, tmp_path, config)

        assert result.passed
        assert result.task_id == task.id

    def test_in_progress_false_after_run(self, tmp_path: Path) -> None:
        coalescer = QualityGateCoalescer()
        task = _make_task()
        config = QualityGatesConfig(enabled=False)

        coalescer.run(task, tmp_path, tmp_path, config)

        assert not coalescer.in_progress

    def test_pending_zero_after_run(self, tmp_path: Path) -> None:
        coalescer = QualityGateCoalescer()
        task = _make_task()
        config = QualityGatesConfig(enabled=False)

        coalescer.run(task, tmp_path, tmp_path, config)

        assert coalescer.pending_count == 0

    def test_run_calls_quality_gates_once(self, tmp_path: Path) -> None:
        coalescer = QualityGateCoalescer()
        task = _make_task()
        config = QualityGatesConfig(enabled=False)

        with patch(
            "bernstein.core.quality.quality_gate_coalescer.run_quality_gates", return_value=_pass_result()
        ) as mock_rqg:
            coalescer.run(task, tmp_path, tmp_path, config)

        mock_rqg.assert_called_once()


# ---------------------------------------------------------------------------
# Serialization under contention (audit-037 regression)
# ---------------------------------------------------------------------------


class TestSerializedExecution:
    def test_each_queued_request_runs_its_own_gates(self, tmp_path: Path) -> None:
        """Every concurrent caller must get its OWN task's gate result — no silent bypass."""
        config = QualityGatesConfig(enabled=False)
        call_log: list[str] = []
        first_started = threading.Event()
        release_first = threading.Event()
        log_lock = threading.Lock()

        def fake_run_qg(
            task: Task, run_dir: Path, workdir: Path, cfg: QualityGatesConfig, **kw: object
        ) -> QualityGatesResult:
            with log_lock:
                call_log.append(task.id)
            if task.id == "T-first":
                first_started.set()
                release_first.wait(timeout=5)
            return _pass_result(task.id)

        coalescer = QualityGateCoalescer()
        results: dict[str, QualityGatesResult] = {}
        threads: list[threading.Thread] = []

        def invoke(task_id: str) -> None:
            results[task_id] = coalescer.run(_make_task(task_id), tmp_path, tmp_path, config)

        with patch("bernstein.core.quality.quality_gate_coalescer.run_quality_gates", side_effect=fake_run_qg):
            first_thread = threading.Thread(target=invoke, args=("T-first",), daemon=True)
            first_thread.start()
            assert first_started.wait(timeout=5)

            for i in range(3):
                t = threading.Thread(target=invoke, args=(f"T-q-{i}",), daemon=True)
                t.start()
                threads.append(t)

            # Give queued threads a moment to actually enqueue.
            time.sleep(0.05)
            assert coalescer.pending_count == 3

            release_first.set()
            first_thread.join(timeout=10)
            for t in threads:
                t.join(timeout=10)

        # Every task got its own real gate run.
        assert sorted(call_log) == ["T-first", "T-q-0", "T-q-1", "T-q-2"]
        # Every caller received a result tagged with its own task id.
        for task_id in ("T-first", "T-q-0", "T-q-1", "T-q-2"):
            assert results[task_id].task_id == task_id
            assert results[task_id].passed

    def test_queued_caller_receives_fail_result_not_silent_pass(self, tmp_path: Path) -> None:
        """If a queued caller's own gate run fails, the fail result propagates — no bypass."""
        config = QualityGatesConfig(enabled=False)
        first_started = threading.Event()
        release_first = threading.Event()

        def fake_run_qg(
            task: Task, run_dir: Path, workdir: Path, cfg: QualityGatesConfig, **kw: object
        ) -> QualityGatesResult:
            if task.id == "T-first":
                first_started.set()
                release_first.wait(timeout=5)
                return _pass_result(task.id)
            # Queued task fails its gates.
            return _fail_result(task.id)

        coalescer = QualityGateCoalescer()
        results: dict[str, QualityGatesResult] = {}

        def invoke(task_id: str) -> None:
            results[task_id] = coalescer.run(_make_task(task_id), tmp_path, tmp_path, config)

        with patch("bernstein.core.quality.quality_gate_coalescer.run_quality_gates", side_effect=fake_run_qg):
            t1 = threading.Thread(target=invoke, args=("T-first",), daemon=True)
            t1.start()
            assert first_started.wait(timeout=5)

            t2 = threading.Thread(target=invoke, args=("T-queued",), daemon=True)
            t2.start()
            # Ensure T-queued has actually enqueued before we release T-first.
            for _ in range(100):
                if coalescer.pending_count >= 1:
                    break
                time.sleep(0.01)

            release_first.set()
            t1.join(timeout=10)
            t2.join(timeout=10)

        assert results["T-first"].passed is True
        # Critical regression assertion: queued caller gets its REAL fail result,
        # not a silent passed=True.
        assert results["T-queued"].passed is False
        assert results["T-queued"].task_id == "T-queued"

    def test_fifo_order_of_execution(self, tmp_path: Path) -> None:
        """Queued callers run in the order they enqueued."""
        config = QualityGatesConfig(enabled=False)
        call_log: list[str] = []
        log_lock = threading.Lock()
        first_started = threading.Event()
        release_first = threading.Event()

        def fake_run_qg(
            task: Task, run_dir: Path, workdir: Path, cfg: QualityGatesConfig, **kw: object
        ) -> QualityGatesResult:
            with log_lock:
                call_log.append(task.id)
            if task.id == "T-first":
                first_started.set()
                release_first.wait(timeout=5)
            return _pass_result(task.id)

        coalescer = QualityGateCoalescer()

        def invoke(task_id: str) -> None:
            coalescer.run(_make_task(task_id), tmp_path, tmp_path, config)

        with patch("bernstein.core.quality.quality_gate_coalescer.run_quality_gates", side_effect=fake_run_qg):
            t0 = threading.Thread(target=invoke, args=("T-first",), daemon=True)
            t0.start()
            assert first_started.wait(timeout=5)

            ordered = [f"T-{i:02d}" for i in range(4)]
            threads: list[threading.Thread] = []
            for task_id in ordered:
                t = threading.Thread(target=invoke, args=(task_id,), daemon=True)
                t.start()
                threads.append(t)
                # Tiny spacing so enqueue order is deterministic.
                time.sleep(0.02)

            release_first.set()
            t0.join(timeout=10)
            for t in threads:
                t.join(timeout=10)

        assert call_log[0] == "T-first"
        assert call_log[1:] == ordered  # FIFO

    def test_state_clean_after_queue_drained(self, tmp_path: Path) -> None:
        """After all queued callers finish, in_progress is False and queue is empty."""
        config = QualityGatesConfig(enabled=False)
        first_started = threading.Event()
        release_first = threading.Event()

        def fake_run_qg(
            task: Task, run_dir: Path, workdir: Path, cfg: QualityGatesConfig, **kw: object
        ) -> QualityGatesResult:
            if task.id == "T-first":
                first_started.set()
                release_first.wait(timeout=5)
            return _pass_result(task.id)

        coalescer = QualityGateCoalescer()

        def invoke(task_id: str) -> None:
            coalescer.run(_make_task(task_id), tmp_path, tmp_path, config)

        with patch("bernstein.core.quality.quality_gate_coalescer.run_quality_gates", side_effect=fake_run_qg):
            t0 = threading.Thread(target=invoke, args=("T-first",), daemon=True)
            t0.start()
            assert first_started.wait(timeout=5)

            t1 = threading.Thread(target=invoke, args=("T-queued",), daemon=True)
            t1.start()
            for _ in range(100):
                if coalescer.pending_count >= 1:
                    break
                time.sleep(0.01)

            release_first.set()
            t0.join(timeout=10)
            t1.join(timeout=10)

        assert not coalescer.in_progress
        assert coalescer.pending_count == 0

    def test_queue_timeout_raises_timeout_error(self, tmp_path: Path) -> None:
        """A queued caller that waits beyond the timeout raises ``TimeoutError``."""
        config = QualityGatesConfig(enabled=False)
        first_started = threading.Event()
        release_first = threading.Event()

        def fake_run_qg(
            task: Task, run_dir: Path, workdir: Path, cfg: QualityGatesConfig, **kw: object
        ) -> QualityGatesResult:
            first_started.set()
            release_first.wait(timeout=5)
            return _pass_result(task.id)

        # Aggressive timeout so the test is fast.
        coalescer = QualityGateCoalescer(queue_timeout_s=0.1)

        with patch("bernstein.core.quality.quality_gate_coalescer.run_quality_gates", side_effect=fake_run_qg):
            t0 = threading.Thread(
                target=coalescer.run,
                args=(_make_task("T-first"), tmp_path, tmp_path, config),
                daemon=True,
            )
            t0.start()
            assert first_started.wait(timeout=5)

            with pytest.raises(TimeoutError, match="T-queued"):
                coalescer.run(_make_task("T-queued"), tmp_path, tmp_path, config)

            release_first.set()
            t0.join(timeout=5)


# ---------------------------------------------------------------------------
# Back-compat internals
# ---------------------------------------------------------------------------


class TestQueuedRunDataclass:
    def test_queued_run_stores_task(self, tmp_path: Path) -> None:
        task = _make_task("T-99")
        pr = _QueuedRun(task=task, run_dir=tmp_path, workdir=tmp_path)
        assert pr.task.id == "T-99"

    def test_queued_run_default_kwargs(self, tmp_path: Path) -> None:
        task = _make_task()
        pr = _QueuedRun(task=task, run_dir=tmp_path, workdir=tmp_path)
        assert pr.kwargs == {}

    def test_pending_run_alias_is_queued_run(self) -> None:
        """Legacy ``_PendingRun`` symbol resolves to :class:`_QueuedRun`."""
        assert _PendingRun is _QueuedRun
