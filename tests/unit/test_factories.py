"""Verify that test data factories produce valid objects."""

from __future__ import annotations

import time

from bernstein.core.tasks.models import (
    AgentSession,
    Complexity,
    ModelConfig,
    Scope,
    Task,
    TaskStatus,
)
from tests.factories import (
    make_completion_data,
    make_session,
    make_task,
    make_task_batch,
)

# ---------------------------------------------------------------------------
# make_task
# ---------------------------------------------------------------------------


class TestMakeTask:
    """Tests for the ``make_task`` factory."""

    def test_returns_task_instance(self) -> None:
        task = make_task()
        assert isinstance(task, Task)

    def test_default_values(self) -> None:
        task = make_task()
        assert task.title == "Implement feature X"
        assert task.status == TaskStatus.OPEN
        assert task.role == "backend"
        assert task.priority == 2
        assert task.scope == Scope.MEDIUM
        assert task.complexity == Complexity.MEDIUM

    def test_unique_ids(self) -> None:
        ids = {make_task().id for _ in range(20)}
        assert len(ids) == 20

    def test_id_is_12_char_hex(self) -> None:
        task = make_task()
        assert len(task.id) == 12
        int(task.id, 16)  # raises ValueError if not hex

    def test_custom_title_and_role(self) -> None:
        task = make_task(title="Fix login bug", role="security")
        assert task.title == "Fix login bug"
        assert task.role == "security"

    def test_custom_status(self) -> None:
        task = make_task(status=TaskStatus.DONE)
        assert task.status == TaskStatus.DONE

    def test_custom_scope_and_complexity(self) -> None:
        task = make_task(scope="large", complexity="high")
        assert task.scope == Scope.LARGE
        assert task.complexity == Complexity.HIGH

    def test_owned_files(self) -> None:
        files = ["src/foo.py", "src/bar.py"]
        task = make_task(owned_files=files)
        assert task.owned_files == files

    def test_depends_on(self) -> None:
        task = make_task(depends_on=["task-1", "task-2"])
        assert task.depends_on == ["task-1", "task-2"]

    def test_override_id(self) -> None:
        task = make_task(id="custom-id-1")
        assert task.id == "custom-id-1"

    def test_override_description(self) -> None:
        task = make_task(description="A very specific description")
        assert task.description == "A very specific description"

    def test_auto_description_includes_title(self) -> None:
        task = make_task(title="Add caching layer")
        assert "Add caching layer" in task.description

    def test_created_at_is_recent(self) -> None:
        before = time.time()
        task = make_task()
        after = time.time()
        assert before <= task.created_at <= after

    def test_extra_overrides_applied(self) -> None:
        task = make_task(model="opus", effort="max")
        assert task.model == "opus"
        assert task.effort == "max"


# ---------------------------------------------------------------------------
# make_session
# ---------------------------------------------------------------------------


class TestMakeSession:
    """Tests for the ``make_session`` factory."""

    def test_returns_agent_session_instance(self) -> None:
        session = make_session()
        assert isinstance(session, AgentSession)

    def test_default_values(self) -> None:
        session = make_session()
        assert session.role == "backend"
        assert session.status == "working"
        assert session.model_config.model == "sonnet"
        assert session.model_config.effort == "high"

    def test_unique_ids(self) -> None:
        ids = {make_session().id for _ in range(20)}
        assert len(ids) == 20

    def test_custom_task_ids(self) -> None:
        session = make_session(task_ids=["t1", "t2"])
        assert session.task_ids == ["t1", "t2"]

    def test_custom_role(self) -> None:
        session = make_session(role="qa")
        assert session.role == "qa"

    def test_custom_model(self) -> None:
        session = make_session(model="opus")
        assert session.model_config.model == "opus"

    def test_override_id(self) -> None:
        session = make_session(id="sess-42")
        assert session.id == "sess-42"

    def test_override_status(self) -> None:
        session = make_session(status="idle")
        assert session.status == "idle"

    def test_override_model_config(self) -> None:
        cfg = ModelConfig(model="haiku", effort="low")
        session = make_session(model_config=cfg)
        assert session.model_config is cfg

    def test_spawn_ts_is_recent(self) -> None:
        before = time.time()
        session = make_session()
        after = time.time()
        assert before <= session.spawn_ts <= after


# ---------------------------------------------------------------------------
# make_task_batch
# ---------------------------------------------------------------------------


class TestMakeTaskBatch:
    """Tests for the ``make_task_batch`` factory."""

    def test_returns_correct_count(self) -> None:
        tasks = make_task_batch(count=7)
        assert len(tasks) == 7

    def test_default_count_is_five(self) -> None:
        tasks = make_task_batch()
        assert len(tasks) == 5

    def test_all_tasks_are_valid(self) -> None:
        for task in make_task_batch():
            assert isinstance(task, Task)
            assert task.id
            assert task.title

    def test_ids_are_unique(self) -> None:
        tasks = make_task_batch(count=10)
        ids = [t.id for t in tasks]
        assert len(set(ids)) == len(ids)

    def test_first_task_has_no_dependencies(self) -> None:
        tasks = make_task_batch()
        assert tasks[0].depends_on == []

    def test_subsequent_tasks_depend_on_predecessor(self) -> None:
        tasks = make_task_batch(count=4)
        for i in range(1, len(tasks)):
            assert tasks[i].depends_on == [tasks[i - 1].id]

    def test_roles_cycle(self) -> None:
        tasks = make_task_batch(count=6, roles=["backend", "qa"])
        assert tasks[0].role == "backend"
        assert tasks[1].role == "qa"
        assert tasks[2].role == "backend"
        assert tasks[3].role == "qa"

    def test_custom_roles(self) -> None:
        tasks = make_task_batch(count=3, roles=["frontend"])
        assert all(t.role == "frontend" for t in tasks)

    def test_count_one(self) -> None:
        tasks = make_task_batch(count=1)
        assert len(tasks) == 1
        assert tasks[0].depends_on == []

    def test_count_zero_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="count must be >= 1"):
            make_task_batch(count=0)


# ---------------------------------------------------------------------------
# make_completion_data
# ---------------------------------------------------------------------------


class TestMakeCompletionData:
    """Tests for the ``make_completion_data`` factory."""

    def test_returns_dict(self) -> None:
        data = make_completion_data()
        assert isinstance(data, dict)

    def test_default_values(self) -> None:
        data = make_completion_data()
        assert data["files_changed"] == 3
        assert data["tests_passing"] is True
        assert data["errors"] == 0

    def test_custom_files_changed(self) -> None:
        data = make_completion_data(files_changed=10)
        assert data["files_changed"] == 10

    def test_failing_tests(self) -> None:
        data = make_completion_data(tests_passing=False)
        assert data["tests_passing"] is False
        assert data["errors"] == 1
        assert "failure" in str(data["result_summary"])

    def test_passing_tests_summary(self) -> None:
        data = make_completion_data(tests_passing=True, files_changed=5)
        summary = str(data["result_summary"])
        assert "5 file(s)" in summary
        assert "passing" in summary

    def test_timestamp_present(self) -> None:
        before = time.time()
        data = make_completion_data()
        after = time.time()
        ts = float(data["timestamp"])  # type: ignore[arg-type]
        assert before <= ts <= after

    def test_has_required_keys(self) -> None:
        data = make_completion_data()
        required = {"files_changed", "tests_passing", "errors", "result_summary", "timestamp"}
        assert required.issubset(data.keys())
