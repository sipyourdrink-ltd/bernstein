"""End-to-end proof that a completed run schedules its bounded test-authoring
follow-up (issue #4462) through the real ``Orchestrator.tick()`` path.

Drives the orchestrator against a mocked task-server HTTP transport (a
"scripted adapter": the transport script decides what the server has and
records what the orchestrator sends it) and a real temporary git repository
standing in for the run's workdir, so ``resolve_run_branch`` /
``diff_name_only`` run against real git rather than a fake. This is the one
integration test the issue's test plan calls for; the decision logic itself
is unit-tested directly in ``tests/unit/test_orchestration_test_followup.py``.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from bernstein.core.models import OrchestratorConfig
from bernstein.core.orchestrator import Orchestrator
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.base import CLIAdapter, SpawnResult

# Ticks any single scenario below is willing to spend. Comfortably above what
# a settled/no-active-holds quiescence needs, far below anything hang-like.
_TICK_BUDGET = 10


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo_with_agent_branch(workdir: Path, *, session_id: str, with_tests: bool) -> None:
    """Build a real repo on ``main`` with one completed agent branch."""
    _git(["init", "-q", "-b", "main", "."], workdir)
    _git(["config", "user.email", "t@example.com"], workdir)
    _git(["config", "user.name", "t"], workdir)
    (workdir / "src").mkdir()
    (workdir / "tests").mkdir()
    (workdir / "src" / "foo.py").write_text("print(1)\n")
    _git(["add", "-A"], workdir)
    _git(["commit", "-q", "-m", "init"], workdir)

    _git(["checkout", "-q", "-b", f"agent/{session_id}"], workdir)
    (workdir / "src" / "foo.py").write_text("print(2)\n")
    if with_tests:
        (workdir / "tests" / "test_foo.py").write_text("def test_foo(): assert True\n")
    _git(["add", "-A"], workdir)
    _git(["commit", "-q", "-m", "agent work"], workdir)
    _git(["checkout", "-q", "main"], workdir)


class _ScriptedServer:
    """Minimal in-memory task-server double: one done task, records POSTs."""

    def __init__(self, *, session_id: str) -> None:
        self.session_id = session_id
        self.task: dict[str, Any] = {
            "id": "t-run",
            "title": "Do the thing",
            "description": "d",
            "role": "backend",
            "status": "done",
            "priority": 1,
            "created_at": time.time() - 60.0,
            "completed_at": time.time() - 1.0,
            "depends_on": [],
            "owned_files": [],
            "assigned_agent": session_id,
            "result_summary": "done",
            "task_type": "standard",
            "retry_count": 0,
            "max_retries": 3,
        }
        self.created_tasks: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if request.method == "GET" and url.path == "/tasks":
            status = url.params.get("status")
            tasks = [dict(self.task)] if status in (None, self.task["status"]) else []
            return httpx.Response(200, json=tasks)
        if request.method == "POST" and url.path == "/tasks":
            body = _json_body(request)
            self.created_tasks.append(body)
            return httpx.Response(201, json={"id": f"t-followup-{len(self.created_tasks)}", **body})
        if request.method == "GET" and url.path == "/orchestrator/holds":
            return httpx.Response(200, json={"holds": []})
        return httpx.Response(200, json={})


def _json_body(request: httpx.Request) -> dict[str, Any]:
    import json

    raw = request.read().decode() or "{}"
    return dict(json.loads(raw))


def _wrap_as_paginated(resp: httpx.Response) -> httpx.Response:
    tasks = resp.json()
    return httpx.Response(200, json={"tasks": tasks, "total": len(tasks)})


def _paginated(inner: httpx.MockTransport) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if request.method == "GET" and url.path == "/tasks" and "limit" in url.params:
            plain_params = {k: v for k, v in url.params.items() if k not in ("limit", "offset")}
            plain_url = url.copy_with(params=plain_params or {})
            plain = httpx.Request(request.method, plain_url, headers=request.headers)
            resp = inner.handle_request(plain)
            return _wrap_as_paginated(resp) if resp.status_code == 200 else resp
        return inner.handle_request(request)

    return httpx.MockTransport(handler)


def _mock_adapter() -> MagicMock:
    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=42, log_path=Path("/tmp/test.log"))
    adapter.is_alive.return_value = False
    adapter.is_rate_limited.return_value = False
    adapter.kill.return_value = None
    adapter.name.return_value = "MockCLI"
    return adapter


def _build_orchestrator(workdir: Path, server: _ScriptedServer, *, test_followup_enabled: bool = True) -> Orchestrator:
    cfg = OrchestratorConfig(
        max_agents=1,
        poll_interval_s=1,
        server_url="http://testserver",
        evolve_mode=False,
        evolution_enabled=False,
        test_followup_enabled=test_followup_enabled,
    )
    templates_dir = workdir / "templates" / "roles"
    templates_dir.mkdir(parents=True)
    spawner = AgentSpawner(_mock_adapter(), templates_dir, workdir, default_model="mock-model")
    client = httpx.Client(transport=_paginated(httpx.MockTransport(server.handler)), base_url="http://testserver")
    return Orchestrator(cfg, spawner, workdir, client=client)


@pytest.fixture
def fast_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the real settle window so the test runs in milliseconds."""
    monkeypatch.setenv("BERNSTEIN_QUIESCENCE_SETTLE_S", "0")


