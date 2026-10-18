"""A run must not end while a remote cluster worker still holds one of its tasks.

Seen on a four-step plan served to a remote ``bernstein worker``: the worker
claimed a retry, the central orchestrator read ``open=0 agents=0`` -- the claim
belongs to no local agent -- confirmed quiescence and self-stopped. When the
retry finished, the steps it unblocked went to the remote worker; the last one,
a reviewer step for a role that worker does not poll, stayed ``open`` with no
spawner left to run it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from bernstein.core.orchestration.orchestrator import OrchestratorConfig, has_inflight_task
from bernstein.core.tasks.models import Task
from tests.unit.test_orchestrator import _build_orchestrator, _tasks_response


def _row(task_id: str, status: str, role: str = "backend") -> dict[str, Any]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "description": "d",
        "role": role,
        "priority": 1,
        "scope": "small",
        "complexity": "low",
        "estimated_minutes": 5,
        "status": status,
        "depends_on": [],
        "owned_files": [],
        "assigned_agent": None,
        "result_summary": "done" if status == "done" else None,
        "task_type": "standard",
        "retry_count": 0,
        "max_retries": 3,
    }


def _task(status: str) -> Task:
    return Task(id=f"T-{status}", title="t", description="d", role="backend", status=status)


class TestHasInflightTask:
    @pytest.mark.parametrize("status", ["claimed", "in_progress"])
    def test_a_held_task_is_in_flight(self, status: str) -> None:
        assert has_inflight_task({status: [_task(status)]}) is True

    def test_terminal_and_open_tasks_are_not_in_flight(self) -> None:
        """Open work is the ``open`` count's business; terminal work is finished."""
        assert has_inflight_task({"open": [_task("open")], "done": [_task("done")], "failed": []}) is False

    def test_a_narrower_status_dict_does_not_raise(self) -> None:
        assert has_inflight_task({}) is False
        assert has_inflight_task({"open": []}) is False


@pytest.mark.parametrize("held_status", ["claimed", "in_progress"])
def test_a_remote_claim_keeps_the_run_alive_until_it_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, held_status: str
) -> None:
    monkeypatch.setenv("BERNSTEIN_QUIESCENCE_SETTLE_S", "0")
    finished = _row("aaaa11112222", "done")
    remote = _row("bbbb33334444", held_status)
    board = {"rows": [finished, remote]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/tasks":
            return _tasks_response(request.url, board["rows"])
        return httpx.Response(200, json={})

    cfg = OrchestratorConfig(server_url="http://testserver", evolve_mode=False, evolution_enabled=False)
    orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler), config=cfg)
    orch._running = True  # as run() sets before its tick loop
    marker = tmp_path / ".sdd" / "runtime" / "spawner-deliberate-stop"

    with caplog.at_level(logging.INFO):
        orch.tick()

    assert orch._running is True, "a task held by a remote worker is work still running"
    assert not marker.exists()
    assert "self-stopping" not in caplog.text

    board["rows"] = [finished, dict(remote, status="done", result_summary="done")]
    with caplog.at_level(logging.INFO):
        orch.tick()

    assert orch._running is False, "once the remote claim finishes the run ends as before"
    assert marker.read_text() == "quiescence"
