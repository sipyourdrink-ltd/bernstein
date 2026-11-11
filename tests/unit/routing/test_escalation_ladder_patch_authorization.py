"""Ladder PATCH authority follows the existing task scope of a real JWT (#4855)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from bernstein.core.models import TaskStatus
from fastapi.testclient import TestClient

from bernstein.core.server import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from fastapi import FastAPI

pytestmark = pytest.mark.auth_enabled
_OPERATOR_TOKEN = "ladder-patch-test-operator"
_OPERATOR_HEADERS = {"Authorization": f"Bearer {_OPERATOR_TOKEN}"}
_RESTRICTED_FIELDS = [
    ("cli", "qwen"),
    ("escalation_ladder_step", 2),
    ("escalation_ladder_attempts", 5),
]


@pytest.fixture
def authority_app(tmp_path: Path) -> Iterator[tuple[FastAPI, TestClient, Path]]:
    path = tmp_path / ".sdd" / "runtime" / "tasks.jsonl"
    app = create_app(jsonl_path=path, auth_token=_OPERATOR_TOKEN, plan_mode=True)
    client = TestClient(app)
    yield app, client, path
    client.close()


def _task_id(client: TestClient) -> str:
    response = client.post(
        "/tasks",
        headers=_OPERATOR_HEADERS,
        json={
            "title": "Ladder authority",
            "description": "local fixture",
            "role": "backend",
            "model": "qwen-small",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _agent_headers(app: FastAPI, task_id: str, role: str, *, scoped: bool = True) -> dict[str, str]:
    _, token = app.state.identity_store.create_identity(
        f"ladder-{role}",
        role,
        task_ids=[task_id] if scoped else [],
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("prefix", ["", "/api/v1"])
@pytest.mark.parametrize("role", ["backend", "manager"])
@pytest.mark.parametrize(("field", "value"), _RESTRICTED_FIELDS)
def test_task_scoped_jwt_cannot_rewrite_ladder_authority_or_adapter(
    authority_app: tuple[FastAPI, TestClient, Path],
    prefix: str,
    role: str,
    field: str,
    value: Any,
) -> None:
    app, client, path = authority_app
    task_id = _task_id(client)
    # Exercise the route's real active-task object, rather than a fake store or handler.
    app.state.store.get_task(task_id).status = TaskStatus.IN_PROGRESS
    before = client.get(f"/tasks/{task_id}", headers=_OPERATOR_HEADERS).json()
    persisted = path.read_bytes()

    response = client.patch(
        f"{prefix}/tasks/{task_id}",
        headers=_agent_headers(app, task_id, role),
        json={field: value, "priority": 0},
    )

    assert response.status_code == 403, response.text
    assert field in response.json()["detail"]
    assert client.get(f"/tasks/{task_id}", headers=_OPERATOR_HEADERS).json() == before
    assert path.read_bytes() == persisted, "the whole mixed PATCH must refuse before persistence"


@pytest.mark.parametrize("prefix", ["", "/api/v1"])
@pytest.mark.parametrize("credential", ["operator", "run-root"])
def test_operator_and_unscoped_run_root_can_persist_ladder_hops(
    authority_app: tuple[FastAPI, TestClient, Path],
    prefix: str,
    credential: str,
) -> None:
    app, client, _path = authority_app
    task_id = _task_id(client)
    headers = _OPERATOR_HEADERS if credential == "operator" else _agent_headers(app, task_id, "manager", scoped=False)

    response = client.patch(f"{prefix}/tasks/{task_id}", headers=headers, json=dict(_RESTRICTED_FIELDS))

    assert response.status_code == 200, response.text
    task = client.get(f"/tasks/{task_id}", headers=_OPERATOR_HEADERS).json()
    assert task["cli"] == "qwen"
    assert task["metadata"]["escalation_ladder_step"] == 2
    assert task["metadata"]["escalation_ladder_attempts"] == 5


def test_worker_retains_existing_model_and_priority_patch_permissions(
    authority_app: tuple[FastAPI, TestClient, Path],
) -> None:
    app, client, _path = authority_app
    task_id = _task_id(client)

    response = client.patch(
        f"/tasks/{task_id}",
        headers=_agent_headers(app, task_id, "backend"),
        json={"priority": 0, "model": "qwen-medium"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["model"] == "qwen-medium"
    assert response.json()["priority"] == 0
