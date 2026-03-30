"""Tests for the ACP (Agent Communication Protocol) bridge.

ACP is the BeeAI Agent Communication Protocol — HTTP+SSE based,
makes Bernstein auto-discoverable in JetBrains (Air), Zed, Neovim, Emacs.

Endpoints implemented:
  GET  /.well-known/acp.json          — discovery document
  GET  /acp/v0/agents                 — list agents/capabilities
  GET  /acp/v0/agents/bernstein       — agent metadata
  POST /acp/v0/runs                   — create a run (→ Bernstein task)
  GET  /acp/v0/runs/{run_id}          — run status
  DELETE /acp/v0/runs/{run_id}        — cancel a run
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.acp import (
    ACPHandler,
    ACPRun,
    ACPRunStatus,
)
from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path):
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture()
def handler() -> ACPHandler:
    return ACPHandler(server_url="http://localhost:8052")


# ---------------------------------------------------------------------------
# Unit tests — ACPHandler
# ---------------------------------------------------------------------------


class TestACPHandler:
    def test_agent_metadata_has_required_fields(self, handler: ACPHandler) -> None:
        meta = handler.agent_metadata()
        assert meta["name"] == "bernstein"
        assert "description" in meta
        assert "capabilities" in meta
        assert isinstance(meta["capabilities"], list)
        assert len(meta["capabilities"]) > 0

    def test_agent_capabilities_include_orchestration(self, handler: ACPHandler) -> None:
        meta = handler.agent_metadata()
        cap_names = [c["name"] for c in meta["capabilities"]]
        assert "orchestrate" in cap_names

    def test_agent_capabilities_include_cost_governance(self, handler: ACPHandler) -> None:
        meta = handler.agent_metadata()
        cap_names = [c["name"] for c in meta["capabilities"]]
        assert "cost_governance" in cap_names

    def test_create_run_returns_acp_run(self, handler: ACPHandler) -> None:
        run = handler.create_run(input_text="Build a REST API", role="backend")
        assert isinstance(run, ACPRun)
        assert run.status == ACPRunStatus.CREATED
        assert run.input_text == "Build a REST API"
        assert run.role == "backend"
        assert run.id  # non-empty

    def test_run_id_is_unique(self, handler: ACPHandler) -> None:
        run1 = handler.create_run(input_text="task one")
        run2 = handler.create_run(input_text="task two")
        assert run1.id != run2.id

    def test_get_run_returns_none_for_missing(self, handler: ACPHandler) -> None:
        assert handler.get_run("nonexistent") is None

    def test_get_run_returns_created_run(self, handler: ACPHandler) -> None:
        run = handler.create_run(input_text="some task")
        found = handler.get_run(run.id)
        assert found is not None
        assert found.id == run.id

    def test_link_bernstein_task(self, handler: ACPHandler) -> None:
        run = handler.create_run(input_text="link test")
        handler.link_bernstein_task(run.id, "btask-123")
        found = handler.get_run(run.id)
        assert found is not None
        assert found.bernstein_task_id == "btask-123"

    def test_link_bernstein_task_raises_for_missing_run(self, handler: ACPHandler) -> None:
        with pytest.raises(KeyError):
            handler.link_bernstein_task("missing-run-id", "btask-xyz")

    def test_cancel_run(self, handler: ACPHandler) -> None:
        run = handler.create_run(input_text="cancel me")
        handler.cancel_run(run.id)
        found = handler.get_run(run.id)
        assert found is not None
        assert found.status == ACPRunStatus.CANCELLED

    def test_cancel_run_raises_for_missing(self, handler: ACPHandler) -> None:
        with pytest.raises(KeyError):
            handler.cancel_run("ghost-run")

    def test_sync_status_from_bernstein(self, handler: ACPHandler) -> None:
        run = handler.create_run(input_text="sync test")
        handler.sync_status(run.id, "in_progress")
        found = handler.get_run(run.id)
        assert found is not None
        assert found.status == ACPRunStatus.RUNNING

    def test_sync_status_done(self, handler: ACPHandler) -> None:
        run = handler.create_run(input_text="sync done")
        handler.sync_status(run.id, "done")
        found = handler.get_run(run.id)
        assert found is not None
        assert found.status == ACPRunStatus.COMPLETED

    def test_sync_status_failed(self, handler: ACPHandler) -> None:
        run = handler.create_run(input_text="sync fail")
        handler.sync_status(run.id, "failed")
        found = handler.get_run(run.id)
        assert found is not None
        assert found.status == ACPRunStatus.FAILED

    def test_list_runs_empty(self, handler: ACPHandler) -> None:
        assert handler.list_runs() == []

    def test_list_runs_returns_all(self, handler: ACPHandler) -> None:
        handler.create_run(input_text="run one")
        handler.create_run(input_text="run two")
        assert len(handler.list_runs()) == 2

    def test_discovery_doc(self, handler: ACPHandler) -> None:
        doc = handler.discovery_doc()
        assert doc["protocol"] == "acp"
        assert "agents" in doc
        assert len(doc["agents"]) > 0
        # Each agent entry must include the endpoint
        for agent in doc["agents"]:
            assert "name" in agent
            assert "endpoint" in agent


# ---------------------------------------------------------------------------
# ACPRunStatus mapping
# ---------------------------------------------------------------------------


class TestACPRunStatus:
    def test_bernstein_open_maps_to_created(self) -> None:
        assert ACPRunStatus.from_bernstein("open") == ACPRunStatus.CREATED

    def test_bernstein_claimed_maps_to_running(self) -> None:
        assert ACPRunStatus.from_bernstein("claimed") == ACPRunStatus.RUNNING

    def test_bernstein_in_progress_maps_to_running(self) -> None:
        assert ACPRunStatus.from_bernstein("in_progress") == ACPRunStatus.RUNNING

    def test_bernstein_done_maps_to_completed(self) -> None:
        assert ACPRunStatus.from_bernstein("done") == ACPRunStatus.COMPLETED

    def test_bernstein_failed_maps_to_failed(self) -> None:
        assert ACPRunStatus.from_bernstein("failed") == ACPRunStatus.FAILED

    def test_bernstein_cancelled_maps_to_cancelled(self) -> None:
        assert ACPRunStatus.from_bernstein("cancelled") == ACPRunStatus.CANCELLED

    def test_unknown_status_maps_to_created(self) -> None:
        assert ACPRunStatus.from_bernstein("unknown_xyz") == ACPRunStatus.CREATED


# ---------------------------------------------------------------------------
# HTTP integration tests
# ---------------------------------------------------------------------------


class TestACPDiscovery:
    @pytest.mark.anyio
    async def test_well_known_acp_json_returns_200(self, client: AsyncClient) -> None:
        resp = await client.get("/.well-known/acp.json")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_well_known_acp_json_contains_protocol(self, client: AsyncClient) -> None:
        resp = await client.get("/.well-known/acp.json")
        data = resp.json()
        assert data["protocol"] == "acp"

    @pytest.mark.anyio
    async def test_well_known_acp_json_lists_bernstein_agent(self, client: AsyncClient) -> None:
        resp = await client.get("/.well-known/acp.json")
        data = resp.json()
        agent_names = [a["name"] for a in data["agents"]]
        assert "bernstein" in agent_names


class TestACPAgents:
    @pytest.mark.anyio
    async def test_list_agents_returns_200(self, client: AsyncClient) -> None:
        resp = await client.get("/acp/v0/agents")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_list_agents_returns_bernstein(self, client: AsyncClient) -> None:
        resp = await client.get("/acp/v0/agents")
        data = resp.json()
        assert isinstance(data, list)
        names = [a["name"] for a in data]
        assert "bernstein" in names

    @pytest.mark.anyio
    async def test_get_agent_bernstein_returns_200(self, client: AsyncClient) -> None:
        resp = await client.get("/acp/v0/agents/bernstein")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_get_agent_bernstein_has_capabilities(self, client: AsyncClient) -> None:
        resp = await client.get("/acp/v0/agents/bernstein")
        data = resp.json()
        assert "capabilities" in data
        assert isinstance(data["capabilities"], list)
        cap_names = [c["name"] for c in data["capabilities"]]
        assert "orchestrate" in cap_names

    @pytest.mark.anyio
    async def test_get_unknown_agent_returns_404(self, client: AsyncClient) -> None:
        resp = await client.get("/acp/v0/agents/does_not_exist")
        assert resp.status_code == 404


class TestACPRuns:
    @pytest.mark.anyio
    async def test_create_run_returns_201(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/acp/v0/runs",
            json={"input": "Write a hello world function", "agent_id": "bernstein"},
        )
        assert resp.status_code == 201

    @pytest.mark.anyio
    async def test_create_run_returns_run_id(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/acp/v0/runs",
            json={"input": "Write a hello world function", "agent_id": "bernstein"},
        )
        data = resp.json()
        assert "run_id" in data
        assert data["run_id"]

    @pytest.mark.anyio
    async def test_create_run_status_is_created(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/acp/v0/runs",
            json={"input": "Write tests", "agent_id": "bernstein"},
        )
        data = resp.json()
        assert data["status"] == "created"

    @pytest.mark.anyio
    async def test_create_run_also_creates_bernstein_task(self, client: AsyncClient) -> None:
        await client.post(
            "/acp/v0/runs",
            json={"input": "Add authentication", "agent_id": "bernstein", "role": "backend"},
        )
        # Verify a Bernstein task was created
        tasks_resp = await client.get("/tasks")
        assert tasks_resp.status_code == 200
        tasks = tasks_resp.json()
        assert len(tasks) == 1
        assert "Add authentication" in tasks[0]["description"]

    @pytest.mark.anyio
    async def test_get_run_returns_200(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/acp/v0/runs",
            json={"input": "some task", "agent_id": "bernstein"},
        )
        run_id = create_resp.json()["run_id"]
        resp = await client.get(f"/acp/v0/runs/{run_id}")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_get_run_returns_status_and_id(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/acp/v0/runs",
            json={"input": "some task", "agent_id": "bernstein"},
        )
        run_id = create_resp.json()["run_id"]
        resp = await client.get(f"/acp/v0/runs/{run_id}")
        data = resp.json()
        assert data["run_id"] == run_id
        assert "status" in data

    @pytest.mark.anyio
    async def test_get_nonexistent_run_returns_404(self, client: AsyncClient) -> None:
        resp = await client.get("/acp/v0/runs/does-not-exist")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_cancel_run_returns_200(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/acp/v0/runs",
            json={"input": "cancel me", "agent_id": "bernstein"},
        )
        run_id = create_resp.json()["run_id"]
        resp = await client.delete(f"/acp/v0/runs/{run_id}")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_cancel_run_status_becomes_cancelled(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/acp/v0/runs",
            json={"input": "cancel this run", "agent_id": "bernstein"},
        )
        run_id = create_resp.json()["run_id"]
        await client.delete(f"/acp/v0/runs/{run_id}")
        get_resp = await client.get(f"/acp/v0/runs/{run_id}")
        assert get_resp.json()["status"] == "cancelled"

    @pytest.mark.anyio
    async def test_cancel_nonexistent_run_returns_404(self, client: AsyncClient) -> None:
        resp = await client.delete("/acp/v0/runs/ghost-run-id")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_acp_endpoints_are_public(self, client: AsyncClient) -> None:
        """ACP discovery must be accessible without auth."""
        resp = await client.get("/.well-known/acp.json")
        assert resp.status_code == 200
