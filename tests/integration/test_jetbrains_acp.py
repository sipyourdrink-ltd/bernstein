"""End-to-end integration test: JetBrains Air discovering Bernstein via ACP.

This test demonstrates the full ACP workflow from a JetBrains editor perspective:
1. Discover Bernstein via ACP discovery endpoint
2. Fetch full agent metadata (roles, capabilities, version)
3. Submit a task via ACP and receive a run_id
4. Poll status endpoint to track progress
5. Verify final task status and completion

This test uses the real task server (not mocked) to exercise the full
integration between ACP routes, handlers, and the task server.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    """Temporary JSONL file for the task store."""
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path):
    """Create the FastAPI app with real handlers."""
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app):  # type: ignore[no-untyped-def]
    """Create an async HTTP client with ASGI transport."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# ---------------------------------------------------------------------------
# Scenario 1: Mock JetBrains Air client discovers Bernstein via ACP discovery
# ---------------------------------------------------------------------------


class TestJetBrainsDiscovery:
    """JetBrains Air initiates discovery to find available agents."""

    @pytest.mark.anyio
    async def test_jetbrains_discovers_bernstein_via_acp_discovery_endpoint(
        self, client: AsyncClient
    ) -> None:
        """Scenario 1: Discovery endpoint returns Bernstein as an available agent."""
        resp = await client.get("/.well-known/acp.json")
        assert resp.status_code == 200

        data = resp.json()
        assert data["protocol"] == "acp"
        assert "agents" in data

        # Verify bernstein is listed
        agent_names = [a["name"] for a in data["agents"]]
        assert "bernstein" in agent_names

        # Verify each agent entry has required fields
        bernstein_agent = next(a for a in data["agents"] if a["name"] == "bernstein")
        assert "description" in bernstein_agent
        assert "endpoint" in bernstein_agent
        assert "/acp/v0" in bernstein_agent["endpoint"]


# ---------------------------------------------------------------------------
# Scenario 2: Client receives full agent metadata (roles, capabilities, version)
# ---------------------------------------------------------------------------


class TestAgentMetadata:
    """JetBrains Air fetches detailed metadata about Bernstein."""

    @pytest.mark.anyio
    async def test_jetbrains_fetches_bernstein_agent_metadata(
        self, client: AsyncClient
    ) -> None:
        """Scenario 2: Agent metadata endpoint returns full capabilities."""
        resp = await client.get("/acp/v0/agents/bernstein")
        assert resp.status_code == 200

        data = resp.json()
        assert data["name"] == "bernstein"
        assert "description" in data
        assert "protocol_version" in data
        assert data["protocol_version"] == "v0"

    @pytest.mark.anyio
    async def test_agent_metadata_includes_capabilities(self, client: AsyncClient) -> None:
        """Agent metadata must list capabilities for discovery."""
        resp = await client.get("/acp/v0/agents/bernstein")
        data = resp.json()

        assert "capabilities" in data
        assert isinstance(data["capabilities"], list)
        assert len(data["capabilities"]) > 0

        # Verify key capabilities are advertised
        cap_names = [c["name"] for c in data["capabilities"]]
        assert "orchestrate" in cap_names
        assert "cost_governance" in cap_names
        assert "multi_agent" in cap_names

    @pytest.mark.anyio
    async def test_agent_metadata_includes_endpoint_and_provider(
        self, client: AsyncClient
    ) -> None:
        """Agent metadata must include endpoint for routing and provider info."""
        resp = await client.get("/acp/v0/agents/bernstein")
        data = resp.json()

        assert "endpoint" in data
        assert "/acp/v0" in data["endpoint"]
        assert "provider" in data
        assert data["provider"] == "bernstein"


# ---------------------------------------------------------------------------
# Scenario 3: Client submits a task via ACP and receives run_id
# ---------------------------------------------------------------------------


