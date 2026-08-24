"""Tasks created through POST /tasks never produce a cycle-break warning.

POST /tasks validates cycles at the route level (``DependencyValidator``)
and at the store level (``TaskStore._detect_cycle``), so a board built
entirely through the API is always acyclic. ``TaskGraph._break_declared_cycles``
is only a backstop for legacy board state, and ``topological_order``'s
defensive warning should never fire for freshly created tasks.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from bernstein.core.knowledge.task_graph import TaskGraph
from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path

LOGGER = "bernstein.core.knowledge.task_graph"


def _task_graph_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Records emitted by the task-graph logger, ignoring unrelated loggers."""
    return [r for r in caplog.records if r.name == LOGGER]


def _create(client: TestClient, *, id: str, depends_on: list[str] | None = None) -> None:
    resp = client.post(
        "/tasks",
        json={
            "id": id,
            "title": f"Task {id}",
            "description": f"Task {id}.",
            "role": "backend",
            "depends_on": depends_on or [],
        },
    )
    assert resp.status_code == 201, resp.text


def _graph_from_store(app) -> TaskGraph:
    return TaskGraph(app.state.store.list_tasks())


def test_acyclic_chain_through_post_tasks_produces_clean_graph(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A -> B -> C via POST /tasks yields a clean, fully-ordered graph."""
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")

    with TestClient(app) as client:
        _create(client, id="task-a")
        _create(client, id="task-b", depends_on=["task-a"])
        _create(client, id="task-c", depends_on=["task-b"])

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            graph = _graph_from_store(app)

        assert graph.cycle_breaks == []
        assert graph.topological_order() == ["task-a", "task-b", "task-c"]
        assert _task_graph_warnings(caplog) == []


def test_independent_tasks_produce_clean_graph(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Several dependency-free tasks via POST /tasks yield a clean graph."""
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")

    with TestClient(app) as client:
        for tid in ("task-1", "task-2", "task-3", "task-4"):
            _create(client, id=tid)

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            graph = _graph_from_store(app)

        assert graph.cycle_breaks == []
        assert set(graph.topological_order()) == {"task-1", "task-2", "task-3", "task-4"}
        assert _task_graph_warnings(caplog) == []


def test_rejected_cycle_attempt_leaves_graph_clean(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A rejected cycle attempt leaves only the original acyclic tasks."""
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")

    with TestClient(app) as client:
        _create(client, id="task-a")
        _create(client, id="task-b", depends_on=["task-a"])

        # Attempt to close the cycle A -> B -> A; rejected with HTTP 400.
        resp = client.post(
            "/tasks",
            json={
                "id": "task-a",
                "title": "Task A replacement",
                "description": "Task A now depends on B.",
                "role": "backend",
                "depends_on": ["task-b"],
            },
        )
        assert resp.status_code == 400, resp.text

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            graph = _graph_from_store(app)

        assert graph.cycle_breaks == []
        assert graph.topological_order() == ["task-a", "task-b"]
        assert _task_graph_warnings(caplog) == []
