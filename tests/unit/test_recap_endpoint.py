"""Tests for recap endpoint — enhanced recap with diff stats and quality scores."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    """Return a temporary JSONL path for each test."""
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path, tmp_path: Path):
    """Create a fresh FastAPI app per test."""
    sdd_dir = tmp_path / ".sdd"
    sdd_dir.mkdir()
    (sdd_dir / "backlog").mkdir()
    (sdd_dir / "backlog" / "open").mkdir()
    (sdd_dir / "backlog" / "claimed").mkdir()
    (sdd_dir / "backlog" / "closed").mkdir()
    (sdd_dir / "metrics").mkdir()
    (sdd_dir / "runtime").mkdir()
    # Set env var for workdir
    import os

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        app_instance = create_app(jsonl_path=jsonl_path)
        app_instance.state.workdir = tmp_path
        yield app_instance
    finally:
        os.chdir(old_cwd)


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    """Async HTTP client wired to the test app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_recap_empty(client: AsyncClient) -> None:
    """Test recap endpoint with no tasks."""
    resp = await client.get("/recap")
    assert resp.status_code == 200
    data = resp.json()

    assert data["summary"]["total"] == 0
    assert data["summary"]["completed"] == 0
    assert data["summary"]["failed"] == 0
    assert data["summary"]["success_rate"] == 0.0
    assert data["diff_stats"]["files_changed"] == 0
    assert data["quality_scores"]["average_score"] == 0
    assert data["cost_breakdown"]["total_cost_usd"] == 0.0


@pytest.mark.anyio
async def test_recap_with_tasks(client: AsyncClient) -> None:
    """Test recap endpoint with tasks."""
    # Create test tasks
    tasks = [
        {"title": "Test task 1", "description": "Test task 1", "role": "backend", "priority": 2},
        {"title": "Test task 2", "description": "Test task 2", "role": "backend", "priority": 2},
        {"title": "Test task 3", "description": "Test task 3", "role": "qa", "priority": 2},
    ]

    # Add tasks to store
    for task in tasks:
        await client.post("/tasks", json=task)

    resp = await client.get("/recap")
    assert resp.status_code == 200
    data = resp.json()

    assert data["summary"]["total"] == 3
    # All tasks start as open, so completed and failed will be 0
    assert data["summary"]["completed"] == 0
    assert data["summary"]["failed"] == 0
    assert data["summary"]["success_rate"] == 0.0


@pytest.mark.anyio
async def test_recap_with_quality_scores(client: AsyncClient, tmp_path: Path) -> None:
    """Test recap endpoint with quality score data."""
    # Create quality scores file
    metrics_dir = tmp_path / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    quality_file = metrics_dir / "quality_scores.jsonl"

    scores_data = [
        {"timestamp": "2026-03-31T10:00:00Z", "task_id": "task1", "total": 85, "breakdown": {"lint": 100, "tests": 80}},
        {"timestamp": "2026-03-31T11:00:00Z", "task_id": "task2", "total": 92, "breakdown": {"lint": 90, "tests": 95}},
        {"timestamp": "2026-03-31T12:00:00Z", "task_id": "task3", "total": 75, "breakdown": {"lint": 80, "tests": 70}},
    ]

    with quality_file.open("w", encoding="utf-8") as f:
        for score in scores_data:
            f.write(json.dumps(score) + "\n")

    resp = await client.get("/recap")
    assert resp.status_code == 200
    data = resp.json()

    assert data["quality_scores"]["average_score"] > 0
    assert "grade_distribution" in data["quality_scores"]
    assert "recent_scores" in data["quality_scores"]


@pytest.mark.anyio
async def test_recap_with_cost_data(client: AsyncClient, tmp_path: Path) -> None:
    """Test recap endpoint with cost data."""
    # Create cost data file
    metrics_dir = tmp_path / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    cost_file = metrics_dir / "costs_test.json"

    cost_data: dict[str, Any] = {
        "run_id": "test",
        "total_spent_usd": 1.25,
        "per_model": [
            {
                "model": "claude-sonnet-4-5-20250929",
                "total_cost_usd": 0.75,
                "total_tokens": 5000,
                "invocation_count": 10,
            },
            {"model": "claude-opus-4-5-20251101", "total_cost_usd": 0.50, "total_tokens": 3000, "invocation_count": 5},
        ],
        "per_agent": [
            {"agent_id": "backend", "total_cost_usd": 1.00},
            {"agent_id": "qa", "total_cost_usd": 0.25},
        ],
    }

    with cost_file.open("w", encoding="utf-8") as f:
        json.dump(cost_data, f)

    resp = await client.get("/recap")
    assert resp.status_code == 200
    data = resp.json()

    assert data["cost_breakdown"]["total_cost_usd"] == pytest.approx(1.25, rel=0.01)
    assert len(data["cost_breakdown"]["per_model"]) == 2
    assert "backend" in data["cost_breakdown"]["per_role"]
    assert "qa" in data["cost_breakdown"]["per_role"]
