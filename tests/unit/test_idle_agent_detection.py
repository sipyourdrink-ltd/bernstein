"""Tests for idle agent detection: backlog auto-ingestion and proc reaping."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.models import (
    AgentSession,
    Complexity,
    ModelConfig,
    OrchestratorConfig,
    Scope,
    Task,
    TaskStatus,
    TaskType,
)
from bernstein.core.orchestrator import Orchestrator
from bernstein.core.spawner import AgentSpawner

# --- Helpers ---


def _make_task(
    *,
    id: str = "T-001",
    role: str = "backend",
    title: str = "Test task",
    description: str = "Do the thing.",
) -> Task:
    return Task(
        id=id,
        title=title,
        description=description,
        role=role,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        status=TaskStatus.OPEN,
        task_type=TaskType.STANDARD,
    )


def _mock_adapter(pid: int = 42, proc: object = None) -> CLIAdapter:
    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=pid, log_path=Path("/tmp/test.log"), proc=proc)
    adapter.is_alive.return_value = True
    adapter.kill.return_value = None
    adapter.name.return_value = "MockCLI"
    return adapter


def _tick_transport(extra_posts: list[dict] | None = None) -> httpx.MockTransport:
    """Build a transport that handles standard tick HTTP calls."""
    posts: list[dict] = [] if extra_posts is None else extra_posts

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = request.url.query.decode() if request.url.query else ""
        method = request.method

        if method == "GET" and path.endswith("/tasks") and "status=open" in query:
            return httpx.Response(200, json=[])
        if method == "GET" and path.endswith("/tasks") and "status=done" in query:
            return httpx.Response(200, json=[])
        if method == "GET" and path.endswith("/tasks") and "status=failed" in query:
            return httpx.Response(200, json=[])
        if method == "POST" and path.endswith("/tasks"):
            body = json.loads(request.content)
            posts.append(body)
            return httpx.Response(200, json={"id": f"ingested-{len(posts)}"})
        if method == "POST":
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"detail": f"No mock for {method} {path}?{query}"})

    return httpx.MockTransport(handler)


def _build_orchestrator(tmp_path: Path, client: httpx.Client) -> Orchestrator:
    cfg = OrchestratorConfig(
        max_agents=6,
        poll_interval_s=1,
        heartbeat_timeout_s=120,
        max_tasks_per_agent=3,
        server_url="http://testserver",
        evolution_enabled=False,
    )
    adapter = _mock_adapter()
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True, exist_ok=True)
    spawner = AgentSpawner(adapter, templates_dir, tmp_path)
    return Orchestrator(cfg, spawner, tmp_path, client=client)


# --- Backlog auto-ingestion tests ---


class TestIngestBacklog:
    """Tests for Orchestrator.ingest_backlog() - scans open/ and POSTs to server."""

    def test_ingest_posts_new_backlog_file(self, tmp_path: Path) -> None:
        """A file in backlog/open/ is POSTed to the task server."""
        open_dir = tmp_path / ".sdd" / "backlog" / "open"
        open_dir.mkdir(parents=True)
        (open_dir / "100-fix-the-bug.md").write_text(
            "# 100 -- Fix the bug\n\n**Role:** backend\n**Priority:** 2\n\nFix it.\n"
        )

        posts: list[dict] = []
        client = httpx.Client(transport=_tick_transport(posts), base_url="http://testserver")
        orc = _build_orchestrator(tmp_path, client)

        count = orc.ingest_backlog()

        assert count == 1
        assert len(posts) == 1

    def test_ingest_moves_file_to_claimed(self, tmp_path: Path) -> None:
        """After ingestion, the file is moved from open/ to claimed/."""
        open_dir = tmp_path / ".sdd" / "backlog" / "open"
        open_dir.mkdir(parents=True)
        backlog_file = open_dir / "101-add-feature.md"
        backlog_file.write_text("# 101 -- Add feature\n\n**Role:** backend\n\nDo it.\n")

        posts: list[dict] = []
        client = httpx.Client(transport=_tick_transport(posts), base_url="http://testserver")
        orc = _build_orchestrator(tmp_path, client)

        orc.ingest_backlog()

        assert not backlog_file.exists()
        claimed_dir = tmp_path / ".sdd" / "backlog" / "claimed"
        assert (claimed_dir / "101-add-feature.md").exists()

    def test_ingest_skips_files_already_in_claimed(self, tmp_path: Path) -> None:
        """Files already in claimed/ are not re-ingested."""
        open_dir = tmp_path / ".sdd" / "backlog" / "open"
        claimed_dir = tmp_path / ".sdd" / "backlog" / "claimed"
        open_dir.mkdir(parents=True)
        claimed_dir.mkdir(parents=True)
        content = "# 102 -- Already done\n\n**Role:** backend\n\nAlready claimed.\n"
        (claimed_dir / "102-already-done.md").write_text(content)

        posts: list[dict] = []
        client = httpx.Client(transport=_tick_transport(posts), base_url="http://testserver")
        orc = _build_orchestrator(tmp_path, client)

        count = orc.ingest_backlog()

        assert count == 0
        assert len(posts) == 0

    def test_ingest_returns_zero_when_open_dir_missing(self, tmp_path: Path) -> None:
        """Returns 0 gracefully when backlog/open/ does not exist."""
        posts: list[dict] = []
        client = httpx.Client(transport=_tick_transport(posts), base_url="http://testserver")
        orc = _build_orchestrator(tmp_path, client)

        count = orc.ingest_backlog()

        assert count == 0

    def test_ingest_returns_zero_when_no_files(self, tmp_path: Path) -> None:
        """Returns 0 when backlog/open/ exists but is empty."""
        open_dir = tmp_path / ".sdd" / "backlog" / "open"
        open_dir.mkdir(parents=True)

        posts: list[dict] = []
        client = httpx.Client(transport=_tick_transport(posts), base_url="http://testserver")
        orc = _build_orchestrator(tmp_path, client)

        count = orc.ingest_backlog()

        assert count == 0

    def test_ingest_multiple_files(self, tmp_path: Path) -> None:
        """Multiple backlog files are all ingested."""
        open_dir = tmp_path / ".sdd" / "backlog" / "open"
        open_dir.mkdir(parents=True)
        for i in range(3):
            (open_dir / f"20{i}-task-{i}.md").write_text(f"# 20{i} -- Task {i}\n\n**Role:** backend\n\nDo task {i}.\n")

        posts: list[dict] = []
        client = httpx.Client(transport=_tick_transport(posts), base_url="http://testserver")
        orc = _build_orchestrator(tmp_path, client)

        count = orc.ingest_backlog()

        assert count == 3
        assert len(posts) == 3

    def test_tick_calls_ingest_backlog(self, tmp_path: Path) -> None:
        """Each tick() invokes ingest_backlog to pull in new work."""
        open_dir = tmp_path / ".sdd" / "backlog" / "open"
        open_dir.mkdir(parents=True)

        posts: list[dict] = []
        client = httpx.Client(transport=_tick_transport(posts), base_url="http://testserver")
        orc = _build_orchestrator(tmp_path, client)

        with patch.object(orc, "ingest_backlog", wraps=orc.ingest_backlog) as spy:
            orc.tick()
            spy.assert_called_once()


# --- Spawner proc.terminate tests ---


class TestSpawnerReapCompletedAgent:
    """Tests for AgentSpawner.reap_completed_agent() - clean process termination."""

    def test_reap_calls_proc_terminate(self, tmp_path: Path) -> None:
        """reap_completed_agent calls proc.terminate() on the spawned process."""
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0

        adapter = _mock_adapter(pid=999, proc=mock_proc)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        session = spawner.spawn_for_tasks([_make_task(id="T-001")])
        spawner.reap_completed_agent(session)

        mock_proc.terminate.assert_called_once()

    def test_reap_calls_proc_wait(self, tmp_path: Path) -> None:
        """reap_completed_agent calls proc.wait() after terminate."""
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0

        adapter = _mock_adapter(pid=888, proc=mock_proc)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        session = spawner.spawn_for_tasks([_make_task(id="T-002")])
        spawner.reap_completed_agent(session)

        mock_proc.wait.assert_called_once()

    def test_reap_is_noop_when_no_proc(self, tmp_path: Path) -> None:
        """reap_completed_agent is a no-op when proc is None (pid-only spawn)."""
        adapter = _mock_adapter(pid=777, proc=None)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        session = spawner.spawn_for_tasks([_make_task(id="T-003")])
        # Should not raise
        spawner.reap_completed_agent(session)

    def test_reap_is_noop_for_unknown_session(self, tmp_path: Path) -> None:
        """reap_completed_agent is a no-op for a session with no stored proc."""
        adapter = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        # Build a session that was never spawned through this spawner
        orphan_session = AgentSession(
            id="orphan-session",
            role="backend",
            task_ids=["T-orphan"],
            model_config=ModelConfig(model="sonnet", effort="normal"),
        )
        # Should not raise
        spawner.reap_completed_agent(orphan_session)

    def test_reap_twice_is_idempotent(self, tmp_path: Path) -> None:
        """Calling reap_completed_agent twice is safe (proc is consumed on first call)."""
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0

        adapter = _mock_adapter(pid=666, proc=mock_proc)
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)

        session = spawner.spawn_for_tasks([_make_task(id="T-004")])
        spawner.reap_completed_agent(session)
        spawner.reap_completed_agent(session)  # second call should not raise

        # terminate only called once
        assert mock_proc.terminate.call_count == 1
