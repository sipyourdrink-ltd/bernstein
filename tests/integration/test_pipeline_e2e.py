"""TEST-001: Integration tests for spawn-execute-verify pipeline.

Tests the full pipeline: create task -> spawn (mock adapter) -> agent output
-> janitor verify -> merge.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.guardrails import GuardrailsConfig
from bernstein.core.janitor import evaluate_signal, run_janitor, verify_task
from bernstein.core.models import (
    CompletionSignal,
    ModelConfig,
    Task,
    TaskStatus,
    TaskType,
)


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
        passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_glob_exists_fails_with_no_matches(self, tmp_path: Path) -> None:
        signal = CompletionSignal(type="glob_exists", value="src/*.py")
        passed, _detail = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_file_contains_passes_when_string_found(self, tmp_path: Path) -> None:
        target = tmp_path / "data.txt"
        target.write_text("Hello World\n")
        signal = CompletionSignal(type="file_contains", value="data.txt :: Hello")
        passed, detail = evaluate_signal(signal, tmp_path)
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
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
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
        assert result.proc is not None
        result.proc.wait(timeout=10)  # type: ignore[union-attr]

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
        assert result.proc is not None
        result.proc.wait(timeout=10)  # type: ignore[union-attr]

        task = _make_task(
            signals=[CompletionSignal(type="path_exists", value="wrong_file.py")],
        )
        all_passed, failed = verify_task(task, tmp_path)
        assert all_passed is False
        assert len(failed) == 1
