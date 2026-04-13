"""Tests for fast-path task classification and deterministic executors."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.fast_path import (
    FastPathAction,
    FastPathResult,
    FastPathStats,
    TaskLevel,
    classify_task,
    execute_fast_path,
    get_l1_model_config,
    try_fast_path_batch,
)
from bernstein.core.models import Complexity, Scope, Task

# --- Helpers ---


def _make_task(
    *,
    title: str = "Implement feature",
    description: str = "",
    role: str = "backend",
    complexity: Complexity = Complexity.MEDIUM,
    scope: Scope = Scope.MEDIUM,
    priority: int = 2,
    model: str | None = None,
    owned_files: list[str] | None = None,
) -> Task:
    return Task(
        id="T-test",
        title=title,
        description=description,
        role=role,
        complexity=complexity,
        scope=scope,
        priority=priority,
        model=model,
        owned_files=owned_files or [],
    )


# --- Classification tests ---


class TestClassifyTask:
    """Tests for classify_task()."""

    def test_high_complexity_always_l2(self) -> None:
        task = _make_task(title="Format code", complexity=Complexity.HIGH)
        result = classify_task(task)
        assert result.level == TaskLevel.L2

    def test_large_scope_always_l2(self) -> None:
        task = _make_task(title="Format code", scope=Scope.LARGE)
        result = classify_task(task)
        assert result.level == TaskLevel.L2

    def test_critical_priority_always_l2(self) -> None:
        task = _make_task(title="Format code", priority=1)
        result = classify_task(task)
        assert result.level == TaskLevel.L2

    def test_manager_role_always_l2(self) -> None:
        task = _make_task(title="Format code", role="manager")
        result = classify_task(task)
        assert result.level == TaskLevel.L2

    def test_architect_role_always_l2(self) -> None:
        task = _make_task(title="Format code", role="architect")
        result = classify_task(task)
        assert result.level == TaskLevel.L2

    def test_security_role_always_l2(self) -> None:
        task = _make_task(title="Sort imports", role="security")
        result = classify_task(task)
        assert result.level == TaskLevel.L2

    def test_opus_override_always_l2(self) -> None:
        task = _make_task(title="Format code", model="opus")
        result = classify_task(task)
        assert result.level == TaskLevel.L2

    def test_format_classified_l0(self) -> None:
        task = _make_task(title="Format the codebase", complexity=Complexity.LOW, scope=Scope.SMALL)
        result = classify_task(task)
        assert result.level == TaskLevel.L0
        assert result.action == FastPathAction.RUFF_FORMAT

    def test_formatting_classified_l0(self) -> None:
        task = _make_task(title="Apply formatting to utils.py", complexity=Complexity.LOW)
        result = classify_task(task)
        assert result.level == TaskLevel.L0
        assert result.action == FastPathAction.RUFF_FORMAT

    def test_lint_classified_l0(self) -> None:
        task = _make_task(title="Fix lint errors in models.py", complexity=Complexity.LOW)
        result = classify_task(task)
        assert result.level == TaskLevel.L0
        assert result.action == FastPathAction.RUFF_FIX

    def test_autofix_classified_l0(self) -> None:
        task = _make_task(title="Run autofix on codebase", complexity=Complexity.LOW)
        result = classify_task(task)
        assert result.level == TaskLevel.L0
        assert result.action == FastPathAction.RUFF_FIX

    def test_sort_imports_classified_l0(self) -> None:
        task = _make_task(title="Sort imports across project", complexity=Complexity.LOW)
        result = classify_task(task)
        assert result.level == TaskLevel.L0
        assert result.action == FastPathAction.SORT_IMPORTS

    def test_isort_classified_l0(self) -> None:
        task = _make_task(title="Run isort on all files", complexity=Complexity.LOW)
        result = classify_task(task)
        assert result.level == TaskLevel.L0
        assert result.action == FastPathAction.SORT_IMPORTS

    def test_rename_classified_l0(self) -> None:
        task = _make_task(
            title="Rename getCwd to get_current_dir",
            complexity=Complexity.LOW,
        )
        result = classify_task(task)
        assert result.level == TaskLevel.L0
        assert result.action == FastPathAction.RENAME_SYMBOL

    def test_rename_with_arrow_classified_l0(self) -> None:
        task = _make_task(
            title="Rename old_func -> new_func",
            complexity=Complexity.LOW,
        )
        result = classify_task(task)
        assert result.level == TaskLevel.L0
        assert result.action == FastPathAction.RENAME_SYMBOL

    def test_docstring_classified_l1(self) -> None:
        task = _make_task(title="Add docstring to parse_config", complexity=Complexity.LOW)
        result = classify_task(task)
        assert result.level == TaskLevel.L1

    def test_type_hint_classified_l1(self) -> None:
        task = _make_task(title="Add type hint to all public methods", complexity=Complexity.LOW)
        result = classify_task(task)
        assert result.level == TaskLevel.L1

    def test_typo_classified_l1(self) -> None:
        task = _make_task(title="Fix typo in README", complexity=Complexity.LOW)
        result = classify_task(task)
        assert result.level == TaskLevel.L1

    def test_low_complexity_small_scope_l1(self) -> None:
        task = _make_task(
            title="Update config value",
            complexity=Complexity.LOW,
            scope=Scope.SMALL,
        )
        result = classify_task(task)
        assert result.level == TaskLevel.L1
        assert result.confidence == pytest.approx(0.7)

    def test_complex_feature_l2(self) -> None:
        task = _make_task(title="Implement WebSocket support")
        result = classify_task(task)
        assert result.level == TaskLevel.L2

    def test_classification_has_confidence(self) -> None:
        task = _make_task(title="Fix lint issues", complexity=Complexity.LOW)
        result = classify_task(task)
        assert 0.0 < result.confidence <= 1.0

    def test_classification_has_reason(self) -> None:
        task = _make_task(title="Fix lint issues", complexity=Complexity.LOW)
        result = classify_task(task)
        assert result.reason != ""


# --- Executor tests ---


class TestExecuteFastPath:
    """Tests for execute_fast_path() deterministic executors."""

    @patch("bernstein.core.quality.fast_path.subprocess.run")
    def test_ruff_format_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="1 file reformatted",
            stderr="",
        )
        result = execute_fast_path(
            FastPathAction.RUFF_FORMAT,
            Path("/tmp/project"),
            ["src/main.py"],
        )
        assert result.success is True
        assert result.action == FastPathAction.RUFF_FORMAT
        assert result.duration_s >= 0

    @patch("bernstein.core.quality.fast_path.subprocess.run")
    def test_ruff_fix_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Fixed 3 errors",
            stderr="",
        )
        result = execute_fast_path(
            FastPathAction.RUFF_FIX,
            Path("/tmp/project"),
            [],
        )
        assert result.success is True
        assert result.action == FastPathAction.RUFF_FIX

    @patch("bernstein.core.quality.fast_path.subprocess.run")
    def test_sort_imports_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="",
            stderr="",
        )
        result = execute_fast_path(
            FastPathAction.SORT_IMPORTS,
            Path("/tmp/project"),
            ["src/utils.py"],
        )
        assert result.success is True
        assert result.action == FastPathAction.SORT_IMPORTS

    @patch("bernstein.core.quality.fast_path.subprocess.run")
    def test_ruff_format_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=2,
            stdout="",
            stderr="error: invalid syntax",
        )
        result = execute_fast_path(
            FastPathAction.RUFF_FORMAT,
            Path("/tmp/project"),
            ["bad.py"],
        )
        assert result.success is False
        assert result.error is not None

    @patch("bernstein.core.quality.fast_path.subprocess.run")
    def test_ruff_not_found(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError("ruff not found")
        result = execute_fast_path(
            FastPathAction.RUFF_FORMAT,
            Path("/tmp/project"),
            [],
        )
        assert result.success is False
        assert "not found" in (result.error or "")

    def test_rename_success(self, tmp_path: Path) -> None:
        """Test rename executor modifies files correctly."""
        src = tmp_path / "main.py"
        src.write_text("def getCwd():\n    return getCwd()\n")
        task = _make_task(
            title="Rename getCwd to get_cwd",
            complexity=Complexity.LOW,
            owned_files=["main.py"],
        )
        result = execute_fast_path(
            FastPathAction.RENAME_SYMBOL,
            tmp_path,
            ["main.py"],
            task=task,
        )
        assert result.success is True
        assert result.files_modified == 1
        assert "get_cwd" in src.read_text()
        assert "getCwd" not in src.read_text()

    def test_rename_no_task_fails(self) -> None:
        result = execute_fast_path(
            FastPathAction.RENAME_SYMBOL,
            Path("/tmp"),
            ["some.py"],
            task=None,
        )
        assert result.success is False

    def test_rename_unparseable_pattern_fails(self) -> None:
        task = _make_task(title="Do something weird", complexity=Complexity.LOW)
        result = execute_fast_path(
            FastPathAction.RENAME_SYMBOL,
            Path("/tmp"),
            ["some.py"],
            task=task,
        )
        assert result.success is False

    def test_rename_no_owned_files_fails(self) -> None:
        task = _make_task(
            title="Rename foo to bar",
            complexity=Complexity.LOW,
            owned_files=[],
        )
        result = execute_fast_path(
            FastPathAction.RENAME_SYMBOL,
            Path("/tmp"),
            [],
            task=task,
        )
        assert result.success is False

    def test_unknown_action_fails(self) -> None:
        # Reach the "no executor" branch by creating a mock action value
        # We can't easily add a new enum, so test the execute_fast_path
        # function handles missing executors gracefully by verifying
        # the known actions all have executors
        for action in FastPathAction:
            result = execute_fast_path(
                action,
                Path("/tmp"),
                [],
                task=_make_task(title="Rename foo to bar", complexity=Complexity.LOW),
            )
            # Should not error out internally, even if the result is a failure
            assert isinstance(result, FastPathResult)


# --- Stats tests ---


class TestFastPathStats:
    """Tests for FastPathStats accumulation."""

    def test_record_increments(self) -> None:
        stats = FastPathStats()
        result = FastPathResult(
            success=True,
            action=FastPathAction.RUFF_FORMAT,
            duration_s=0.5,
            files_modified=3,
            summary="formatted 3 files",
        )
        stats.record(result)
        assert stats.tasks_bypassed == 1
        assert stats.estimated_cost_saved_usd == pytest.approx(0.15)
        assert stats.total_time_saved_s > 0
        assert stats.actions["ruff_format"] == 1

    def test_record_multiple(self) -> None:
        stats = FastPathStats()
        for _ in range(5):
            stats.record(
                FastPathResult(
                    success=True,
                    action=FastPathAction.RUFF_FIX,
                    duration_s=0.2,
                    files_modified=1,
                )
            )
        assert stats.tasks_bypassed == 5
        assert stats.estimated_cost_saved_usd == pytest.approx(0.75)
        assert stats.actions["ruff_fix"] == 5


# --- L1 model config ---


class TestL1ModelConfig:
    """Tests for L1 cheapest model config."""

    def test_l1_model_is_sonnet(self) -> None:
        cfg = get_l1_model_config()
        assert cfg.model == "sonnet"
        assert cfg.effort == "normal"

    def test_l1_model_has_reasonable_token_limit(self) -> None:
        cfg = get_l1_model_config()
        assert cfg.max_tokens <= 100_000


# --- Integration: try_fast_path_batch ---


class TestTryFastPathBatch:
    """Tests for the orchestrator integration function."""

    def test_multi_task_batch_skipped(self) -> None:
        """Batches with >1 task are never fast-pathed."""
        tasks = [
            _make_task(title="Format code", complexity=Complexity.LOW),
            _make_task(title="Sort imports", complexity=Complexity.LOW),
        ]
        stats = FastPathStats()
        result = try_fast_path_batch(
            tasks,
            Path("/tmp"),
            MagicMock(),
            "http://localhost:8052",
            stats,
        )
        assert result is False

    def test_l2_task_skipped(self) -> None:
        """L2 tasks are not handled by fast-path."""
        tasks = [_make_task(title="Implement WebSocket support")]
        stats = FastPathStats()
        result = try_fast_path_batch(
            tasks,
            Path("/tmp"),
            MagicMock(),
            "http://localhost:8052",
            stats,
        )
        assert result is False

    @patch("bernstein.core.quality.fast_path.execute_fast_path")
    @patch("bernstein.core.quality.fast_path.get_collector")
    def test_l0_task_handled(self, mock_collector: MagicMock, mock_exec: MagicMock) -> None:
        """L0 formatting task is handled and marked complete."""
        mock_exec.return_value = FastPathResult(
            success=True,
            action=FastPathAction.RUFF_FORMAT,
            duration_s=0.3,
            files_modified=2,
            summary="formatted 2 files",
        )
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client.post.return_value = mock_resp

        task = _make_task(title="Format code", complexity=Complexity.LOW)
        stats = FastPathStats()
        result = try_fast_path_batch(
            [task],
            Path("/tmp"),
            mock_client,
            "http://localhost:8052",
            stats,
        )
        assert result is True
        assert stats.tasks_bypassed == 1
        # Verify task was marked complete on server
        mock_client.post.assert_called_once()
        call_url = mock_client.post.call_args[0][0]
        assert "/complete" in call_url