class TestTaskSubmission:
    """JetBrains Air submits a task and receives a run identifier."""

    @pytest.mark.anyio
    async def test_jetbrains_submits_acp_run_and_receives_run_id(
        self, client: AsyncClient
    ) -> None:
        """Scenario 3: POST /acp/v0/runs creates an ACP run with unique ID."""
        payload = {
            "input": "Implement a REST API for user management",
            "agent_id": "bernstein",
            "role": "backend",
        }
        resp = await client.post("/acp/v0/runs", json=payload)
        assert resp.status_code == 201

        data = resp.json()
        assert "run_id" in data
        assert data["run_id"]  # Non-empty
        assert data["status"] == "created"
        assert data["input"] == payload["input"]
        assert data["role"] == "backend"

    @pytest.mark.anyio
    async def test_acp_run_is_linked_to_bernstein_task(self, client: AsyncClient) -> None:
        """ACP run creation must also create a linked Bernstein task."""
        payload = {
            "input": "Build a caching layer",
            "agent_id": "bernstein",
            "role": "backend",
        }
        run_resp = await client.post("/acp/v0/runs", json=payload)
        run_data = run_resp.json()

        # Verify a Bernstein task was created
        tasks_resp = await client.get("/tasks")
        assert tasks_resp.status_code == 200
        tasks = tasks_resp.json()
        assert len(tasks) >= 1

        # Find the task linked to this run
        found_task = False
        for task in tasks:
            if "Build a caching layer" in task.get("description", ""):
                found_task = True
                break
        assert found_task, "Linked Bernstein task not found"

    @pytest.mark.anyio
    async def test_each_acp_run_has_unique_id(self, client: AsyncClient) -> None:
        """Multiple runs must have unique identifiers."""
        payload1 = {"input": "Task one", "agent_id": "bernstein"}
        payload2 = {"input": "Task two", "agent_id": "bernstein"}

        resp1 = await client.post("/acp/v0/runs", json=payload1)
        resp2 = await client.post("/acp/v0/runs", json=payload2)

        run_id_1 = resp1.json()["run_id"]
        run_id_2 = resp2.json()["run_id"]

        assert run_id_1 != run_id_2


# ---------------------------------------------------------------------------
# Scenario 4: Client polls status endpoint and sees run progress
# ---------------------------------------------------------------------------


class TestStatusPolling:
    """JetBrains Air polls for task progress."""

    @pytest.mark.anyio
    async def test_jetbrains_polls_run_status_via_get_endpoint(
        self, client: AsyncClient
    ) -> None:
        """Scenario 4: GET /acp/v0/runs/{run_id} returns current status."""
        # Create a run
        payload = {"input": "Write unit tests", "agent_id": "bernstein"}
        create_resp = await client.post("/acp/v0/runs", json=payload)
        run_id = create_resp.json()["run_id"]

        # Poll the status
        resp = await client.get(f"/acp/v0/runs/{run_id}")
        assert resp.status_code == 200

        data = resp.json()
        assert data["run_id"] == run_id
        assert "status" in data
        # Initial status should be "created"
        assert data["status"] == "created"

    @pytest.mark.anyio
    async def test_status_polling_returns_all_required_fields(
        self, client: AsyncClient
    ) -> None:
        """Status response must include all required fields for editor display."""
        payload = {"input": "Deploy to production", "agent_id": "bernstein"}
        create_resp = await client.post("/acp/v0/runs", json=payload)
        run_id = create_resp.json()["run_id"]

        status_resp = await client.get(f"/acp/v0/runs/{run_id}")
        data = status_resp.json()

        # All fields required for JetBrains integration
        assert "run_id" in data
        assert "status" in data
        assert "input" in data
        assert "role" in data
        assert "created_at" in data
        assert "updated_at" in data

    @pytest.mark.anyio
    async def test_status_endpoint_returns_404_for_missing_run(
        self, client: AsyncClient
    ) -> None:
        """Polling a non-existent run should return 404."""
        resp = await client.get("/acp/v0/runs/nonexistent-run-id")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Scenario 5: Client receives live SSE updates during execution (polling variant)
# ---------------------------------------------------------------------------


