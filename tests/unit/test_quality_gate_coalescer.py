"""Tests for trailing-run coalescence in quality gates (quality_gate_coalescer.py)."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

from bernstein.core.models import Complexity, Scope, Task
from bernstein.core.quality_gate_coalescer import QualityGateCoalescer, _PendingRun
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

        with patch("bernstein.core.quality_gate_coalescer.run_quality_gates", return_value=_pass_result()) as mock_rqg:
            coalescer.run(task, tmp_path, tmp_path, config)

        mock_rqg.assert_called_once()


# ---------------------------------------------------------------------------
# Coalescence during active run
# ---------------------------------------------------------------------------


class TestCoalescence:
    def test_coalesced_request_returns_pass_immediately(self, tmp_path: Path) -> None:
        """A request arriving during an active run returns a lightweight pass result."""
        coalescer = QualityGateCoalescer()
        # Manually set in_progress without holding the lock (safe in tests)
        coalescer._in_progress = True

        task = _make_task("T-002")
        config = QualityGatesConfig(enabled=False)
        result = coalescer.run(task, tmp_path, tmp_path, config)

        assert result.passed
        assert result.task_id == "T-002"
        assert coalescer.pending_count == 1

    def test_only_latest_pending_request_kept(self, tmp_path: Path) -> None:
        """Multiple coalesced requests replace each other — only the latest is kept."""
        coalescer = QualityGateCoalescer()
        coalescer._in_progress = True

        config = QualityGatesConfig(enabled=False)
        for i in range(5):
            coalescer.run(_make_task(f"T-{i:03d}"), tmp_path, tmp_path, config)

        assert coalescer.pending_count == 1
        assert coalescer._pending is not None
        assert coalescer._pending.task.id == "T-004"  # last one wins

    def test_rapid_completions_produce_one_trailing_run(self, tmp_path: Path) -> None:
        """When tasks complete rapidly, only ONE trailing run is performed."""
        config = QualityGatesConfig(enabled=False)
        call_log: list[str] = []

        def fake_run_qg(task: Task, run_dir: Path, workdir: Path, cfg: QualityGatesConfig, **kw: object) -> QualityGatesResult:
            call_log.append(task.id)
            return _pass_result(task.id)

        coalescer = QualityGateCoalescer()
        barrier = threading.Barrier(2)
        first_run_released = threading.Event()

        def slow_run_qg(task: Task, run_dir: Path, workdir: Path, cfg: QualityGatesConfig, **kw: object) -> QualityGatesResult:
            call_log.append(task.id)
            barrier.wait(timeout=5)     # signal: first run started
            first_run_released.wait(timeout=5)  # wait: coalesced tasks queued
            return _pass_result(task.id)

        with patch("bernstein.core.quality_gate_coalescer.run_quality_gates", side_effect=slow_run_qg):
            coalescer_thread_result: list[QualityGatesResult] = []

            def run_first() -> None:
                result = coalescer.run(_make_task("T-first"), tmp_path, tmp_path, config)
                coalescer_thread_result.append(result)

            t = threading.Thread(target=run_first, daemon=True)
            t.start()

            # Wait until the first run has started
            barrier.wait(timeout=5)

            # These arrive while first run is active — should all coalesce into one slot
            for i in range(3):
                coalescer.run(_make_task(f"T-rapid-{i}"), tmp_path, tmp_path, config)

            # Release the first run to finish
            first_run_released.set()
            t.join(timeout=10)

        # First run + exactly ONE trailing run = 2 total calls
        assert len(call_log) == 2, f"Expected 2 calls, got {len(call_log)}: {call_log}"
        assert call_log[0] == "T-first"
        assert call_log[1] == "T-rapid-2"  # last coalesced task

    def test_state_clean_after_trailing_run(self, tmp_path: Path) -> None:
        """After the trailing run completes, in_progress is False and pending is 0."""
        config = QualityGatesConfig(enabled=False)
        coalescer = QualityGateCoalescer()

        calls: list[str] = []

        barrier = threading.Barrier(2)
        released = threading.Event()

        def slow_run(task: Task, run_dir: Path, workdir: Path, cfg: QualityGatesConfig, **kw: object) -> QualityGatesResult:
            calls.append(task.id)
            if task.id == "T-first":
                barrier.wait(timeout=5)
                released.wait(timeout=5)
            return _pass_result(task.id)

        with patch("bernstein.core.quality_gate_coalescer.run_quality_gates", side_effect=slow_run):
            t = threading.Thread(
                target=coalescer.run, args=(_make_task("T-first"), tmp_path, tmp_path, config), daemon=True
            )
            t.start()
            barrier.wait(timeout=5)
            coalescer.run(_make_task("T-pending"), tmp_path, tmp_path, config)
            released.set()
            t.join(timeout=10)

        assert not coalescer.in_progress
        assert coalescer.pending_count == 0


# ---------------------------------------------------------------------------
# _PendingRun dataclass
# ---------------------------------------------------------------------------


class TestPendingRun:
    def test_pending_run_stores_task(self, tmp_path: Path) -> None:
        task = _make_task("T-99")
        pr = _PendingRun(task=task, run_dir=tmp_path, workdir=tmp_path)
        assert pr.task.id == "T-99"

    def test_pending_run_default_kwargs(self, tmp_path: Path) -> None:
        task = _make_task()
        pr = _PendingRun(task=task, run_dir=tmp_path, workdir=tmp_path)
        assert pr.kwargs == {}
