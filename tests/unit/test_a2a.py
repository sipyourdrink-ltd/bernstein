"""Tests for the A2A (Agent-to-Agent) protocol support."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.a2a import (
    A2AHandler,
    A2ATaskStatus,
    AgentCard,
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
def handler() -> A2AHandler:
    return A2AHandler(server_url="http://localhost:8052")


# ---------------------------------------------------------------------------
# Unit tests — A2AHandler
# ---------------------------------------------------------------------------


class TestAgentCard:
    def test_orchestrator_card_fields(self, handler: A2AHandler) -> None:
        card = handler.orchestrator_card()
        assert card.name == "bernstein-orchestrator"
        assert card.protocol_version == "0.1"
        assert "task_orchestration" in card.capabilities

    def test_card_to_dict(self) -> None:
        card = AgentCard(name="test", description="a test agent", capabilities=["read"])
        d = card.to_dict()
        assert d["name"] == "test"
        assert d["capabilities"] == ["read"]
        assert d["protocol_version"] == "0.1"

    def test_agent_card_from_dict(self) -> None:
        data = {
            "name": "scanner",
            "description": "scans things",
            "capabilities": ["scan", "report"],
            "protocol_version": "0.2",
            "endpoint": "http://localhost:9000",
            "provider": "external",
        }
        card = AgentCard.from_dict(data)
        assert card.name == "scanner"
        assert card.description == "scans things"
        assert card.capabilities == ["scan", "report"]
        assert card.protocol_version == "0.2"
        assert card.endpoint == "http://localhost:9000"
        assert card.provider == "external"

    def test_agent_card_from_dict_defaults(self) -> None:
        card = AgentCard.from_dict({"name": "minimal", "description": "bare"})
        assert card.capabilities == []
        assert card.protocol_version == "0.1"
        assert card.endpoint == ""
        assert card.provider == "bernstein"

    def test_agent_card_roundtrip(self) -> None:
        original = AgentCard(
            name="rt",
            description="roundtrip",
            capabilities=["a", "b"],
            endpoint="http://x",
            provider="test",
        )
        rebuilt = AgentCard.from_dict(original.to_dict())
        assert rebuilt == original

    def test_agent_card_validate_missing_name(self) -> None:
        with pytest.raises(ValueError, match="name"):
            AgentCard.validate({"description": "no name"})

    def test_agent_card_validate_empty_name(self) -> None:
        with pytest.raises(ValueError, match="name"):
            AgentCard.validate({"name": "", "description": "empty name"})

    def test_agent_card_validate_missing_description(self) -> None:
        with pytest.raises(ValueError, match="description"):
            AgentCard.validate({"name": "ok"})

    def test_agent_card_validate_bad_capabilities(self) -> None:
        with pytest.raises(ValueError, match="capabilities"):
            AgentCard.validate({"name": "ok", "description": "d", "capabilities": "not-a-list"})

    def test_agent_card_validate_bad_capability_item(self) -> None:
        with pytest.raises(ValueError, match="capability at index 0"):
            AgentCard.validate({"name": "ok", "description": "d", "capabilities": [123]})

    def test_agent_card_json_schema(self) -> None:
        schema = AgentCard.json_schema()
        assert schema["type"] == "object"
        assert "name" in schema["required"]
        assert "description" in schema["required"]
        assert schema["properties"]["capabilities"]["type"] == "array"


class TestA2AHandler:
    def test_create_task(self, handler: A2AHandler) -> None:
        task = handler.create_task(sender="ext-agent", message="Fix the parser")
        assert task.sender == "ext-agent"
        assert task.message == "Fix the parser"
        assert task.status == A2ATaskStatus.SUBMITTED

    def test_link_and_lookup(self, handler: A2AHandler) -> None:
        task = handler.create_task(sender="ext", message="Do something")
        handler.link_bernstein_task(task.id, "b123")
        assert task.bernstein_task_id == "b123"
        assert handler.get_by_bernstein_id("b123") is task

    def test_link_unknown_raises(self, handler: A2AHandler) -> None:
        with pytest.raises(KeyError):
            handler.link_bernstein_task("nonexistent", "b1")

    def test_sync_status(self, handler: A2AHandler) -> None:
        task = handler.create_task(sender="ext", message="t")
        assert task.status == A2ATaskStatus.SUBMITTED
        new = handler.sync_status(task.id, "in_progress")
        assert new == A2ATaskStatus.WORKING
        assert task.status == A2ATaskStatus.WORKING

    def test_sync_status_done(self, handler: A2AHandler) -> None:
        task = handler.create_task(sender="ext", message="t")
        handler.sync_status(task.id, "done")
        assert task.status == A2ATaskStatus.COMPLETED

    def test_sync_status_unknown_task(self, handler: A2AHandler) -> None:
        with pytest.raises(KeyError):
            handler.sync_status("bad-id", "done")

    def test_add_artifact(self, handler: A2AHandler) -> None:
        task = handler.create_task(sender="ext", message="t")
        art = handler.add_artifact(task.id, name="patch.diff", data="--- a/f\n+++ b/f")
        assert art.name == "patch.diff"
        assert len(task.artifacts) == 1
        assert task.artifacts[0].data == "--- a/f\n+++ b/f"

    def test_add_artifact_unknown_task(self, handler: A2AHandler) -> None:
        with pytest.raises(KeyError):
            handler.add_artifact("bad", name="x", data="y")

    def test_list_tasks_all(self, handler: A2AHandler) -> None:
        handler.create_task(sender="a", message="1")
        handler.create_task(sender="b", message="2")
        assert len(handler.list_tasks()) == 2

    def test_list_tasks_by_sender(self, handler: A2AHandler) -> None:
        handler.create_task(sender="a", message="1")
        handler.create_task(sender="b", message="2")
        handler.create_task(sender="a", message="3")
        assert len(handler.list_tasks(sender="a")) == 2

    def test_status_mapping_roundtrip(self) -> None:
        assert A2AHandler.bernstein_status_for(A2ATaskStatus.COMPLETED) == "done"
        assert A2AHandler.a2a_status_for("done") == A2ATaskStatus.COMPLETED
        assert A2AHandler.a2a_status_for("claimed") == A2ATaskStatus.WORKING
        assert A2AHandler.a2a_status_for("cancelled") == A2ATaskStatus.CANCELED

    def test_task_to_dict(self, handler: A2AHandler) -> None:
        task = handler.create_task(sender="ext", message="test")
        d = task.to_dict()
        assert d["sender"] == "ext"
        assert d["status"] == "submitted"


# ---------------------------------------------------------------------------
# Integration tests — A2A HTTP endpoints
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_agent_card_endpoint(client: AsyncClient) -> None:
    """GET /.well-known/agent.json returns the orchestrator Agent Card."""
    resp = await client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "bernstein-orchestrator"
    assert data["protocol_version"] == "0.1"
    assert "task_orchestration" in data["capabilities"]


@pytest.mark.anyio
async def test_a2a_send_task(client: AsyncClient) -> None:
    """POST /a2a/tasks/send creates an A2A task and a linked Bernstein task."""
    resp = await client.post(
        "/a2a/tasks/send",
        json={
            "sender": "external-scanner",
            "message": "Scan for vulnerabilities",
            "role": "security",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["sender"] == "external-scanner"
    assert data["status"] == "submitted"
    assert data["bernstein_task_id"] is not None
    # Verify the Bernstein task was created.
    bt_resp = await client.get(f"/tasks/{data['bernstein_task_id']}")
    assert bt_resp.status_code == 200
    assert bt_resp.json()["role"] == "security"


@pytest.mark.anyio
async def test_a2a_get_task_syncs_status(client: AsyncClient) -> None:
    """GET /a2a/tasks/{id} syncs status from the Bernstein task."""
    # Create an A2A task.
    send_resp = await client.post(
        "/a2a/tasks/send",
        json={
            "sender": "ext",
            "message": "Do work",
        },
    )
    a2a_id = send_resp.json()["id"]
    bt_id = send_resp.json()["bernstein_task_id"]
    # Claim then complete the underlying Bernstein task.
    await client.post(f"/tasks/{bt_id}/claim")
    await client.post(f"/tasks/{bt_id}/complete", json={"result_summary": "Done"})
    # A2A task should now reflect completed status.
    resp = await client.get(f"/a2a/tasks/{a2a_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


@pytest.mark.anyio
async def test_a2a_get_task_not_found(client: AsyncClient) -> None:
    """GET /a2a/tasks/{id} returns 404 for unknown task."""
    resp = await client.get("/a2a/tasks/nonexistent")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_a2a_add_artifact(client: AsyncClient) -> None:
    """POST /a2a/tasks/{id}/artifacts attaches an artifact."""
    send_resp = await client.post(
        "/a2a/tasks/send",
        json={
            "sender": "ext",
            "message": "Review code",
        },
    )
    a2a_id = send_resp.json()["id"]
    art_resp = await client.post(
        f"/a2a/tasks/{a2a_id}/artifacts",
        json={
            "name": "review.md",
            "data": "# Review\nLooks good.",
            "content_type": "text/markdown",
        },
    )
    assert art_resp.status_code == 201
    data = art_resp.json()
    assert data["name"] == "review.md"
    assert data["content_type"] == "text/markdown"
    # Verify artifact appears on the task.
    task_resp = await client.get(f"/a2a/tasks/{a2a_id}")
    assert len(task_resp.json()["artifacts"]) == 1


@pytest.mark.anyio
async def test_a2a_add_artifact_not_found(client: AsyncClient) -> None:
    """POST /a2a/tasks/{id}/artifacts returns 404 for unknown task."""
    resp = await client.post(
        "/a2a/tasks/nonexistent/artifacts",
        json={
            "name": "x",
            "data": "y",
        },
    )
    assert resp.status_code == 404
