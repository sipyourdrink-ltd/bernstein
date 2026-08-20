"""Tenant scoping for readers that used to list the whole task table.

Each call site gets the same pair: a caller scoped to tenant A must see a figure
that does not move when tenant B's tasks change, plus a positive control proving
A's own tasks still count (so the scoping cannot pass by returning nothing).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app
from bernstein.core.server.server_models import TaskCreate


def _write_seed(tmp_path: Path) -> None:
    (tmp_path / "bernstein.yaml").write_text(
        'goal: "Ship multi-tenant platform"\n'
        "tenants:\n"
        "  - id: team-a\n"
        "    budget: 100\n"
        "  - id: team-b\n"
        "    budget: 250\n",
        encoding="utf-8",
    )


@pytest.fixture()
def app(tmp_path: Path) -> FastAPI:
    _write_seed(tmp_path)
    application = create_app(jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl")
    application.state.reload_seed_config()
    return application


@pytest_asyncio.fixture()
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client


async def _create(app: FastAPI, title: str, tenant_id: str, **extra: object) -> None:
    await app.state.store.create(TaskCreate(title=title, description="scoping fixture", tenant_id=tenant_id, **extra))


class TestBudgetForecastTenantScope:
    @pytest.mark.asyncio
    async def test_forecast_ignores_another_tenants_tasks(self, app: FastAPI, client: AsyncClient) -> None:
        baseline = (await client.get("/quality/budget-forecast", headers={"X-Tenant-Id": "team-a"})).json()

        await _create(app, "team-b work", "team-b")

        after = await client.get("/quality/budget-forecast", headers={"X-Tenant-Id": "team-a"})
        assert after.status_code == 200
        assert after.json()["task_count"] == baseline["task_count"]

    @pytest.mark.asyncio
    async def test_forecast_still_counts_own_tasks(self, app: FastAPI, client: AsyncClient) -> None:
        baseline = (await client.get("/quality/budget-forecast", headers={"X-Tenant-Id": "team-a"})).json()[
            "task_count"
        ]

        await _create(app, "team-a work", "team-a")

        after = await client.get("/quality/budget-forecast", headers={"X-Tenant-Id": "team-a"})
        assert after.json()["task_count"] == baseline + 1


class TestHealthCheckTenantScope:
    @pytest.mark.asyncio
    async def test_health_ignores_another_tenants_tasks(self, app: FastAPI, client: AsyncClient) -> None:
        baseline = (await client.get("/health", headers={"X-Tenant-Id": "team-a"})).json()

        await _create(app, "team-b work", "team-b")

        after = await client.get("/health", headers={"X-Tenant-Id": "team-a"})
        assert after.status_code == 200
        assert after.json()["task_count"] == baseline["task_count"]

    @pytest.mark.asyncio
    async def test_health_still_counts_own_tasks(self, app: FastAPI, client: AsyncClient) -> None:
        baseline = (await client.get("/health", headers={"X-Tenant-Id": "team-a"})).json()["task_count"]

        await _create(app, "team-a work", "team-a")

        after = await client.get("/health", headers={"X-Tenant-Id": "team-a"})
        assert after.json()["task_count"] == baseline + 1
