"""Tests for the hooks route endpoint (POST /hooks/{session_id})."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path, tmp_path: Path):  # type: ignore[no-untyped-def]
    application = create_app(jsonl_path=jsonl_path)
    # Set workdir so the route can write sidecar files
    application.state.workdir = tmp_path  # type: ignore[attr-defined]
    return application


@pytest.fixture()
def app_with_auth(jsonl_path: Path, tmp_path: Path):  # type: ignore[no-untyped-def]
    application = create_app(jsonl_path=jsonl_path, auth_token="secret-token")
    application.state.workdir = tmp_path  # type: ignore[attr-defined]
    return application


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


@pytest.fixture()
async def auth_client(app_with_auth) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app_with_auth)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


# ---------------------------------------------------------------------------
# POST /hooks/{session_id} — basic functionality
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_post_tool_use_returns_200(client: AsyncClient) -> None:
    """PostToolUse hook event returns 200 with tool_use_logged action."""
    response = await client.post(
        "/hooks/sess-001",
        json={"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": "ls"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["action"] == "tool_use_logged"


@pytest.mark.anyio
async def test_stop_event_returns_200(client: AsyncClient) -> None:
    """Stop hook event returns 200 with stop_marker_written action."""
    response = await client.post(
        "/hooks/sess-002",
        json={"hook_event_name": "Stop"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "stop_marker_written"


@pytest.mark.anyio
async def test_pre_compact_event_returns_200(client: AsyncClient) -> None:
    response = await client.post(
        "/hooks/sess-003",
        json={"hook_event_name": "PreCompact"},
    )
    assert response.status_code == 200
    assert response.json()["action"] == "compaction_logged"


@pytest.mark.anyio
async def test_subagent_start_returns_200(client: AsyncClient) -> None:
    response = await client.post(
        "/hooks/sess-004",
        json={"hook_event_name": "SubagentStart"},
    )
    assert response.status_code == 200
    assert response.json()["action"] == "subagent_start_logged"


@pytest.mark.anyio
async def test_subagent_stop_returns_200(client: AsyncClient) -> None:
    response = await client.post(
        "/hooks/sess-005",
        json={"hook_event_name": "SubagentStop"},
    )
    assert response.status_code == 200
    assert response.json()["action"] == "subagent_stop_logged"


@pytest.mark.anyio
async def test_unknown_event_accepted(client: AsyncClient) -> None:
    """Unknown hook events are accepted and logged gracefully."""
    response = await client.post(
        "/hooks/sess-006",
        json={"hook_event_name": "SomeFutureEvent"},
    )
    assert response.status_code == 200
    assert response.json()["action"] == "event_logged"


@pytest.mark.anyio
async def test_invalid_json_returns_400(client: AsyncClient) -> None:
    """Non-JSON body returns 400."""
    response = await client.post(
        "/hooks/sess-007",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["status"] == "error"


# ---------------------------------------------------------------------------
# Side-effects: sidecar files written
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stop_event_writes_completion_marker(client: AsyncClient, tmp_path: Path) -> None:
    """Stop hook writes a completion marker file."""
    await client.post("/hooks/sess-marker", json={"hook_event_name": "Stop"})
    marker = tmp_path / ".sdd" / "runtime" / "completed" / "sess-marker"
    assert marker.exists()
    assert marker.read_text(encoding="utf-8") == "hook:Stop"


@pytest.mark.anyio
async def test_hook_event_writes_sidecar_jsonl(client: AsyncClient, tmp_path: Path) -> None:
    """Each hook event is appended to the session's JSONL sidecar."""
    await client.post(
        "/hooks/sess-sidecar",
        json={"hook_event_name": "PostToolUse", "tool_name": "Read"},
    )
    sidecar = tmp_path / ".sdd" / "runtime" / "hooks" / "sess-sidecar.jsonl"
    assert sidecar.exists()
    import json

    record = json.loads(sidecar.read_text(encoding="utf-8").strip())
    assert record["event"] == "PostToolUse"
    assert record["tool_name"] == "Read"


@pytest.mark.anyio
async def test_hook_event_touches_heartbeat(client: AsyncClient, tmp_path: Path) -> None:
    """Each hook event updates the heartbeat file."""
    await client.post("/hooks/sess-hbeat", json={"hook_event_name": "PostToolUse", "tool_name": "Bash"})
    hb = tmp_path / ".sdd" / "runtime" / "heartbeats" / "sess-hbeat.json"
    assert hb.exists()
    ts = int(hb.read_text(encoding="utf-8"))
    assert ts > 0


# ---------------------------------------------------------------------------
# Auth bypass: hooks endpoint is public
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_hooks_endpoint_bypasses_auth(auth_client: AsyncClient) -> None:
    """Hooks endpoint works without Authorization header even when auth is enabled."""
    response = await auth_client.post(
        "/hooks/sess-noauth",
        json={"hook_event_name": "Stop"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
