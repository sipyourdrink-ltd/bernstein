"""TEST-005: Error path coverage for spawner.

Exercises each error path in the spawn pipeline with failure injection:
adapter spawn failures, rate limit errors, template errors, worktree errors,
and process exit detection.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from bernstein.core.models import (
    Complexity,
    ModelConfig,
    Scope,
    Task,
    TaskStatus,
)
from bernstein.core.spawn_errors import (
    AdapterNotInstalledError,
    CategorizedSpawnError,
    ModelNotAvailableError,
    PermissionDeniedError,
    PromptTooLongError,
    ResourceExhaustedError,
    RetryStrategy,
    WorktreeCreationError,
    classify_spawn_error,
)

from bernstein.adapters.base import (
    CLIAdapter,
    RateLimitError,
    SpawnError,
    SpawnResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str = "T-SP-001",
    status: TaskStatus = TaskStatus.OPEN,
) -> Task:
    return Task(
        id=task_id,
        title="Spawner failure test",
        description="Test spawner error paths.",
        role="backend",
        scope=Scope.SMALL,
        complexity=Complexity.LOW,
        status=status,
    )


class _FailingAdapter(CLIAdapter):
    """Adapter that raises the configured error on spawn."""

    def __init__(self, error: Exception) -> None:
        self._error = error

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
        raise self._error

    def name(self) -> str:
        return "failing-mock"


class _FastExitAdapter(CLIAdapter):
    """Adapter that returns a process that exits immediately."""

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
        log_path = workdir / ".sdd" / "runtime" / f"agent-{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("fast exit\n")

        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.exit(1)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(workdir),
        )
        proc.wait()  # Wait for immediate exit
        return SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)

    def name(self) -> str:
        return "fast-exit-mock"


class _SuccessAdapter(CLIAdapter):
    """Adapter that succeeds with a mock PID."""

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
        log_path = workdir / ".sdd" / "runtime" / f"agent-{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("started\n")
        return SpawnResult(pid=99999, log_path=log_path)

    def name(self) -> str:
        return "success-mock"


# ---------------------------------------------------------------------------
# TEST-005a: SpawnError on adapter failure
# ---------------------------------------------------------------------------


class TestSpawnError:
    """SpawnError is raised when adapter spawn fails."""

    def test_spawn_error_is_runtime_error(self) -> None:
        err = SpawnError("process exited too early")
        assert isinstance(err, RuntimeError)
        assert "too early" in str(err)

    def test_failing_adapter_raises_spawn_error(self, tmp_path: Path) -> None:
        adapter = _FailingAdapter(SpawnError("binary not found"))
        with pytest.raises(SpawnError, match="binary not found"):
            adapter.spawn(
                prompt="test",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="sess-001",
            )


# ---------------------------------------------------------------------------
# TEST-005b: RateLimitError on provider throttling
# ---------------------------------------------------------------------------


class TestRateLimitError:
    """RateLimitError is a subclass of SpawnError."""

    def test_rate_limit_error_hierarchy(self) -> None:
        err = RateLimitError("429 Too Many Requests")
        assert isinstance(err, SpawnError)
        assert isinstance(err, RuntimeError)

    def test_failing_adapter_raises_rate_limit(self, tmp_path: Path) -> None:
        adapter = _FailingAdapter(RateLimitError("quota exceeded"))
        with pytest.raises(RateLimitError, match="quota exceeded"):
            adapter.spawn(
                prompt="test",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="sess-002",
            )

    def test_rate_limit_detection_in_log_lines(self) -> None:
        """CLIAdapter._is_rate_limit_error detects various rate limit strings."""
        assert CLIAdapter._is_rate_limit_error(["Error: rate limit exceeded"]) is True
        assert CLIAdapter._is_rate_limit_error(["HTTP 429 too many requests"]) is True
        assert CLIAdapter._is_rate_limit_error(["quota exceeded for this model"]) is True
        assert CLIAdapter._is_rate_limit_error(["you've hit your limit"]) is True
        assert CLIAdapter._is_rate_limit_error(["Everything is fine"]) is False
        assert CLIAdapter._is_rate_limit_error([]) is False


# ---------------------------------------------------------------------------
# TEST-005c: Fast-exit process detection
# ---------------------------------------------------------------------------


class TestFastExitDetection:
    """Adapter processes that exit immediately are detected."""

    def test_fast_exit_process_has_exit_code(self, tmp_path: Path) -> None:
        adapter = _FastExitAdapter()
        result = adapter.spawn(
            prompt="test",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="sess-003",
        )
        assert result.proc is not None
        # Process already exited, so poll should return exit code
        exit_code = result.proc.poll()  # type: ignore[union-attr]
        assert exit_code is not None
        assert exit_code != 0

    def test_successful_spawn_returns_pid(self, tmp_path: Path) -> None:
        adapter = _SuccessAdapter()
        result = adapter.spawn(
            prompt="test",
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id="sess-004",
        )
        assert result.pid > 0
        assert result.log_path.exists()


# ---------------------------------------------------------------------------
# TEST-005d: Template rendering failure
# ---------------------------------------------------------------------------


class TestTemplateFailure:
    """Missing or corrupt role templates are handled."""

    def test_missing_template_dir_handled(self, tmp_path: Path) -> None:
        from bernstein.templates.renderer import TemplateError, render_role_prompt

        # Non-existent templates directory should raise (FileNotFoundError or TemplateError)
        with pytest.raises((TemplateError, FileNotFoundError)):
            render_role_prompt(
                role="nonexistent_role",
                context={"task_description": "test"},
                templates_dir=tmp_path / "no_such_dir" / "roles",
            )


# ---------------------------------------------------------------------------
# TEST-005e: SpawnResult fields
# ---------------------------------------------------------------------------


class TestSpawnResultFields:
    """Verify SpawnResult dataclass holds all expected fields."""

    def test_basic_fields(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        log.write_text("log\n")
        result = SpawnResult(pid=42, log_path=log)
        assert result.pid == 42
        assert result.log_path == log
        assert result.proc is None
        assert result.timeout_timer is None
        assert result.abort_reason is None
        assert result.abort_detail == ""
        assert result.finish_reason == ""

    def test_abort_reason_propagation(self) -> None:
        from bernstein.core.models import AbortReason

        result = SpawnResult(
            pid=1,
            log_path=Path("/tmp/test.log"),
            abort_reason=AbortReason.TIMEOUT,
            abort_detail="killed after 30min",
        )
        assert result.abort_reason == AbortReason.TIMEOUT
        assert "30min" in result.abort_detail


# ---------------------------------------------------------------------------
# TEST-005f: Log line reading for error detection
# ---------------------------------------------------------------------------


class TestLogLineReading:
    """CLIAdapter._read_last_lines reads the tail of a log."""

    def test_read_last_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        lines = [f"line {i}\n" for i in range(20)]
        log.write_text("".join(lines))
        result = CLIAdapter._read_last_lines(log, n=5)
        assert len(result) == 5
        assert result[-1] == "line 19"

    def test_read_last_lines_short_file(self, tmp_path: Path) -> None:
        log = tmp_path / "short.log"
        log.write_text("one\ntwo\n")
        result = CLIAdapter._read_last_lines(log, n=10)
        assert len(result) == 2

    def test_read_last_lines_missing_file(self, tmp_path: Path) -> None:
        result = CLIAdapter._read_last_lines(tmp_path / "missing.log", n=5)
        assert result == []


# ---------------------------------------------------------------------------
# TEST-005g: Multiple error types in sequence
# ---------------------------------------------------------------------------


class TestSequentialFailures:
    """Multiple spawns can fail with different error types."""

    def test_different_failure_modes(self, tmp_path: Path) -> None:
        errors: list[Exception] = [
            SpawnError("binary not found"),
            RateLimitError("429"),
            OSError("permission denied"),
        ]
        for err in errors:
            adapter = _FailingAdapter(err)
            with pytest.raises(type(err)):
                adapter.spawn(
                    prompt="test",
                    workdir=tmp_path,
                    model_config=ModelConfig(model="sonnet", effort="high"),
                    session_id="sess-multi",
                )


# ---------------------------------------------------------------------------
# TEST-005h: Failure injection matrix — each error path → correct category + strategy
# ---------------------------------------------------------------------------


# (raw_exception, expected_category_type, expected_retry_strategy)
_CLASSIFY_CASES: list[tuple[Exception, type[CategorizedSpawnError], RetryStrategy]] = [
    (
        FileNotFoundError("claude: not found"),
        AdapterNotInstalledError,
        RetryStrategy.NO_RETRY,
    ),
    (
        RuntimeError("Model not available: opus-3 deprecated"),
        ModelNotAvailableError,
        RetryStrategy.RETRY_FALLBACK,
    ),
    (
        ValueError("Prompt too long, exceeds context window"),
        PromptTooLongError,
        RetryStrategy.RETRY_FALLBACK,
    ),
    (
        RuntimeError("git worktree add failed: stale lock"),
        WorktreeCreationError,
        RetryStrategy.RETRY_AFTER_FIX,
    ),
    (
        PermissionError("Permission denied: /var/run/agent.sock"),
        PermissionDeniedError,
        RetryStrategy.RETRY_AFTER_FIX,
    ),
    (
        OSError("No space left on device"),
        ResourceExhaustedError,
        RetryStrategy.RETRY_SAME,
    ),
    (
        MemoryError("Out of memory"),
        ResourceExhaustedError,
        RetryStrategy.RETRY_SAME,
    ),
    (
        RuntimeError("HTTP 403 Forbidden"),
        PermissionDeniedError,
        RetryStrategy.RETRY_AFTER_FIX,
    ),
    (
        OSError("Too many open files"),
        ResourceExhaustedError,
        RetryStrategy.RETRY_SAME,
    ),
]


class TestFailureInjectionMatrix:
    """TEST-005h: Parametrized failure injection across all error categories.

    Verifies that each error path produces the correct CategorizedSpawnError
    subclass (failure category) with the correct RetryStrategy attached.
    """

    @pytest.mark.parametrize(
        "raw_error,expected_type,expected_strategy",
        _CLASSIFY_CASES,
        ids=[type(c[0]).__name__ + "/" + c[1].__name__ for c in _CLASSIFY_CASES],
    )
    def test_classify_yields_correct_category_and_strategy(
        self,
        raw_error: Exception,
        expected_type: type[CategorizedSpawnError],
        expected_strategy: RetryStrategy,
    ) -> None:
        """classify_spawn_error maps each raw error to the right category + strategy."""
        classified = classify_spawn_error(raw_error)
        assert isinstance(classified, expected_type), (
            f"Expected {expected_type.__name__}, got {type(classified).__name__}: {classified}"
        )
        assert classified.retry_strategy == expected_strategy, (
            f"Expected {expected_strategy}, got {classified.retry_strategy}"
        )

    @pytest.mark.parametrize(
        "pre_categorized,expected_strategy",
        [
            (AdapterNotInstalledError("not installed"), RetryStrategy.NO_RETRY),
            (ModelNotAvailableError("model gone"), RetryStrategy.RETRY_FALLBACK),
            (PromptTooLongError("too long"), RetryStrategy.RETRY_FALLBACK),
            (WorktreeCreationError("stale lock"), RetryStrategy.RETRY_AFTER_FIX),
            (PermissionDeniedError("bad key"), RetryStrategy.RETRY_AFTER_FIX),
            (ResourceExhaustedError("disk full"), RetryStrategy.RETRY_SAME),
        ],
        ids=[
            "adapter-not-installed",
            "model-not-available",
            "prompt-too-long",
            "worktree-creation",
            "permission-denied",
            "resource-exhausted",
        ],
    )
    def test_adapter_injection_propagates_category_and_strategy(
        self,
        pre_categorized: CategorizedSpawnError,
        expected_strategy: RetryStrategy,
        tmp_path: Path,
    ) -> None:
        """Errors injected via _FailingAdapter propagate with correct type and strategy."""
        adapter = _FailingAdapter(pre_categorized)
        with pytest.raises(type(pre_categorized)) as exc_info:
            adapter.spawn(
                prompt="failure injection test",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="sess-matrix",
            )
        raised = exc_info.value
        assert isinstance(raised, CategorizedSpawnError)
        assert raised.retry_strategy == expected_strategy

    def test_passthrough_preserves_identity(self) -> None:
        """classify_spawn_error returns the same object for already-categorized errors."""
        original = WorktreeCreationError("stale .git/worktrees lock")
        result = classify_spawn_error(original)
        assert result is original

    def test_unknown_error_gets_no_retry(self) -> None:
        """Unrecognized errors default to NO_RETRY base category."""
        raw = RuntimeError("something completely unexpected happened")
        classified = classify_spawn_error(raw)
        assert isinstance(classified, CategorizedSpawnError)
        assert classified.retry_strategy == RetryStrategy.NO_RETRY

    def test_to_dict_captures_category_metadata(self) -> None:
        """to_dict() exposes error type, strategy, provider, and detail for telemetry."""
        err = ModelNotAvailableError("opus deprecated", provider="anthropic", detail="HTTP 404")
        d = err.to_dict()
        assert d["error_type"] == "ModelNotAvailableError"
        assert d["retry_strategy"] == RetryStrategy.RETRY_FALLBACK
        assert d["provider"] == "anthropic"
        assert "404" in d["detail"]
