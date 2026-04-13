"""TEST-001: Integration tests for spawn-execute-verify pipeline.

Tests the full pipeline: create task -> spawn (mock adapter) -> agent output
-> janitor verify -> merge.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest
from bernstein.core.git_pr import merge_branch
from bernstein.core.guardrails import GuardrailsConfig
from bernstein.core.janitor import evaluate_signal, run_janitor, verify_task
from bernstein.core.lifecycle import transition_task
from bernstein.core.models import (
    CompletionSignal,
    ModelConfig,
    Task,
    TaskStatus,
)

from bernstein.adapters.base import CLIAdapter, SpawnResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    task_id: str = "T-E2E-001",
    title: str = "Create feature module",
    description: str = "Write src/feature.py with a greet() function.",
    signals: list[CompletionSignal] | None = None,
    status: TaskStatus = TaskStatus.OPEN,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        description=description,
        role="backend",
        status=status,
        completion_signals=signals or [],
    )


def _init_git_repo(workdir: Path) -> None:
    """Initialize a bare git repo with an initial commit."""
    subprocess.run(["git", "init", "-b", "main"], cwd=workdir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@e2e.local"],
        cwd=workdir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "E2E Test"],
        cwd=workdir,
        check=True,
        capture_output=True,
    )
    readme = workdir / "README.md"
    readme.write_text("# E2E Pipeline Test\n")
    subprocess.run(["git", "add", "."], cwd=workdir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=workdir,
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# TEST-001a: Evaluate individual signals
# ---------------------------------------------------------------------------


class TestSignalEvaluation:
    """Verify each signal type evaluates correctly."""

    def test_path_exists_passes_when_file_present(self, tmp_path: Path) -> None:
        (tmp_path / "output.txt").write_text("hello")
        signal = CompletionSignal(type="path_exists", value="output.txt")
        passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is True
        assert detail == "exists"

    def test_path_exists_fails_when_missing(self, tmp_path: Path) -> None:
        signal = CompletionSignal(type="path_exists", value="missing.txt")
        passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert detail == "not found"

    def test_glob_exists_passes_with_matches(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "module.py").write_text("# code\n")
        signal = CompletionSignal(type="glob_exists", value="src/*.py")
        passed, _detail = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_glob_exists_fails_with_no_matches(self, tmp_path: Path) -> None:
        signal = CompletionSignal(type="glob_exists", value="src/*.py")
        passed, _detail = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_file_contains_passes_when_string_found(self, tmp_path: Path) -> None:
        target = tmp_path / "data.txt"
        target.write_text("Hello World\n")
        signal = CompletionSignal(type="file_contains", value="data.txt :: Hello")
        passed, _detail = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_file_contains_fails_when_string_missing(self, tmp_path: Path) -> None:
        target = tmp_path / "data.txt"
        target.write_text("Goodbye\n")
        signal = CompletionSignal(type="file_contains", value="data.txt :: Hello")
        passed, _detail = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_unknown_signal_type_fails(self, tmp_path: Path) -> None:
        signal = CompletionSignal(type="path_exists", value="")
        # Patch to simulate an unknown signal type
        object.__setattr__(signal, "type", "bogus_type")
        passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert "unknown signal type" in detail


# ---------------------------------------------------------------------------
# TEST-001b: Full verify_task with multiple signals
# ---------------------------------------------------------------------------


class TestVerifyTask:
    """Verify that verify_task aggregates signal results correctly."""

    def test_all_signals_pass(self, tmp_path: Path) -> None:
        (tmp_path / "feature.py").write_text("def greet(): return 'hi'\n")
        task = _make_task(
            signals=[
                CompletionSignal(type="path_exists", value="feature.py"),
                CompletionSignal(type="file_contains", value="feature.py :: def greet"),
            ],
        )
        all_passed, failed = verify_task(task, tmp_path)
        assert all_passed is True
        assert failed == []

    def test_partial_failure_reports_failed_signals(self, tmp_path: Path) -> None:
        (tmp_path / "feature.py").write_text("def greet(): return 'hi'\n")
        task = _make_task(
            signals=[
                CompletionSignal(type="path_exists", value="feature.py"),
                CompletionSignal(type="path_exists", value="tests/test_feature.py"),
            ],
        )
        all_passed, failed = verify_task(task, tmp_path)
        assert all_passed is False
        assert len(failed) == 1
        assert "tests/test_feature.py" in failed[0]

    def test_no_signals_passes_vacuously(self, tmp_path: Path) -> None:
        task = _make_task(signals=[])
        all_passed, failed = verify_task(task, tmp_path)
        assert all_passed is True
        assert failed == []


# ---------------------------------------------------------------------------
# TEST-001c: run_janitor end-to-end (async, mocked LLM)
# ---------------------------------------------------------------------------


class TestJanitorPipeline:
    """Integration test for run_janitor with concrete signals."""

    @pytest.mark.asyncio
    async def test_janitor_passes_on_satisfied_signals(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        (tmp_path / "output.txt").write_text("result\n")
        await asyncio.to_thread(subprocess.run, ["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        await asyncio.to_thread(
            subprocess.run,
            ["git", "commit", "-m", "add output"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

        task = _make_task(
            status=TaskStatus.DONE,
            signals=[CompletionSignal(type="path_exists", value="output.txt")],
        )

        # Disable guardrails to test signals only
        no_guardrails = GuardrailsConfig(
            secrets=False,
            scope=False,
            file_permissions=False,
            license_scan=False,
            readme_reminder=False,
        )
        results = await run_janitor([task], tmp_path, guardrails_config=no_guardrails)
        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].task_id == task.id

    @pytest.mark.asyncio
    async def test_janitor_fails_on_unsatisfied_signals(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)

        task = _make_task(
            status=TaskStatus.DONE,
            signals=[CompletionSignal(type="path_exists", value="nonexistent.py")],
        )

        no_guardrails = GuardrailsConfig(
            secrets=False,
            scope=False,
            file_permissions=False,
            license_scan=False,
            readme_reminder=False,
        )
        results = await run_janitor([task], tmp_path, guardrails_config=no_guardrails)
        assert len(results) == 1
        assert results[0].passed is False

    @pytest.mark.asyncio
    async def test_janitor_skips_tasks_without_signals(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        task = _make_task(status=TaskStatus.DONE, signals=[])

        results = await run_janitor([task], tmp_path)
        assert len(results) == 0  # Skipped — no signals


# ---------------------------------------------------------------------------
# TEST-001d: Mock adapter spawn -> output -> verify pipeline
# ---------------------------------------------------------------------------


class _FakeAdapter(CLIAdapter):
    """Adapter that creates a file then exits."""

    def __init__(self, output_file: str = "agent_output.txt") -> None:
        self._output_file = output_file

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = 1800,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
    ) -> SpawnResult:
        import sys
        import tempfile

        script = f"""
