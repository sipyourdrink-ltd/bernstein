"""Unit tests for POST /tasks dependency cycle validation (#4298)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.testclient import TestClient

from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


def test_post_tasks_accepts_normal_acyclic_chain(tmp_path: Path) -> None:
    """Acyclic dependency chain A -> B -> C creates successfully."""
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")

    with TestClient(app) as client:
        resp_a = client.post(
            "/tasks",
            json={
                "id": "task-a",
                "title": "Task A",
                "description": "First task in chain.",
                "role": "backend",
            },
        )
        assert resp_a.status_code == 201, resp_a.text

        resp_b = client.post(
            "/tasks",
            json={
                "id": "task-b",
                "title": "Task B",
                "description": "Second task in chain.",
                "role": "backend",
                "depends_on": ["task-a"],
            },
        )
        assert resp_b.status_code == 201, resp_b.text

        resp_c = client.post(
            "/tasks",
            json={
                "id": "task-c",
                "title": "Task C",
                "description": "Third task in chain.",
                "role": "backend",
                "depends_on": ["task-b"],
            },
        )
        assert resp_c.status_code == 201, resp_c.text


def test_post_tasks_rejects_two_node_dependency_cycle(tmp_path: Path) -> None:
    """Creating a cycle A -> B -> A is rejected with HTTP 400 naming the cycle."""
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")

    with TestClient(app) as client:
        resp_a = client.post(
            "/tasks",
            json={
                "id": "task-a",
                "title": "Task A",
                "description": "Task A.",
                "role": "backend",
            },
        )
        assert resp_a.status_code == 201

        resp_b = client.post(
            "/tasks",
            json={
                "id": "task-b",
                "title": "Task B",
                "description": "Task B depends on A.",
                "role": "backend",
                "depends_on": ["task-a"],
            },
        )
        assert resp_b.status_code == 201

        # Attempt to create or overwrite Task A with dependency on B -> closing the cycle
        resp_cycle = client.post(
            "/tasks",
            json={
                "id": "task-a",
                "title": "Task A replacement",
                "description": "Task A now depends on B.",
                "role": "backend",
                "depends_on": ["task-b"],
            },
        )
        assert resp_cycle.status_code == 400, resp_cycle.text
        error_detail = resp_cycle.json().get("detail", "")
        assert "task-a" in error_detail
        assert "task-b" in error_detail
        assert "cycle" in error_detail.lower()


def test_post_tasks_rejects_three_node_dependency_cycle(tmp_path: Path) -> None:
    """Creating a 3-node cycle A -> B -> C -> A is rejected with HTTP 400."""
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")

    with TestClient(app) as client:
        client.post(
            "/tasks",
            json={"id": "task-1", "title": "Task 1", "description": "1", "role": "backend"},
        )
        client.post(
            "/tasks",
            json={"id": "task-2", "title": "Task 2", "description": "2", "role": "backend", "depends_on": ["task-1"]},
        )
        client.post(
            "/tasks",
            json={"id": "task-3", "title": "Task 3", "description": "3", "role": "backend", "depends_on": ["task-2"]},
        )

        resp = client.post(
            "/tasks",
            json={
                "id": "task-1",
                "title": "Task 1 cycle",
                "description": "1 depends on 3",
                "role": "backend",
                "depends_on": ["task-3"],
            },
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "task-1" in detail and "task-2" in detail and "task-3" in detail


def test_post_tasks_rejects_self_dependency_cycle(tmp_path: Path) -> None:
    """A task depending on itself is rejected with HTTP 400."""
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")

    with TestClient(app) as client:
        resp = client.post(
            "/tasks",
            json={
                "id": "task-self",
                "title": "Self cycle",
                "description": "Task depending on itself.",
                "role": "backend",
                "depends_on": ["task-self"],
            },
        )
        assert resp.status_code == 400
        assert "task-self" in resp.json()["detail"]
