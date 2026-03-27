"""Tests for interactive session streaming (ticket 708).

Covers:
- GET /agents/{session_id}/stream — SSE log tailing
- GET /agents/{session_id}/logs  — log content endpoint
- POST /agents/{session_id}/kill — kill signal file creation
- _read_log_tail helper
- _check_kill_signals orchestrator method
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app, read_log_tail

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_runtime(tmp_path: Path) -> Path:
    """Create a minimal .sdd/runtime/ layout and return the runtime dir."""
    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "tasks.jsonl").write_text("")
    return runtime


@pytest.fixture()
def app(tmp_runtime: Path):
    """FastAPI test app wired to a temp runtime directory."""
    jsonl_path = tmp_runtime / "tasks.jsonl"
    return create_app(jsonl_path=jsonl_path)


# ---------------------------------------------------------------------------
# _read_log_tail helper
# ---------------------------------------------------------------------------


def test_read_log_tail_full(tmp_path: Path) -> None:
    """Reading from offset 0 returns all content."""
    log = tmp_path / "test.log"
    log.write_text("line one\nline two\nline three\n", encoding="utf-8")
    result = read_log_tail(log, 0)
    assert "line one" in result
    assert "line two" in result
    assert "line three" in result


def test_read_log_tail_partial(tmp_path: Path) -> None:
    """Reading from mid-file skips the partial leading line."""
    log = tmp_path / "test.log"
    content = "line one\nline two\nline three\n"
    log.write_bytes(content.encode("utf-8"))
    # Seek into the middle of "line one\n" (offset 5 = mid-word)
    result = read_log_tail(log, 5)
    # Partial first line (e.g., "one\n") should be stripped
    assert "line two" in result
    assert "line three" in result
    # The partial fragment "one" should not appear
    assert not result.startswith("one")


def test_read_log_tail_empty_file(tmp_path: Path) -> None:
    """Empty file returns empty string."""
    log = tmp_path / "empty.log"
    log.write_bytes(b"")
    result = read_log_tail(log, 0)
    assert result == ""


# ---------------------------------------------------------------------------
# GET /agents/{session_id}/logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_logs_not_found(app) -> None:
    """Missing log returns 404."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/agents/nonexistent-session/logs")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_agent_logs_returns_content(app, tmp_runtime: Path) -> None:
    """Existing log file is returned with correct fields."""
    session_id = "backend-abc12345"
    log_path = tmp_runtime / f"{session_id}.log"
    log_path.write_text("hello world\nsecond line\n", encoding="utf-8")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/agents/{session_id}/logs")

    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == session_id
    assert "hello world" in data["content"]
    assert "second line" in data["content"]
    assert data["size"] > 0


@pytest.mark.asyncio
async def test_agent_logs_tail_bytes(app, tmp_runtime: Path) -> None:
    """tail_bytes parameter limits content returned."""
    session_id = "qa-deadbeef"
    log_path = tmp_runtime / f"{session_id}.log"
    # Write 200 bytes worth of content
    log_path.write_text("x" * 100 + "\n" + "y" * 100 + "\n", encoding="utf-8")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Only request last 50 bytes
        resp = await client.get(f"/agents/{session_id}/logs?tail_bytes=50")

    assert resp.status_code == 200
    data = resp.json()
    # Full 200-byte content should NOT be present in tail
    assert len(data["content"]) < 150


# ---------------------------------------------------------------------------
# POST /agents/{session_id}/kill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_writes_signal_file(app, tmp_runtime: Path) -> None:
    """Kill endpoint writes a .kill signal file in the runtime directory."""
    session_id = "manager-cafebabe"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/agents/{session_id}/kill")

    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == session_id
    assert data["kill_requested"] is True

    kill_file = tmp_runtime / f"{session_id}.kill"
    assert kill_file.exists(), "Kill signal file should be created"
    # File should contain a timestamp
    ts = float(kill_file.read_text().strip())
    assert abs(ts - time.time()) < 5.0


