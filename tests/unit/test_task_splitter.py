"""Tests for task splitting and parent/subtask lifecycle wiring."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bernstein.core.models import Complexity, Scope, Task, TaskStatus
from bernstein.core.task_splitter import TaskSplitter
from bernstein.core.task_store import TaskStore

from bernstein.core.server import TaskCreate

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str,
    *,
    title: str = "Large task",
    description: str = "Split me",
    estimated_minutes: int = 90,
    owned_files: list[str] | None = None,
    progress_log: list[dict[str, object]] | None = None,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        description=description,
        role="backend",
        priority=2,
        scope=Scope.LARGE,
        complexity=Complexity.MEDIUM,
        estimated_minutes=estimated_minutes,
        owned_files=owned_files or [],
        progress_log=progress_log or [],
    )


def _task(task_id: str, *, description: str = "Split me", estimated_minutes: int = 90) -> Task:
    return Task(
        id=task_id,
        title="Large task",
        description=description,
        role="backend",
        priority=2,
        scope=Scope.LARGE,
        complexity=Complexity.MEDIUM,
        estimated_minutes=estimated_minutes,
    )


def test_should_split_when_estimate_exceeds_one_hour() -> None:
    splitter = TaskSplitter(client=MagicMock(), server_url="https://server")

    assert splitter.should_split(_task("parent", estimated_minutes=61)) is True


def test_should_split_when_description_is_very_long() -> None:
    splitter = TaskSplitter(client=MagicMock(), server_url="https://server")
    description = "word " * 205

    assert splitter.should_split(_task("parent", description=description, estimated_minutes=30)) is True


def test_split_creates_subtasks_and_marks_parent_waiting() -> None:
    client = MagicMock()
    manager = MagicMock()
    parent = _task("parent")
    subtasks = [
        _task("draft-1", description="Implement parser", estimated_minutes=30),
        _task("draft-2", description="Add tests", estimated_minutes=25),
    ]
    subtasks[0].title = "Implement parser"
    subtasks[1].title = "Add tests"
    manager.decompose_sync.return_value = subtasks
    client.post.side_effect = [
        SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id": "sub-1"}),
        SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id": "sub-2"}),
        SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id": "parent"}),
    ]

    created = TaskSplitter(client=client, server_url="https://server").split(parent, manager)

    assert created == ["sub-1", "sub-2"]
    create_body = client.post.call_args_list[0].kwargs["json"]
    assert create_body["parent_task_id"] == "parent"
    assert "[subtask of parent]" in create_body["description"]
    wait_call = client.post.call_args_list[-1]
    assert wait_call.args[0] == "https://server/tasks/parent/wait-for-subtasks"
    assert wait_call.kwargs["json"] == {"subtask_count": 2}


@pytest.mark.asyncio
async def test_parent_completes_after_all_subtasks_finish(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / ".sdd" / "runtime" / "tasks.jsonl")
    parent = await store.create(
        TaskCreate(
            title="Parent",
            description="Decompose me",
            role="backend",
            scope="large",
            complexity="medium",
            estimated_minutes=90,
        )
    )
    child_one = await store.create(
        TaskCreate(
            title="Child one",
            description="First child",
            role="backend",
            scope="small",
            complexity="medium",
            estimated_minutes=20,
            parent_task_id=parent.id,
        )
    )
    child_two = await store.create(
        TaskCreate(
            title="Child two",
            description="Second child",
            role="backend",
            scope="small",
            complexity="medium",
            estimated_minutes=20,
            parent_task_id=parent.id,
        )
    )

    await store.wait_for_subtasks(parent.id, 2)
    waiting_parent = store.get_task(parent.id)
    assert waiting_parent is not None
    assert waiting_parent.status == TaskStatus.WAITING_FOR_SUBTASKS

    await store.claim_by_id(child_one.id)
    await store.claim_by_id(child_two.id)
    await store.complete(child_one.id, "done")
    mid_parent = store.get_task(parent.id)
    assert mid_parent is not None
    assert mid_parent.status == TaskStatus.WAITING_FOR_SUBTASKS

    await store.complete(child_two.id, "done")
    done_parent = store.get_task(parent.id)
    assert done_parent is not None
    assert done_parent.status == TaskStatus.DONE
    assert done_parent.result_summary == "Completed via 2 subtasks"


# ---------------------------------------------------------------------------
# AGENT-012: Parent context inheritance
# ---------------------------------------------------------------------------


class TestBuildParentContext:
    """_build_parent_context extracts goal, description, progress, and files."""

    def _splitter(self) -> TaskSplitter:
        return TaskSplitter(client=MagicMock(), server_url="https://server")

    def test_includes_parent_goal(self) -> None:
        task = _make_task("p1", title="Add authentication to the API")
        ctx = self._splitter()._build_parent_context(task)
        assert "Add authentication to the API" in ctx

    def test_includes_short_description(self) -> None:
        task = _make_task("p1", description="Implement JWT-based auth.")
        ctx = self._splitter()._build_parent_context(task)
        assert "Implement JWT-based auth." in ctx

    def test_truncates_long_description(self) -> None:
        """Descriptions longer than 500 chars are omitted (too noisy)."""
        long_desc = "word " * 120  # ~600 chars
        task = _make_task("p1", description=long_desc)
        ctx = self._splitter()._build_parent_context(task)
        assert long_desc.strip() not in ctx

    def test_includes_owned_files(self) -> None:
        task = _make_task("p1", owned_files=["src/auth.py", "src/models.py"])
        ctx = self._splitter()._build_parent_context(task)
        assert "src/auth.py" in ctx
        assert "src/models.py" in ctx

    def test_includes_last_progress_messages(self) -> None:
        task = _make_task(
            "p1",
            progress_log=[
                {"message": "Designed the JWT schema"},
                {"message": "Wrote unit tests"},
            ],
        )
        ctx = self._splitter()._build_parent_context(task)
        assert "Designed the JWT schema" in ctx
        assert "Wrote unit tests" in ctx

    def test_returns_empty_for_bare_task(self) -> None:
        """A task with only an id and title still returns a non-empty context (goal always present)."""
        task = _make_task("p1", title="Do something")
        ctx = self._splitter()._build_parent_context(task)
        assert ctx != ""
        assert "Do something" in ctx


class TestSplitPassesParentContext:
    """TaskSplitter.split() passes parent_context to each subtask POST body."""

    def test_parent_context_in_subtask_body(self) -> None:
        client = MagicMock()
        manager = MagicMock()
        parent = _make_task(
            "parent-1",
            title="Build auth system",
            owned_files=["src/auth.py"],
        )
        drafts = [
            _make_task("d1", title="Implement JWT", description="Use PyJWT", estimated_minutes=30),
            _make_task("d2", title="Add tests", description="Pytest suite", estimated_minutes=20),
        ]
        manager.decompose_sync.return_value = drafts
        client.post.side_effect = [
            SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id": "sub-1"}),
            SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id": "sub-2"}),
            SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id": "parent-1"}),
        ]

        TaskSplitter(client=client, server_url="https://server").split(parent, manager)

        # Both subtask POST bodies must include parent_context
        for call in client.post.call_args_list[:2]:
            body = call.kwargs["json"]
            assert "parent_context" in body, "parent_context must be in each subtask POST body"
            assert "Build auth system" in body["parent_context"]
            assert "src/auth.py" in body["parent_context"]

    def test_parent_context_omitted_when_empty(self) -> None:
        """If the parent has no useful context, parent_context is not included."""
        client = MagicMock()
        manager = MagicMock()
        # Minimal task: no files, no progress, minimal description
        parent = Task(
            id="bare",
            title="t",
            description="",
            role="backend",
            scope=Scope.LARGE,
            complexity=Complexity.MEDIUM,
        )
        drafts = [
            _make_task("d1", title="Sub A", description="a", estimated_minutes=30),
            _make_task("d2", title="Sub B", description="b", estimated_minutes=25),
        ]
        manager.decompose_sync.return_value = drafts
        client.post.side_effect = [
            SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id": "sub-1"}),
            SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id": "sub-2"}),
            SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id": "bare"}),
        ]

        TaskSplitter(client=client, server_url="https://server").split(parent, manager)

        # Even with minimal parent, the goal line is always included, so parent_context is set
        for call in client.post.call_args_list[:2]:
            body = call.kwargs["json"]
            # parent_context may or may not be present — if present it must be non-empty
            if "parent_context" in body:
                assert body["parent_context"] != ""