def _run_until_stopped_or_budget(orch: Orchestrator) -> int:
    ticks_used = 0
    for _ in range(_TICK_BUDGET):
        ticks_used += 1
        orch.tick()
        if not orch._running:
            break
    return ticks_used


class TestTestFollowupEndToEnd:
    def test_src_without_tests_schedules_followup_carrying_the_file_list(
        self, tmp_path: Path, fast_settle: None
    ) -> None:
        _init_repo_with_agent_branch(tmp_path, session_id="sess-notests", with_tests=False)
        server = _ScriptedServer(session_id="sess-notests")
        orch = _build_orchestrator(tmp_path, server)
        orch._running = True

        _run_until_stopped_or_budget(orch)

        assert len(server.created_tasks) == 1, "exactly one follow-up task must be scheduled"
        created = server.created_tasks[0]
        assert created["role"] == "qa"
        assert "src/foo.py" in created["description"]
        assert "tests/" in created["description"]
        assert created["metadata"]["origin"] == "test_followup"
        assert created["metadata"]["source_branch"] == "agent/sess-notests"
        assert orch._test_followup_scheduled is True

    def test_run_still_self_stops_after_scheduling_the_followup(self, tmp_path: Path, fast_settle: None) -> None:
        _init_repo_with_agent_branch(tmp_path, session_id="sess-notests", with_tests=False)
        server = _ScriptedServer(session_id="sess-notests")
        orch = _build_orchestrator(tmp_path, server)
        orch._running = True

        _run_until_stopped_or_budget(orch)

        # The mock server's task list never changes across ticks (it always
        # reports the same single done task), so once the latch is set the
        # orchestrator has nothing left to wait for and must still reach its
        # ordinary self-stop rather than idling forever.
        assert orch._running is False
        assert len(server.created_tasks) == 1, "no second follow-up must be scheduled on a later tick"

    def test_no_followup_when_branch_already_has_tests(self, tmp_path: Path, fast_settle: None) -> None:
        _init_repo_with_agent_branch(tmp_path, session_id="sess-withtests", with_tests=True)
        server = _ScriptedServer(session_id="sess-withtests")
        orch = _build_orchestrator(tmp_path, server)
        orch._running = True

        _run_until_stopped_or_budget(orch)

        assert server.created_tasks == []
        assert orch._test_followup_scheduled is False
        assert orch._running is False

    def test_no_followup_when_disabled_via_config(self, tmp_path: Path, fast_settle: None) -> None:
        _init_repo_with_agent_branch(tmp_path, session_id="sess-notests", with_tests=False)
        server = _ScriptedServer(session_id="sess-notests")
        orch = _build_orchestrator(tmp_path, server, test_followup_enabled=False)
        orch._running = True

        _run_until_stopped_or_budget(orch)

        assert server.created_tasks == []
        assert orch._test_followup_scheduled is False
        assert orch._running is False

    def test_no_followup_when_disabled_via_env(
        self, tmp_path: Path, fast_settle: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BERNSTEIN_TEST_FOLLOWUP", "0")
        _init_repo_with_agent_branch(tmp_path, session_id="sess-notests", with_tests=False)
        server = _ScriptedServer(session_id="sess-notests")
        # Config says enabled; the env override must win.
        orch = _build_orchestrator(tmp_path, server, test_followup_enabled=True)
        orch._running = True

        _run_until_stopped_or_budget(orch)

        assert server.created_tasks == []
        assert orch._running is False