import sys
from pathlib import Path
workdir = Path(sys.argv[1])
(workdir / "{self._output_file}").write_text("agent completed\\n")
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir=str(workdir)) as f:
            f.write(script)
            script_path = f.name

        log_path = workdir / ".sdd" / "runtime" / f"agent-{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        proc = subprocess.Popen(
            [sys.executable, script_path, str(workdir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(workdir),
        )
        return SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)

    def name(self) -> str:
        return "fake-e2e"


class TestSpawnExecuteVerifyPipeline:
    """Full pipeline: spawn mock adapter -> wait -> verify signals."""

    def test_adapter_creates_output_and_janitor_verifies(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)

        adapter = _FakeAdapter(output_file="feature.py")
        result = adapter.spawn(
            prompt="Create feature.py",
            workdir=tmp_path,
            model_config=ModelConfig(model="mock", effort="high"),
            session_id="sess-001",
        )

        # Wait for the subprocess to complete
        if result.proc is None:
            pytest.fail("Expected adapter to return a process handle")
        result.proc.wait(timeout=10)

        # Verify the agent produced the expected file
        assert (tmp_path / "feature.py").exists()

        # Now run janitor verification
        task = _make_task(
            signals=[CompletionSignal(type="path_exists", value="feature.py")],
        )
        all_passed, failed = verify_task(task, tmp_path)
        assert all_passed is True
        assert failed == []

    def test_pipeline_fails_when_agent_does_not_produce_output(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)

        # Adapter produces agent_output.txt, but signal checks for wrong_file.py
        adapter = _FakeAdapter(output_file="agent_output.txt")
        result = adapter.spawn(
            prompt="Create something",
            workdir=tmp_path,
            model_config=ModelConfig(model="mock", effort="high"),
            session_id="sess-002",
        )
        if result.proc is None:
            pytest.fail("Expected adapter to return a process handle")
        result.proc.wait(timeout=10)

        task = _make_task(
            signals=[CompletionSignal(type="path_exists", value="wrong_file.py")],
        )
        all_passed, failed = verify_task(task, tmp_path)
        assert all_passed is False
        assert len(failed) == 1


# ---------------------------------------------------------------------------
# TEST-001e: Full spawn → execute → verify → merge pipeline with status transitions
# ---------------------------------------------------------------------------


def _create_agent_branch(workdir: Path, branch: str) -> None:
    """Create a new branch off main to simulate an agent worktree."""
    subprocess.run(["git", "checkout", "-b", branch], cwd=workdir, check=True, capture_output=True)


def _checkout_main(workdir: Path) -> None:
    """Return to main branch."""
    subprocess.run(["git", "checkout", "main"], cwd=workdir, check=True, capture_output=True)


