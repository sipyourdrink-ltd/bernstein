"""Tests for bernstein.core.test_data_gen."""

from __future__ import annotations

import random

import pytest

from bernstein.core.test_data_gen import (
    GeneratedTask,
    TaskTemplate,
    generate_completion_signal,
    generate_plan,
    generate_task,
    generate_task_batch,
)


class TestGenerateTask:
    def test_returns_generated_task(self) -> None:
        task = generate_task()
        assert isinstance(task, GeneratedTask)
        assert len(task.task_id) == 8
        assert task.title
        assert task.goal
        assert task.role in {"coder", "reviewer", "tester", "devops", "architect"}
        assert task.complexity in {"low", "medium", "high"}

    def test_with_template_low_complexity(self) -> None:
        template = TaskTemplate(
            role="coder",
            complexity="low",
            file_count_range=(1, 3),
            has_dependencies=False,
            quality_gates=("tests_pass",),
        )
        task = generate_task(template)
        assert task.complexity == "low"
        assert task.priority >= 7
        assert len(task.scope) <= 3

    def test_with_template_high_complexity(self) -> None:
        template = TaskTemplate(
            role="architect",
            complexity="high",
            file_count_range=(5, 15),
            has_dependencies=True,
            quality_gates=("tests_pass", "lint_clean", "type_check", "security_scan"),
        )
        task = generate_task(template)
        assert task.complexity == "high"
        assert task.priority <= 3
        assert len(task.scope) >= 5

    def test_title_format(self) -> None:
        for _ in range(20):
            task = generate_task()
            parts = task.title.split(" ")
            assert len(parts) >= 2
            assert parts[0] in {
                "Fix",
                "Add",
                "Refactor",
                "Optimize",
                "Implement",
                "Remove",
                "Update",
                "Migrate",
                "Split",
                "Merge",
            }

    def test_scope_paths_format(self) -> None:
        task = generate_task()
        for path in task.scope:
            assert path.startswith("src/bernstein/") or path.startswith("tests/unit/")


class TestGenerateTaskBatch:
    def test_returns_correct_count(self) -> None:
        tasks = generate_task_batch(10)
        assert len(tasks) == 10

    def test_no_duplicate_ids(self) -> None:
        tasks = generate_task_batch(100)
        ids = [t.task_id for t in tasks]
        assert len(ids) == len(set(ids))

    def test_role_filter(self) -> None:
        tasks = generate_task_batch(20, roles=["tester"])
        for task in tasks:
            assert task.role == "tester"

    def test_complexity_filter(self) -> None:
        tasks = generate_task_batch(20, complexity="high")
        for task in tasks:
            assert task.complexity == "high"
            assert task.priority <= 3

    def test_batch_unique_ids_across_calls(self) -> None:
        batch1 = generate_task_batch(50)
        batch2 = generate_task_batch(50)
        ids1 = {t.task_id for t in batch1}
        ids2 = {t.task_id for t in batch2}
        assert ids1.isdisjoint(ids2)


class TestGeneratePlan:
    def test_returns_stages_and_tasks(self) -> None:
        plan = generate_plan(stages=3, tasks_per_stage=4)
        assert "stages" in plan
        assert "tasks" in plan
        assert len(plan["stages"]) == 3
        assert len(plan["tasks"]) == 12  # 3 * 4

    def test_inter_stage_dependencies(self) -> None:
        plan = generate_plan(stages=3, tasks_per_stage=2)
        tasks_by_id = {t["task_id"]: t for t in plan["tasks"]}

        # Stage 1 tasks should have no dependencies
        stage1_ids = plan["stages"][0]["task_ids"]
        for tid in stage1_ids:
            assert tasks_by_id[tid]["dependencies"] == []

        # Stage 2+ tasks may have dependencies on previous stage
        for stage in plan["stages"][1:]:
            for tid in stage["task_ids"]:
                for dep in tasks_by_id[tid]["dependencies"]:
                    # Dependencies must be from an earlier stage
                    assert dep in sum((s["task_ids"] for s in plan["stages"][: stage["stage_id"] - 1]), [])

    def test_all_task_ids_from_stages(self) -> None:
        plan = generate_plan(stages=4, tasks_per_stage=3)
        stage_ids = set()
        for s in plan["stages"]:
            stage_ids.update(s["task_ids"])
        task_ids = {t["task_id"] for t in plan["tasks"]}
        assert stage_ids == task_ids


class TestGenerateCompletionSignal:
    def test_required_keys_present(self) -> None:
        task = generate_task()
        signal = generate_completion_signal(task)
        assert "task_id" in signal
        assert "files_changed" in signal
        assert "tests_pass" in signal
        assert "quality_gates_pass" in signal
        assert "duration_seconds" in signal
        assert "completed_at" in signal

    def test_task_id_matches(self) -> None:
        task = generate_task()
        signal = generate_completion_signal(task)
        assert signal["task_id"] == task.task_id

    def test_files_changed_matches_scope(self) -> None:
        task = generate_task()
        signal = generate_completion_signal(task)
        assert signal["files_changed"] == len(task.scope)

    def test_quality_gates_contain_all_task_gates(self) -> None:
        task = generate_task()
        signal = generate_completion_signal(task)
        for gate in task.quality_gates:
            assert gate in signal["quality_gates_pass"]

    def test_completed_at_is_iso_format(self) -> None:
        import re
        task = generate_task()
        signal = generate_completion_signal(task)
        # ISO 8601 with timezone
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", signal["completed_at"])


class TestFrozenDataclasses:
    def test_task_template_immutable(self) -> None:
        template = TaskTemplate(
            role="coder",
            complexity="low",
            file_count_range=(1, 3),
            has_dependencies=False,
            quality_gates=("tests_pass",),
        )
        with pytest.raises(Exception):  # frozen dataclass
            template.role = "reviewer"  # type: ignore[assignment]

    def test_generated_task_immutable(self) -> None:
        task = generate_task()
        with pytest.raises(Exception):  # frozen dataclass
            task.title = "Hacked"  # type: ignore[assignment]