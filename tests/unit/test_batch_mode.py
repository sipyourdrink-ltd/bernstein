"""Unit tests for bernstein.core.batch_mode."""

from __future__ import annotations

from bernstein.core.batch_mode import (
    BatchConfig,
    BatchTask,
    combine_tasks_for_batch,
    should_use_batch,
    split_batch_result,
)
from bernstein.core.models import Complexity, Scope, Task, TaskStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    tid: str = "t-1",
    role: str = "backend",
    scope: str = "medium",
    owned_files: list[str] | None = None,
) -> Task:
    return Task(
        id=tid,
        title=f"Task {tid}",
        description=f"Do something for {tid}",
        role=role,
        scope=Scope(scope),
        complexity=Complexity.MEDIUM,
        status=TaskStatus.OPEN,
        owned_files=owned_files or [],
    )


# ---------------------------------------------------------------------------
# should_use_batch
# ---------------------------------------------------------------------------


class TestShouldUseBatch:
    """Decide whether tasks should be batched."""

    def test_five_same_role_large_scope(self) -> None:
        """5 same-role tasks with one large scope -> True."""
        tasks = [_task(f"t-{i}", scope="large" if i == 0 else "medium") for i in range(5)]
        assert should_use_batch(tasks) is True

    def test_five_same_role_many_files(self) -> None:
        """5 same-role tasks with many owned files -> True."""
        tasks = [_task(f"t-{i}", owned_files=[f"src/{i}.py"]) for i in range(5)]
        assert should_use_batch(tasks) is True

    def test_two_tasks_returns_false(self) -> None:
        """Only 2 tasks -> False (below minimum)."""
        tasks = [_task(f"t-{i}") for i in range(2)]
        assert should_use_batch(tasks) is False

    def test_three_tasks_returns_false(self) -> None:
        """Exactly 3 tasks -> False (boundary: need MORE than 3)."""
        tasks = [_task(f"t-{i}", scope="large") for i in range(3)]
        assert should_use_batch(tasks) is False

    def test_mixed_roles_returns_false(self) -> None:
        """Tasks with different roles -> False."""
        tasks = [
            _task("t-0", role="backend", scope="large"),
            _task("t-1", role="backend", scope="large"),
            _task("t-2", role="qa", scope="large"),
            _task("t-3", role="backend", scope="large"),
        ]
        assert should_use_batch(tasks) is False

    def test_four_same_role_small_scope_no_files(self) -> None:
        """4 tasks, same role, small scope, no files -> False."""
        tasks = [_task(f"t-{i}", scope="small") for i in range(4)]
        assert should_use_batch(tasks) is False


# ---------------------------------------------------------------------------
# combine_tasks_for_batch
# ---------------------------------------------------------------------------


class TestCombineTasksForBatch:
    """Merge task descriptions into a single prompt."""

    def test_produces_merged_prompt(self) -> None:
        tasks = [_task(f"t-{i}") for i in range(3)]
        batch = combine_tasks_for_batch(tasks)

        assert isinstance(batch, BatchTask)
        assert batch.original_task_ids == ["t-0", "t-1", "t-2"]
        assert batch.scope == "large"

        # Each task should have a section header
        for t in tasks:
            assert t.id in batch.combined_prompt
            assert t.title in batch.combined_prompt

    def test_includes_owned_files(self) -> None:
        tasks = [_task("t-0", owned_files=["src/foo.py", "src/bar.py"])]
        batch = combine_tasks_for_batch(tasks)
        assert "src/foo.py" in batch.combined_prompt
        assert "src/bar.py" in batch.combined_prompt

    def test_preamble_present(self) -> None:
        tasks = [_task("t-0")]
        batch = combine_tasks_for_batch(tasks)
        assert "## Result:" in batch.combined_prompt  # output format instruction


# ---------------------------------------------------------------------------
# split_batch_result
# ---------------------------------------------------------------------------


class TestSplitBatchResult:
    """Parse combined result back into per-task summaries."""

    def test_parses_sections(self) -> None:
        result_text = "All done.\n## Result: t-0\nFixed the widget.\n## Result: t-1\nUpdated the config.\n"
        results = split_batch_result(result_text, ["t-0", "t-1"])
        assert results["t-0"] == "Fixed the widget."
        assert results["t-1"] == "Updated the config."

    def test_fallback_for_missing_section(self) -> None:
        result_text = "## Result: t-0\nDone."
        results = split_batch_result(result_text, ["t-0", "t-1"])
        assert results["t-0"] == "Done."
        assert "batch" in results["t-1"].lower()  # fallback message

    def test_empty_result(self) -> None:
        results = split_batch_result("", ["t-0", "t-1"])
        assert len(results) == 2
        for v in results.values():
            assert "batch" in v.lower()


# ---------------------------------------------------------------------------
# BatchConfig / BatchTask dataclasses
# ---------------------------------------------------------------------------


class TestDataclasses:
    """Smoke-test dataclass defaults."""

    def test_batch_config_defaults(self) -> None:
        cfg = BatchConfig()
        assert cfg.max_files_per_batch == 50
        assert cfg.timeout_minutes == 60
        assert cfg.model == "sonnet"

    def test_batch_task_defaults(self) -> None:
        bt = BatchTask()
        assert bt.original_task_ids == []
        assert bt.combined_prompt == ""
        assert bt.scope == "large"
