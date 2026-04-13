"""Tests for multi-tenant workspace isolation."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from bernstein.core.cost_tracker import CostTracker
from bernstein.core.metric_collector import MetricsCollector
from bernstein.core.seed import parse_seed
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app


def _write_seed(tmp_path: Path) -> None:
    (tmp_path / "bernstein.yaml").write_text(
        'goal: "Ship multi-tenant platform"\n'
        "tenants:\n"
        "  - id: team-a\n"
        "    budget: 100\n"
        "    allowed_agents: [claude, codex]\n"
        "  - id: team-b\n"
        "    budget: 250\n"
        "    agents: [gemini]\n",
        encoding="utf-8",
    )


def _jsonl_records(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture()
def app(tmp_path: Path) -> FastAPI:
    _write_seed(tmp_path)
    application = create_app(jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl")
    application.state.reload_seed_config()
    return application


@pytest.fixture()
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client


class TestTenantSeedConfig:
    def test_parse_seed_reads_tenants(self, tmp_path: Path) -> None:
        _write_seed(tmp_path)

        config = parse_seed(tmp_path / "bernstein.yaml")

        assert [tenant.id for tenant in config.tenants] == ["team-a", "team-b"]
        assert config.tenants[0].budget_usd == pytest.approx(100.0)
        assert config.tenants[0].allowed_agents == ("claude", "codex")
        assert config.tenants[1].allowed_agents == ("gemini",)


class TestTenantTaskIsolation:
    @pytest.mark.anyio
    async def test_get_tasks_filters_by_tenant(self, client: AsyncClient, app: FastAPI) -> None:
        response_a = await client.post(
            "/tasks",
            json={"title": "Tenant A task", "description": "A", "role": "backend"},
            headers={"x-tenant-id": "team-a"},
        )
        response_b = await client.post(
            "/tasks",
            json={"title": "Tenant B task", "description": "B", "role": "backend"},
            headers={"x-tenant-id": "team-b"},
        )
        await app.state.store.flush_buffer()

        assert response_a.status_code == 201
        assert response_b.status_code == 201

        visible_a = await client.get("/tasks?tenant=team-a", headers={"x-tenant-id": "team-a"})
        visible_b = await client.get("/tasks", headers={"x-tenant-id": "team-b"})
        forbidden = await client.get("/tasks?tenant=team-a", headers={"x-tenant-id": "team-b"})

        assert [task["tenant_id"] for task in visible_a.json()] == ["team-a"]
        assert [task["tenant_id"] for task in visible_b.json()] == ["team-b"]
        assert forbidden.status_code == 403

    @pytest.mark.anyio
    async def test_task_creation_writes_tenant_backlog_file(self, client: AsyncClient, app: FastAPI) -> None:
        response = await client.post(
            "/tasks",
            json={"title": "Scoped backlog", "description": "Backlog mirror", "role": "backend"},
            headers={"x-tenant-id": "team-a"},
        )
        await app.state.store.flush_buffer()

        assert response.status_code == 201
        tenant_backlog = app.state.sdd_dir / "team-a" / "backlog" / "tasks.jsonl"
        assert tenant_backlog.exists()
        assert _jsonl_records(tenant_backlog)[-1]["tenant_id"] == "team-a"


class TestTenantMetricsAndCosts:
    def test_metrics_are_mirrored_into_tenant_metrics_dir(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / ".sdd" / "metrics"
        collector = MetricsCollector(metrics_dir=metrics_dir)
        collector.start_task("task-1", role="backend", model="sonnet", provider="openai", tenant_id="team-a")
        collector.complete_task("task-1", success=True, tokens_used=42, cost_usd=1.2, janitor_passed=True)
        collector.flush()

        tenant_metrics = list((tmp_path / ".sdd" / "team-a" / "metrics").glob("*.jsonl"))
        assert tenant_metrics
        has_team_a_record = False
        for record in _jsonl_records(tenant_metrics[0]):
            labels = record.get("labels")
            if isinstance(labels, dict):
                label_map = cast("dict[object, object]", labels)
                tenant_label = label_map.get("tenant_id")
                if tenant_label == "team-a":
                    has_team_a_record = True
                    break
        assert has_team_a_record

    @pytest.mark.anyio
    async def test_cost_routes_are_tenant_scoped(self, client: AsyncClient, app: FastAPI) -> None:
        tracker = CostTracker(run_id="run-tenant", budget_usd=500.0)
        tracker.record(
            agent_id="agent-a",
            task_id="task-a",
            model="sonnet",
            input_tokens=100,
            output_tokens=50,
            cost_usd=1.5,
            tenant_id="team-a",
        )
        tracker.record(
            agent_id="agent-b",
            task_id="task-b",
            model="opus",
            input_tokens=100,
            output_tokens=50,
            cost_usd=3.0,
            tenant_id="team-b",
        )
        tracker.save(app.state.sdd_dir)

        costs_a = await client.get("/costs?tenant=team-a", headers={"x-tenant-id": "team-a"})
        live_b = await client.get("/costs/live?tenant=team-b", headers={"x-tenant-id": "team-b"})

        assert costs_a.status_code == 200
        assert costs_a.json()["tenant_id"] == "team-a"
        assert costs_a.json()["total_spent_usd"] == pytest.approx(1.5)
        assert costs_a.json()["total_budget_usd"] == pytest.approx(100.0)

        assert live_b.status_code == 200
        assert live_b.json()["tenant_id"] == "team-b"
        assert live_b.json()["spent_usd"] == pytest.approx(3.0)