@pytest.mark.asyncio
async def test_kill_idempotent(app, tmp_runtime: Path) -> None:
    """Multiple kill requests for the same session all succeed."""
    session_id = "backend-11112222"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r1 = await client.post(f"/agents/{session_id}/kill")
        r2 = await client.post(f"/agents/{session_id}/kill")

    assert r1.status_code == 200
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# GET /agents/{session_id}/stream  (SSE)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_stream_yields_existing_lines(app, tmp_runtime: Path) -> None:
    """Stream endpoint sends existing log content as SSE log events."""
    session_id = "frontend-stream01"
    log_path = tmp_runtime / f"{session_id}.log"
    log_path.write_text("first line\nsecond line\n", encoding="utf-8")

    received_lines: list[str] = []

    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        client.stream("GET", f"/agents/{session_id}/stream") as resp,
    ):
        assert resp.status_code == 200
        async for raw_line in resp.aiter_lines():
            if raw_line.startswith("data: "):
                payload = json.loads(raw_line[6:])
                if "line" in payload:
                    received_lines.append(payload["line"])
                # Stop after collecting initial content
                if len(received_lines) >= 2:
                    break

    assert "first line" in received_lines
    assert "second line" in received_lines


@pytest.mark.asyncio
async def test_agent_stream_no_log_file(app, tmp_runtime: Path) -> None:
    """Stream endpoint works even if no log file exists yet (waits/tails)."""
    from unittest.mock import AsyncMock, patch

    session_id = "missing-session-x"

    # ASGITransport buffers the full response before returning, so the streaming
    # generator must complete before we can read any data.  Patch asyncio.sleep
    # to be a no-op so the idle-tick loop finishes in microseconds instead of ~30s.
    _real_sleep = asyncio.sleep

    async def _instant_sleep(_: float) -> None:
        await _real_sleep(0)

    with patch("asyncio.sleep", new=AsyncMock(side_effect=_instant_sleep)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async with client.stream("GET", f"/agents/{session_id}/stream") as resp:
                assert resp.status_code == 200
                lines = [line async for line in resp.aiter_lines()]

    # The generator should have emitted at least the initial "connected" event
    assert any("connected" in line for line in lines)


# ---------------------------------------------------------------------------
# _check_kill_signals orchestrator integration
# ---------------------------------------------------------------------------


def test_check_kill_signals_removes_file_and_kills(tmp_path: Path) -> None:
    """_check_kill_signals deletes .kill files and terminates agents."""
    from unittest.mock import MagicMock

    from bernstein.core.models import AgentSession, ModelConfig, OrchestratorConfig
    from bernstein.core.orchestrator import Orchestrator, TickResult

    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True)

    session_id = "backend-killme01"
    kill_file = runtime_dir / f"{session_id}.kill"
    kill_file.write_text(str(time.time()))

    mock_spawner = MagicMock()

    session = AgentSession(
        id=session_id,
        role="backend",
        pid=12345,
        status="working",
        model_config=ModelConfig("sonnet", "high"),
    )

    orch = Orchestrator(
        config=OrchestratorConfig(),
        spawner=mock_spawner,
        workdir=tmp_path,
    )
    orch._agents[session_id] = session

    result = TickResult()
    orch._check_kill_signals(result)

    # Kill signal file should be gone
    assert not kill_file.exists()
    # Spawner.kill should have been called
    mock_spawner.kill.assert_called_once_with(session)
    # Session should be in reaped list
    assert session_id in result.reaped


def test_check_kill_signals_ignores_dead_agents(tmp_path: Path) -> None:
    """_check_kill_signals skips sessions that are already dead."""
    from unittest.mock import MagicMock

    from bernstein.core.models import AgentSession, ModelConfig, OrchestratorConfig
    from bernstein.core.orchestrator import Orchestrator, TickResult

    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True)

    session_id = "backend-already-dead"
    kill_file = runtime_dir / f"{session_id}.kill"
    kill_file.write_text(str(time.time()))

    mock_spawner = MagicMock()

    dead_session = AgentSession(
        id=session_id,
        role="backend",
        status="dead",
        model_config=ModelConfig("sonnet", "high"),
    )

    orch = Orchestrator(
        config=OrchestratorConfig(),
        spawner=mock_spawner,
        workdir=tmp_path,
    )
    orch._agents[session_id] = dead_session

    result = TickResult()
    orch._check_kill_signals(result)

    # Kill should NOT be called for dead sessions
    mock_spawner.kill.assert_not_called()
    assert session_id not in result.reaped


def test_check_kill_signals_no_runtime_dir(tmp_path: Path) -> None:
    """_check_kill_signals is a no-op when the runtime directory doesn't exist."""
    from unittest.mock import MagicMock

    from bernstein.core.models import OrchestratorConfig
    from bernstein.core.orchestrator import Orchestrator, TickResult

    mock_spawner = MagicMock()

    orch = Orchestrator(
        config=OrchestratorConfig(),
        spawner=mock_spawner,
        workdir=tmp_path,
    )

    result = TickResult()
    orch._check_kill_signals(result)  # Should not raise

    assert result.reaped == []
