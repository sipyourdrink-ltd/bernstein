"""Tests for the real-time web dashboard endpoints."""
from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    """Return a temporary JSONL path for each test."""
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path):  # type: ignore[no-untyped-def]
    """Create a fresh FastAPI app per test."""
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    """Async HTTP client wired to the test app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# GET /dashboard
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_dashboard_returns_200(client: AsyncClient) -> None:
    """/dashboard returns 200 with HTML content."""
    resp = await client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.anyio
async def test_dashboard_contains_bernstein_title(client: AsyncClient) -> None:
    """/dashboard HTML includes the Bernstein title."""
    resp = await client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.text
    assert "BERNSTEIN" in body


@pytest.mark.anyio
async def test_dashboard_contains_htmx_script(client: AsyncClient) -> None:
    """/dashboard HTML loads HTMX from CDN."""
    resp = await client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.text
    assert "htmx.org" in body


@pytest.mark.anyio
async def test_dashboard_contains_tailwind_script(client: AsyncClient) -> None:
    """/dashboard HTML loads Tailwind CSS from CDN."""
    resp = await client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.text
    assert "tailwindcss" in body


# ---------------------------------------------------------------------------
# GET /dashboard/data  (HTMX partial)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_dashboard_data_returns_200(client: AsyncClient) -> None:
    """/dashboard/data returns 200 with HTML content."""
    resp = await client.get("/dashboard/data")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.anyio
async def test_dashboard_data_contains_task_table(client: AsyncClient) -> None:
    """/dashboard/data HTML contains the task board table."""
    resp = await client.get("/dashboard/data")
    assert resp.status_code == 200
    body = resp.text
    assert "Task Board" in body


@pytest.mark.anyio
async def test_dashboard_data_contains_agent_section(client: AsyncClient) -> None:
    """/dashboard/data HTML contains the active agents section."""
    resp = await client.get("/dashboard/data")
    assert resp.status_code == 200
    body = resp.text
    assert "Active Agents" in body


@pytest.mark.anyio
async def test_dashboard_data_contains_stats(client: AsyncClient) -> None:
    """/dashboard/data HTML contains stats counters."""
    resp = await client.get("/dashboard/data")
    assert resp.status_code == 200
    body = resp.text
    assert "TOTAL" in body
    assert "DONE" in body
    assert "FAILED" in body


@pytest.mark.anyio
async def test_dashboard_data_reflects_tasks(client: AsyncClient) -> None:
    """/dashboard/data shows tasks that have been created."""
    # Create a task first
    await client.post(
        "/tasks",
        json={
            "title": "Dashboard smoke test",
            "description": "Verify dashboard displays this task.",
            "role": "qa",
            "priority": 2,
        },
    )
    resp = await client.get("/dashboard/data")
    assert resp.status_code == 200
    body = resp.text
    assert "Dashboard smoke test" in body


# ---------------------------------------------------------------------------
# GET /events  (SSE)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_events_content_type(client: AsyncClient) -> None:
    """/events returns text/event-stream content-type."""
    # Use a HEAD-equivalent: send the request and read only the response headers.
    # We break out of the stream immediately after checking headers so the test
    # does not block on the infinite SSE generator.
    async with client.stream("GET", "/events") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        # Read one chunk to confirm the stream is live, then exit.
        async for _chunk in resp.aiter_bytes(chunk_size=1):
            break
