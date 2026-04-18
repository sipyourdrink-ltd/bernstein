"""Tests for the hooks route endpoint (POST /hooks/{session_id})."""

from __future__ import annotations

import json as _json
from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app
from bernstein.core.server.webhook_signatures import sign_hmac_sha256

if TYPE_CHECKING:
    from pathlib import Path


# audit-042/audit-113: hooks endpoint requires HMAC-SHA256 signature over raw
# body, keyed with BERNSTEIN_HOOK_SECRET.  Tests share a fixed secret and sign
# each payload via ``_signed_post``.
_HOOK_SECRET = "test-hook-secret"


def _signed_headers(payload: bytes) -> dict[str, str]:
    """Return request headers with a valid hook signature."""
    return {
        "content-type": "application/json",
        "X-Bernstein-Hook-Signature-256": sign_hmac_sha256(_HOOK_SECRET, payload),
    }


async def _signed_post(
    client: AsyncClient,
    url: str,
    body: dict[str, Any] | bytes,
) -> Any:
    """POST ``body`` to ``url`` with a valid HMAC signature header."""
    raw = body if isinstance(body, bytes) else _json.dumps(body).encode("utf-8")
    return await client.post(url, content=raw, headers=_signed_headers(raw))


@pytest.fixture(autouse=True)
def _set_hook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """All tests in this module run with a known hook secret."""
    monkeypatch.setenv("BERNSTEIN_HOOK_SECRET", _HOOK_SECRET)


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
    response = await _signed_post(
        client,
        "/hooks/sess-001",
        {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": "ls"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["action"] == "tool_use_logged"


@pytest.mark.anyio
async def test_stop_event_returns_200(client: AsyncClient) -> None:
    """Stop hook event returns 200 with stop_marker_written action."""
    response = await _signed_post(
        client,
        "/hooks/sess-002",
        {"hook_event_name": "Stop"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "stop_marker_written"


@pytest.mark.anyio
async def test_pre_compact_event_returns_200(client: AsyncClient) -> None:
    response = await _signed_post(
        client,
        "/hooks/sess-003",
        {"hook_event_name": "PreCompact"},
    )
    assert response.status_code == 200
    assert response.json()["action"] == "compaction_logged"


@pytest.mark.anyio
async def test_subagent_start_returns_200(client: AsyncClient) -> None:
    response = await _signed_post(
        client,
        "/hooks/sess-004",
        {"hook_event_name": "SubagentStart"},
    )
    assert response.status_code == 200
    assert response.json()["action"] == "subagent_start_logged"


@pytest.mark.anyio
async def test_subagent_stop_returns_200(client: AsyncClient) -> None:
    response = await _signed_post(
        client,
        "/hooks/sess-005",
        {"hook_event_name": "SubagentStop"},
    )
    assert response.status_code == 200
    assert response.json()["action"] == "subagent_stop_logged"


@pytest.mark.anyio
async def test_unknown_event_accepted(client: AsyncClient) -> None:
    """Unknown hook events are accepted and logged gracefully."""
    response = await _signed_post(
        client,
        "/hooks/sess-006",
        {"hook_event_name": "SomeFutureEvent"},
    )
    assert response.status_code == 200
    assert response.json()["action"] == "event_logged"


@pytest.mark.anyio
async def test_invalid_json_returns_400(client: AsyncClient) -> None:
    """Non-JSON body returns 400."""
    response = await _signed_post(client, "/hooks/sess-007", b"not json")
    assert response.status_code == 400
    assert response.json()["status"] == "error"


# ---------------------------------------------------------------------------
# Side-effects: sidecar files written
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stop_event_writes_completion_marker(client: AsyncClient, tmp_path: Path) -> None:
    """Stop hook writes a completion marker file."""
    await _signed_post(client, "/hooks/sess-marker", {"hook_event_name": "Stop"})
    marker = tmp_path / ".sdd" / "runtime" / "completed" / "sess-marker"
    assert marker.exists()
    assert marker.read_text(encoding="utf-8") == "hook:Stop"


@pytest.mark.anyio
async def test_hook_event_writes_sidecar_jsonl(client: AsyncClient, tmp_path: Path) -> None:
    """Each hook event is appended to the session's JSONL sidecar."""
    await _signed_post(
        client,
        "/hooks/sess-sidecar",
        {"hook_event_name": "PostToolUse", "tool_name": "Read"},
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
    await _signed_post(
        client,
        "/hooks/sess-hbeat",
        {"hook_event_name": "PostToolUse", "tool_name": "Bash"},
    )
    hb = tmp_path / ".sdd" / "runtime" / "heartbeats" / "sess-hbeat.json"
    assert hb.exists()
    ts = int(hb.read_text(encoding="utf-8"))
    assert ts > 0


# ---------------------------------------------------------------------------
# Auth bypass: hooks endpoint uses HMAC signature instead of bearer auth
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_hooks_endpoint_bypasses_auth(auth_client: AsyncClient) -> None:
    """Hooks endpoint accepts HMAC-signed requests without bearer auth (audit-042/113)."""
    response = await _signed_post(
        auth_client,
        "/hooks/sess-noauth",
        {"hook_event_name": "Stop"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