class TestFullSpawnExecuteVerifyMergePipeline:
    """TEST-001e: Complete pipeline with task status transitions and git merge.

    Exercises the critical path:
    create task (OPEN) → claim (CLAIMED) → in progress (IN_PROGRESS) →
    spawn agent → agent writes output → mark done (DONE) →
    janitor verify → close (CLOSED) → merge branch into main.
    """

    def test_complete_pipeline_with_status_transitions_and_merge(self, tmp_path: Path) -> None:
        """Full pipeline: task lifecycle transitions + spawn + verify + git merge."""
        _init_git_repo(tmp_path)

        # --- Task lifecycle: OPEN → CLAIMED → IN_PROGRESS ---
        task = _make_task(
            task_id="T-FULL-001",
            signals=[
                CompletionSignal(type="path_exists", value="src/feature.py"),
                CompletionSignal(type="file_contains", value="src/feature.py :: def greet"),
            ],
        )
        assert task.status == TaskStatus.OPEN

        transition_task(task, TaskStatus.CLAIMED, actor="spawner")
        assert task.status == TaskStatus.CLAIMED

        transition_task(task, TaskStatus.IN_PROGRESS, actor="agent")
        assert task.status == TaskStatus.IN_PROGRESS

        # --- Agent work: create branch and write output ---
        _create_agent_branch(tmp_path, "agent/T-FULL-001")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "feature.py").write_text("def greet():\n    return 'hello'\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "feat: add greet function"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

        # --- Task lifecycle: IN_PROGRESS → DONE ---
        transition_task(task, TaskStatus.DONE, actor="agent")
        assert task.status == TaskStatus.DONE

        # --- Janitor verification on the agent branch ---
        all_passed, failed = verify_task(task, tmp_path)
        assert all_passed is True, f"Janitor failed: {failed}"
        assert failed == []

        # --- Merge into main ---
        _checkout_main(tmp_path)
        merge_result = merge_branch(tmp_path, "agent/T-FULL-001", message="Merge agent/T-FULL-001")
        assert merge_result.ok, f"Merge failed: {merge_result.stderr}"

        # Confirm the merged file exists on main
        assert (tmp_path / "src" / "feature.py").exists()

        # --- Task lifecycle: DONE → CLOSED ---
        transition_task(task, TaskStatus.CLOSED, actor="janitor")
        assert task.status == TaskStatus.CLOSED

    def test_pipeline_janitor_blocks_close_on_missing_output(self, tmp_path: Path) -> None:
        """If janitor fails, the task must NOT transition to CLOSED."""
        _init_git_repo(tmp_path)

        task = _make_task(
            task_id="T-FULL-002",
            signals=[CompletionSignal(type="path_exists", value="src/missing.py")],
        )
        transition_task(task, TaskStatus.CLAIMED, actor="spawner")
        transition_task(task, TaskStatus.IN_PROGRESS, actor="agent")

        # Agent does NOT create the required file
        _create_agent_branch(tmp_path, "agent/T-FULL-002")
        # (no file written)

        transition_task(task, TaskStatus.DONE, actor="agent")

        all_passed, failed = verify_task(task, tmp_path)
        assert all_passed is False
        assert len(failed) > 0

        # Task should NOT be closed — re-open for retry
        transition_task(task, TaskStatus.FAILED, actor="janitor")
        assert task.status == TaskStatus.FAILED

        # Retry path: FAILED → OPEN
        transition_task(task, TaskStatus.OPEN, actor="retry_handler")
        assert task.status == TaskStatus.OPEN

    @pytest.mark.asyncio
    async def test_async_janitor_integrate_with_spawn_verify(self, tmp_path: Path) -> None:
        """Async janitor pipeline: spawn fake adapter, then run_janitor to confirm."""
        _init_git_repo(tmp_path)

        # Spawn the fake adapter to create the required file
        adapter = _FakeAdapter(output_file="result.py")
        result = adapter.spawn(
            prompt="Create result.py",
            workdir=tmp_path,
            model_config=ModelConfig(model="mock", effort="high"),
            session_id="sess-e2e-003",
        )
        assert result.proc is not None
        await asyncio.to_thread(result.proc.wait, 10)

        # Commit the agent's output (simulating agent commit step)
        await asyncio.to_thread(
            subprocess.run,
            ["git", "add", "."],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        await asyncio.to_thread(
            subprocess.run,
            ["git", "commit", "-m", "agent: create result.py"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

        task = _make_task(
            task_id="T-FULL-003",
            status=TaskStatus.DONE,
            signals=[CompletionSignal(type="path_exists", value="result.py")],
        )

        no_guardrails = GuardrailsConfig(
            secrets=False,
            scope=False,
            file_permissions=False,
            license_scan=False,
            readme_reminder=False,
        )
        results = await run_janitor([task], tmp_path, guardrails_config=no_guardrails)
        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].task_id == task.id
