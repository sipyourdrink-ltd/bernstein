"""Tests for the Bernstein web dashboard."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.cli.dashboard import (
    AgentWidget,
    _build_runtime_subtitle,
    _format_gate_report_lines,
    _format_relative_age,
    _gate_status_color,
    _mini_cost_sparkline,
    _summarize_agent_errors,
    _task_retry_count,
)
from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path

TASK_PAYLOAD = {
    "title": "Implement parser",
    "description": "Write the YAML parser module",
    "role": "backend",
    "priority": 2,
}


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
    transport = ASGITransport(app=app)  # pyright: ignore[reportUnknownArgumentType]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


# -- GET /dashboard ---------------------------------------------------------


@pytest.mark.anyio
async def test_dashboard_returns_200(client: AsyncClient) -> None:
    """GET /dashboard returns 200 with HTML content."""
    resp = await client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.anyio
async def test_dashboard_contains_key_elements(client: AsyncClient) -> None:
    """Dashboard HTML contains the task table, agent section, and stats bar."""
    resp = await client.get("/dashboard")
    html = resp.text
    assert "Bernstein" in html
    assert "task" in html.lower()
    assert "agent" in html.lower()
    assert "cost" in html.lower() or "stat" in html.lower()


@pytest.mark.anyio
async def test_dashboard_contains_script(client: AsyncClient) -> None:
    """Dashboard HTML includes JavaScript for auto-refresh."""
    resp = await client.get("/dashboard")
    html = resp.text
    assert "<script" in html.lower()


# -- GET /events (SSE) ------------------------------------------------------


@pytest.mark.anyio
async def test_events_returns_sse_content_type(app) -> None:  # type: ignore[no-untyped-def]
    """GET /events returns text/event-stream content type.

    SSE is a long-lived streaming connection. Instead of trying to read from
    the stream (which blocks on ASGI transport), we test the SSE bus and the
    route registration independently.
    """
    from bernstein.core.server import SSEBus

    # Verify the /events route is registered
    routes = [r.path for r in app.routes if hasattr(r, "path")]  # type: ignore[union-attr]
    assert "/events" in routes

    # Verify the SSE bus works correctly
    bus = SSEBus()
    queue = bus.subscribe()
    bus.publish("task_update", '{"id": "abc"}')
    msg = queue.get_nowait()
    assert "event: task_update" in msg
    assert '{"id": "abc"}' in msg
    bus.unsubscribe(queue)
    assert bus.subscriber_count == 0


@pytest.mark.anyio
async def test_sse_bus_fan_out() -> None:
    """SSE bus delivers events to all subscribers."""
    from bernstein.core.server import SSEBus

    bus = SSEBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    bus.publish("heartbeat", '{"ts": 1}')
    assert "heartbeat" in q1.get_nowait()
    assert "heartbeat" in q2.get_nowait()
    bus.unsubscribe(q1)
    bus.unsubscribe(q2)


# -- GET /dashboard/data ----------------------------------------------------


@pytest.mark.anyio
async def test_dashboard_data_returns_json(client: AsyncClient) -> None:
    """GET /dashboard/data returns JSON with expected top-level keys."""
    resp = await client.get("/dashboard/data")
    assert resp.status_code == 200
    data = resp.json()
    assert "stats" in data
    assert "tasks" in data
    assert "agents" in data
    assert "cost_by_role" in data
    assert "live_costs" in data


@pytest.mark.anyio
async def test_dashboard_data_stats_keys(client: AsyncClient) -> None:
    """Dashboard data stats object has the expected fields."""
    resp = await client.get("/dashboard/data")
    stats = resp.json()["stats"]
    for key in ("total", "open", "claimed", "done", "failed", "agents", "cost_usd"):
        assert key in stats, f"Missing stats key: {key}"


@pytest.mark.anyio
async def test_dashboard_data_live_costs_keys(client: AsyncClient) -> None:
    """Dashboard data live_costs has per-model, per-agent, and budget fields."""
    resp = await client.get("/dashboard/data")
    live_costs = resp.json()["live_costs"]
    for key in ("spent_usd", "budget_usd", "percentage_used", "per_model", "per_agent"):
        assert key in live_costs, f"Missing live_costs key: {key}"


@pytest.mark.anyio
async def test_dashboard_data_with_tasks(client: AsyncClient) -> None:
    """Dashboard data includes task data after creating a task."""
    # Create a task first
    await client.post("/tasks", json=TASK_PAYLOAD)
    resp = await client.get("/dashboard/data")
    data = resp.json()
    assert data["stats"]["total"] == 1
    assert data["stats"]["open"] == 1
    assert len(data["tasks"]) == 1
    assert data["tasks"][0]["title"] == "Implement parser"
    assert data["tasks"][0]["role"] == "backend"


@pytest.mark.anyio
async def test_dashboard_data_with_agent(client: AsyncClient) -> None:
    """Dashboard data reflects agent heartbeats."""
    await client.post(
        "/agents/agent-001/heartbeat",
        json={"role": "backend", "status": "working"},
    )
    resp = await client.get("/dashboard/data")
    data = resp.json()
    assert data["stats"]["agents"] == 1
    assert len(data["agents"]) == 1
    assert data["agents"][0]["id"] == "agent-001"


@pytest.mark.anyio
async def test_dashboard_data_reads_context_window_fields_from_agents_snapshot(tmp_path: Path) -> None:
    jsonl_path = tmp_path / ".sdd" / "runtime" / "tasks.jsonl"
    app = create_app(jsonl_path=jsonl_path)
    agents_path = tmp_path / ".sdd" / "runtime" / "agents.json"
    agents_path.parent.mkdir(parents=True, exist_ok=True)
    agents_path.write_text(
        json.dumps(
            {
                "ts": 1_000.0,
                "agents": [
                    {
                        "id": "agent-context",
                        "role": "backend",
                        "status": "working",
                        "model": "sonnet",
                        "pid": 123,
                        "task_ids": ["task-001"],
                        "tokens_used": 170_000,
                        "token_budget": 200_000,
                        "context_window_tokens": 200_000,
                        "context_utilization_pct": 85.0,
                        "context_utilization_alert": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    transport = ASGITransport(app=app)  # pyright: ignore[reportUnknownArgumentType]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/dashboard/data")

    assert resp.status_code == 200
    data = resp.json()
    assert data["agents"][0]["context_window_tokens"] == 200000
    assert data["agents"][0]["context_utilization_pct"] == 85.0
    assert data["agents"][0]["context_utilization_alert"] is True
    assert any("nearing context limit" in alert["message"] for alert in data["alerts"])


@pytest.mark.anyio
async def test_task_gate_report_endpoint_returns_saved_report(client: AsyncClient, jsonl_path: Path) -> None:
    """GET /tasks/{id}/gates returns the saved runtime gate report."""
    created = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = created.json()["id"]
    gates_dir = jsonl_path.parent / "gates"
    gates_dir.mkdir(parents=True, exist_ok=True)
    (gates_dir / f"{task_id}.json").write_text(
        f'{{"task_id":"{task_id}","overall_pass":true,"total_duration_ms":12,"gates_run":["lint"],'
        '"changed_files":["src/app.py"],"cache_hits":0,'
        '"results":[{"name":"lint","status":"pass","required":true,"blocked":false,"cached":false,'
        '"duration_ms":12,"details":"ok","metadata":{}}]}',
        encoding="utf-8",
    )

    resp = await client.get(f"/tasks/{task_id}/gates")
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == task_id
    assert data["results"][0]["status"] == "pass"


@pytest.mark.anyio
async def test_task_gate_report_endpoint_missing_report_returns_404(client: AsyncClient) -> None:
    """GET /tasks/{id}/gates returns 404 when the report is missing."""
    created = await client.post("/tasks", json=TASK_PAYLOAD)
    task_id = created.json()["id"]
    resp = await client.get(f"/tasks/{task_id}/gates")
    assert resp.status_code == 404


def test_gate_status_color_mapping() -> None:
    assert _gate_status_color("pass") == "green"


def test_mini_cost_sparkline_uses_recent_ten_points() -> None:
    spark = _mini_cost_sparkline([float(v) for v in range(15)], width=10)
    assert len(spark) == 10
    assert spark[0] == "▁"
    assert spark[-1] == "█"


def test_mini_cost_sparkline_handles_empty_series() -> None:
    assert _mini_cost_sparkline([], width=5) == "▁" * 5
    assert _gate_status_color("fail") == "red"
    assert _gate_status_color("timeout") == "yellow"
    assert _gate_status_color("bypassed") == "yellow"
    assert _gate_status_color("skipped") == "grey50"


def test_format_gate_report_lines() -> None:
    lines = _format_gate_report_lines(
        {
            "overall_pass": False,
            "total_duration_ms": 125,
            "cache_hits": 1,
            "changed_files": ["src/app.py"],
            "results": [
                {
                    "name": "lint",
                    "status": "fail",
                    "duration_ms": 120,
                    "cached": False,
                    "details": "ruff found 2 issues",
                }
            ],
        }
    )
    assert any("BLOCKED" in line for line in lines)
    assert any("lint: fail" in line for line in lines)
    assert any("ruff found 2 issues" in line for line in lines)


def test_agent_widget_renders_context_window_utilization() -> None:
    widget = AgentWidget(
        {
            "id": "agent-001",
            "role": "backend",
            "model": "sonnet",
            "status": "working",
            "runtime_s": 12,
            "context_window_tokens": 200_000,
            "context_utilization_pct": 84.5,
            "context_utilization_alert": True,
            "task_ids": [],
        },
        tasks={},
    )

    rendered = widget.render().plain
    assert "CTX 84.5%/200k" in rendered


def test_task_retry_count_from_title_and_description() -> None:
    assert _task_retry_count({"title": "[RETRY 2] Fix auth", "description": ""}) == 2
    assert _task_retry_count({"title": "Fix auth", "description": "[retry:3] retried"}) == 3
    assert _task_retry_count({"title": "Fix auth", "description": "plain"}) == 0


def test_build_runtime_subtitle_includes_branch_progress_and_restarts() -> None:
    subtitle = _build_runtime_subtitle(
        git_branch="main",
        elapsed_s=390,
        done=12,
        total=45,
        worktrees=5,
        restart_count=2,
    )
    assert "Running for 6m 30s" in subtitle
    assert "branch main" in subtitle
    assert "12/45 tasks (26%)" in subtitle
    assert "5 worktrees" in subtitle
    assert "2 restarts" in subtitle


def test_format_relative_age() -> None:
    assert _format_relative_age(12) == "12s ago"
    assert _format_relative_age(120) == "2m ago"
    assert _format_relative_age(7200) == "2h ago"


def test_summarize_agent_errors_uses_dead_and_nonzero_exit_code() -> None:
    count, lines = _summarize_agent_errors(
        [
            {"role": "backend", "status": "dead", "task_ids": ["task-12345678"]},
            {"role": "qa", "status": "working", "exit_code": 2, "task_ids": ["task-abcdef12"]},
            {"role": "docs", "status": "working", "exit_code": 0, "task_ids": ["task-ignore"]},
        ]
    )
    assert count == 2
    assert lines[0].startswith("BACKEND: dead")
    assert "QA: exit 2" in lines[1]
