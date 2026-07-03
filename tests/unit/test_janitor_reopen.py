"""Tests for bounded janitor reopen-on-FAIL (commit-before-complete follow-up).

Covers the janitor-verdict action wiring in ``task_lifecycle``:

* done task + janitor FAIL -> reopened once (same id), logged
* FAIL with exhausted reopen budget -> permanent fail, logged
* janitor PASS -> stays done, no state change

Plus the ``TaskStore.reopen`` DONE -> OPEN transition itself.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bernstein.core.tasks.models import Task, TaskStatus
from bernstein.core.tasks.task_lifecycle import _apply_janitor_verdict_action
from bernstein.core.tasks.task_store import TaskStore


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    """Records POSTs made by the orchestrator janitor-verdict action."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, json: dict[str, Any] | None = None, **_kwargs: Any) -> _FakeResponse:
        self.posts.append((url, json or {}))
        return _FakeResponse()


def _make_task(task_id: str = "task123", reopen_count: int = 0) -> Task:
    task = Task(
        id=task_id,
        title="Implement hello subcommand",
        description="Add a hello subcommand to cli.py",
        role="backend",
        status=TaskStatus.DONE,
    )
    if reopen_count:
        task.metadata["janitor_reopen_count"] = reopen_count
    return task


def _make_orch(client: _FakeClient) -> SimpleNamespace:
    return SimpleNamespace(
        _config=SimpleNamespace(server_url="http://127.0.0.1:8052"),
        _client=client,
        _processed_done_tasks={"task123": None},
    )


def test_janitor_fail_reopens_task_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    """First janitor FAIL on a done task reopens it (same id) and logs cycle 1/2."""
    client = _FakeClient()
    orch = _make_orch(client)
    task = _make_task()

    with caplog.at_level(logging.INFO, logger="bernstein.core.tasks.task_lifecycle"):
        _apply_janitor_verdict_action(orch, task, janitor_passed=False)

    assert len(client.posts) == 1
    url, body = client.posts[0]
    assert url.endswith("/tasks/task123/reopen")
    assert "janitor verification failed" in body["reason"]
    # Processed marker cleared so the re-completed task gets re-verified.
    assert "task123" not in orch._processed_done_tasks
    assert "janitor_verdict_action: task=task123 verdict=FAIL action=reopen cycle=1/2" in caplog.text


def test_janitor_fail_budget_exhausted_permanent_fails(caplog: pytest.LogCaptureFixture) -> None:
    """Janitor FAIL after the reopen budget is spent permanently fails the task."""
    client = _FakeClient()
    orch = _make_orch(client)
    task = _make_task(reopen_count=2)  # default budget of 2 already spent

    with caplog.at_level(logging.INFO, logger="bernstein.core.tasks.task_lifecycle"):
        _apply_janitor_verdict_action(orch, task, janitor_passed=False)

    assert len(client.posts) == 1
    url, body = client.posts[0]
    assert url.endswith("/tasks/task123/fail")
    assert "reopen_budget_exhausted" in body["reason"]
    assert (
        "janitor_verdict_action: task=task123 verdict=FAIL action=permanent_fail reason=reopen_budget_exhausted"
    ) in caplog.text


def test_second_fail_with_budget_one_permanent_fails(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With BERNSTEIN_JANITOR_REOPEN_MAX=1: first FAIL reopens, second FAIL is permanent."""
    monkeypatch.setenv("BERNSTEIN_JANITOR_REOPEN_MAX", "1")
    client = _FakeClient()
    orch = _make_orch(client)
    task = _make_task()

    with caplog.at_level(logging.INFO, logger="bernstein.core.tasks.task_lifecycle"):
        # First FAIL -> reopen cycle 1/1.
        _apply_janitor_verdict_action(orch, task, janitor_passed=False)
        # Simulate the server-side counter bump from the reopen.
        task.metadata["janitor_reopen_count"] = 1
        # Second FAIL -> budget exhausted -> permanent fail.
        _apply_janitor_verdict_action(orch, task, janitor_passed=False)

    assert client.posts[0][0].endswith("/tasks/task123/reopen")
    assert client.posts[1][0].endswith("/tasks/task123/fail")
    assert "action=reopen cycle=1/1" in caplog.text
    assert "action=permanent_fail reason=reopen_budget_exhausted" in caplog.text


def test_janitor_pass_leaves_task_done() -> None:
    """Janitor PASS makes no state change and no HTTP call."""
    client = _FakeClient()
    orch = _make_orch(client)
    task = _make_task()

    _apply_janitor_verdict_action(orch, task, janitor_passed=True)

    assert client.posts == []
    assert task.status is TaskStatus.DONE
    assert "task123" in orch._processed_done_tasks


def test_store_reopen_done_task_keeps_id_and_increments_counter(tmp_path: Path) -> None:
    """TaskStore.reopen transitions DONE -> OPEN under the same task id."""

    async def _run() -> None:
        store = TaskStore(jsonl_path=tmp_path / "tasks.jsonl", archive_path=tmp_path / "archive.jsonl")
        task = _make_task()
        task.status = TaskStatus.DONE
        store._tasks[task.id] = task
        store._index_add(task)

        reopened = await store.reopen("task123", "janitor verification failed")

        assert reopened.id == "task123"
        assert reopened.status is TaskStatus.OPEN
        assert reopened.metadata["janitor_reopen_count"] == 1
        assert reopened.claimed_by_session is None
        assert reopened.result_summary is None

    asyncio.run(_run())


def test_store_reopen_rejects_non_done_task(tmp_path: Path) -> None:
    """Reopening a task that is not DONE raises IllegalTransitionError."""
    from bernstein.core.tasks.lifecycle import IllegalTransitionError

    async def _run() -> None:
        store = TaskStore(jsonl_path=tmp_path / "tasks.jsonl", archive_path=tmp_path / "archive.jsonl")
        task = _make_task()
        task.status = TaskStatus.OPEN
        store._tasks[task.id] = task
        store._index_add(task)

        with pytest.raises(IllegalTransitionError):
            await store.reopen("task123", "should not work")

    asyncio.run(_run())
