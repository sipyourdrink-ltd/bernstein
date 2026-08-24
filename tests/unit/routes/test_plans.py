"""Route-level tests for the plan approval rendering-hash gate.

The approval routes bind their decision to the SHA-256 rendering hash
computed at plan creation.  A plan modified after it was rendered for
review must be refused (409) before any task is promoted or cancelled,
so the operator never signs off on content they did not see.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bernstein.core.models import TaskStatus
from fastapi.testclient import TestClient

from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

_OPERATOR_TOKEN = "operator-token-for-plan-hash-tests"


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    """The real application with plan mode enabled and an operator token."""
    return create_app(
        jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl",
        auth_token=_OPERATOR_TOKEN,
        plan_mode=True,
    )


def _client(application: FastAPI, index: int) -> TestClient:
    return TestClient(application, client=(f"10.40.{index // 256}.{index % 256}", 43000 + index))


def _operator_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_OPERATOR_TOKEN}"}


def _create_planned_task(application: FastAPI, index: int, title: str) -> str:
    """Create a task and move it to PLANNED, returning its id."""
    response = _client(application, index).post(
        "/tasks",
        headers=_operator_headers(),
        json={"title": title, "description": title, "role": "backend"},
    )
    assert response.status_code == 201, response.text
    task_id = str(response.json()["id"])
    store = application.state.store
    task = store.get_task(task_id)
    assert task is not None
    store._index_remove(task)  # pyright: ignore[reportPrivateUsage]
    task.status = TaskStatus.PLANNED
    store._index_add(task)  # pyright: ignore[reportPrivateUsage]
    return task_id


def _save_plan(application: FastAPI, task_id: str) -> str:
    """Persist a plan over *task_id* and return its id."""
    from bernstein.core.security.plan_approval import create_plan

    task = application.state.store.get_task(task_id)
    assert task is not None
    plan = create_plan("hash gate goal", [task])
    application.state.plan_store.save_plan(plan)
    return str(plan.id)


def test_approve_refuses_a_plan_modified_after_review(app: FastAPI) -> None:
    """A tampered plan is refused before any task is promoted."""
    task_id = _create_planned_task(app, 0, "hash-gate-task")
    plan_id = _save_plan(app, task_id)

    # Tamper with the plan after it was rendered for review.
    plan = app.state.plan_store.get_plan(plan_id)
    assert plan is not None
    plan.goal = "tampered goal"

    response = _client(app, 1).post(
        f"/plans/{plan_id}/approve",
        headers=_operator_headers(),
        json={"reason": "reviewed"},
    )

    assert response.status_code == 409, response.text
    assert "rendering hash mismatch" in response.json()["detail"]
    # No task was promoted and the plan is still pending.
    assert app.state.store.get_task(task_id).status is TaskStatus.PLANNED
    assert app.state.plan_store.get_plan(plan_id).status.value == "pending"


def test_reject_refuses_a_plan_modified_after_review(app: FastAPI) -> None:
    """A tampered plan is refused before any task is cancelled."""
    task_id = _create_planned_task(app, 2, "hash-gate-reject")
    plan_id = _save_plan(app, task_id)

    plan = app.state.plan_store.get_plan(plan_id)
    assert plan is not None
    plan.goal = "tampered goal"

    response = _client(app, 3).post(
        f"/plans/{plan_id}/reject",
        headers=_operator_headers(),
        json={"reason": "out of scope"},
    )

    assert response.status_code == 409, response.text
    assert "rendering hash mismatch" in response.json()["detail"]
    assert app.state.store.get_task(task_id).status is TaskStatus.PLANNED
    assert app.state.plan_store.get_plan(plan_id).status.value == "pending"


def test_approve_succeeds_for_an_unchanged_plan(app: FastAPI) -> None:
    """An unchanged plan approves normally and promotes its task."""
    task_id = _create_planned_task(app, 4, "hash-gate-ok")
    plan_id = _save_plan(app, task_id)

    response = _client(app, 5).post(
        f"/plans/{plan_id}/approve",
        headers=_operator_headers(),
        json={"reason": "reviewed"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["promoted_task_ids"] == [task_id]
    assert app.state.store.get_task(task_id).status is TaskStatus.OPEN
    assert app.state.plan_store.get_plan(plan_id).status.value == "approved"