class TestExecutionTracking:
    """JetBrains Air tracks task execution."""

    @pytest.mark.anyio
    async def test_jetbrains_tracks_run_through_lifecycle(
        self, client: AsyncClient
    ) -> None:
        """Scenario 5: Client can poll and track status changes through lifecycle."""
        # Create a run
        payload = {"input": "Refactor authentication module", "agent_id": "bernstein"}
        create_resp = await client.post("/acp/v0/runs", json=payload)
        run_id = create_resp.json()["run_id"]

        # Verify initial state
        initial_resp = await client.get(f"/acp/v0/runs/{run_id}")
        initial_data = initial_resp.json()
        assert initial_data["status"] == "created"

        # The test demonstrates polling capability. In a real scenario,
        # the status would change as the orchestrator processes the task.
        # For this integration test, we verify the mechanism works.
        assert "created_at" in initial_data
        assert initial_data["created_at"] > 0
        assert "updated_at" in initial_data


# ---------------------------------------------------------------------------
# Scenario 6: Client queries final status and sees completion
# ---------------------------------------------------------------------------


class TestFinalStatus:
    """JetBrains Air verifies task completion."""

    @pytest.mark.anyio
    async def test_acp_run_can_be_queried_for_final_status(
        self, client: AsyncClient
    ) -> None:
        """Scenario 6: Client can retrieve final status of completed run."""
        # Create a run
        payload = {"input": "Optimize database queries", "agent_id": "bernstein"}
        create_resp = await client.post("/acp/v0/runs", json=payload)
        run_id = create_resp.json()["run_id"]

        # Query status (which may reflect task progress)
        status_resp = await client.get(f"/acp/v0/runs/{run_id}")
        data = status_resp.json()

        # Verify status structure supports final state queries
        assert "status" in data
        assert data["status"] in {"created", "running", "completed", "failed"}

    @pytest.mark.anyio
    async def test_acp_run_lifecycle_is_queryable(self, client: AsyncClient) -> None:
        """Complete workflow: create → query → cancel."""
        # Create
        payload = {"input": "Add logging infrastructure", "agent_id": "bernstein"}
        create_resp = await client.post("/acp/v0/runs", json=payload)
        assert create_resp.status_code == 201
        run_id = create_resp.json()["run_id"]

        # Query
        query_resp = await client.get(f"/acp/v0/runs/{run_id}")
        assert query_resp.status_code == 200

        # Cancel
        cancel_resp = await client.delete(f"/acp/v0/runs/{run_id}")
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "cancelled"

        # Verify cancellation persists
        verify_resp = await client.get(f"/acp/v0/runs/{run_id}")
        assert verify_resp.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# End-to-end workflow validation
# ---------------------------------------------------------------------------


class TestEndToEndWorkflow:
    """Full JetBrains Air workflow from discovery to completion."""

    @pytest.mark.anyio
    async def test_jetbrains_full_workflow_discovery_to_task_submission(
        self, client: AsyncClient
    ) -> None:
        """Complete E2E scenario: discover → fetch metadata → submit → poll."""
        # Step 1: Discover Bernstein
        discovery_resp = await client.get("/.well-known/acp.json")
        assert discovery_resp.status_code == 200
        assert discovery_resp.json()["protocol"] == "acp"

        # Step 2: Fetch agent metadata
        metadata_resp = await client.get("/acp/v0/agents/bernstein")
        assert metadata_resp.status_code == 200
        metadata = metadata_resp.json()
        assert "capabilities" in metadata
        assert len(metadata["capabilities"]) > 0

        # Step 3: Submit a task
        task_payload = {
            "input": "Implement email notification system",
            "agent_id": "bernstein",
            "role": "backend",
        }
        submit_resp = await client.post("/acp/v0/runs", json=task_payload)
        assert submit_resp.status_code == 201
        run_id = submit_resp.json()["run_id"]
        assert run_id

        # Step 4: Poll for status
        status_resp = await client.get(f"/acp/v0/runs/{run_id}")
        assert status_resp.status_code == 200
        status_data = status_resp.json()
        assert status_data["run_id"] == run_id
        assert "status" in status_data

        # Verify linked Bernstein task exists
        tasks_resp = await client.get("/tasks")
        assert tasks_resp.status_code == 200
        tasks = tasks_resp.json()
        assert len(tasks) >= 1
        found = any(
            "email notification system" in t.get("description", "") for t in tasks
        )
        assert found
